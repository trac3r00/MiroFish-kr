import pytest

from app import create_app
from app.config import Config
from app.graph_local import LocalZepClient
from app.llm import profiles
from app.services.graph_builder import GraphBuilderService
from app.services.zep_entity_reader import ZepEntityReader
from app.services.zep_tools import ZepToolsService
from app.utils import zep


def test_local_is_selected_without_key_and_validation_passes(monkeypatch, tmp_path):
    monkeypatch.delenv("ZEP_API_KEY", raising=False)
    monkeypatch.delenv("GRAPH_BACKEND", raising=False)
    monkeypatch.setattr(Config, "ZEP_API_KEY", None)
    monkeypatch.setenv("GRAPH_LOCAL_DB", str(tmp_path / "graphs.db"))
    monkeypatch.setenv("LLM_API_KEY", "test")
    zep.clear_zep_client_cache()
    try:
        assert isinstance(zep.get_zep_client(), LocalZepClient)
        assert not any("Zep" in error or "zep" in error for error in Config.validate())
    finally:
        zep.clear_zep_client_cache()


def test_key_defaults_to_cloud_client_without_network(monkeypatch):
    class Cloud:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    monkeypatch.setenv("ZEP_API_KEY", "test-key")
    monkeypatch.setattr(Config, "ZEP_API_KEY", "test-key")
    monkeypatch.delenv("GRAPH_BACKEND", raising=False)
    monkeypatch.setattr(zep, "Zep", Cloud)
    zep.clear_zep_client_cache()
    try:
        client = zep.get_zep_client()
        assert isinstance(client, Cloud)
        assert client.kwargs["api_key"] == "test-key"
    finally:
        zep.clear_zep_client_cache()


def test_invalid_backend_is_reported(monkeypatch):
    monkeypatch.setenv("GRAPH_BACKEND", "bogus")
    monkeypatch.setenv("LLM_API_KEY", "test")
    errors = Config.validate()
    assert any("config.graph_backend_invalid" in error for error in errors)
    with pytest.raises(ValueError, match="GRAPH_BACKEND"):
        zep.get_zep_client()


def test_graph_extract_is_routable():
    settings = profiles.parse_settings({
        "LLM_API_KEY": "test",
        "LLM_ROUTES": '{"graph_extract":"default"}',
    })
    assert settings.routes["graph_extract"] == "default"


def test_status_includes_local_backend_details(monkeypatch, tmp_path):
    monkeypatch.setenv("GRAPH_BACKEND", "local")
    monkeypatch.setenv("GRAPH_LOCAL_DB", str(tmp_path / "private-name.db"))
    monkeypatch.setenv("GRAPH_LOCAL_EMBEDDINGS", "off")
    client = create_app().test_client()
    response = client.get("/api/llm/status")
    assert response.status_code == 200
    payload = response.get_json()["data"]["graph_backend"]
    assert payload == {
        "kind": "local",
        "embeddings": "off",
        "db_path": "private-name.db",
    }


def test_graph_builder_reader_and_search_use_selected_local_backend(monkeypatch, tmp_path):
    from app import llm

    monkeypatch.setenv("GRAPH_BACKEND", "local")
    monkeypatch.setenv("GRAPH_LOCAL_DB", str(tmp_path / "pipeline.db"))
    monkeypatch.setenv("GRAPH_LOCAL_EMBEDDINGS", "off")
    monkeypatch.setattr(
        llm, "chat_json",
        lambda *_args, **_kwargs: {
            "entities": [{"name": "한빛시", "type": "City", "summary": "도시"}],
            "edges": [],
        },
    )
    builder = GraphBuilderService(api_key="local-test-key")
    graph_id = builder.create_graph("local pipeline", graph_id="local_pipeline")
    builder.set_ontology(graph_id, {
        "entity_types": [{"name": "City", "attributes": []}],
        "edge_types": [],
    })
    submission = builder.add_text_batches(graph_id, ["한빛시의 정책"])
    assert builder._wait_for_batch(submission, timeout=5)
    reader = ZepEntityReader(api_key="local-test-key")
    assert [entity.name for entity in reader.filter_defined_entities(graph_id).entities] == ["한빛시"]
    tools = ZepToolsService(api_key="local-test-key")
    assert tools.search_graph(graph_id, "한빛시", scope="nodes").nodes
    local = builder.client
    assert local.worker.wait_idle(timeout=5)
    local.store.close()
    zep.clear_zep_client_cache()
