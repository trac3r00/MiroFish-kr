# backend/app/api/

## OVERVIEW
Flask HTTP contract for the 5-step workflow: 52 routes in 3 modules (~5k LOC). Score ~11, kept as distinct domain: owns graph lifecycle locking, background-thread launch, and the JSON envelope the frontend interceptor depends on.

## WHERE TO LOOK
| Prefix (set in `app/__init__.py`) | Module | LOC | Routes | Covers |
|------|--------|-----|--------|--------|
| `/api/graph` | `graph.py` | 963 | 10 | projects (get/list/delete/reset), `ontology/generate`, `build`, `task/<id>`, `data/<graph_id>`, `delete/<graph_id>` |
| `/api/simulation` | `simulation.py` | 2,878 | 24 | entities, `create`, `prepare` (+status), profiles/config (realtime, download), `start`/`stop`, run-status, actions/timeline/posts/comments, `interview*`, `env-status`, `close-env` |
| `/api/report` | `report.py` | 1,139 | 18 | `generate` (+status), report CRUD/download, `chat`, progress/sections, agent-log & console-log (+`/stream`), `tools/search`, `tools/statistics` |

- Blueprints are created in `__init__.py` (`graph_bp`, `simulation_bp`, `report_bp`); modules are imported after creation (`noqa: E402`) to attach routes. New module => add import there + `register_blueprint` in `app/__init__.py`.
- Frontend mirror: `frontend/src/api/{graph,simulation,report}.js` - update both sides together.

## CONVENTIONS
- Envelope: `jsonify({"success": bool, "data"|"message"|"error": ...}), status`. Frontend rejects any body with `success === false`, and surfaces `error` text from non-2xx bodies - keep `error` a user-safe string, localized via `t('api.<key>')`.
- 500 handlers currently include `traceback` in the body; follow the existing pattern only where neighbours do.
- Handlers are sync. Long jobs (`build`, `prepare`, report `generate`) start `threading.Thread(daemon=True)`, return a `task_id`/status immediately; clients poll `TaskManager`-backed status routes. Capture locale before the thread starts.
- Several "status" routes are `POST` with JSON body (`prepare/status`, `generate/status`, `interview/history`, `env-status`) - not REST-pure; keep method parity with frontend.
- Services are imported directly from modules (`..services.graph_builder`, not the package `__init__`); `ZepToolsService` is imported lazily inside report tool routes.

## GRAPH LIFECYCLE (graph.py, report.py, simulation.py)
- Mutating a Zep graph (delete/reset/rebuild) must hold `_project_build_lock(project_id)` for every owning project, then `graph_lifecycle_lock(graph_id)`, re-read projects under the locks, and refuse with 409 if a build is active or `get_graph_readers(graph_id)` is non-empty (`GraphInUseError`).
- Report generation registers a graph reader lease (`register_graph_reader(graph_id, report_id)` in `report.py`) and must `unregister_graph_reader` on every exit path; `graph.py` and simulation start (`simulation.py` ~L1732) consult `get_graph_readers` before mutating.
- Lock order: project build locks (sorted ids) -> graph lifecycle lock. Reversing it risks deadlock.

## ANTI-PATTERNS
- Never call Zep delete outside the lock + re-read sequence above.
- Never block the request thread on LLM/Zep/subprocess work; use the thread + task pattern.
- Do not hardcode user-facing error strings in new code; add a key to root `locales/*.json` and use `t()`.
- `simulation.py` is the biggest hotspot (prep, process lifecycle, history, interviews in one file); add new endpoints near their siblings, do not duplicate runner logic here - it belongs in `SimulationRunner`/`SimulationManager`.

## TESTS
- `backend/tests/test_ontology_api_errors.py`, `test_simulation_prepare_failure.py`, `test_zep_graph_lifecycle.py`, `test_zep_report_barrier.py`, `test_zep_simulation_barrier.py` drive these routes via `create_app()` test clients.
