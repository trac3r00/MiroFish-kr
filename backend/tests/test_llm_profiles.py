import json
import random
import string

import pytest

from app.config import Config
from app.llm.profiles import STAGES, list_profiles_public, parse_settings

SEED = 20261102
CASES = 300

LEGACY = {
    "LLM_API_KEY": "sk-legacy-KEY-0000000000",
    "LLM_BASE_URL": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "LLM_MODEL_NAME": "qwen-plus",
}


@pytest.fixture
def clean_llm_env(monkeypatch):
    import os

    for key in list(os.environ):
        if key.startswith("LLM_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(Config, "LLM_API_KEY", None)
    return monkeypatch


def test_legacy_env_yields_single_default_profile_routed_everywhere():
    settings = parse_settings(LEGACY)

    assert [p.id for p in settings.profiles] == ["default"]
    profile = settings.profiles[0]
    assert (profile.kind, profile.model, profile.base_url, profile.api_key) == (
        "openai_compatible", "qwen-plus", LEGACY["LLM_BASE_URL"], LEGACY["LLM_API_KEY"],
    )
    assert settings.routes == {stage: "default" for stage in STAGES}
    assert settings.agent_pool == {"default": 1.0}


def test_legacy_defaults_match_config_defaults():
    profile = parse_settings({"LLM_API_KEY": "k"}).profiles[0]
    assert (profile.base_url, profile.model) == ("https://api.openai.com/v1", "gpt-4o-mini")


def test_boost_profile_only_when_all_three_vars_set():
    boost = {"LLM_BOOST_API_KEY": "bk", "LLM_BOOST_BASE_URL": "https://b.example/v1", "LLM_BOOST_MODEL_NAME": "bm"}
    assert [p.id for p in parse_settings({**LEGACY, **boost}).profiles] == ["default", "boost"]
    partial = dict(boost, LLM_BOOST_MODEL_NAME="")
    assert [p.id for p in parse_settings({**LEGACY, **partial}).profiles] == ["default"]


def test_no_configuration_yields_no_profiles():
    settings = parse_settings({})
    assert settings.profiles == () and settings.routes == {} and settings.default_profile_id is None


def test_profiles_file_path_is_supported(tmp_path):
    path = tmp_path / "profiles.json"
    path.write_text(json.dumps([
        {"id": "local", "kind": "openai_compatible", "model": "m", "base_url": "http://127.0.0.1:11434/v1", "api_key_env": "LOCAL_KEY"}
    ]))
    settings = parse_settings({"LLM_PROFILES": str(path), "LOCAL_KEY": "x"})
    assert [p.id for p in settings.profiles] == ["local"]
    assert settings.default_profile_id == "local"


def test_profiles_entry_replaces_legacy_profile_with_same_id():
    entry = {"id": "default", "kind": "chatgpt_oauth", "model": "gpt-5"}
    settings = parse_settings({**LEGACY, "LLM_PROFILES": json.dumps([entry])})
    assert [(p.id, p.kind) for p in settings.profiles] == [("default", "chatgpt_oauth")]


def test_config_validate_surfaces_llm_config_errors(clean_llm_env):
    clean_llm_env.setenv("LLM_API_KEY", "k")
    clean_llm_env.setenv("LLM_ROUTES", '{"not_a_stage": "default"}')
    errors = Config.validate()
    assert any("not_a_stage" in e for e in errors)


def test_config_validate_accepts_profiles_without_legacy_key(clean_llm_env):
    clean_llm_env.setenv("LLM_PROFILES", '[{"id": "chatgpt", "kind": "chatgpt_oauth", "model": "gpt-5"}]')
    assert not any("LLM" in e for e in Config.validate())
    clean_llm_env.delenv("LLM_PROFILES")
    # Message text is localized (t()); assert on the env var name, which every locale keeps.
    assert any("LLM_API_KEY" in e for e in Config.validate())


# ---- property tests (seeded generated cases) ----

def _rand_id(rng):
    return "p" + "".join(rng.choice(string.ascii_lowercase + string.digits + "_-") for _ in range(rng.randint(1, 10)))


def _rand_secret(rng):
    return "SK" + "".join(rng.choice(string.ascii_uppercase + string.digits) for _ in range(rng.randint(20, 40)))


def _generate_valid(rng):
    env = {}
    use_legacy = rng.random() < 0.5
    if use_legacy:
        env.update(LLM_API_KEY=_rand_secret(rng), LLM_MODEL_NAME="legacy-model")
    entries, ids = [], set(["default"] if use_legacy else [])
    for index in range(rng.randint(0 if use_legacy else 1, 5)):
        pid = _rand_id(rng)
        while pid in ids:
            pid = _rand_id(rng)
        ids.add(pid)
        if rng.random() < 0.3:
            entry = {"id": pid, "kind": "chatgpt_oauth", "model": "gpt-5"}
        else:
            key_env = f"KEY_ENV_{index}"
            env[key_env] = _rand_secret(rng)
            entry = {
                "id": pid, "kind": "openai_compatible", "model": f"model-{index}",
                "base_url": rng.choice(["https://api.openai.com/v1", "https://dashscope.aliyuncs.com/v1", "http://localhost:8000/v1"]),
                "api_key_env": key_env,
            }
            if rng.random() < 0.5:
                entry["extra_headers"] = {"X-Secret": _rand_secret(rng)}
        if rng.random() < 0.5:
            entry["label"] = f"Label {index}"
        entries.append(entry)
    if entries:
        env["LLM_PROFILES"] = json.dumps(entries)
    ordered = (["default"] if use_legacy else []) + [e["id"] for e in entries]
    routes = {stage: rng.choice(ordered) for stage in rng.sample(STAGES, rng.randint(0, len(STAGES)))}
    if routes:
        env["LLM_ROUTES"] = json.dumps(routes)
    pool = None
    if rng.random() < 0.5:
        chosen = rng.sample(ordered, rng.randint(1, len(ordered)))
        if rng.random() < 0.5:
            env["LLM_AGENT_POOL"] = ",".join(chosen)
            pool = {pid: 1.0 for pid in chosen}
        else:
            pool = {pid: float(rng.randint(1, 9)) for pid in chosen}
            env["LLM_AGENT_POOL"] = json.dumps(pool)
    return env, ordered, routes, pool, entries


def test_property_random_valid_configs_parse_to_model():
    rng = random.Random(SEED)
    executed = 0
    for _ in range(CASES):
        env, ordered, routes, pool, entries = _generate_valid(rng)
        settings = parse_settings(env)

        default_id = "default" if "default" in ordered else ordered[0]
        expected_routes = {stage: routes.get(stage, default_id) for stage in STAGES}
        assert [p.id for p in settings.profiles] == ordered
        assert settings.default_profile_id == default_id
        assert settings.routes == expected_routes
        assert settings.agent_pool == (pool or {expected_routes["simulation_agent"]: 1.0})
        for entry in entries:
            profile = settings.profile(entry["id"])
            assert profile.label == entry.get("label", entry["id"])
            assert profile.api_key == (env[entry["api_key_env"]] if "api_key_env" in entry else None)
        executed += 1
    assert executed == CASES


def _malformed_cases(rng):
    good_entry = {"id": "good", "kind": "openai_compatible", "model": "m", "base_url": "https://x.example/v1", "api_key_env": "GOOD_KEY"}
    base = {"LLM_API_KEY": "k", "GOOD_KEY": "g", "LLM_PROFILES": json.dumps([good_entry])}
    stage_word = "".join(rng.choice(string.ascii_lowercase) for _ in range(rng.randint(3, 12)))
    unknown_pid = "zz" + _rand_id(rng)
    raw = json.dumps({"report": "good"})
    return [
        ({**base, "LLM_ROUTES": raw[: rng.randint(1, len(raw) - 1)]}, "LLM_ROUTES"),
        ({**base, "LLM_ROUTES": json.dumps({"x_" + stage_word: "good"})}, "x_" + stage_word),
        ({**base, "LLM_ROUTES": json.dumps({rng.choice(STAGES): unknown_pid})}, unknown_pid),
        ({**base, "LLM_ROUTES": json.dumps([rng.choice(STAGES)])}, "LLM_ROUTES"),
        ({**base, "LLM_AGENT_POOL": f"good,{unknown_pid}"}, unknown_pid),
        ({**base, "LLM_AGENT_POOL": json.dumps({"good": -rng.randint(0, 5)})}, "good"),
        ({**base, "LLM_AGENT_POOL": "good,,default"}, "LLM_AGENT_POOL"),
        ({**base, "LLM_PROFILES": base["LLM_PROFILES"][: rng.randint(1, 20)]}, "LLM_PROFILES"),
        ({**base, "LLM_PROFILES": json.dumps({"id": "x"})}, "LLM_PROFILES"),
        ({**base, "LLM_PROFILES": json.dumps([good_entry, good_entry])}, "good"),
        ({**base, "LLM_PROFILES": json.dumps([dict(good_entry, kind=stage_word)])}, "kind"),
        ({**base, "LLM_PROFILES": json.dumps([dict(good_entry, api_key_env="MISSING_" + stage_word.upper())])}, "MISSING_"),
        ({**base, "LLM_PROFILES": json.dumps([dict(good_entry, base_url="ftp://" + stage_word)])}, "base_url"),
        ({**base, "LLM_PROFILES": json.dumps([dict(good_entry, id="bad id!")])}, "id"),
        ({**base, "LLM_PROFILES": json.dumps([dict(good_entry, **{stage_word: 1})])}, stage_word),
        ({**base, "LLM_PROFILES": "/nonexistent/" + stage_word + ".json"}, stage_word),
    ]


def test_property_malformed_configs_raise_value_error_with_message():
    rng = random.Random(SEED + 1)
    attempted = executed = 0
    for _ in range(CASES // 10):
        for env, needle in _malformed_cases(rng):
            attempted += 1
            with pytest.raises(ValueError) as captured:
                parse_settings(env)
            assert needle in str(captured.value)
            executed += 1
    assert executed == attempted == (CASES // 10) * 16


def test_property_public_listing_never_contains_secrets():
    rng = random.Random(SEED + 2)
    executed = 0
    for _ in range(CASES):
        env, _, _, _, entries = _generate_valid(rng)
        settings = parse_settings(env)
        secrets = [p.api_key for p in settings.profiles if p.api_key]
        secrets += [v for p in settings.profiles for v in p.extra_headers.values()]
        dumped = json.dumps(list_profiles_public(env)) + json.dumps(settings.to_public())
        dumped += "".join(repr(p) for p in settings.profiles)
        for secret in secrets:
            for start in range(0, len(secret) - 11):
                assert secret[start:start + 12] not in dumped
        executed += 1
    assert executed == CASES
