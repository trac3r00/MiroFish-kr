import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run_parallel_simulation.py"


def _load():
    spec = importlib.util.spec_from_file_location("run_parallel_simulation_start", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


R = _load()
KO = {"peak_hours": [19, 20, 21, 22, 23], "morning_hours": [6, 7, 8], "work_hours": list(range(9, 18)),
      "off_peak_hours": [0, 1, 2, 3, 4, 5]}


def test_full_run_keeps_midnight_start():
    assert R.simulation_start_minute(KO, 168, 168) == 0


def test_truncated_run_starts_at_first_peak_hour():
    assert R.simulation_start_minute(KO, 5, 168) == 19 * 60


def test_truncated_run_without_peak_uses_morning_then_work():
    assert R.simulation_start_minute({"morning_hours": [7, 6]}, 5, 168) == 6 * 60
    assert R.simulation_start_minute({"work_hours": [9, 10]}, 5, 168) == 9 * 60
    assert R.simulation_start_minute({}, 5, 168) == 0


def test_explicit_start_hour_wins():
    assert R.simulation_start_minute({**KO, "start_hour": 8}, 168, 168) == 8 * 60
    assert R.simulation_start_minute({**KO, "start_hour": 26}, 5, 168) == 2 * 60
