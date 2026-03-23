import asyncio
import os
import sys

if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

os.environ["FACTWEAVER_API_MODE"] = "1"

PASS = 0
FAIL = 0


def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} :: {detail}")


async def run_all_checks():
    global PASS, FAIL

    print("\n[1/7] config.py")
    try:
        from core.config import (
            CHECKPOINT_DB_PATH,
            MAX_TASK_DURATION_SECONDS,
            REDIS_BROKER_URL,
            REDIS_RESULT_BACKEND,
            STATE_DB_PATH,
        )

        check("Redis broker configured", REDIS_BROKER_URL.startswith("redis://"), REDIS_BROKER_URL)
        check("Redis result backend configured", REDIS_RESULT_BACKEND.startswith("redis://"), REDIS_RESULT_BACKEND)
        check("State DB path configured", bool(STATE_DB_PATH), STATE_DB_PATH)
        check("Checkpoint DB path configured", bool(CHECKPOINT_DB_PATH), CHECKPOINT_DB_PATH)
        check("Circuit breaker configured", MAX_TASK_DURATION_SECONDS > 0, str(MAX_TASK_DURATION_SECONDS))
    except Exception as exc:
        check("config import", False, str(exc))

    print("\n[2/7] memory.py")
    try:
        from core.memory import (
            activate_session,
            cleanup_session_km,
            get_current_km,
            get_session_km,
            reset_active_session,
        )

        km_a = get_session_km("A")
        km_b = get_session_km("B")
        check("Session KM isolation", km_a is not km_b)
        token = activate_session("A")
        try:
            check("Current KM follows active session", get_current_km() is km_a)
        finally:
            reset_active_session(token)
        cleanup_session_km("A")
        cleanup_session_km("B")
        check("Session cleanup callable", True)
    except Exception as exc:
        check("memory import", False, str(exc))

    print("\n[3/7] checkpoint.py")
    try:
        from core.checkpoint import SQLiteBackedMemorySaver
        from core.config import CHECKPOINT_DB_PATH

        saver = SQLiteBackedMemorySaver(CHECKPOINT_DB_PATH)
        check("SQLiteBackedMemorySaver init", saver is not None)
        check("Checkpoint file exists", os.path.exists(CHECKPOINT_DB_PATH), CHECKPOINT_DB_PATH)
    except Exception as exc:
        check("checkpoint import", False, str(exc))

    print("\n[4/7] tools.py")
    try:
        import inspect
        from core.tools import clean_json_output, heuristic_dom_probe, scrape_jina_ai, search_client, search_provider_name

        check("Search client sync search", hasattr(search_client, "search"))
        check("Search client async search", hasattr(search_client, "asearch"))
        check("Search provider resolved", bool(search_provider_name), search_provider_name)
        check("scrape_jina_ai async", inspect.iscoroutinefunction(scrape_jina_ai))
        check("JSON repair basic", clean_json_output('```json\n{\"a\":1,}\n```') == {"a": 1})
        check("JSON repair fallback", clean_json_output("{'a': 1}") == {"a": 1})
        check(
            "DOM probe visual hint",
            heuristic_dom_probe("<body><svg></svg><svg></svg><svg></svg><svg></svg></body>", "x")
            is not None,
        )
    except Exception as exc:
        check("tools import", False, str(exc))

    print("\n[5/7] graph.py / writer_graph.py")
    try:
        import inspect
        from core.graph import app, node_init_search, router_conflict
        from core.writer_graph import continue_to_writers, writer_app

        check("Graph compiled", app is not None)
        check("Writer graph compiled", writer_app is not None)
        check("Planner node async", inspect.iscoroutinefunction(node_init_search))
        check("Conflict router", router_conflict({"conflict_detected": True, "conflict_count": 1}) == "planner")
        writer_route = continue_to_writers(
            {
                "outline": [{"id": "1", "title": "Section", "description": "desc"}],
                "iteration": 0,
                "user_feedback": "",
                "task_contract": {"must_answer_points": []},
                "required_analysis_modes": [],
            }
        )
        check("Writer router", isinstance(writer_route, list) and bool(writer_route) and writer_route[0].get("node") == "section_writer")
    except Exception as exc:
        check("graph import", False, str(exc))

    print("\n[6/7] gateway")
    try:
        from gateway.api import ResearchRequest, app as fastapi_app
        from gateway.celery_app import CELERY_AVAILABLE, celery
        from gateway.outbox import publish_outbox_once, queue_publish_request
        from gateway.state_store import get_outbox_stats, get_task, hash_query, list_dlq

        routes = [route.path for route in fastapi_app.routes if hasattr(route, "path")]
        check("FastAPI app", fastapi_app is not None)
        check("POST /research", "/research" in routes)
        check("GET /research/{task_id}", "/research/{task_id}" in routes)
        check("GET /dlq", "/dlq" in routes)
        check("Celery compatibility layer", celery is not None, str(CELERY_AVAILABLE))
        check("DLQ list callable", isinstance(list_dlq(limit=1), list))
        check("Task lookup callable", get_task("missing") is None)
        check("Outbox stats callable", isinstance(get_outbox_stats(), dict))
        check("Outbox queue helper", callable(queue_publish_request))
        check("Outbox publish_once", callable(publish_outbox_once))
        req = ResearchRequest(query="hello")
        check("Research mode default", req.research_mode == "medium", req.research_mode)
        check(
            "Cache key includes mode",
            hash_query("same", research_mode="low") != hash_query("same", research_mode="high"),
        )
    except Exception as exc:
        check("gateway import", False, str(exc))

    print("\n[7/7] tests smoke")
    try:
        from core.tools import LLMFormatError, clean_json_output

        raised = False
        try:
            clean_json_output("not json", strict=True)
        except LLMFormatError:
            raised = True
        check("strict JSON raises", raised)
    except Exception as exc:
        check("test smoke", False, str(exc))

    total = PASS + FAIL
    print(f"\nSummary: {PASS}/{total} passed, {FAIL} failed")
    return FAIL == 0


if __name__ == "__main__":
    ok = asyncio.run(run_all_checks())
    sys.exit(0 if ok else 1)
