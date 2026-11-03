import io
import json

import pytest

import app.cli.main as cli
from app.cli.client import ApiError, filename_from_disposition


class FakeApi:
    """Scripted stand-in for the HTTP API: each (method, path) returns its queued responses in order."""

    def __init__(self, script):
        self.base_url = "http://test"
        self.lang = "ko"
        self.script = {key: list(values) for key, values in script.items()}
        self.calls = []

    def _next(self, method, path, body=None):
        self.calls.append((method, path, body))
        queue = self.script.get((method, path))
        if not queue:
            raise AssertionError(f"unexpected call {method} {path}")
        value = queue[0] if len(queue) == 1 else queue.pop(0)
        if isinstance(value, Exception):
            raise value
        return {"success": True, "data": value}

    def get(self, path, **query):
        return self._next("GET", path, query or None)

    def post(self, path, body=None):
        return self._next("POST", path, body)

    def upload(self, path, files, fields):
        return self._next("UPLOAD", path, {"files": [f.name for f in files], **fields})

    def download(self, path):
        self.calls.append(("DOWNLOAD", path, None))
        return "제목_2026-11-02.md", "# 보고서".encode("utf-8")


def run_cli(argv, api):
    out = io.StringIO()
    code = cli.main(argv + ["--lang", "en", "--interval", "0"], api_factory=lambda *_a, **_k: api)
    return code, out


def test_run_drives_every_stage_in_order_and_saves_the_report(tmp_path, monkeypatch, capsys):
    seed = tmp_path / "seed.md"
    seed.write_text("hello", encoding="utf-8")
    api = FakeApi({
        ("UPLOAD", "/api/graph/ontology/generate"): [{"project_id": "proj_1"}],
        ("POST", "/api/graph/build"): [{"task_id": "task_g"}],
        ("GET", "/api/graph/task/task_g"): [
            {"status": "processing", "progress": 60, "progress_detail": {"ratio": 0.5}},
            {"status": "completed", "progress": 100},
        ],
        ("GET", "/api/graph/project/proj_1"): [{"graph_id": "g1"}],
        ("POST", "/api/simulation/create"): [{"simulation_id": "sim_1"}],
        ("POST", "/api/simulation/prepare"): [{"task_id": "task_p"}],
        ("POST", "/api/simulation/prepare/status"): [{"status": "completed", "progress": 100}],
        ("GET", "/api/simulation/sim_1/estimate"): [{"rounds": 10, "estimated_seconds": 160, "history_runs": 2,
                                                     "agent_turns": 40, "estimated_tokens": None}],
        ("POST", "/api/simulation/start"): [{"runner_status": "starting"}],
        ("GET", "/api/simulation/sim_1/run-status"): [
            {"runner_status": "running", "current_round": 4, "total_rounds": 10},
            {"runner_status": "running", "current_round": 10, "total_rounds": 10, "rounds_finished": True},
        ],
        ("POST", "/api/report/generate"): [{"report_id": "report_1", "status": "generating"}],
        ("GET", "/api/report/report_1/progress"): [
            {"status": "generating", "completed_sections": ["a"]},
            {"status": "completed", "completed_sections": ["a", "b"]},
        ],
        ("GET", "/api/report/report_1"): [{"outline": {"sections": [{}, {}]}}],
    })

    code, _ = run_cli(["--json", "run", str(seed), "-p", "predict", "-o", str(tmp_path / "out") + "/"], api)

    assert code == 0
    order = [(m, p) for m, p, _ in api.calls if p != "/api/report/report_1"]
    assert order[:5] == [
        ("UPLOAD", "/api/graph/ontology/generate"),
        ("POST", "/api/graph/build"),
        ("GET", "/api/graph/task/task_g"),
        ("GET", "/api/graph/task/task_g"),
        ("GET", "/api/graph/project/proj_1"),
    ]
    start_body = next(b for m, p, b in api.calls if p == "/api/simulation/start")
    assert start_body == {"simulation_id": "sim_1", "platform": "parallel", "force": False,
                          "enable_graph_memory_update": True, "max_rounds": cli.QUICK_TEST_ROUNDS}
    prepare_body = next(b for m, p, b in api.calls if p == "/api/simulation/prepare")
    assert prepare_body["persona_mix"]["preset"] == "ko_everyday"
    assert ("DOWNLOAD", "/api/report/report_1/download", None) in api.calls
    saved = tmp_path / "out" / "제목_2026-11-02.md"
    assert saved.read_text(encoding="utf-8") == "# 보고서"
    result = json.loads(capsys.readouterr().out)
    assert result == {"project_id": "proj_1", "simulation_id": "sim_1", "report_id": "report_1",
                      "report_file": str(saved)}


@pytest.mark.parametrize("argv", [["--json", "show", "sim_9"], ["show", "sim_9", "--json"]])
def test_global_options_work_before_or_after_the_subcommand(argv):
    args = cli.build_parser().parse_args(argv + ["--interval", "1"])
    assert args.json is True
    assert args.interval == 1.0


def test_full_and_rounds_flags_choose_the_round_count():
    parser = cli.build_parser()
    assert cli._rounds_for(parser.parse_args(["start", "sim_1"])) == cli.QUICK_TEST_ROUNDS
    assert cli._rounds_for(parser.parse_args(["start", "sim_1", "--full"])) is None
    assert cli._rounds_for(parser.parse_args(["start", "sim_1", "--rounds", "48"])) == 48


def test_eta_waits_for_real_progress_then_uses_the_recent_rate():
    now = [0.0]
    eta = cli.Eta(clock=lambda: now[0])
    eta.update(0)
    now[0] = 8.0
    eta.update(55)
    assert eta.remaining(55, 100) is None
    now[0] = 68.0
    eta.update(65)
    assert eta.remaining(65, 100) == pytest.approx(35 / (10 / 60))
    assert eta.remaining(100, 100) == 0.0


def test_stop_reports_pending_when_graph_writes_are_still_draining(capsys):
    api = FakeApi({("POST", "/api/simulation/stop"): [{"pending": True}]})
    api.post = lambda path, body=None: (api.calls.append(("POST", path, body)) or
                                        {"success": False, "pending": True, "_status": 202})

    code, _ = run_cli(["stop", "sim_1"], api)

    assert code == 0
    assert "Stopping" in capsys.readouterr().out


def test_api_errors_exit_non_zero_with_the_server_message(capsys):
    api = FakeApi({("GET", "/api/simulation/sim_x"): [ApiError("Simulation sim_x not found", 404)]})

    code, _ = run_cli(["show", "sim_x"], api)

    assert code == 1
    assert "Simulation sim_x not found" in capsys.readouterr().err


@pytest.mark.parametrize("explicit, env, expected", [
    ("zh", {}, "zh"), (None, {"MIROFISH_LANG": "ko"}, "ko"), (None, {"LANG": "ko_KR.UTF-8"}, "ko"),
    (None, {"LANG": "fr_FR.UTF-8"}, "en"),
])
def test_language_detection(monkeypatch, explicit, env, expected):
    for name in ("MIROFISH_LANG", "LANG"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert cli.detect_lang(explicit) == expected


def test_download_name_prefers_the_utf8_filename():
    header = "attachment; filename=\"___.md\"; filename*=UTF-8''%ED%95%9C%EB%B9%9B_2026-11-02.md"
    assert filename_from_disposition(header) == "한빛_2026-11-02.md"


def test_report_wait_tolerates_progress_404_right_after_generate():
    api = FakeApi({
        ("GET", "/api/report/report_1/progress"): [
            ApiError("not found", 404),
            {"status": "completed", "completed_sections": ["a"]},
        ],
        ("GET", "/api/report/report_1"): [{"outline": {"sections": [{}]}}],
    })
    out = cli.Output(as_json=True)

    progress = cli.wait_report(api, out, "report_1", interval=0, sleep=lambda _s: None)

    assert progress["status"] == "completed"


def test_eta_falls_back_to_the_average_since_start_after_one_step():
    now = [0.0]
    eta = cli.Eta(clock=lambda: now[0])
    eta.update(0)
    now[0] = 300.0
    eta.update(1)
    now[0] = 330.0
    assert eta.remaining(1, 4) == pytest.approx(3 * 300 - 30)
