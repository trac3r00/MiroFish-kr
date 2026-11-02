# backend/

## OVERVIEW
Flask app (`app/`, hatch package `app`, Python >=3.11,<3.13, uv) plus out-of-process OASIS simulation runners (`scripts/`) and pytest suite (`tests/`). Score ~18: >20 files, own pyproject, highest symbol/reference density in repo.

## STRUCTURE
```
backend/
├── run.py            # entry: Config.validate() then create_app(); FLASK_HOST/FLASK_PORT (0.0.0.0:5001)
├── app/__init__.py   # create_app(): CORS /api/*, request-body debug logging, 3 blueprints
├── app/config.py     # Config: loads ROOT ../../.env with override=True
├── app/api/          # Flask blueprints (own AGENTS.md)
├── app/services/     # domain workflows (own AGENTS.md)
├── app/models/       # Project (JSON files under uploads/), TaskManager (in-memory singleton)
├── app/utils/        # Zep client/paging/lifecycle, LLMClient, locale t(), retry, FileParser
├── scripts/          # run_{twitter,reddit,parallel}_simulation.py, action_logger.py, manual validators
├── tests/            # 18 test_*.py, no conftest.py
└── uploads/          # runtime data: projects, simulations/<id>/ (gitignored, compose-mounted)
```

## WHERE TO LOOK
| Task | Location |
|------|----------|
| New setting / env var | `app/config.py` (class attr from `os.environ`), add to `Config.validate()` if required |
| Zep client, timeouts, search limits | `app/utils/zep.py` (`get_zep_client`, `ZEP_CLOUD_BASE_URL`, `MAX_ZEP_SEARCH_*`) |
| Paginated node/edge reads | `app/utils/zep_paging.py` `fetch_all_nodes` / `fetch_all_edges` |
| Graph delete/reset vs. active readers | `app/utils/zep_lifecycle.py` `graph_lifecycle_lock`, `register_graph_reader` |
| Backend i18n strings | `app/utils/locale.py` `t(key)`; JSON in root `locales/` |
| LLM calls / JSON parsing | `app/utils/llm_client.py` `LLMClient`, `openai_chat_compat.py` |
| Project/task persistence | `app/models/project.py` `ProjectManager`, `app/models/task.py` `TaskManager` |
| Simulation subprocess body | `scripts/run_parallel_simulation.py` (~1.6k LOC) |
| Live Zep check | `uv run python scripts/validate_zep_cloud_integration.py [--keep-graph]` |

## CONVENTIONS
- Zep Cloud only: `ZEP_API_URL` set => `Config.validate()` error. Base URL and HTTP timeout (60s) / ingestion wait (600s) are code constants, not env.
- Locale: request locale = exact `Accept-Language` match against loaded translations, else `zh`. Background threads have no request: capture `get_locale()` before spawning, `set_locale()` inside the thread.
- Long work runs in `threading.Thread(daemon=True)` started from API handlers; progress via `TaskManager` (process-local - lost on restart, not shared across workers).
- Retry helpers: `retry_with_backoff` (sync, `time.sleep`) vs `retry_with_backoff_async`; never use the sync one on an async path.
- Scripts are not packaged CLIs: they compute paths from `__file__`, prepend `sys.path`, load root `.env` then `backend/.env`. `LLM_BOOST_*` keys are read only by `run_parallel_simulation.py`.
- Simulation runtime IPC is file-based under `uploads/simulations/<id>/ipc_commands|ipc_responses` (see services).
- Chinese comments/log text are normal; identifiers and test names English snake_case.

## ANTI-PATTERNS
- Never mutate a Zep graph outside the lock protocol in `app/api/AGENTS.md`; never write Zep outside the batch rules in `app/services/AGENTS.md`.
- Do not add `ActionLogger` usages in scripts; legacy, use `SimulationLogManager` / `PlatformActionLogger`.
- Do not copy runner scripts into simulation dirs; `SimulationRunner` runs them from `scripts/`.
- Windows shims (`builtins.open` monkey-patch, UTF-8 setup in `run.py`) are intentional; do not "clean up".
- Debug logging prints full JSON request bodies; never log secrets at debug in new middleware.

## TESTS
- `uv run pytest` from `backend/` (pytest + pytest-asyncio; no pytest config). Tests import `app.*` directly and use `create_app()` test clients with monkeypatch/MagicMock doubles (`RecordingLLMClient`, `CompletionSequence`) - no live Zep/LLM.
- Zep contract tests: `test_zep_*` (paging, retry, lifecycle barriers, memory updater).
