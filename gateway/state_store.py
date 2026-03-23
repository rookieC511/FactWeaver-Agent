import hashlib
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from typing import Any

from core.config import DEFAULT_ARCHITECTURE_MODE, DEFAULT_RESEARCH_MODE, REDIS_BROKER_URL, SEMANTIC_CACHE_TTL_SECONDS, STATE_DB_PATH


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


def _now_ts() -> float:
    return time.time()


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
                "attempt_count": "INTEGER DEFAULT 0",
                "resume_count": "INTEGER DEFAULT 0",
                "resumed_from_checkpoint": "INTEGER DEFAULT 0",
                "started_at": "INTEGER",
                "completed_at": "INTEGER",
                "last_checkpoint_id": "TEXT",
                "last_checkpoint_ns": "TEXT DEFAULT ''",
                "last_checkpoint_node": "TEXT",
                "interruption_state": "TEXT",
                "last_km_snapshot_id": "INTEGER",
                "publish_status": "TEXT DEFAULT ''",
                "publish_attempt_count": "INTEGER DEFAULT 0",
                "publish_last_error": "TEXT",
                "queued_at": "REAL",
                "architecture_mode": "TEXT DEFAULT 'supervisor_team'",
            },
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS publish_outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                queue_name TEXT NOT NULL,
                task_name TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                published_at REAL
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_publish_outbox_status_created
            ON publish_outbox(status, created_at ASC)
            """
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
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS knowledge_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                thread_id TEXT NOT NULL,
                checkpoint_id TEXT,
                checkpoint_ns TEXT DEFAULT '',
                checkpoint_node TEXT,
                snapshot_json TEXT NOT NULL,
                created_at INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_knowledge_snapshots_task_created
            ON knowledge_snapshots(task_id, created_at DESC)
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
    attempt_count: int | None = None,
    resume_count: int | None = None,
    resumed_from_checkpoint: bool | int | None = None,
    started_at: int | None = None,
    completed_at: int | None = None,
    last_checkpoint_id: str | None = None,
    last_checkpoint_ns: str | None = None,
    last_checkpoint_node: str | None = None,
    interruption_state: str | None = None,
    last_km_snapshot_id: int | None = None,
    publish_status: str | None = None,
    publish_attempt_count: int | None = None,
    publish_last_error: str | None = None,
    queued_at: float | None = None,
    architecture_mode: str | None = DEFAULT_ARCHITECTURE_MODE,
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
                elapsed_seconds, attempt_count, resume_count, resumed_from_checkpoint,
                started_at, completed_at, last_checkpoint_id, last_checkpoint_ns,
                last_checkpoint_node, interruption_state, last_km_snapshot_id,
                publish_status, publish_attempt_count, publish_last_error, queued_at
                , architecture_mode
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                elapsed_seconds = COALESCE(excluded.elapsed_seconds, tasks.elapsed_seconds),
                attempt_count = COALESCE(excluded.attempt_count, tasks.attempt_count),
                resume_count = COALESCE(excluded.resume_count, tasks.resume_count),
                resumed_from_checkpoint = COALESCE(excluded.resumed_from_checkpoint, tasks.resumed_from_checkpoint),
                started_at = COALESCE(excluded.started_at, tasks.started_at),
                completed_at = COALESCE(excluded.completed_at, tasks.completed_at),
                last_checkpoint_id = COALESCE(excluded.last_checkpoint_id, tasks.last_checkpoint_id),
                last_checkpoint_ns = COALESCE(excluded.last_checkpoint_ns, tasks.last_checkpoint_ns),
                last_checkpoint_node = COALESCE(excluded.last_checkpoint_node, tasks.last_checkpoint_node),
                interruption_state = COALESCE(excluded.interruption_state, tasks.interruption_state),
                last_km_snapshot_id = COALESCE(excluded.last_km_snapshot_id, tasks.last_km_snapshot_id),
                publish_status = COALESCE(excluded.publish_status, tasks.publish_status),
                publish_attempt_count = COALESCE(excluded.publish_attempt_count, tasks.publish_attempt_count),
                publish_last_error = COALESCE(excluded.publish_last_error, tasks.publish_last_error),
                queued_at = COALESCE(excluded.queued_at, tasks.queued_at),
                architecture_mode = COALESCE(excluded.architecture_mode, tasks.architecture_mode)
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
                attempt_count,
                resume_count,
                None if resumed_from_checkpoint is None else int(bool(resumed_from_checkpoint)),
                started_at,
                completed_at,
                last_checkpoint_id,
                last_checkpoint_ns,
                last_checkpoint_node,
                interruption_state,
                last_km_snapshot_id,
                publish_status,
                publish_attempt_count,
                publish_last_error,
                queued_at,
                architecture_mode,
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


def enqueue_publish_job(
    task_id: str,
    *,
    queue_name: str,
    task_name: str,
    payload: dict[str, Any],
) -> int:
    now = _now_ts()
    payload_json = json.dumps(payload, ensure_ascii=False)
    with _connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO publish_outbox (
                task_id, queue_name, task_name, payload_json, status,
                attempt_count, last_error, created_at, updated_at, published_at
            )
            VALUES (?, ?, ?, ?, 'PENDING', 0, NULL, ?, ?, NULL)
            """,
            (task_id, queue_name, task_name, payload_json, now, now),
        )
        conn.execute(
            """
            UPDATE tasks
            SET publish_status = 'PENDING',
                publish_attempt_count = 0,
                publish_last_error = NULL,
                queued_at = NULL,
                updated_at = ?
            WHERE task_id = ?
            """,
            (now, task_id),
        )
        return int(cursor.lastrowid or 0)


def _publish_backoff_seconds(attempt_count: int) -> float:
    return float(min(60, 2 ** max(0, attempt_count)))


def claim_publish_jobs(
    *,
    limit: int = 10,
    max_attempts: int = 6,
    stale_after_seconds: float = 30.0,
) -> list[dict[str, Any]]:
    now = _now_ts()
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            """
            SELECT *
            FROM publish_outbox
            WHERE status IN ('PENDING', 'FAILED', 'PUBLISHING')
              AND attempt_count < ?
            ORDER BY created_at ASC, id ASC
            """,
            (max_attempts,),
        ).fetchall()
        selected_ids: list[int] = []
        selected_rows: list[dict[str, Any]] = []
        for row in rows:
            data = dict(row)
            status = str(data.get("status") or "")
            attempt_count = int(data.get("attempt_count") or 0)
            updated_at = float(data.get("updated_at") or 0.0)
            if status == "PUBLISHING" and (now - updated_at) < stale_after_seconds:
                continue
            if status == "FAILED" and (now - updated_at) < _publish_backoff_seconds(attempt_count):
                continue
            selected_ids.append(int(data["id"]))
            selected_rows.append(data)
            if len(selected_rows) >= limit:
                break

        if selected_ids:
            placeholders = ", ".join("?" for _ in selected_ids)
            conn.execute(
                f"""
                UPDATE publish_outbox
                SET status = 'PUBLISHING',
                    updated_at = ?
                WHERE id IN ({placeholders})
                """,
                (now, *selected_ids),
            )

        return selected_rows


def mark_publish_job_published(outbox_id: int) -> None:
    now = _now_ts()
    with _connect() as conn:
        row = conn.execute(
            "SELECT task_id, attempt_count FROM publish_outbox WHERE id = ?",
            (outbox_id,),
        ).fetchone()
        if not row:
            return
        task_id = str(row["task_id"])
        attempt_count = int(row["attempt_count"] or 0) + 1
        conn.execute(
            """
            UPDATE publish_outbox
            SET status = 'PUBLISHED',
                attempt_count = ?,
                last_error = NULL,
                updated_at = ?,
                published_at = ?
            WHERE id = ?
            """,
            (attempt_count, now, now, outbox_id),
        )
        conn.execute(
            """
            UPDATE tasks
            SET status = CASE WHEN status = 'PENDING' THEN 'QUEUED' ELSE status END,
                detail = CASE
                    WHEN status = 'PENDING' THEN '任务已发布到 Redis/Celery，等待 worker 拉取'
                    ELSE detail
                END,
                publish_status = 'PUBLISHED',
                publish_attempt_count = ?,
                publish_last_error = NULL,
                queued_at = COALESCE(queued_at, ?),
                updated_at = ?
            WHERE task_id = ?
            """,
            (attempt_count, now, now, task_id),
        )


def mark_publish_job_failed(outbox_id: int, error: str) -> None:
    now = _now_ts()
    with _connect() as conn:
        row = conn.execute(
            "SELECT task_id, attempt_count FROM publish_outbox WHERE id = ?",
            (outbox_id,),
        ).fetchone()
        if not row:
            return
        task_id = str(row["task_id"])
        attempt_count = int(row["attempt_count"] or 0) + 1
        conn.execute(
            """
            UPDATE publish_outbox
            SET status = 'FAILED',
                attempt_count = ?,
                last_error = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (attempt_count, error, now, outbox_id),
        )
        conn.execute(
            """
            UPDATE tasks
            SET detail = CASE
                    WHEN status = 'PENDING' THEN '任务投递到 Redis/Celery 失败，系统将自动重试'
                    ELSE detail
                END,
                publish_status = 'FAILED',
                publish_attempt_count = ?,
                publish_last_error = ?,
                updated_at = ?
            WHERE task_id = ?
            """,
            (attempt_count, error, now, task_id),
        )


def get_outbox_stats() -> dict[str, int]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT status, COUNT(*) AS count
            FROM publish_outbox
            GROUP BY status
            """
        ).fetchall()
    return {str(row["status"]): int(row["count"]) for row in rows}


def save_knowledge_snapshot(
    task_id: str,
    *,
    thread_id: str,
    checkpoint_id: str | None,
    checkpoint_ns: str | None,
    checkpoint_node: str | None,
    snapshot: dict[str, Any],
) -> int:
    now = _now_ts()
    snapshot_json = json.dumps(snapshot, ensure_ascii=False)
    with _connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO knowledge_snapshots (
                task_id, thread_id, checkpoint_id, checkpoint_ns, checkpoint_node,
                snapshot_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                thread_id,
                checkpoint_id,
                checkpoint_ns or "",
                checkpoint_node,
                snapshot_json,
                now,
            ),
        )
        snapshot_id = int(cursor.lastrowid or 0)
        conn.execute(
            """
            UPDATE tasks
            SET last_km_snapshot_id = ?, updated_at = ?
            WHERE task_id = ?
            """,
            (snapshot_id, now, task_id),
        )
    return snapshot_id


def get_latest_knowledge_snapshot(task_id: str) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT *
            FROM knowledge_snapshots
            WHERE task_id = ?
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (task_id,),
        ).fetchone()
    if not row:
        return None
    data = dict(row)
    data["snapshot"] = json.loads(data.get("snapshot_json") or "{}")
    return data


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
