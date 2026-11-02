"""
Report Agent service
Generates simulation reports using LangChain, Zep, and ReACT.

Features:
1. Generate reports from simulation requirements and Zep graph data.
2. Plan an outline and generate the report section by section.
3. Use multi-turn ReACT reasoning and reflection for each section.
4. Support user conversations with retrieval-tool calls.
"""

import os
import json
import time
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Any, List, Optional, Callable, Tuple
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from ..config import Config
from ..llm import ChatResult, LLMProfile
from ..llm import chat as llm_chat
from ..llm import chat_json as llm_chat_json
from ..utils.llm_client import LLMClient
from ..utils.logger import get_logger
from ..utils.locale import get_language_instruction, get_locale, set_locale, t
from .zep_tools import (
    ZepToolsService, 
    SearchResult, 
    InsightForgeResult, 
    PanoramaResult,
    InterviewResult
)

logger = get_logger('mirofish.report_agent')


class ReportLogger:
    """
    Report Agent 详细日志记录器
    
    在报告文件夹中生成 agent_log.jsonl 文件，记录每一步详细动作。
    每行是一个完整的 JSON 对象，包含时间戳、动作类型、详细内容等。
    """
    
    def __init__(self, report_id: str):
        """
        初始化日志记录器
        
        Args:
            report_id: 报告ID，用于确定日志文件路径
        """
        self.report_id = report_id
        self.log_file_path = os.path.join(
            Config.UPLOAD_FOLDER, 'reports', report_id, 'agent_log.jsonl'
        )
        self.start_time = datetime.now()
        self._ensure_log_file()
    
    def _ensure_log_file(self):
        """确保日志文件所在目录存在"""
        log_dir = os.path.dirname(self.log_file_path)
        os.makedirs(log_dir, exist_ok=True)
    
    def _get_elapsed_time(self) -> float:
        """获取从开始到现在的耗时（秒）"""
        return (datetime.now() - self.start_time).total_seconds()
    
    def log(
        self, 
        action: str, 
        stage: str,
        details: Dict[str, Any],
        section_title: str = None,
        section_index: int = None
    ):
        """
        记录一条日志
        
        Args:
            action: 动作类型，如 'start', 'tool_call', 'llm_response', 'section_complete' 等
            stage: 当前阶段，如 'planning', 'generating', 'completed'
            details: 详细内容字典，不截断
            section_title: 当前章节标题（可选）
            section_index: 当前章节索引（可选）
        """
        log_entry = {
            "timestamp": datetime.now().isoformat(),
            "elapsed_seconds": round(self._get_elapsed_time(), 2),
            "report_id": self.report_id,
            "action": action,
            "stage": stage,
            "section_title": section_title,
            "section_index": section_index,
            "details": details
        }
        
        # 追加写入 JSONL 文件
        with open(self.log_file_path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(log_entry, ensure_ascii=False) + '\n')
    
    def log_start(self, simulation_id: str, graph_id: str, simulation_requirement: str):
        """记录报告生成开始"""
        self.log(
            action="report_start",
            stage="pending",
            details={
                "simulation_id": simulation_id,
                "graph_id": graph_id,
                "simulation_requirement": simulation_requirement,
                "message": t('report.taskStarted')
            }
        )
    
    def log_planning_start(self):
        """记录大纲规划开始"""
        self.log(
            action="planning_start",
            stage="planning",
            details={"message": t('report.planningStart')}
        )
    
    def log_planning_context(self, context: Dict[str, Any]):
        """记录规划时获取的上下文信息"""
        self.log(
            action="planning_context",
            stage="planning",
            details={
                "message": t('report.fetchSimContext'),
                "context": context
            }
        )
    
    def log_planning_complete(self, outline_dict: Dict[str, Any], llm: Optional[Dict[str, Any]] = None):
        """记录大纲规划完成"""
        details = {
            "message": t('report.planningComplete'),
            "outline": outline_dict
        }
        if llm is not None:
            details["llm"] = llm
        self.log(
            action="planning_complete",
            stage="planning",
            details=details
        )
    
    def log_section_start(self, section_title: str, section_index: int):
        """记录章节生成开始"""
        self.log(
            action="section_start",
            stage="generating",
            section_title=section_title,
            section_index=section_index,
            details={"message": t('report.sectionStart', title=section_title)}
        )
    
    def log_react_thought(self, section_title: str, section_index: int, iteration: int, thought: str):
        """记录 ReACT 思考过程"""
        self.log(
            action="react_thought",
            stage="generating",
            section_title=section_title,
            section_index=section_index,
            details={
                "iteration": iteration,
                "thought": thought,
                "message": t('report.reactThought', iteration=iteration)
            }
        )
    
    def log_tool_call(
        self, 
        section_title: str, 
        section_index: int,
        tool_name: str, 
        parameters: Dict[str, Any],
        iteration: int
    ):
        """记录工具调用"""
        self.log(
            action="tool_call",
            stage="generating",
            section_title=section_title,
            section_index=section_index,
            details={
                "iteration": iteration,
                "tool_name": tool_name,
                "parameters": parameters,
                "message": t('report.toolCall', toolName=tool_name)
            }
        )
    
    def log_tool_result(
        self,
        section_title: str,
        section_index: int,
        tool_name: str,
        result: str,
        iteration: int,
        cached: bool = False
    ):
        """记录工具调用结果（完整内容，不截断；cached 表示命中报告内缓存）"""
        self.log(
            action="tool_result",
            stage="generating",
            section_title=section_title,
            section_index=section_index,
            details={
                "iteration": iteration,
                "tool_name": tool_name,
                "result": result,  # 完整结果，不截断
                "result_length": len(result),
                "cached": cached,
                "message": t('report.toolResult', toolName=tool_name)
            }
        )
    
    def log_llm_response(
        self,
        section_title: str,
        section_index: int,
        response: str,
        iteration: int,
        has_tool_calls: bool,
        has_final_answer: bool,
        llm: Optional[Dict[str, Any]] = None
    ):
        """记录 LLM 响应（完整内容，不截断；llm = {stage, model, profile_id, usage}）"""
        self.log(
            action="llm_response",
            stage="generating",
            section_title=section_title,
            section_index=section_index,
            details={
                "iteration": iteration,
                "response": response,  # 完整响应，不截断
                "response_length": len(response),
                "has_tool_calls": has_tool_calls,
                "has_final_answer": has_final_answer,
                "llm": llm,
                "message": t('report.llmResponse', hasToolCalls=has_tool_calls, hasFinalAnswer=has_final_answer)
            }
        )
    
    def log_section_content(
        self,
        section_title: str,
        section_index: int,
        content: str,
        tool_calls_count: int,
        llm: Optional[Dict[str, Any]] = None
    ):
        """记录章节内容生成完成（仅记录内容，不代表整个章节完成）"""
        self.log(
            action="section_content",
            stage="generating",
            section_title=section_title,
            section_index=section_index,
            details={
                "content": content,  # 完整内容，不截断
                "content_length": len(content),
                "tool_calls_count": tool_calls_count,
                "llm": llm,
                "message": t('report.sectionContentDone', title=section_title)
            }
        )
    
    def log_section_full_complete(
        self,
        section_title: str,
        section_index: int,
        full_content: str,
        llm: Optional[Dict[str, Any]] = None
    ):
        """
        记录章节生成完成

        前端应监听此日志来判断一个章节是否真正完成，并获取完整内容
        """
        self.log(
            action="section_complete",
            stage="generating",
            section_title=section_title,
            section_index=section_index,
            details={
                "content": full_content,
                "content_length": len(full_content),
                "llm": llm,
                "message": t('report.sectionComplete', title=section_title)
            }
        )
    
    def log_report_complete(
        self,
        total_sections: int,
        total_time_seconds: float,
        llm: Optional[Dict[str, Any]] = None
    ):
        """记录报告生成完成"""
        self.log(
            action="report_complete",
            stage="completed",
            details={
                "total_sections": total_sections,
                "total_time_seconds": round(total_time_seconds, 2),
                "llm": llm,
                "message": t('report.reportComplete')
            }
        )
    
    def log_error(self, error_message: str, stage: str, section_title: str = None):
        """记录错误"""
        self.log(
            action="error",
            stage=stage,
            section_title=section_title,
            section_index=None,
            details={
                "error": error_message,
                "message": t('report.errorOccurred', error=error_message)
            }
        )


class ReportConsoleLogger:
    """
    Report Agent 控制台日志记录器
    
    将控制台风格的日志（INFO、WARNING等）写入报告文件夹中的 console_log.txt 文件。
    这些日志与 agent_log.jsonl 不同，是纯文本格式的控制台输出。
    """
    
    def __init__(self, report_id: str):
        """
        初始化控制台日志记录器
        
        Args:
            report_id: 报告ID，用于确定日志文件路径
        """
        self.report_id = report_id
        self.log_file_path = os.path.join(
            Config.UPLOAD_FOLDER, 'reports', report_id, 'console_log.txt'
        )
        self._ensure_log_file()
        self._file_handler = None
        self._setup_file_handler()
    
    def _ensure_log_file(self):
        """确保日志文件所在目录存在"""
        log_dir = os.path.dirname(self.log_file_path)
        os.makedirs(log_dir, exist_ok=True)
    
    def _setup_file_handler(self):
        """设置文件处理器，将日志同时写入文件"""
        import logging
        
        # 创建文件处理器
        self._file_handler = logging.FileHandler(
            self.log_file_path,
            mode='a',
            encoding='utf-8'
        )
        self._file_handler.setLevel(logging.INFO)
        
        # 使用与控制台相同的简洁格式
        formatter = logging.Formatter(
            '[%(asctime)s] %(levelname)s: %(message)s',
            datefmt='%H:%M:%S'
        )
        self._file_handler.setFormatter(formatter)
        
        # 添加到 report_agent 相关的 logger
        loggers_to_attach = [
            'mirofish.report_agent',
            'mirofish.zep_tools',
        ]
        
        for logger_name in loggers_to_attach:
            target_logger = logging.getLogger(logger_name)
            # 避免重复添加
            if self._file_handler not in target_logger.handlers:
                target_logger.addHandler(self._file_handler)
    
    def close(self):
        """关闭文件处理器并从 logger 中移除"""
        import logging
        
        if self._file_handler:
            loggers_to_detach = [
                'mirofish.report_agent',
                'mirofish.zep_tools',
            ]
            
            for logger_name in loggers_to_detach:
                target_logger = logging.getLogger(logger_name)
                if self._file_handler in target_logger.handlers:
                    target_logger.removeHandler(self._file_handler)
            
            self._file_handler.close()
            self._file_handler = None
    
    def __del__(self):
        """析构时确保关闭文件处理器"""
        self.close()


class ReportStatus(str, Enum):
    """报告状态"""
    PENDING = "pending"
    PLANNING = "planning"
    GENERATING = "generating"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class ReportSection:
    """报告章节"""
    title: str
    content: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "title": self.title,
            "content": self.content
        }

    def to_markdown(self, level: int = 2) -> str:
        """转换为Markdown格式"""
        md = f"{'#' * level} {self.title}\n\n"
        if self.content:
            md += f"{self.content}\n\n"
        return md


@dataclass
class ReportOutline:
    """报告大纲"""
    title: str
    summary: str
    sections: List[ReportSection]
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "title": self.title,
            "summary": self.summary,
            "sections": [s.to_dict() for s in self.sections]
        }
    
    def to_markdown(self) -> str:
        """转换为Markdown格式"""
        md = f"# {self.title}\n\n"
        md += f"> {self.summary}\n\n"
        for section in self.sections:
            md += section.to_markdown()
        return md


@dataclass
class Report:
    """完整报告"""
    report_id: str
    simulation_id: str
    graph_id: str
    simulation_requirement: str
    status: ReportStatus
    outline: Optional[ReportOutline] = None
    markdown_content: str = ""
    created_at: str = ""
    completed_at: str = ""
    error: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "report_id": self.report_id,
            "simulation_id": self.simulation_id,
            "graph_id": self.graph_id,
            "simulation_requirement": self.simulation_requirement,
            "status": self.status.value,
            "outline": self.outline.to_dict() if self.outline else None,
            "markdown_content": self.markdown_content,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
            "error": self.error
        }


# ═══════════════════════════════════════════════════════════════
# Prompt template constants
# Instruction bodies are English; the output language is appended at the END of
# each system prompt via get_language_instruction(). System prompts contain no
# per-report data so they stay byte-identical across calls (provider prefix
# caching); dynamic data goes into user messages.
# ═══════════════════════════════════════════════════════════════

REPORT_STAGE = "report"
CHAT_STAGE = "report_chat"

# ── Tool descriptions ──

TOOL_DESC_INSIGHT_FORGE = """\
[Deep insight retrieval - the most thorough tool]
Designed for in-depth analysis. It:
1. Breaks your question into several sub-questions
2. Retrieves information from the simulation graph along several dimensions
3. Combines semantic search, entity analysis, and relationship-chain tracing
4. Returns the most comprehensive material

Use when:
- A topic needs deep analysis
- You need several aspects of an event
- You need rich material to support a section

Returns:
- Relevant facts verbatim (quotable)
- Key entity insights
- Relationship-chain analysis"""

TOOL_DESC_PANORAMA_SEARCH = """\
[Panorama search - the full picture]
Retrieves the complete picture of the simulation results; best for understanding how events evolved. It:
1. Fetches all related nodes and relationships
2. Separates currently valid facts from historical/expired facts
3. Shows how public opinion changed over time

Use when:
- You need the full timeline of an event
- You need to compare opinion across stages
- You need comprehensive entity and relationship information

Returns:
- Currently valid facts (latest simulation state)
- Historical/expired facts (evolution record)
- All entities involved"""

TOOL_DESC_QUICK_SEARCH = """\
[Quick search - fast lookup]
Lightweight retrieval for simple, direct questions.

Use when:
- You need one specific piece of information
- You need to verify a fact
- A simple lookup is enough

Returns:
- The facts most relevant to the query"""

TOOL_DESC_INTERVIEW_AGENTS = """\
[Agent interviews - real answers from simulated agents (both platforms)]
Calls the OASIS interview API to interview the running simulated agents.
This is not an LLM imitation: it returns the agents' own answers from the simulation.
By default it interviews on both the Twitter-like and Reddit-like platforms.

How it works:
1. Reads the persona files to learn about all simulated agents
2. Picks the agents most relevant to the interview topic (e.g. residents, media, officials)
3. Generates interview questions automatically
4. Calls /api/simulation/interview/batch on both platforms
5. Combines all answers into a multi-perspective summary

Use when:
- You need the views of different roles on an event
- You need to collect opinions and positions from several sides
- You want first-person answers from simulated agents
- You want the report to include interview excerpts

Returns:
- Identity of each interviewed agent
- Each agent's answers on both platforms
- Key quotes (quotable)
- Interview summary and comparison of views

[Important] Requires the OASIS simulation environment to be running."""

# ── Outline-planning prompt ──

PLAN_SYSTEM_PROMPT = """\
You are an expert author of "future forecast reports". You have a god's-eye view of a simulated world: you can see every agent's behavior, statements, and interactions.

[Core idea]
We built a simulated world and injected a specific "simulation requirement" into it as the variable. How the simulated world evolves is a forecast of what may happen in the future. What you are looking at is not experimental data but a rehearsal of the future.

[Your task]
Write a future forecast report that answers:
1. Under the conditions we set, what happened in the future?
2. How did the different groups of agents (people) react and act?
3. Which future trends and risks does the simulation reveal?

[Report positioning]
- DO: a simulation-based forecast that shows "if this happens, what follows"
- DO: focus on forecast results: how events unfold, group reactions, emergent phenomena, potential risks
- DO: treat agents' statements and actions in the simulation as forecasts of how people will behave
- DO NOT: analyze the current state of the real world
- DO NOT: write a generic overview of public opinion

[Section limits]
- At least 2 and at most 5 sections
- No subsections; each section is written as one complete body of text
- Keep the content concise and focused on the core forecast findings
- Design the section structure yourself based on the forecast results

Output the report outline as JSON in exactly this format:
{
    "title": "Report title",
    "summary": "Report summary (one sentence with the core forecast finding)",
    "sections": [
        {
            "title": "Section title",
            "description": "What the section covers"
        }
    ]
}

Note: the sections array must have at least 2 and at most 5 elements."""

PLAN_USER_PROMPT_TEMPLATE = """\
[Forecast scenario]
Variable injected into the simulated world (simulation requirement): {simulation_requirement}

[Scale of the simulated world]
- Entities in the simulation: {total_nodes}
- Relationships between entities: {total_edges}
- Entity types: {entity_types}
- Active agents: {total_entities}
{model_mix_block}
[Sample of future facts produced by the simulation]
{related_facts_json}

Review this rehearsal of the future from a god's-eye view:
1. Under the conditions we set, what state did the future reach?
2. How did each group (agents) react and act?
3. Which future trends deserve attention?

Design the most suitable section structure for the report based on the forecast results.

[Reminder] 2 to 5 sections, concise and focused on the core forecast findings."""

MODEL_MIX_BLOCK_TEMPLATE = "\n[AI models behind the agents]\n{sentence}\n"

# ── Section-generation prompt ──

SECTION_SYSTEM_PROMPT_TEMPLATE = """\
You are an expert author of "future forecast reports" and you are writing one section of a report. The report title, summary, scenario, section to write, and previously written sections are given in the user message.

==============================================================
[Core idea]
==============================================================

The simulated world is a rehearsal of the future. We injected specific conditions (the simulation requirement) into it; the agents' behavior and interactions are forecasts of how people will behave.

Your task:
- Show what happened in the future under the given conditions
- Forecast how different groups (agents) reacted and acted
- Identify future trends, risks, and opportunities worth attention

DO NOT write an analysis of the real world's current state.
DO focus on "what the future looks like": the simulation results are the forecast.

==============================================================
[Most important rules - must follow]
==============================================================

1. [Observe the simulated world with tools]
   - You are observing a rehearsal of the future from a god's-eye view
   - All content must come from events and agent behavior in the simulated world
   - Do not use your own knowledge to write report content
   - Call tools at least {min_tool_calls} times (at most {max_tool_calls}) per section to observe the simulated world

2. [Quote agents' original statements and actions]
   - Agents' statements and actions are forecasts of future behavior
   - Present them as quotes, for example:
     > "A group of residents would say: original text..."
   - These quotes are the core evidence of the forecast

3. [Language consistency - translate quoted material]
   - Tool results may be in a language different from the report language
   - Write the whole report in the output language specified at the end of these instructions
   - When quoting tool content in another language, translate it into the report language first
   - Keep the original meaning and make the wording natural
   - This applies to body text and to quote blocks (> format)

4. [Present the forecast faithfully]
   - The content must reflect the simulation results that represent the future
   - Do not add information that does not exist in the simulation
   - If information on some aspect is insufficient, say so

5. [No fabricated data]
   - Never invent user names, quotes, statistics, or engagement figures
   - Never include <tool_result> blocks in your reply; only the system provides tool results
   - Only cite entities, quotes, and data that actually appear in tool results
   - If tool results contain nothing relevant, say so instead of making something up

==============================================================
[Formatting rules - very important]
==============================================================

[One section = the smallest content unit]
- Each section is the smallest unit of the report
- Do not use any Markdown headings (#, ##, ###, ####, etc.) inside the section
- Do not start with the section title
- The system adds the section title; write body text only
- Use **bold**, paragraph breaks, quotes, and lists to structure the content, never headings

[Correct example]
```
This section examines how the discussion spread. The simulation shows that...

**Initial spike**

Early posts on the main platform set the tone of the debate:

> "A quote copied verbatim from a tool result..."

**Amplification**

Reposts by well-connected accounts widened the reach:

- First observation
- Second observation
```

[Wrong example]
```
## Executive summary      <- wrong: no headings
### 1. First stage        <- wrong: no ### subsections
#### 1.1 Details          <- wrong: no #### either

This section analyzes...
```

==============================================================
[Retrieval tools] (call {min_tool_calls}-{max_tool_calls} times per section)
==============================================================

{tools_description}

[Tool tips - mix different tools instead of using only one]
- insight_forge: deep analysis; decomposes the question and retrieves facts and relationships from several angles
- panorama_search: wide panorama; the full picture, timeline, and evolution of events
- quick_search: quickly verify one specific point
- interview_agents: interview simulated agents for first-person views and reactions from different roles

==============================================================
[Workflow]
==============================================================

In each reply do exactly ONE of the following (never both):

Option A - call tools:
Write your reasoning, then call one or more tools (at most {max_parallel_tools} per reply, each in its own block):
<tool_call>
{{"name": "tool_name", "parameters": {{"param_name": "value"}}}}
</tool_call>
The system runs the tools and returns the results to you. Never write tool results yourself.

Option B - write the final content:
Once the tools have given you enough information, start your reply with "Final Answer:" followed by the section content.

Strictly forbidden:
- Tool calls and Final Answer in the same reply
- Inventing tool results (Observation); all tool results are injected by the system
- More than {max_parallel_tools} tool calls in one reply

==============================================================
[Section content requirements]
==============================================================

1. Content must be based on the simulation data retrieved with tools
2. Quote original text generously to show what the simulation produced
3. Use Markdown (but no headings):
   - **bold text** marks key points (instead of subheadings)
   - Lists (- or 1. 2. 3.) organize points
   - Blank lines separate paragraphs
   - Never use #, ##, ###, #### or any heading syntax
4. [Quote formatting - quotes stand alone]
   A quote is its own paragraph with a blank line before and after; never inline:

   Correct:
   ```
   The official response was seen as lacking substance.

   > "The response felt rigid and slow for a fast-moving online debate."

   This reaction reflects wider frustration.
   ```

   Wrong:
   ```
   The official response was seen as lacking substance. > "The response felt..." This reaction reflects...
   ```
5. Keep the section logically consistent with the other sections
6. [Avoid repetition] Read the previously written sections in the user message and do not repeat the same information
7. [Again] No headings; use **bold** instead of subsection titles"""

SECTION_USER_PROMPT_TEMPLATE = """\
Report title: {report_title}
Report summary: {report_summary}
Forecast scenario (simulation requirement): {simulation_requirement}
{model_mix_block}
Previously written sections (read carefully and do not repeat them):
{previous_content}

==============================================================
[Current task] Write the section: {section_title}
==============================================================

[Reminders]
1. Read the sections above and do not repeat their content
2. Call tools to get simulation data before writing
3. Mix different tools instead of using only one
4. Report content must come from retrieval results, not from your own knowledge

[Format warning - must follow]
- No headings at all (#, ##, ###, #### are all forbidden)
- Do not start with "{section_title}"
- The system adds the section title
- Write body text directly; use **bold** instead of subsection titles

Begin:
1. Think (Thought) about what information this section needs
2. Call tools (Action) to get simulation data
3. When you have enough, output the Final Answer (body text only, no headings)"""

FIRST_SECTION_PLACEHOLDER = "(This is the first section.)"
PREVIOUS_SECTIONS_OMITTED = "(Previous sections omitted for length.)"

# ── ReACT loop message templates ──

REACT_OBSERVATION_TEMPLATE = """\
Observation (retrieval results):

{results}

==============================================================
Tool calls used: {tool_calls_count}/{max_tool_calls} (used: {used_tools_str}){unused_hint}
- If you have enough information: start with "Final Answer:" and write the section content (quote the material above)
- If you need more information: call more tools
=============================================================="""

REACT_OBSERVATION_RESULT_TEMPLATE = "=== Tool {tool_name} returned{cached_note} ===\n{result}"

REACT_CACHED_NOTE = " (cached: identical call already made earlier; try a different query)"

REACT_INSUFFICIENT_TOOLS_MSG = (
    "[Note] You have called tools only {tool_calls_count} times; at least {min_tool_calls} are required. "
    "Call more tools to get simulation data before writing the Final Answer.{unused_hint}"
)

REACT_INSUFFICIENT_TOOLS_MSG_ALT = (
    "So far you have called tools {tool_calls_count} times; at least {min_tool_calls} are required. "
    "Call tools to get simulation data.{unused_hint}"
)

REACT_TOOL_LIMIT_MSG = (
    "The tool call limit has been reached ({tool_calls_count}/{max_tool_calls}); no more tool calls are allowed. "
    'Write the section now based on the information you have, starting with "Final Answer:".'
)

REACT_UNUSED_TOOLS_HINT = "\nTip: you have not used {unused_list} yet; try different tools for more perspectives."

REACT_INSUFFICIENT_UNUSED_HINT = " (Not used yet, worth trying: {unused_list})"

REACT_FORCE_FINAL_MSG = 'The tool call limit has been reached. Output "Final Answer:" followed by the section content now.'

REACT_FORMAT_ERROR_MSG = (
    "[Format error] Your reply contained both tool calls and a Final Answer, which is not allowed.\n"
    "Each reply must do exactly one of:\n"
    "- call tools (one or more <tool_call> blocks, no Final Answer)\n"
    "- write the final content (starting with 'Final Answer:', no <tool_call>)\n"
    "Reply again and do only one of them."
)

REACT_EMPTY_RESPONSE_PLACEHOLDER = "(empty response)"
REACT_CONTINUE_MSG = "Please continue."

TOOL_RESULT_TRUNCATION_MARKER = "\n\n[... truncated {omitted} characters of tool output ...]"

# ── Chat prompt ──

CHAT_SYSTEM_PROMPT_TEMPLATE = """\
You are a concise, efficient assistant for a simulation-based forecast. The forecast conditions and the generated report are given in the first user message.

[Rules]
1. Answer primarily from the report content
2. Answer the question directly; avoid long deliberation
3. Call tools for more data only when the report is not enough to answer
4. Keep answers concise, clear, and well organized

[Available tools] (only when needed, at most {max_tool_calls} calls)
{tools_description}

[Tool call format]
<tool_call>
{{"name": "tool_name", "parameters": {{"param_name": "value"}}}}
</tool_call>

[Answer style]
- Short and direct, no long essays
- Quote key content with the > format
- Give the conclusion first, then the reasons"""

CHAT_CONTEXT_TEMPLATE = """\
[Background]
Forecast conditions: {simulation_requirement}

[Generated analysis report]
{report_content}"""

CHAT_CONTEXT_ACK = "Understood. I will answer from this report and use tools only when needed."

CHAT_NO_REPORT = "(No report yet.)"

CHAT_REPORT_TRUNCATED = "\n\n... [report truncated] ..."

CHAT_OBSERVATION_TEMPLATE = "[{tool_name} result]\n{result}"

CHAT_OBSERVATION_SUFFIX = "\n\nAnswer the question concisely."

_TOOL_CALL_OPEN_RE = re.compile(r'<tool_call\s*>', re.IGNORECASE)
_TOOL_CALL_BOUNDARY_RE = re.compile(r'</?tool_call\s*>', re.IGNORECASE)
_BARE_TOOL_CALL_START_RE = re.compile(r'\{\s*"(?:name|tool)"\s*:')
_JSON_DECODER = json.JSONDecoder()

_USAGE_KEYS = ("input_tokens", "cached_tokens", "output_tokens")


def _empty_llm_stats(stage: str) -> Dict[str, Any]:
    return {
        "stage": stage,
        "model": None,
        "profile_id": None,
        "calls": 0,
        "usage": {key: 0 for key in _USAGE_KEYS},
    }


def _add_llm_stats(stats: Dict[str, Any], meta: Dict[str, Any]) -> None:
    """Accumulate one call's metadata (model/profile/usage) into a stats dict."""
    stats["calls"] += 1
    stats["model"] = meta.get("model") or stats["model"]
    stats["profile_id"] = meta.get("profile_id") or stats["profile_id"]
    usage = meta.get("usage") or {}
    for key in _USAGE_KEYS:
        value = usage.get(key)
        if isinstance(value, (int, float)):
            stats["usage"][key] += int(value)


# ═══════════════════════════════════════════════════════════════
# ReportAgent 主类
# ═══════════════════════════════════════════════════════════════


class ReportAgent:
    """
    Report Agent - 模拟报告生成Agent

    采用ReACT（Reasoning + Acting）模式：
    1. 规划阶段：分析模拟需求，规划报告目录结构
    2. 生成阶段：逐章节生成内容，每章节可多次调用工具获取信息
    3. 反思阶段：检查内容完整性和准确性

    工具调用：每轮最多并行执行 REPORT_MAX_PARALLEL_TOOLS 个不同调用；相同调用
    （名称+参数）在同一报告内只执行一次（缓存）；观察结果截断到
    REPORT_TOOL_RESULT_MAX_CHARS；每章节总数受 REPORT_AGENT_MAX_TOOL_CALLS 限制。
    """

    # 最大工具调用次数（每个章节）的默认值；实例值来自 Config.REPORT_AGENT_MAX_TOOL_CALLS
    MAX_TOOL_CALLS_PER_SECTION = 5

    # 每个章节最少工具调用次数（不超过最大值）
    MIN_TOOL_CALLS_PER_SECTION = 3

    # 最大反思轮数
    MAX_REFLECTION_ROUNDS = 3

    # 对话中的最大工具调用次数
    MAX_TOOL_CALLS_PER_CHAT = 2

    # 对话中单个工具结果的最大长度
    CHAT_TOOL_RESULT_MAX_CHARS = 1500

    # 对话历史最多保留的消息条数
    CHAT_HISTORY_MAX_MESSAGES = 10

    # 对话中报告内容的最大长度
    CHAT_REPORT_MAX_CHARS = 15000

    # 每个已完成章节在上下文中的最大长度
    PREVIOUS_SECTION_MAX_CHARS = 4000

    # 章节生成的输出 token 上限
    SECTION_MAX_TOKENS = 4096

    # 合法的工具名称集合，用于裸 JSON 兜底解析时校验
    VALID_TOOL_NAMES = {"insight_forge", "panorama_search", "quick_search", "interview_agents"}

    def __init__(
        self,
        graph_id: str,
        simulation_id: str,
        simulation_requirement: str,
        llm_client: Optional[LLMClient] = None,
        zep_tools: Optional[ZepToolsService] = None
    ):
        """
        初始化Report Agent

        Args:
            graph_id: 图谱ID
            simulation_id: 模拟ID
            simulation_requirement: 模拟需求描述
            llm_client: LLM客户端（可选；同时用于报告和对话）
            zep_tools: Zep工具服务（可选）
        """
        self.graph_id = graph_id
        self.simulation_id = simulation_id
        self.simulation_requirement = simulation_requirement

        self.llm = llm_client or LLMClient(stage=REPORT_STAGE)
        # 对话阶段使用独立路由（report_chat）；注入的客户端两者共用
        self._chat_llm: Optional[LLMClient] = llm_client
        self.zep_tools = zep_tools or ZepToolsService()

        # 限制参数（来自配置，实例化时读取）
        self.max_tool_calls = max(1, int(Config.REPORT_AGENT_MAX_TOOL_CALLS))
        self.min_tool_calls = min(self.MIN_TOOL_CALLS_PER_SECTION, self.max_tool_calls)
        self.max_parallel_tools = max(1, int(Config.REPORT_MAX_PARALLEL_TOOLS))
        self.tool_result_max_chars = max(1, int(Config.REPORT_TOOL_RESULT_MAX_CHARS))
        self.prev_sections_max_chars = max(0, int(Config.REPORT_PREV_SECTIONS_MAX_CHARS))
        self.chat_history_max_chars = max(0, int(Config.REPORT_CHAT_HISTORY_MAX_CHARS))
        self.temperature = float(Config.REPORT_AGENT_TEMPERATURE)

        # 工具定义
        self.tools = self._define_tools()

        # 报告内工具结果缓存：相同 (工具名, 参数) 只执行一次
        self._tool_cache: Dict[str, str] = {}
        self._tool_cache_lock = threading.Lock()
        self.tool_executions = 0
        self.tool_cache_hits = 0

        # LLM 用量统计（每章节 + 整份报告）
        self.section_llm_stats: Dict[int, Dict[str, Any]] = {}
        self.report_llm_stats: Dict[str, Any] = _empty_llm_stats(REPORT_STAGE)

        # 日志记录器（在 generate_report 中初始化）
        self.report_logger: Optional[ReportLogger] = None
        # 控制台日志记录器（在 generate_report 中初始化）
        self.console_logger: Optional[ReportConsoleLogger] = None

        logger.info(t('report.agentInitDone', graphId=graph_id, simulationId=simulation_id))

    def _define_tools(self) -> Dict[str, Dict[str, Any]]:
        """定义可用工具"""
        return {
            "insight_forge": {
                "name": "insight_forge",
                "description": TOOL_DESC_INSIGHT_FORGE,
                "parameters": {
                    "query": "The question or topic you want to analyze in depth",
                    "report_context": "Context of the current report section (optional; helps generate sharper sub-questions)"
                }
            },
            "panorama_search": {
                "name": "panorama_search",
                "description": TOOL_DESC_PANORAMA_SEARCH,
                "parameters": {
                    "query": "Search query used for relevance ranking",
                    "include_expired": "Whether to include expired/historical content (default True)"
                }
            },
            "quick_search": {
                "name": "quick_search",
                "description": TOOL_DESC_QUICK_SEARCH,
                "parameters": {
                    "query": "Search query string",
                    "limit": "Number of results (optional, default 10)"
                }
            },
            "interview_agents": {
                "name": "interview_agents",
                "description": TOOL_DESC_INTERVIEW_AGENTS,
                "parameters": {
                    "interview_topic": "Interview topic or goal (e.g. 'how local residents view the new policy')",
                    "max_agents": "Maximum number of agents to interview (optional, default 5, max 10)"
                }
            }
        }

    # ── LLM 调用（经 app.llm 接缝，记录模型与用量） ──

    def _client_for_stage(self, stage: str) -> Any:
        if stage == CHAT_STAGE:
            if self._chat_llm is None:
                self._chat_llm = LLMClient(stage=CHAT_STAGE)
            return self._chat_llm
        return self.llm

    def _call_llm(
        self,
        messages: List[Dict[str, str]],
        *,
        stage: str,
        temperature: float,
        max_tokens: Optional[int],
    ) -> ChatResult:
        """Send one chat request; returns text plus model/profile/usage metadata."""
        client = self._client_for_stage(stage)
        profile = getattr(client, "profile", None)
        if isinstance(profile, LLMProfile):
            return llm_chat(
                messages,
                stage=stage,
                temperature=temperature,
                max_tokens=max_tokens,
                profile=profile,
            )
        # 测试替身或旧式客户端：只返回文本，无用量信息
        text = client.chat(messages=messages, temperature=temperature, max_tokens=max_tokens)
        return ChatResult(
            text=text,
            model=str(getattr(client, "model", "") or ""),
            profile_id="",
            usage={},
        )

    def _call_llm_json(
        self,
        messages: List[Dict[str, str]],
        *,
        stage: str,
        temperature: float,
        max_tokens: Optional[int],
    ) -> Dict[str, Any]:
        client = self._client_for_stage(stage)
        profile = getattr(client, "profile", None)
        if isinstance(profile, LLMProfile):
            return llm_chat_json(
                messages,
                stage=stage,
                temperature=temperature,
                max_tokens=max_tokens,
                profile=profile,
            )
        return client.chat_json(messages=messages, temperature=temperature, max_tokens=max_tokens)

    def _client_meta(self, stage: str) -> Dict[str, Any]:
        client = self._client_for_stage(stage)
        profile = getattr(client, "profile", None)
        return {
            "stage": stage,
            "model": getattr(profile, "model", None) or getattr(client, "model", None),
            "profile_id": getattr(profile, "id", None),
            "usage": None,
        }

    @staticmethod
    def _result_meta(result: ChatResult, stage: str) -> Dict[str, Any]:
        return {
            "stage": stage,
            "model": result.model or None,
            "profile_id": result.profile_id or None,
            "usage": dict(result.usage or {}),
        }

    # ── 工具执行 ──

    def _execute_tool(self, tool_name: str, parameters: Dict[str, Any], report_context: str = "") -> str:
        """
        执行工具调用（失败时返回错误文本，不抛出）

        Args:
            tool_name: 工具名称
            parameters: 工具参数
            report_context: 报告上下文（用于InsightForge）

        Returns:
            工具执行结果（文本格式）
        """
        try:
            return self._run_tool(tool_name, parameters, report_context)
        except Exception as e:
            logger.error(t('report.toolExecFailed', toolName=tool_name, error=str(e)))
            return f"Tool execution failed: {str(e)}"

    def _run_tool(self, tool_name: str, parameters: Dict[str, Any], report_context: str = "") -> str:
        """执行工具调用（异常向上抛出）"""
        logger.info(t('report.executingTool', toolName=tool_name, params=parameters))

        if tool_name == "insight_forge":
            query = parameters.get("query", "")
            ctx = parameters.get("report_context", "") or report_context
            result = self.zep_tools.insight_forge(
                graph_id=self.graph_id,
                query=query,
                simulation_requirement=self.simulation_requirement,
                report_context=ctx
            )
            return result.to_text()

        elif tool_name == "panorama_search":
            # 广度搜索 - 获取全貌
            query = parameters.get("query", "")
            include_expired = parameters.get("include_expired", True)
            if isinstance(include_expired, str):
                include_expired = include_expired.lower() in ['true', '1', 'yes']
            result = self.zep_tools.panorama_search(
                graph_id=self.graph_id,
                query=query,
                include_expired=include_expired
            )
            return result.to_text()

        elif tool_name == "quick_search":
            # 简单搜索 - 快速检索
            query = parameters.get("query", "")
            limit = parameters.get("limit", 10)
            if isinstance(limit, str):
                limit = int(limit)
            result = self.zep_tools.quick_search(
                graph_id=self.graph_id,
                query=query,
                limit=limit
            )
            return result.to_text()

        elif tool_name == "interview_agents":
            # 深度采访 - 调用真实的OASIS采访API获取模拟Agent的回答（双平台）
            interview_topic = parameters.get("interview_topic", parameters.get("query", ""))
            max_agents = parameters.get("max_agents", 5)
            if isinstance(max_agents, str):
                max_agents = int(max_agents)
            max_agents = min(max_agents, 10)
            result = self.zep_tools.interview_agents(
                simulation_id=self.simulation_id,
                interview_requirement=interview_topic,
                simulation_requirement=self.simulation_requirement,
                max_agents=max_agents
            )
            return result.to_text()

        # ========== 向后兼容的旧工具（内部重定向到新工具） ==========

        elif tool_name == "search_graph":
            # 重定向到 quick_search
            logger.info(t('report.redirectToQuickSearch'))
            return self._run_tool("quick_search", parameters, report_context)

        elif tool_name == "get_graph_statistics":
            result = self.zep_tools.get_graph_statistics(self.graph_id)
            return json.dumps(result, ensure_ascii=False, indent=2)

        elif tool_name == "get_entity_summary":
            entity_name = parameters.get("entity_name", "")
            result = self.zep_tools.get_entity_summary(
                graph_id=self.graph_id,
                entity_name=entity_name
            )
            return json.dumps(result, ensure_ascii=False, indent=2)

        elif tool_name == "get_simulation_context":
            # 重定向到 insight_forge，因为它更强大
            logger.info(t('report.redirectToInsightForge'))
            query = parameters.get("query", self.simulation_requirement)
            return self._run_tool("insight_forge", {"query": query}, report_context)

        elif tool_name == "get_entities_by_type":
            entity_type = parameters.get("entity_type", "")
            nodes = self.zep_tools.get_entities_by_type(
                graph_id=self.graph_id,
                entity_type=entity_type
            )
            result = [n.to_dict() for n in nodes]
            return json.dumps(result, ensure_ascii=False, indent=2)

        return (
            f"Unknown tool: {tool_name}. Use one of: "
            "insight_forge, panorama_search, quick_search, interview_agents"
        )

    @staticmethod
    def _tool_cache_key(tool_name: str, parameters: Dict[str, Any]) -> str:
        try:
            params = json.dumps(parameters, sort_keys=True, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            params = repr(parameters)
        return f"{tool_name}\x00{params}"

    def _execute_tool_cached(
        self,
        tool_name: str,
        parameters: Dict[str, Any],
        report_context: str = "",
    ) -> Tuple[str, bool]:
        """执行工具；相同调用复用报告内缓存。返回 (完整结果, 是否命中缓存)。失败结果不缓存。"""
        key = self._tool_cache_key(tool_name, parameters)
        with self._tool_cache_lock:
            if key in self._tool_cache:
                self.tool_cache_hits += 1
                cached = self._tool_cache[key]
                hit = True
            else:
                self.tool_executions += 1
                hit = False
        if hit:
            logger.info(t('report.toolCacheHit', toolName=tool_name))
            return cached, True
        try:
            result = self._run_tool(tool_name, parameters, report_context)
        except Exception as e:
            logger.error(t('report.toolExecFailed', toolName=tool_name, error=str(e)))
            return f"Tool execution failed: {str(e)}", False
        with self._tool_cache_lock:
            self._tool_cache[key] = result
        return result, False

    def _execute_tool_batch(
        self,
        calls: List[Dict[str, Any]],
        report_context: str = "",
    ) -> List[Tuple[str, bool]]:
        """并行执行一批不同的工具调用，结果按输入顺序返回"""
        if len(calls) <= 1:
            return [
                self._execute_tool_cached(call["name"], call.get("parameters", {}), report_context)
                for call in calls
            ]
        locale = get_locale()

        def run(call: Dict[str, Any]) -> Tuple[str, bool]:
            set_locale(locale)
            return self._execute_tool_cached(call["name"], call.get("parameters", {}), report_context)

        logger.info(t('report.parallelTools', count=len(calls)))
        workers = min(len(calls), self.max_parallel_tools)
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="report-tool") as pool:
            return list(pool.map(run, calls))

    def _select_tool_calls(self, tool_calls: List[Dict[str, Any]], budget: int) -> List[Dict[str, Any]]:
        """去重（保留首次出现）并按预算和并行上限截取"""
        limit = max(0, min(budget, self.max_parallel_tools))
        selected: List[Dict[str, Any]] = []
        seen = set()
        for call in tool_calls:
            key = self._tool_cache_key(call["name"], call.get("parameters", {}))
            if key in seen:
                continue
            seen.add(key)
            if len(selected) >= limit:
                break
            selected.append(call)
        if len(selected) < len(tool_calls):
            logger.info(t('report.toolCallsTrimmed', total=len(tool_calls), kept=len(selected)))
        return selected

    @staticmethod
    def _truncate_text(text: str, limit: int) -> str:
        """截断过长的工具结果，并附加截断标记"""
        if text is None:
            return ""
        if len(text) <= limit:
            return text
        return text[:limit] + TOOL_RESULT_TRUNCATION_MARKER.format(omitted=len(text) - limit)

    # ── 工具调用解析 ──

    @classmethod
    def _normalize_tool_call(cls, data: Any, require_known: bool) -> Optional[Dict[str, Any]]:
        """把解析出的 JSON 规范化为 {"name", "parameters"}；不合法时返回 None"""
        if not isinstance(data, dict):
            return None
        name = data.get("name", data.get("tool"))
        if not isinstance(name, str) or not name.strip():
            return None
        name = name.strip()
        if require_known and name not in cls.VALID_TOOL_NAMES:
            return None
        if "parameters" in data:
            params = data["parameters"]
        else:
            params = data.get("params", {})
        if params is None:
            params = {}
        if not isinstance(params, dict):
            return None
        return {"name": name, "parameters": params}

    def _parse_tool_calls(self, response: str) -> List[Dict[str, Any]]:
        """
        从LLM响应中解析工具调用（永不抛出）

        支持的格式（按优先级）：
        1. <tool_call>{"name": "tool_name", "parameters": {...}}</tool_call>（可多个）
        2. 裸 JSON（响应整体或末尾就是一个已知工具的调用 JSON）
        """
        if not isinstance(response, str) or not response:
            return []

        tool_calls: List[Dict[str, Any]] = []

        # 格式1: XML风格（标准格式）。每个 <tool_call> 的内容截止到下一个开/闭标签，
        # 这样一个未闭合或损坏的调用不会吞掉后面的合法调用。
        for opener in _TOOL_CALL_OPEN_RE.finditer(response):
            start = opener.end()
            boundary = _TOOL_CALL_BOUNDARY_RE.search(response, start)
            end = boundary.start() if boundary else len(response)
            segment = response[start:end].strip()
            if not segment.startswith('{'):
                continue
            try:
                data, _ = _JSON_DECODER.raw_decode(segment)
            except (ValueError, RecursionError):
                continue
            call = self._normalize_tool_call(data, require_known=False)
            if call:
                tool_calls.append(call)

        if tool_calls:
            return tool_calls

        # 格式2: 兜底 - LLM 直接输出裸 JSON（没包 <tool_call> 标签）
        # 只在格式1未匹配时尝试，且必须是已知工具名，避免误匹配正文中的 JSON
        stripped = response.strip()
        candidates = []
        if stripped.startswith('{'):
            candidates.append(0)
        candidates.extend(m.start() for m in _BARE_TOOL_CALL_START_RE.finditer(stripped))
        for pos in candidates:
            try:
                data, end = _JSON_DECODER.raw_decode(stripped, pos)
            except (ValueError, RecursionError):
                continue
            if stripped[end:].strip():
                continue
            call = self._normalize_tool_call(data, require_known=True)
            if call:
                return [call]

        return []

    def _get_tools_description(self) -> str:
        """生成工具描述文本（静态，便于前缀缓存）"""
        desc_parts = ["Available tools:"]
        for name, tool in self.tools.items():
            params_desc = ", ".join([f"{k}: {v}" for k, v in tool["parameters"].items()])
            desc_parts.append(f"- {name}: {tool['description']}")
            if params_desc:
                desc_parts.append(f"  Parameters: {params_desc}")
        return "\n".join(desc_parts)

    def _section_system_prompt(self) -> str:
        """章节系统提示词：同一进程配置下逐字节不变，语言指令在末尾"""
        body = SECTION_SYSTEM_PROMPT_TEMPLATE.format(
            tools_description=self._get_tools_description(),
            min_tool_calls=self.min_tool_calls,
            max_tool_calls=self.max_tool_calls,
            max_parallel_tools=self.max_parallel_tools,
        )
        return f"{body}\n\n{get_language_instruction()}"

    def _chat_system_prompt(self) -> str:
        body = CHAT_SYSTEM_PROMPT_TEMPLATE.format(
            tools_description=self._get_tools_description(),
            max_tool_calls=self.MAX_TOOL_CALLS_PER_CHAT,
        )
        return f"{body}\n\n{get_language_instruction()}"

    @staticmethod
    def _strip_fake_tool_results(response: str) -> str:
        """Strip any <tool_result> blocks the LLM fabricated in its response.

        When the LLM generates a <tool_call> block and then continues to generate
        a <tool_result> block in the same response, we must strip the fake result
        before appending to message history. The real tool result will be injected
        separately by the system.
        """
        tag_pattern = re.compile(r'</?tool_result\b[^>]*>', flags=re.IGNORECASE)
        parts = []
        cursor = 0
        depth = 0

        for match in tag_pattern.finditer(response):
            if depth == 0:
                parts.append(response[cursor:match.start()])

            if match.group(0).lstrip().startswith('</'):
                depth = max(0, depth - 1)
            else:
                depth += 1
            cursor = match.end()

        if depth == 0:
            parts.append(response[cursor:])

        cleaned = ''.join(parts)
        # Treat a malformed opening tag without a closing `>` as unsafe too.
        cleaned = re.sub(r'<tool_result\b.*$', '', cleaned, flags=re.IGNORECASE | re.DOTALL)
        cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
        return cleaned.strip()

    # ── 上下文构建 ──

    def _load_llm_models(self) -> Dict[str, Any]:
        """读取模拟配置中的 llm_models（{profile_id: {label, model, count}}）；缺失时返回空"""
        path = os.path.join(Config.OASIS_SIMULATION_DATA_DIR, self.simulation_id, "simulation_config.json")
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except (OSError, ValueError):
            return {}
        models = data.get("llm_models") if isinstance(data, dict) else None
        return models if isinstance(models, dict) else {}

    def _model_mix_sentence(self) -> str:
        """一句话说明哪些模型驱动了多少比例的 Agent（供报告把 AI 模型当作分析变量）"""
        entries = []
        for profile_id, info in self._load_llm_models().items():
            if not isinstance(info, dict):
                continue
            try:
                count = int(info.get("count", 0))
            except (TypeError, ValueError):
                continue
            if count <= 0:
                continue
            model = str(info.get("model") or profile_id)
            label = str(info.get("label") or profile_id)
            entries.append((model, label, count))
        total = sum(count for _, _, count in entries)
        if not entries or total <= 0:
            return ""
        parts = [
            f"{model} ({label}) drove {count * 100 / total:.0f}% of agents ({count}/{total})"
            for model, label, count in entries
        ]
        sentence = "AI model mix among the simulated agents: " + "; ".join(parts)
        if len({model for model, _, _ in entries}) > 1:
            sentence += (
                "; where relevant, treat the AI model as an analysis variable and compare how agents "
                "driven by different models behaved, without overstating differences"
            )
        return sentence + "."

    def _model_mix_block(self) -> str:
        sentence = self._model_mix_sentence()
        return MODEL_MIX_BLOCK_TEMPLATE.format(sentence=sentence) if sentence else ""

    def _build_previous_context(self, previous_sections: List[str]) -> str:
        """已完成章节上下文：单章节上限 + 总量上限（优先保留最近的章节）"""
        if not previous_sections:
            return FIRST_SECTION_PLACEHOLDER
        separator = "\n\n---\n\n"
        budget = self.prev_sections_max_chars
        per_section = self.PREVIOUS_SECTION_MAX_CHARS
        parts: List[str] = []
        used = 0
        for sec in reversed(previous_sections):
            piece = sec if len(sec) <= per_section else sec[:per_section] + "..."
            extra = len(separator) if parts else 0
            room = budget - used - extra
            if room <= 3:
                break
            if len(piece) > room:
                parts.append(piece[:room - 3] + "...")
                break
            parts.append(piece)
            used += extra + len(piece)
        if not parts:
            return PREVIOUS_SECTIONS_OMITTED
        parts.reverse()
        return separator.join(parts)

    def _trim_chat_history(self, chat_history: List[Dict[str, str]]) -> List[Dict[str, str]]:
        """对话历史：最近 N 条且总字符数不超过上限（优先保留最近的消息）"""
        recent = [
            {"role": h["role"], "content": h["content"]}
            for h in chat_history[-self.CHAT_HISTORY_MAX_MESSAGES:]
            if isinstance(h, dict)
            and h.get("role") in ("user", "assistant")
            and isinstance(h.get("content"), str)
        ]
        budget = self.chat_history_max_chars
        kept: List[Dict[str, str]] = []
        used = 0
        for h in reversed(recent):
            room = budget - used
            if room <= 0:
                break
            if len(h["content"]) > room:
                if not kept:
                    kept.append({"role": h["role"], "content": h["content"][:room]})
                break
            kept.append(h)
            used += len(h["content"])
        kept.reverse()
        return kept

    def plan_outline(
        self,
        progress_callback: Optional[Callable] = None
    ) -> ReportOutline:
        """
        规划报告大纲

        使用LLM分析模拟需求，规划报告的目录结构

        Args:
            progress_callback: 进度回调函数

        Returns:
            ReportOutline: 报告大纲
        """
        logger.info(t('report.startPlanningOutline'))

        if progress_callback:
            progress_callback("planning", 0, t('progress.analyzingRequirements'))

        # 首先获取模拟上下文
        context = self.zep_tools.get_simulation_context(
            graph_id=self.graph_id,
            simulation_requirement=self.simulation_requirement
        )

        if progress_callback:
            progress_callback("planning", 30, t('progress.generatingOutline'))

        system_prompt = f"{PLAN_SYSTEM_PROMPT}\n\n{get_language_instruction()}"
        user_prompt = PLAN_USER_PROMPT_TEMPLATE.format(
            simulation_requirement=self.simulation_requirement,
            total_nodes=context.get('graph_statistics', {}).get('total_nodes', 0),
            total_edges=context.get('graph_statistics', {}).get('total_edges', 0),
            entity_types=list(context.get('graph_statistics', {}).get('entity_types', {}).keys()),
            total_entities=context.get('total_entities', 0),
            model_mix_block=self._model_mix_block(),
            related_facts_json=json.dumps(context.get('related_facts', [])[:10], ensure_ascii=False, indent=2),
        )

        try:
            response = self._call_llm_json(
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                stage=REPORT_STAGE,
                temperature=0.3,
                max_tokens=4096,
            )

            if progress_callback:
                progress_callback("planning", 80, t('progress.parsingOutline'))

            # 解析大纲
            sections = []
            for section_data in response.get("sections", []):
                sections.append(ReportSection(
                    title=section_data.get("title", ""),
                    content=""
                ))

            outline = ReportOutline(
                title=response.get("title", t('progress.reportComplete')),
                summary=response.get("summary", ""),
                sections=sections
            )

            if progress_callback:
                progress_callback("planning", 100, t('progress.outlinePlanComplete'))

            logger.info(t('report.outlinePlanDone', count=len(sections)))
            return outline

        except Exception as e:
            logger.error(t('report.outlinePlanFailed', error=str(e)))
            # 返回默认大纲（3个章节，作为fallback）
            return ReportOutline(
                title=t('progress.generatingOutline'),
                summary=t('progress.analyzingRequirements'),
                sections=[
                    ReportSection(title=t('progress.analyzingRequirements')),
                    ReportSection(title=t('progress.generatingOutline')),
                    ReportSection(title=t('progress.parsingOutline'))
                ]
            )

    def _generate_section_react(
        self,
        section: ReportSection,
        outline: ReportOutline,
        previous_sections: List[str],
        progress_callback: Optional[Callable] = None,
        section_index: int = 0
    ) -> str:
        """
        使用ReACT模式生成单个章节内容

        ReACT循环：
        1. Thought（思考）- 分析需要什么信息
        2. Action（行动）- 调用工具获取信息（每轮可并行多个）
        3. Observation（观察）- 分析工具返回结果
        4. 重复直到信息足够或达到最大次数
        5. Final Answer（最终回答）- 生成章节内容

        Args:
            section: 要生成的章节
            outline: 完整大纲
            previous_sections: 之前章节的内容（用于保持连贯性）
            progress_callback: 进度回调
            section_index: 章节索引（用于日志记录）

        Returns:
            章节内容（Markdown格式）
        """
        logger.info(t('report.reactGenerateSection', title=section.title))

        # 记录章节开始日志
        if self.report_logger:
            self.report_logger.log_section_start(section.title, section_index)

        # 系统提示词静态（前缀缓存），章节相关的动态数据放在用户消息
        system_prompt = self._section_system_prompt()
        user_prompt = SECTION_USER_PROMPT_TEMPLATE.format(
            report_title=outline.title,
            report_summary=outline.summary,
            simulation_requirement=self.simulation_requirement,
            model_mix_block=self._model_mix_block(),
            previous_content=self._build_previous_context(previous_sections),
            section_title=section.title,
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ]

        section_stats = _empty_llm_stats(REPORT_STAGE)
        self.section_llm_stats[section_index] = section_stats

        def call_llm() -> Tuple[Optional[str], Dict[str, Any]]:
            result = self._call_llm(
                messages,
                stage=REPORT_STAGE,
                temperature=self.temperature,
                max_tokens=self.SECTION_MAX_TOKENS,
            )
            meta = self._result_meta(result, REPORT_STAGE)
            _add_llm_stats(section_stats, meta)
            _add_llm_stats(self.report_llm_stats, meta)
            return result.text, meta

        # ReACT循环
        tool_calls_count = 0
        max_iterations = max(5, self.max_tool_calls + 2)  # 最大迭代轮数
        min_tool_calls = self.min_tool_calls  # 最少工具调用次数
        conflict_retries = 0  # 工具调用与Final Answer同时出现的连续冲突次数
        used_tools: List[str] = []  # 记录已调用过的工具名（按首次使用顺序）
        all_tools = ["insight_forge", "panorama_search", "quick_search", "interview_agents"]

        # 报告上下文，用于InsightForge的子问题生成
        report_context = f"Section title: {section.title}\nSimulation requirement: {self.simulation_requirement}"

        def unused_list() -> List[str]:
            return [name for name in all_tools if name not in used_tools]

        def log_content(content: str) -> None:
            if self.report_logger:
                self.report_logger.log_section_content(
                    section_title=section.title,
                    section_index=section_index,
                    content=content,
                    tool_calls_count=tool_calls_count,
                    llm=section_stats,
                )

        for iteration in range(max_iterations):
            if progress_callback:
                progress_callback(
                    "generating",
                    int((iteration / max_iterations) * 100),
                    t('progress.deepSearchAndWrite', current=tool_calls_count, max=self.max_tool_calls)
                )

            # 调用LLM
            response, llm_meta = call_llm()

            # 检查 LLM 返回是否为空（API 异常或内容为空）
            if not response:
                logger.warning(t('report.sectionIterNone', title=section.title, iteration=iteration + 1))
                # 如果还有迭代次数，添加消息并重试
                if iteration < max_iterations - 1:
                    messages.append({"role": "assistant", "content": REACT_EMPTY_RESPONSE_PLACEHOLDER})
                    messages.append({"role": "user", "content": REACT_CONTINUE_MSG})
                    continue
                # 最后一次迭代也为空，跳出循环进入强制收尾
                break

            logger.debug(f"LLM响应: {response[:200]}...")

            # 解析一次，复用结果
            tool_calls = self._parse_tool_calls(response)
            has_tool_calls = bool(tool_calls)
            has_final_answer = "Final Answer:" in response

            # ── 冲突处理：LLM 同时输出了工具调用和 Final Answer ──
            if has_tool_calls and has_final_answer:
                conflict_retries += 1
                logger.warning(
                    t('report.sectionConflict', title=section.title, iteration=iteration+1, conflictCount=conflict_retries)
                )

                if conflict_retries <= 2:
                    # 前两次：丢弃本次响应，要求 LLM 重新回复
                    cleaned_response = ReportAgent._strip_fake_tool_results(response)
                    messages.append({"role": "assistant", "content": cleaned_response})
                    messages.append({"role": "user", "content": REACT_FORMAT_ERROR_MSG})
                    continue
                else:
                    # 第三次：降级处理，截断到第一个工具调用，强制执行
                    logger.warning(
                        t('report.sectionConflictDowngrade', title=section.title, conflictCount=conflict_retries)
                    )
                    first_tool_end = response.find('</tool_call>')
                    if first_tool_end != -1:
                        response = response[:first_tool_end + len('</tool_call>')]
                        tool_calls = self._parse_tool_calls(response)
                        has_tool_calls = bool(tool_calls)
                    has_final_answer = False
                    conflict_retries = 0

            # 记录 LLM 响应日志
            if self.report_logger:
                self.report_logger.log_llm_response(
                    section_title=section.title,
                    section_index=section_index,
                    response=response,
                    iteration=iteration + 1,
                    has_tool_calls=has_tool_calls,
                    has_final_answer=has_final_answer,
                    llm=llm_meta,
                )

            # ── 情况1：LLM 输出了 Final Answer ──
            if has_final_answer:
                cleaned_response = ReportAgent._strip_fake_tool_results(response)
                # 工具调用次数不足，拒绝并要求继续调工具
                if tool_calls_count < min_tool_calls:
                    messages.append({"role": "assistant", "content": cleaned_response})
                    unused = unused_list()
                    unused_hint = REACT_INSUFFICIENT_UNUSED_HINT.format(unused_list=", ".join(unused)) if unused else ""
                    messages.append({
                        "role": "user",
                        "content": REACT_INSUFFICIENT_TOOLS_MSG.format(
                            tool_calls_count=tool_calls_count,
                            min_tool_calls=min_tool_calls,
                            unused_hint=unused_hint,
                        ),
                    })
                    continue

                # 正常结束
                final_answer = cleaned_response.split("Final Answer:")[-1].strip()
                logger.info(t('report.sectionGenDone', title=section.title, count=tool_calls_count))
                log_content(final_answer)
                return final_answer

            # ── 情况2：LLM 尝试调用工具 ──
            if has_tool_calls:
                cleaned_response = ReportAgent._strip_fake_tool_results(response)
                # 工具额度已耗尽 → 明确告知，要求输出 Final Answer
                if tool_calls_count >= self.max_tool_calls:
                    messages.append({"role": "assistant", "content": cleaned_response})
                    messages.append({
                        "role": "user",
                        "content": REACT_TOOL_LIMIT_MSG.format(
                            tool_calls_count=tool_calls_count,
                            max_tool_calls=self.max_tool_calls,
                        ),
                    })
                    continue

                # 去重 + 预算/并行上限截取，然后并行执行
                selected = self._select_tool_calls(tool_calls, self.max_tool_calls - tool_calls_count)

                if self.report_logger:
                    for call in selected:
                        self.report_logger.log_tool_call(
                            section_title=section.title,
                            section_index=section_index,
                            tool_name=call["name"],
                            parameters=call.get("parameters", {}),
                            iteration=iteration + 1
                        )

                outcomes = self._execute_tool_batch(selected, report_context=report_context)

                result_blocks = []
                for call, (result, cached) in zip(selected, outcomes):
                    if self.report_logger:
                        self.report_logger.log_tool_result(
                            section_title=section.title,
                            section_index=section_index,
                            tool_name=call["name"],
                            result=result,
                            iteration=iteration + 1,
                            cached=cached,
                        )
                    tool_calls_count += 1
                    if call["name"] not in used_tools:
                        used_tools.append(call["name"])
                    result_blocks.append(REACT_OBSERVATION_RESULT_TEMPLATE.format(
                        tool_name=call["name"],
                        cached_note=REACT_CACHED_NOTE if cached else "",
                        result=self._truncate_text(result, self.tool_result_max_chars),
                    ))

                # 构建未使用工具提示
                unused = unused_list()
                unused_hint = ""
                if unused and tool_calls_count < self.max_tool_calls:
                    unused_hint = REACT_UNUSED_TOOLS_HINT.format(unused_list=", ".join(unused))

                messages.append({"role": "assistant", "content": cleaned_response})
                messages.append({
                    "role": "user",
                    "content": REACT_OBSERVATION_TEMPLATE.format(
                        results="\n\n".join(result_blocks),
                        tool_calls_count=tool_calls_count,
                        max_tool_calls=self.max_tool_calls,
                        used_tools_str=", ".join(used_tools),
                        unused_hint=unused_hint,
                    ),
                })
                continue

            # ── 情况3：既没有工具调用，也没有 Final Answer ──
            cleaned_response = ReportAgent._strip_fake_tool_results(response)
            messages.append({"role": "assistant", "content": cleaned_response})

            if tool_calls_count < min_tool_calls:
                # 工具调用次数不足，推荐未用过的工具
                unused = unused_list()
                unused_hint = REACT_INSUFFICIENT_UNUSED_HINT.format(unused_list=", ".join(unused)) if unused else ""

                messages.append({
                    "role": "user",
                    "content": REACT_INSUFFICIENT_TOOLS_MSG_ALT.format(
                        tool_calls_count=tool_calls_count,
                        min_tool_calls=min_tool_calls,
                        unused_hint=unused_hint,
                    ),
                })
                continue

            # 工具调用已足够，LLM 输出了内容但没带 "Final Answer:" 前缀
            # 直接将这段内容作为最终答案，不再空转
            logger.info(t('report.sectionNoPrefix', title=section.title, count=tool_calls_count))
            final_answer = cleaned_response
            log_content(final_answer)
            return final_answer

        # 达到最大迭代次数，强制生成内容
        logger.warning(t('report.sectionMaxIter', title=section.title))
        messages.append({"role": "user", "content": REACT_FORCE_FINAL_MSG})

        response, _ = call_llm()

        # 检查强制收尾时 LLM 返回是否为空
        if not response:
            logger.error(t('report.sectionForceFailed', title=section.title))
            final_answer = t('report.sectionGenFailedContent')
        elif "Final Answer:" in response:
            final_answer = response.split("Final Answer:")[-1].strip()
        else:
            final_answer = response

        # 记录章节内容生成完成日志
        log_content(final_answer)
        return final_answer

    
    def generate_report(
        self, 
        progress_callback: Optional[Callable[[str, int, str], None]] = None,
        report_id: Optional[str] = None
    ) -> Report:
        """
        生成完整报告（分章节实时输出）
        
        每个章节生成完成后立即保存到文件夹，不需要等待整个报告完成。
        文件结构：
        reports/{report_id}/
            meta.json       - 报告元信息
            outline.json    - 报告大纲
            progress.json   - 生成进度
            section_01.md   - 第1章节
            section_02.md   - 第2章节
            ...
            full_report.md  - 完整报告
        
        Args:
            progress_callback: 进度回调函数 (stage, progress, message)
            report_id: 报告ID（可选，如果不传则自动生成）
            
        Returns:
            Report: 完整报告
        """
        import uuid
        
        # 如果没有传入 report_id，则自动生成
        if not report_id:
            report_id = f"report_{uuid.uuid4().hex[:12]}"
        start_time = datetime.now()
        
        report = Report(
            report_id=report_id,
            simulation_id=self.simulation_id,
            graph_id=self.graph_id,
            simulation_requirement=self.simulation_requirement,
            status=ReportStatus.PENDING,
            created_at=datetime.now().isoformat()
        )
        
        # 已完成的章节标题列表（用于进度追踪）
        completed_section_titles = []
        
        try:
            # 初始化：创建报告文件夹并保存初始状态
            ReportManager._ensure_report_folder(report_id)
            
            # 初始化日志记录器（结构化日志 agent_log.jsonl）
            self.report_logger = ReportLogger(report_id)
            self.report_logger.log_start(
                simulation_id=self.simulation_id,
                graph_id=self.graph_id,
                simulation_requirement=self.simulation_requirement
            )
            
            # 初始化控制台日志记录器（console_log.txt）
            self.console_logger = ReportConsoleLogger(report_id)
            
            ReportManager.update_progress(
                report_id, "pending", 0, t('progress.initReport'),
                completed_sections=[]
            )
            ReportManager.save_report(report)
            
            # 阶段1: 规划大纲
            report.status = ReportStatus.PLANNING
            ReportManager.update_progress(
                report_id, "planning", 5, t('progress.startPlanningOutline'),
                completed_sections=[]
            )
            
            # 记录规划开始日志
            self.report_logger.log_planning_start()
            
            if progress_callback:
                progress_callback("planning", 0, t('progress.startPlanningOutline'))
            
            outline = self.plan_outline(
                progress_callback=lambda stage, prog, msg: 
                    progress_callback(stage, prog // 5, msg) if progress_callback else None
            )
            report.outline = outline
            
            # 记录规划完成日志
            self.report_logger.log_planning_complete(outline.to_dict(), llm=self._client_meta(REPORT_STAGE))
            
            # 保存大纲到文件
            ReportManager.save_outline(report_id, outline)
            ReportManager.update_progress(
                report_id, "planning", 15, t('progress.outlineDone', count=len(outline.sections)),
                completed_sections=[]
            )
            ReportManager.save_report(report)
            
            logger.info(t('report.outlineSavedToFile', reportId=report_id))
            
            # 阶段2: 逐章节生成（分章节保存）
            report.status = ReportStatus.GENERATING
            
            total_sections = len(outline.sections)
            generated_sections = []  # 保存内容用于上下文
            
            for i, section in enumerate(outline.sections):
                section_num = i + 1
                base_progress = 20 + int((i / total_sections) * 70)
                
                # 更新进度
                ReportManager.update_progress(
                    report_id, "generating", base_progress,
                    t('progress.generatingSection', title=section.title, current=section_num, total=total_sections),
                    current_section=section.title,
                    completed_sections=completed_section_titles
                )

                if progress_callback:
                    progress_callback(
                        "generating",
                        base_progress,
                        t('progress.generatingSection', title=section.title, current=section_num, total=total_sections)
                    )
                
                # 生成主章节内容
                section_content = self._generate_section_react(
                    section=section,
                    outline=outline,
                    previous_sections=generated_sections,
                    progress_callback=lambda stage, prog, msg:
                        progress_callback(
                            stage, 
                            base_progress + int(prog * 0.7 / total_sections),
                            msg
                        ) if progress_callback else None,
                    section_index=section_num
                )
                
                section.content = section_content
                generated_sections.append(f"## {section.title}\n\n{section_content}")

                # 保存章节
                ReportManager.save_section(report_id, section_num, section)
                completed_section_titles.append(section.title)

                # 记录章节完成日志
                full_section_content = f"## {section.title}\n\n{section_content}"

                if self.report_logger:
                    self.report_logger.log_section_full_complete(
                        section_title=section.title,
                        section_index=section_num,
                        full_content=full_section_content.strip(),
                        llm=self.section_llm_stats.get(section_num)
                    )

                logger.info(t('report.sectionSaved', reportId=report_id, sectionNum=f"{section_num:02d}"))
                
                # 更新进度
                ReportManager.update_progress(
                    report_id, "generating", 
                    base_progress + int(70 / total_sections),
                    t('progress.sectionDone', title=section.title),
                    current_section=None,
                    completed_sections=completed_section_titles
                )
            
            # 阶段3: 组装完整报告
            if progress_callback:
                progress_callback("generating", 95, t('progress.assemblingReport'))
            
            ReportManager.update_progress(
                report_id, "generating", 95, t('progress.assemblingReport'),
                completed_sections=completed_section_titles
            )
            
            # 使用ReportManager组装完整报告
            report.markdown_content = ReportManager.assemble_full_report(report_id, outline)
            report.status = ReportStatus.COMPLETED
            report.completed_at = datetime.now().isoformat()
            
            # 计算总耗时
            total_time_seconds = (datetime.now() - start_time).total_seconds()
            
            # 记录报告完成日志
            if self.report_logger:
                self.report_logger.log_report_complete(
                    total_sections=total_sections,
                    total_time_seconds=total_time_seconds,
                    llm=self.report_llm_stats
                )
            
            # 保存最终报告
            ReportManager.save_report(report)
            ReportManager.update_progress(
                report_id, "completed", 100, t('progress.reportComplete'),
                completed_sections=completed_section_titles
            )
            
            if progress_callback:
                progress_callback("completed", 100, t('progress.reportComplete'))
            
            logger.info(t('report.reportGenDone', reportId=report_id))
            
            # 关闭控制台日志记录器
            if self.console_logger:
                self.console_logger.close()
                self.console_logger = None
            
            return report
            
        except Exception as e:
            logger.error(t('report.reportGenFailed', error=str(e)))
            report.status = ReportStatus.FAILED
            report.error = str(e)
            
            # 记录错误日志
            if self.report_logger:
                self.report_logger.log_error(str(e), "failed")
            
            # 保存失败状态
            try:
                ReportManager.save_report(report)
                ReportManager.update_progress(
                    report_id, "failed", -1, t('progress.reportFailed', error=str(e)),
                    completed_sections=completed_section_titles
                )
            except Exception:
                pass  # 忽略保存失败的错误
            
            # 关闭控制台日志记录器
            if self.console_logger:
                self.console_logger.close()
                self.console_logger = None
            
            return report
    
    def chat(
        self,
        message: str,
        chat_history: List[Dict[str, str]] = None
    ) -> Dict[str, Any]:
        """
        与Report Agent对话

        在对话中Agent可以自主调用检索工具来回答问题

        Args:
            message: 用户消息
            chat_history: 对话历史

        Returns:
            {
                "response": "Agent回复",
                "tool_calls": [调用的工具列表],
                "sources": [信息来源],
                "llm": {stage, model, profile_id, calls, usage}
            }
        """
        logger.info(t('report.agentChat', message=message[:50]))

        chat_history = chat_history or []

        # 获取已生成的报告内容
        report_content = ""
        try:
            report = ReportManager.get_report_by_simulation(self.simulation_id)
            if report and report.markdown_content:
                # 限制报告长度，避免上下文过长
                report_content = report.markdown_content[:self.CHAT_REPORT_MAX_CHARS]
                if len(report.markdown_content) > self.CHAT_REPORT_MAX_CHARS:
                    report_content += CHAT_REPORT_TRUNCATED
        except Exception as e:
            logger.warning(t('report.fetchReportFailed', error=e))

        # 静态系统提示词 + 动态上下文放在第一条用户消息
        context = CHAT_CONTEXT_TEMPLATE.format(
            simulation_requirement=self.simulation_requirement,
            report_content=report_content if report_content else CHAT_NO_REPORT,
        )
        messages = [
            {"role": "system", "content": self._chat_system_prompt()},
            {"role": "user", "content": context},
            {"role": "assistant", "content": CHAT_CONTEXT_ACK},
        ]

        # 添加历史对话（条数 + 字符总量限制）
        messages.extend(self._trim_chat_history(chat_history))

        # 添加用户消息
        messages.append({
            "role": "user",
            "content": message
        })

        chat_stats = _empty_llm_stats(CHAT_STAGE)

        def call_llm() -> str:
            result = self._call_llm(messages, stage=CHAT_STAGE, temperature=0.5, max_tokens=4096)
            _add_llm_stats(chat_stats, self._result_meta(result, CHAT_STAGE))
            return result.text or ""

        def finish(text: str) -> Dict[str, Any]:
            clean_response = re.sub(r'<tool_call>.*?</tool_call>', '', text, flags=re.DOTALL)
            clean_response = re.sub(r'\[TOOL_CALL\].*?\)', '', clean_response)
            clean_response = ReportAgent._strip_fake_tool_results(clean_response)
            return {
                "response": clean_response.strip(),
                "tool_calls": tool_calls_made,
                "sources": [tc.get("parameters", {}).get("query", "") for tc in tool_calls_made],
                "llm": chat_stats,
            }

        # ReACT循环（简化版）
        tool_calls_made: List[Dict[str, Any]] = []
        max_iterations = 2  # 减少迭代轮数

        for iteration in range(max_iterations):
            response = call_llm()

            # 解析工具调用
            tool_calls = self._parse_tool_calls(response)

            if not tool_calls:
                # 没有工具调用，直接返回响应
                return finish(response)

            # 执行工具调用（去重、限制数量、并行）
            budget = self.MAX_TOOL_CALLS_PER_CHAT - len(tool_calls_made)
            selected = self._select_tool_calls(tool_calls, budget)
            if not selected:
                break
            outcomes = self._execute_tool_batch(selected)
            observation = "\n".join(
                CHAT_OBSERVATION_TEMPLATE.format(
                    tool_name=call["name"],
                    result=self._truncate_text(result, self.CHAT_TOOL_RESULT_MAX_CHARS),
                )
                for call, (result, _cached) in zip(selected, outcomes)
            )
            tool_calls_made.extend(selected)

            # 将结果添加到消息
            cleaned_response = ReportAgent._strip_fake_tool_results(response)
            messages.append({"role": "assistant", "content": cleaned_response})
            messages.append({
                "role": "user",
                "content": observation + CHAT_OBSERVATION_SUFFIX
            })

        # 达到最大迭代，获取最终响应
        return finish(call_llm())


class ReportManager:
    """
    报告管理器
    
    负责报告的持久化存储和检索
    
    文件结构（分章节输出）：
    reports/
      {report_id}/
        meta.json          - 报告元信息和状态
        outline.json       - 报告大纲
        progress.json      - 生成进度
        section_01.md      - 第1章节
        section_02.md      - 第2章节
        ...
        full_report.md     - 完整报告
    """
    
    # 报告存储目录
    REPORTS_DIR = os.path.join(Config.UPLOAD_FOLDER, 'reports')
    
    @classmethod
    def _ensure_reports_dir(cls):
        """确保报告根目录存在"""
        os.makedirs(cls.REPORTS_DIR, exist_ok=True)
    
    @classmethod
    def _get_report_folder(cls, report_id: str) -> str:
        """获取报告文件夹路径"""
        return os.path.join(cls.REPORTS_DIR, report_id)
    
    @classmethod
    def _ensure_report_folder(cls, report_id: str) -> str:
        """确保报告文件夹存在并返回路径"""
        folder = cls._get_report_folder(report_id)
        os.makedirs(folder, exist_ok=True)
        return folder
    
    @classmethod
    def _get_report_path(cls, report_id: str) -> str:
        """获取报告元信息文件路径"""
        return os.path.join(cls._get_report_folder(report_id), "meta.json")
    
    @classmethod
    def _get_report_markdown_path(cls, report_id: str) -> str:
        """获取完整报告Markdown文件路径"""
        return os.path.join(cls._get_report_folder(report_id), "full_report.md")
    
    @classmethod
    def _get_outline_path(cls, report_id: str) -> str:
        """获取大纲文件路径"""
        return os.path.join(cls._get_report_folder(report_id), "outline.json")
    
    @classmethod
    def _get_progress_path(cls, report_id: str) -> str:
        """获取进度文件路径"""
        return os.path.join(cls._get_report_folder(report_id), "progress.json")
    
    @classmethod
    def _get_section_path(cls, report_id: str, section_index: int) -> str:
        """获取章节Markdown文件路径"""
        return os.path.join(cls._get_report_folder(report_id), f"section_{section_index:02d}.md")
    
    @classmethod
    def _get_agent_log_path(cls, report_id: str) -> str:
        """获取 Agent 日志文件路径"""
        return os.path.join(cls._get_report_folder(report_id), "agent_log.jsonl")
    
    @classmethod
    def _get_console_log_path(cls, report_id: str) -> str:
        """获取控制台日志文件路径"""
        return os.path.join(cls._get_report_folder(report_id), "console_log.txt")
    
    @classmethod
    def get_console_log(cls, report_id: str, from_line: int = 0) -> Dict[str, Any]:
        """
        获取控制台日志内容
        
        这是报告生成过程中的控制台输出日志（INFO、WARNING等），
        与 agent_log.jsonl 的结构化日志不同。
        
        Args:
            report_id: 报告ID
            from_line: 从第几行开始读取（用于增量获取，0 表示从头开始）
            
        Returns:
            {
                "logs": [日志行列表],
                "total_lines": 总行数,
                "from_line": 起始行号,
                "has_more": 是否还有更多日志
            }
        """
        log_path = cls._get_console_log_path(report_id)
        
        if not os.path.exists(log_path):
            return {
                "logs": [],
                "total_lines": 0,
                "from_line": 0,
                "has_more": False
            }
        
        logs = []
        total_lines = 0
        
        with open(log_path, 'r', encoding='utf-8') as f:
            for i, line in enumerate(f):
                total_lines = i + 1
                if i >= from_line:
                    # 保留原始日志行，去掉末尾换行符
                    logs.append(line.rstrip('\n\r'))
        
        return {
            "logs": logs,
            "total_lines": total_lines,
            "from_line": from_line,
            "has_more": False  # 已读取到末尾
        }
    
    @classmethod
    def get_console_log_stream(cls, report_id: str) -> List[str]:
        """
        获取完整的控制台日志（一次性获取全部）
        
        Args:
            report_id: 报告ID
            
        Returns:
            日志行列表
        """
        result = cls.get_console_log(report_id, from_line=0)
        return result["logs"]
    
    @classmethod
    def get_agent_log(cls, report_id: str, from_line: int = 0) -> Dict[str, Any]:
        """
        获取 Agent 日志内容
        
        Args:
            report_id: 报告ID
            from_line: 从第几行开始读取（用于增量获取，0 表示从头开始）
            
        Returns:
            {
                "logs": [日志条目列表],
                "total_lines": 总行数,
                "from_line": 起始行号,
                "has_more": 是否还有更多日志
            }
        """
        log_path = cls._get_agent_log_path(report_id)
        
        if not os.path.exists(log_path):
            return {
                "logs": [],
                "total_lines": 0,
                "from_line": 0,
                "has_more": False
            }
        
        logs = []
        total_lines = 0
        
        with open(log_path, 'r', encoding='utf-8') as f:
            for i, line in enumerate(f):
                total_lines = i + 1
                if i >= from_line:
                    try:
                        log_entry = json.loads(line.strip())
                        logs.append(log_entry)
                    except json.JSONDecodeError:
                        # 跳过解析失败的行
                        continue
        
        return {
            "logs": logs,
            "total_lines": total_lines,
            "from_line": from_line,
            "has_more": False  # 已读取到末尾
        }
    
    @classmethod
    def get_agent_log_stream(cls, report_id: str) -> List[Dict[str, Any]]:
        """
        获取完整的 Agent 日志（用于一次性获取全部）
        
        Args:
            report_id: 报告ID
            
        Returns:
            日志条目列表
        """
        result = cls.get_agent_log(report_id, from_line=0)
        return result["logs"]
    
    @classmethod
    def save_outline(cls, report_id: str, outline: ReportOutline) -> None:
        """
        保存报告大纲
        
        在规划阶段完成后立即调用
        """
        cls._ensure_report_folder(report_id)
        
        with open(cls._get_outline_path(report_id), 'w', encoding='utf-8') as f:
            json.dump(outline.to_dict(), f, ensure_ascii=False, indent=2)
        
        logger.info(t('report.outlineSaved', reportId=report_id))
    
    @classmethod
    def save_section(
        cls,
        report_id: str,
        section_index: int,
        section: ReportSection
    ) -> str:
        """
        保存单个章节

        在每个章节生成完成后立即调用，实现分章节输出

        Args:
            report_id: 报告ID
            section_index: 章节索引（从1开始）
            section: 章节对象

        Returns:
            保存的文件路径
        """
        cls._ensure_report_folder(report_id)

        # 构建章节Markdown内容 - 清理可能存在的重复标题
        cleaned_content = cls._clean_section_content(section.content, section.title)
        md_content = f"## {section.title}\n\n"
        if cleaned_content:
            md_content += f"{cleaned_content}\n\n"

        # 保存文件
        file_suffix = f"section_{section_index:02d}.md"
        file_path = os.path.join(cls._get_report_folder(report_id), file_suffix)
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(md_content)

        logger.info(t('report.sectionFileSaved', reportId=report_id, fileSuffix=file_suffix))
        return file_path
    
    @classmethod
    def _clean_section_content(cls, content: str, section_title: str) -> str:
        """
        清理章节内容
        
        1. 移除内容开头与章节标题重复的Markdown标题行
        2. 将所有 ### 及以下级别的标题转换为粗体文本
        
        Args:
            content: 原始内容
            section_title: 章节标题
            
        Returns:
            清理后的内容
        """
        import re
        
        if not content:
            return content
        
        content = content.strip()
        lines = content.split('\n')
        cleaned_lines = []
        skip_next_empty = False
        
        for i, line in enumerate(lines):
            stripped = line.strip()
            
            # 检查是否是Markdown标题行
            heading_match = re.match(r'^(#{1,6})\s+(.+)$', stripped)
            
            if heading_match:
                level = len(heading_match.group(1))
                title_text = heading_match.group(2).strip()
                
                # 检查是否是与章节标题重复的标题（跳过前5行内的重复）
                if i < 5:
                    if title_text == section_title or title_text.replace(' ', '') == section_title.replace(' ', ''):
                        skip_next_empty = True
                        continue
                
                # 将所有级别的标题（#, ##, ###, ####等）转换为粗体
                # 因为章节标题由系统添加，内容中不应有任何标题
                cleaned_lines.append(f"**{title_text}**")
                cleaned_lines.append("")  # 添加空行
                continue
            
            # 如果上一行是被跳过的标题，且当前行为空，也跳过
            if skip_next_empty and stripped == '':
                skip_next_empty = False
                continue
            
            skip_next_empty = False
            cleaned_lines.append(line)
        
        # 移除开头的空行
        while cleaned_lines and cleaned_lines[0].strip() == '':
            cleaned_lines.pop(0)
        
        # 移除开头的分隔线
        while cleaned_lines and cleaned_lines[0].strip() in ['---', '***', '___']:
            cleaned_lines.pop(0)
            # 同时移除分隔线后的空行
            while cleaned_lines and cleaned_lines[0].strip() == '':
                cleaned_lines.pop(0)
        
        return '\n'.join(cleaned_lines)
    
    @classmethod
    def update_progress(
        cls, 
        report_id: str, 
        status: str, 
        progress: int, 
        message: str,
        current_section: str = None,
        completed_sections: List[str] = None
    ) -> None:
        """
        更新报告生成进度
        
        前端可以通过读取progress.json获取实时进度
        """
        cls._ensure_report_folder(report_id)
        
        progress_data = {
            "status": status,
            "progress": progress,
            "message": message,
            "current_section": current_section,
            "completed_sections": completed_sections or [],
            "updated_at": datetime.now().isoformat()
        }
        
        with open(cls._get_progress_path(report_id), 'w', encoding='utf-8') as f:
            json.dump(progress_data, f, ensure_ascii=False, indent=2)
    
    @classmethod
    def get_progress(cls, report_id: str) -> Optional[Dict[str, Any]]:
        """获取报告生成进度"""
        path = cls._get_progress_path(report_id)
        
        if not os.path.exists(path):
            return None
        
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    
    @classmethod
    def get_generated_sections(cls, report_id: str) -> List[Dict[str, Any]]:
        """
        获取已生成的章节列表
        
        返回所有已保存的章节文件信息
        """
        folder = cls._get_report_folder(report_id)
        
        if not os.path.exists(folder):
            return []
        
        sections = []
        for filename in sorted(os.listdir(folder)):
            if filename.startswith('section_') and filename.endswith('.md'):
                file_path = os.path.join(folder, filename)
                with open(file_path, 'r', encoding='utf-8') as f:
                    content = f.read()

                # 从文件名解析章节索引
                parts = filename.replace('.md', '').split('_')
                section_index = int(parts[1])

                sections.append({
                    "filename": filename,
                    "section_index": section_index,
                    "content": content
                })

        return sections
    
    @classmethod
    def assemble_full_report(cls, report_id: str, outline: ReportOutline) -> str:
        """
        组装完整报告
        
        从已保存的章节文件组装完整报告，并进行标题清理
        """
        folder = cls._get_report_folder(report_id)
        
        # 构建报告头部
        md_content = f"# {outline.title}\n\n"
        md_content += f"> {outline.summary}\n\n"
        md_content += f"---\n\n"
        
        # 按顺序读取所有章节文件
        sections = cls.get_generated_sections(report_id)
        for section_info in sections:
            md_content += section_info["content"]
        
        # 后处理：清理整个报告的标题问题
        md_content = cls._post_process_report(md_content, outline)
        
        # 保存完整报告
        full_path = cls._get_report_markdown_path(report_id)
        with open(full_path, 'w', encoding='utf-8') as f:
            f.write(md_content)
        
        logger.info(t('report.fullReportAssembled', reportId=report_id))
        return md_content
    
    @classmethod
    def _post_process_report(cls, content: str, outline: ReportOutline) -> str:
        """
        后处理报告内容
        
        1. 移除重复的标题
        2. 保留报告主标题(#)和章节标题(##)，移除其他级别的标题(###, ####等)
        3. 清理多余的空行和分隔线
        
        Args:
            content: 原始报告内容
            outline: 报告大纲
            
        Returns:
            处理后的内容
        """
        import re
        
        lines = content.split('\n')
        processed_lines = []
        prev_was_heading = False
        
        # 收集大纲中的所有章节标题
        section_titles = set()
        for section in outline.sections:
            section_titles.add(section.title)
        
        i = 0
        while i < len(lines):
            line = lines[i]
            stripped = line.strip()
            
            # 检查是否是标题行
            heading_match = re.match(r'^(#{1,6})\s+(.+)$', stripped)
            
            if heading_match:
                level = len(heading_match.group(1))
                title = heading_match.group(2).strip()
                
                # 检查是否是重复标题（在连续5行内出现相同内容的标题）
                is_duplicate = False
                for j in range(max(0, len(processed_lines) - 5), len(processed_lines)):
                    prev_line = processed_lines[j].strip()
                    prev_match = re.match(r'^(#{1,6})\s+(.+)$', prev_line)
                    if prev_match:
                        prev_title = prev_match.group(2).strip()
                        if prev_title == title:
                            is_duplicate = True
                            break
                
                if is_duplicate:
                    # 跳过重复标题及其后的空行
                    i += 1
                    while i < len(lines) and lines[i].strip() == '':
                        i += 1
                    continue
                
                # 标题层级处理：
                # - # (level=1) 只保留报告主标题
                # - ## (level=2) 保留章节标题
                # - ### 及以下 (level>=3) 转换为粗体文本
                
                if level == 1:
                    if title == outline.title:
                        # 保留报告主标题
                        processed_lines.append(line)
                        prev_was_heading = True
                    elif title in section_titles:
                        # 章节标题错误使用了#，修正为##
                        processed_lines.append(f"## {title}")
                        prev_was_heading = True
                    else:
                        # 其他一级标题转为粗体
                        processed_lines.append(f"**{title}**")
                        processed_lines.append("")
                        prev_was_heading = False
                elif level == 2:
                    if title in section_titles or title == outline.title:
                        # 保留章节标题
                        processed_lines.append(line)
                        prev_was_heading = True
                    else:
                        # 非章节的二级标题转为粗体
                        processed_lines.append(f"**{title}**")
                        processed_lines.append("")
                        prev_was_heading = False
                else:
                    # ### 及以下级别的标题转换为粗体文本
                    processed_lines.append(f"**{title}**")
                    processed_lines.append("")
                    prev_was_heading = False
                
                i += 1
                continue
            
            elif stripped == '---' and prev_was_heading:
                # 跳过标题后紧跟的分隔线
                i += 1
                continue
            
            elif stripped == '' and prev_was_heading:
                # 标题后只保留一个空行
                if processed_lines and processed_lines[-1].strip() != '':
                    processed_lines.append(line)
                prev_was_heading = False
            
            else:
                processed_lines.append(line)
                prev_was_heading = False
            
            i += 1
        
        # 清理连续的多个空行（保留最多2个）
        result_lines = []
        empty_count = 0
        for line in processed_lines:
            if line.strip() == '':
                empty_count += 1
                if empty_count <= 2:
                    result_lines.append(line)
            else:
                empty_count = 0
                result_lines.append(line)
        
        return '\n'.join(result_lines)
    
    @classmethod
    def save_report(cls, report: Report) -> None:
        """保存报告元信息和完整报告"""
        cls._ensure_report_folder(report.report_id)
        
        # 保存元信息JSON
        with open(cls._get_report_path(report.report_id), 'w', encoding='utf-8') as f:
            json.dump(report.to_dict(), f, ensure_ascii=False, indent=2)
        
        # 保存大纲
        if report.outline:
            cls.save_outline(report.report_id, report.outline)
        
        # 保存完整Markdown报告
        if report.markdown_content:
            with open(cls._get_report_markdown_path(report.report_id), 'w', encoding='utf-8') as f:
                f.write(report.markdown_content)
        
        logger.info(t('report.reportSaved', reportId=report.report_id))
    
    @classmethod
    def get_report(cls, report_id: str) -> Optional[Report]:
        """获取报告"""
        path = cls._get_report_path(report_id)
        
        if not os.path.exists(path):
            # 兼容旧格式：检查直接存储在reports目录下的文件
            old_path = os.path.join(cls.REPORTS_DIR, f"{report_id}.json")
            if os.path.exists(old_path):
                path = old_path
            else:
                return None
        
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        # 重建Report对象
        outline = None
        if data.get('outline'):
            outline_data = data['outline']
            sections = []
            for s in outline_data.get('sections', []):
                sections.append(ReportSection(
                    title=s['title'],
                    content=s.get('content', '')
                ))
            outline = ReportOutline(
                title=outline_data['title'],
                summary=outline_data['summary'],
                sections=sections
            )
        
        # 如果markdown_content为空，尝试从full_report.md读取
        markdown_content = data.get('markdown_content', '')
        if not markdown_content:
            full_report_path = cls._get_report_markdown_path(report_id)
            if os.path.exists(full_report_path):
                with open(full_report_path, 'r', encoding='utf-8') as f:
                    markdown_content = f.read()
        
        return Report(
            report_id=data['report_id'],
            simulation_id=data['simulation_id'],
            graph_id=data['graph_id'],
            simulation_requirement=data['simulation_requirement'],
            status=ReportStatus(data['status']),
            outline=outline,
            markdown_content=markdown_content,
            created_at=data.get('created_at', ''),
            completed_at=data.get('completed_at', ''),
            error=data.get('error')
        )
    
    @classmethod
    def get_report_by_simulation(cls, simulation_id: str) -> Optional[Report]:
        """根据模拟ID获取报告"""
        cls._ensure_reports_dir()
        
        for item in os.listdir(cls.REPORTS_DIR):
            item_path = os.path.join(cls.REPORTS_DIR, item)
            # 新格式：文件夹
            if os.path.isdir(item_path):
                report = cls.get_report(item)
                if report and report.simulation_id == simulation_id:
                    return report
            # 兼容旧格式：JSON文件
            elif item.endswith('.json'):
                report_id = item[:-5]
                report = cls.get_report(report_id)
                if report and report.simulation_id == simulation_id:
                    return report
        
        return None
    
    @classmethod
    def list_reports(cls, simulation_id: Optional[str] = None, limit: int = 50) -> List[Report]:
        """列出报告"""
        cls._ensure_reports_dir()
        
        reports = []
        for item in os.listdir(cls.REPORTS_DIR):
            item_path = os.path.join(cls.REPORTS_DIR, item)
            # 新格式：文件夹
            if os.path.isdir(item_path):
                report = cls.get_report(item)
                if report:
                    if simulation_id is None or report.simulation_id == simulation_id:
                        reports.append(report)
            # 兼容旧格式：JSON文件
            elif item.endswith('.json'):
                report_id = item[:-5]
                report = cls.get_report(report_id)
                if report:
                    if simulation_id is None or report.simulation_id == simulation_id:
                        reports.append(report)
        
        # 按创建时间倒序
        reports.sort(key=lambda r: r.created_at, reverse=True)
        
        return reports[:limit]
    
    @classmethod
    def delete_report(cls, report_id: str) -> bool:
        """删除报告（整个文件夹）"""
        import shutil
        
        folder_path = cls._get_report_folder(report_id)
        
        # 新格式：删除整个文件夹
        if os.path.exists(folder_path) and os.path.isdir(folder_path):
            shutil.rmtree(folder_path)
            logger.info(t('report.reportFolderDeleted', reportId=report_id))
            return True
        
        # 兼容旧格式：删除单独的文件
        deleted = False
        old_json_path = os.path.join(cls.REPORTS_DIR, f"{report_id}.json")
        old_md_path = os.path.join(cls.REPORTS_DIR, f"{report_id}.md")
        
        if os.path.exists(old_json_path):
            os.remove(old_json_path)
            deleted = True
        if os.path.exists(old_md_path):
            os.remove(old_md_path)
            deleted = True
        
        return deleted
