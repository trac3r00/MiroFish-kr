# backend/app/services/

## OVERVIEW
Domain workflows behind the API: document -> ontology -> Zep graph -> OASIS agent profiles/config -> subprocess simulation -> ReACT report agent. 13 flat modules, ~12.6k LOC; score ~15 (dense symbols, 33-name `__all__`, imported by every API module) and a distinct domain.

## PIPELINE MAP
| Step | Module | Key symbols |
|------|--------|-------------|
| 1 text | `text_processor.py` | `TextProcessor` (chunking, `Config.DEFAULT_CHUNK_SIZE/OVERLAP`) |
| 1 ontology | `ontology_generator.py` | `OntologyGenerator` (LLM -> entity/edge types; normalized via `utils/ontology.py`) |
| 1 graph | `graph_builder.py` | `GraphBuilderService.build_graph_async`, `BatchSubmission`, `build_operation_id`, `GraphInfo` |
| 2 entities | `zep_entity_reader.py` | `ZepEntityReader`, `EntityNode`, `FilteredEntities` |
| 2 profiles | `oasis_profile_generator.py` (1,248) | `OasisProfileGenerator`, `OasisAgentProfile` |
| 2 personas | `persona_archetypes.py` | `list_presets`, `assign_archetypes` (seeded largest-remainder), `archetype_prompt_block`; catalogs in `app/data/archetypes/` |
| 2 config | `simulation_config_generator.py` | `SimulationConfigGenerator`, `REGION_PROFILES` (zh-CN/ko-KR/en-US), `*Config` dataclasses |
| 2 state | `simulation_manager.py` | `SimulationManager`, `SimulationState`, `SimulationStatus`, `PlatformType`; emits runner commands |
| 3 run | `simulation_runner.py` (2,033) | `SimulationRunner` (classmethod registry of `subprocess.Popen`), `RunnerStatus`, `AgentAction`, `RoundSummary` |
| 3 IPC | `simulation_ipc.py` | `SimulationIPCClient/Server`, `CommandType` - JSON files in `<sim>/ipc_commands`, `<sim>/ipc_responses` |
| 3 memory | `zep_graph_memory_updater.py` | `ZepGraphMemoryManager` / `ZepGraphMemoryUpdater` - agent actions -> Zep episodes, queued batches |
| 4 report | `report_agent.py` (2,619) | `ReportAgent`, `ReportManager`, `ReportStatus`, `ReportLogger`, `ReportConsoleLogger` |
| 4-5 tools | `zep_tools.py` (1,734) | `ZepToolsService`: `insight_forge`, `panorama_search`, `quick_search`, `interview_agents` |

Most-used: `SimulationRunner` (~6 import sites), `SimulationManager` (~5), `GraphBuilderService` (~4), `ZepEntityReader` (~4), `OasisProfileGenerator` (~3).

## CONVENTIONS
- `__all__` in `__init__.py` is curated; `GraphInfo`, `BatchSubmission`, `ReportAgent`, `ZepToolsService` are NOT exported - import from the module. Update `__all__` when adding a public class.
- All Zep access goes through `utils/zep.py` (`get_zep_client`, `call_zep_read_with_retry`) and `utils/zep_paging.py`; LLM through `app.llm` (`chat`/`chat_json`/`create_completion`) or `LLMClient(stage=...)`, always with the matching stage. Settings only via `..config.Config`.
- Zep batch ingestion is journaled: deterministic `operation_id` stored in batch metadata `mirofish_operation_id` before submit; on ambiguity reconcile by lookup (`_find_batch_by_operation_id`), refuse if multiple match.
- Dataclasses expose `to_dict()` (and `to_text()`/`to_markdown()` for LLM/report context); state persists as JSON under `Config.OASIS_SIMULATION_DATA_DIR` (`backend/uploads/simulations/<id>/`).
- Report agent tools are declared in `ReportAgent._define_tools()` and dispatched in `_execute_tool()`; tool calls are parsed from LLM text (`_parse_tool_calls`), bounded by `REPORT_AGENT_MAX_TOOL_CALLS` / `MAX_REFLECTION_ROUNDS`. Add a tool in both places plus `ZepToolsService`.
- Locale: services running in background threads re-set the captured locale.
- Simulation config carries `persona_mix` (`{preset, seed}`; presets `ko_everyday`, `en_everyday`, `zh_everyday`, `mixed_global`, `off`), per-agent `archetype` and `llm_profile`, and top-level `llm_models`. Same seed gives the same mix.
- Region comes from the locale (`select_region()`), never a hardcoded country. Profile fallback country uses `default_country()`.
- `SimulationRunner.get_actions_page()` serves the cursor detail endpoint from an incremental byte-offset index; terminal run states are evicted after `RUN_STATE_TTL_SECONDS`.
- `SimulationRunner` registers process cleanup (`register_cleanup`, signal handler) and terminates child trees on stop; runner scripts live in `backend/scripts/`.

## ANTI-PATTERNS
- NEVER replay a failed batch in `ZepGraphMemoryUpdater`: `graph.add` has no idempotency key; failures stay in `_failed_batches` and surface to `SimulationRunner`.
- Never submit a Zep batch without an `operation_id` journal entry - a lost response then cannot be reconciled.
- Do not construct `Zep(...)` clients or call raw Zep pagination here; reuse the utils helpers (retry, limits, cursor paging).
- Do not spawn simulation subprocesses outside `SimulationRunner` - its class-level `_processes` map is the only stop/cleanup authority.
- Hotspots `report_agent.py`, `simulation_runner.py`, `zep_tools.py`: change narrowly. Prompts are English with JSON field names fixed by parsers; don't rename fields or reintroduce Chinese instruction text.
- Report tool limits are env-tunable (`REPORT_TOOL_RESULT_MAX_CHARS`, `REPORT_MAX_PARALLEL_TOOLS`, ...); keep the tool cache and parallel-call path intact.

## TESTS
- `backend/tests/test_zep_graph_memory_updater.py`, `test_zep_entity_reader_edges.py`, `test_ontology_generator.py`, `test_platform_profiles.py`, `test_profile_field_normalization.py`, `test_report_tool_result_sanitizer.py`, `test_llm_json_responses.py`, `test_persona_archetypes.py`, `test_report_agent_opt.py`, `test_simulation_config_i18n.py`, `test_runner_eviction.py`.
