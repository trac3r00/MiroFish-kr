import json
import random
from typing import Any

import pytest

import app.llm.responses_adapter as adapter
from app.llm.client import ChatResult
from app.llm.responses_adapter import (
    ResponsesError,
    build_request_input,
    consume_events,
    iter_sse_events,
    responses_chat,
    responses_create,
)

PROPERTY_SEED = 20261102
PROPERTY_CASES = 600
FAILURE_CODES = [
    "subscription_sharing_usage_limit_exceeded",
    "subscription_sharing_usage_unavailable",
    "server_error",
    None,
]
NOISE_TYPES = ["response.created", "response.in_progress", "response.output_item.added",
               "response.content_part.added", "response.output_text.done"]
ALPHABET = "abc XYZ 한국어 中文 \n\t\"{}[]:,\\é😀"


def _random_text(rng):
    return "".join(rng.choice(ALPHABET) for _ in range(rng.randint(0, 12)))


def generate_case(rng):
    events: list[dict[str, Any]] = []
    for _ in range(rng.randint(0, 8)):
        if rng.random() < 0.6:
            events.append({"type": "response.output_text.delta", "delta": _random_text(rng)})
        else:
            events.append({"type": rng.choice(NOISE_TYPES), "sequence_number": rng.randint(0, 99)})
    terminal = rng.choice(["completed", "failed", "incomplete", "error", "truncated"])
    if terminal == "completed":
        usage = {"input_tokens": rng.randint(0, 9999), "output_tokens": rng.randint(0, 9999),
                 "input_tokens_details": {"cached_tokens": rng.randint(0, 9999)}}
        response: dict[str, Any] = {"model": rng.choice(["gpt-a", "gpt-b"]), "status": "completed"}
        if rng.random() < 0.9:
            response["usage"] = usage
        events.append({"type": "response.completed", "response": response})
    elif terminal == "failed":
        code = rng.choice(FAILURE_CODES)
        error = {"code": code, "message": "boom"} if code else None
        events.append({"type": "response.failed", "response": {"error": error}})
    elif terminal == "incomplete":
        events.append({"type": "response.incomplete",
                       "response": {"incomplete_details": {"reason": "max_output_tokens"}}})
    elif terminal == "error":
        events.append({"type": "error", "code": "rate_limit_exceeded", "message": "slow down"})
    if terminal != "truncated" and rng.random() < 0.3:
        events.append({"type": "response.output_text.delta", "delta": "AFTER-TERMINAL"})
    return events


def reference_outcome(events):
    """Independent oracle written from the SIWC spec, not from the adapter."""
    text = ""
    for event in events:
        kind = event["type"]
        if kind == "response.output_text.delta":
            text += event["delta"]
        elif kind == "response.completed":
            usage = event["response"].get("usage") or {}
            return ("ok", text, {
                "input_tokens": usage.get("input_tokens", 0),
                "cached_tokens": usage.get("input_tokens_details", {}).get("cached_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
            })
        elif kind == "response.failed":
            error = event["response"]["error"]
            return ("error", (error or {}).get("code") or "response_failed")
        elif kind == "response.incomplete":
            return ("error", "response_incomplete")
        elif kind == "error":
            return ("error", event["code"])
    return ("error", "stream_ended_early")


def serialize_sse(events, rng):
    lines = []
    if rng.random() < 0.3:
        lines.append(": keep-alive")
        lines.append("")
    for event in events:
        if rng.random() < 0.7:
            lines.append(f"event: {event['type']}")
        payload = json.dumps(event, ensure_ascii=rng.random() < 0.5)
        lines.append(("data: " if rng.random() < 0.8 else "data:") + payload)
        lines.append("" if rng.random() < 0.9 else "\r")
    if rng.random() < 0.2:
        lines += ["data: [DONE]", ""]
    return lines


def test_property_stream_outcome_matches_reference_model():
    rng = random.Random(PROPERTY_SEED)
    attempted = executed = 0
    outcomes = {"ok": 0, "error": 0}
    for _ in range(PROPERTY_CASES):
        attempted += 1
        events = generate_case(rng)
        expected = reference_outcome(events)
        lines = serialize_sse(events, rng)
        if expected[0] == "ok":
            result = consume_events(iter_sse_events(lines))
            assert (result.text, result.usage) == (expected[1], expected[2]), events
            assert result.model == events[[e["type"] for e in events].index("response.completed")]["response"]["model"]
        else:
            with pytest.raises(ResponsesError) as error:
                consume_events(iter_sse_events(lines))
            assert error.value.code == expected[1], events
        outcomes[expected[0]] += 1
        executed += 1
    print(f"property tests (responses stream): attempted={attempted} executed={executed} {outcomes}")
    assert executed == attempted == PROPERTY_CASES
    assert outcomes["ok"] > 50 and outcomes["error"] > 50


def test_usage_limit_failure_after_text_started_is_an_error():
    lines = [
        'data: {"type":"response.output_text.delta","delta":"partial"}', "",
        'data: {"type":"response.failed","response":{"error":{"code":"subscription_sharing_usage_limit_exceeded"}}}',
        "",
    ]
    with pytest.raises(ResponsesError) as error:
        consume_events(iter_sse_events(lines))
    assert error.value.code == "subscription_sharing_usage_limit_exceeded"


def test_build_request_input_spec_examples():
    messages = [
        {"role": "system", "content": "Be brief."},
        {"role": "developer", "content": "Answer in Korean."},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "like_post", "arguments": '{"post_id": 3}'}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
        {"role": "assistant", "content": "done"},
    ]
    instructions, items = build_request_input(messages)
    assert instructions == "Be brief.\n\nAnswer in Korean."
    assert items == [
        {"role": "user", "content": "hi"},
        {"type": "function_call", "call_id": "call_1", "name": "like_post", "arguments": '{"post_id": 3}'},
        {"type": "function_call_output", "call_id": "call_1", "output": "ok"},
        {"role": "assistant", "content": "done"},
    ]


class FakeStreamResponse:
    def __init__(self, status_code, lines=(), body=None, headers=None):
        self.status_code = status_code
        self._lines = list(lines)
        self._body = body
        self.headers = headers or {"x-request-id": "req_1"}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_lines(self, decode_unicode=False):
        return iter(self._lines)

    def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


def test_responses_create_request_shape_and_tools(monkeypatch):
    calls = []
    lines = [
        'data: {"type":"response.output_item.done","item":{"type":"function_call","call_id":"c9","name":"do_nothing","arguments":"{}"}}', "",
        'data: {"type":"response.completed","response":{"model":"gpt-a","usage":{"input_tokens":10,"output_tokens":2,"input_tokens_details":{"cached_tokens":8}}}}', "",
    ]

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return FakeStreamResponse(200, lines)

    monkeypatch.setattr(adapter.requests, "post", fake_post)
    output = responses_create(
        "tok", "gpt-a", [{"role": "system", "content": "S"}, {"role": "user", "content": "U"}],
        max_output_tokens=64,
        tools=[{"type": "function", "function": {"name": "do_nothing", "description": "d",
                                                 "parameters": {"type": "object"}}}],
        tool_choice="auto",
    )
    url, kwargs = calls[0]
    assert url == "https://api.openai.com/v1/responses"
    assert kwargs["headers"]["Authorization"] == "Bearer tok"
    assert kwargs["stream"] is True
    assert kwargs["json"] == {
        "model": "gpt-a", "input": [{"role": "user", "content": "U"}], "store": False, "stream": True,
        "instructions": "S", "max_output_tokens": 64,
        "tools": [{"type": "function", "name": "do_nothing", "description": "d", "parameters": {"type": "object"}}],
        "tool_choice": "auto",
    }
    assert output.tool_calls == [{"id": "c9", "type": "function", "function": {"name": "do_nothing", "arguments": "{}"}}]
    assert output.usage == {"input_tokens": 10, "cached_tokens": 8, "output_tokens": 2}


@pytest.mark.parametrize("status,body,code", [
    (429, {"error": {"code": "subscription_sharing_usage_limit_exceeded", "message": "limit"}},
     "subscription_sharing_usage_limit_exceeded"),
    (403, {"detail": "region not permitted"}, "http_403"),
    (503, None, "http_503"),
])
def test_http_errors_before_stream(monkeypatch, status, body, code):
    monkeypatch.setattr(adapter.requests, "post", lambda url, **kw: FakeStreamResponse(status, body=body))
    with pytest.raises(ResponsesError) as error:
        responses_create("tok", "gpt-a", [{"role": "user", "content": "x"}])
    assert (error.value.code, error.value.status, error.value.request_id) == (code, status, "req_1")


def test_responses_chat_returns_chat_result(monkeypatch):
    lines = [
        'data: {"type":"response.output_text.delta","delta":"Hello, "}', "",
        'data: {"type":"response.output_text.delta","delta":"world!"}', "",
        'data: {"type":"response.completed","response":{"usage":{"input_tokens":3,"output_tokens":4}}}', "",
    ]
    monkeypatch.setattr(adapter.requests, "post", lambda url, **kw: FakeStreamResponse(200, lines))
    result = responses_chat("tok", "gpt-a", [{"role": "user", "content": "Say hello"}])
    assert isinstance(result, ChatResult)
    assert (result.text, result.model) == ("Hello, world!", "gpt-a")
    assert result.usage == {"input_tokens": 3, "cached_tokens": 0, "output_tokens": 4}
