# frontend/src/components/

## OVERVIEW
10 flat SFCs, ~16k LOC (~72% of frontend code): the actual workflow UI. Score ~9, kept as distinct domain: giant single-file components, D3 graph, hand-rolled markdown -> `v-html`.

## WHERE TO LOOK
| Component | LOC | Props | Emits | Role |
|-----------|-----|-------|-------|------|
| `GraphPanel.vue` | 1,423 | `graphData`, `loading`, `currentPhase`, `isSimulating` | `refresh`, `toggle-maximize` | D3 force graph (SVG, edge grouping/curvature, selection detail); used by 5 views |
| `Step1GraphBuild.vue` | 700 | `currentPhase`, `projectData`, `ontologyProgress`, `buildProgress`, ... | `next-step` | ontology + graph build progress, creates simulation |
| `Step2EnvSetup.vue` | 2,623 | `simulationId`, `projectData`, `graphData`, `systemLogs` | `go-back`, `next-step`, `add-log`, `update-status` | prepare: profiles + config generation, realtime polling |
| `Step3Simulation.vue` | 1,268 | `simulationId`, `maxRounds`, `minutesPerRound`, ... | same 4 as Step2 | start/stop run, run-status polling, action feed |
| `Step4Report.vue` | 5,162 | `reportId`, `simulationId`, `systemLogs` | `add-log`, `update-status` | report generation, agent/console log streams, section render |
| `Step5Interaction.vue` | 2,584 | `reportId`, `simulationId` | `add-log`, `update-status` | report chat, agent interviews/surveys, markdown answers |
| `HistoryDatabase.vue` | 1,348 | none | none | project/simulation history on Home |
| `LanguageSwitcher.vue` | 124 | none | none | locale picker; used by 6 views |
| `ModelBadge.vue` | small | `model`, `profileLabel`, `stage?` | none | pill showing which model handled a stage/agent |
| `AiConnectionPanel.vue` | drawer | `open` | `close`, `status-change` | profiles, stage routes, agent pool, ChatGPT sign-in, token usage (polls `/api/llm/usage` only while open) |
| `StageTimer.vue` | small | `startedAt`, `done`, `total`, `finished`, `finishedAt`, `remainingOnly` | none | real elapsed + time left (`utils/eta.js`, windowed rate, shown only after 20 s of observed progress); feed it a real counter, never a jumpy percent |
| `GuideBanner.vue` | small | `step`, `state` (`working`/`action`/`choose`/`ingesting`/`error`) | none | one-line "what now" bar under every view header; message keys `guide.s<step>.<state>` |

## CONVENTIONS
- Contract with views is props down / events up only; no Pinia/Vuex. Parent views own `systemLogs` and status; children report through `add-log` / `update-status` and move the flow with `next-step` / `go-back` (or `useRouter()` push inside Step1/Step3/Step4/HistoryDatabase).
- Each SFC keeps template + logic + a large `<style scoped>` block; no shared component barrel or design-system files - shared look is copy-pasted CSS.
- Templates use global `$t('stepN.key')`; script-side strings use `useI18n().t`. Key namespaces in `locales/*.json`: `common`, `step1`..`step5`, `graph`, `history`, `log`, `report`, `console`, ...
- Show model attribution with `ModelBadge`; resolve profiles from `getLlmStatus()` routes or the config's `llm_models`. Don't build another model label.
- Step2 gates `prepare` behind the persona preset/seed picker (`getPersonaPresets()`); Step3 feeds actions through the detail cursor.
- API access via named imports from `../api/{graph,simulation,report,llm}.js`; no env access in components.
- Polling: Step2 2-3 s, Step3 2/3 s, Step4 1.5/2 s; timers are cleared in stop functions and `onUnmounted` (Step1 has no `onUnmounted` - parent owns its polling).
- `LanguageSwitcher` sets i18n `locale`, `localStorage['locale']`, and `document.documentElement.lang` together - keep all three in sync.

## ANTI-PATTERNS
- `renderMarkdown` in `Step4Report.vue` (~L1874) and `Step5Interaction.vue` (~L557) is regex string replacement with NO HTML escaping, fed to `v-html` (4 bindings) with LLM/report text. Do not add new `v-html` sinks; if touching it, escape `<`, `>`, `&` before replacements.
- The two `renderMarkdown` copies are duplicated - change both or extract deliberately, never just one.
- Do not add literal UI copy; add keys to `zh.json`, `en.json` and `ko.json`. Guard optional keys with `te()`.
- Shared styling uses the global `--mf-*` tokens from `App.vue`; `Home.vue`'s scoped `--black`/`--orange` vars don't reach `:root`.
- Do not grow `Step4Report.vue` further without need; it is the largest file in the repo (5,162 LOC).
- Do not import D3 elsewhere for graphs; extend `GraphPanel` (it already handles zoom/resize and maximize).
