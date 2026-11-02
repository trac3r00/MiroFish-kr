"""O4 before/after resource benchmarks (no network, no LLM).

Not collected by pytest (file name is bench_*.py). Run with the backend venv
against any checkout root, for example the current tree and a worktree of
ceb8e32:

    backend/.venv/bin/python backend/tests/perf/bench_o4.py --root <checkout> <bench>

<bench> is one of: detail, runstates, reqlog, actionlog, ipc, disk, frontend.
Each bench prints one JSON object. Every bench uses temp dirs it deletes.
"""

import argparse
import asyncio
import builtins
import gc
import importlib.util
import json
import os
import random
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import tracemalloc

SEED = 20261102


def _setup_backend(root):
    backend = os.path.join(root, "backend")
    sys.path.insert(0, backend)
    os.environ.pop("LOG_REQUEST_BODIES", None)
    import app.utils.logger as app_logger

    log_dir = tempfile.mkdtemp(prefix="o4-logs-")
    app_logger.LOG_DIR = log_dir  # keep app logs out of the checkout
    os.environ.pop("LOG_REQUEST_BODIES", None)
    return backend, log_dir


def _dir_bytes(path):
    total = 0
    for base, _dirs, files in os.walk(path):
        for name in files:
            total += os.path.getsize(os.path.join(base, name))
    return total


def _rss_bytes():
    out = subprocess.run(["ps", "-o", "rss=", "-p", str(os.getpid())], capture_output=True, text=True)
    return int(out.stdout.strip()) * 1024


def _write_detail_fixture(sim_dir, n_actions=5000, rounds=50):
    rng = random.Random(SEED)
    os.makedirs(os.path.join(sim_dir, "twitter"), exist_ok=True)
    os.makedirs(os.path.join(sim_dir, "reddit"), exist_ok=True)
    files = {p: open(os.path.join(sim_dir, p, "actions.jsonl"), "w", encoding="utf-8") for p in ("twitter", "reddit")}
    per_round = n_actions // rounds
    ts = 1_780_000_000
    counts = {"twitter": 0, "reddit": 0}
    for r in range(1, rounds + 1):
        for p, f in files.items():
            f.write(json.dumps({"round": r, "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts)), "event_type": "round_start", "simulated_hour": r}) + "\n")
        for _ in range(per_round):
            ts += 1
            p = rng.choice(("twitter", "reddit"))
            counts[p] += 1
            content = "".join(rng.choice("abcdefghij klmnopqrstuvwxyz") for _ in range(rng.randint(40, 200)))
            files[p].write(json.dumps({
                "round": r,
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts)),
                "agent_id": rng.randint(0, 99),
                "agent_name": f"agent_{rng.randint(0, 99)}",
                "action_type": rng.choice(["CREATE_POST", "LIKE_POST", "REPOST", "CREATE_COMMENT"]),
                "action_args": {"content": content},
                "result": None,
                "success": True,
            }, ensure_ascii=False) + "\n")
        for p, f in files.items():
            f.write(json.dumps({"round": r, "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts)), "event_type": "round_end", "actions_count": per_round}) + "\n")
    for f in files.values():
        f.close()
    state = {
        "simulation_id": os.path.basename(sim_dir),
        "runner_status": "completed",
        "current_round": rounds,
        "total_rounds": rounds,
        "simulated_hours": rounds,
        "total_simulation_hours": rounds,
        "twitter_current_round": rounds,
        "reddit_current_round": rounds,
        "twitter_completed": True,
        "reddit_completed": True,
        "twitter_actions_count": counts["twitter"],
        "reddit_actions_count": counts["reddit"],
        "started_at": "2026-11-02T00:00:00",
        "completed_at": "2026-11-02T01:00:00",
        "recent_actions": [],
    }
    with open(os.path.join(sim_dir, "run_state.json"), "w", encoding="utf-8") as f:
        json.dump(state, f)
    return counts


def bench_detail(root):
    _backend, log_dir = _setup_backend(root)
    from app import create_app
    from app.services.simulation_runner import SimulationRunner

    tmp = tempfile.mkdtemp(prefix="o4-detail-")
    try:
        SimulationRunner.RUN_STATE_DIR = tmp
        sim_id = "sim_bench5000"
        counts = _write_detail_fixture(os.path.join(tmp, sim_id))
        fixture_bytes = _dir_bytes(os.path.join(tmp, sim_id))
        app = create_app()
        client = app.test_client()
        has_cursor = hasattr(SimulationRunner, "get_actions_page")
        variants = {"full": ""}
        if has_cursor:
            variants["compact_full"] = "?compact=1"
            variants["cursor_after0_limit200"] = "?after=0&limit=200"
            variants["cursor_after4800_limit200"] = "?after=4800&limit=200"
            variants["cursor_idle_poll"] = "?after=5000&limit=200"
        results = {}
        for name, qs in variants.items():
            url = f"/api/simulation/{sim_id}/run-status/detail{qs}"
            times, sizes = [], set()
            for _ in range(6):
                t0 = time.perf_counter()
                resp = client.get(url)
                times.append(time.perf_counter() - t0)
                assert resp.status_code == 200, resp.data[:300]
                sizes.add(len(resp.data))
            body = resp.get_json()["data"]
            n = len(body.get("actions", body.get("all_actions", [])))
            results[name] = {
                "bytes": sorted(sizes),
                "first_ms": round(times[0] * 1000, 2),
                "median_next5_ms": round(statistics.median(times[1:]) * 1000, 2),
                "actions_in_response": n,
            }
        return {"bench": "detail", "fixture_actions": counts, "fixture_dir_bytes": fixture_bytes, "results": results}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(log_dir, ignore_errors=True)


def bench_runstates(root, n_states=200):
    _backend, log_dir = _setup_backend(root)
    from app.services.simulation_runner import SimulationRunner, SimulationRunState, RunnerStatus, AgentAction

    tmp = tempfile.mkdtemp(prefix="o4-runstates-")
    try:
        SimulationRunner.RUN_STATE_DIR = tmp
        has_eviction = hasattr(SimulationRunner, "evict_expired_run_states")
        fake_now = [1000.0]
        if has_eviction:
            SimulationRunner._clock = staticmethod(lambda: fake_now[0])
        gc.collect()
        rss_start = _rss_bytes()
        tracemalloc.start()
        base = tracemalloc.take_snapshot()
        rng = random.Random(SEED)
        for i in range(n_states):
            state = SimulationRunState(simulation_id=f"sim_{i:04d}", runner_status=RunnerStatus.COMPLETED, current_round=50, total_rounds=50)
            for j in range(50):
                state.add_action(AgentAction(
                    round_num=50, timestamp="2026-11-02T00:00:00", platform="twitter",
                    agent_id=j, agent_name=f"agent_{j}", action_type="CREATE_POST",
                    action_args={"content": "x" * rng.randint(40, 200)},
                ))
            SimulationRunner._save_run_state(state)
        del state
        gc.collect()
        held_before_eviction = len(SimulationRunner._run_states)
        mem_loaded = tracemalloc.take_snapshot().compare_to(base, "filename")
        loaded_bytes = sum(s.size_diff for s in mem_loaded)
        rss_loaded = _rss_bytes()
        run_state_json_bytes = _dir_bytes(tmp)
        # advance the injected clock past the default TTL (600 s) and trigger the
        # same path the API uses (get_run_state runs eviction first in the after code)
        fake_now[0] += 601.0
        SimulationRunner.get_run_state("sim_missing_trigger")
        gc.collect()
        mem_after = tracemalloc.take_snapshot().compare_to(base, "filename")
        after_bytes = sum(s.size_diff for s in mem_after)
        tracemalloc.stop()
        return {
            "bench": "runstates",
            "has_eviction": has_eviction,
            "states_created": n_states,
            "held_after_save": held_before_eviction,
            "held_after_ttl_plus_get": len(SimulationRunner._run_states),
            "tracemalloc_bytes_after_save": loaded_bytes,
            "tracemalloc_bytes_after_ttl": after_bytes,
            "rss_start": rss_start,
            "rss_after_save": rss_loaded,
            "rss_after_ttl": _rss_bytes(),
            "run_state_json_total_bytes": run_state_json_bytes,
            "run_state_json_bytes_per_sim": run_state_json_bytes // n_states,
        }
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(log_dir, ignore_errors=True)


def bench_reqlog(root, n=2000):
    _backend, log_dir = _setup_backend(root)
    from app import create_app

    try:
        app = create_app()

        @app.route("/__bench_echo", methods=["POST"])
        def _bench_echo():
            return "ok"

        client = app.test_client()
        rng = random.Random(SEED)
        payload = {"items": []}
        while len(json.dumps(payload)) < 50_000:
            payload["items"].append({"id": len(payload["items"]), "text": "".join(rng.choice("abcdefghij ") for _ in range(80))})
        data = json.dumps(payload).encode()
        for _ in range(20):
            client.post("/__bench_echo", data=data, content_type="application/json")
        for h in __import__("logging").getLogger("mirofish.request").handlers:
            h.flush()
        bytes_before = _dir_bytes(log_dir)
        c0, w0 = time.process_time(), time.perf_counter()
        for _ in range(n):
            client.post("/__bench_echo", data=data, content_type="application/json")
        cpu, wall = time.process_time() - c0, time.perf_counter() - w0
        import logging
        for name in ("mirofish", "mirofish.request"):
            for h in logging.getLogger(name).handlers:
                h.flush()
        return {
            "bench": "reqlog",
            "requests": n,
            "body_bytes": len(data),
            "LOG_REQUEST_BODIES": os.environ.get("LOG_REQUEST_BODIES"),
            "cpu_s": round(cpu, 3),
            "wall_s": round(wall, 3),
            "cpu_ms_per_request": round(cpu / n * 1000, 4),
            "log_bytes_written": _dir_bytes(log_dir) - bytes_before,
        }
    finally:
        shutil.rmtree(log_dir, ignore_errors=True)


def _load_script(root, name):
    scripts = os.path.join(root, "backend", "scripts")
    sys.path.insert(0, scripts)
    spec = importlib.util.spec_from_file_location(f"o4_{name}", os.path.join(scripts, f"{name}.py"))
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def bench_actionlog(root, n=10_000):
    mod = _load_script(root, "action_logger")
    real_open = builtins.open
    results = {}
    for scenario in ("actions_only", "100_rounds_x_100"):
        tmp = tempfile.mkdtemp(prefix="o4-actionlog-")
        opens = [0]

        def counting_open(file, *a, **kw):
            if str(file).endswith("actions.jsonl"):
                opens[0] += 1
            return real_open(file, *a, **kw)

        try:
            log = mod.PlatformActionLogger("twitter", tmp)
            builtins.open = counting_open
            t0 = time.perf_counter()
            if scenario == "actions_only":
                for i in range(n):
                    log.log_action(1, i % 100, f"agent_{i % 100}", "CREATE_POST", {"content": "x" * 120})
            else:
                for r in range(100):
                    log.log_round_start(r, r)
                    for i in range(100):
                        log.log_action(r, i, f"agent_{i}", "CREATE_POST", {"content": "x" * 120})
                    log.log_round_end(r, 100)
            if hasattr(log, "close"):
                log.close()
            wall = time.perf_counter() - t0
        finally:
            builtins.open = real_open
        path = os.path.join(tmp, "twitter", "actions.jsonl")
        with real_open(path, encoding="utf-8") as f:
            lines = sum(1 for _ in f)
        results[scenario] = {
            "wall_ms": round(wall * 1000, 2),
            "open_calls": opens[0],
            "lines": lines,
            "file_bytes": os.path.getsize(path),
        }
        shutil.rmtree(tmp, ignore_errors=True)
    return {"bench": "actionlog", "has_buffer": hasattr(mod.PlatformActionLogger, "flush"), "results": results}


def bench_ipc(root, sim_seconds=60.0, real_seconds=10.0):
    src = open(os.path.join(root, "backend", "scripts", "run_parallel_simulation.py"), encoding="utf-8").read()
    has_backoff = "def run_command_loop" in src
    if has_backoff:
        mod = _load_script(root, "run_parallel_simulation")
        run_loop = lambda poll, wait: mod.run_command_loop(poll, _noop_handle, wait, mod.IdleBackoff())
        real_wait_factory = lambda ev: (lambda d: mod.wait_for_shutdown(ev, d))
        source = "run_parallel_simulation.run_command_loop + IdleBackoff (imported)"
    else:
        # ceb8e32 inlines the loop in main(); this mirrors lines 1615-1621:
        #   while not _shutdown_event.is_set(): process_commands(); wait_for(event.wait(), timeout=0.5)
        m = re.search(r"wait_for\(_shutdown_event\.wait\(\), timeout=([0-9.]+)\)", src)
        assert m, "before loop timeout not found"
        fixed = float(m.group(1))

        async def run_loop(poll, wait):
            while True:
                poll()
                if await wait(fixed):
                    return "shutdown"

        async def _real_wait(ev, d):
            try:
                await asyncio.wait_for(ev.wait(), timeout=d)
                return True
            except asyncio.TimeoutError:
                return False

        real_wait_factory = lambda ev: (lambda d: _real_wait(ev, d))
        source = f"replica of inline main() loop, timeout parsed from source = {fixed}"

    clock = [0.0]
    polls = [0]

    def poll():
        polls[0] += 1
        return None

    async def fake_wait(d):
        clock[0] += d
        return clock[0] >= sim_seconds

    asyncio.run(run_loop(poll, fake_wait))
    simulated = polls[0]

    async def real():
        ev = asyncio.Event()
        asyncio.get_running_loop().call_later(real_seconds, ev.set)
        await run_loop(poll, real_wait_factory(ev))

    polls[0] = 0
    t0 = time.perf_counter()
    asyncio.run(real())
    return {
        "bench": "ipc",
        "loop_source": source,
        "polls_per_60_simulated_s": simulated,
        "polls_real_window": polls[0],
        "real_window_s": round(time.perf_counter() - t0, 3),
    }


async def _noop_handle(_cmd):
    return True


def bench_frontend(root):
    fe = os.path.join(root, "frontend")
    out = tempfile.mkdtemp(prefix="o4-dist-")
    try:
        proc = subprocess.run(["npx", "vite", "build", "--outDir", out, "--emptyOutDir"], cwd=fe, capture_output=True, text=True)
        files = {}
        for base, _d, names in os.walk(out):
            for name in names:
                p = os.path.join(base, name)
                files[os.path.relpath(p, out)] = os.path.getsize(p)
        js = {k: v for k, v in files.items() if k.endswith(".js")}
        html = open(os.path.join(out, "index.html"), encoding="utf-8").read()
        initial = sorted(set(re.findall(r'(?:src|href)="/?(assets/[^"]+\.js)"', html)))
        du = subprocess.run(["du", "-sk", out], capture_output=True, text=True).stdout.split()[0]
        largest = max(js.items(), key=lambda kv: kv[1])
        return {
            "bench": "frontend",
            "exit_code": proc.returncode,
            "vite_tail": proc.stdout.strip().splitlines()[-25:],
            "dist_total_bytes": sum(files.values()),
            "dist_du_kib": int(du),
            "js_chunks": len(js),
            "largest_js": {"file": largest[0], "bytes": largest[1]},
            "initial_js": {k: js[k] for k in initial},
            "initial_js_total_bytes": sum(js[k] for k in initial),
            "logo_files": {k: v for k, v in files.items() if "MiroFish_logo_left" in k},
        }
    finally:
        shutil.rmtree(out, ignore_errors=True)


def bench_disk(root, n_actions=5000, rounds=50):
    """Bytes a 5,000-action run leaves on disk: action logs + simulation.log + run_state.json.

    Drives the real action logger (SimulationLogManager) and the real
    SimulationRunner._save_run_state; does not start OASIS.
    """
    _backend, log_dir = _setup_backend(root)
    mod = _load_script(root, "action_logger")
    from app.services.simulation_runner import SimulationRunner, SimulationRunState, RunnerStatus, AgentAction
    import logging

    tmp = tempfile.mkdtemp(prefix="o4-disk-")
    try:
        sim_dir = os.path.join(tmp, "sim_disk5000")
        os.makedirs(sim_dir)
        rng = random.Random(SEED)
        mgr = mod.SimulationLogManager(sim_dir)
        loggers = {"twitter": mgr.get_twitter_logger(), "reddit": mgr.get_reddit_logger()}
        state = SimulationRunState(simulation_id="sim_disk5000", runner_status=RunnerStatus.RUNNING, total_rounds=rounds)
        for lg in loggers.values():
            lg.log_simulation_start({"rounds": rounds})
        per_round = n_actions // rounds
        SimulationRunner.RUN_STATE_DIR = tmp
        for r in range(1, rounds + 1):
            mgr.info(f"round {r} start")
            for lg in loggers.values():
                lg.log_round_start(r, r)
            for _ in range(per_round):
                p = rng.choice(("twitter", "reddit"))
                content = "".join(rng.choice("abcdefghij klmnopqrstuvwxyz") for _ in range(rng.randint(40, 200)))
                aid = rng.randint(0, 99)
                loggers[p].log_action(r, aid, f"agent_{aid}", "CREATE_POST", {"content": content})
                state.add_action(AgentAction(round_num=r, timestamp="2026-11-02T00:00:00", platform=p,
                                             agent_id=aid, agent_name=f"agent_{aid}", action_type="CREATE_POST",
                                             action_args={"content": content}))
            for lg in loggers.values():
                lg.log_round_end(r, per_round)
            state.current_round = r
            SimulationRunner._save_run_state(state)  # the runner rewrites run_state.json as rounds advance
        for lg in loggers.values():
            lg.log_simulation_end(rounds, n_actions)
        state.runner_status = RunnerStatus.COMPLETED
        SimulationRunner._save_run_state(state)
        if hasattr(mgr, "close"):
            mgr.close()
        for h in list(getattr(getattr(mgr, "logger", None), "handlers", []) or []):
            h.flush()
        logging.shutdown()
        files = {}
        for base, _d, names in os.walk(tmp):
            for name in names:
                p = os.path.join(base, name)
                files[os.path.relpath(p, tmp)] = os.path.getsize(p)
        return {"bench": "disk", "actions": n_actions, "files": files, "total_bytes": sum(files.values())}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(log_dir, ignore_errors=True)


BENCHES = {
    "disk": bench_disk,
    "detail": bench_detail,
    "runstates": bench_runstates,
    "reqlog": bench_reqlog,
    "actionlog": bench_actionlog,
    "ipc": bench_ipc,
    "frontend": bench_frontend,
}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("bench", choices=sorted(BENCHES))
    args = ap.parse_args()
    result = BENCHES[args.bench](os.path.abspath(args.root))
    result["root"] = os.path.abspath(args.root)
    print("O4RESULT " + json.dumps(result, ensure_ascii=False))
