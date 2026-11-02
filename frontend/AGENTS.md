# frontend/

## OVERVIEW
Vue 3 + Vite 7 SPA (plain JavaScript, no TypeScript, no tests/lint) driving the 5-step workflow against the Flask API. Score ~16: 7 source subdirs, own package.json/vite config, ~21k LOC.

## STRUCTURE
```
frontend/
├── vite.config.js      # port 3000, opens browser, proxies /api -> http://localhost:5001; aliases @ -> src, @locales -> ../locales
├── index.html          # sets <html lang> from localStorage 'locale' before boot
└── src/
    ├── main.js / App.vue   # bootstrap; App.vue holds global reset styles
    ├── router/index.js     # 6 routes, createWebHistory, params passed as props
    ├── views/              # route shells: layout + GraphPanel + one StepN component
    ├── components/         # workflow UI (own AGENTS.md)
    ├── api/                # axios service + endpoint modules
    ├── i18n/index.js       # vue-i18n (legacy:false), locale JSON from root locales/
    └── store/pendingUpload.js  # reactive hand-off of files Home -> MainView
```

## WHERE TO LOOK
| Route (name) | Path | View | Step component(s) |
|------|------|------|------|
| Home | `/` | `Home.vue` | `HistoryDatabase` |
| Process | `/process/:projectId` | `MainView.vue` | `Step1GraphBuild`, `Step2EnvSetup` |
| Simulation | `/simulation/:simulationId` | `SimulationView.vue` | `Step2EnvSetup` |
| SimulationRun | `/simulation/:simulationId/start` | `SimulationRunView.vue` | `Step3Simulation` |
| Report | `/report/:reportId` | `ReportView.vue` | `Step4Report` |
| Interaction | `/interaction/:reportId` | `InteractionView.vue` | `Step5Interaction` |

- API calls: `src/api/graph.js` (5 fns), `simulation.js` (19), `report.js` (6), all over default-exported axios `service` in `src/api/index.js`. Most-imported: `getProject`, `getGraphData` (6 views each), `getSimulation` (4).
- Backend counterpart for every endpoint: `backend/app/api/` (same graph/simulation/report split).

## CONVENTIONS
- `<script setup>` Composition API everywhere; PascalCase SFC filenames, camelCase API functions.
- axios `service`: baseURL `VITE_API_BASE_URL` || `http://localhost:5001` (bypasses the Vite proxy by default), timeout 300000 ms, sends `Accept-Language` = current i18n locale. Response interceptor returns `response.data` (callers get the `{success, data, ...}` envelope, not the axios response) and rejects when `success === false`; non-2xx `error` text becomes `error.message`.
- i18n: messages loaded eagerly from `locales/*.json` but only keys listed in `locales/languages.json`; default/fallback `zh`; locale persisted in `localStorage['locale']`. Add a language = JSON file + `languages.json` entry (backend reads the same files).
- Long-running steps are tracked by fixed-interval polling (`setInterval` 1.5-10 s) of backend status routes; timers are cleared in stop paths/unmount.
- Imports are mostly relative (`../api/...`); `@` alias exists but is rarely used.

## ANTI-PATTERNS
- `src/views/Process.vue` (2,076 LOC) is NOT routed: route `Process` imports `MainView.vue`. Edit `MainView.vue` + `components/Step1GraphBuild.vue` for step 1; do not "fix" the dead file by accident. Contains a TODO at ~L484.
- Do not call axios directly or create a second instance; add a named function to the matching `src/api/*.js`.
- Do not read `response.data.data` patterns blindly - the interceptor already unwrapped one level.
- Every new `setInterval` needs a matching `clearInterval` on unmount and on terminal status.
- `console.log` noise in `Process.vue`/`MainView.vue` is legacy; do not add more.

## COMMANDS
```bash
cd frontend && npm run dev      # vite --host, :3000
cd frontend && npm run build    # vite build -> dist/
cd frontend && npm run preview
```
