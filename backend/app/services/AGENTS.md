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
| 2 config | `simulation_config_generator.py` (993) | `SimulationConfigGenerator`, `SimulationParameters`, `*Config` dataclasses |
| 2 state | `simulation_manager.py` | `SimulationManager`, `SimulationState`, `SimulationStatus`, `PlatformType`; emits runner commands |
| 3 run | `simulation_runner.py` (2,033) | `SimulationRunner` (classmethod registry of `subprocess.Popen`), `RunnerStatus`, `AgentAction`, `RoundSummary` |
| 3 IPC | `simulation_ipc.py` | `SimulationIPCClient/Server`, `CommandType` - JSON files in `<sim>/ipc_commands`, `<sim>/ipc_responses` |
| 3 memory | `zep_graph_memory_updater.py` | `ZepGraphMemoryManager` / `ZepGraphMemoryUpdater` - agent actions -> Zep episodes, queued batches |
| 4 report | `report_agent.py` (2,619) | `ReportAgent`, `ReportManager`, `ReportStatus`, `ReportLogger`, `ReportConsoleLogger` |
| 4-5 tools | `zep_tools.py` (1,734) | `ZepToolsService`: `insight_forge`, `panorama_search`, `quick_search`, `interview_agents` |

Most-used: `SimulationRunner` (~6 import sites), `SimulationManager` (~5), `GraphBuilderService` (~4), `ZepEntityReader` (~4), `OasisProfileGenerator` (~3).

## CONVENTIONS
- `__all__` in `__init__.py` is curated; `GraphInfo`, `BatchSubmission`, `ReportAgent`, `ZepToolsService` are NOT exported - import from the module. Update `__all__` when adding a public class.
- All Zep access goes through `utils/zep.py` (`get_zep_client`, `call_zep_read_with_retry`) and `utils/zep_paging.py`; LLM through `utils/llm_client.LLMClient`. Settings only via `..config.Config`.
- Zep batch ingestion is journaled: deterministic `operation_id` stored in batch metadata `mirofish_operation_id` before submit; on ambiguity reconcile by lookup (`_find_batch_by_operation_id`), refuse if multiple match.
- Dataclasses expose `to_dict()` (and `to_text()`/`to_markdown()` for LLM/report context); state persists as JSON under `Config.OASIS_SIMULATION_DATA_DIR` (`backend/uploads/simulations/<id>/`).
- Report agent tools are declared in `ReportAgent._define_tools()` and dispatched in `_execute_tool()`; tool calls are parsed from LLM text (`_parse_tool_calls`), bounded by `REPORT_AGENT_MAX_TOOL_CALLS` / `MAX_REFLECTION_ROUNDS`. Add a tool in both places plus `ZepToolsService`.
- Locale: services running in background threads re-set the captured locale; LLM language instruction comes from `utils.locale.get_language_instruction()`.
- `SimulationRunner` registers process cleanup (`register_cleanup`, signal handler) and terminates child trees on stop; runner scripts live in `backend/scripts/`.

## ANTI-PATTERNS
- NEVER replay a failed batch in `ZepGraphMemoryUpdater`: `graph.add` has no idempotency key; failures stay in `_failed_batches` and surface to `SimulationRunner`.
- Never submit a Zep batch without an `operation_id` journal entry - a lost response then cannot be reconciled.
- Do not construct `Zep(...)` clients or call raw Zep pagination here; reuse the utils helpers (retry, limits, cursor paging).
- Do not spawn simulation subprocesses outside `SimulationRunner` - its class-level `_processes` map is the only stop/cleanup authority.
- Hotspots `report_agent.py`, `simulation_runner.py`, `zep_tools.py`: change narrowly; prompts there are long Chinese strings consumed by LLMs.

## TESTS
- `backend/tests/test_zep_graph_memory_updater.py`, `test_zep_entity_reader_edges.py`, `test_ontology_generator.py`, `test_platform_profiles.py`, `test_profile_field_normalization.py`, `test_report_tool_result_sanitizer.py`, `test_llm_json_responses.py`.
