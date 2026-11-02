# PROJECT KNOWLEDGE BASE

**Generated:** 2026-11-01
**Commit:** 7657031
**Branch:** main

## OVERVIEW
MiroFish: document -> Zep Cloud knowledge graph -> OASIS multi-agent social simulation -> LLM report agent.
Python/Flask backend (uv, Zep Cloud, OpenAI-compatible LLM, camel-oasis) + Vue 3/Vite SPA, orchestrated by a root npm wrapper.

## STRUCTURE
```
MiroFish-kr/
├── backend/       # Flask API + pipeline services + OASIS subprocess scripts (see backend/AGENTS.md)
│   └── uploads/   # runtime data: projects, simulations/<id>/, logs, file IPC (gitignored, compose-mounted)
├── frontend/      # Vue 3 SPA (see frontend/AGENTS.md)
├── locales/       # shared i18n JSON for backend AND frontend
├── scripts/       # star-history tooling only; unrelated to the app
├── tests/         # root tests: star-history tooling only
├── static/image/  # README assets incl. generated star-history SVGs
├── .github/       # star-history workflow + generated history.json
├── package.json   # orchestration only (concurrently); AGPL-3.0, node >=18
└── Dockerfile / docker-compose.yml
```

## WHERE TO LOOK
| Task | Location | Notes |
|------|----------|-------|
| Backend layout, config, utils/models | `backend/AGENTS.md` | graph backend (local default / Zep Cloud), LLM profile kinds, locale-in-threads |
| HTTP routes, response envelope, graph locks | `backend/app/api/AGENTS.md` | 4 blueprints (graph, simulation, report, llm) |
| Pipeline (ontology -> graph -> profiles -> sim -> report) | `backend/app/services/AGENTS.md` | Zep batch journaling, archetypes |
| LLM profiles, routing, usage, ChatGPT OAuth, gateway | `backend/app/llm/`, `backend/AGENTS.md` | the only LLM seam |
| Frontend routes, views, axios, i18n | `frontend/AGENTS.md` | route -> view -> Step table |
| Big Step SFCs, polling, markdown rendering | `frontend/src/components/AGENTS.md` | |
| Env vars | `.env.example`, `backend/app/config.py` | root `.env`, loaded with override=True |
| Translations | `locales/{zh,en,languages}.json` | |
| Star-history automation | `scripts/`, `tests/`, `.github/workflows/update-star-history.yml` | separate from app |

## CODE MAP
| Symbol | Type | Location | Refs | Role |
|--------|------|----------|------|------|
| `create_app` | fn | `backend/app/__init__.py` | run.py + API tests | Flask app factory |
| `t` / `get_locale` / `set_locale` | fn | `backend/app/utils/locale.py` | ~8 modules | backend i18n |
| `graph_lifecycle_lock` | ctx mgr | `backend/app/utils/zep_lifecycle.py` | ~10 | Zep graph mutation guard |
| `SimulationRunner` | class | `backend/app/services/simulation_runner.py` | ~6 | runs OASIS subprocesses |
| `SimulationManager` | class | `backend/app/services/simulation_manager.py` | ~5 | simulation state |
| `TaskManager` | class | `backend/app/models/task.py` | ~5 | process-local task progress |
| `ProjectManager` | class | `backend/app/models/project.py` | ~4 | project JSON persistence |
| `chat` / `chat_json` / `resolve_profile` | fn | `backend/app/llm/` | all LLM callers | stage-routed LLM seam |
| `LLMClient` | class | `backend/app/utils/llm_client.py` | ~4 | legacy wrapper over the seam (`stage=`) |
| `GraphBuilderService` | class | `backend/app/services/graph_builder.py` | ~4 | Zep graph build |
| `getProject` / `getGraphData` | fn | `frontend/src/api/graph.js` | 6 views | core API calls |
| `LanguageSwitcher` / `GraphPanel` | SFC | `frontend/src/components/` | 6 / 5 | shared UI |

## CONVENTIONS
- API contract spans both halves: backend returns `{success, data|message|error}`; `frontend/src/api/index.js` interceptor unwraps `response.data` and rejects on `success === false`.
- Endpoint modules mirror 1:1: `backend/app/api/{graph,simulation,report}.py` <-> `frontend/src/api/{graph,simulation,report}.js`. Change both together.
- `locales/` is shared: backend `utils/locale.py` and frontend `src/i18n` (`@locales` alias). Supported: `zh`, `en`, `ko`; unsupported or missing locales fall back to `en`. `languages.json` lists more languages, but only those with a JSON file load.
- Every new user-facing string needs a key in `zh.json`, `en.json` AND `ko.json`. Korean wording follows `locales/GLOSSARY.md`.
- LLM prompts are written in English; the locale's language instruction (`get_language_instruction()`) goes at the END of the system prompt. Never hardcode region assumptions (China, UTC+8, Weibo) in prompts.
- Every LLM call goes through `backend/app/llm` with an explicit stage. Config is env-driven: `LLM_PROFILES`, `LLM_ROUTES`, `LLM_AGENT_POOL` (all optional; unset means one `default` profile from `LLM_*`).
- Single backend process assumed: `TaskManager` singleton + daemon threads hold progress in memory.

## ANTI-PATTERNS (THIS PROJECT)
- NEVER construct `OpenAI(...)` clients or call provider SDKs outside `backend/app/llm` (and its compat helper `utils/openai_chat_compat.py`).
- NEVER add Claude or Gemini subscription OAuth. The only OAuth provider is Sign in with ChatGPT via its official OSS flow.
- NEVER set `ZEP_API_URL`; `Config.validate()` and `utils/zep.py` reject it (Zep Cloud has no self-hosted URL; use `GRAPH_BACKEND=local` for a free local graph).
- NEVER leave `LLM_BOOST_*` vars blank; remove them entirely if unused.
- NEVER replay failed Zep `graph.add` batches (no idempotency key).
- NEVER mutate/delete a Zep graph outside the lock protocol; NEVER construct raw Zep clients (use `utils/zep.py` helpers).
- NEVER hand-edit `.github/star-history/history.json` or `static/image/star-history-{light,dark}.svg`; they are generated.
- NEVER edit `scripts/fetch_star_count.py` casually: the workflow SHA-256-pins it.
- NEVER bake `.env` into the image.
- Do not build on `frontend/src/views/Process.vue`; it is dead (route `Process` -> `MainView.vue`).

## UNIQUE STYLES
- Mixed Chinese/English comments, logs, and docs are normal (`README.md` + `README-ZH.md`).
- Root `package.json` holds no app code; it only fans out to `frontend/` (npm) and `backend/` (uv).

## COMMANDS
```bash
npm run setup:all            # root+frontend npm install, then backend uv sync
npm run dev                  # backend :5001 + frontend :3000 (concurrently --kill-others)
npm run backend              # cd backend && uv run python run.py
npm run frontend             # Vite dev server; /api proxied to :5001
npm run build                # frontend production build
cd backend && uv run pytest  # backend tests; no live services needed
GITHUB_TOKEN= GH_TOKEN= python3 -m unittest tests.test_local_star_history tests.test_local_star_count_fetch -v
docker compose up            # ghcr.io/666ghj/mirofish:latest, env_file .env, mounts ./backend/uploads
```
No frontend tests/lint/typecheck; no backend lint/format config.

## NOTES
- Required env: one LLM profile (legacy `LLM_API_KEY`/`LLM_BASE_URL`/`LLM_MODEL_NAME`, or `LLM_PROFILES`); `ZEP_API_KEY` only for `GRAPH_BACKEND=zep_cloud`. `run.py` -> `Config.validate()` fails startup otherwise.
- Simulation start hour: a run cut short by `max_rounds` starts at the first peak hour (`simulation_start_minute` in `run_parallel_simulation.py`; override with `time_config.start_hour`). Step3 resumes an existing run instead of restarting it.
- Python >=3.11,<3.13. The Docker image runs `npm run dev`, so the container runs in dev mode.
- Known risks (not yet fixed): CORS `*` on `/api/*`; markdown -> `v-html` unescaped in Step4/Step5 components; `SECRET_KEY` has a literal dev default.
- Frontend axios base URL: `VITE_API_BASE_URL` || `http://localhost:5001`.
