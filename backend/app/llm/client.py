from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Tuple, Union
from urllib.parse import urlsplit

from ..utils.openai_chat_compat import create_chat_completion, extract_chat_completion_text
from .profiles import LLMProfile, load_settings
from .router import resolve_profile, validate_stage
from .usage import usage_from_chat_completion, usage_tracker

logger = logging.getLogger(__name__)


@dataclass
class ChatResult:
    text: str
    model: str
    profile_id: str
    usage: Dict[str, int] = field(default_factory=dict)
    estimated: bool = False


CLI_KINDS = ("claude_code_cli", "cursor_agent_cli")


class LLMResponseError(ValueError):
    """A safe, structured error for unusable model responses."""

    def __init__(self, message: str, *, finish_reason: Optional[str] = None):
        super().__init__(message)
        self.finish_reason = finish_reason


_clients: Dict[Tuple[Any, ...], Any] = {}
_clients_lock = threading.Lock()


def get_openai_client(profile: LLMProfile) -> Any:
    from openai import OpenAI

    key = (profile.id, profile.base_url, profile.api_key, tuple(sorted(profile.extra_headers.items())))
    with _clients_lock:
        client = _clients.get(key)
        if client is None:
            client = OpenAI(
                api_key=profile.api_key,
                base_url=profile.base_url,
                default_headers=dict(profile.extra_headers) or None,
            )
            _clients[key] = client
        return client


def _host(base_url: Optional[str]) -> str:
    if not base_url:
        return ""
    return (urlsplit(base_url).hostname or "").lower()


def provider_request_options(profile: LLMProfile, stage: str) -> Dict[str, Any]:
    """Provider-specific cache hints; unknown params are only sent where accepted."""
    host = _host(profile.base_url)
    options: Dict[str, Any] = {}
    if host == "api.openai.com":
        options["prompt_cache_key"] = f"mirofish-{stage}"
    if "dashscope" in host:
        options["extra_headers"] = {"x-dashscope-session-cache": "enable"}
    return options


def _resolve(profile: Union[LLMProfile, str, None], stage: str) -> LLMProfile:
    if isinstance(profile, LLMProfile):
        return profile
    if isinstance(profile, str):
        return load_settings().profile(profile)
    return resolve_profile(stage)


def _oauth_completion(
    profile: LLMProfile, messages: List[Dict[str, Any]], max_tokens: Optional[int]
) -> Tuple[Any, Dict[str, int]]:
    from .oauth_chatgpt import get_access_token
    from .responses_adapter import responses_chat

    result = responses_chat(get_access_token(), profile.model, messages, max_output_tokens=max_tokens)
    usage = {
        "input_tokens": int(result.usage.get("input_tokens", 0) or 0),
        "cached_tokens": int(result.usage.get("cached_tokens", 0) or 0),
        "output_tokens": int(result.usage.get("output_tokens", 0) or 0),
    }
    response = SimpleNamespace(
        model=result.model or profile.model,
        choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=result.text))],
        usage=None,
    )
    return response, usage


def _cli_completion(
    profile: LLMProfile,
    messages: List[Dict[str, Any]],
    max_tokens: Optional[int],
    response_format: Optional[Dict[str, Any]],
) -> Tuple[Any, Dict[str, int]]:
    from .cli_adapter import run_claude_cli, run_cursor_cli

    runner = run_cursor_cli if profile.kind == "cursor_agent_cli" else run_claude_cli
    json_mode = (response_format or {}).get("type") in ("json_object", "json_schema")
    result = runner(messages, profile.model or None, max_output_tokens=max_tokens, json_mode=json_mode)
    response = SimpleNamespace(
        model=result.model,
        choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=result.text))],
        usage=None,
    )
    return response, dict(result.usage)


def create_completion(
    profile: LLMProfile,
    *,
    stage: str,
    messages: List[Dict[str, Any]],
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
    response_format: Optional[Dict[str, Any]] = None,
    openai_client: Any = None,
) -> Any:
    """Send one request for `profile`, record usage, return a Chat Completions-shaped response."""
    return _send(
        profile,
        stage=stage,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
        response_format=response_format,
        openai_client=openai_client,
    )[0]


def _send(
    profile: LLMProfile,
    *,
    stage: str,
    messages: List[Dict[str, Any]],
    temperature: Optional[float],
    max_tokens: Optional[int],
    response_format: Optional[Dict[str, Any]],
    openai_client: Any = None,
) -> Tuple[Any, Dict[str, int]]:
    validate_stage(stage)
    if profile.kind == "chatgpt_oauth":
        response, usage = _oauth_completion(profile, messages, max_tokens)
    elif profile.kind in CLI_KINDS:
        response, usage = _cli_completion(profile, messages, max_tokens, response_format)
        usage_tracker.record(stage, profile.id, response.model, **usage)
        return response, usage
    else:
        response = create_chat_completion(
            openai_client or get_openai_client(profile),
            model=profile.model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format=response_format if profile.supports_json_mode else None,
            **provider_request_options(profile, stage),
        )
        usage = usage_from_chat_completion(response)
    usage_tracker.record(stage, profile.id, profile.model, **usage)
    return response, usage


def clean_chat_text(content: str) -> str:
    """Remove common reasoning wrappers and an outer Markdown JSON fence."""
    cleaned = re.sub(r'<think>[\s\S]*?</think>', '', content).strip()
    cleaned = cleaned.lstrip("\ufeff")
    cleaned = re.sub(r'^```(?:json)?\s*\n?', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\n?```\s*$', '', cleaned)
    return cleaned.strip()


def is_response_format_unsupported(error: Exception) -> bool:
    """Detect an explicit provider rejection of JSON response_format."""
    if getattr(error, "status_code", None) not in {400, 422}:
        return False
    body = getattr(error, "body", None)
    if not isinstance(body, dict):
        return False
    details = body.get("error", body)
    if not isinstance(details, dict):
        return False

    param = str(details.get("param") or "").strip().lower()
    if param == "response_format" or param.startswith("response_format."):
        return True

    message = str(details.get("message") or "").lower()
    if "response_format" not in message:
        return False

    code = str(details.get("code") or "").lower()
    unsupported_codes = {"unsupported_parameter", "unsupported_value", "unknown_parameter", "invalid_parameter"}
    unsupported_phrases = ("not support", "unsupported", "unknown parameter", "unrecognized parameter")
    return code in unsupported_codes or any(phrase in message for phrase in unsupported_phrases)


def _contains_additional_json_container(content: str) -> bool:
    decoder = json.JSONDecoder()
    for match in re.finditer(r"[\[{]", content):
        try:
            value, _ = decoder.raw_decode(content[match.start():])
        except json.JSONDecodeError:
            continue
        if isinstance(value, (dict, list)):
            return True
    return False


def parse_json_response(response: Any) -> Dict[str, Any]:
    choices = getattr(response, "choices", None) or []
    if not choices:
        raise LLMResponseError("LLM returned no choices")

    finish_reason = getattr(choices[0], "finish_reason", None)
    if finish_reason == "length":
        raise LLMResponseError(
            "LLM JSON output was truncated at the token limit", finish_reason=finish_reason
        )
    if finish_reason not in {None, "stop"}:
        raise LLMResponseError(
            f"LLM JSON generation stopped unexpectedly ({finish_reason})", finish_reason=finish_reason
        )

    content = clean_chat_text(extract_chat_completion_text(response))
    if not content:
        raise LLMResponseError("LLM returned empty JSON content", finish_reason=finish_reason)

    try:
        value = json.loads(content)
    except json.JSONDecodeError as strict_error:
        # Accept only an object decoded from the beginning followed by prose;
        # never repair or invent truncated JSON.
        try:
            value, end = json.JSONDecoder().raw_decode(content)
        except json.JSONDecodeError:
            raise LLMResponseError(
                "LLM returned invalid JSON "
                f"(line {strict_error.lineno}, column {strict_error.colno})",
                finish_reason=finish_reason,
            ) from strict_error

        trailing = content[end:].strip()
        if trailing:
            if _contains_additional_json_container(trailing):
                raise LLMResponseError("LLM returned multiple JSON values", finish_reason=finish_reason)
            logger.warning("Ignoring text after a complete LLM JSON object")

    if not isinstance(value, dict):
        raise LLMResponseError(
            "LLM JSON response must be a top-level JSON object", finish_reason=finish_reason
        )
    return value


def run_json_loop(
    create: Callable[..., Any],
    *,
    messages: List[Dict[str, Any]],
    temperature: Optional[float],
    max_tokens: Optional[int],
    max_attempts: int,
) -> Dict[str, Any]:
    """JSON-mode request with one capability fallback and bounded content retries.

    An explicit response_format rejection costs one extra request, not a content
    attempt. A failed parse retries without the caller's output token cap.
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")

    response_format: Optional[Dict[str, str]] = {"type": "json_object"}
    request_max_tokens = max_tokens

    for attempt in range(1, max_attempts + 1):
        while True:
            try:
                response = create(
                    messages=messages,
                    temperature=temperature,
                    max_tokens=request_max_tokens,
                    response_format=response_format,
                )
            except Exception as error:
                if response_format is not None and is_response_format_unsupported(error):
                    logger.warning(
                        "LLM provider explicitly rejected response_format; "
                        "retrying once with prompt-only JSON guidance"
                    )
                    response_format = None
                    continue
                raise
            break

        try:
            return parse_json_response(response)
        except LLMResponseError as error:
            if attempt >= max_attempts:
                raise
            had_token_cap = request_max_tokens is not None
            request_max_tokens = None
            logger.warning(
                "LLM returned unusable JSON (finish_reason=%s); retrying content generation%s",
                error.finish_reason or "unknown",
                " without an output token cap" if had_token_cap else "",
            )

    raise LLMResponseError("LLM did not produce a JSON response")  # pragma: no cover


def chat(
    messages: List[Dict[str, Any]],
    *,
    stage: str,
    temperature: float = 0.7,
    max_tokens: Optional[int] = None,
    response_format: Optional[Dict[str, Any]] = None,
    profile: Union[LLMProfile, str, None] = None,
) -> ChatResult:
    resolved = _resolve(profile, stage)
    response, usage = _send(
        resolved,
        stage=stage,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
        response_format=response_format,
    )
    return ChatResult(
        text=clean_chat_text(extract_chat_completion_text(response)),
        model=response.model if resolved.kind in CLI_KINDS else resolved.model,
        profile_id=resolved.id,
        usage=usage,
        estimated=resolved.kind == "cursor_agent_cli",
    )


def chat_json(
    messages: List[Dict[str, Any]],
    *,
    stage: str,
    temperature: float = 0.3,
    max_tokens: Optional[int] = None,
    max_attempts: int = 1,
    profile: Union[LLMProfile, str, None] = None,
) -> Dict[str, Any]:
    resolved = _resolve(profile, stage)

    def create(**kwargs: Any) -> Any:
        return create_completion(resolved, stage=stage, **kwargs)

    return run_json_loop(
        create,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
        max_attempts=max_attempts,
    )
