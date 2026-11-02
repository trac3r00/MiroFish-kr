from types import SimpleNamespace

from app import create_app
from app.api import simulation as simulation_api
from app.models.task import TaskManager, TaskStatus


def test_prepare_reattaches_to_a_running_task_instead_of_starting_another(monkeypatch):
    state = SimpleNamespace(
        simulation_id="sim-reload", project_id="proj-1", graph_id="g-1",
        entities_count=8, entity_types=["Person"],
    )
    monkeypatch.setattr(
        simulation_api, "SimulationManager",
        lambda: SimpleNamespace(get_simulation=lambda _id: state),
    )
    monkeypatch.setattr(
        simulation_api, "_check_simulation_prepared", lambda _id: (False, {})
    )
    tasks = TaskManager()
    running = tasks.create_task("simulation_prepare", metadata={"simulation_id": "sim-reload"})
    tasks.update_task(running, status=TaskStatus.PROCESSING)
    finished = tasks.create_task("simulation_prepare", metadata={"simulation_id": "sim-other"})
    tasks.update_task(finished, status=TaskStatus.PROCESSING)
    before = len(tasks.list_tasks(task_type="simulation_prepare"))

    client = create_app().test_client()
    body = client.post("/api/simulation/prepare", json={"simulation_id": "sim-reload"}).get_json()

    assert body["data"]["task_id"] == running
    assert body["data"]["already_running"] is True
    assert len(tasks.list_tasks(task_type="simulation_prepare")) == before
