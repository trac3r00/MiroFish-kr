"""`mirofish` command line: run, monitor and inspect MiroFish through its HTTP API."""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..utils.locale import set_locale, t
from .client import Api, ApiError

QUICK_TEST_ROUNDS = 10
REPORT_START_GRACE_SECONDS = 120
SUPPORTED_LANGS = ("ko", "en", "zh")
PERSONA_PRESET_FOR_LANG = {"ko": "ko_everyday", "en": "en_everyday", "zh": "zh_everyday"}
ACTIVE_RUNNER = {"starting", "running", "paused", "stopping"}
TERMINAL_RUNNER = {"completed", "stopped", "failed"}


def detect_lang(explicit: Optional[str] = None) -> str:
    for candidate in (explicit, os.environ.get("MIROFISH_LANG"), os.environ.get("LANG")):
        code = (candidate or "").split(".")[0].split("_")[0].split("-")[0].lower()
        if code in SUPPORTED_LANGS:
            return code
    return "en"


class Eta:
    """Time-left from a real counter: rate over the last 3 minutes, shown only
    after 20 s of observed progress (a jumpy start would otherwise mislead)."""

    WINDOW = 180.0
    MIN_OBSERVED = 20.0

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        self.samples: List[tuple] = []

    def update(self, done: float) -> None:
        if not self.samples or self.samples[-1][1] != done:
            self.samples.append((self.clock(), done))
            self.samples = self.samples[-60:]

    def remaining(self, done: float, total: float) -> Optional[float]:
        if not total or done <= 0:
            return None
        if done >= total:
            return 0.0
        now = self.clock()
        progressed = [s for s in self.samples if s[1] > 0]
        if not progressed or (now - progressed[0][0] < self.MIN_OBSERVED and len(progressed) < 3):
            return None
        last = self.samples[-1]
        base = next((s for s in progressed if now - s[0] <= self.WINDOW and s[1] < last[1]), None)
        if base and last[0] > base[0]:
            rate = (last[1] - base[1]) / (last[0] - base[0])
        elif self.samples[0][1] == 0 and last[0] > self.samples[0][0]:
            rate = last[1] / (last[0] - self.samples[0][0])
        else:
            return None
        return max(0.0, (total - done) / rate - (now - last[0])) if rate > 0 else None


def fmt_duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return "-"
    seconds = int(max(0, seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return t("timer.hourMin", h=hours, m=minutes)
    if minutes:
        return t("timer.minSec", m=minutes, s=secs)
    return t("timer.sec", s=secs)


class Output:
    def __init__(self, as_json: bool, stream=None):
        self.as_json = as_json
        self.stream = stream or sys.stdout
        self.live = not as_json and self.stream.isatty()
        self._last_line = ""

    def line(self, text: str = "") -> None:
        if self.as_json:
            return
        self._end_progress()
        print(text, file=self.stream, flush=True)

    def progress(self, text: str) -> None:
        if self.as_json or text == self._last_line:
            return
        if self.live:
            print("\r\033[2K" + text, end="", file=self.stream, flush=True)
        else:
            print(text, file=self.stream, flush=True)
        self._last_line = text

    def _end_progress(self) -> None:
        if self.live and self._last_line:
            print(file=self.stream, flush=True)
        self._last_line = ""

    def data(self, payload: Any) -> None:
        if self.as_json:
            print(json.dumps(payload, ensure_ascii=False, indent=2, default=str), file=self.stream, flush=True)

    def table(self, headers: List[str], rows: List[List[Any]]) -> None:
        if self.as_json:
            return
        cells = [[str(c) if c is not None else "-" for c in row] for row in rows]
        widths = [max([_width(h)] + [_width(r[i]) for r in cells]) for i, h in enumerate(headers)]
        self.line("  ".join(_pad(h, w) for h, w in zip(headers, widths)))
        for row in cells:
            self.line("  ".join(_pad(c, w) for c, w in zip(row, widths)))


def _width(text: str) -> int:
    # East Asian characters take two terminal columns.
    return sum(2 if ord(ch) > 0x2E7F else 1 for ch in text)


def _pad(text: str, width: int) -> str:
    return text + " " * (width - _width(text))


def _short(text: Optional[str], limit: int = 48) -> str:
    text = (text or "").replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _newest(items: List[dict], limit: int) -> List[dict]:
    return sorted(items, key=lambda i: str(i.get("created_at") or ""), reverse=True)[:limit]


def cmd_status(api: Api, out: Output, args) -> int:
    status = api.get("/api/llm/status")["data"]
    out.data({"api": api.base_url, **status})
    graph = status.get("graph_backend") or {}
    out.line(t("cli.status.server", url=api.base_url))
    out.line(t("cli.status.graph", kind=graph.get("kind", "-"), embeddings=graph.get("embeddings", "-")))
    out.line(t("cli.status.profiles"))
    out.table(
        [t("cli.col.id"), t("cli.col.kind"), t("cli.col.model"), t("cli.col.label")],
        [[p.get("id"), p.get("kind"), p.get("model"), p.get("label")] for p in status.get("profiles", [])],
    )
    routes = status.get("routes") or {}
    if routes:
        out.line(t("cli.status.routes", routes=", ".join(f"{k}→{v}" for k, v in sorted(routes.items()))))
    pool = status.get("agent_pool") or {}
    if pool:
        out.line(t("cli.status.pool", pool=", ".join(f"{k} {v:g}" for k, v in pool.items())))
    return 0


def cmd_list(api: Api, out: Output, args) -> int:
    if args.what == "projects":
        items = _newest(api.get("/api/graph/project/list")["data"], args.limit)
        out.data(items)
        out.table(
            [t("cli.col.id"), t("cli.col.status"), t("cli.col.name"), t("cli.col.created")],
            [[p.get("project_id"), p.get("status"), _short(p.get("name"), 30), str(p.get("created_at", ""))[:16]] for p in items],
        )
    elif args.what == "sims":
        items = _newest(api.get("/api/simulation/list")["data"], args.limit)
        out.data(items)
        out.table(
            [t("cli.col.id"), t("cli.col.status"), t("cli.col.project"), t("cli.col.agents"), t("cli.col.created")],
            [[s.get("simulation_id"), s.get("status"), s.get("project_id"), s.get("profiles_count"), str(s.get("created_at", ""))[:16]] for s in items],
        )
    else:
        items = _newest(api.get("/api/report/list")["data"], args.limit)
        for item in items:
            item.pop("markdown_content", None)
        out.data(items)
        out.table(
            [t("cli.col.id"), t("cli.col.status"), t("cli.col.simulation"), t("cli.col.title")],
            [[r.get("report_id"), r.get("status"), r.get("simulation_id"), _short((r.get("outline") or {}).get("title"))] for r in items],
        )
    return 0


def _sim_snapshot(api: Api, sim_id: str) -> Dict[str, Any]:
    sim = api.get(f"/api/simulation/{sim_id}")["data"]
    run = api.get(f"/api/simulation/{sim_id}/run-status")["data"]
    try:
        report = api.get(f"/api/report/by-simulation/{sim_id}")["data"]
    except ApiError:
        report = None
    return {"simulation": sim, "run": run, "report": report}


def cmd_show(api: Api, out: Output, args) -> int:
    target = args.id
    if target.startswith("proj_"):
        project = api.get(f"/api/graph/project/{target}")["data"]
        sims = api.get("/api/simulation/list", project_id=target)["data"]
        out.data({"project": project, "simulations": sims})
        out.line(t("cli.show.project", id=target, status=project.get("status"), name=project.get("name")))
        out.line(t("cli.show.requirement", text=_short(project.get("simulation_requirement"), 120)))
        out.line(t("cli.show.graph", graph=project.get("graph_id") or "-"))
        out.line(t("cli.show.files", files=", ".join(f.get("filename", "") for f in project.get("files", []))))
        for sim in _newest(sims, 20):
            out.line(f"  - {sim.get('simulation_id')}  {sim.get('status')}")
    elif target.startswith("sim_"):
        snap = _sim_snapshot(api, target)
        out.data(snap)
        sim, run, report = snap["simulation"], snap["run"], snap["report"]
        out.line(t("cli.show.simulation", id=target, status=sim.get("status"), project=sim.get("project_id")))
        out.line(t("cli.show.run", runner=run.get("runner_status"), round=run.get("current_round", 0),
                   total=run.get("total_rounds", 0), actions=run.get("total_actions_count") or
                   (run.get("twitter_actions_count", 0) + run.get("reddit_actions_count", 0))))
        if run.get("graph_ingest_pending") is not None:
            out.line(t("cli.show.ingest", n=run.get("graph_ingest_pending")))
        models = run.get("llm_models") or {}
        if models:
            out.line(t("cli.show.models", models=", ".join(f"{m.get('label') or k} ×{m.get('count')}" for k, m in models.items())))
        if report:
            out.line(t("cli.show.report", id=report.get("report_id"), status=report.get("status")))
    elif target.startswith("report_"):
        report = api.get(f"/api/report/{target}")["data"]
        outline = report.get("outline") or {}
        report.pop("markdown_content", None)
        out.data(report)
        out.line(t("cli.show.reportTitle", id=target, status=report.get("status"), title=outline.get("title", "-")))
        if outline.get("summary"):
            out.line(_short(outline["summary"], 300))
        for index, section in enumerate(outline.get("sections", []), 1):
            out.line(f"  {index}. {section.get('title')}")
        out.line(t("cli.show.reportSim", sim=report.get("simulation_id")))
    else:
        raise ApiError(t("cli.error.unknownId", id=target))
    return 0


def cmd_usage(api: Api, out: Output, args) -> int:
    usage = api.get("/api/llm/usage")["data"]
    out.data(usage)
    out.table(
        [t("cli.col.stage"), t("cli.col.profile"), t("cli.col.model"), t("cli.col.calls"),
         t("cli.col.input"), t("cli.col.cached"), t("cli.col.output")],
        [[e["stage"], e["profile_id"], e["model"], e["calls"], f"{e['input_tokens']:,}",
          f"{e['cached_tokens']:,}", f"{e['output_tokens']:,}"] for e in usage.get("entries", [])],
    )
    totals = usage.get("totals") or {}
    out.line(t("cli.usage.totals", calls=totals.get("calls", 0), input=f"{totals.get('input_tokens', 0):,}",
               cached=f"{totals.get('cached_tokens', 0):,}", output=f"{totals.get('output_tokens', 0):,}"))
    return 0


def _print_estimate(out: Output, est: dict) -> None:
    if est.get("estimated_seconds"):
        out.line(t("cli.estimate.time", time=fmt_duration(est["estimated_seconds"]), runs=est.get("history_runs", 0)))
    else:
        out.line(t("estimate.timeUnknown"))
    if est.get("estimated_tokens"):
        out.line(t("estimate.tokens", turns=est.get("agent_turns", 0), tokens=f"{est['estimated_tokens']:,}"))
    else:
        out.line(t("estimate.turns", turns=est.get("agent_turns", 0), rounds=est.get("rounds", 0)))


def cmd_estimate(api: Api, out: Output, args) -> int:
    est = api.get(f"/api/simulation/{args.sim}/estimate", max_rounds=args.rounds)["data"]
    out.data(est)
    out.line(t("cli.estimate.header", rounds=est.get("rounds"), agents=est.get("agents")))
    _print_estimate(out, est)
    return 0


def _poll(fetch: Callable[[], dict], render: Callable[[dict], Optional[str]], done: Callable[[dict], bool],
          out: Output, interval: float, sleep: Callable[[float], None] = time.sleep) -> dict:
    first = True
    while True:
        state = fetch()
        finished = done(state)
        if not (first and finished):
            line = render(state)
            if line:
                out.progress(line)
        if finished:
            return state
        first = False
        sleep(interval)


def _stage_line(label: str, started: float, clock: Callable[[], float], eta: Optional[Eta],
                done: float = 0, total: float = 0, extra: str = "") -> str:
    parts = [label, t("timer.elapsed", time=fmt_duration(clock() - started))]
    if eta is not None and total:
        eta.update(done)
        left = eta.remaining(done, total)
        parts.append(t("cli.progress.left", time=fmt_duration(left)) if left else t("timer.calculating"))
    if extra:
        parts.append(extra)
    return " · ".join(parts)


def wait_graph(api: Api, out: Output, task_id: str, interval: float, clock=time.monotonic, sleep=time.sleep) -> dict:
    started, eta = clock(), Eta(clock)

    def render(task: dict) -> str:
        ratio = (task.get("progress_detail") or {}).get("ratio")
        done, total = (ratio * 1000, 1000) if ratio is not None else (0, 0)
        return _stage_line(t("cli.stage.graph", pct=task.get("progress", 0)), started, clock, eta, done, total,
                           _short(task.get("message"), 60))

    task = _poll(lambda: api.get(f"/api/graph/task/{task_id}")["data"], render,
                 lambda s: s.get("status") in ("completed", "failed"), out, interval, sleep)
    if task.get("status") == "failed":
        raise ApiError(t("cli.error.stageFailed", stage="graph", error=task.get("error")))
    return task


def wait_prepare(api: Api, out: Output, sim_id: str, task_id: Optional[str], interval: float,
                 clock=time.monotonic, sleep=time.sleep) -> dict:
    started, eta = clock(), Eta(clock)

    def render(state: dict) -> str:
        detail = state.get("progress_detail") or {}
        counting = detail.get("current_stage") == "generating_profiles" and detail.get("total_items")
        done, total = (detail.get("current_item", 0), detail["total_items"]) if counting else (0, 0)
        label = t("cli.stage.prepare", stage=detail.get("current_stage_name") or "-", pct=state.get("progress", 0))
        extra = f"{done}/{total}" if counting else ""
        return _stage_line(label, started, clock, eta if counting else None, done, total, extra)

    state = _poll(lambda: api.post("/api/simulation/prepare/status", {"task_id": task_id, "simulation_id": sim_id})["data"],
                  render, lambda s: s.get("already_prepared") or s.get("status") in ("completed", "ready", "failed"),
                  out, interval, sleep)
    if state.get("status") == "failed":
        raise ApiError(t("cli.error.stageFailed", stage="prepare", error=state.get("error")))
    return state


def wait_run(api: Api, out: Output, sim_id: str, interval: float, clock=time.monotonic, sleep=time.sleep) -> dict:
    started, eta = clock(), Eta(clock)

    def render(run: dict) -> str:
        total, current = run.get("total_rounds", 0), run.get("current_round", 0)
        actions = run.get("total_actions_count") or (run.get("twitter_actions_count", 0) + run.get("reddit_actions_count", 0))
        ingesting = run.get("graph_ingest_pending") is not None
        if ingesting and total and current >= total and not run.get("rounds_finished") and run.get("runner_status") in ACTIVE_RUNNER:
            return _stage_line(t("cli.stage.ingest", n=run.get("graph_ingest_pending", "-")), started, clock, None)
        return _stage_line(t("cli.stage.run", round=current, total=total, actions=actions), started, clock, eta, current, total)

    run = _poll(lambda: api.get(f"/api/simulation/{sim_id}/run-status")["data"], render,
                lambda r: r.get("rounds_finished") or r.get("runner_status") in TERMINAL_RUNNER, out, interval, sleep)
    if run.get("runner_status") == "failed":
        raise ApiError(t("cli.error.stageFailed", stage="run", error=run.get("error")))
    return run


def wait_report(api: Api, out: Output, report_id: str, interval: float, clock=time.monotonic, sleep=time.sleep) -> dict:
    started, eta = clock(), Eta(clock)
    totals: Dict[str, int] = {}

    def fetch() -> dict:
        try:
            progress = api.get(f"/api/report/{report_id}/progress")["data"]
        except ApiError as error:
            # Right after /generate the progress file does not exist yet (404).
            if error.status != 404 or clock() - started > REPORT_START_GRACE_SECONDS:
                raise
            return {"status": "starting", "completed_sections": []}
        if not totals.get("sections"):
            try:
                outline = api.get(f"/api/report/{report_id}")["data"].get("outline") or {}
                totals["sections"] = len(outline.get("sections") or [])
            except ApiError:
                pass
        return progress

    def render(progress: dict) -> str:
        done, total = len(progress.get("completed_sections") or []), totals.get("sections", 0)
        return _stage_line(t("cli.stage.report", done=done, total=total or "-"), started, clock, eta, done, total,
                           _short(progress.get("current_section"), 40))

    progress = _poll(fetch, render, lambda p: p.get("status") in ("completed", "failed"), out, interval, sleep)
    if progress.get("status") == "failed":
        raise ApiError(t("cli.error.stageFailed", stage="report", error=progress.get("message")))
    return progress


def _rounds_for(args) -> Optional[int]:
    if getattr(args, "rounds", None):
        return args.rounds
    return None if getattr(args, "full", False) else QUICK_TEST_ROUNDS


def start_run(api: Api, out: Output, sim_id: str, rounds: Optional[int], graph_memory: bool, force: bool) -> dict:
    body = {"simulation_id": sim_id, "platform": "parallel", "force": force, "enable_graph_memory_update": graph_memory}
    if rounds:
        body["max_rounds"] = rounds
    return api.post("/api/simulation/start", body)["data"]


def generate_report(api: Api, out: Output, sim_id: str, interval: float, force: bool = False) -> str:
    data = api.post("/api/report/generate", {"simulation_id": sim_id, "force_regenerate": force})["data"]
    report_id = data["report_id"]
    if data.get("status") != "completed":
        out.line(t("cli.report.started", id=report_id))
        wait_report(api, out, report_id, interval)
    return report_id


def download_report(api: Api, out: Output, report_id: str, destination: Optional[str]) -> Path:
    name, content = api.download(f"/api/report/{report_id}/download")
    target = Path(destination or ".")
    if target.is_dir() or (destination or "").endswith(("/", os.sep)) or destination is None:
        target.mkdir(parents=True, exist_ok=True)
        target = target / name
    target.write_bytes(content)
    out.line(t("cli.report.saved", path=str(target), size=f"{len(content):,}"))
    return target


def cmd_run(api: Api, out: Output, args) -> int:
    files = [Path(f) for f in args.files]
    missing = [str(f) for f in files if not f.is_file()]
    if missing:
        raise ApiError(t("cli.error.missingFiles", files=", ".join(missing)))
    rounds = _rounds_for(args)
    result: Dict[str, Any] = {}
    try:
        out.line(t("cli.run.ontology", files=len(files)))
        project = api.upload("/api/graph/ontology/generate", files, {
            "simulation_requirement": args.prompt,
            "project_name": args.name or files[0].stem,
        })["data"]
        result["project_id"] = project_id = project["project_id"]
        out.line(t("cli.run.project", id=project_id))

        task_id = api.post("/api/graph/build", {"project_id": project_id})["data"]["task_id"]
        wait_graph(api, out, task_id, args.interval)
        graph_id = api.get(f"/api/graph/project/{project_id}")["data"]["graph_id"]
        out.line(t("cli.run.graphDone", graph=graph_id))

        sim = api.post("/api/simulation/create", {"project_id": project_id, "graph_id": graph_id,
                                                   "enable_twitter": True, "enable_reddit": True})["data"]
        result["simulation_id"] = sim_id = sim["simulation_id"]
        out.line(t("cli.run.simulation", id=sim_id))
        preset = args.persona or PERSONA_PRESET_FOR_LANG.get(api.lang, "mixed_global")
        seed = args.seed if args.seed is not None else random.randint(1, 999_999)
        prep = api.post("/api/simulation/prepare", {"simulation_id": sim_id, "use_llm_for_profiles": True,
                                                     "parallel_profile_count": 5,
                                                     "persona_mix": {"preset": preset, "seed": seed}})["data"]
        wait_prepare(api, out, sim_id, prep.get("task_id"), args.interval)
        out.line(t("cli.run.prepared", preset=preset, seed=seed))

        est = api.get(f"/api/simulation/{sim_id}/estimate", max_rounds=rounds)["data"]
        _print_estimate(out, est)
        start_run(api, out, sim_id, rounds, not args.no_graph_memory, force=False)
        run = wait_run(api, out, sim_id, args.interval)
        out.line(t("cli.run.roundsDone", round=run.get("current_round"), total=run.get("total_rounds")))

        if not args.skip_report:
            result["report_id"] = report_id = generate_report(api, out, sim_id, args.interval)
            result["report_file"] = str(download_report(api, out, report_id, args.out))
    except KeyboardInterrupt:
        out.line(t("cli.run.interrupted", target=result.get("simulation_id") or result.get("project_id") or "-"))
        out.data({**result, "interrupted": True})
        return 130
    out.data(result)
    out.line(t("cli.run.done"))
    return 0


def cmd_watch(api: Api, out: Output, args) -> int:
    target = args.id
    try:
        if target.startswith("proj_"):
            project = api.get(f"/api/graph/project/{target}")["data"]
            task_id = project.get("graph_build_task_id")
            if task_id and project.get("status") not in ("graph_completed", "failed"):
                wait_graph(api, out, task_id, args.interval)
            out.line(t("cli.show.project", id=target, status=api.get(f"/api/graph/project/{target}")["data"].get("status"),
                       name=project.get("name")))
        elif target.startswith("sim_"):
            run = wait_run(api, out, target, args.interval)
            out.data(run)
            out.line(t("cli.show.run", runner=run.get("runner_status"), round=run.get("current_round", 0),
                       total=run.get("total_rounds", 0), actions=run.get("total_actions_count", 0)))
        elif target.startswith("report_"):
            progress = wait_report(api, out, target, args.interval)
            out.data(progress)
            out.line(t("cli.report.done", id=target))
        else:
            raise ApiError(t("cli.error.unknownId", id=target))
    except KeyboardInterrupt:
        out.line(t("cli.watch.stopped"))
        return 130
    return 0


def cmd_start(api: Api, out: Output, args) -> int:
    data = start_run(api, out, args.sim, _rounds_for(args), not args.no_graph_memory, force=args.force)
    out.data(data)
    out.line(t("cli.start.started", id=args.sim))
    if args.follow:
        return cmd_watch(api, out, argparse.Namespace(id=args.sim, interval=args.interval))
    return 0


def cmd_stop(api: Api, out: Output, args) -> int:
    response = api.post("/api/simulation/stop", {"simulation_id": args.sim})
    out.data(response)
    out.line(t("cli.stop.pending") if response.get("pending") else t("cli.stop.done", id=args.sim))
    return 0


def cmd_report(api: Api, out: Output, args) -> int:
    if args.action == "generate":
        report_id = generate_report(api, out, args.id, args.interval, force=args.force)
        out.data({"report_id": report_id})
        out.line(t("cli.report.done", id=report_id))
    else:
        path = download_report(api, out, args.id, args.out)
        out.data({"report_id": args.id, "file": str(path)})
    return 0


def cmd_chat(api: Api, out: Output, args) -> int:
    data = api.post("/api/report/chat", {"simulation_id": args.sim, "message": args.message})["data"]
    out.data(data)
    out.line(data.get("response", ""))
    return 0


def cmd_serve(api: Api, out: Output, args) -> int:
    run_py = Path(__file__).resolve().parents[2] / "run.py"
    os.execv(sys.executable, [sys.executable, str(run_py)])
    return 0


def build_parser() -> argparse.ArgumentParser:
    def common_options(suppress: bool) -> argparse.ArgumentParser:
        # Subcommands repeat the global options with SUPPRESS defaults so
        # `mirofish --json show X` and `mirofish show X --json` both work.
        default = (lambda value: argparse.SUPPRESS) if suppress else (lambda value: value)
        options = argparse.ArgumentParser(add_help=False)
        options.add_argument("--api", default=default(None), help=t("cli.help.api"))
        options.add_argument("--lang", choices=SUPPORTED_LANGS, default=default(None), help=t("cli.help.lang"))
        options.add_argument("--json", action="store_true", default=default(False), help=t("cli.help.json"))
        options.add_argument("--interval", type=float, default=default(3.0), help=t("cli.help.interval"))
        return options

    common = common_options(suppress=True)
    parser = argparse.ArgumentParser(prog="mirofish", description=t("cli.help.description"),
                                     parents=[common_options(suppress=False)])
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("serve", parents=[common], help=t("cli.help.serve")).set_defaults(func=cmd_serve)
    sub.add_parser("status", parents=[common], help=t("cli.help.status")).set_defaults(func=cmd_status)

    p = sub.add_parser("list", parents=[common], help=t("cli.help.list"))
    p.add_argument("what", choices=("projects", "sims", "reports"))
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("show", parents=[common], help=t("cli.help.show"))
    p.add_argument("id")
    p.set_defaults(func=cmd_show)

    sub.add_parser("usage", parents=[common], help=t("cli.help.usage")).set_defaults(func=cmd_usage)

    p = sub.add_parser("estimate", parents=[common], help=t("cli.help.estimate"))
    p.add_argument("sim")
    p.add_argument("--rounds", type=int)
    p.set_defaults(func=cmd_estimate)

    def add_scale(p):
        scale = p.add_mutually_exclusive_group()
        scale.add_argument("--quick", action="store_true", help=t("cli.help.quick", rounds=QUICK_TEST_ROUNDS))
        scale.add_argument("--full", action="store_true", help=t("cli.help.full"))
        scale.add_argument("--rounds", type=int, help=t("cli.help.rounds"))
        p.add_argument("--no-graph-memory", action="store_true", help=t("cli.help.noGraphMemory"))

    p = sub.add_parser("run", parents=[common], help=t("cli.help.run"))
    p.add_argument("files", nargs="+")
    p.add_argument("--prompt", "-p", required=True, help=t("cli.help.prompt"))
    p.add_argument("--name")
    p.add_argument("--persona", help=t("cli.help.persona"))
    p.add_argument("--seed", type=int)
    p.add_argument("--out", "-o", help=t("cli.help.out"))
    p.add_argument("--skip-report", action="store_true")
    add_scale(p)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("watch", parents=[common], help=t("cli.help.watch"))
    p.add_argument("id")
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("start", parents=[common], help=t("cli.help.start"))
    p.add_argument("sim")
    p.add_argument("--force", action="store_true", help=t("cli.help.force"))
    p.add_argument("--follow", "-f", action="store_true", help=t("cli.help.follow"))
    add_scale(p)
    p.set_defaults(func=cmd_start)

    p = sub.add_parser("stop", parents=[common], help=t("cli.help.stop"))
    p.add_argument("sim")
    p.set_defaults(func=cmd_stop)

    p = sub.add_parser("report", parents=[common], help=t("cli.help.report"))
    p.add_argument("action", choices=("generate", "download"))
    p.add_argument("id", help=t("cli.help.reportId"))
    p.add_argument("--out", "-o", help=t("cli.help.out"))
    p.add_argument("--force", action="store_true", help=t("cli.help.force"))
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("chat", parents=[common], help=t("cli.help.chat"))
    p.add_argument("sim")
    p.add_argument("message")
    p.set_defaults(func=cmd_chat)
    return parser


def main(argv: Optional[List[str]] = None, api_factory: Callable[..., Api] = Api) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--lang")
    lang = detect_lang(pre.parse_known_args(argv)[0].lang)
    set_locale(lang)
    args = build_parser().parse_args(argv)
    out = Output(args.json)
    api = api_factory(args.api, lang=lang)
    try:
        return args.func(api, out, args)
    except ApiError as error:
        out.data({"error": str(error), "status": error.status})
        print(t("cli.error.prefix", error=str(error)), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
