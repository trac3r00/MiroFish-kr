import io
import threading

from app import create_app
from app import llm
from app.models.project import ProjectManager, ProjectStatus
from app.models.task import TaskManager, TaskStatus
from app.services.graph_builder import GraphBuilderService
from app.services.text_processor import TextProcessor
from app.utils import zep


def test_local_mode_api_pipeline_without_zep_key(tmp_path, monkeypatch):
    monkeypatch.delenv("ZEP_API_KEY", raising=False)
    monkeypatch.setattr(zep.Config, "ZEP_API_KEY", None)
    monkeypatch.setenv("GRAPH_BACKEND", "local")
    monkeypatch.setenv("GRAPH_LOCAL_DB", str(tmp_path / "api-flow.db"))
    monkeypatch.setenv("GRAPH_LOCAL_EMBEDDINGS", "off")
    monkeypatch.setattr(ProjectManager, "PROJECTS_DIR", str(tmp_path / "projects"))
    # A configured LLM profile is still required; its calls are faked below (no network).
    for name in ("LLM_PROFILES", "LLM_ROUTES", "LLM_AGENT_POOL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LLM_API_KEY", "test-dummy-key")
    # Unroutable base URL: any call that escapes the fakes fails locally instead of reaching a provider.
    monkeypatch.setenv("LLM_BASE_URL", "http://127.0.0.1:9/v1")
    zep.clear_zep_client_cache()
    monkeypatch.setattr(TextProcessor, "split_text", staticmethod(lambda text, *_a, **_k: [text]))

    ontology = {
        "entity_types": [
            {"name": "Organization", "attributes": []},
            {"name": "Person", "attributes": []},
        ],
        "edge_types": [
            {
                "name": "WORKS_WITH",
                "source_targets": [{"source": "Person", "target": "Organization"}],
                "attributes": [],
            }
        ],
        "analysis_summary": "지역 정책 논의",
    }
    graph_extract = {
        "entities": [
            {"name": "한빛시청", "type": "Organization", "summary": "지역 행정기관"},
            {"name": "김민수", "type": "Person", "summary": "주민"},
        ],
        "edges": [
            {
                "source": "김민수",
                "target": "한빛시청",
                "name": "WORKS_WITH",
                "fact": "김민수는 한빛시 정책에 의견을 냈다.",
            }
        ],
    }

    def fake_chat_json(messages, *, stage, **_kwargs):
        if stage == "ontology":
            return ontology
        if stage == "graph_extract":
            return graph_extract
        raise AssertionError(f"Unexpected chat_json stage: {stage}")

    monkeypatch.setattr(llm, "chat_json", fake_chat_json)
    # Services such as OntologyGenerator go through LLMClient, which binds the seam at import time.
    from app.utils.llm_client import LLMClient
    monkeypatch.setattr(
        LLMClient, "chat_json", lambda self, messages, **_kw: fake_chat_json(messages, stage=self.stage)
    )

    app = create_app()
    app.config.update(TESTING=True)
    client = app.test_client()
    response = client.post(
        "/api/graph/ontology/generate",
        data={
            "simulation_requirement": "지역 정책에 대한 주민 반응을 살펴봅니다.",
            "project_name": "지역 정책",
            "files": (io.BytesIO("한빛시청은 새 정책을 발표했다.".encode()), "source.txt"),
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 200, response.get_json()
    project_id = response.get_json()["data"]["project_id"]

    task_manager = TaskManager()
    task_id_holder = {"value": None}
    completed = threading.Event()
    original_update = task_manager.update_task
    original_create = task_manager.create_task

    def create_and_subscribe(task_type, metadata=None):
        task_id = original_create(task_type, metadata)
        # The build API passes a localized task name as task_type; this test creates exactly one task.
        task_id_holder["value"] = task_id
        return task_id

    def update_and_signal(updated_id, **fields):
        original_update(updated_id, **fields)
        if updated_id == task_id_holder["value"] and fields.get("status") in {
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
        }:
            completed.set()

    monkeypatch.setattr(task_manager, "create_task", create_and_subscribe)
    monkeypatch.setattr(task_manager, "update_task", update_and_signal)
    response = client.post("/api/graph/build", json={"project_id": project_id})
    assert response.status_code == 200, response.get_json()
    task_id = response.get_json()["data"]["task_id"]
    task = task_manager.get_task(task_id)
    assert task is not None
    if task.status not in {TaskStatus.COMPLETED, TaskStatus.FAILED}:
        assert completed.wait(timeout=10)
    task = task_manager.get_task(task_id)
    assert task is not None
    assert task.status == TaskStatus.COMPLETED, task.error

    project = ProjectManager.get_project(project_id)
    assert project is not None
    assert project.status == ProjectStatus.GRAPH_COMPLETED
    assert project.graph_id

    response = client.get(f"/api/graph/task/{task_id}")
    assert response.status_code == 200
    assert response.get_json()["data"]["status"] == TaskStatus.COMPLETED.value

    response = client.get(f"/api/graph/data/{project.graph_id}")
    assert response.status_code == 200, response.get_json()
    graph_data = response.get_json()["data"]
    assert graph_data["nodes"]
    assert graph_data["edges"]

    response = client.get(f"/api/simulation/entities/{project.graph_id}?enrich=false")
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["data"]["entities"]

    local_client = GraphBuilderService().client
    local_client.worker.wait_idle(timeout=5)
    local_client.store.close()
    zep.clear_zep_client_cache()
