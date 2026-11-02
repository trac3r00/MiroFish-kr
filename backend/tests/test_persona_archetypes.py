"""Property tests for deterministic persona archetype mixes."""

import json
import random
from pathlib import Path

import pytest

from app.services.persona_archetypes import (
    _preset_archetypes,
    archetype_activity_overrides,
    archetype_prompt_block,
    assign_archetypes,
    get_archetype,
    list_presets,
)


DATA_DIR = Path(__file__).parents[1] / "app" / "data" / "archetypes"
REQUIRED_FIELDS = {
    "id",
    "label",
    "description",
    "traits_en",
    "weight",
    "activity_level",
    "active_hours",
    "response_delay_minutes",
    "sentiment_bias",
    "influence_weight",
    "repeat_question_rate",
    "block_tendency",
    "posting_style_en",
}


def test_seeded_assignment_properties_across_generated_cases():
    """Property test: assignment/count invariants for 500 seeded generated cases."""
    rng = random.Random(918273)
    attempted = 500
    executed = 0
    for case in range(attempted):
        preset = ("ko_everyday", "en_everyday", "zh_everyday", "mixed_global")[
            case % 4
        ]
        agent_ids = [f"agent-{case}-{i}" for i in range(rng.randint(0, 250))]
        seed = rng.randrange(2**32)
        assigned = assign_archetypes(agent_ids, preset, seed)
        assert assigned == assign_archetypes(agent_ids, preset, seed)
        archetypes = _preset_archetypes(preset)
        allowed = {item["id"] for item in archetypes}
        assert list(assigned) == agent_ids
        assert set(assigned.values()) <= allowed

        total_weight = sum(item["weight"] for item in archetypes)
        counts = {archetype_id: 0 for archetype_id in allowed}
        for value in assigned.values():
            counts[value] += 1
        for item in archetypes:
            expected = len(agent_ids) * item["weight"] / total_weight
            assert abs(counts[item["id"]] - expected) <= 1

        executed += 1
    assert executed == attempted
    print(f"property test cases: attempted={attempted}, executed={executed}")


def test_seeded_assignment_matches_independent_largest_remainder_oracle():
    """Property test: seeded shuffling preserves independent oracle allocations."""
    rng = random.Random(44102)
    attempted = 500
    executed = 0
    generated_counts = [0, 0, 0, 0]
    for case in range(attempted):
        preset_index = case % 4
        generated_counts[preset_index] += 1
        preset = ("ko_everyday", "en_everyday", "zh_everyday", "mixed_global")[preset_index]
        archetypes = _preset_archetypes(preset)
        agent_ids = list(range(rng.randint(0, 300)))
        seed = rng.randrange(2**32)
        total_weight = sum(item["weight"] for item in archetypes)
        quotas = [
            len(agent_ids) * item["weight"] / total_weight for item in archetypes
        ]
        expected_counts = [int(quota) for quota in quotas]
        remaining = len(agent_ids) - sum(expected_counts)
        winners = sorted(
            range(len(archetypes)),
            key=lambda i: (-(quotas[i] - expected_counts[i]), i),
        )[:remaining]
        for i in winners:
            expected_counts[i] += 1
        expected = {
            item["id"]: count
            for item, count in zip(archetypes, expected_counts)
        }
        actual_values = assign_archetypes(agent_ids, preset, seed).values()
        actual = {key: 0 for key in expected}
        for archetype_id in actual_values:
            actual[archetype_id] += 1
        assert actual == expected
        executed += 1
    assert executed == attempted
    print(
        "property test cases: "
        f"attempted={attempted}, executed={executed}, "
        f"presets=ko:{generated_counts[0]},en:{generated_counts[1]},"
        f"zh:{generated_counts[2]},mixed:{generated_counts[3]}"
    )


def test_off_preset_assigns_none_and_unknown_preset_fails():
    assert assign_archetypes(["a", "b"], "off", seed=1) == {"a": None, "b": None}
    with pytest.raises(ValueError, match="Unknown persona archetype preset"):
        assign_archetypes(["a"], "unknown", seed=1)


def test_archetype_catalogs_have_required_fields_and_valid_ranges():
    for path in DATA_DIR.glob("*.json"):
        archetypes = json.loads(path.read_text(encoding="utf-8"))
        assert 8 <= len(archetypes) <= 11
        assert sum(item["weight"] for item in archetypes) == pytest.approx(1.0)
        assert len({item["id"] for item in archetypes}) == len(archetypes)
        for item in archetypes:
            assert REQUIRED_FIELDS <= item.keys()
            assert all(isinstance(item[key], str) and item[key] for key in (
                "id", "label", "description", "traits_en", "posting_style_en"
            ))
            assert 0 <= item["weight"] <= 1
            assert 0 <= item["activity_level"] <= 1
            assert set(item["active_hours"]) <= set(range(24))
            delay = item["response_delay_minutes"]
            assert len(delay) == 2 and 0 <= delay[0] <= delay[1]
            assert -1 <= item["sentiment_bias"] <= 1
            assert 0 <= item["influence_weight"] <= 3
            assert 0 <= item["repeat_question_rate"] <= 1
            assert 0 <= item["block_tendency"] <= 1


def test_korean_catalog_contains_required_everyday_archetypes():
    labels = {item["label"] for item in json.loads((DATA_DIR / "ko.json").read_text())}
    required = {
        "계속 묻는 디지털 소외 어르신",
        "동네 아줌마·맘카페 네트워커",
        "길막형 수동 방관자",
        "단톡방 정보 전달자",
        "악플러·어그로꾼",
        "눈팅족",
        "커뮤니티 고인물",
        "바쁜 직장인",
        "취준생·대학생",
        "자영업 사장님",
        "깐깐한 팩트체커",
    }
    assert required <= labels


def test_public_catalog_prompt_and_behavior_helpers():
    assert [item["id"] for item in list_presets("ko")] == ["ko_everyday"]
    assert {item["id"] for item in list_presets()} == {
        "ko_everyday", "en_everyday", "zh_everyday", "mixed_global", "off"
    }
    archetype = get_archetype("passive_blocker")
    assert "English" in archetype_prompt_block(archetype["id"], "English")
    assert archetype_activity_overrides(archetype["id"]) == {
        key: archetype[key]
        for key in (
            "activity_level", "active_hours", "response_delay_minutes",
            "sentiment_bias", "influence_weight"
        )
    }
