import json
import os
import subprocess
import sys
import threading

import pytest

from app.services import simulation_runner as runner_module
from app.services.simulation_runner import RunnerStatus, SimulationRunner, SimulationRunState


@pytest.fixture
def isolated_runner(tmp_path, monkeypatch):
    monkeypatch.setattr(SimulationRunner, "RUN_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(SimulationRunner, "_run_states", {})
    monkeypatch.setattr(SimulationRunner, "_processes", {})
    monkeypatch.setattr(SimulationRunner, "_monitor_threads", {})
    monkeypatch.setattr(SimulationRunner, "_graph_memory_enabled", {})
    monkeypatch.setattr(SimulationRunner, "_draining_after_rounds", set())
    synced = []
    monkeypatch.setattr(
        SimulationRunner,
        "_sync_simulation_status",
        classmethod(lambda _cls, sim_id, status, error=None: synced.append((sim_id, status))),
    )
    return tmp_path, synced


def test_stop_is_not_blocked_by_the_after_rounds_drain(isolated_runner, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    drains = []

    def slow_stop_updater(_cls, simulation_id):
        drains.append(simulation_id)
        entered.set()
        release.wait(timeout=10)

    monkeypatch.setattr(
        runner_module.ZepGraphMemoryManager, "stop_updater", classmethod(slow_stop_updater)
    )
    state = SimulationRunState(simulation_id="sim-1")
    state.runner_status = RunnerStatus.RUNNING
    SimulationRunner._run_states["sim-1"] = state
    SimulationRunner._graph_memory_enabled["sim-1"] = True

    SimulationRunner._drain_graph_memory_after_rounds("sim-1")
    assert entered.wait(timeout=5)

    finished = threading.Event()
    result = {}

    def stop():
        result["state"] = SimulationRunner.stop_simulation("sim-1")
        finished.set()

    threading.Thread(target=stop, daemon=True).start()
    try:
        assert finished.wait(timeout=5), "stop waited for the graph drain"
    finally:
        release.set()
    assert result["state"].runner_status == RunnerStatus.STOPPED
    assert drains == ["sim-1"]


def _write_state(base, sim_id, **fields):
    os.makedirs(base / sim_id, exist_ok=True)
    (base / sim_id / "run_state.json").write_text(json.dumps(fields), encoding="utf-8")


def test_recovery_frees_runs_whose_process_is_gone(isolated_runner):
    base, synced = isolated_runner
    finished = subprocess.Popen([sys.executable, "-c", "pass"])
    finished.wait()
    _write_state(base, "sim-dead", runner_status="stopping", process_pid=finished.pid)
    _write_state(base, "sim-alive", runner_status="running", process_pid=os.getpid())
    _write_state(base, "sim-done", runner_status="completed", process_pid=finished.pid)

    recovered = SimulationRunner.recover_interrupted_runs()

    assert recovered == ["sim-dead"]
    assert synced == [("sim-dead", RunnerStatus.STOPPED)]
    saved = json.loads((base / "sim-dead" / "run_state.json").read_text(encoding="utf-8"))
    assert saved["runner_status"] == "stopped"
    alive = json.loads((base / "sim-alive" / "run_state.json").read_text(encoding="utf-8"))
    assert alive["runner_status"] == "running"
