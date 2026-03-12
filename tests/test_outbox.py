from fastapi.testclient import TestClient

import gateway.api as api_module
import gateway.state_store as state_store
from gateway.api import app


def _use_temp_state_db(monkeypatch, tmp_path):
    db_path = tmp_path / "state.sqlite3"
    monkeypatch.setattr(state_store, "STATE_DB_PATH", str(db_path))
    state_store.init_state_store()
    return db_path


def test_outbox_publish_roundtrip_updates_task(monkeypatch, tmp_path):
    _use_temp_state_db(monkeypatch, tmp_path)
    task_id = "task-outbox-success"
    state_store.upsert_task(
        task_id,
        "hello",
        "PENDING",
        backend="celery",
        thread_id=task_id,
        research_mode="medium",
        publish_status="PENDING",
    )
    outbox_id = state_store.enqueue_publish_job(
        task_id,
        queue_name="research_queue",
        task_name="tasks.run_research_task",
        payload={"task_id": task_id, "query": "hello", "research_mode": "medium"},
    )

    claimed = state_store.claim_publish_jobs(limit=1)
    assert len(claimed) == 1
    assert claimed[0]["id"] == outbox_id

    state_store.mark_publish_job_published(outbox_id)
    task = state_store.get_task(task_id)
    assert task is not None
    assert task["status"] == "QUEUED"
    assert task["publish_status"] == "PUBLISHED"
    assert float(task["queued_at"]) > 0


def test_outbox_failed_publish_tracks_attempts(monkeypatch, tmp_path):
    _use_temp_state_db(monkeypatch, tmp_path)
    task_id = "task-outbox-failed"
    state_store.upsert_task(
        task_id,
        "hello",
        "PENDING",
        backend="celery",
        thread_id=task_id,
        research_mode="medium",
        publish_status="PENDING",
    )
    outbox_id = state_store.enqueue_publish_job(
        task_id,
        queue_name="research_queue",
        task_name="tasks.run_research_task",
        payload={"task_id": task_id, "query": "hello", "research_mode": "medium"},
    )

    claimed = state_store.claim_publish_jobs(limit=1)
    assert len(claimed) == 1
    state_store.mark_publish_job_failed(outbox_id, "RuntimeError('broker down')")
    task = state_store.get_task(task_id)
    assert task is not None
    assert task["status"] == "PENDING"
    assert task["publish_status"] == "FAILED"
    assert int(task["publish_attempt_count"]) == 1
    assert "broker down" in str(task["publish_last_error"])


def test_submit_research_queues_outbox(monkeypatch):
    captured: dict[str, object] = {}

    monkeypatch.setattr(api_module, "CELERY_AVAILABLE", True)
    monkeypatch.setattr(api_module, "get_cached_report", lambda *args, **kwargs: None)

    async def _noop():
        return None

    monkeypatch.setattr(api_module, "start_outbox_publisher", _noop)
    monkeypatch.setattr(api_module, "stop_outbox_publisher", _noop)

    def fake_upsert_task(task_id, query, status, **kwargs):
        captured["task_id"] = task_id
        captured["query"] = query
        captured["status"] = status
        captured["kwargs"] = kwargs

    def fake_queue_publish_request(task_id, *, queue_name, task_name, payload):
        captured["queued"] = {
            "task_id": task_id,
            "queue_name": queue_name,
            "task_name": task_name,
            "payload": payload,
        }
        return 1

    monkeypatch.setattr(api_module, "upsert_task", fake_upsert_task)
    monkeypatch.setattr(api_module, "queue_publish_request", fake_queue_publish_request)

    with TestClient(app) as client:
        response = client.post("/research", json={"query": "hello world", "research_mode": "medium"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "PENDING"
    assert captured["status"] == "PENDING"
    assert captured["queued"]["task_name"] == "tasks.run_research_task"
