import json
import os
import sys

import pytest
from flask import Flask

import app.api.llm as llm_api
import app.llm.cli_adapter as cli
from app.config import Config
from app.llm import chat, chat_json, parse_settings, usage_tracker

FAKE_AGENT = r'''#!{python}
import json, os, sys

args = sys.argv[1:]
mode = os.environ.get("FAKE_AGENT_MODE", "success")
log_dir = os.environ["FAKE_AGENT_DIR"]

if args[:1] in (["--version"], ["-v"]):
    print("2099.01.01-fake")
    sys.exit(0)
if args[:1] == ["status"]:
    if os.environ.get("FAKE_AGENT_AUTH", "1") == "1":
        print("Logged in as someone@example.com")
    else:
        print("Not logged in")
    sys.exit(0)

workspace = args[args.index("--workspace") + 1] if "--workspace" in args else None
permissions = None
if workspace and os.path.isfile(os.path.join(workspace, ".cursor", "cli.json")):
    permissions = json.load(open(os.path.join(workspace, ".cursor", "cli.json")))
with open(os.path.join(log_dir, "call-%d.json" % os.getpid()), "w") as handle:
    json.dump({{"argv": args, "cwd": os.getcwd(), "workspace": workspace, "permissions": permissions,
               "env": dict(os.environ)}}, handle)

if "--force" in args or "--yolo" in args or "--approve-mcps" in args:
    sys.stderr.write("refusing: unsafe flag")
    sys.exit(9)
if mode == "exit":
    sys.stderr.write("internal failure details someone@example.com")
    sys.exit(3)
if mode == "signed_out":
    sys.stderr.write("Error: Not logged in. Run agent login.")
    sys.exit(1)
if mode == "malformed":
    print("not json")
    sys.exit(0)

print(json.dumps({{
    "type": "result", "subtype": "success", "is_error": False,
    "result": os.environ.get("FAKE_AGENT_RESULT", "hello there"),
    "session_id": "s-1", "duration_ms": 12, "duration_api_ms": 10,
}}))
'''


@pytest.fixture
def fake(monkeypatch, tmp_path):
    bin_dir = tmp_path / "bin"
    log_dir = tmp_path / "log"
    bin_dir.mkdir()
    log_dir.mkdir()
    script = bin_dir / "agent"
    script.write_text(FAKE_AGENT.format(python=sys.executable))
    script.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.delenv("CURSOR_CLI_PATH", raising=False)
    monkeypatch.delenv("CLI_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("CLI_MAX_CONCURRENCY", raising=False)
    monkeypatch.setenv("FAKE_AGENT_DIR", str(log_dir))
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


def test_success_parses_result_with_estimated_usage(fake):
    result = cli.run_cursor_cli(MESSAGES, "gpt-5")
    assert result.text == "hello there"
    assert result.model == "gpt-5"
    assert result.estimated is True
    prompt = "Answer briefly.\n\nSay hi"
    assert result.usage == {"input_tokens": len(prompt) // 4, "cached_tokens": 0,
                            "output_tokens": len("hello there") // 4}

    [call] = _calls(fake)
    argv = call["argv"]
    assert argv[:3] == ["-p", "--output-format", "json"]
    assert argv[argv.index("--model") + 1] == "gpt-5"
    assert argv[argv.index("--mode") + 1] == "ask"
    assert argv[-2:] == ["--", prompt]
    assert not {"--force", "--yolo", "--approve-mcps"} & set(argv)
    assert os.path.realpath(call["workspace"]) == os.path.realpath(call["cwd"])
    assert "mirofish-cursor-" in call["cwd"]
    assert call["permissions"] == cli.CURSOR_PERMISSIONS
    deny = call["permissions"]["permissions"]["deny"]
    assert {"Shell(*)", "Write(*)", "Mcp(*:*)"} <= set(deny)
    assert not os.path.exists(call["cwd"])


def test_default_model_when_profile_has_none(fake):
    result = cli.run_cursor_cli([{"role": "user", "content": "x"}], None)
    assert result.model == cli.CURSOR_DEFAULT_MODEL
    [call] = _calls(fake)
    assert "--model" not in call["argv"]
    assert call["argv"][-1] == "x"


def test_nonzero_exit_raises_without_stderr(fake, monkeypatch):
    monkeypatch.setenv("FAKE_AGENT_MODE", "exit")
    with pytest.raises(cli.CursorCLIError) as info:
        cli.run_cursor_cli(MESSAGES, None)
    assert info.value.code == "cli_exit"
    assert "3" in str(info.value)
    assert "internal failure" not in str(info.value)
    assert "someone@example.com" not in str(info.value)
    [call] = _calls(fake)
    assert not os.path.exists(call["cwd"])


def test_signed_out_call_is_reported(fake, monkeypatch):
    monkeypatch.setenv("FAKE_AGENT_MODE", "signed_out")
    with pytest.raises(cli.CursorCLIError) as info:
        cli.run_cursor_cli(MESSAGES, None)
    assert info.value.code == "not_signed_in"


def test_malformed_and_missing_binary(fake, monkeypatch, tmp_path):
    monkeypatch.setenv("FAKE_AGENT_MODE", "malformed")
    with pytest.raises(cli.CursorCLIError) as info:
        cli.run_cursor_cli(MESSAGES, None)
    assert info.value.code == "malformed_output"

    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    with pytest.raises(cli.CursorCLIError) as info:
        cli.run_cursor_cli(MESSAGES, None)
    assert info.value.code == "not_installed"


def test_cursor_agent_alias_is_found(fake, monkeypatch, tmp_path):
    alias_dir = tmp_path / "alias"
    alias_dir.mkdir()
    os.rename(tmp_path / "bin" / "agent", alias_dir / "cursor-agent")
    monkeypatch.setenv("PATH", str(alias_dir))
    assert cli.find_cursor_binary() == str(alias_dir / "cursor-agent")
    assert cli.run_cursor_cli(MESSAGES, None).text == "hello there"


def test_env_is_scrubbed(fake, monkeypatch):
    secrets = {
        "LLM_API_KEY": "sk-llm", "OPENAI_API_KEY": "sk-openai", "CURSOR_API_KEY": "cur",
        "GITHUB_TOKEN": "ghp", "MIROFISH_GATEWAY_TOKEN": "gw",
    }
    for key, value in secrets.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("KEEP_ME", "1")
    cli.run_cursor_cli(MESSAGES, None)
    [call] = _calls(fake)
    env = call["env"]
    assert not set(secrets) & set(env)
    assert not any(k.endswith(("_API_KEY", "_TOKEN")) or k.startswith(("LLM_", "MIROFISH_")) for k in env)
    assert env["KEEP_ME"] == "1"


def test_status_parses_not_logged_in_with_exit_zero(fake, monkeypatch):
    monkeypatch.setenv("FAKE_AGENT_AUTH", "0")
    assert cli.cursor_cli_status(force=True) == {"installed": True, "version": "2099.01.01-fake", "signed_in": False}
    monkeypatch.setenv("FAKE_AGENT_AUTH", "1")
    assert cli.cursor_cli_status()["signed_in"] is False
    assert cli.cursor_cli_status(force=True)["signed_in"] is True


@pytest.fixture
def cursor_profile_env(fake, monkeypatch):
    for key in list(os.environ):
        if key.startswith("LLM_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(Config, "LLM_API_KEY", None)
    monkeypatch.setattr(Config, "LLM_BASE_URL", None)
    monkeypatch.setattr(Config, "LLM_MODEL_NAME", None)
    monkeypatch.setenv("LLM_PROFILES", json.dumps([
        {"id": "cursor", "label": "Cursor", "kind": "cursor_agent_cli", "model": "gpt-5"}]))
    return fake


def test_profile_model_is_optional():
    settings = parse_settings({"LLM_PROFILES": json.dumps([{"id": "c", "kind": "cursor_agent_cli"}])})
    assert settings.profile("c").model == ""


def test_seam_dispatches_and_records_estimated_usage(cursor_profile_env, monkeypatch):
    result = chat(MESSAGES, stage="report")
    assert (result.text, result.profile_id, result.model, result.estimated) == ("hello there", "cursor", "gpt-5", True)

    monkeypatch.setenv("FAKE_AGENT_RESULT", '```json\n{"ok": true}\n```')
    assert chat_json(MESSAGES, stage="ontology") == {"ok": True}
    [call_a, call_b] = sorted(_calls(cursor_profile_env), key=lambda c: len(c["argv"][-1]))
    assert cli.JSON_ONLY_INSTRUCTION in call_b["argv"][-1]

    entries = {(e["stage"], e["profile_id"], e["model"]): e for e in usage_tracker.snapshot()["entries"]}
    assert entries[("report", "cursor", "gpt-5")]["cached_tokens"] == 0
    assert entries[("report", "cursor", "gpt-5")]["output_tokens"] == len("hello there") // 4


@pytest.fixture
def api_client(cursor_profile_env):
    app = Flask(__name__)
    app.register_blueprint(llm_api.llm_bp)
    return app.test_client()


def test_status_endpoint_never_leaks_status_output(api_client, monkeypatch):
    monkeypatch.setenv("FAKE_AGENT_AUTH", "1")
    body = api_client.get("/api/llm/status?refresh=1").get_json()
    assert body["data"]["cli"]["cursor"] == {"installed": True, "version": "2099.01.01-fake", "signed_in": True}
    assert "someone@example.com" not in json.dumps(body)


def _gateway(api_client, payload):
    return api_client.post(
        "/api/llm/gateway/v1/chat/completions",
        json=payload,
        headers={llm_api.GATEWAY_TOKEN_HEADER: llm_api.get_gateway_token()},
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )


def test_gateway_serves_cursor_kind(api_client, monkeypatch):
    monkeypatch.setenv("FAKE_AGENT_RESULT", json.dumps(
        {"tool_calls": [{"name": "create_post", "arguments": {"content": "hi"}}]}))
    response = _gateway(api_client, {
        "model": "cursor",
        "messages": [{"role": "system", "content": "You are an agent."}, {"role": "user", "content": "act"}],
        "tools": [{"type": "function", "function": {"name": "create_post", "parameters": {"type": "object"}}}],
    })
    assert response.status_code == 200
    choice = response.get_json()["choices"][0]
    [call] = choice["message"]["tool_calls"]
    assert call["function"]["name"] == "create_post"
    [entry] = usage_tracker.snapshot()["entries"]
    assert (entry["stage"], entry["profile_id"], entry["model"]) == ("simulation_agent", "cursor", "gpt-5")


def test_gateway_maps_cursor_errors(api_client, monkeypatch):
    monkeypatch.setenv("FAKE_AGENT_MODE", "signed_out")
    response = _gateway(api_client, {"model": "cursor", "messages": [{"role": "user", "content": "x"}]})
    assert response.status_code == 502
    assert response.get_json()["error"]["code"] == "not_signed_in"
    assert usage_tracker.snapshot()["entries"] == []
