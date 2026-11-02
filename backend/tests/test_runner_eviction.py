from typing import Any, cast

import pytest

from app.services import simulation_runner as runner_module
from app.services.simulation_runner import (
    RunnerStatus,
    SimulationRunState,
    SimulationRunner,
)


@pytest.fixture
def clock(tmp_path, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(SimulationRunner, "RUN_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(SimulationRunner, "_clock", staticmethod(lambda: now[0]))
    for name in ("_run_states", "_terminal_since", "_finalization_locks", "_action_indexes",
                 "_processes", "_monitor_threads", "_graph_memory_enabled", "_action_queues"):
        monkeypatch.setattr(SimulationRunner, name, {})
    monkeypatch.setattr(SimulationRunner, "_manual_stop_requests", set())
    monkeypatch.setattr(
        runner_module.ZepGraphMemoryManager, "get_updater",
        classmethod(lambda _cls, _sid: None),
    )
    return now


def _save(simulation_id, status):
    state = SimulationRunState(simulation_id=simulation_id, runner_status=status)
    SimulationRunner._save_run_state(state)
    return state


def _resident(simulation_id):
    return {
        "state": simulation_id in SimulationRunner._run_states,
        "lock": simulation_id in SimulationRunner._finalization_locks,
        "index": simulation_id in SimulationRunner._action_indexes,
        "tracked": simulation_id in SimulationRunner._terminal_since,
    }


def test_default_ttl_is_600_seconds():
    assert SimulationRunner.RUN_STATE_TTL_SECONDS == 600


def test_terminal_state_lock_and_index_evicted_after_ttl(clock):
    _save("sim-done", RunnerStatus.COMPLETED)
    with SimulationRunner._finalization_lock("sim-done"):
        pass
    SimulationRunner.get_actions_page("sim-done", after=0, limit=10)
    assert all(_resident("sim-done").values())

    clock[0] += 599
    assert SimulationRunner.evict_expired_run_states() == []
    clock[0] += 1
    assert SimulationRunner.evict_expired_run_states() == ["sim-done"]
    assert not any(_resident("sim-done").values())

    reloaded = SimulationRunner.get_run_state("sim-done")
    assert reloaded is not None
    assert reloaded.runner_status == RunnerStatus.COMPLETED
    assert "sim-done" in SimulationRunner._terminal_since


def test_get_run_state_triggers_eviction_of_other_runs(clock):
    for status in (RunnerStatus.STOPPED, RunnerStatus.FAILED):
        _save(f"sim-{status.value}", status)
    clock[0] += 600
    SimulationRunner.get_run_state("unknown")
    assert SimulationRunner._run_states == {}


def test_active_runs_are_never_evicted_and_ttl_starts_at_terminal(clock):
    state = _save("sim-live", RunnerStatus.RUNNING)
    clock[0] += 10_000
    assert SimulationRunner.evict_expired_run_states() == []

    state.runner_status = RunnerStatus.COMPLETED
    SimulationRunner._save_run_state(state)
    clock[0] += 599
    assert SimulationRunner.evict_expired_run_states() == []
    clock[0] += 1
    assert SimulationRunner.evict_expired_run_states() == ["sim-live"]


@pytest.mark.parametrize("owner", ["process", "monitor", "manual_stop", "graph", "updater"])
def test_runs_still_owning_resources_are_kept(clock, monkeypatch, owner):
    _save("sim-own", RunnerStatus.FAILED)
    if owner == "process":
        SimulationRunner._processes["sim-own"] = cast(Any, object())
    elif owner == "monitor":
        SimulationRunner._monitor_threads["sim-own"] = cast(Any, object())
    elif owner == "manual_stop":
        SimulationRunner._manual_stop_requests.add("sim-own")
    elif owner == "graph":
        SimulationRunner._graph_memory_enabled["sim-own"] = True
    else:
        monkeypatch.setattr(
            runner_module.ZepGraphMemoryManager, "get_updater",
            classmethod(lambda _cls, _sid: object()),
        )
    clock[0] += 601
    assert SimulationRunner.evict_expired_run_states() == []
    assert _resident("sim-own")["state"]


def test_held_finalization_lock_blocks_eviction(clock):
    _save("sim-held", RunnerStatus.STOPPED)
    lock = SimulationRunner._finalization_lock("sim-held")
    lock.acquire()
    try:
        clock[0] += 601
        assert SimulationRunner.evict_expired_run_states() == []
    finally:
        lock.release()
    assert SimulationRunner.evict_expired_run_states() == ["sim-held"]


def test_lock_handle_taken_before_eviction_still_excludes_new_handles(clock):
    _save("sim-race", RunnerStatus.COMPLETED)
    early = SimulationRunner._finalization_lock("sim-race")
    SimulationRunner._finalization_lock_entry("sim-race")
    clock[0] += 601
    assert SimulationRunner.evict_expired_run_states() == ["sim-race"]

    assert early.acquire()
    try:
        assert SimulationRunner._finalization_lock("sim-race").acquire(blocking=False) is False
    finally:
        early.release()
    assert SimulationRunner._finalization_lock("sim-race").acquire(blocking=False) is True
