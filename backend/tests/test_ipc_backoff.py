import asyncio
import importlib
import json
import random
import sys
from pathlib import Path

import dotenv
import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"


def _load_script(name):
    if name in sys.modules:
        return sys.modules[name]
    # The runner script loads the root .env at import time; keep the test process environment clean.
    original = dotenv.load_dotenv
    dotenv.load_dotenv = lambda *args, **kwargs: False
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    try:
        return importlib.import_module(name)
    finally:
        dotenv.load_dotenv = original


runner = _load_script("run_parallel_simulation")
action_logger = _load_script("action_logger")


def test_idle_backoff_grows_to_cap_and_resets():
    backoff = runner.IdleBackoff()
    assert [backoff.next_delay() for _ in range(5)] == [0.5, 1.0, 2.0, 2.0, 2.0]
    backoff.reset()
    assert backoff.next_delay() == 0.5


class ScriptedIPC:
    def __init__(self, polls, shutdown_after=None):
        self.polls = list(polls)
        self.handled = []
        self.waits = []
        self.shutdown_after = shutdown_after

    def poll(self):
        return self.polls.pop(0) if self.polls else None

    async def handle(self, command):
        self.handled.append(command["command_id"])
        return command["command_type"] != "close_env"

    async def wait(self, delay):
        self.waits.append(delay)
        return self.shutdown_after is not None and len(self.waits) >= self.shutdown_after


def _cmd(command_id, command_type="interview"):
    return {"command_id": command_id, "command_type": command_type, "args": {}}


def test_command_loop_backs_off_while_idle_and_resets_on_command():
    ipc = ScriptedIPC([None, None, None, None, _cmd("a"), _cmd("b"), None, None, _cmd("c", "close_env")])
    outcome = asyncio.run(runner.run_command_loop(ipc.poll, ipc.handle, ipc.wait))
    assert outcome == "closed"
    assert ipc.handled == ["a", "b", "c"]
    assert ipc.waits == [0.5, 1.0, 2.0, 2.0, 0.5, 1.0]


def test_command_loop_stops_on_shutdown():
    ipc = ScriptedIPC([], shutdown_after=3)
    assert asyncio.run(runner.run_command_loop(ipc.poll, ipc.handle, ipc.wait)) == "shutdown"
    assert ipc.waits == [0.5, 1.0, 2.0]
    assert ipc.handled == []


def test_wait_for_shutdown_reports_event_state():
    async def scenario():
        event = asyncio.Event()
        idle = await runner.wait_for_shutdown(event, 0)
        event.set()
        return idle, await runner.wait_for_shutdown(event, 2.0)

    assert asyncio.run(scenario()) == (False, True)


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def _file_lines(logger):
    path = Path(logger.log_path)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _log(logger, i):
    logger.log_action(round_num=1, agent_id=i, agent_name=f"a{i}", action_type="CREATE_POST",
                      action_args={"content": f"post {i}"})


def test_action_logger_flushes_at_fifty_lines(tmp_path):
    logger = action_logger.PlatformActionLogger("twitter", str(tmp_path), clock=FakeClock())
    for i in range(49):
        _log(logger, i)
    assert _file_lines(logger) == []
    _log(logger, 49)
    assert [e["agent_id"] for e in _file_lines(logger)] == list(range(50))
    logger.close()


def test_action_logger_flushes_after_one_second_and_on_events(tmp_path):
    clock = FakeClock()
    logger = action_logger.PlatformActionLogger("reddit", str(tmp_path), clock=clock)
    _log(logger, 0)
    clock.now = 0.99
    _log(logger, 1)
    assert _file_lines(logger) == []
    clock.now = 1.0
    _log(logger, 2)
    assert len(_file_lines(logger)) == 3

    _log(logger, 3)
    logger.log_round_end(1, 4)
    assert [e.get("event_type") for e in _file_lines(logger)][-2:] == [None, "round_end"]
    logger.close()


def test_action_logger_keeps_lines_when_write_fails(tmp_path, monkeypatch):
    logger = action_logger.PlatformActionLogger("twitter", str(tmp_path), clock=FakeClock())
    _log(logger, 0)

    def failing_open(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(action_logger, "open", failing_open, raising=False)
    with pytest.raises(OSError):
        logger.log_round_end(1, 1)
    monkeypatch.delattr(action_logger, "open")
    logger.close()
    assert [e.get("event_type") for e in _file_lines(logger)] == [None, "round_end"]


def test_property_action_bursts_never_lose_or_reorder_lines(tmp_path):
    rng = random.Random(20261102)
    attempted = executed = 0
    for case in range(200):
        attempted += 1
        clock = FakeClock()
        logger = action_logger.PlatformActionLogger("twitter", str(tmp_path / f"c{case}"), clock=clock)
        expected = []
        first_pending_at = None
        for step in range(rng.randint(0, 400)):
            clock.now += rng.choice([0.0, 0.0, 0.001, 0.05, 0.3, 1.2])
            op = rng.random()
            if op < 0.85:
                _log(logger, step)
                expected.append(("action", step))
            elif op < 0.95:
                logger.log_round_start(step, step % 24)
                expected.append(("round_start", step))
            else:
                logger.flush()

            written = _file_lines(logger)
            keys = [(e.get("event_type") or "action", e.get("agent_id", e.get("round"))) for e in written]
            assert keys == expected[:len(keys)]
            pending = len(expected) - len(keys)
            assert pending < 50
            if pending == 0:
                first_pending_at = None
            elif first_pending_at is None or pending == 1:
                first_pending_at = clock.now
            if pending:
                assert clock.now - first_pending_at < 1.0
                assert expected[-1][0] == "action"
        logger.close()
        written = _file_lines(logger)
        assert [(e.get("event_type") or "action", e.get("agent_id", e.get("round"))) for e in written] == expected
        executed += 1
    assert executed == attempted == 200
