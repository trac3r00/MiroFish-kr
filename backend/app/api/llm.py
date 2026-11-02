"""
/api/llm: model visibility (status, usage), ChatGPT sign-in, and the loopback-only
Chat Completions gateway that lets OASIS subprocess agents use any profile.
"""

from __future__ import annotations

import hmac
import os
import secrets
import time
import uuid
from typing import Any, Dict, Optional, Tuple

from flask import Blueprint, jsonify, request

from ..llm import usage_tracker
from ..llm import cli_adapter
from ..llm.cli_adapter import CLIError
from ..llm.client import get_openai_client
from ..llm.oauth_chatgpt import (
    OAuthError,
    get_access_token,
    get_status,
    sign_out,
    start_sign_in,
)
from ..llm.profiles import LLMProfile, LLMSettings, load_settings
from ..llm.responses_adapter import ResponsesError, responses_create
from ..llm.usage import usage_from_chat_completion
from ..utils.locale import t
from ..utils.zep import graph_backend
from ..utils.logger import get_logger

logger = get_logger('mirofish.api.llm')

llm_bp = Blueprint('llm', __name__, url_prefix='/api/llm')

GATEWAY_TOKEN_ENV = "MIROFISH_GATEWAY_TOKEN"
GATEWAY_TOKEN_HEADER = "X-MiroFish-Gateway-Token"
GATEWAY_PROFILE_HEADER = "X-MiroFish-Profile"
GATEWAY_STAGE = "simulation_agent"
_LOOPBACK_ADDRS = {"127.0.0.1", "::1", "::ffff:127.0.0.1"}
_PASSTHROUGH_FIELDS = (
    "messages", "temperature", "top_p", "max_tokens", "max_completion_tokens", "tools",
    "tool_choice", "parallel_tool_calls", "response_format", "stop", "n", "seed",
    "presence_penalty", "frequency_penalty",
)

if not os.environ.get(GATEWAY_TOKEN_ENV):
    os.environ[GATEWAY_TOKEN_ENV] = secrets.token_urlsafe(32)


def get_gateway_token() -> str:
    """Per-process gateway secret; simulation runners pass it to children via the environment."""
    return os.environ[GATEWAY_TOKEN_ENV]


def _ok(data: Any, status: int = 200):
    return jsonify({"success": True, "data": data}), status


def _fail(error: str, status: int, **extra: Any):
    return jsonify({"success": False, "error": error, **extra}), status


def _settings_or_error() -> Tuple[Optional[LLMSettings], Optional[str]]:
    try:
        return load_settings(), None
    except ValueError as error:
        return None, str(error)


@llm_bp.route('/status', methods=['GET'])
def llm_status():
    settings, config_error = _settings_or_error()
    if settings is None:
        return _fail(t('api.llm.configError', error=config_error), 500)
    data = settings.to_public()
    data["oauth"] = {"chatgpt": get_status()}
    force = request.args.get("refresh") == "1"
    checkers = {"claude_code_cli": cli_adapter.cli_status, "cursor_agent_cli": cli_adapter.cursor_cli_status}
    cli_status: Dict[str, Dict[str, Any]] = {}
    data["cli"] = {}
    for p in settings.profiles:
        if p.kind in checkers:
            if p.kind not in cli_status:
                cli_status[p.kind] = checkers[p.kind](force=force)
            data["cli"][p.id] = dict(cli_status[p.kind])
    backend = graph_backend()
    if backend == "local":
        from ..graph_local import status as local_graph_status
        graph = local_graph_status()
        embedding = graph["embeddings"]
        data["graph_backend"] = {
            "kind": "local",
            "embeddings": embedding["model"] if embedding["enabled"] else ("off" if os.environ.get("GRAPH_LOCAL_EMBEDDINGS", "").strip().lower() == "off" else "unavailable"),
            "db_path": os.path.basename(graph["db_path"]),
        }
    else:
        data["graph_backend"] = {"kind": "zep_cloud", "embeddings": "unavailable", "db_path": ""}
    return _ok(data)


@llm_bp.route('/usage', methods=['GET'])
def llm_usage():
    return _ok(usage_tracker.snapshot())


@llm_bp.route('/oauth/chatgpt/start', methods=['POST'])
def chatgpt_start():
    body = request.get_json(silent=True) or {}
    try:
        data = start_sign_in(new_account=bool(body.get("new_account")))
    except OSError:
        logger.exception("ChatGPT sign-in listener could not start")
        return _fail(t('api.llm.oauthListenerFailed'), 500)
    return _ok(data)


@llm_bp.route('/oauth/chatgpt/status', methods=['GET'])
def chatgpt_status():
    return _ok(get_status(request.args.get("attempt_id") or None))


@llm_bp.route('/oauth/chatgpt/signout', methods=['POST'])
def chatgpt_signout():
    return _ok(sign_out())


def _gateway_error(message: str, status: int, code: str):
    return jsonify({"error": {"message": message, "type": "mirofish_gateway_error", "code": code}}), status


def _gateway_authorized() -> bool:
    if request.remote_addr not in _LOOPBACK_ADDRS:
        return False
    supplied = request.headers.get(GATEWAY_TOKEN_HEADER, "")
    return bool(supplied) and hmac.compare_digest(supplied.encode(), get_gateway_token().encode())


def _gateway_profile(settings: LLMSettings, body: Dict[str, Any]) -> LLMProfile:
    wanted = request.headers.get(GATEWAY_PROFILE_HEADER) or body.get("model")
    by_id = {p.id: p for p in settings.profiles}
    if wanted in by_id:
        return by_id[wanted]
    for profile in settings.profiles:
        if profile.kind == "chatgpt_oauth" and profile.model == wanted:
            return profile
    return settings.profile(settings.routes[GATEWAY_STAGE])


def _oauth_completion(profile: LLMProfile, body: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, int]]:
    output = responses_create(
        get_access_token(),
        profile.model,
        body["messages"],
        max_output_tokens=body.get("max_completion_tokens") or body.get("max_tokens"),
        tools=body.get("tools"),
        tool_choice=body.get("tool_choice"),
    )
    message: Dict[str, Any] = {"role": "assistant", "content": output.text or None}
    if output.tool_calls:
        message["tool_calls"] = output.tool_calls
    usage = output.usage
    completion = {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": output.model or profile.model,
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": "tool_calls" if output.tool_calls else "stop",
        }],
        "usage": {
            "prompt_tokens": usage["input_tokens"],
            "completion_tokens": usage["output_tokens"],
            "total_tokens": usage["input_tokens"] + usage["output_tokens"],
            "prompt_tokens_details": {"cached_tokens": usage["cached_tokens"]},
        },
    }
    return completion, usage


def _compatible_completion(profile: LLMProfile, body: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, int]]:
    kwargs = {key: body[key] for key in _PASSTHROUGH_FIELDS if key in body}
    response = get_openai_client(profile).chat.completions.create(model=profile.model, **kwargs)
    return response.model_dump(exclude_none=True), usage_from_chat_completion(response)


@llm_bp.route('/gateway/v1/chat/completions', methods=['POST'])
def gateway_chat_completions():
    if not _gateway_authorized():
        return _gateway_error("forbidden", 403, "forbidden")
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or not isinstance(body.get("messages"), list) or not body["messages"]:
        return _gateway_error("messages must be a non-empty list", 400, "invalid_request")
    if body.get("stream"):
        return _gateway_error("stream=true is not supported by the gateway", 400, "unsupported_stream")

    settings, config_error = _settings_or_error()
    if settings is None or not settings.routes:
        return _gateway_error(config_error or "no LLM profile configured", 500, "config_error")
    profile = _gateway_profile(settings, body)

    try:
        if profile.kind == "chatgpt_oauth":
            completion, usage = _oauth_completion(profile, body)
        elif profile.kind == "claude_code_cli":
            completion, usage = cli_adapter.chat_completion(profile.model or None, body)
        elif profile.kind == "cursor_agent_cli":
            completion, usage = cli_adapter.chat_completion(
                profile.model or None, body, runner=cli_adapter.run_cursor_cli
            )
        else:
            completion, usage = _compatible_completion(profile, body)
    except OAuthError as error:
        return _gateway_error(str(error), 401, error.code)
    except ResponsesError as error:
        return _gateway_error(str(error), error.status or 502, error.code)
    except CLIError as error:
        return _gateway_error(str(error), 504 if error.code == "timeout" else 502, error.code)
    except Exception as error:
        status = getattr(error, "status_code", None)
        logger.warning("LLM gateway upstream error for profile %s: %s", profile.id, type(error).__name__)
        return _gateway_error(str(error), status if isinstance(status, int) else 502, "upstream_error")

    model = completion.get("model") if profile.kind in ("claude_code_cli", "cursor_agent_cli") else profile.model
    usage_tracker.record(GATEWAY_STAGE, profile.id, model or profile.model, **usage)
    return jsonify(completion), 200
