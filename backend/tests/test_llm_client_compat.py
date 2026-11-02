import json
import logging
import os
import sys
from types import ModuleType, SimpleNamespace

import pytest

import app.utils.llm_client as llm_client_module
from app.config import Config
from app.llm import chat, chat_json, usage_tracker
from app.llm.client import ChatResult
from app.llm.profiles import LLMProfile
from app.utils.llm_client import LLMClient

SECRET = "sk-SECRET-DO-NOT-LOG-1234567890"


class CompletionRecorder:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.results.pop(0)


def _response(content, *, finish_reason="stop", usage=None):
    return SimpleNamespace(
        choices=[SimpleNamespace(finish_reason=finish_reason, message=SimpleNamespace(content=content))],
        usage=usage,
    )


@pytest.fixture
def llm_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("LLM_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(Config, "LLM_API_KEY", None)
    monkeypatch.setenv("LLM_API_KEY", SECRET)
    monkeypatch.setenv("LLM_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
    monkeypatch.setenv("LLM_MODEL_NAME", "qwen-plus")
    recorder = CompletionRecorder()
    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=recorder))
    monkeypatch.setattr(llm_client_module, "get_openai_client", lambda profile: fake_client)
    monkeypatch.setattr("app.llm.client.get_openai_client", lambda profile: fake_client)
    usage_tracker.reset()
    yield SimpleNamespace(monkeypatch=monkeypatch, recorder=recorder)
    usage_tracker.reset()


def test_legacy_env_llmclient_uses_default_profile_and_returns_str(llm_env):
    llm_env.recorder.results.append(_response("<think>x</think>hello", usage={"prompt_tokens": 9, "completion_tokens": 2}))
    client = LLMClient()

    assert (client.profile.id, client.model, client.api_key) == ("default", "qwen-plus", SECRET)
    assert client.chat([{"role": "user", "content": "hi"}]) == "hello"
    call = llm_env.recorder.calls[0]
    assert call["max_tokens"] == 4096 and call["temperature"] == 0.7
    assert call["extra_headers"] == {"x-dashscope-session-cache": "enable"}
    assert "prompt_cache_key" not in call
    assert usage_tracker.snapshot()["entries"] == [
        {"stage": "default", "profile_id": "default", "model": "qwen-plus", "calls": 1, "input_tokens": 9, "cached_tokens": 0, "output_tokens": 2}
    ]


def test_legacy_env_chat_json_keeps_response_format_fallback(llm_env):
    class Unsupported(RuntimeError):
        status_code = 400
        body = {"error": {"param": "response_format", "message": "unsupported"}}

    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise Unsupported()
        return _response('{"ok": true}')

    llm_env.recorder.create = create
    assert LLMClient(stage="ontology").chat_json([{"role": "user", "content": "j"}]) == {"ok": True}
    assert calls[0]["response_format"] == {"type": "json_object"}
    assert "response_format" not in calls[1]


def test_prompt_cache_key_only_for_api_openai_com(llm_env):
    llm_env.monkeypatch.setenv("LLM_BASE_URL", "https://api.openai.com/v1")
    llm_env.recorder.results.append(_response("a"))
    result = chat([{"role": "user", "content": "hi"}], stage="report")

    assert isinstance(result, ChatResult)
    assert (result.text, result.profile_id, result.model) == ("a", "default", "qwen-plus")
    call = llm_env.recorder.calls[0]
    assert call["prompt_cache_key"] == "mirofish-report"
    assert "extra_headers" not in call

    llm_env.monkeypatch.setenv("LLM_BASE_URL", "https://api.deepseek.com/v1")
    llm_env.recorder.results.append(_response("b"))
    chat([{"role": "user", "content": "hi"}], stage="report")
    assert "prompt_cache_key" not in llm_env.recorder.calls[1]
    assert "extra_headers" not in llm_env.recorder.calls[1]


def test_chat_result_usage_reads_cached_tokens(llm_env):
    usage = {"prompt_tokens": 100, "completion_tokens": 5, "prompt_tokens_details": {"cached_tokens": 80}}
    llm_env.recorder.results.append(_response("x", usage=usage))
    result = chat([{"role": "user", "content": "hi"}], stage="interview")
    assert result.usage == {"input_tokens": 100, "cached_tokens": 80, "output_tokens": 5}
    assert usage_tracker.snapshot()["totals"]["cached_tokens"] == 80


def test_stage_routes_select_profile(llm_env):
    llm_env.monkeypatch.setenv("CHEAP_KEY", "c")
    llm_env.monkeypatch.setenv("LLM_PROFILES", json.dumps([
        {"id": "cheap", "kind": "openai_compatible", "model": "deepseek-chat", "base_url": "https://api.deepseek.com/v1", "api_key_env": "CHEAP_KEY"}
    ]))
    llm_env.monkeypatch.setenv("LLM_ROUTES", json.dumps({"profile": "cheap"}))
    assert LLMClient(stage="profile").model == "deepseek-chat"
    assert LLMClient(stage="report").model == "qwen-plus"
    assert LLMClient().profile.id == "default"


def test_per_call_stage_routes_and_records_usage(llm_env):
    llm_env.monkeypatch.setenv("CHEAP_KEY", "c")
    llm_env.monkeypatch.setenv("LLM_PROFILES", json.dumps([
        {"id": "cheap", "kind": "openai_compatible", "model": "deepseek-chat", "base_url": "https://api.deepseek.com/v1", "api_key_env": "CHEAP_KEY"}
    ]))
    llm_env.monkeypatch.setenv("LLM_ROUTES", json.dumps({"search_planning": "cheap"}))
    llm_env.recorder.results.extend([_response('{"sub_queries": ["q"]}'), _response("summary")])
    client = LLMClient()

    assert client.chat_json([{"role": "user", "content": "j"}], stage="search_planning") == {"sub_queries": ["q"]}
    assert client.chat([{"role": "user", "content": "s"}], stage="interview", max_tokens=800) == "summary"
    assert [call["model"] for call in llm_env.recorder.calls] == ["deepseek-chat", "qwen-plus"]
    assert {(e["stage"], e["profile_id"]) for e in usage_tracker.snapshot()["entries"]} == {
        ("search_planning", "cheap"), ("interview", "default")}


def test_explicit_constructor_args_make_custom_profile(llm_env):
    llm_env.monkeypatch.setattr(Config, "LLM_API_KEY", "config-key")
    client = LLMClient(api_key="explicit", base_url="https://example.com/v1", model="m")
    assert (client.profile.id, client.api_key, client.base_url, client.model) == ("custom", "explicit", "https://example.com/v1", "m")
    assert LLMClient(model="only-model").api_key == "config-key"


def test_missing_key_and_unknown_stage_raise_value_error(llm_env):
    with pytest.raises(ValueError, match="nope"):
        LLMClient(stage="nope")
    llm_env.monkeypatch.delenv("LLM_API_KEY")
    with pytest.raises(ValueError, match="LLM_API_KEY"):
        LLMClient()
    with pytest.raises(ValueError, match="nope"):
        chat([], stage="nope", profile=LLMProfile(id="x", kind="openai_compatible", label="x", model="m", base_url=None))


def _install_fake_oauth(monkeypatch, text):
    calls = []
    oauth = ModuleType("app.llm.oauth_chatgpt")
    oauth.get_access_token = lambda: "access-token-SECRET"
    adapter = ModuleType("app.llm.responses_adapter")

    def responses_chat(access_token, model, messages, max_output_tokens=None):
        calls.append((access_token, model, messages, max_output_tokens))
        return ChatResult(text=text, model=model, profile_id="", usage={"input_tokens": 11, "cached_tokens": 3, "output_tokens": 4})

    adapter.responses_chat = responses_chat
    monkeypatch.setitem(sys.modules, "app.llm.oauth_chatgpt", oauth)
    monkeypatch.setitem(sys.modules, "app.llm.responses_adapter", adapter)
    return calls


def test_chatgpt_oauth_profile_uses_responses_adapter(llm_env):
    calls = _install_fake_oauth(llm_env.monkeypatch, '```json\n{"ok": 1}\n```')
    llm_env.monkeypatch.setenv("LLM_PROFILES", json.dumps([{"id": "chatgpt", "kind": "chatgpt_oauth", "model": "gpt-5"}]))
    llm_env.monkeypatch.setenv("LLM_ROUTES", json.dumps({"report": "chatgpt"}))

    assert chat_json([{"role": "user", "content": "j"}], stage="report", max_tokens=50) == {"ok": 1}
    client = LLMClient(stage="report")
    assert client.client is None
    assert client.chat([{"role": "user", "content": "x"}]) == '{"ok": 1}'

    assert calls[0][:2] == ("access-token-SECRET", "gpt-5") and calls[0][3] == 50
    entry = usage_tracker.snapshot()["entries"][0]
    assert (entry["profile_id"], entry["calls"], entry["cached_tokens"]) == ("chatgpt", 2, 6)
    assert llm_env.recorder.calls == []


def test_secrets_not_logged_or_in_repr(llm_env, caplog):
    caplog.set_level(logging.DEBUG)
    llm_env.recorder.results.append(_response("{bad"))
    with pytest.raises(Exception):
        LLMClient().chat_json([{"role": "user", "content": "j"}])
    client = LLMClient()
    assert SECRET not in caplog.text
    assert SECRET not in repr(client.profile)
