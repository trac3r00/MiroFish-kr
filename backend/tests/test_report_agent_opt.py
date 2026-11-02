import json
import logging
import random
import re
import threading

import pytest

from app.config import Config
from app.llm import ChatResult, LLMProfile
from app.services import report_agent as ra
from app.services.report_agent import ReportAgent, ReportOutline, ReportSection
from app.utils.locale import get_language_instruction, t

VALID = sorted(ReportAgent.VALID_TOOL_NAMES)


class Text:
    def __init__(self, text):
        self.text = text

    def to_text(self):
        return self.text


class FakeZepTools:
    def __init__(self, barrier=None, payload=None):
        self.calls = []
        self.thread_names = []
        self.barrier = barrier
        self.payload = payload
        self.lock = threading.Lock()

    def _record(self, name, query):
        with self.lock:
            self.calls.append((name, query))
            self.thread_names.append(threading.current_thread().name)
        if self.barrier is not None:
            self.barrier.wait()
        return Text(self.payload if self.payload is not None else f"RESULT[{name}:{query}]")

    def insight_forge(self, graph_id, query, simulation_requirement, report_context):
        return self._record("insight_forge", query)

    def panorama_search(self, graph_id, query, include_expired=True):
        return self._record("panorama_search", query)

    def quick_search(self, graph_id, query, limit=10):
        return self._record("quick_search", query)

    def interview_agents(self, simulation_id, interview_requirement, simulation_requirement, max_agents=5):
        return self._record("interview_agents", interview_requirement)

    def get_simulation_context(self, graph_id, simulation_requirement):
        return {"graph_statistics": {"total_nodes": 3, "total_edges": 2, "entity_types": {"Person": 3}},
                "total_entities": 3, "related_facts": ["fact"]}


class ScriptedLLM:
    def __init__(self, script, default="Final Answer: forced"):
        self.script = list(script)
        self.default = default
        self.model = "scripted-model"
        self.seen = []

    def _next(self, messages):
        self.seen.append([dict(m) for m in messages])
        return self.script.pop(0) if self.script else self.default

    def chat(self, messages, temperature=0.7, max_tokens=None):
        return self._next(messages)

    def chat_json(self, messages, temperature=0.3, max_tokens=None):
        self._next(messages)
        return {"title": "T", "summary": "S", "sections": [{"title": "A"}, {"title": "B"}]}


def call(name, **params):
    return "<tool_call>" + json.dumps({"name": name, "parameters": params}) + "</tool_call>"


@pytest.fixture(autouse=True)
def isolated_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, "UPLOAD_FOLDER", str(tmp_path / "uploads"))
    monkeypatch.setattr(Config, "OASIS_SIMULATION_DATA_DIR", str(tmp_path / "sims"))
    return tmp_path


def make_agent(llm, zep=None, requirement="req"):
    return ReportAgent(graph_id="g", simulation_id="sim1", simulation_requirement=requirement,
                       llm_client=llm, zep_tools=zep or FakeZepTools())


def attach_logger(agent, report_id="r1"):
    agent.report_logger = ra.ReportLogger(report_id)
    return agent.report_logger.log_file_path


def read_log(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f]


def outline():
    return ReportOutline(title="Title", summary="Summary", sections=[ReportSection(title="S1")])


def test_identical_tool_call_executes_once_and_logs_cache_hit():
    llm = ScriptedLLM([
        call("quick_search", query="a"),
        call("quick_search", query="a"),
        call("panorama_search", query="b"),
        "Final Answer: done",
    ])
    zep = FakeZepTools()
    agent = make_agent(llm, zep)
    log_path = attach_logger(agent)
    records = []
    handler = logging.Handler()
    handler.emit = records.append
    target = logging.getLogger("mirofish.report_agent")
    target.addHandler(handler)
    try:
        content = agent._generate_section_react(ReportSection(title="S1"), outline(), [], section_index=1)
    finally:
        target.removeHandler(handler)

    assert content == "done"
    assert zep.calls == [("quick_search", "a"), ("panorama_search", "b")]
    assert agent.tool_executions == 2 and agent.tool_cache_hits == 1
    results = [e["details"] for e in read_log(log_path) if e["action"] == "tool_result"]
    assert [r["cached"] for r in results] == [False, True, False]
    assert t("report.toolCacheHit", toolName="quick_search") in [r.getMessage() for r in records]
    observation = llm.seen[2][-1]["content"]
    assert ra.REACT_CACHED_NOTE in observation


def test_distinct_calls_in_one_response_run_in_parallel_and_keep_order(monkeypatch):
    monkeypatch.setattr(Config, "REPORT_MAX_PARALLEL_TOOLS", 3)
    first = call("panorama_search", query="p") + call("quick_search", query="q") + call("insight_forge", query="i")
    llm = ScriptedLLM([first, "Final Answer: ok"])
    # A 3-party barrier only releases when all three tools run at the same time.
    zep = FakeZepTools(barrier=threading.Barrier(3, timeout=5))
    agent = make_agent(llm, zep)

    assert agent._generate_section_react(ReportSection(title="S1"), outline(), []) == "ok"

    assert sorted(zep.calls) == [("insight_forge", "i"), ("panorama_search", "p"), ("quick_search", "q")]
    assert all(name.startswith("report-tool") for name in zep.thread_names)
    observation = llm.seen[1][-1]["content"]
    positions = [observation.index(f"RESULT[{n}:{q}]") for n, q in
                 [("panorama_search", "p"), ("quick_search", "q"), ("insight_forge", "i")]]
    assert positions == sorted(positions)
    assert "Tool execution failed" not in observation


def test_long_observation_is_truncated_with_marker(monkeypatch):
    monkeypatch.setattr(Config, "REPORT_TOOL_RESULT_MAX_CHARS", 100)
    llm = ScriptedLLM([call("quick_search", query="a"), call("quick_search", query="b"),
                       call("quick_search", query="c"), "Final Answer: x"])
    agent = make_agent(llm, FakeZepTools(payload="x" * 1000))
    log_path = attach_logger(agent)
    agent._generate_section_react(ReportSection(title="S1"), outline(), [])

    observation = llm.seen[1][-1]["content"]
    marker = ra.TOOL_RESULT_TRUNCATION_MARKER.format(omitted=900)
    assert "x" * 100 + marker in observation
    assert "x" * 101 not in observation
    results = [e["details"] for e in read_log(log_path) if e["action"] == "tool_result"]
    assert results[0]["result_length"] == 1000
    assert ReportAgent._truncate_text("short", 100) == "short"


def test_max_tool_calls_setting_is_honored(monkeypatch):
    monkeypatch.setattr(Config, "REPORT_AGENT_MAX_TOOL_CALLS", 2)
    counter = iter(range(1000))

    class AlwaysTools(ScriptedLLM):
        def chat(self, messages, temperature=0.7, max_tokens=None):
            self.seen.append(messages)
            if messages[-1]["content"] == ra.REACT_FORCE_FINAL_MSG:
                return "Final Answer: forced"
            return "".join(call("quick_search", query=f"q{next(counter)}") for _ in range(3))

    zep = FakeZepTools()
    agent = make_agent(AlwaysTools([]), zep)
    content = agent._generate_section_react(ReportSection(title="S1"), outline(), [])

    assert len(zep.calls) == 2
    assert agent.max_tool_calls == 2
    assert content == "forced"
    assert ra.SECTION_SYSTEM_PROMPT_TEMPLATE.count("{max_tool_calls}") >= 1
    assert "at most 2" in agent._section_system_prompt()


def test_previous_sections_context_total_is_capped(monkeypatch):
    rng = random.Random(7)
    for limit in (0, 50, 1000, 5000, 12000):
        monkeypatch.setattr(Config, "REPORT_PREV_SECTIONS_MAX_CHARS", limit)
        agent = make_agent(ScriptedLLM([]))
        for _ in range(40):
            sections = [f"## S{i}\n\n" + "y" * rng.randint(0, 9000) for i in range(rng.randint(1, 8))]
            context = agent._build_previous_context(sections)
            if context != ra.PREVIOUS_SECTIONS_OMITTED:
                assert len(context) <= limit
                assert context.split("\n\n---\n\n")[-1].startswith(sections[-1][: min(10, len(context))])
    assert make_agent(ScriptedLLM([]))._build_previous_context([]) == ra.FIRST_SECTION_PLACEHOLDER


def test_chat_history_total_is_capped(monkeypatch):
    monkeypatch.setattr(Config, "REPORT_CHAT_HISTORY_MAX_CHARS", 300)
    agent = make_agent(ScriptedLLM([]))
    history = [{"role": "user" if i % 2 else "assistant", "content": "z" * 120} for i in range(20)]
    trimmed = agent._trim_chat_history(history + [{"role": "system", "content": "evil"}, "junk"])
    assert sum(len(h["content"]) for h in trimmed) <= 300
    assert trimmed == history[-2:]


_ALPHA = "abc xyz 한국어 中文 {}[]\":,\\'<>/\n\t$%"


def _rand_str(rng, n=12):
    return "".join(rng.choice(_ALPHA) for _ in range(rng.randint(0, n))).replace("<", "(")


def _rand_params(rng, depth=0):
    params = {}
    for _ in range(rng.randint(0, 3)):
        kind = rng.random()
        key = rng.choice(["query", "limit", "report_context", "max_agents", _rand_str(rng, 5)])
        if kind < 0.5:
            params[key] = _rand_str(rng)
        elif kind < 0.7:
            params[key] = rng.randint(-5, 50)
        elif kind < 0.85 and depth < 2:
            params[key] = _rand_params(rng, depth + 1)
        else:
            params[key] = [rng.random() > 0.5, None, _rand_str(rng, 4)]
    return params


def _well_formed(rng):
    name = rng.choice(VALID + ["search_graph", "get_graph_statistics"])
    params = _rand_params(rng)
    keys = rng.choice([("name", "parameters"), ("tool", "params"), ("name", "params")])
    body = {keys[0]: name}
    if params or rng.random() < 0.5:
        body[keys[1]] = params
    open_tag = rng.choice(["<tool_call>", "<TOOL_CALL>", "<tool_call >"])
    pad = rng.choice(["", "\n", "  \n"])
    text = f"{open_tag}{pad}{json.dumps(body, ensure_ascii=rng.random() < 0.5)}{pad}</tool_call>"
    return text, {"name": name, "parameters": params}


def _malformed(rng):
    good = json.dumps({"name": rng.choice(VALID), "parameters": {"query": _rand_str(rng)}})
    kind = rng.randrange(10)
    if kind == 0:  # truncated JSON, closed tag
        return f"<tool_call>{good[: rng.randint(1, len(good) - 1)]}</tool_call>"
    if kind == 1:  # non-object JSON
        return "<tool_call>" + rng.choice(['[1, 2]', '"str"', '42', 'null']) + "</tool_call>"
    if kind == 2:  # missing name
        return "<tool_call>" + json.dumps({"parameters": {"query": "q"}}) + "</tool_call>"
    if kind == 3:  # non-string / empty name
        return "<tool_call>" + json.dumps({"name": rng.choice([123, "", "  ", None]), "parameters": {}}) + "</tool_call>"
    if kind == 4:  # parameters not an object
        return "<tool_call>" + json.dumps({"name": rng.choice(VALID), "parameters": rng.choice(["abc", 5, [1]])}) + "</tool_call>"
    if kind == 5:  # unclosed broken call (swallows nothing after it)
        return f"<tool_call>{good[: rng.randint(1, len(good) - 1)]}"
    if kind == 6:  # stray closer and prose with braces
        return "</tool_call> some {braces} and [brackets] }"
    if kind == 7:  # a non-tool JSON object in prose
        return json.dumps({"foo": _rand_str(rng), "bar": [1, 2]})
    if kind == 8:  # thought text
        return "Thought: I should search. " + _rand_str(rng, 30)
    return "<tool_call>not json at all</tool_call>"


def test_property_parse_tool_calls_recovers_all_well_formed():
    rng = random.Random(20261102)
    agent = make_agent(ScriptedLLM([]))
    attempted = executed = 0
    for _ in range(1000):
        attempted += 1
        fragments, expected = [], []
        for _ in range(rng.randint(0, 6)):
            if rng.random() < 0.45:
                text, parsed = _well_formed(rng)
                fragments.append(text)
                expected.append(parsed)
            else:
                fragments.append(_malformed(rng))
        sep = rng.choice(["", " ", "\n", "\n\nFinal Answer: maybe\n"])
        response = sep.join(fragments)
        if not expected and rng.random() < 0.3:
            # bare JSON fallback: one known tool call at the very end, no tags
            response = re.sub(r"</?tool_call\s*>", "", response, flags=re.I).replace("{", "(").replace("}", ")")
            bare = {"name": rng.choice(VALID), "parameters": _rand_params(rng)}
            response = f"{response}\n{json.dumps(bare)}"
            expected = [bare]
        assert agent._parse_tool_calls(response) == expected, response
        executed += 1

    garbage_alpha = "{}[]\":,<>/tool_call name parameters\n "
    for _ in range(500):
        attempted += 1
        response = "".join(rng.choice(garbage_alpha) for _ in range(rng.randint(0, 200)))
        parsed = agent._parse_tool_calls(response)
        assert isinstance(parsed, list)
        assert all(set(c) == {"name", "parameters"} and isinstance(c["parameters"], dict) for c in parsed)
        executed += 1
    deep = "<tool_call>" + '{"name": "quick_search", "parameters": ' + "[" * 100000 + "</tool_call>"
    assert agent._parse_tool_calls(deep) == []
    assert agent._parse_tool_calls(None) == []
    print(f"attempted={attempted} executed={executed}")
    assert attempted == executed == 1500


class ProfileLLM(ScriptedLLM):
    def __init__(self, script):
        super().__init__(script)
        self.profile = LLMProfile(id="p1", kind="openai_compatible", label="P1", model="model-x",
                                  base_url="https://example.invalid/v1", api_key="k")


def install_seam(monkeypatch, llm):
    stages = []

    def fake_chat(messages, *, stage, temperature, max_tokens, profile):
        stages.append((stage, profile.id))
        text = llm._next(messages)
        return ChatResult(text=text, model=profile.model, profile_id=profile.id,
                          usage={"input_tokens": 10, "cached_tokens": 4, "output_tokens": 2})

    monkeypatch.setattr(ra, "llm_chat", fake_chat)
    return stages


def test_agent_log_entries_include_model_profile_stage_usage(monkeypatch):
    llm = ProfileLLM([call("quick_search", query="a"), call("panorama_search", query="b"),
                      call("insight_forge", query="c"), "Final Answer: body"])
    stages = install_seam(monkeypatch, llm)
    agent = make_agent(llm)
    log_path = attach_logger(agent)
    agent._generate_section_react(ReportSection(title="S1"), outline(), [], section_index=1)

    entries = read_log(log_path)
    responses = [e["details"]["llm"] for e in entries if e["action"] == "llm_response"]
    assert len(responses) == 4
    for meta in responses:
        assert meta == {"stage": "report", "model": "model-x", "profile_id": "p1",
                        "usage": {"input_tokens": 10, "cached_tokens": 4, "output_tokens": 2}}
    section = [e["details"]["llm"] for e in entries if e["action"] == "section_content"][0]
    assert section["stage"] == "report" and section["model"] == "model-x" and section["profile_id"] == "p1"
    assert section["calls"] == 4
    assert section["usage"] == {"input_tokens": 40, "cached_tokens": 16, "output_tokens": 8}
    assert stages == [("report", "p1")] * 4

    chat_stages = install_seam(monkeypatch, llm)
    monkeypatch.setattr(ra.ReportManager, "get_report_by_simulation", classmethod(lambda cls, sid: None))
    llm.script = ["plain answer"]
    result = agent.chat("question?")
    assert result["response"] == "plain answer"
    assert result["llm"]["stage"] == "report_chat" and result["llm"]["usage"]["input_tokens"] == 10
    assert chat_stages == [("report_chat", "p1")]


def test_prompts_are_neutral_and_system_prompts_static(monkeypatch):
    source = open(ra.__file__, encoding="utf-8").read()
    for banned in ("微博", "抖音", "68%", "Weibo", "Douyin", "中国"):
        assert banned not in source

    llm = ScriptedLLM([call("quick_search", query="a"), call("quick_search", query="b"),
                       call("quick_search", query="c"), "Final Answer: one",
                       call("panorama_search", query="a"), call("panorama_search", query="b"),
                       call("panorama_search", query="c"), "Final Answer: two"])
    agent = make_agent(llm, requirement="Zq3Req")
    agent._generate_section_react(ReportSection(title="Zq1Title"), outline(), [])
    agent._generate_section_react(ReportSection(title="Zq2Title"), ReportOutline("Other", "Other", []), ["## Zq1Title\n\none"])
    systems = {messages[0]["content"] for messages in llm.seen}
    assert len(llm.seen) == 8 and len(systems) == 1
    system = systems.pop()
    assert system.endswith(get_language_instruction())
    assert "Zq1Title" not in system and "Zq2Title" not in system and "Zq3Req" not in system

    monkeypatch.setattr(ra.ReportManager, "get_report_by_simulation", classmethod(lambda cls, sid: None))
    llm.script = ["a1", "a2"]
    make_agent(llm, requirement="one").chat("q1")
    make_agent(llm, requirement="two").chat("q2", chat_history=[{"role": "user", "content": "x"}])
    chat_systems = {llm.seen[-1][0]["content"], llm.seen[-2][0]["content"]}
    assert len(chat_systems) == 1
    assert chat_systems.pop().endswith(get_language_instruction())


def test_plan_prompt_includes_agent_model_mix(isolated_dirs):
    sim_dir = isolated_dirs / "sims" / "sim1"
    sim_dir.mkdir(parents=True)
    (sim_dir / "simulation_config.json").write_text(json.dumps({"llm_models": {
        "default": {"label": "Default", "model": "gpt-a", "count": 6},
        "cheap": {"label": "Cheap", "model": "small-b", "count": 4},
    }}), encoding="utf-8")
    llm = ScriptedLLM([])
    agent = make_agent(llm)
    result = agent.plan_outline()

    assert [s.title for s in result.sections] == ["A", "B"]
    system, user = llm.seen[0][0]["content"], llm.seen[0][1]["content"]
    assert "gpt-a (Default) drove 60% of agents (6/10)" in user
    assert "small-b (Cheap) drove 40% of agents (4/10)" in user
    assert "analysis variable" in user
    assert "gpt-a" not in system

    (sim_dir / "simulation_config.json").write_text("{}", encoding="utf-8")
    assert agent._model_mix_sentence() == ""
