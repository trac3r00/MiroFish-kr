import pytest

from app import create_app
from app.config import Config
from app.api import graph as graph_api


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(Config, "ZEP_API_KEY", "test-zep-key")
    monkeypatch.setenv("LLM_API_KEY", "test-llm-key")
    monkeypatch.setenv("LLM_BASE_URL", "https://api.example.com/v1")
    monkeypatch.setenv("LLM_MODEL_NAME", "test-model")
    for key in ("LLM_PROFILES", "LLM_ROUTES", "LLM_AGENT_POOL"):
        monkeypatch.delenv(key, raising=False)
    return create_app().test_client()


def test_graph_data_returns_etag_and_not_modified(client, monkeypatch):
    monkeypatch.setattr(
        graph_api.GraphBuilderService,
        "get_graph_data",
        lambda self, graph_id: {"nodes": [{"id": "n1"}], "edges": []},
    )

    response = client.get("/api/graph/data/test-graph")
    assert response.status_code == 200
    assert response.headers.get("ETag")
    assert response.get_json()["data"] == {
        "nodes": [{"id": "n1"}],
        "edges": [],
    }

    unchanged = client.get(
        "/api/graph/data/test-graph",
        headers={"If-None-Match": response.headers["ETag"]},
    )
    assert unchanged.status_code == 304
    assert unchanged.data == b""


def test_registered_llm_status_has_profiles(client):
    response = client.get("/api/llm/status")
    assert response.status_code == 200
    assert response.get_json()["data"]["profiles"]


@pytest.mark.parametrize("language", ["en", "ko"])
def test_representative_api_errors_are_localized(client, monkeypatch, language):
    monkeypatch.setattr(Config, "ZEP_API_KEY", None)
    graph_error = client.get(
        "/api/graph/data/test-graph",
        headers={"Accept-Language": language},
    )
    report_error = client.post(
        "/api/report/generate",
        json={},
        headers={"Accept-Language": language},
    )

    assert graph_error.status_code == 500
    assert report_error.status_code == 400
    for response in (graph_error, report_error):
        assert not any("\u3400" <= char <= "\u9fff" for char in response.get_json()["error"])
