import random
import unicodedata

import numpy as np
import pytest
from zep_cloud import BatchAddItem

import app.llm as llm
from app.graph_local import LocalZepClient, embed
from app.graph_local.store import new_uuid
from app.utils.zep_paging import fetch_all_edges, fetch_all_nodes

SEED = 20261102
ONTOLOGY = {
    "entity_types": [
        {"name": "Person", "description": "A person.", "attributes": [{"name": "age", "description": "Age"}]},
        {"name": "Org", "description": "An organization.", "attributes": []},
    ],
    "edge_types": [
        {"name": "WORKS_FOR", "description": "Employment.", "attributes": [],
         "source_targets": [{"source": "Person", "target": "Org"}]},
    ],
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("GRAPH_LOCAL_EMBEDDINGS", "off")
    embed.set_encoder(None)
    local = LocalZepClient(db_path=str(tmp_path / "graph.db"))
    yield local
    local.worker.wait_idle(timeout=30)
    local.store.close()
    embed.set_encoder(None)


def _new_graph(client, graph_id):
    client.graph.create(graph_id=graph_id, name=graph_id, description="property")
    client.store.set_ontology(graph_id, ONTOLOGY)


def test_cursor_paging_returns_exactly_the_inserted_rows(client):
    rng = random.Random(SEED)
    graphs = [f"page_{i}" for i in range(200)]
    for graph_id in graphs:
        client.graph.create(graph_id=graph_id, name=graph_id, description="d")
    inserted = {g: {"nodes": [], "edges": []} for g in graphs}
    plan = [(g, kind) for g in graphs for kind in ("nodes", "edges") for _ in range(rng.randint(0, 40))]
    rng.shuffle(plan)
    with client.store.transaction() as conn:
        for graph_id, kind in plan:
            row_uuid = new_uuid()
            if kind == "nodes":
                client.store.insert_node(conn, {
                    "uuid": row_uuid, "graph_id": graph_id, "name": f"n{row_uuid[:6]}",
                    "labels": ["Entity"], "summary": "", "attributes": {}, "created_at": "2026-01-01T00:00:00Z",
                })
            else:
                client.store.insert_edge(conn, {
                    "uuid": row_uuid, "graph_id": graph_id, "name": "RELATED_TO", "fact": f"f{row_uuid[:6]}",
                    "source_node_uuid": "s", "target_node_uuid": "t", "attributes": {},
                    "created_at": "2026-01-01T00:00:00Z", "episodes": [],
                })
            inserted[graph_id][kind].append(row_uuid)

    executed = 0
    for graph_id in graphs:
        for kind, api, fetch_all in (
            ("nodes", client.graph.node, fetch_all_nodes),
            ("edges", client.graph.edge, fetch_all_edges),
        ):
            limit = rng.randint(1, 25)
            expected = inserted[graph_id][kind]
            seen, cursor, pages = [], None, 0
            while True:
                kwargs = {"limit": limit} if cursor is None else {"limit": limit, "cursor": cursor}
                response = api.with_raw_response.get_by_graph_id(graph_id, **kwargs)
                pages += 1
                assert len(response.data) <= limit
                seen.extend(item.uuid_ for item in response.data)
                cursor = response.headers.get("zep-next-cursor")
                if cursor is None:
                    break
                assert len(response.data) == limit
            assert seen == expected
            assert len(set(seen)) == len(seen)
            assert pages == max(1, -(-len(expected) // limit))
            assert [item.uuid_ for item in fetch_all(client, graph_id, page_size=limit)] == expected
        executed += 1
    assert executed == len(graphs) == 200


JUNK_ENTITIES = [None, "string", 7, {"name": ""}, {"name": "   "}, {"name": 3}, {"type": "Person"}]
NAMES = ["김민수", "이서연", "한빛전자", "Acme Corp", "박지훈", "정다은", "서울교통공사", "Lee Jun", "최유진", "한빛시"]


def _generate_case(rng, case):
    kind = rng.choices(["valid", "envelope", "llm_json", "llm_down"], weights=[6, 2, 1, 1])[0]
    if kind == "envelope":
        payload = rng.choice([None, [], "text", 42, {"entities": "x", "edges": []},
                              {"entities": [], "edges": {"a": 1}}, {"entities": None}])
        return kind, payload, None
    if kind != "valid":
        return kind, None, None

    names = rng.sample(NAMES, rng.randint(0, 6))
    entities, expected_nodes, payload_entities = {}, {}, []
    for name in names:
        entity_type = rng.choice(["Person", "Org", "Alien", None, 5])
        age = rng.choice([None, 31, "40", [1]])
        entity = {"name": name, "type": entity_type, "summary": f"{name} summary"}
        if age is not None:
            entity["attributes"] = {"age": age}
        payload_entities.append(entity)
        labels = ["Entity"] + ([entity_type] if entity_type in ("Person", "Org") else [])
        attributes = {"age": str(age)} if isinstance(age, (int, str)) else {}
        expected_nodes[name] = (tuple(labels), f"{name} summary", attributes)
        entities[name] = entity
    payload_entities.extend(rng.sample(JUNK_ENTITIES, rng.randint(0, 3)))
    rng.shuffle(payload_entities)

    payload_edges, expected_edges = [], set()
    pairs = [(a, b) for a in names for b in names if a != b]
    for index, (source, target) in enumerate(rng.sample(pairs, min(len(pairs), rng.randint(0, 5)))):
        edge_type = rng.choice(["WORKS_FOR", "BOGUS", None])
        fact = f"case {case} fact {index}"
        payload_edges.append({"source": source, "target": target, "type": edge_type, "fact": fact,
                              "valid_at": rng.choice([None, "2026-01-01T00:00:00Z", "not a date"])})
        expected_edges.add((source, target, edge_type if edge_type == "WORKS_FOR" else "RELATED_TO", fact))
    if names and rng.random() < 0.3:
        ghost = f"유령{case}"
        payload_edges.append({"source": names[0], "target": ghost, "type": "WORKS_FOR", "fact": f"ghost {case}"})
        expected_nodes[ghost] = (("Entity",), "", {})
        expected_edges.add((names[0], ghost, "WORKS_FOR", f"ghost {case}"))
    junk_edges = [None, "edge", {"source": "a"}, {"source": "x", "target": "x", "fact": "self"},
                  {"source": "a", "target": "b", "fact": ""}, {"source": 1, "target": 2, "fact": "n"}]
    payload_edges.extend(rng.sample(junk_edges, rng.randint(0, 3)))
    rng.shuffle(payload_edges)
    payload = {"entities": payload_entities, "edges": payload_edges}
    return kind, payload, (expected_nodes, expected_edges)


def test_extraction_survives_generated_outputs_and_builds_expected_graph(client, monkeypatch):
    rng = random.Random(SEED + 1)
    cases = {case: _generate_case(rng, case) for case in range(300)}

    def fake_chat_json(messages, *, stage, **_kwargs):
        case = int(messages[-1]["content"].rsplit("case-", 1)[1].split()[0])
        kind, payload, _ = cases[case]
        if kind == "llm_json":
            raise llm.LLMResponseError("LLM returned invalid JSON")
        if kind == "llm_down":
            raise ConnectionError("provider down")
        return payload

    monkeypatch.setattr(llm, "chat_json", fake_chat_json)
    batches = {}
    for case in cases:
        graph_id = f"extract_{case}"
        _new_graph(client, graph_id)
        batch = client.batch.create(metadata={"graph_id": graph_id})
        client.batch.add(batch_id=batch.batch_id, items=[BatchAddItem(
            type="graph_episode", graph_id=graph_id, data=f"episode case-{case} text", data_type="text")])
        batches[case] = batch.batch_id
    for batch_id in batches.values():
        client.batch.process(batch_id=batch_id)
    assert client.worker.wait_idle(timeout=60)
    assert client.worker._thread.is_alive()

    executed = {"valid": 0, "envelope": 0, "llm_json": 0, "llm_down": 0}
    for case, (kind, _payload, expected) in cases.items():
        graph_id = f"extract_{case}"
        summary = client.batch.get(batch_id=batches[case])
        item = client.batch.list_items(batch_id=batches[case]).items[0]
        assert client.graph.episode.get(uuid_=item.episode_uuid).processed is True
        nodes = fetch_all_nodes(client, graph_id)
        edges = fetch_all_edges(client, graph_id)
        if kind == "llm_down":
            assert (summary.status, item.status) == ("failed", "failed")
            assert "provider down" in item.error
        else:
            assert (summary.status, item.status) == ("succeeded", "succeeded")
        if kind in ("envelope", "llm_json"):
            assert "malformed" in item.error
        if kind != "valid":
            assert nodes == [] and edges == []
        else:
            expected_nodes, expected_edges = expected
            assert item.error is None
            got_nodes = {n.name: (tuple(n.labels), n.summary, n.attributes) for n in nodes}
            assert got_nodes == expected_nodes
            names = {n.uuid_: n.name for n in nodes}
            got_edges = {(names[e.source_node_uuid], names[e.target_node_uuid], e.name, e.fact) for e in edges}
            assert got_edges == expected_edges
            assert len(edges) == len(expected_edges)
        executed[kind] += 1
    assert sum(executed.values()) == 300
    assert all(count > 0 for count in executed.values()), executed


def _variant(rng, name):
    text = name
    if rng.random() < 0.5:
        text = unicodedata.normalize("NFKC", text).translate(
            {c: c + 0xFEE0 for c in range(0x21, 0x7F)})
    if rng.random() < 0.5:
        text = text.upper() if rng.random() < 0.5 else text.lower()
    return rng.choice(["", " ", "  ", "\u3000"]) + text + rng.choice(["", " ", "\t"])


def test_entity_resolution_merges_normalized_names(client, monkeypatch):
    queue = []
    monkeypatch.setattr(llm, "chat_json", lambda messages, *, stage, **_k: queue.pop(0))
    _new_graph(client, "resolve")
    for payload in (
        {"entities": [{"name": "한빛시", "type": "Org", "summary": "광역시"}]},
        {"entities": [{"name": " 한빛시 ", "type": "Person", "summary": "시청"}]},
        {"entities": [{"name": "ＨＡＮＢＩＴ Corp", "summary": "fullwidth"}]},
        {"entities": [{"name": "hanbit  corp", "type": "Org"}]},
    ):
        queue.append(payload)
        client.graph.add(graph_id="resolve", type="text", data="x")
    assert client.worker.wait_idle(timeout=10)
    nodes = {n.name: n for n in fetch_all_nodes(client, "resolve")}
    assert set(nodes) == {"ＨＡＮＢＩＴ Corp", "한빛시"}
    assert nodes["한빛시"].labels == ["Entity", "Org", "Person"]
    assert nodes["한빛시"].summary == "광역시 시청"
    assert nodes["ＨＡＮＢＩＴ Corp"].labels == ["Entity", "Org"]

    rng = random.Random(SEED + 2)
    attempted = executed = 0
    for trial in range(50):
        attempted += 1
        graph_id = f"resolve_{trial}"
        _new_graph(client, graph_id)
        bases = rng.sample(["Acme", "Zep Labs", "한빛시", "OpenData", "서울"], rng.randint(1, 5))
        for _ in range(rng.randint(1, 4)):
            queue.append({"entities": [{"name": _variant(rng, b), "type": "Org"} for b in bases]})
            client.graph.add(graph_id=graph_id, type="text", data="x")
        assert client.worker.wait_idle(timeout=10)
        assert len(fetch_all_nodes(client, graph_id)) == len(bases)
        executed += 1
    assert attempted == executed == 50


class _AliasEncoder:
    name = "fake-alias"
    aliases = {"seoul city": 0, "서울시": 0, "busan": 1}

    def encode(self, texts, normalize_embeddings=True):
        vectors = []
        for text in texts:
            body = text.split(": ", 1)[1] if ": " in text else text
            key = body.split(":")[0].strip().casefold()
            vector = np.zeros(8, dtype=np.float32)
            vector[self.aliases.get(key, 2 + sum(map(ord, key)) % 6)] = 1.0
            vectors.append(vector)
        return np.stack(vectors)


def test_embedding_similarity_merges_aliases(client, monkeypatch):
    monkeypatch.delenv("GRAPH_LOCAL_EMBEDDINGS")
    embed.set_encoder(_AliasEncoder())
    assert embed.status()["mode"] == "fts+embeddings"
    queue = [
        {"entities": [{"name": "서울시", "type": "Org", "summary": "capital"}]},
        {"entities": [{"name": "Seoul City", "type": "Org"}, {"name": "Busan", "type": "Org"}]},
    ]
    monkeypatch.setattr(llm, "chat_json", lambda messages, *, stage, **_k: queue.pop(0))
    _new_graph(client, "alias")
    for _ in range(2):
        client.graph.add(graph_id="alias", type="text", data="x")
    assert client.worker.wait_idle(timeout=10)
    assert sorted(n.name for n in fetch_all_nodes(client, "alias")) == ["Busan", "서울시"]
    found = client.graph.search(graph_id="alias", query="Seoul City", scope="nodes", limit=1)
    assert [n.name for n in found.nodes] == ["서울시"]


FACTS = [
    "한빛시가 지하철 기본요금 인상안을 발표했다",
    "시민단체는 버스 노선 개편에 반대했다",
    "한빛전자는 신규 공장 착공을 연기했다",
    "교육청이 급식 단가 조정을 검토 중이다",
    "The council approved the new bike lanes",
    "교통공사는 무임승차 연령 기준 상향을 논의했다",
    "청년 연령 기준이 다음 달 바뀐다",
]


def test_korean_search_finds_facts_by_short_terms(client, monkeypatch):
    def fake(messages, *, stage, **_kwargs):
        index = int(messages[-1]["content"].rsplit("fact-", 1)[1].split()[0])
        return {"entities": [{"name": f"주체{index}"}, {"name": f"대상{index}"}],
                "edges": [{"source": f"주체{index}", "target": f"대상{index}", "fact": FACTS[index]}]}

    monkeypatch.setattr(llm, "chat_json", fake)
    _new_graph(client, "ko")
    _new_graph(client, "other")
    for index in range(len(FACTS)):
        client.graph.add(graph_id="ko", type="text", data=f"fact-{index} body")
    client.graph.add(graph_id="other", type="text", data="fact-0 body")
    assert client.worker.wait_idle(timeout=10)

    edges = client.graph.search(graph_id="ko", query="요금 인상", scope="edges", limit=10, reranker="cross_encoder").edges
    assert edges[0].fact == FACTS[0]
    assert {e.graph_id for e in edges} == {"ko"}
    assert [e.fact for e in edges] == [FACTS[0]]
    mixed = client.graph.search(graph_id="ko", query="무임승차 연령", scope="edges", limit=10).edges
    assert [e.fact for e in mixed] == [FACTS[5], FACTS[6]]
    assert client.graph.search(graph_id="ko", query="기본요금", scope="edges").edges[0].fact == FACTS[0]
    assert client.graph.search(graph_id="ko", query="BIKE lanes", scope="edges").edges[0].fact == FACTS[4]
    assert client.graph.search(graph_id="ko", query="우주선", scope="edges").edges == []
    assert [n.name for n in client.graph.search(graph_id="ko", query="주체3", scope="nodes").nodes] == ["주체3"]

    rng = random.Random(SEED + 3)
    attempted = executed = 0
    for _ in range(200):
        attempted += 1
        fact = rng.choice(FACTS)
        words = [w for w in fact.split() if len(w) >= 2]
        word = rng.choice(words)
        start = rng.randint(0, len(word) - 2)
        term = word[start:start + rng.randint(2, len(word) - start)]
        expected = {f for f in FACTS if term.casefold() in f.casefold()}
        found = client.graph.search(graph_id="ko", query=term, scope="edges", limit=10).edges
        assert {e.fact for e in found} == expected, term
        executed += 1
    assert attempted == executed == 200
