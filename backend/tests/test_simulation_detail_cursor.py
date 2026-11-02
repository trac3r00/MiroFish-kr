import json
import random
import threading
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import create_app
from app.api import simulation as simulation_api
from app.config import Config
from app.services import persona_archetypes
from app.services.simulation_runner import (
    RunnerStatus,
    SimulationRunState,
    SimulationRunner,
)
from app.utils import locale as locale_module


PLATFORM_RANK = {"twitter": 0, "reddit": 1}
KEYS_FILE = (
    Path(__file__).resolve().parents[2]
    / ".omo" / "work" / "keys" / "api-simulation-runner.json"
)


@pytest.fixture
def sim_root(tmp_path, monkeypatch):
    monkeypatch.setattr(SimulationRunner, "RUN_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(Config, "OASIS_SIMULATION_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(SimulationRunner, "_run_states", {})
    monkeypatch.setattr(SimulationRunner, "_action_indexes", {})
    monkeypatch.setattr(SimulationRunner, "_terminal_since", {})
    monkeypatch.setattr(SimulationRunner, "_finalization_locks", {})
    monkeypatch.setattr(simulation_api, "_agent_llm_meta_cache", OrderedDict())
    return tmp_path


@pytest.fixture
def client():
    app = create_app()
    app.config.update(TESTING=True)
    return app.test_client()


class LogGenerator:
    """Appends action/event lines to two platform logs and keeps the oracle.

    The oracle is the generator's own bookkeeping: actions completed since the
    previous poll form one batch, ordered by (timestamp, platform, line order).
    """

    def __init__(self, root: Path, simulation_id: str, rng: random.Random):
        self.rng = rng
        self.paths = {}
        for platform in PLATFORM_RANK:
            path = root / simulation_id / platform / "actions.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("", encoding="utf-8")
            self.paths[platform] = path
        self.pending_partial: dict = {platform: None for platform in PLATFORM_RANK}
        self.batch = []
        self.oracle = []
        self.line_no = {platform: 0 for platform in PLATFORM_RANK}
        self.next_uid = 0

    def _append(self, platform, text):
        with open(self.paths[platform], "a", encoding="utf-8") as handle:
            handle.write(text)

    def append_burst(self, count):
        for _ in range(count):
            platform = self.rng.choice(list(PLATFORM_RANK))
            partial = self.pending_partial[platform]
            if partial is not None:
                self._append(platform, partial["rest"])
                self._complete(platform, partial["record"])
                self.pending_partial[platform] = None
                continue
            if self.rng.random() < 0.1:
                event = {"event_type": "round_end", "round": 1, "simulated_hours": 1}
                self._append(platform, json.dumps(event) + "\n")
                self.line_no[platform] += 1
                continue
            uid = self.next_uid
            self.next_uid += 1
            record = {
                "round": self.rng.randint(1, 5),
                # coarse timestamps force ties and cross-platform disorder
                "timestamp": f"2026-11-02T10:{self.rng.randint(0, 59):02d}:00",
                "agent_id": self.rng.randint(0, 9),
                "agent_name": f"agent-{uid}",
                "action_type": "CREATE_POST",
                "action_args": {"uid": uid},
                "success": True,
            }
            line = json.dumps(record) + "\n"
            if self.rng.random() < 0.08:
                cut = self.rng.randint(1, len(line) - 2)
                self._append(platform, line[:cut])
                self.pending_partial[platform] = {"rest": line[cut:], "record": record}
            else:
                self._append(platform, line)
                self._complete(platform, record)

    def finish_partials(self):
        for platform, partial in self.pending_partial.items():
            if partial is not None:
                self._append(platform, partial["rest"])
                self._complete(platform, partial["record"])
                self.pending_partial[platform] = None

    def _complete(self, platform, record):
        self.line_no[platform] += 1
        self.batch.append((record["timestamp"], PLATFORM_RANK[platform],
                           self.line_no[platform], record["action_args"]["uid"], platform))

    def on_poll(self):
        self.batch.sort()
        self.oracle.extend((uid, platform) for *_rest, uid, platform in self.batch)
        self.batch = []


def _poll(client, simulation_id, after, limit, platform=None):
    query = {"after": after, "limit": limit}
    if platform:
        query["platform"] = platform
    response = client.get(f"/api/simulation/{simulation_id}/run-status/detail", query_string=query)
    assert response.status_code == 200, response.json
    return response.json["data"]


def test_property_cursor_pages_equal_generated_order_without_duplicates(sim_root, client):
    rng = random.Random(20261102)
    simulation_id = "sim_cursor"
    generator = LogGenerator(sim_root, simulation_id, rng)
    collected, cursors = [], []
    state = {"cursor": 0, "polls": 0}

    def poll_once():
        generator.on_poll()
        data = _poll(client, simulation_id, state["cursor"], rng.randint(1, 60))
        state["polls"] += 1
        assert data["cursor_reset"] is False
        assert data["next_cursor"] >= state["cursor"]
        for action in data["actions"]:
            collected.append((action["action_args"]["uid"], action["platform"]))
            cursors.append(action["cursor"])
        state["cursor"] = data["next_cursor"]
        return data

    while generator.next_uid < 400:
        generator.append_burst(rng.randint(0, 25))
        poll_once()
    generator.finish_partials()
    while poll_once()["has_more"]:
        pass

    executed = len(generator.oracle)
    assert executed >= 300
    assert {platform for _uid, platform in generator.oracle} == {"twitter", "reddit"}
    assert state["polls"] > 20
    assert collected == generator.oracle
    assert len({uid for uid, _ in collected}) == len(collected)
    assert cursors == list(range(1, len(cursors) + 1))
    assert state["cursor"] == executed


def test_platform_filtered_pages_follow_global_cursor(sim_root, client):
    rng = random.Random(7)
    simulation_id = "sim_cursor_platform"
    generator = LogGenerator(sim_root, simulation_id, rng)
    generator.append_burst(150)
    generator.finish_partials()
    generator.on_poll()

    collected, cursor = [], 0
    while True:
        data = _poll(client, simulation_id, cursor, rng.randint(1, 15), platform="reddit")
        collected.extend(a["action_args"]["uid"] for a in data["actions"])
        assert all(a["platform"] == "reddit" for a in data["actions"])
        cursor = data["next_cursor"]
        if not data["has_more"]:
            break
    assert collected == [uid for uid, platform in generator.oracle if platform == "reddit"]


def test_cursor_beyond_total_and_invalid_params(sim_root, client):
    simulation_id = "sim_small"
    log = sim_root / simulation_id / "twitter" / "actions.jsonl"
    log.parent.mkdir(parents=True)
    log.write_text(json.dumps({"agent_id": 1, "timestamp": "t", "round": 1}) + "\n", encoding="utf-8")

    data = _poll(client, simulation_id, 5, 10)
    assert data["cursor_reset"] is True and data["next_cursor"] == 1 and data["actions"] == []

    url = f"/api/simulation/{simulation_id}/run-status/detail"
    for query in ({"after": "x"}, {"after": -1}, {"after": 0, "limit": 0}, {"after": 0, "platform": "x"}):
        assert client.get(url, query_string=query).status_code == 400

    big = client.get(url, query_string={"after": 0, "limit": 9999})
    assert big.status_code == 200 and big.json["data"]["cursor_total"] == 1


def test_log_rewrite_changes_epoch(sim_root, client):
    simulation_id = "sim_epoch"
    log = sim_root / simulation_id / "reddit" / "actions.jsonl"
    log.parent.mkdir(parents=True)
    line = json.dumps({"agent_id": 1, "timestamp": "t", "round": 1}) + "\n"
    log.write_text(line * 3, encoding="utf-8")
    first = _poll(client, simulation_id, 0, 10)
    log.write_text(line, encoding="utf-8")
    second = _poll(client, simulation_id, 0, 10)
    assert first["cursor_total"] == 3 and second["cursor_total"] == 1
    assert first["cursor_epoch"] != second["cursor_epoch"]


def _write_run(sim_root, simulation_id, actions):
    for platform in PLATFORM_RANK:
        path = sim_root / simulation_id / platform / "actions.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "".join(json.dumps(a) + "\n" for a in actions if a["_p"] == platform),
            encoding="utf-8",
        )
    SimulationRunner._save_run_state(SimulationRunState(
        simulation_id=simulation_id,
        runner_status=RunnerStatus.RUNNING,
        current_round=2,
    ))


def test_omitted_after_keeps_old_full_shape(sim_root, client):
    simulation_id = "sim_old"
    actions = [
        {"_p": "twitter", "agent_id": 1, "timestamp": "2026-01-01T00:00:01", "round": 1},
        {"_p": "reddit", "agent_id": 2, "timestamp": "2026-01-01T00:00:03", "round": 2},
        {"_p": "twitter", "agent_id": 3, "timestamp": "2026-01-01T00:00:02", "round": 2},
    ]
    _write_run(sim_root, simulation_id, actions)
    url = f"/api/simulation/{simulation_id}/run-status/detail"

    data = client.get(url).json["data"]
    for key in ("all_actions", "twitter_actions", "reddit_actions", "recent_actions", "rounds_count", "runner_status"):
        assert key in data
    assert "actions" not in data and "next_cursor" not in data
    assert [a["agent_id"] for a in data["all_actions"]] == [2, 3, 1]
    assert [a["agent_id"] for a in data["twitter_actions"]] == [3, 1]
    assert [a["agent_id"] for a in data["reddit_actions"]] == [2]
    assert [a["agent_id"] for a in data["recent_actions"]] == [2, 3]
    assert "cursor" not in data["all_actions"][0]

    compact = client.get(url, query_string={"compact": "1"}).json["data"]
    assert "twitter_actions" not in compact and "reddit_actions" not in compact
    assert compact["all_actions"] == data["all_actions"]

    filtered = client.get(url, query_string={"platform": "reddit"}).json["data"]
    assert [a["agent_id"] for a in filtered["all_actions"]] == [2]
    assert filtered["twitter_actions"] == []


def test_config_and_status_expose_llm_models_and_agent_fields(sim_root, client, monkeypatch):
    simulation_id = "sim_models"
    _write_run(sim_root, simulation_id, [
        {"_p": "twitter", "agent_id": 1, "timestamp": "t1", "round": 1},
        {"_p": "reddit", "agent_id": 2, "timestamp": "t2", "round": 1},
    ])
    config = {
        "agent_configs": [
            {"agent_id": 1, "llm_profile": "default", "archetype": "busy_worker"},
            {"agent_id": 2, "llm_profile": "default"},
            {"agent_id": 3},
        ],
        "time_config": {},
        "event_config": {},
    }
    (sim_root / simulation_id / "simulation_config.json").write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setattr(
        "app.llm.get_profile",
        lambda profile_id: SimpleNamespace(label="Default", model="gpt-test"),
    )
    expected = {"default": {"label": "Default", "model": "gpt-test", "count": 2}}

    realtime = client.get(f"/api/simulation/{simulation_id}/config/realtime").json["data"]
    assert realtime["summary"]["llm_models"] == expected
    agents = realtime["config"]["agent_configs"]
    assert [(a["llm_profile"], a["archetype"]) for a in agents] == [
        ("default", "busy_worker"), ("default", None), (None, None)
    ]

    monkeypatch.setattr(
        simulation_api.SimulationManager, "get_simulation_config",
        lambda _self, _sid: json.loads(json.dumps(config)),
    )
    full = client.get(f"/api/simulation/{simulation_id}/config").json["data"]
    assert full["llm_models"] == expected
    assert full["agent_configs"][2]["llm_profile"] is None

    status = client.get(f"/api/simulation/{simulation_id}/run-status").json["data"]
    assert status["llm_models"] == expected

    page = _poll(client, simulation_id, 0, 10)
    assert page["llm_models"] == expected
    by_agent = {a["agent_id"]: (a["llm_profile"], a["archetype"]) for a in page["actions"]}
    assert by_agent == {1: ("default", "busy_worker"), 2: ("default", None)}

    declared = dict(config, llm_models={"x": {"label": "X", "model": "m", "count": 3}})
    (sim_root / simulation_id / "simulation_config.json").write_text(json.dumps(declared) + " ", encoding="utf-8")
    assert client.get(f"/api/simulation/{simulation_id}/run-status").json["data"]["llm_models"] == declared["llm_models"]


def test_persona_presets_follow_request_locale(client):
    for header, locale in (("ko-KR,ko;q=0.9", "ko"), ("en-US", "en"), ("zh-CN", "zh")):
        response = client.get("/api/simulation/persona-presets", headers={"Accept-Language": header})
        assert response.status_code == 200
        assert response.json["data"] == persona_archetypes.list_presets(locale)


def test_interview_prefix_comes_from_locale_keys(monkeypatch):
    keys = json.loads(KEYS_FILE.read_text(encoding="utf-8"))
    entry = keys["api.interviewPromptPrefix"]
    for locale in ("zh", "en", "ko"):
        api_table = locale_module._translations[locale].setdefault("api", {})
        monkeypatch.setitem(api_table, "interviewPromptPrefix", entry[locale])

    app = create_app()
    for header, locale in (("zh-CN", "zh"), ("en", "en"), ("ko-KR", "ko")):
        with app.test_request_context(headers={"Accept-Language": header}):
            prompt = simulation_api.optimize_interview_prompt("Q?")
            assert prompt == entry[locale] + "Q?"
            assert simulation_api.optimize_interview_prompt(prompt) == prompt
    assert len({entry[locale] for locale in ("zh", "en", "ko")}) == 3


def _prepare_fakes(monkeypatch, calls):
    called = threading.Event()

    class FakeManager:
        def get_simulation(self, simulation_id):
            return SimpleNamespace(
                simulation_id=simulation_id, project_id="p", graph_id="g",
                entities_count=0, entity_types=[], status=None,
            )

        def _save_simulation_state(self, state):
            pass

        def prepare_simulation(self, **kwargs):
            calls.append(kwargs)
            called.set()
            return SimpleNamespace(status="ready", error=None, to_simple_dict=lambda: {})

    class FakeReader:
        def filter_defined_entities(self, **kwargs):
            raise RuntimeError("offline")

    monkeypatch.setattr(simulation_api, "SimulationManager", FakeManager)
    monkeypatch.setattr(simulation_api, "ZepEntityReader", FakeReader)
    monkeypatch.setattr(simulation_api, "_check_simulation_prepared", lambda _sid: (False, {}))
    monkeypatch.setattr(
        simulation_api.ProjectManager, "get_project",
        classmethod(lambda _cls, _pid: SimpleNamespace(simulation_requirement="req")),
    )
    monkeypatch.setattr(
        simulation_api.ProjectManager, "get_extracted_text",
        classmethod(lambda _cls, _pid: "doc"),
    )
    return called


def test_prepare_forwards_persona_mix(monkeypatch, client):
    calls = []
    called = _prepare_fakes(monkeypatch, calls)
    response = client.post("/api/simulation/prepare", json={
        "simulation_id": "sim_mix", "persona_mix": {"preset": "ko_everyday", "seed": 42},
    })
    assert response.status_code == 200, response.json
    assert response.json["data"]["persona_mix"] == {"preset": "ko_everyday", "seed": 42}
    assert called.wait(5)
    assert calls[0]["persona_mix"] == {"preset": "ko_everyday", "seed": 42}


def test_prepare_without_persona_mix_keeps_old_call(monkeypatch, client):
    calls = []
    called = _prepare_fakes(monkeypatch, calls)
    response = client.post("/api/simulation/prepare", json={"simulation_id": "sim_plain"})
    assert response.status_code == 200
    assert called.wait(5)
    assert "persona_mix" not in calls[0]


@pytest.mark.parametrize("mix", [
    "ko_everyday", {"preset": "nope", "seed": 1}, {"preset": "off", "seed": "1"},
    {"preset": "off", "seed": True},
])
def test_prepare_rejects_invalid_persona_mix(monkeypatch, client, mix):
    calls = []
    _prepare_fakes(monkeypatch, calls)
    response = client.post("/api/simulation/prepare", json={"simulation_id": "s", "persona_mix": mix})
    assert response.status_code == 400
    assert calls == []
