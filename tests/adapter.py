import asyncio
import builtins
import os
import sys
from typing import Any, Dict

project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

builtins.input = lambda prompt="": ""


def invoke_agent(query: str, task_id: str = None) -> Dict[str, Any]:
    try:
        from core.graph import app
        from core.memory import activate_session, get_current_km, reset_active_session
    except ImportError as exc:
        raise ImportError(f"Failed to import graph or memory: {exc}")

    token = activate_session(task_id or "test-session")
    try:
        inputs = {
            "query": query,
            "task_id": task_id or "test-session",
            "iteration": 1,
            "plan": [],
            "outline": [],
            "user_feedback": "",
            "final_report": "",
            "metrics": {"tool_calls": 0, "backtracking": 0},
            "history": [],
            "conflict_detected": False,
            "conflict_count": 0,
            "missing_sources": [],
            "degraded_items": [],
        }
        result = asyncio.run(app.ainvoke(inputs))
        km = get_current_km()
        retrieval_context = [doc.page_content for doc in km.retrieve(k=5)]
        citations = list(km.seen_urls)
        return {
            "actual_output": result.get("final_report", "NO REPORT GENERATED"),
            "retrieval_context": retrieval_context,
            "citations": citations,
            "metrics": result.get("metrics", {"tool_calls": 0, "backtracking": 0}),
            "history": result.get("history", []),
        }
    finally:
        reset_active_session(token)


def invoke_agent_with_custom_context(query: str, context_str: str) -> Dict[str, Any]:
    from unittest.mock import patch

    try:
        from core.graph import app
        from core.memory import activate_session, get_current_km, reset_active_session
    except ImportError as exc:
        raise ImportError(f"Failed to import graph or memory: {exc}")

    token = activate_session("custom-context")
    try:
        km = get_current_km()
        km.add_document(context_str, "injected_context_url", "Injected LongBench Source")
        with patch("core.tools.search_client.asearch") as mock_search, patch(
            "core.tools.scrape_jina_ai"
        ) as mock_scrape:
            mock_search.return_value = {"results": []}
            mock_scrape.return_value = ""
            inputs = {
                "query": query,
                "task_id": "custom-context",
                "iteration": 1,
                "plan": [],
                "outline": [],
                "user_feedback": "",
                "final_report": "",
                "metrics": {"tool_calls": 0, "backtracking": 0},
                "history": [],
                "conflict_detected": False,
                "conflict_count": 0,
                "missing_sources": [],
                "degraded_items": [],
            }
            result = asyncio.run(app.ainvoke(inputs))
            retrieval_context = [doc.page_content for doc in km.retrieve(k=5)]
            citations = list(km.seen_urls)
            return {
                "actual_output": result.get("final_report", "NO REPORT GENERATED"),
                "retrieval_context": retrieval_context,
                "citations": citations,
            }
    finally:
        reset_active_session(token)
