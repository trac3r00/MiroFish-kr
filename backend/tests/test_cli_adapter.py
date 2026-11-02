import json
import os
import random
import sys
import threading

import pytest
from flask import Flask

import app.api.llm as llm_api
import app.llm.cli_adapter as cli
from app.config import Config
from app.llm import chat, chat_json, usage_tracker

PROPERTY_SEED = 20261102
PROPERTY_CASES = 6

FAKE_CLAUDE = r'''#!{python}
import fcntl, json, os, sys, time

args = sys.argv[1:]
mode = os.environ.get("FAKE_CLAUDE_MODE", "success")
log_dir = os.environ["FAKE_CLAUDE_DIR"]

if args[:1] == ["--version"]:
    print("9.9.9 (Claude Code)")
    sys.exit(0)
if args[:2] == ["auth", "status"]:
    with open(os.path.join(log_dir, "auth-calls"), "a") as handle:
        handle.write("x")
    print("account: someone@example.com token-ish-output")
    sys.exit(0 if os.environ.get("FAKE_CLAUDE_AUTH", "1") == "1" else 1)

stdin = sys.stdin.read()
with open(os.path.join(log_dir, "call-%d.json" % os.getpid()), "w") as handle:
    json.dump({{"argv": args, "stdin": stdin, "cwd": os.getcwd(), "cwd_entries": os.listdir("."),
               "env": dict(os.environ)}}, handle)

if mode == "concurrency":
    counter = os.path.join(log_dir, "counter.json")
    def update(delta):
        with open(os.path.join(log_dir, "counter.lock"), "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            state = json.load(open(counter)) if os.path.exists(counter) else {{"active": 0, "max": 0}}
            state["active"] += delta
            state["max"] = max(state["max"], state["active"])
            json.dump(state, open(counter, "w"))
    update(1)
    time.sleep(float(os.environ.get("FAKE_CLAUDE_HOLD", "0.05")))
    update(-1)
elif mode == "sleep":
    time.sleep(30)
elif mode == "malformed":
    print("this is not json")
    sys.exit(0)
elif mode == "exit":
    sys.stderr.write("internal failure details")
    sys.exit(3)
elif mode == "is_error":
    print(json.dumps({{"type": "result", "subtype": "success", "is_error": True,
                      "result": "Not logged in - Please run /login"}}))
    sys.exit(1)

print(json.dumps({{
    "type": "result", "subtype": "success", "is_error": False,
    "result": os.environ.get("FAKE_CLAUDE_RESULT", "hello"),
    "duration_ms": 12, "session_id": "s-1",
    "usage": {{"input_tokens": 10, "cache_creation_input_tokens": 200,
              "cache_read_input_tokens": 3000, "output_tokens": 7}},
    "modelUsage": {{"claude-haiku-small": {{"outputTokens": 2}},
                   "claude-sonnet-big": {{"outputTokens": 5}}}},
}}))
'''


@pytest.fixture
def fake(monkeypatch, tmp_path):
    bin_dir = tmp_path / "bin"
    log_dir = tmp_path / "log"
    bin_dir.mkdir()
    log_dir.mkdir()
    script = bin_dir / "claude"
    script.write_text(FAKE_CLAUDE.format(python=sys.executable))
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.delenv("CLAUDE_CLI_PATH", raising=False)
    monkeypatch.delenv("CLI_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("CLI_MAX_CONCURRENCY", raising=False)
    monkeypatch.setenv("FAKE_CLAUDE_DIR", str(log_dir))
    cli.clear_status_cache()
    usage_tracker.reset()
    yield log_dir
    cli.clear_status_cache()
    usage_tracker.reset()


def _calls(log_dir):
    return [json.loads(p.read_text()) for p in sorted(log_dir.glob("call-*.json"))]


MESSAGES = [
    {"role": "system", "content": "Answer briefly."},
    {"role": "user", "content": "Say hi"},
]


def test_success_parses_text_usage_and_model(fake):
    result = cli.run_claude_cli(MESSAGES, "sonnet")
    assert result.text == "hello"
    assert result.model == "claude-sonnet-big"
    assert result.usage == {"input_tokens": 3210, "cached_tokens": 3000, "output_tokens": 7}

    [call] = _calls(fake)
    argv = call["argv"]
    assert argv[argv.index("--system-prompt") + 1] == "Answer briefly."
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert argv[argv.index("--output-format") + 1] == "json"
    assert "--no-session-persistence" in argv and "-p" in argv
    assert argv[argv.index("--disallowedTools") + 1] == cli.DISALLOWED_TOOLS
    assert call["stdin"] == "Say hi"
    assert "Say hi" not in argv
    assert call["cwd_entries"] == []
    assert "mirofish-claude-" in call["cwd"]
    assert not os.path.exists(call["cwd"])


def test_multi_turn_transcript_and_no_model_flag(fake):
    cli.run_claude_cli([
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "reply"},
        {"role": "user", "content": [{"type": "text", "text": "second"}]},
    ], None)
    [call] = _calls(fake)
    assert "--model" not in call["argv"]
    assert call["argv"][call["argv"].index("--system-prompt") + 1] == cli.DEFAULT_SYSTEM_PROMPT
    assert call["stdin"].index("first") < call["stdin"].index("reply") < call["stdin"].index("second")


def test_model_falls_back_to_requested_when_model_usage_missing():
    assert cli.pick_model({"result": "x"}, "opus") == "opus"


def test_is_error_raises_typed_error(fake, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "is_error")
    with pytest.raises(cli.ClaudeCLIError) as info:
        cli.run_claude_cli(MESSAGES, None)
    assert info.value.code == "cli_error"
    assert "/login" in str(info.value)


def test_nonzero_exit_raises_without_stderr(fake, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "exit")
    with pytest.raises(cli.ClaudeCLIError) as info:
        cli.run_claude_cli(MESSAGES, None)
    assert info.value.code == "cli_exit"
    assert "3" in str(info.value)
    assert "internal failure details" not in str(info.value)


def test_timeout_raises(fake, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "sleep")
    monkeypatch.setenv("CLI_TIMEOUT_SECONDS", "0.5")
    with pytest.raises(cli.ClaudeCLIError) as info:
        cli.run_claude_cli(MESSAGES, None)
    assert info.value.code == "timeout"


def test_malformed_stdout_raises(fake, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "malformed")
    with pytest.raises(cli.ClaudeCLIError) as info:
        cli.run_claude_cli(MESSAGES, None)
    assert info.value.code == "malformed_output"


def test_missing_binary_raises(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CLI_PATH", raising=False)
    with pytest.raises(cli.ClaudeCLIError) as info:
        cli.run_claude_cli(MESSAGES, None)
    assert info.value.code == "not_installed"


def test_env_is_scrubbed(fake, monkeypatch):
    secrets = {
        "LLM_API_KEY": "sk-llm", "LLM_PROFILES": "[]", "OPENAI_API_KEY": "sk-openai",
        "ANTHROPIC_API_KEY": "sk-ant", "GITHUB_TOKEN": "ghp", "MIROFISH_GATEWAY_TOKEN": "gw",
        "MIROFISH_CREDENTIAL_DIR": "/x",
    }
    for key, value in secrets.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("KEEP_ME", "1")
    cli.run_claude_cli(MESSAGES, None, max_output_tokens=123)
    [call] = _calls(fake)
    env = call["env"]
    assert not set(secrets) & set(env)
    assert not any(k.endswith(("_API_KEY", "_TOKEN")) or k.startswith(("LLM_", "MIROFISH_")) for k in env)
    assert env["KEEP_ME"] == "1"
    # Caps below the floor (CLI_MIN_OUTPUT_TOKENS, default 4096) are raised: the CLI budget
    # includes the model's reasoning, so a tiny cap aborts the answer.
    assert env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "4096"


def test_output_cap_above_floor_passes_through(fake):
    cli.run_claude_cli(MESSAGES, None, max_output_tokens=8000)
    [call] = _calls(fake)
    assert call["env"]["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "8000"


def test_json_mode_asks_for_json_and_strips_fences(fake, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_RESULT", '```json\n{"a": 1}\n```')
    result = cli.run_claude_cli(MESSAGES, None, json_mode=True)
    assert json.loads(result.text) == {"a": 1}
    [call] = _calls(fake)
    assert cli.JSON_ONLY_INSTRUCTION in call["argv"][call["argv"].index("--system-prompt") + 1]


def test_property_concurrency_never_exceeds_semaphore(fake, monkeypatch):
    rng = random.Random(PROPERTY_SEED)
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "concurrency")
    executed = 0
    for case in range(PROPERTY_CASES):
        limit = rng.randint(1, 4)
        calls = rng.randint(limit + 1, 9)
        monkeypatch.setenv("CLI_MAX_CONCURRENCY", str(limit))
        monkeypatch.setenv("FAKE_CLAUDE_HOLD", f"{rng.uniform(0.02, 0.08):.3f}")
        (fake / "counter.json").unlink(missing_ok=True)
        errors = []

        def worker():
            try:
                cli.run_claude_cli(MESSAGES, None)
            except Exception as error:
                errors.append(error)

        threads = [threading.Thread(target=worker) for _ in range(calls)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        state = json.loads((fake / "counter.json").read_text())
        assert errors == [], (case, errors)
        assert state["active"] == 0
        assert 1 <= state["max"] <= limit, (case, limit, state)
        executed += 1
    print(f"concurrency property seed={PROPERTY_SEED} attempted={PROPERTY_CASES} executed={executed}")


@pytest.fixture
def claude_profile_env(fake, monkeypatch):
    for key in list(os.environ):
        if key.startswith("LLM_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(Config, "LLM_API_KEY", None)
    monkeypatch.setattr(Config, "LLM_BASE_URL", None)
    monkeypatch.setattr(Config, "LLM_MODEL_NAME", None)
    monkeypatch.setenv("LLM_PROFILES", json.dumps([
        {"id": "claude", "label": "Claude", "kind": "claude_code_cli", "model": "sonnet"}]))
    return fake


def test_seam_dispatches_and_records_usage_per_stage(claude_profile_env, monkeypatch):
    result = chat(MESSAGES, stage="report")
    assert result.text == "hello"
    assert result.profile_id == "claude"
    assert result.model == "claude-sonnet-big"

    monkeypatch.setenv("FAKE_CLAUDE_RESULT", '```json\n{"ok": true}\n```')
    assert chat_json(MESSAGES, stage="ontology") == {"ok": True}

    entries = {(e["stage"], e["profile_id"], e["model"]): e for e in usage_tracker.snapshot()["entries"]}
    assert entries[("report", "claude", "claude-sonnet-big")]["input_tokens"] == 3210
    assert entries[("ontology", "claude", "claude-sonnet-big")]["cached_tokens"] == 3000


def test_profile_model_is_optional_for_cli_kind(monkeypatch):
    from app.llm import parse_settings

    settings = parse_settings({"LLM_PROFILES": json.dumps([{"id": "c", "kind": "claude_code_cli"}])})
    assert settings.profile("c").model == ""
    assert settings.profile("c").to_public()["has_api_key"] is False


@pytest.fixture
def api_client(claude_profile_env):
    app = Flask(__name__)
    app.register_blueprint(llm_api.llm_bp)
    return app.test_client()


def test_status_reports_cli_state_cached_and_refreshable(api_client, claude_profile_env, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_AUTH", "0")
    body = api_client.get("/api/llm/status").get_json()
    assert body["data"]["cli"]["claude"] == {"installed": True, "version": "9.9.9 (Claude Code)", "signed_in": False}
    assert "someone@example.com" not in json.dumps(body)

    monkeypatch.setenv("FAKE_CLAUDE_AUTH", "1")
    cached = api_client.get("/api/llm/status").get_json()["data"]["cli"]["claude"]
    assert cached["signed_in"] is False
    assert (claude_profile_env / "auth-calls").read_text() == "x"

    fresh = api_client.get("/api/llm/status?refresh=1").get_json()["data"]["cli"]["claude"]
    assert fresh["signed_in"] is True


def test_gateway_serves_cli_with_emulated_tool_calls(api_client, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_RESULT", json.dumps(
        {"tool_calls": [{"name": "create_post", "arguments": {"content": "hi"}}, {"name": "unknown", "arguments": {}}]}))
    response = api_client.post(
        "/api/llm/gateway/v1/chat/completions",
        json={
            "model": "claude",
            "messages": [{"role": "system", "content": "You are an agent."}, {"role": "user", "content": "act"}],
            "tools": [{"type": "function", "function": {"name": "create_post", "parameters": {"type": "object"}}}],
        },
        headers={llm_api.GATEWAY_TOKEN_HEADER: llm_api.get_gateway_token()},
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )
    assert response.status_code == 200
    body = response.get_json()
    choice = body["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    [call] = choice["message"]["tool_calls"]
    assert call["function"]["name"] == "create_post"
    assert json.loads(call["function"]["arguments"]) == {"content": "hi"}
    assert body["usage"]["prompt_tokens_details"]["cached_tokens"] == 3000
    [entry] = usage_tracker.snapshot()["entries"]
    assert (entry["stage"], entry["profile_id"], entry["model"]) == ("simulation_agent", "claude", "claude-sonnet-big")


def test_gateway_maps_cli_errors(api_client, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "is_error")
    response = api_client.post(
        "/api/llm/gateway/v1/chat/completions",
        json={"model": "claude", "messages": [{"role": "user", "content": "x"}]},
        headers={llm_api.GATEWAY_TOKEN_HEADER: llm_api.get_gateway_token()},
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )
    assert response.status_code == 502
    assert response.get_json()["error"]["code"] == "cli_error"
    assert usage_tracker.snapshot()["entries"] == []
