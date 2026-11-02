import json

import pytest

from app.services.simulation_runner import SimulationRunner


def _write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


@pytest.fixture
def runs_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(SimulationRunner, "RUN_STATE_DIR", str(tmp_path))
    _write(tmp_path / "sim-new" / "simulation_config.json", {
        "agent_configs": [{} for _ in range(14)],
        "time_config": {
            "total_simulation_hours": 72,
            "minutes_per_round": 60,
            "agents_per_hour_min": 2,
            "agents_per_hour_max": 10,
        },
    })
    return tmp_path


def test_estimate_uses_median_seconds_per_round_from_past_runs(runs_dir):
    # 48 rounds over 800 s, 6 rounds over 96 s, 72 rounds over 1,440 s.
    for name, rounds, end in (
        ("a", 48, "2026-11-02T09:13:20"),
        ("b", 6, "2026-11-02T09:01:36"),
        ("c", 72, "2026-11-02T09:24:00"),
    ):
        _write(runs_dir / name / "run_state.json", {
            "current_round": rounds,
            "started_at": "2026-11-02T09:00:00",
            "updated_at": end,
            "completed_at": "2026-11-02T12:00:00",
        })
    _write(runs_dir / "too-short" / "run_state.json", {
        "current_round": 1,
        "started_at": "2026-11-02T09:00:00",
        "updated_at": "2026-11-02T10:00:00",
    })

    estimate = SimulationRunner.estimate_run("sim-new", max_rounds=48, tokens_per_turn=1500)

    assert estimate["history_runs"] == 3
    assert estimate["seconds_per_round"] == pytest.approx(800 / 48)
    assert estimate["estimated_seconds"] == 800
    assert estimate["agent_turns"] == 6 * 48
    assert estimate["estimated_tokens"] == 1500 * 6 * 48


def test_estimate_without_history_or_usage_reports_unknowns(runs_dir):
    estimate = SimulationRunner.estimate_run("sim-new")

    assert estimate["rounds"] == 72
    assert estimate["history_runs"] == 0
    assert estimate["estimated_seconds"] is None
    assert estimate["estimated_tokens"] is None
