from app.graph_local import LocalZepClient
from app.services.graph_builder import GraphBuilderService
from app.services.oasis_profile_generator import OasisProfileGenerator
from app.services.zep_entity_reader import ZepEntityReader
from app.services.zep_graph_memory_updater import ZepGraphMemoryUpdater
from app.services.zep_tools import ZepToolsService
from app.utils import zep
from app.utils.llm_client import LLMClient


class _LLMClient:
    api_key = None
    base_url = None
    model = None
    client = None


def _clear_graph_config(monkeypatch):
    monkeypatch.delenv("ZEP_API_KEY", raising=False)
    monkeypatch.delenv("GRAPH_BACKEND", raising=False)
    monkeypatch.delenv("ZEP_API_URL", raising=False)
    monkeypatch.setattr(zep.Config, "ZEP_API_KEY", "")
    zep.clear_zep_client_cache()


def test_graph_backend_readiness_defaults_to_local_without_key(monkeypatch):
    _clear_graph_config(monkeypatch)

    assert zep.graph_backend_ready()
    assert zep.graph_backend_error() is None


def test_graph_backend_readiness_requires_cloud_key(monkeypatch):
    _clear_graph_config(monkeypatch)
    monkeypatch.setenv("GRAPH_BACKEND", "zep_cloud")

    assert not zep.graph_backend_ready()
    assert zep.graph_backend_error()


def test_services_construct_with_local_client_without_key(monkeypatch, tmp_path):
    _clear_graph_config(monkeypatch)
    monkeypatch.setenv("GRAPH_LOCAL_DB_PATH", str(tmp_path / "graph.db"))
    monkeypatch.setattr(LLMClient, "__init__", lambda self, *args, **kwargs: self.__dict__.update({
        "api_key": None, "base_url": None, "model": None, "client": None
    }))

    services = [
        (GraphBuilderService(), "client"),
        (ZepEntityReader(), "client"),
        (ZepGraphMemoryUpdater("graph-id"), "client"),
        (OasisProfileGenerator(), "zep_client"),
        (ZepToolsService(), "client"),
    ]

    assert all(isinstance(getattr(service, attribute), LocalZepClient)
               for service, attribute in services)


def test_graph_services_construct_cloud_clients_with_key(monkeypatch):
    _clear_graph_config(monkeypatch)
    monkeypatch.setenv("GRAPH_BACKEND", "zep_cloud")
    monkeypatch.setenv("ZEP_API_KEY", "test-key")
    monkeypatch.setattr(zep.Config, "ZEP_API_KEY", "test-key")
    monkeypatch.setattr(LLMClient, "__init__", lambda self, *args, **kwargs: self.__dict__.update({
        "api_key": None, "base_url": None, "model": None, "client": None
    }))

    services = [
        (GraphBuilderService(), "client"),
        (ZepEntityReader(), "client"),
        (ZepGraphMemoryUpdater("graph-id"), "client"),
        (OasisProfileGenerator(), "zep_client"),
        (ZepToolsService(), "client"),
    ]

    assert all(getattr(service, attribute).__class__.__name__ == "Zep"
               for service, attribute in services)


def test_get_zep_client_local_mode_and_translated_cloud_error(monkeypatch):
    _clear_graph_config(monkeypatch)
    assert isinstance(zep.get_zep_client(), LocalZepClient)

    monkeypatch.setenv("GRAPH_BACKEND", "zep_cloud")
    try:
        zep.get_zep_client()
    except ValueError as error:
        assert error.args[0] == zep.t("errors.zepApiKeyMissing")
    else:
        raise AssertionError("Cloud client construction should require a key")
