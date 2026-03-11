from core.memory import KnowledgeManager
from gateway.api import app
import gateway.api as api_module
from gateway.state_store import get_latest_knowledge_snapshot, init_state_store, save_knowledge_snapshot
from fastapi.testclient import TestClient


def test_knowledge_manager_snapshot_roundtrip():
    km = KnowledgeManager(session_id="snapshot-test")
    km.add_compact_document("hello world " * 20, "https://example.com/a", "A", section_id="1")
    snapshot = km.snapshot()

    restored = KnowledgeManager(session_id="other")
    restored.restore(snapshot)

    assert restored.session_id == "snapshot-test"
    assert "https://example.com/a" in restored.seen_urls
    assert len(restored.fact_blocks) == 1
    assert restored.fact_blocks[0].metadata["section_id"] == "1"


def test_save_and_load_knowledge_snapshot_roundtrip():
    init_state_store()
    snapshot_id = save_knowledge_snapshot(
        "task-snapshot",
        thread_id="task-snapshot",
        checkpoint_id="cp-1",
        checkpoint_ns="",
        checkpoint_node="executor",
        snapshot={"seen_urls": ["https://example.com"], "fact_blocks": []},
    )
    loaded = get_latest_knowledge_snapshot("task-snapshot")
    assert snapshot_id >= 1
    assert loaded is not None
    assert loaded["checkpoint_id"] == "cp-1"
    assert loaded["checkpoint_node"] == "executor"
    assert loaded["snapshot"]["seen_urls"] == ["https://example.com"]


def test_resume_endpoint_requires_checkpoint(monkeypatch):
    monkeypatch.setattr(
        api_module,
        "get_task",
        lambda task_id: {
            "task_id": task_id,
            "query": "resume me",
            "research_mode": "medium",
            "status": "FAILED",
            "last_checkpoint_id": None,
        },
    )
    client = TestClient(app)
    response = client.post("/research/task-no-checkpoint/resume")
    assert response.status_code == 409


def test_resume_endpoint_accepts_resumable_task(monkeypatch):
    captured = {}

    def fake_upsert_task(task_id, query, status, **kwargs):
        captured["task_id"] = task_id
        captured["status"] = status
        captured["kwargs"] = kwargs

    monkeypatch.setattr(
        api_module,
        "get_task",
        lambda task_id: {
            "task_id": task_id,
            "query": "resume me",
            "research_mode": "medium",
            "status": "INTERRUPTED",
            "last_checkpoint_id": "cp-123",
            "last_checkpoint_ns": "",
            "last_checkpoint_node": "executor",
            "llm_cost_rmb": 0.5,
            "external_cost_usd_est": 0.1,
            "serper_queries": 2,
            "serper_cost_usd_est": 0.02,
            "tavily_credits_est": 4,
            "tavily_cost_usd_est": 0.08,
            "elapsed_seconds": 12.0,
            "attempt_count": 1,
            "resume_count": 0,
            "started_at": 100,
            "last_km_snapshot_id": 11,
        },
    )
    monkeypatch.setattr(api_module, "CELERY_AVAILABLE", False)
    monkeypatch.setattr(api_module, "upsert_task", fake_upsert_task)
    monkeypatch.setattr(api_module, "_run_local_task", lambda *args, **kwargs: None)

    client = TestClient(app)
    response = client.post("/research/task-1/resume")
    assert response.status_code == 200
    assert captured["task_id"] == "task-1"
    assert captured["status"] == "PENDING"
    assert captured["kwargs"]["resumed_from_checkpoint"] is True
