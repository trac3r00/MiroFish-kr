from app.services.ontology_generator import OntologyGenerator
from app.utils.locale import set_locale


class RecordingLLMClient:
    def __init__(self):
        self.calls = []

    def chat_json(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "entity_types": [],
            "edge_types": [],
            "analysis_summary": "ok",
        }


def _generator_for_test() -> OntologyGenerator:
    generator = OntologyGenerator(llm_client=object())
    generator.MAX_TEXT_LENGTH_FOR_LLM = 2000
    generator.LONG_TEXT_CHUNK_SIZE = 500
    generator.LONG_TEXT_CHUNK_OVERLAP = 0
    generator.MAX_LONG_TEXT_CHUNKS = 3
    generator.MIN_LONG_TEXT_EXCERPT = 120
    return generator


def test_short_ontology_context_keeps_original_text():
    generator = _generator_for_test()

    context = generator._build_document_context(["short document body"])

    assert context == "short document body"
    assert "[Long-document chunk overview]" not in context


def test_long_ontology_context_samples_across_document():
    generator = _generator_for_test()
    long_text = "BEGIN" + ("a" * 1050) + "MIDDLE" + ("b" * 1050) + "END"

    context = generator._build_document_context([long_text])

    assert len(context) <= generator.MAX_TEXT_LENGTH_FOR_LLM
    assert "[Long-document chunk overview]" in context
    assert "BEGIN" in context
    assert "MIDDLE" in context
    assert "END" in context
    assert "Chunk 1/" in context
    assert "Chunk 3/" in context
    assert "Chunk 5/" in context


def test_very_long_ontology_context_selects_representative_chunks():
    generator = _generator_for_test()
    chunks = ["BEGIN"] + [
        f"CHUNK{i:02d}-" + (str(i) * 490)
        for i in range(12)
    ] + ["FINALEND"]
    long_text = "".join(chunks)

    context = generator._build_document_context([long_text])

    assert len(context) <= generator.MAX_TEXT_LENGTH_FOR_LLM
    assert "BEGIN" in context
    assert "FINALEND" in context
    assert context.count("--- Document 1 / Chunk") == generator.MAX_LONG_TEXT_CHUNKS


def test_converted_prompt_markers_are_english_for_korean_and_english_locales():
    generator = _generator_for_test()
    text = "BEGIN" + ("a" * 1050) + "MIDDLE" + ("b" * 1050) + "END"
    rendered = {}
    try:
        for locale in ("ko", "en"):
            set_locale(locale)
            rendered[locale] = generator._build_document_context([text])
    finally:
        set_locale("en")

    for locale_text in rendered.values():
        assert "[Long-document chunk overview]" in locale_text
        assert "Chunk 1/" in locale_text
        assert "The following excerpts show" in locale_text


def test_ontology_generation_does_not_cap_structured_output_tokens():
    llm = RecordingLLMClient()
    generator = OntologyGenerator(llm_client=llm)

    result = generator.generate(
        document_texts=["A short source document."],
        simulation_requirement="Simulate the public discussion.",
    )

    assert result["analysis_summary"] == "ok"
    assert llm.calls[0]["max_tokens"] is None
    assert llm.calls[0]["max_attempts"] == 2
