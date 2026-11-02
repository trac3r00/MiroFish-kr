import json
import os
import random
import string
import time
from types import SimpleNamespace

import pytest
from flask import Flask

import app.api.llm as llm_api
import app.llm.oauth_chatgpt as oauth
from app.config import Config
from app.llm import usage_tracker
from app.llm.responses_adapter import ResponsesError, ResponsesOutput

PROPERTY_SEED = 4242
PROPERTY_CASES = 60
LOOPBACK = {"REMOTE_ADDR": "127.0.0.1"}


@pytest.fixture
def client(monkeypatch, tmp_path):
    for key in list(os.environ):
        if key.startswith("LLM_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(Config, "LLM_API_KEY", None)
    monkeypatch.setattr(Config, "LLM_BASE_URL", None)
    monkeypatch.setattr(Config, "LLM_MODEL_NAME", None)
    monkeypatch.setenv("LLM_API_KEY", "sk-default-secret")
    monkeypatch.setenv("LLM_BASE_URL", "https://api.example.com/v1")
    monkeypatch.setenv("LLM_MODEL_NAME", "model-default")
    monkeypatch.setenv("MIROFISH_CREDENTIAL_DIR", str(tmp_path / "cred"))
    monkeypatch.setattr(oauth, "PREFERRED_PORT", 0)
    oauth._attempts.clear()
    oauth._models_cache.clear()
    usage_tracker.reset()
    app = Flask(__name__)
    app.register_blueprint(llm_api.llm_bp)
    yield app.test_client()
    oauth.cancel_pending_sign_in()
    oauth._attempts.clear()
    usage_tracker.reset()


def _oauth_profile_env(monkeypatch):
    monkeypatch.setenv("LLM_PROFILES", json.dumps([
        {"id": "chatgpt", "kind": "chatgpt_oauth", "label": "ChatGPT", "model": "gpt-sub"}]))
    monkeypatch.setenv("LLM_ROUTES", json.dumps({"simulation_agent": "chatgpt"}))


def test_status_backward_compatible_single_default(client):
    body = client.get("/api/llm/status").get_json()
    assert body["success"] is True
    data = body["data"]
    assert [p["id"] for p in data["profiles"]] == ["default"]
    assert set(data["routes"].values()) == {"default"}
    assert data["agent_pool"] == {"default": 1.0}
    assert data["oauth"]["chatgpt"]["signed_in"] is False
    assert "sk-default-secret" not in json.dumps(body)


def test_status_reports_config_error(client, monkeypatch):
    monkeypatch.setenv("LLM_ROUTES", "{not json")
    response = client.get("/api/llm/status")
    body = response.get_json()
    assert response.status_code == 500 and body["success"] is False and body["error"]


def _random_token(rng, prefix):
    return prefix + "".join(rng.choice(string.ascii_letters + string.digits + "-_.") for _ in range(rng.randint(12, 64)))


def test_property_status_and_usage_never_leak_tokens(client, monkeypatch):
    _oauth_profile_env(monkeypatch)
    monkeypatch.setattr(oauth, "list_models", lambda: [{"slug": "gpt-sub", "display_name": "GPT Sub"}])
    rng = random.Random(PROPERTY_SEED)
    attempted = executed = 0
    for case in range(PROPERTY_CASES):
        attempted += 1
        oauth._models_cache.clear()
        secrets_ = {
            "access_token": _random_token(rng, "at"),
            "refresh_token": _random_token(rng, "rt"),
            "id_token": _random_token(rng, "eyJ"),
        }
        api_key = _random_token(rng, "sk-")
        monkeypatch.setenv("LLM_API_KEY", api_key)
        record = {
            "client_id": f"oaiapp_{case}", "email": "u@example.com", "subject": "s",
            "scopes": sorted(oauth.SCOPE.split()), "expires_at": int(time.time()) + 3600, **secrets_,
        }
        oauth._save_record(record, make_active=True)
        usage_tracker.record("simulation_agent", "chatgpt", "gpt-sub", rng.randint(0, 99), 0, rng.randint(0, 99))
        bodies = [client.get(path).get_data(as_text=True)
                  for path in ("/api/llm/status", "/api/llm/usage", "/api/llm/oauth/chatgpt/status")]
        assert json.loads(bodies[0])["data"]["oauth"]["chatgpt"]["signed_in"] is True
        for body in bodies:
            for value in (*secrets_.values(), api_key):
                assert value not in body, (case, value)
        executed += 1
    print(f"property tests (status/usage token leak): attempted={attempted} executed={executed}")
    assert executed == attempted == PROPERTY_CASES


def test_usage_endpoint_shape(client):
    usage_tracker.record("report", "default", "model-default", 10, 4, 3)
    data = client.get("/api/llm/usage").get_json()["data"]
    assert data["entries"] == [{"stage": "report", "profile_id": "default", "model": "model-default",
                                "calls": 1, "input_tokens": 10, "cached_tokens": 4, "output_tokens": 3}]


def test_oauth_start_status_signout_endpoints(client):
    body = client.post("/api/llm/oauth/chatgpt/start", json={}).get_json()
    assert body["success"] is True
    assert body["data"]["authorize_url"].startswith(oauth.AUTHORIZE_URL)
    attempt_id = body["data"]["attempt_id"]
    status = client.get(f"/api/llm/oauth/chatgpt/status?attempt_id={attempt_id}").get_json()["data"]
    assert status["attempt"]["status"] == "pending" and status["signed_in"] is False
    out = client.post("/api/llm/oauth/chatgpt/signout").get_json()
    assert out == {"success": True, "data": {"signed_out": True, "revoked": False}}


def _gateway(client, headers=None, environ=LOOPBACK, body=None):
    return client.post(
        "/api/llm/gateway/v1/chat/completions",
        json=body or {"model": "chatgpt", "messages": [{"role": "user", "content": "hi"}]},
        headers=headers or {},
        environ_base=environ,
    )


def test_gateway_rejects_untrusted_callers(client):
    token = llm_api.get_gateway_token()
    assert token and token == os.environ["MIROFISH_GATEWAY_TOKEN"]
    cases = [
        ({"X-MiroFish-Gateway-Token": token}, {"REMOTE_ADDR": "10.1.2.3"}),
        ({}, LOOPBACK),
        ({"X-MiroFish-Gateway-Token": token + "x"}, LOOPBACK),
        ({"X-MiroFish-Gateway-Token": ""}, LOOPBACK),
    ]
    for headers, environ in cases:
        response = _gateway(client, headers, environ)
        assert response.status_code == 403, (headers, environ)
    assert usage_tracker.snapshot()["entries"] == []


def test_gateway_oauth_profile_returns_chat_completion_and_records_usage(client, monkeypatch):
    _oauth_profile_env(monkeypatch)
    calls = []

    def fake_responses_create(token, model, messages, **kwargs):
        calls.append((token, model, messages, kwargs))
        return ResponsesOutput(
            text="", model="gpt-sub", usage={"input_tokens": 12, "cached_tokens": 6, "output_tokens": 5},
            tool_calls=[{"id": "c1", "type": "function", "function": {"name": "like_post", "arguments": "{}"}}],
        )

    monkeypatch.setattr(llm_api, "get_access_token", lambda: "at-secret")
    monkeypatch.setattr(llm_api, "responses_create", fake_responses_create)
    tools = [{"type": "function", "function": {"name": "like_post", "parameters": {"type": "object"}}}]
    response = _gateway(client, {"X-MiroFish-Gateway-Token": llm_api.get_gateway_token()}, body={
        "model": "chatgpt", "messages": [{"role": "user", "content": "hi"}], "tools": tools,
        "tool_choice": "auto", "max_tokens": 50})
    assert response.status_code == 200
    data = response.get_json()
    assert data["object"] == "chat.completion" and data["model"] == "gpt-sub"
    assert data["choices"][0]["finish_reason"] == "tool_calls"
    assert data["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "like_post"
    assert data["usage"] == {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17,
                             "prompt_tokens_details": {"cached_tokens": 6}}
    assert "at-secret" not in response.get_data(as_text=True)
    assert calls[0][:2] == ("at-secret", "gpt-sub")
    assert calls[0][3] == {"max_output_tokens": 50, "tools": tools, "tool_choice": "auto"}
    assert usage_tracker.snapshot()["entries"] == [{
        "stage": "simulation_agent", "profile_id": "chatgpt", "model": "gpt-sub",
        "calls": 1, "input_tokens": 12, "cached_tokens": 6, "output_tokens": 5}]


def test_gateway_maps_upstream_errors(client, monkeypatch):
    _oauth_profile_env(monkeypatch)
    monkeypatch.setattr(llm_api, "get_access_token", lambda: "at")

    def limited(*args, **kwargs):
        raise ResponsesError("subscription_sharing_usage_limit_exceeded", "limit", status=429)

    monkeypatch.setattr(llm_api, "responses_create", limited)
    response = _gateway(client, {"X-MiroFish-Gateway-Token": llm_api.get_gateway_token()})
    assert response.status_code == 429
    assert response.get_json()["error"]["code"] == "subscription_sharing_usage_limit_exceeded"

    def not_signed_in():
        raise oauth.OAuthError("not_signed_in")

    monkeypatch.setattr(llm_api, "get_access_token", not_signed_in)
    response = _gateway(client, {"X-MiroFish-Gateway-Token": llm_api.get_gateway_token()})
    assert response.status_code == 401 and response.get_json()["error"]["code"] == "not_signed_in"
    assert usage_tracker.snapshot()["entries"] == []


def test_gateway_api_key_profile_passthrough(client, monkeypatch):
    seen = {}

    class FakeCompletion:
        usage = SimpleNamespace(prompt_tokens=7, completion_tokens=2, prompt_tokens_details=None,
                                prompt_cache_hit_tokens=None)

        def model_dump(self, exclude_none=True):
            return {"object": "chat.completion", "model": "model-default",
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                                 "finish_reason": "stop"}]}

    def create(**kwargs):
        seen.update(kwargs)
        return FakeCompletion()

    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(llm_api, "get_openai_client", lambda profile: fake_client)
    response = _gateway(client, {"X-MiroFish-Gateway-Token": llm_api.get_gateway_token()}, body={
        "model": "whatever", "messages": [{"role": "user", "content": "hi"}], "temperature": 0.2})
    assert response.status_code == 200
    assert response.get_json()["choices"][0]["message"]["content"] == "ok"
    assert seen == {"model": "model-default", "messages": [{"role": "user", "content": "hi"}], "temperature": 0.2}
    entry = usage_tracker.snapshot()["entries"][0]
    assert (entry["stage"], entry["profile_id"], entry["input_tokens"], entry["output_tokens"]) == (
        "simulation_agent", "default", 7, 2)
