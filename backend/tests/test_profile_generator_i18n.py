import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.llm.usage import usage_tracker
from app.services import oasis_profile_generator as module
from app.services.oasis_profile_generator import OasisProfileGenerator
from app.services.persona_archetypes import archetype_prompt_block
from app.services.zep_entity_reader import EntityNode
from app.utils.locale import get_language_instruction, set_locale

BIAS_PATTERN = re.compile(r"China|中国|Chinese")


class FakeCompletions:
    def __init__(self, contents):
        self.contents = list(contents)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        content = self.contents.pop(0) if len(self.contents) > 1 else self.contents[0]
        return SimpleNamespace(
            model=kwargs["model"],
            choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=content))],
            usage=None,
        )


def make_generator(contents, region=None):
    generator = OasisProfileGenerator(
        api_key="test-key", base_url="http://127.0.0.1:9/v1", model_name="test-model", region=region
    )
    completions = FakeCompletions(contents)
    generator.llm.client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    generator.zep_client = None
    return generator, completions


def entity(uuid, name, label):
    return EntityNode(uuid=uuid, name=name, labels=["Entity", label], summary=f"{name} summary", attributes={})


VALID_PERSONA = json.dumps(
    {
        "bio": "bio",
        "persona": "persona",
        "age": 40,
        "gender": "female",
        "mbti": "INTJ",
        "country": "",
        "profession": "teacher",
        "interested_topics": ["transit"],
    }
)


@pytest.fixture(autouse=True)
def reset_locale():
    yield
    set_locale("en")


def test_profile_generator_has_no_direct_openai_client_construction():
    source = Path(module.__file__).read_text(encoding="utf-8")
    assert "OpenAI(" not in source
    assert "from openai" not in source


def test_generator_uses_profile_stage_and_records_usage():
    usage_tracker.reset()
    generator, completions = make_generator([VALID_PERSONA])

    result = generator._generate_profile_with_llm("Kim", "Person", "summary", {}, "")

    assert generator.llm.stage == "profile"
    assert result["profession"] == "teacher"
    assert len(completions.calls) == 1
    assert [entry["stage"] for entry in usage_tracker.snapshot()["entries"]] == ["profile"]


@pytest.mark.parametrize("locale", ["ko", "en"])
def test_prompts_are_neutral_and_static_with_language_instruction_last(locale):
    set_locale(locale)
    generator, _ = make_generator([VALID_PERSONA])

    for is_individual in (True, False):
        system_prompt = generator._get_system_prompt(is_individual)
        assert system_prompt == generator._get_system_prompt(is_individual)
        assert system_prompt.endswith(get_language_instruction())
        assert not BIAS_PATTERN.search(system_prompt)

    user_prompt = generator._build_individual_persona_prompt(
        "Kim", "Person", "summary", {"role": "commuter"}, "context", archetype_id="digital_help_seeker"
    )
    assert not BIAS_PATTERN.search(user_prompt)


@pytest.mark.parametrize(
    ("locale", "region", "expected"),
    [
        ("ko", None, "대한민국"),
        ("en", "ko", "South Korea"),
        ("en", None, None),
        ("zh", None, "中国"),
    ],
)
def test_rule_based_country_follows_region_and_output_locale(locale, region, expected):
    set_locale(locale)
    generator, _ = make_generator([VALID_PERSONA], region=region)

    for entity_type in ("Student", "Expert", "MediaOutlet", "University", "Other"):
        profile = generator._generate_profile_rule_based("Name", entity_type, "", {})
        assert profile["country"] == expected

    user_prompt = generator._build_group_persona_prompt("Org", "University", "", {}, "")
    assert f"Default region: {expected or 'not specified'}" in user_prompt


def test_reddit_export_uses_region_default_country(tmp_path):
    set_locale("en")
    generator, _ = make_generator([VALID_PERSONA])
    profile = module.OasisAgentProfile(user_id=0, user_name="u", name="N", bio="b", persona="p")
    path = tmp_path / "reddit.json"

    generator._save_reddit_json([profile], str(path))
    assert json.loads(path.read_text(encoding="utf-8"))[0]["country"] == module.UNSPECIFIED_COUNTRY

    set_locale("ko")
    generator._save_reddit_json([profile], str(path))
    assert json.loads(path.read_text(encoding="utf-8"))[0]["country"] == "대한민국"


def test_archetype_block_is_injected_only_for_assigned_agents_and_tokens_are_capped():
    set_locale("ko")
    generator, completions = make_generator([VALID_PERSONA])
    entities = [
        entity("u-person", "Kim", "Person"),
        entity("u-org", "City Hall", "GovernmentAgency"),
        entity("u-plain", "Lee", "Student"),
    ]

    profiles = generator.generate_profiles_from_entities(
        entities,
        parallel_count=1,
        archetype_by_entity={"u-person": "digital_help_seeker", "u-org": "digital_help_seeker"},
    )

    assert [p.source_entity_uuid for p in profiles] == ["u-person", "u-org", "u-plain"]
    block = archetype_prompt_block("digital_help_seeker", "Korean")
    by_name = {
        next(line for line in call["messages"][1]["content"].splitlines() if line.startswith("Entity name:")): call
        for call in completions.calls
    }
    assert block in by_name["Entity name: Kim"]["messages"][1]["content"]
    assert block in by_name["Entity name: City Hall"]["messages"][1]["content"]
    assert "Persona archetype:" not in by_name["Entity name: Lee"]["messages"][1]["content"]
    assert by_name["Entity name: Kim"]["max_tokens"] == 1200
    assert by_name["Entity name: Lee"]["max_tokens"] == 1200
    assert by_name["Entity name: City Hall"]["max_tokens"] == 900
    system_prompts = {call["messages"][0]["content"] for call in completions.calls}
    assert len(system_prompts) == 2


def test_unusable_json_is_retried_at_most_once_then_falls_back_to_rules():
    set_locale("ko")
    generator, completions = make_generator(["not json"])

    result = generator._generate_profile_with_llm("Kim", "Person", "summary", {}, "")

    assert len(completions.calls) == module.PROFILE_MAX_ATTEMPTS == 2
    assert all(call["max_tokens"] == 1200 for call in completions.calls)
    assert result["country"] == "대한민국"


def test_transport_errors_are_retried_at_most_once(monkeypatch):
    sleeps = []
    monkeypatch.setattr(module.time, "sleep", sleeps.append)
    generator, _ = make_generator([VALID_PERSONA])
    calls = []

    def failing_create(**kwargs):
        calls.append(kwargs)
        raise RuntimeError("connection reset")

    generator.llm.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=failing_create)))

    result = generator._generate_profile_with_llm("Org", "University", "summary", {}, "")

    assert len(calls) == 2
    assert sleeps == [1]
    assert result["gender"] == "other"
