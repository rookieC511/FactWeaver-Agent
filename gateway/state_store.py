import hashlib
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from typing import Any

from core.config import DEFAULT_RESEARCH_MODE, REDIS_BROKER_URL, SEMANTIC_CACHE_TTL_SECONDS, STATE_DB_PATH


@contextmanager
def _connect():
    conn = sqlite3.connect(STATE_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _now_ts() -> int:
    return int(time.time())


def init_state_store() -> None:
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                query TEXT NOT NULL,
                status TEXT NOT NULL,
                detail TEXT,
                report TEXT,
                thread_id TEXT,
                cache_key TEXT,
                backend TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                last_error TEXT
            )
            """
        )
        _ensure_columns(
            conn,
            "tasks",
            {
                "research_mode": f"TEXT NOT NULL DEFAULT '{DEFAULT_RESEARCH_MODE}'",
                "llm_cost_rmb": "REAL DEFAULT 0",
                "external_cost_usd_est": "REAL DEFAULT 0",
                "serper_queries": "INTEGER DEFAULT 0",
                "serper_cost_usd_est": "REAL DEFAULT 0",
                "tavily_credits_est": "REAL DEFAULT 0",
                "tavily_cost_usd_est": "REAL DEFAULT 0",
                "elapsed_seconds": "REAL DEFAULT 0",
            },
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS semantic_cache (
                cache_key TEXT PRIMARY KEY,
                query TEXT NOT NULL,
                report TEXT NOT NULL,
                metadata_json TEXT,
                expires_at INTEGER NOT NULL,
                created_at INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS dlq_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                query TEXT NOT NULL,
                thread_id TEXT,
                retries INTEGER NOT NULL,
                error TEXT NOT NULL,
                payload_json TEXT,
                created_at INTEGER NOT NULL
            )
            """
        )


def _ensure_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = {
        row["name"]
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }
    for name, definition in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def normalize_query(query: str, research_mode: str = DEFAULT_RESEARCH_MODE) -> str:
    return f"{research_mode.strip().lower()}::" + " ".join((query or "").strip().lower().split())


def hash_query(query: str, research_mode: str = DEFAULT_RESEARCH_MODE) -> str:
    normalized = normalize_query(query, research_mode=research_mode)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def upsert_task(
    task_id: str,
    query: str,
    status: str,
    *,
    detail: str | None = None,
    report: str | None = None,
    thread_id: str | None = None,
    cache_key: str | None = None,
    backend: str = "celery",
    last_error: str | None = None,
    research_mode: str = DEFAULT_RESEARCH_MODE,
    llm_cost_rmb: float | None = None,
    external_cost_usd_est: float | None = None,
    serper_queries: int | None = None,
    serper_cost_usd_est: float | None = None,
    tavily_credits_est: float | None = None,
    tavily_cost_usd_est: float | None = None,
    elapsed_seconds: float | None = None,
) -> None:
    now = _now_ts()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO tasks (
                task_id, query, status, detail, report, thread_id, cache_key,
                backend, created_at, updated_at, last_error, research_mode,
                llm_cost_rmb, external_cost_usd_est, serper_queries,
                serper_cost_usd_est, tavily_credits_est, tavily_cost_usd_est,
                elapsed_seconds
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(task_id) DO UPDATE SET
                query = excluded.query,
                status = excluded.status,
                detail = COALESCE(excluded.detail, tasks.detail),
                report = COALESCE(excluded.report, tasks.report),
                thread_id = COALESCE(excluded.thread_id, tasks.thread_id),
                cache_key = COALESCE(excluded.cache_key, tasks.cache_key),
                backend = excluded.backend,
                updated_at = excluded.updated_at,
                last_error = COALESCE(excluded.last_error, tasks.last_error),
                research_mode = COALESCE(excluded.research_mode, tasks.research_mode),
                llm_cost_rmb = COALESCE(excluded.llm_cost_rmb, tasks.llm_cost_rmb),
                external_cost_usd_est = COALESCE(excluded.external_cost_usd_est, tasks.external_cost_usd_est),
                serper_queries = COALESCE(excluded.serper_queries, tasks.serper_queries),
                serper_cost_usd_est = COALESCE(excluded.serper_cost_usd_est, tasks.serper_cost_usd_est),
                tavily_credits_est = COALESCE(excluded.tavily_credits_est, tasks.tavily_credits_est),
                tavily_cost_usd_est = COALESCE(excluded.tavily_cost_usd_est, tasks.tavily_cost_usd_est),
                elapsed_seconds = COALESCE(excluded.elapsed_seconds, tasks.elapsed_seconds)
            """,
            (
                task_id,
                query,
                status,
                detail,
                report,
                thread_id,
                cache_key,
                backend,
                now,
                now,
                last_error,
                research_mode,
                llm_cost_rmb,
                external_cost_usd_est,
                serper_queries,
                serper_cost_usd_est,
                tavily_credits_est,
                tavily_cost_usd_est,
                elapsed_seconds,
            ),
        )


def get_task(task_id: str) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    return dict(row) if row else None


def list_dlq(limit: int = 50) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM dlq_records ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(row) for row in rows]


def record_dlq(
    task_id: str,
    query: str,
    *,
    thread_id: str | None,
    retries: int,
    error: str,
    payload: dict[str, Any] | None = None,
) -> None:
    now = _now_ts()
    payload_json = json.dumps(payload or {}, ensure_ascii=False)
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO dlq_records (task_id, query, thread_id, retries, error, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (task_id, query, thread_id, retries, error, payload_json, now),
        )
    _push_dlq_to_redis(
        {
            "task_id": task_id,
            "query": query,
            "thread_id": thread_id,
            "retries": retries,
            "error": error,
            "payload": payload or {},
            "created_at": now,
        }
    )


def _redis_client():
    try:
        import redis  # type: ignore
    except Exception:
        return None
    try:
        return redis.from_url(REDIS_BROKER_URL, decode_responses=True)
    except Exception:
        return None


def _push_dlq_to_redis(payload: dict[str, Any]) -> None:
    client = _redis_client()
    if client is None:
        return
    try:
        client.lpush("factweaver:dlq", json.dumps(payload, ensure_ascii=False))
    except Exception:
        pass


def get_cached_report(query: str, research_mode: str = DEFAULT_RESEARCH_MODE) -> dict[str, Any] | None:
    cache_key = hash_query(query, research_mode=research_mode)
    client = _redis_client()
    if client is not None:
        try:
            raw = client.get(f"factweaver:cache:{cache_key}")
            if raw:
                return json.loads(raw)
        except Exception:
            pass

    now = _now_ts()
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT cache_key, query, report, metadata_json, expires_at, created_at
            FROM semantic_cache
            WHERE cache_key = ? AND expires_at > ?
            """,
            (cache_key, now),
        ).fetchone()
    if not row:
        return None

    data = dict(row)
    metadata_json = data.get("metadata_json")
    data["metadata"] = json.loads(metadata_json) if metadata_json else {}
    return data


def cache_report(
    query: str,
    report: str,
    *,
    research_mode: str = DEFAULT_RESEARCH_MODE,
    metadata: dict[str, Any] | None = None,
    ttl_seconds: int = SEMANTIC_CACHE_TTL_SECONDS,
) -> str:
    cache_key = hash_query(query, research_mode=research_mode)
    now = _now_ts()
    expires_at = now + ttl_seconds
    payload = {
        "cache_key": cache_key,
        "query": query,
        "research_mode": research_mode,
        "report": report,
        "metadata": metadata or {},
        "created_at": now,
        "expires_at": expires_at,
    }

    client = _redis_client()
    if client is not None:
        try:
            client.setex(
                f"factweaver:cache:{cache_key}",
                ttl_seconds,
                json.dumps(payload, ensure_ascii=False),
            )
        except Exception:
            pass

    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO semantic_cache (cache_key, query, report, metadata_json, expires_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
                query = excluded.query,
                report = excluded.report,
                metadata_json = excluded.metadata_json,
                expires_at = excluded.expires_at
            """,
            (
                cache_key,
                query,
                report,
                json.dumps(metadata or {}, ensure_ascii=False),
                expires_at,
                now,
            ),
        )
    return cache_key


init_state_store()
