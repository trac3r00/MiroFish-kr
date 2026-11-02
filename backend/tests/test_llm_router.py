import json
import random
from collections import Counter

import pytest

from app.llm.profiles import STAGES, parse_settings
from app.llm.router import assign_agent_profiles, resolve_profile

SEED = 777
CASES = 300


def _settings():
    entries = [
        {"id": "cheap", "kind": "openai_compatible", "model": "deepseek-chat", "base_url": "https://api.deepseek.com/v1", "api_key_env": "DS_KEY"},
        {"id": "chatgpt", "kind": "chatgpt_oauth", "model": "gpt-5"},
    ]
    return parse_settings({
        "LLM_API_KEY": "k",
        "DS_KEY": "d",
        "LLM_PROFILES": json.dumps(entries),
        "LLM_ROUTES": json.dumps({"report": "chatgpt", "simulation_agent": "cheap"}),
    })


def test_resolve_profile_follows_routes_and_defaults():
    settings = _settings()
    assert resolve_profile("report", settings).id == "chatgpt"
    assert resolve_profile("simulation_agent", settings).id == "cheap"
    assert resolve_profile("ontology", settings).id == "default"
    assert resolve_profile("default", settings).id == "default"
    assert settings.agent_pool == {"cheap": 1.0}


def test_resolve_profile_rejects_unknown_stage_and_empty_config():
    with pytest.raises(ValueError, match="nope"):
        resolve_profile("nope", _settings())
    with pytest.raises(ValueError, match="No LLM profile"):
        resolve_profile("report", parse_settings({}))


def test_legacy_only_routes_every_stage_to_default():
    settings = parse_settings({"LLM_API_KEY": "k"})
    assert {resolve_profile(stage, settings).id for stage in STAGES} == {"default"}


@pytest.mark.parametrize(
    ("pool", "n", "expected"),
    [
        ({"a": 1, "b": 1}, 10, {"a": 5, "b": 5}),
        ({"a": 3, "b": 1}, 8, {"a": 6, "b": 2}),
        ({"a": 1}, 7, {"a": 7}),
        ({"a": 1, "b": 1, "c": 2}, 4, {"a": 1, "b": 1, "c": 2}),
    ],
)
def test_assign_agent_profiles_spec_examples(pool, n, expected):
    result = assign_agent_profiles(range(n), seed=1, pool=pool)
    assert dict(Counter(result.values())) == expected


def test_assign_agent_profiles_empty_pool_raises():
    with pytest.raises(ValueError):
        assign_agent_profiles([1, 2], seed=1, pool={})


def test_property_assign_agent_profiles_deterministic_weighted_complete():
    rng = random.Random(SEED)
    executed = 0
    for _ in range(CASES):
        pool = {f"p{i}": rng.choice([1, 2, 3, 0.5, 7]) for i in range(rng.randint(1, 5))}
        n = rng.randint(0, 120)
        agent_ids = rng.sample(range(10_000), n)
        seed = rng.randint(0, 2**31)

        first = assign_agent_profiles(agent_ids, seed=seed, pool=pool)
        shuffled = list(agent_ids)
        rng.shuffle(shuffled)
        second = assign_agent_profiles(shuffled, seed=seed, pool=pool)

        assert first == second
        assert set(first) == set(agent_ids)
        assert set(first.values()) <= set(pool)
        counts = Counter(first.values())
        total_weight = sum(pool.values())
        for pid, weight in pool.items():
            assert abs(counts.get(pid, 0) - n * weight / total_weight) < 1 + 1e-9
        executed += 1
    assert executed == CASES
