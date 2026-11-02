"""
Chat-style calls over the OpenAI Responses API for ChatGPT-plan (`chatgpt_oauth`) profiles.

Every request uses store=false and stream=true; a call succeeds only when the stream delivers
`response.completed` (https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

import requests

from .client import ChatResult

RESPONSES_URL = "https://api.openai.com/v1/responses"
CONNECT_TIMEOUT = 30
READ_TIMEOUT = 300

_INSTRUCTION_ROLES = {"system", "developer"}
_MESSAGE_ROLES = {"user", "assistant"}


class ResponsesError(RuntimeError):
    """Inference failure with the provider's machine-readable code, HTTP status and request id."""

    def __init__(self, code: str, message: str = "", *, status: Optional[int] = None,
                 request_id: Optional[str] = None):
        super().__init__(message or code)
        self.code = code
        self.status = status
        self.request_id = request_id


@dataclass
class ResponsesOutput:
    text: str
    model: Optional[str]
    usage: Dict[str, int]
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(part.get("text", "")) for part in content if isinstance(part, dict)
        )
    return "" if content is None else str(content)


def _content_parts(content: Any, role: str) -> Any:
    if not isinstance(content, list):
        return _text_of(content)
    text_type = "output_text" if role == "assistant" else "input_text"
    parts = []
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text":
            parts.append({"type": text_type, "text": str(part.get("text", ""))})
        elif part.get("type") == "image_url" and role != "assistant":
            image = part.get("image_url")
            url = image.get("url") if isinstance(image, dict) else image
            parts.append({"type": "input_image", "image_url": url})
        else:
            parts.append(part)
    return parts


def build_request_input(messages: List[Dict[str, Any]]) -> Tuple[Optional[str], List[Dict[str, Any]]]:
    """Map Chat Completions messages to (instructions, Responses input items)."""
    instructions: List[str] = []
    items: List[Dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        content = message.get("content")
        if role in _INSTRUCTION_ROLES:
            text = _text_of(content)
            if text:
                instructions.append(text)
        elif role == "tool":
            items.append({
                "type": "function_call_output",
                "call_id": str(message.get("tool_call_id", "")),
                "output": _text_of(content),
            })
        else:
            item_role = role if role in _MESSAGE_ROLES else "user"
            if content not in (None, "", []):
                items.append({"role": item_role, "content": _content_parts(content, item_role)})
            for call in message.get("tool_calls") or []:
                function = call.get("function") or {}
                items.append({
                    "type": "function_call",
                    "call_id": str(call.get("id", "")),
                    "name": str(function.get("name", "")),
                    "arguments": function.get("arguments") or "{}",
                })
    return ("\n\n".join(instructions) or None), items


def convert_tools(tools: Optional[List[Dict[str, Any]]]) -> Optional[List[Dict[str, Any]]]:
    if not tools:
        return None
    converted = []
    for tool in tools:
        function = tool.get("function") if tool.get("type") == "function" else None
        if not isinstance(function, dict):
            converted.append(tool)
            continue
        entry: Dict[str, Any] = {"type": "function", "name": function.get("name")}
        if function.get("description"):
            entry["description"] = function["description"]
        if function.get("parameters") is not None:
            entry["parameters"] = function["parameters"]
        if function.get("strict") is not None:
            entry["strict"] = function["strict"]
        converted.append(entry)
    return converted


def convert_tool_choice(tool_choice: Any) -> Any:
    if isinstance(tool_choice, dict) and isinstance(tool_choice.get("function"), dict):
        return {"type": "function", "name": tool_choice["function"].get("name")}
    return tool_choice


def iter_sse_events(lines: Iterable[str]) -> Iterator[Dict[str, Any]]:
    """Parse `text/event-stream` lines into JSON event objects (event type taken from `type`)."""
    event_name: Optional[str] = None
    data_lines: List[str] = []

    def flush() -> Optional[Dict[str, Any]]:
        if not data_lines:
            return None
        raw = "\n".join(data_lines)
        if raw.strip() == "[DONE]":
            return None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            raise ResponsesError("invalid_stream", "malformed SSE data") from None
        if isinstance(payload, dict):
            payload.setdefault("type", event_name)
            return payload
        return None

    for line in lines:
        line = line.rstrip("\r")
        if line == "":
            payload = flush()
            event_name, data_lines = None, []
            if payload is not None:
                yield payload
        elif line.startswith(":"):
            continue
        elif line.startswith("event:"):
            event_name = line[6:].strip()
        elif line.startswith("data:"):
            value = line[5:]
            data_lines.append(value[1:] if value.startswith(" ") else value)
    payload = flush()
    if payload is not None:
        yield payload


def parse_usage(usage: Any) -> Dict[str, int]:
    usage = usage if isinstance(usage, dict) else {}
    details = usage.get("input_tokens_details")
    cached = details.get("cached_tokens") if isinstance(details, dict) else 0
    return {
        "input_tokens": int(usage.get("input_tokens") or 0),
        "cached_tokens": int(cached or 0),
        "output_tokens": int(usage.get("output_tokens") or 0),
    }


def _tool_call(item: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": str(item.get("call_id") or item.get("id") or ""),
        "type": "function",
        "function": {"name": str(item.get("name", "")), "arguments": item.get("arguments") or "{}"},
    }


def consume_events(events: Iterable[Dict[str, Any]], *, request_id: Optional[str] = None) -> ResponsesOutput:
    """Fold a Responses event stream into a result; raise ResponsesError unless it completed."""
    deltas: List[str] = []
    streamed_calls: List[Dict[str, Any]] = []
    for event in events:
        kind = event.get("type")
        if kind == "response.output_text.delta":
            deltas.append(str(event.get("delta", "")))
        elif kind == "response.output_item.done":
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "function_call":
                streamed_calls.append(_tool_call(item))
        elif kind == "response.completed":
            response = event.get("response") or {}
            final_calls = [
                _tool_call(item) for item in response.get("output") or []
                if isinstance(item, dict) and item.get("type") == "function_call"
            ]
            return ResponsesOutput(
                text="".join(deltas),
                model=response.get("model"),
                usage=parse_usage(response.get("usage")),
                tool_calls=final_calls or streamed_calls,
            )
        elif kind == "response.failed":
            error = (event.get("response") or {}).get("error") or {}
            raise ResponsesError(
                str(error.get("code") or "response_failed"), str(error.get("message") or ""),
                request_id=request_id,
            )
        elif kind == "response.incomplete":
            details = (event.get("response") or {}).get("incomplete_details") or {}
            raise ResponsesError(
                "response_incomplete", str(details.get("reason") or "incomplete"), request_id=request_id
            )
        elif kind == "error":
            nested = event.get("error")
            error = nested if isinstance(nested, dict) else event
            raise ResponsesError(
                str(error.get("code") or "stream_error"), str(error.get("message") or ""),
                request_id=request_id,
            )
    raise ResponsesError("stream_ended_early", "stream ended without response.completed",
                         request_id=request_id)


def _http_error(response: requests.Response) -> ResponsesError:
    request_id = response.headers.get("x-request-id")
    try:
        body = response.json()
    except ValueError:
        body = None
    code, message = f"http_{response.status_code}", ""
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            code = str(error.get("code") or code)
            message = str(error.get("message") or "")
        elif body.get("detail"):
            message = str(body["detail"])
    return ResponsesError(code, message, status=response.status_code, request_id=request_id)


def responses_create(
    access_token: str,
    model: str,
    messages: List[Dict[str, Any]],
    *,
    max_output_tokens: Optional[int] = None,
    tools: Optional[List[Dict[str, Any]]] = None,
    tool_choice: Any = None,
) -> ResponsesOutput:
    instructions, items = build_request_input(messages)
    body: Dict[str, Any] = {"model": model, "input": items, "store": False, "stream": True}
    if instructions:
        body["instructions"] = instructions
    if max_output_tokens:
        body["max_output_tokens"] = int(max_output_tokens)
    converted_tools = convert_tools(tools)
    if converted_tools:
        body["tools"] = converted_tools
        if tool_choice is not None:
            body["tool_choice"] = convert_tool_choice(tool_choice)

    try:
        with requests.post(
            RESPONSES_URL,
            json=body,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "text/event-stream",
                "Content-Type": "application/json",
            },
            stream=True,
            timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
        ) as response:
            if response.status_code != 200:
                raise _http_error(response)
            request_id = response.headers.get("x-request-id")
            lines = response.iter_lines(decode_unicode=True)
            return consume_events(iter_sse_events(lines), request_id=request_id)
    except requests.RequestException:
        raise ResponsesError("stream_ended_early", "connection to the Responses API failed") from None


def responses_chat(
    access_token: str,
    model: str,
    messages: List[Dict[str, Any]],
    max_output_tokens: Optional[int] = None,
) -> ChatResult:
    output = responses_create(access_token, model, messages, max_output_tokens=max_output_tokens)
    return ChatResult(text=output.text, model=output.model or model, profile_id="", usage=output.usage)
