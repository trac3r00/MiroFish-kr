import re

import pytest

from app.services import ontology_generator
from app.utils.locale import get_language_instruction, set_locale


HAN = re.compile(r"[\u3400-\u9fff]")


class RecordingLLMClient:
    instances = []

    def __init__(self, **kwargs):
        self.init_kwargs = kwargs
        self.calls = []
        self.instances.append(self)

    def chat_json(self, **kwargs):
        self.calls.append(kwargs)
        return {"entity_types": [], "edge_types": [], "analysis_summary": "ok"}


@pytest.mark.parametrize("locale", ["ko", "en", "zh"])
def test_ontology_prompt_is_neutral_stable_and_locale_instruction_is_last(monkeypatch, locale):
    RecordingLLMClient.instances.clear()
    monkeypatch.setattr(ontology_generator, "LLMClient", RecordingLLMClient)
    set_locale(locale)
    try:
        generator = ontology_generator.OntologyGenerator()
        assert generator.llm_client.init_kwargs["stage"] == "ontology"

        for _ in range(2):
            generator.generate(["A short source document."], "Simulate the public discussion.")

        prompts = [call["messages"][0]["content"] for call in generator.llm_client.calls]
        language_instruction = get_language_instruction()
        assert prompts[0].endswith(language_instruction)
        assert prompts[0] == prompts[1]
        static_prompt = prompts[0][: -len(language_instruction)].rstrip()
        assert not HAN.search(static_prompt)
        assert static_prompt == ontology_generator.ONTOLOGY_SYSTEM_PROMPT
    finally:
        set_locale("en")
