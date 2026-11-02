"""Simulation config generation: LLM seam, locale region profiles, archetypes, agent LLM profiles."""

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services import simulation_config_generator as scg
from app.services.persona_archetypes import archetype_activity_overrides, assign_archetypes
from app.services.zep_entity_reader import EntityNode
from app.utils.locale import get_language_instruction, set_locale

MODULE_PATH = Path(scg.__file__)
ENTITY_TYPES = ["Person", "Student", "MediaOutlet", "University"]


def _entities(n):
    return [
        EntityNode(
            uuid=f"uuid-{i}",
            name=f"Entity {i}",
            labels=["Entity", ENTITY_TYPES[i % len(ENTITY_TYPES)]],
            summary=f"Summary {i}",
            attributes={},
        )
        for i in range(n)
    ]


def _response(payload, finish_reason="stop"):
    return SimpleNamespace(
        choices=[SimpleNamespace(finish_reason=finish_reason, message=SimpleNamespace(content=json.dumps(payload)))]
    )


class FakeSeam:
    """Stands in for app.llm.client.create_completion; answers by system prompt kind."""

    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def __call__(self, profile, *, stage, messages, temperature=None, max_tokens=None, response_format=None):
        self.calls.append(
            {"profile": profile, "stage": stage, "messages": messages, "max_tokens": max_tokens}
        )
        if self.fail:
            raise RuntimeError("provider down")
        system, user = messages[0]["content"], messages[1]["content"]
        if system.startswith(scg.SimulationConfigGenerator.TIME_CONFIG_SYSTEM_PROMPT):
            return _response({
                "total_simulation_hours": 48, "minutes_per_round": 60,
                "agents_per_hour_min": 2, "agents_per_hour_max": 6,
                "peak_hours": [21, 22, 23], "off_peak_hours": [2, 3, 4, 5],
                "morning_hours": [7, 8], "work_hours": list(range(9, 18)), "reasoning": "r",
            })
        if system.startswith(scg.SimulationConfigGenerator.EVENT_CONFIG_SYSTEM_PROMPT):
            return _response({
                "hot_topics": ["fare"], "narrative_direction": "d",
                "initial_posts": [{"content": "hello", "poster_type": "MediaOutlet"}], "reasoning": "r",
            })
        ids = [int(x) for x in re.findall(r'"agent_id": (\d+)', user)]
        return _response({"agent_configs": [
            {"agent_id": i, "activity_level": 0.33, "posts_per_hour": 0.5, "comments_per_hour": 1.0,
             "active_hours": [10, 11], "response_delay_min": 3, "response_delay_max": 9,
             "sentiment_bias": 0.0, "stance": "neutral", "influence_weight": 1.7}
            for i in ids
        ]})


@pytest.fixture
def llm_env(monkeypatch):
    for key in ("LLM_ROUTES", "LLM_BOOST_API_KEY", "LLM_BOOST_BASE_URL", "LLM_BOOST_MODEL_NAME"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("LLM_API_KEY", "sk-test-default")
    monkeypatch.setenv("LLM_BASE_URL", "https://llm.example/v1")
    monkeypatch.setenv("LLM_MODEL_NAME", "default-model")
    monkeypatch.setenv("KEY_A", "ka")
    monkeypatch.setenv("KEY_B", "kb")
    monkeypatch.setenv("LLM_PROFILES", json.dumps([
        {"id": "pa", "kind": "openai_compatible", "label": "A", "model": "model-a",
         "base_url": "https://a.example/v1", "api_key_env": "KEY_A"},
        {"id": "pb", "kind": "openai_compatible", "label": "B", "model": "model-b",
         "base_url": "https://b.example/v1", "api_key_env": "KEY_B"},
    ]))
    monkeypatch.setenv("LLM_AGENT_POOL", "pa,pb")
    monkeypatch.setattr(scg.time, "sleep", lambda _s: None)
    yield monkeypatch
    set_locale("en")


def _generate(monkeypatch, locale, n=7, persona_mix=None, seam=None, sim_id="sim_abc"):
    set_locale(locale)
    seam = seam or FakeSeam()
    monkeypatch.setattr(scg, "create_completion", seam)
    gen = scg.SimulationConfigGenerator()
    params = gen.generate_config(
        simulation_id=sim_id, project_id="p", graph_id="g",
        simulation_requirement="Subway fare increase", document_text="doc",
        entities=_entities(n), persona_mix=persona_mix,
    )
    return params, seam


def test_region_profiles_values():
    ko = scg.REGION_PROFILES["ko-KR"]
    assert ko["timezone"] == "Asia/Seoul"
    assert ko["routine_windows"] == {
        "commute": list(range(7, 9)), "lunch": list(range(12, 13)), "after_work": list(range(18, 20)),
    }
    assert ko["peak_hours"] == list(range(21, 24))
    assert ko["dead_hours"] == list(range(2, 6))

    zh = scg.REGION_PROFILES["zh-CN"]
    assert zh["dead_hours"] == [0, 1, 2, 3, 4, 5]
    assert zh["morning_hours"] == [6, 7, 8]
    assert zh["work_hours"] == list(range(9, 19))
    assert zh["peak_hours"] == [19, 20, 21, 22]
    assert zh["night_hours"] == [23]
    assert zh["activity_multipliers"] == {"dead": 0.05, "morning": 0.4, "work": 0.7, "peak": 1.5, "night": 0.5}

    en = scg.REGION_PROFILES["en-US"]
    assert en["timezone"] == "local" and en["routine_windows"] == {}
    for profile in scg.REGION_PROFILES.values():
        for hours in [profile["dead_hours"], profile["peak_hours"], *profile["rule_active_hours"].values()]:
            assert all(0 <= h <= 23 for h in hours)


@pytest.mark.parametrize("locale,region", [("ko", "ko-KR"), ("zh", "zh-CN"), ("en", "en-US"), ("fr", "en-US")])
def test_region_selected_from_current_locale(locale, region):
    set_locale(locale)
    try:
        assert scg.select_region() == region
    finally:
        set_locale("en")


def test_generate_config_routes_through_seam_with_caps_and_cache_friendly_prompts(llm_env):
    params, seam = _generate(llm_env, "ko", n=20)  # 2 agent batches

    assert seam.calls and all(c["stage"] == "config" for c in seam.calls)
    assert all(isinstance(c["max_tokens"], int) and c["max_tokens"] > 0 for c in seam.calls)
    set_locale("ko")
    instruction = get_language_instruction()
    systems = [c["messages"][0]["content"] for c in seam.calls]
    assert all(s.endswith(instruction) for s in systems)
    agent_systems = systems[2:]
    assert len(agent_systems) == 2 and agent_systems[0] == agent_systems[1]
    assert "Asia/Seoul" in seam.calls[0]["messages"][1]["content"]
    assert "OpenAI(" not in MODULE_PATH.read_text(encoding="utf-8")

    time_config = params.to_dict()["time_config"]
    assert time_config["region"] == "ko-KR" and time_config["timezone"] == "Asia/Seoul"
    assert time_config["routine_windows"]["commute"] == [7, 8]


def test_default_time_config_uses_region_when_llm_fails(llm_env):
    params, _ = _generate(llm_env, "ko", n=4, seam=FakeSeam(fail=True))
    tc = params.time_config
    assert tc.peak_hours == [21, 22, 23]
    assert tc.off_peak_hours == [2, 3, 4, 5]
    assert tc.region == "ko-KR"

    params_zh, _ = _generate(llm_env, "zh", n=4, seam=FakeSeam(fail=True))
    assert params_zh.time_config.peak_hours == [19, 20, 21, 22]
    assert params_zh.time_config.off_peak_hours == [0, 1, 2, 3, 4, 5]
    assert params_zh.time_config.work_hours == list(range(9, 19))


def test_agent_llm_profiles_and_summary(llm_env):
    params, _ = _generate(llm_env, "en", n=9, sim_id="sim_1")
    data = params.to_dict()
    profiles = [a["llm_profile"] for a in data["agent_configs"]]
    assert set(profiles) <= {"pa", "pb"} and None not in profiles
    assert sum(v["count"] for v in data["llm_models"].values()) == len(data["agent_configs"]) == 9
    assert {pid: v["model"] for pid, v in data["llm_models"].items()} == {
        pid: {"pa": "model-a", "pb": "model-b"}[pid] for pid in data["llm_models"]
    }
    assert sorted(v["count"] for v in data["llm_models"].values()) == [4, 5]
    assert data["llm_model"] and "llm_base_url" in data

    again, _ = _generate(llm_env, "en", n=9, sim_id="sim_1")
    assert [a.llm_profile for a in again.agent_configs] == profiles


def test_archetype_overrides_and_persona_mix(llm_env):
    mix = {"preset": "ko_everyday", "seed": 42}
    params, _ = _generate(llm_env, "ko", n=12, persona_mix=mix)
    data = params.to_dict()
    assert data["persona_mix"] == mix

    expected = assign_archetypes(list(range(12)), "ko_everyday", 42)
    for agent in data["agent_configs"]:
        archetype_id = expected[agent["agent_id"]]
        assert agent["archetype"] == archetype_id
        overrides = archetype_activity_overrides(archetype_id)
        assert agent["activity_level"] == overrides["activity_level"]
        assert agent["active_hours"] == list(overrides["active_hours"])
        assert [agent["response_delay_min"], agent["response_delay_max"]] == list(overrides["response_delay_minutes"])
        assert agent["influence_weight"] == 1.7  # entity reach stays from the LLM config


def test_persona_mix_off_by_default(llm_env):
    params, _ = _generate(llm_env, "en", n=3, sim_id="sim_off")
    data = params.to_dict()
    assert data["persona_mix"]["preset"] == "off" and isinstance(data["persona_mix"]["seed"], int)
    assert all(a["archetype"] is None for a in data["agent_configs"])
    assert all(a["activity_level"] == 0.33 for a in data["agent_configs"])


def test_unknown_persona_preset_fails_before_llm_calls(llm_env):
    seam = FakeSeam()
    with pytest.raises(ValueError):
        _generate(llm_env, "en", n=3, persona_mix={"preset": "nope", "seed": 1}, seam=seam)
    assert seam.calls == []
