import json
from types import SimpleNamespace

import pytest
from flask import Flask

from app.api import report as report_api
from app.services import simulation_runner as runner_module
from app.services.simulation_runner import (
    RunnerStatus,
    SimulationRunner,
    SimulationRunState,
)


class _InlineThread:
    def __init__(self, target, **_kwargs):
        self._target = target

    def start(self):
        self._target()


@pytest.fixture
def finished_run(tmp_path, monkeypatch):
    monkeypatch.setattr(SimulationRunner, "RUN_STATE_DIR", str(tmp_path))
    (tmp_path / "sim-1" / "twitter").mkdir(parents=True)
    (tmp_path / "sim-1" / "twitter" / "actions.jsonl").write_text(
        json.dumps({"event_type": "simulation_end"}) + "\n", encoding="utf-8"
    )
    state = SimulationRunState(simulation_id="sim-1")
    state.runner_status = RunnerStatus.RUNNING
    state.twitter_completed = True
    monkeypatch.setattr(runner_module.threading, "Thread", _InlineThread)
    monkeypatch.setitem(SimulationRunner._graph_memory_enabled, "sim-1", True)
    return state


def test_rounds_finish_only_after_graph_memory_drains(finished_run, monkeypatch):
    stopped = []
    monkeypatch.setattr(
        runner_module.ZepGraphMemoryManager,
        "stop_updater",
        classmethod(lambda _cls, simulation_id: stopped.append(simulation_id)),
    )

    assert SimulationRunner.rounds_finished(finished_run) is False
    SimulationRunner._drain_graph_memory_after_rounds("sim-1")

    assert stopped == ["sim-1"]
    assert SimulationRunner.rounds_finished(finished_run) is True


def test_failed_drain_keeps_the_report_blocked(finished_run, monkeypatch):
    def fail(_cls, _simulation_id):
        raise RuntimeError("ingestion timed out")

    monkeypatch.setattr(
        runner_module.ZepGraphMemoryManager, "stop_updater", classmethod(fail)
    )

    SimulationRunner._drain_graph_memory_after_rounds("sim-1")

    assert SimulationRunner.rounds_finished(finished_run) is False


@pytest.mark.parametrize("rounds_done, expected_status", [(True, 404), (False, 409)])
def test_report_gate_accepts_a_running_env_once_rounds_are_done(
    monkeypatch, rounds_done, expected_status
):
    monkeypatch.setattr(
        report_api,
        "SimulationManager",
        lambda: SimpleNamespace(
            get_simulation=lambda _id: SimpleNamespace(project_id="proj-1", graph_id="g-1")
        ),
    )
    monkeypatch.setattr(
        report_api.SimulationRunner,
        "get_run_state",
        classmethod(lambda _cls, _id: SimpleNamespace(runner_status=RunnerStatus.RUNNING)),
    )
    monkeypatch.setattr(
        report_api.SimulationRunner,
        "rounds_finished",
        classmethod(lambda _cls, _state: rounds_done),
    )
    monkeypatch.setattr(
        report_api.ZepGraphMemoryManager,
        "get_updater",
        classmethod(lambda _cls, _id: None),
    )
    # Past the run gate the next check is the project; None proves the gate let it through.
    monkeypatch.setattr(
        report_api.ProjectManager, "get_project", classmethod(lambda _cls, _id: None)
    )

    app = Flask(__name__)
    with app.test_request_context(
        "/api/report/generate", method="POST", json={"simulation_id": "sim-1"}
    ):
        result = report_api.generate_report()

    assert result[1] == expected_status
