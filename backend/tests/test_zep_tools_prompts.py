from app.services.zep_tools import AgentInterview, ZepToolsService


class RecordingLLM:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def chat_json(self, *, messages, stage, **kwargs):
        self.calls.append((messages, stage, kwargs))
        return self.response

    def chat(self, *, messages, stage, **kwargs):
        self.calls.append((messages, stage, kwargs))
        return "summary"


def service_with_llm(response):
    service = object.__new__(ZepToolsService)
    service._llm_client = RecordingLLM(response)
    return service


def test_search_planning_prompt_is_neutral_and_routed(monkeypatch):
    monkeypatch.setattr("app.services.zep_tools.get_language_instruction", lambda: "Please respond in Korean.")
    service = service_with_llm({"sub_queries": ["질문"]})

    assert service._generate_sub_queries("주제", "배경") == ["질문"]
    messages, stage, _ = service.llm.calls[0]
    assert stage == "search_planning"
    assert messages[0]["content"].endswith("Please respond in Korean.")
    assert "You are an expert" in messages[0]["content"]
    assert "你是" not in messages[0]["content"]


def test_interview_prompts_are_routed_and_summary_caps_tokens(monkeypatch):
    monkeypatch.setattr("app.services.zep_tools.get_language_instruction", lambda: "Please respond in Korean.")
    service = service_with_llm({"selected_indices": [0], "reasoning": "fit"})
    service._select_agents_for_interview([{"realname": "A"}], "need", "", 1)
    service._llm_client.response = {"questions": ["Q"]}
    service._generate_interview_questions("need", "", [{"profession": "student"}])
    service._llm_client.response = "summary"
    service._generate_interview_summary([AgentInterview("A", "role", "", "Q", "A")], "need")

    assert [call[1] for call in service.llm.calls] == ["interview"] * 3
    assert all(call[0][0]["content"].endswith("Please respond in Korean.") for call in service.llm.calls)
    assert service.llm.calls[-1][2]["max_tokens"] <= 800


def test_interview_summary_quote_style_tracks_locale(monkeypatch):
    service = service_with_llm("summary")
    interview = AgentInterview("A", "role", "", "Q", "A")

    monkeypatch.setattr("app.services.zep_tools.get_locale", lambda: "ko")
    service._generate_interview_summary([interview], "topic")
    korean_prompt = service.llm.calls[-1][0][0]["content"]
    assert "“ ”" in korean_prompt

    monkeypatch.setattr("app.services.zep_tools.get_locale", lambda: "zh")
    service._generate_interview_summary([interview], "topic")
    chinese_prompt = service.llm.calls[-1][0][0]["content"]
    assert "「」" in chinese_prompt
