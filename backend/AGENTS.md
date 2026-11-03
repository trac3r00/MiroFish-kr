# backend/

## OVERVIEW
Flask app (`app/`, hatch package `app`, Python >=3.11,<3.13, uv) plus out-of-process OASIS simulation runners (`scripts/`) and pytest suite (`tests/`). Score ~18: >20 files, own pyproject, highest symbol/reference density in repo.

## STRUCTURE
```
backend/
├── run.py            # entry: Config.validate() then create_app(); FLASK_HOST/FLASK_PORT (0.0.0.0:5001)
├── app/__init__.py   # create_app(): CORS /api/*, opt-in request-body logging, 4 blueprints
├── app/config.py     # Config: loads ROOT ../../.env with override=True
├── app/llm/          # LLM seam: profiles, router, client, usage, ChatGPT OAuth, Responses adapter
├── app/cli/          # `mirofish` CLI (stdlib only): HTTP client over the same API as the UI; run/watch/list/show/report/chat
├── app/data/archetypes/  # persona archetype catalogs {ko,en,zh}.json
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
| LLM calls / JSON parsing | `app/llm` `chat(messages, stage=...)` / `chat_json`; legacy `utils/llm_client.LLMClient(stage=...)` delegates there |
| Profiles, stage routes, agent pool | `app/llm/profiles.py` (`load_settings`, `to_public()` never holds secrets), `router.py` |
| Token usage | `app/llm/usage.py` `usage_tracker.record/snapshot` (seam records it; adapters must not) |
| ChatGPT sign-in | `app/llm/oauth_chatgpt.py` (loopback callback, tokens on disk 0600), `responses_adapter.py` |
| Project/task persistence | `app/models/project.py` `ProjectManager`, `app/models/task.py` `TaskManager` |
| Simulation subprocess body | `scripts/run_parallel_simulation.py` (~1.6k LOC) |
| Live Zep check | `uv run python scripts/validate_zep_cloud_integration.py [--keep-graph]` |

## CONVENTIONS
- Graph backend: `GRAPH_BACKEND=local` (default when `ZEP_API_KEY` is empty) uses `app/graph_local/` (SQLite + FTS5 trigram, LIKE for <3-char Korean terms, optional multilingual-e5 embeddings) behind the same client surface as Zep; `zep_cloud` needs `ZEP_API_KEY`. Services call `get_zep_client()` and must not require a Zep key themselves (`graph_backend_ready()`). `ZEP_API_URL` set => error (Zep Cloud only, no self-hosted URL).
- LLM stages (exact strings): `ontology`, `profile`, `config`, `report`, `report_chat`, `interview`, `search_planning`, `simulation_agent`, `graph_extract` (plus `default`). Any other stage raises `ValueError`. New LLM work picks one of these, or adds a stage in `app/llm` first.
- Profile kinds: `openai_compatible` (base_url + key env), `chatgpt_oauth` (Responses API via sign-in; JSON is prompt-only), `claude_code_cli` / `cursor_agent_cli` (spawn the user's own signed-in official CLI via `llm/cli_adapter.py`; never read their credentials; output floor `CLI_MIN_OUTPUT_TOKENS`, concurrency `CLI_MAX_CONCURRENCY`). Subscription kinds reach OASIS agents through the loopback gateway.
- Locale: `Accept-Language` is q-value negotiated (exact, then primary subtag) against `zh`/`en`/`ko`, else `en`. Background threads have no request: capture `get_locale()` before spawning, `set_locale()` inside the thread.
- Long work runs in `threading.Thread(daemon=True)` started from API handlers; progress via `TaskManager` (process-local - lost on restart, not shared across workers).
- Retry helpers: `retry_with_backoff` (sync, `time.sleep`) vs `retry_with_backoff_async`; never use the sync one on an async path.
- Scripts are not packaged CLIs: they compute paths from `__file__`, prepend `sys.path`, load root `.env` then `backend/.env`. `LLM_BOOST_*` keys are read only by `run_parallel_simulation.py`, and only on the legacy path. With per-agent `llm_profile` in the config, runners build one model backend per profile (`build_agent_model_specs`); `chatgpt_oauth` agents talk to the local gateway.
- Simulation runtime IPC is file-based under `uploads/simulations/<id>/ipc_commands|ipc_responses` (see services).
- Chinese comments/log text are normal; identifiers and test names English snake_case.

## ANTI-PATTERNS
- Never mutate a Zep graph outside the lock protocol in `app/api/AGENTS.md`; never write Zep outside the batch rules in `app/services/AGENTS.md`.
- Do not add `ActionLogger` usages in scripts; legacy, use `SimulationLogManager` / `PlatformActionLogger`.
- Do not copy runner scripts into simulation dirs; `SimulationRunner` runs them from `scripts/`.
- Windows shims (`builtins.open` monkey-patch, UTF-8 setup in `run.py`) are intentional; do not "clean up".
- Request-body logging is off unless `LOG_REQUEST_BODIES=1` (redacted, capped by `LOG_BODY_MAX_CHARS`). Never log api keys, OAuth tokens or `MIROFISH_GATEWAY_TOKEN`.

## TESTS
- `uv run pytest` from `backend/` (pytest + pytest-asyncio; no pytest config). Tests import `app.*` directly and use `create_app()` test clients with monkeypatch/MagicMock doubles (`RecordingLLMClient`, `CompletionSequence`) - no live Zep/LLM.
- Zep contract tests: `test_zep_*` (paging, retry, lifecycle barriers, memory updater).
