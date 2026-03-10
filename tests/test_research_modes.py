import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.graph import node_deep_research
from core.memory import activate_session, cleanup_session_km, get_current_km, reset_active_session
from gateway.api import ResearchRequest
from gateway.state_store import hash_query


def _base_state(mode: str) -> dict:
    return {
        "query": "test query",
        "task_id": f"task-{mode}",
        "plan": [{"task": "test query section", "section_id": "1"}],
        "outline": [],
        "user_feedback": "",
        "iteration": 1,
        "final_report": "",
        "metrics": {"tool_calls": 0, "backtracking": 0},
        "history": [],
        "conflict_detected": False,
        "conflict_count": 0,
        "missing_sources": [],
        "degraded_items": [],
        "research_mode": mode,
        "cost_breakdown": {},
        "retrieval_metrics": {},
    }


def test_hash_query_includes_research_mode():
    assert hash_query("same question", research_mode="low") != hash_query(
        "same question",
        research_mode="high",
    )


def test_research_request_defaults_and_validation():
    req = ResearchRequest(query="hello world")
    assert req.research_mode == "medium"
    with pytest.raises(Exception):
        ResearchRequest(query="hello world", research_mode="invalid")


def test_low_mode_skips_aadd_document(monkeypatch):
    import core.graph as graph

    token = activate_session("test-low")
    km = get_current_km()
    km.clear()
    km.aadd_document = AsyncMock(side_effect=AssertionError("low mode should not call aadd_document"))

    monkeypatch.setattr(graph, "safe_ainvoke", AsyncMock(return_value=None))
    monkeypatch.setattr(
        graph.serper_client,
        "asearch",
        AsyncMock(return_value={"results": [{"url": "https://example.com/a", "title": "A"}]}),
    )
    monkeypatch.setattr(graph, "scrape_jina_ai", AsyncMock(return_value="useful content " * 50))

    try:
        result = asyncio.run(node_deep_research(_base_state("low")))
        assert result["missing_sources"] == []
        assert km.aadd_document.await_count == 0
        assert len(km.fact_blocks) >= 1
    finally:
        cleanup_session_km("test-low")
        reset_active_session(token)


def test_medium_mode_uses_extracted_chunks(monkeypatch):
    import core.graph as graph

    token = activate_session("test-medium")
    km = get_current_km()
    km.clear()
    km.aadd_document = AsyncMock(side_effect=AssertionError("medium mode should not call aadd_document"))
    km.add_extracted_chunks = MagicMock(return_value=1)
    km.add_compact_document = MagicMock(return_value=0)

    monkeypatch.setattr(graph, "safe_ainvoke", AsyncMock(return_value=None))
    monkeypatch.setattr(
        graph.serper_client,
        "asearch",
        AsyncMock(return_value={"results": [{"url": "https://example.com/a", "title": "A"}]}),
    )
    monkeypatch.setattr(graph.tavily_extract_client, "is_configured", lambda: True)
    monkeypatch.setattr(
        graph.tavily_extract_client,
        "aextract",
        AsyncMock(
            return_value={
                "results": [
                    {"url": "https://example.com/a", "title": "A", "raw_content": "chunk one\n\nchunk two"}
                ]
            }
        ),
    )

    try:
        result = asyncio.run(node_deep_research(_base_state("medium")))
        assert km.aadd_document.await_count == 0
        assert km.add_extracted_chunks.called
        assert result["retrieval_metrics"]["extract_calls"] >= 1
    finally:
        cleanup_session_km("test-medium")
        reset_active_session(token)


def test_high_mode_triggers_map_and_crawl(monkeypatch):
    import core.graph as graph

    token = activate_session("test-high")
    km = get_current_km()
    km.clear()
    km.aadd_document = AsyncMock(side_effect=AssertionError("high mode should not call aadd_document"))
    km.add_extracted_chunks = MagicMock(return_value=1)
    km.add_compact_document = MagicMock(return_value=0)

    monkeypatch.setattr(graph, "safe_ainvoke", AsyncMock(return_value=None))
    monkeypatch.setattr(
        graph.tavily_search_client,
        "asearch",
        AsyncMock(
            return_value={
                "results": [
                    {"url": "https://docs.example.com/a", "title": "A"},
                    {"url": "https://docs.example.com/b", "title": "B"},
                    {"url": "https://other.example.com/c", "title": "C"},
                ]
            }
        ),
    )
    monkeypatch.setattr(graph.tavily_map_client, "is_configured", lambda: True)
    map_mock = AsyncMock(
        return_value={
            "results": [
                "https://docs.example.com/a",
                "https://docs.example.com/b",
                "https://docs.example.com/c",
            ]
        }
    )
    crawl_mock = AsyncMock(
        return_value={
            "results": [
                {"url": "https://docs.example.com/a", "raw_content": "crawl a"},
                {"url": "https://docs.example.com/b", "raw_content": "crawl b"},
                {"url": "https://docs.example.com/c", "raw_content": "crawl c"},
            ]
        }
    )
    extract_mock = AsyncMock(
        return_value={
            "results": [
                {"url": "https://docs.example.com/a", "title": "A", "raw_content": "extract a"}
            ]
        }
    )
    monkeypatch.setattr(graph.tavily_map_client, "amap", map_mock)
    monkeypatch.setattr(graph.tavily_crawl_client, "is_configured", lambda: True)
    monkeypatch.setattr(graph.tavily_crawl_client, "acrawl", crawl_mock)
    monkeypatch.setattr(graph.tavily_extract_client, "is_configured", lambda: True)
    monkeypatch.setattr(graph.tavily_extract_client, "aextract", extract_mock)
    monkeypatch.setattr(graph, "scrape_jina_ai", AsyncMock(return_value="fallback content " * 20))

    try:
        result = asyncio.run(node_deep_research(_base_state("high")))
        assert map_mock.await_count == 1
        assert crawl_mock.await_count == 1
        assert extract_mock.await_count == 1
        assert result["retrieval_metrics"]["map_calls"] >= 1
        assert result["retrieval_metrics"]["crawl_calls"] >= 1
    finally:
        cleanup_session_km("test-high")
        reset_active_session(token)
