"""
Subscription CLI providers: Claude Code CLI (kind "claude_code_cli") and Cursor CLI
(kind "cursor_agent_cli", binary `agent`, alias `cursor-agent`).

Claude Code CLI:

Runs completions through the user's own, unmodified, already-signed-in `claude` binary
(`claude -p --output-format json`). MiroFish never reads Claude credentials, config files,
keychain items or tokens, and never offers a Claude login: the user signs in to Claude Code
themselves (`claude` then `/login`). We only spawn the binary and parse its stdout JSON.

Cursor CLI: `agent -p --output-format json --trust --mode ask --workspace <tmp> -- <prompt>`.
The CLI has no system-prompt flag (system text is prepended to the prompt) and reports no
token usage (usage is estimated as len(text)//4 and flagged). Print mode has tools and no flag
disables them all, so every call runs in a fresh empty temp workspace whose
`.cursor/cli.json` denies Shell, Write and MCP; the workspace is deleted afterwards.
We never pass --force/--yolo or --approve-mcps and never read Cursor credential files.

Env:
- CLAUDE_CLI_PATH       binary path (default: `claude` on PATH)
- CURSOR_CLI_PATH       binary path (default: `agent`, then `cursor-agent` on PATH)
- CLI_TIMEOUT_SECONDS   per-call timeout (default 180)
- CLI_MAX_CONCURRENCY   process-wide cap on concurrent CLI calls (default 2); plan limits
                        assume ordinary individual usage
"""

from __future__ import annotations

import errno
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from .client import ChatResult

DEFAULT_TIMEOUT_SECONDS = 180.0
DEFAULT_MAX_CONCURRENCY = 2
STATUS_TTL_SECONDS = 30.0
STATUS_TIMEOUT_SECONDS = 15.0
DISALLOWED_TOOLS = "Bash Edit Write Read Glob Grep WebFetch WebSearch"
DEFAULT_SYSTEM_PROMPT = "You are a helpful assistant."
JSON_ONLY_INSTRUCTION = (
    "Respond with a single valid JSON object only. Do not add prose, explanations, "
    "or Markdown code fences."
)
_MAX_ERROR_CHARS = 300
_SCRUB_PREFIXES = ("LLM_", "MIROFISH_")
_SCRUB_SUFFIXES = ("_API_KEY", "_TOKEN")
CURSOR_DEFAULT_MODEL = "cursor-default"
CURSOR_PERMISSIONS: Dict[str, Any] = {
    "permissions": {"allow": [], "deny": ["Shell(*)", "Write(*)", "Write(**)", "Mcp(*:*)"]},
}
_CURSOR_SIGNED_OUT_RE = re.compile(
    r"not\s+(logged|signed)\s+in|not\s+authenticated|unauthenticated|logged\s+out|"
    r"(cursor-)?agent\s+login",
    re.IGNORECASE,
)


class CLIError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class ClaudeCLIError(CLIError):
    pass


class CursorCLIError(CLIError):
    pass



def find_claude_binary() -> Optional[str]:
    configured = (os.environ.get("CLAUDE_CLI_PATH") or "").strip()
    if configured:
        path = os.path.expanduser(configured)
        return path if os.path.isfile(path) and os.access(path, os.X_OK) else shutil.which(path)
    return shutil.which("claude")


def find_cursor_binary() -> Optional[str]:
    configured = (os.environ.get("CURSOR_CLI_PATH") or "").strip()
    if configured:
        path = os.path.expanduser(configured)
        return path if os.path.isfile(path) and os.access(path, os.X_OK) else shutil.which(path)
    return shutil.which("agent") or shutil.which("cursor-agent")


def scrubbed_env(source: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    env = dict(os.environ if source is None else source)
    return {
        key: value for key, value in env.items()
        if not key.upper().startswith(_SCRUB_PREFIXES) and not key.upper().endswith(_SCRUB_SUFFIXES)
    }


def _env_number(name: str, default: float, cast=float) -> Any:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = cast(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def timeout_seconds() -> float:
    return _env_number("CLI_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS, float)


def max_concurrency() -> int:
    return _env_number("CLI_MAX_CONCURRENCY", DEFAULT_MAX_CONCURRENCY, int)


_semaphore_lock = threading.Lock()
_semaphore: Optional[Tuple[int, threading.BoundedSemaphore]] = None


def _get_semaphore() -> threading.BoundedSemaphore:
    global _semaphore
    limit = max_concurrency()
    with _semaphore_lock:
        if _semaphore is None or _semaphore[0] != limit:
            _semaphore = (limit, threading.BoundedSemaphore(limit))
        return _semaphore[1]


def _spawn(binary: str, args: List[str], *, stdin: Optional[str], timeout: float, cwd: str,
           capture: bool = True, extra_env: Optional[Dict[str, str]] = None) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        [binary, *args],
        input=stdin,
        stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
        stderr=subprocess.PIPE if capture else subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        cwd=cwd,
        env={**scrubbed_env(), **(extra_env or {})},
    )


def _run(binary: str, args: List[str], *, stdin: Optional[str], timeout: float,
         capture: bool = True, extra_env: Optional[Dict[str, str]] = None,
         prefix: str = "mirofish-claude-") -> "subprocess.CompletedProcess[str]":
    with tempfile.TemporaryDirectory(prefix=prefix) as workdir:
        return _spawn(binary, args, stdin=stdin, timeout=timeout, cwd=workdir,
                      capture=capture, extra_env=extra_env)



def _content_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
        return "\n".join(parts)
    return str(content)


def _render_tool_calls(calls: Any) -> List[str]:
    lines = []
    for call in calls or []:
        function = (call or {}).get("function") or {}
        lines.append(
            f"[assistant called tool {function.get('name', '?')} "
            f"(id {call.get('id', '?')}) with arguments {function.get('arguments', '{}')}]"
        )
    return lines


def split_messages(messages: List[Dict[str, Any]]) -> Tuple[str, str]:
    system_parts: List[str] = []
    turns: List[Tuple[str, str]] = []
    for message in messages:
        role = message.get("role")
        text = _content_text(message.get("content"))
        if role in ("system", "developer"):
            if text.strip():
                system_parts.append(text)
        elif role == "assistant":
            body = "\n".join(([text] if text else []) + _render_tool_calls(message.get("tool_calls")))
            turns.append(("Assistant", body))
        elif role == "tool":
            turns.append((f"Tool result (id {message.get('tool_call_id', '?')})", text))
        else:
            turns.append(("User", text))

    system = "\n\n".join(system_parts)
    if len(turns) == 1 and turns[0][0] == "User":
        return system, turns[0][1]
    transcript = "\n\n".join(f"{speaker}:\n{body}" for speaker, body in turns)
    if turns:
        transcript = (
            "Conversation so far:\n\n" + transcript
            + "\n\nWrite the assistant's next reply to the conversation above."
        )
    return system, transcript


def strip_code_fences(text: str) -> str:
    cleaned = text.strip().lstrip("\ufeff")
    cleaned = re.sub(r"^```[A-Za-z0-9_-]*\s*\n?", "", cleaned)
    cleaned = re.sub(r"\n?```\s*$", "", cleaned)
    return cleaned.strip()



def _safe(text: Any) -> str:
    value = " ".join(str(text or "").split())
    return value[:_MAX_ERROR_CHARS]


def _parse_stdout(stdout: str) -> Optional[Dict[str, Any]]:
    text = (stdout or "").strip()
    if not text:
        return None
    candidates = [text] + [line for line in reversed(text.splitlines()) if line.strip().startswith("{")]
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def map_usage(payload: Dict[str, Any]) -> Dict[str, int]:
    raw = payload.get("usage")
    usage: Dict[str, Any] = raw if isinstance(raw, dict) else {}
    cache_read = _int(usage.get("cache_read_input_tokens"))
    return {
        "input_tokens": _int(usage.get("input_tokens")) + _int(usage.get("cache_creation_input_tokens")) + cache_read,
        "cached_tokens": cache_read,
        "output_tokens": _int(usage.get("output_tokens")),
    }


def pick_model(payload: Dict[str, Any], fallback: str) -> str:
    model_usage = payload.get("modelUsage")
    if not isinstance(model_usage, dict) or not model_usage:
        return fallback
    def output_of(item: Tuple[str, Any]) -> int:
        stats = item[1] if isinstance(item[1], dict) else {}
        return _int(stats.get("outputTokens", stats.get("output_tokens")))
    return max(model_usage.items(), key=output_of)[0] or fallback



def run_claude_cli(
    messages: List[Dict[str, Any]],
    model: Optional[str],
    max_output_tokens: Optional[int] = None,
    *,
    json_mode: bool = False,
    extra_system: Optional[str] = None,
    timeout: Optional[float] = None,
) -> ChatResult:
    binary = find_claude_binary()
    if not binary:
        raise ClaudeCLIError(
            "not_installed",
            "Claude Code CLI not found; install it or set CLAUDE_CLI_PATH",
        )

    system, transcript = split_messages(messages)
    system_parts = [system or DEFAULT_SYSTEM_PROMPT]
    if extra_system:
        system_parts.append(extra_system)
    if json_mode:
        system_parts.append(JSON_ONLY_INSTRUCTION)

    args = [
        "-p",
        "--output-format", "json",
        "--input-format", "text",
        "--no-session-persistence",
        # Skip the user's MCP servers: completions need no tools, and their tool
        # definitions cost ~6k input tokens per call (measured 27,799 -> 21,430).
        "--strict-mcp-config",
        "--system-prompt", "\n\n".join(system_parts),
    ]
    if model:
        args += ["--model", model]
    args += ["--disallowedTools", DISALLOWED_TOOLS]

    limit = timeout if timeout is not None else timeout_seconds()
    # The CLI's output budget also covers the model's own reasoning, so tight per-stage caps
    # (e.g. 900 for group personas) abort Korean answers mid-way and the retry costs more
    # than it saves. Keep the caller's cap only above a floor.
    min_output = int(os.environ.get("CLI_MIN_OUTPUT_TOKENS", "4096"))
    extra_env = (
        {"CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(max(int(max_output_tokens), min_output))}
        if max_output_tokens
        else None
    )

    with _get_semaphore():
        try:
            proc = _run(binary, args, stdin=transcript, timeout=limit, extra_env=extra_env)
        except subprocess.TimeoutExpired:
            raise ClaudeCLIError("timeout", f"Claude Code CLI timed out after {limit:g}s") from None
        except OSError as error:
            raise ClaudeCLIError("spawn_failed", f"Claude Code CLI could not start ({type(error).__name__})") from None

    payload = _parse_stdout(proc.stdout)
    if payload is not None and payload.get("is_error"):
        detail = _safe(payload.get("result")) or _safe(payload.get("subtype")) or "unknown error"
        raise ClaudeCLIError("cli_error", f"Claude Code CLI reported an error: {detail}")
    if proc.returncode != 0:
        raise ClaudeCLIError("cli_exit", f"Claude Code CLI exited with status {proc.returncode}")
    if payload is None or not isinstance(payload.get("result"), str):
        raise ClaudeCLIError("malformed_output", "Claude Code CLI returned output that is not the expected JSON")

    text = payload["result"]
    if json_mode:
        text = strip_code_fences(text)
    return ChatResult(
        text=text,
        model=pick_model(payload, model or "claude"),
        profile_id="",
        usage=map_usage(payload),
    )


def estimate_tokens(text: str) -> int:
    return len(text or "") // 4


def run_cursor_cli(
    messages: List[Dict[str, Any]],
    model: Optional[str],
    max_output_tokens: Optional[int] = None,
    *,
    json_mode: bool = False,
    extra_system: Optional[str] = None,
    timeout: Optional[float] = None,
) -> ChatResult:
    """One completion through the Cursor CLI. `max_output_tokens` is accepted for signature
    parity but the CLI has no output cap flag."""
    binary = find_cursor_binary()
    if not binary:
        raise CursorCLIError(
            "not_installed",
            "Cursor CLI (agent) not found; install it or set CURSOR_CLI_PATH",
        )

    system, transcript = split_messages(messages)
    system_parts = [system] if system else []
    if extra_system:
        system_parts.append(extra_system)
    if json_mode:
        system_parts.append(JSON_ONLY_INSTRUCTION)
    prompt = "\n\n".join(system_parts + [transcript]) if system_parts else transcript

    limit = timeout if timeout is not None else timeout_seconds()
    with _get_semaphore():
        with tempfile.TemporaryDirectory(prefix="mirofish-cursor-") as workspace:
            os.makedirs(os.path.join(workspace, ".cursor"))
            with open(os.path.join(workspace, ".cursor", "cli.json"), "w", encoding="utf-8") as handle:
                json.dump(CURSOR_PERMISSIONS, handle)
            args = ["-p", "--output-format", "json", "--trust", "--mode", "ask", "--workspace", workspace]
            if model:
                args += ["--model", model]
            args += ["--", prompt]
            try:
                proc = _spawn(binary, args, stdin=None, timeout=limit, cwd=workspace)
            except subprocess.TimeoutExpired:
                raise CursorCLIError("timeout", f"Cursor CLI timed out after {limit:g}s") from None
            except OSError as error:
                if error.errno == errno.E2BIG:
                    raise CursorCLIError("prompt_too_large", "Prompt is too large for the Cursor CLI command line") from None
                raise CursorCLIError("spawn_failed", f"Cursor CLI could not start ({type(error).__name__})") from None

    payload = _parse_stdout(proc.stdout)
    is_error = payload is not None and bool(payload.get("is_error"))
    if proc.returncode != 0 or is_error:
        if _CURSOR_SIGNED_OUT_RE.search(f"{proc.stderr or ''}\n{proc.stdout or ''}"):
            raise CursorCLIError("not_signed_in", "Cursor CLI is not signed in; run `agent login` in a terminal")
        if proc.returncode == 0:
            raise CursorCLIError("cli_error", "Cursor CLI reported an error")
        raise CursorCLIError("cli_exit", f"Cursor CLI exited with status {proc.returncode}")
    if payload is None or not isinstance(payload.get("result"), str):
        raise CursorCLIError("malformed_output", "Cursor CLI returned output that is not the expected JSON")

    text = payload["result"]
    if json_mode:
        text = strip_code_fences(text)
    return ChatResult(
        text=text,
        model=model or CURSOR_DEFAULT_MODEL,
        profile_id="",
        usage={"input_tokens": estimate_tokens(prompt), "cached_tokens": 0, "output_tokens": estimate_tokens(text)},
        estimated=True,
    )


_status_lock = threading.Lock()
_status_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}


def clear_status_cache() -> None:
    with _status_lock:
        _status_cache.clear()


def cli_status(force: bool = False) -> Dict[str, Any]:
    """{installed, version, signed_in}; signed_in comes only from `claude auth status`'s exit code."""
    binary = find_claude_binary()
    key = binary or ""
    now = time.monotonic()
    with _status_lock:
        cached = _status_cache.get(key)
        if cached and not force and now - cached[0] < STATUS_TTL_SECONDS:
            return dict(cached[1])

    status: Dict[str, Any] = {"installed": False, "version": None, "signed_in": False}
    if binary:
        status["installed"] = True
        try:
            version = _run(binary, ["--version"], stdin=None, timeout=STATUS_TIMEOUT_SECONDS)
            first = (version.stdout or "").strip().splitlines()
            if version.returncode == 0 and first:
                status["version"] = _safe(first[0])[:80]
        except (subprocess.TimeoutExpired, OSError):
            pass
        try:
            auth = _run(binary, ["auth", "status"], stdin=None, timeout=STATUS_TIMEOUT_SECONDS, capture=False)
            status["signed_in"] = auth.returncode == 0
        except (subprocess.TimeoutExpired, OSError):
            status["signed_in"] = False

    with _status_lock:
        _status_cache[key] = (time.monotonic(), status)
    return dict(status)


def cursor_cli_status(force: bool = False) -> Dict[str, Any]:
    """{installed, version, signed_in}. `agent status` exits 0 even when signed out, so its
    output is parsed for a signed-out marker; the output (may contain an email) is never logged
    or returned."""
    binary = find_cursor_binary()
    key = f"cursor:{binary or ''}"
    now = time.monotonic()
    with _status_lock:
        cached = _status_cache.get(key)
        if cached and not force and now - cached[0] < STATUS_TTL_SECONDS:
            return dict(cached[1])

    status: Dict[str, Any] = {"installed": False, "version": None, "signed_in": False}
    if binary:
        status["installed"] = True
        try:
            version = _run(binary, ["--version"], stdin=None, timeout=STATUS_TIMEOUT_SECONDS,
                           prefix="mirofish-cursor-")
            first = (version.stdout or "").strip().splitlines()
            if version.returncode == 0 and first:
                status["version"] = _safe(first[0])[:80]
        except (subprocess.TimeoutExpired, OSError):
            pass
        try:
            auth = _run(binary, ["status"], stdin=None, timeout=STATUS_TIMEOUT_SECONDS,
                        prefix="mirofish-cursor-")
            output = f"{auth.stdout or ''}\n{auth.stderr or ''}"
            status["signed_in"] = auth.returncode == 0 and bool(output.strip()) \
                and not _CURSOR_SIGNED_OUT_RE.search(output)
        except (subprocess.TimeoutExpired, OSError):
            status["signed_in"] = False

    with _status_lock:
        _status_cache[key] = (time.monotonic(), status)
    return dict(status)



def _tool_instructions(tools: List[Dict[str, Any]], tool_choice: Any) -> str:
    specs = []
    for tool in tools:
        function = (tool or {}).get("function") or {}
        if function.get("name"):
            specs.append({
                "name": function["name"],
                "description": function.get("description", ""),
                "parameters": function.get("parameters", {}),
            })
    rule = "You may call tools or reply in text."
    if tool_choice == "required":
        rule = "You must call at least one tool."
    elif isinstance(tool_choice, dict) and (tool_choice.get("function") or {}).get("name"):
        rule = f"You must call the tool {tool_choice['function']['name']}."
    elif tool_choice == "none":
        rule = "Do not call any tool; reply in text."
    return (
        "You can use these tools (JSON Schema parameters):\n"
        + json.dumps(specs, ensure_ascii=False)
        + f"\n{rule}\n"
        'To call tools, answer {"tool_calls": [{"name": "<tool name>", "arguments": {...}}]}. '
        'To reply in text, answer {"content": "<your reply>"}.'
    )


def chat_completion(model: Optional[str], body: Dict[str, Any], runner=None) -> Tuple[Dict[str, Any], Dict[str, int]]:
    tools = [t for t in body.get("tools") or [] if isinstance(t, dict)]
    use_tools = bool(tools) and body.get("tool_choice") != "none"
    wants_json = (body.get("response_format") or {}).get("type") in ("json_object", "json_schema")
    result = (runner or run_claude_cli)(
        body["messages"],
        model,
        max_output_tokens=body.get("max_completion_tokens") or body.get("max_tokens"),
        json_mode=use_tools or wants_json,
        extra_system=_tool_instructions(tools, body.get("tool_choice")) if use_tools else None,
    )

    message: Dict[str, Any] = {"role": "assistant", "content": result.text}
    if use_tools:
        known = {(t.get("function") or {}).get("name") for t in tools}
        content, calls = result.text, []
        try:
            parsed = json.loads(result.text)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            content = parsed.get("content") if isinstance(parsed.get("content"), str) else None
            for call in parsed.get("tool_calls") or []:
                if isinstance(call, dict) and call.get("name") in known:
                    arguments = call.get("arguments", {})
                    calls.append({
                        "id": f"call_{uuid.uuid4().hex[:24]}",
                        "type": "function",
                        "function": {
                            "name": call["name"],
                            "arguments": arguments if isinstance(arguments, str)
                            else json.dumps(arguments, ensure_ascii=False),
                        },
                    })
        message = {"role": "assistant", "content": content or None}
        if calls:
            message["tool_calls"] = calls

    usage = result.usage
    completion = {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": result.model,
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": "tool_calls" if message.get("tool_calls") else "stop",
        }],
        "usage": {
            "prompt_tokens": usage["input_tokens"],
            "completion_tokens": usage["output_tokens"],
            "total_tokens": usage["input_tokens"] + usage["output_tokens"],
            "prompt_tokens_details": {"cached_tokens": usage["cached_tokens"]},
        },
    }
    return completion, usage
