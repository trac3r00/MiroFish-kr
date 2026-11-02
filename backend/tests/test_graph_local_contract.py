import pytest
from zep_cloud import NotFoundError

import app.llm as llm
from app.graph_local import LocalZepClient
from app.graph_local import embed
from app.services.graph_builder import GraphBuilderService
from app.services.zep_entity_reader import ZepEntityReader
from app.services.zep_tools import ZepToolsService
from app.utils.zep_paging import fetch_all_edges, fetch_all_nodes

ONTOLOGY = {
    "entity_types": [
        {"name": "City", "description": "A city government.", "attributes": [
            {"name": "mayor", "type": "text", "description": "Current mayor"}]},
        {"name": "Service", "description": "A public service.", "attributes": []},
    ],
    "edge_types": [
        {"name": "ANNOUNCES", "description": "Announces a policy about.",
         "source_targets": [{"source": "City", "target": "Service"}], "attributes": []},
    ],
}
FACT = "한빛시가 지하철 기본요금 인상안을 발표했다"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("GRAPH_LOCAL_EMBEDDINGS", "off")
    embed.set_encoder(None)
    local = LocalZepClient(db_path=str(tmp_path / "graph.db"))
    yield local
    local.worker.wait_idle(timeout=10)
    local.store.close()


def _extraction(messages, *, stage, **_kwargs):
    text = messages[-1]["content"]
    if "지하철" in text:
        return {
            "entities": [
                {"name": "한빛시", "type": "City", "summary": "광역시", "attributes": {"mayor": "김한빛"}},
                {"name": "지하철", "type": "Service", "summary": "도시철도"},
            ],
            "edges": [{"source": "한빛시", "target": "지하철", "type": "ANNOUNCES", "fact": FACT,
                       "valid_at": "2026-10-01T00:00:00Z"}],
        }
    return {"entities": [{"name": "한빛시", "type": "City", "summary": "시청 소재지"}], "edges": []}


def _builder(client):
    builder = object.__new__(GraphBuilderService)
    builder.client = client
    return builder


def test_graph_builder_call_sequence_against_local_client(client, monkeypatch):
    monkeypatch.setattr(llm, "chat_json", _extraction)
    builder = _builder(client)

    graph_id = builder.create_graph("Contract graph", graph_id="contract_graph")
    assert client.graph.get(graph_id).graph_id == graph_id
    builder.set_ontology(graph_id, ONTOLOGY)
    stored = client.store.get_ontology(graph_id)
    assert [e["name"] for e in stored["entity_types"]] == ["City", "Service"]
    assert stored["entity_types"][0]["description"] == "A city government."
    assert stored["entity_types"][0]["attributes"] == [{"name": "mayor", "description": "Current mayor"}]
    assert stored["edge_types"][0]["source_targets"] == [{"source": "City", "target": "Service"}]

    chunks = ["한빛시는 지하철 요금을 올린다.", "한빛시 시청은 중앙로에 있다."]
    journal = []
    submission = builder.add_text_batches(
        graph_id, chunks, batch_created_callback=lambda b, o: journal.append((b, o))
    )
    assert journal[0] == (None, submission.operation_id)
    assert journal[1] == (submission.batch_id, submission.operation_id)
    assert len(submission.episode_uuids) == 2
    assert builder._find_batch_by_operation_id(graph_id, submission.operation_id).batch_id == submission.batch_id

    assert client.worker.wait_idle(timeout=10)
    episode_uuids = builder._wait_for_batch(submission, timeout=5)
    assert episode_uuids == submission.episode_uuids
    builder._wait_for_episodes(episode_uuids, timeout=5)
    for episode_uuid in episode_uuids:
        assert client.graph.episode.get(uuid_=episode_uuid).processed is True

    summary = client.batch.get(batch_id=submission.batch_id)
    assert summary.status == "succeeded"
    assert summary.progress.percent_complete == 100.0
    assert summary.progress.succeeded_items == 2

    data = builder.get_graph_data(graph_id)
    assert data["node_count"] == 2
    assert data["edge_count"] == 1
    city = next(n for n in data["nodes"] if n["name"] == "한빛시")
    assert city["labels"] == ["Entity", "City"]
    assert city["attributes"] == {"mayor": "김한빛"}
    assert city["summary"] == "광역시 시청 소재지"
    edge = data["edges"][0]
    assert edge["name"] == "ANNOUNCES"
    assert edge["fact"] == FACT
    assert edge["source_node_name"] == "한빛시" and edge["target_node_name"] == "지하철"
    assert edge["valid_at"] == "2026-10-01T00:00:00Z"
    assert edge["episodes"] == [episode_uuids[0]]

    info = builder._get_graph_info(graph_id)
    assert (info.node_count, info.edge_count, sorted(info.entity_types)) == (2, 1, ["City", "Service"])

    reader = object.__new__(ZepEntityReader)
    reader.client = client
    filtered = reader.filter_defined_entities(graph_id)
    assert sorted(e.name for e in filtered.entities) == ["지하철", "한빛시"]
    entity = reader.get_entity_with_context(graph_id, city["uuid"])
    assert entity.related_edges[0]["direction"] == "outgoing"
    assert entity.related_nodes[0]["name"] == "지하철"
    assert reader.get_entity_with_context(graph_id, "missing-node") is None

    tools = object.__new__(ZepToolsService)
    tools.client = client
    result = tools.search_graph(graph_id, "요금 인상", limit=5, scope="edges")
    assert result.facts == [FACT]


def test_graph_add_episode_is_processed_by_worker(client, monkeypatch):
    monkeypatch.setattr(llm, "chat_json", _extraction)
    client.graph.create(graph_id="sim", name="sim", description="d")
    episode = client.graph.add(
        graph_id="sim", type="text", data="에이전트가 지하철 요금 글을 올렸다",
        created_at="2026-11-01T09:00:00Z", source_description="activity", metadata={"round": 1},
    )
    assert episode.uuid_
    assert client.worker.wait_idle(timeout=10)
    fetched = client.graph.episode.get(uuid_=episode.uuid_)
    assert fetched.processed is True
    assert fetched.metadata == {"round": 1}
    edges = fetch_all_edges(client, "sim")
    assert [e.created_at for e in edges] == ["2026-11-01T09:00:00Z"]


def test_delete_isolates_graphs_and_missing_resources_raise_not_found(client, monkeypatch):
    monkeypatch.setattr(llm, "chat_json", _extraction)
    for graph_id in ("keep", "drop"):
        client.graph.create(graph_id=graph_id, name=graph_id, description="d")
        client.graph.add(graph_id=graph_id, type="text", data="한빛시 지하철")
    assert client.worker.wait_idle(timeout=10)
    dropped_nodes = [n.uuid_ for n in fetch_all_nodes(client, "drop")]
    kept_nodes = fetch_all_nodes(client, "keep")
    assert len(dropped_nodes) == 2 and len(kept_nodes) == 2

    client.graph.delete(graph_id="drop")

    assert [n.uuid_ for n in fetch_all_nodes(client, "keep")] == [n.uuid_ for n in kept_nodes]
    assert len(fetch_all_edges(client, "keep")) == 1
    assert client.graph.search(graph_id="keep", query="요금 인상", scope="edges").edges
    for call in (
        lambda: client.graph.get("drop"),
        lambda: client.graph.delete(graph_id="drop"),
        lambda: fetch_all_nodes(client, "drop"),
        lambda: client.graph.node.get(uuid_=dropped_nodes[0]),
        lambda: client.graph.node.get_edges(node_uuid=dropped_nodes[0]),
        lambda: client.graph.edge.get(uuid_="missing"),
        lambda: client.graph.episode.get(uuid_="missing"),
        lambda: client.graph.search(graph_id="drop", query="요금", scope="edges"),
        lambda: client.graph.add(graph_id="drop", type="text", data="x"),
        lambda: client.batch.get(batch_id="missing"),
    ):
        with pytest.raises(NotFoundError):
            call()


def test_malformed_and_failing_llm_outputs_keep_batches_terminal(client, monkeypatch):
    def broken(messages, *, stage, **_kwargs):
        text = messages[-1]["content"]
        if "bad-json" in text:
            raise llm.LLMResponseError("LLM returned invalid JSON")
        if "transport" in text:
            raise ConnectionError("provider down")
        return ["not", "an", "object"]

    monkeypatch.setattr(llm, "chat_json", broken)
    client.graph.create(graph_id="g", name="g", description="d")
    batch = client.batch.create(metadata={"graph_id": "g"})
    from zep_cloud import BatchAddItem

    client.batch.add(batch_id=batch.batch_id, items=[
        BatchAddItem(type="graph_episode", graph_id="g", data=text, data_type="text")
        for text in ("bad-json", "transport", "list")
    ])
    assert client.batch.get(batch_id=batch.batch_id).status == "draft"
    client.batch.process(batch_id=batch.batch_id)
    assert client.worker.wait_idle(timeout=10)

    summary = client.batch.get(batch_id=batch.batch_id)
    assert summary.status == "partial"
    assert (summary.progress.succeeded_items, summary.progress.failed_items) == (2, 1)
    items = client.batch.list_items(batch_id=batch.batch_id, limit=2)
    assert len(items.items) == 2 and items.next_cursor is not None
    rest = client.batch.list_items(batch_id=batch.batch_id, limit=2, cursor=items.next_cursor)
    assert rest.next_cursor is None
    statuses = [i.status for i in items.items + rest.items]
    assert statuses == ["succeeded", "failed", "succeeded"]
    assert "provider down" in (items.items[1].error or "")
    assert all(client.graph.episode.get(uuid_=i.episode_uuid).processed for i in items.items + rest.items)
    assert fetch_all_nodes(client, "g") == []
