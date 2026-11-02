import importlib
import json
import random
import sys
from pathlib import Path

import dotenv
import pytest

from app.llm.profiles import LLMProfile

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"


def _load_runner():
    if "run_parallel_simulation" in sys.modules:
        return sys.modules["run_parallel_simulation"]
    # The script loads the root .env at import time; keep the test process environment clean.
    original = dotenv.load_dotenv
    dotenv.load_dotenv = lambda *args, **kwargs: False
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    try:
        return importlib.import_module("run_parallel_simulation")
    finally:
        dotenv.load_dotenv = original


runner = _load_runner()
from camel.types import ModelPlatformType, ModelType  # noqa: E402  (installed with the runner deps)

OPENAI_KEY = "sk-openai-secret-0123456789"
DEEPSEEK_KEY = "sk-deepseek-secret-0123456789"
GATEWAY_TOKEN = "gw-token-secret-0123456789"


def _profiles():
    return [
        LLMProfile(id="default", kind="openai_compatible", label="Default", model="gpt-4o-mini",
                   base_url="https://api.openai.com/v1", api_key=OPENAI_KEY, api_key_env="LLM_API_KEY"),
        LLMProfile(id="deepseek", kind="openai_compatible", label="DeepSeek", model="deepseek-chat",
                   base_url="https://api.deepseek.com/v1", api_key=DEEPSEEK_KEY, api_key_env="DEEPSEEK_KEY",
                   extra_headers={"X-Extra": "1"}),
        LLMProfile(id="chatgpt", kind="chatgpt_oauth", label="ChatGPT", model="gpt-5", base_url=None),
    ]


ENV = {"FLASK_PORT": "5099", "MIROFISH_GATEWAY_TOKEN": GATEWAY_TOKEN}


def _config(profile_ids):
    return {"agent_configs": [
        {"agent_id": i, **({"llm_profile": pid} if pid is not None else {})}
        for i, pid in enumerate(profile_ids)
    ]}


class RecordingFactory:
    def __init__(self):
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return object()


def test_config_without_llm_profile_keeps_single_model():
    assert runner.build_agent_model_specs(_config([None, None]), _profiles(), env=ENV) is None
    assert runner.plan_agent_models(_config([None])) is None


def test_specs_per_profile_kind():
    plan = runner.build_agent_model_specs(_config(["default", "deepseek", "chatgpt"]), _profiles(), env=ENV)

    openai = plan.specs["default"]
    assert openai.model_platform == ModelPlatformType.OPENAI
    assert (openai.model_type, openai.url, openai.api_key) == ("gpt-4o-mini", "https://api.openai.com/v1", OPENAI_KEY)

    compatible = plan.specs["deepseek"]
    assert compatible.model_platform == ModelPlatformType.OPENAI_COMPATIBLE_MODEL
    assert (compatible.model_type, compatible.url, compatible.api_key) == (
        "deepseek-chat", "https://api.deepseek.com/v1", DEEPSEEK_KEY)
    assert dict(compatible.headers) == {"X-Extra": "1"}

    gateway = plan.specs["chatgpt"]
    assert gateway.model_platform == ModelPlatformType.OPENAI_COMPATIBLE_MODEL
    assert gateway.url == "http://127.0.0.1:5099/api/llm/gateway/v1"
    assert gateway.model_type == "chatgpt"
    assert dict(gateway.headers) == {"X-MiroFish-Gateway-Token": GATEWAY_TOKEN, "X-MiroFish-Profile": "chatgpt"}
    assert gateway.api_key != GATEWAY_TOKEN and gateway.via_gateway

    assert plan.agent_profiles == {0: "default", 1: "deepseek", 2: "chatgpt"}


def test_gateway_default_port_and_missing_token():
    plan = runner.build_agent_model_specs(_config(["chatgpt"]), _profiles(), env={"MIROFISH_GATEWAY_TOKEN": "t"})
    assert plan.specs["chatgpt"].url == "http://127.0.0.1:5001/api/llm/gateway/v1"
    with pytest.raises(ValueError, match="MIROFISH_GATEWAY_TOKEN"):
        runner.build_agent_model_specs(_config(["chatgpt"]), _profiles(), env={})


@pytest.mark.parametrize("kind", ["claude_code_cli", "cursor_agent_cli"])
def test_cli_profiles_route_through_gateway(kind):
    profiles = [LLMProfile(id="cli", kind=kind, label="CLI", model="sonnet", base_url=None)]
    spec = runner.build_agent_model_specs(_config(["cli"]), profiles, env=ENV).specs["cli"]
    assert spec.via_gateway and spec.model_type == "cli"
    assert spec.url == "http://127.0.0.1:5099/api/llm/gateway/v1"
    assert dict(spec.headers) == {"X-MiroFish-Gateway-Token": GATEWAY_TOKEN, "X-MiroFish-Profile": "cli"}


def test_missing_and_unknown_profiles_use_fallback_or_legacy():
    config = _config(["deepseek", None, "gone"])
    legacy = runner.build_agent_model_specs(config, _profiles(), env=ENV)
    assert legacy.agent_profiles == {0: "deepseek", 1: None, 2: None}
    assert legacy.unknown_profiles == {"gone": [2]}

    routed = runner.build_agent_model_specs(config, _profiles(), fallback_profile_id="default", env=ENV)
    assert routed.agent_profiles == {0: "deepseek", 1: "default", 2: "default"}
    assert set(routed.specs) == {"deepseek", "default"}


def test_secrets_not_in_repr_or_plan_description():
    plan = runner.build_agent_model_specs(_config(["default", "deepseek", "chatgpt", "gone"]), _profiles(), env=ENV)
    text = repr(plan) + "\n".join(runner.describe_agent_model_plan(plan))
    for secret in (OPENAI_KEY, DEEPSEEK_KEY, GATEWAY_TOKEN):
        assert secret not in text


def test_one_backend_per_distinct_profile():
    ids = ["default", "deepseek", "chatgpt"] * 20 + ["deepseek"] * 7
    plan = runner.build_agent_model_specs(_config(ids), _profiles(), env=ENV)
    factory = RecordingFactory()
    backends = runner.create_model_backends(plan, factory=factory)

    assert len(factory.calls) == 3
    assert sorted(call["model_type"] for call in factory.calls) == ["chatgpt", "deepseek-chat", "gpt-4o-mini"]
    gateway_call = next(call for call in factory.calls if call["model_type"] == "chatgpt")
    assert gateway_call["default_headers"]["X-MiroFish-Gateway-Token"] == GATEWAY_TOKEN
    openai_call = next(call for call in factory.calls if call["model_type"] == "gpt-4o-mini")
    assert "default_headers" not in openai_call
    for agent_id, profile_id in plan.agent_profiles.items():
        assert backends[profile_id] is backends[ids[agent_id]]


def test_property_every_agent_mapped_and_backends_deduplicated():
    rng = random.Random(20261102)
    profile_ids = [p.id for p in _profiles()]
    attempted = executed = 0
    for _ in range(300):
        attempted += 1
        n = rng.randint(1, 60)
        choices = profile_ids + [None, "unknown"]
        ids = [rng.choice(choices) for _ in range(n)]
        if not any(ids):
            ids[0] = rng.choice(profile_ids)
        fallback = rng.choice([None, *profile_ids])
        plan = runner.build_agent_model_specs(_config(ids), _profiles(), fallback_profile_id=fallback, env=ENV)

        expected = {i: (pid if pid in profile_ids else fallback) for i, pid in enumerate(ids)}
        assert plan.agent_profiles == expected
        used = {pid for pid in expected.values() if pid is not None}
        assert set(plan.specs) == used

        factory = RecordingFactory()
        backends = runner.create_model_backends(plan, factory=factory)
        assert len(factory.calls) == len(used)
        assert set(backends) == used
        executed += 1
    assert executed == attempted == 300


def _stub_backend():
    return runner.ModelFactory.create(model_platform=ModelPlatformType.OPENAI, model_type=ModelType.STUB)


def test_build_agent_graph_assigns_each_agent_its_backend(tmp_path):
    reddit_path = tmp_path / "reddit_profiles.json"
    reddit_path.write_text(json.dumps([
        {"username": f"user{i}", "bio": "bio", "persona": "persona", "mbti": "INTJ",
         "gender": "female", "age": 30, "country": "KR"}
        for i in range(4)
    ]), encoding="utf-8")
    twitter_path = tmp_path / "twitter_profiles.csv"
    twitter_path.write_text(
        "username,description,user_char\n" + "".join(f"user{i},desc,char{i}\n" for i in range(4)),
        encoding="utf-8",
    )
    backends = [_stub_backend(), _stub_backend()]

    for platform, path, actions in (
        ("reddit", reddit_path, runner.REDDIT_ACTIONS),
        ("twitter", twitter_path, runner.TWITTER_ACTIONS),
    ):
        graph = runner.build_agent_graph(platform, str(path), lambda i: backends[i % 2], actions)
        agents = dict(graph.get_agents())
        assert sorted(agents) == [0, 1, 2, 3]
        for agent_id, agent in agents.items():
            assert agent.model_backend.models == [backends[agent_id % 2]]
            assert agent.user_info.name == f"user{agent_id}"
            assert agent.user_info.recsys_type == platform
