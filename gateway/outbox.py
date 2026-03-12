from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from gateway.celery_app import CELERY_AVAILABLE, celery
from gateway.state_store import claim_publish_jobs, enqueue_publish_job, mark_publish_job_failed, mark_publish_job_published

OUTBOX_POLL_INTERVAL_SECONDS = float(os.getenv("OUTBOX_POLL_INTERVAL_SECONDS", "0.5"))
OUTBOX_BATCH_SIZE = int(os.getenv("OUTBOX_BATCH_SIZE", "10"))
OUTBOX_MAX_ATTEMPTS = int(os.getenv("OUTBOX_MAX_ATTEMPTS", "6"))
OUTBOX_STALE_AFTER_SECONDS = float(os.getenv("OUTBOX_STALE_AFTER_SECONDS", "30"))
OUTBOX_PUBLISH_TIMEOUT_SECONDS = float(os.getenv("OUTBOX_PUBLISH_TIMEOUT_SECONDS", "4"))
OUTBOX_WARMUP_TIMEOUT_SECONDS = float(os.getenv("OUTBOX_WARMUP_TIMEOUT_SECONDS", "35"))

_publisher_task: asyncio.Task | None = None
_stop_event: asyncio.Event | None = None


def queue_publish_request(
    task_id: str,
    *,
    queue_name: str,
    task_name: str,
    payload: dict[str, Any],
) -> int:
    return enqueue_publish_job(
        task_id,
        queue_name=queue_name,
        task_name=task_name,
        payload=payload,
    )


def _send_to_celery(row: dict[str, Any]) -> None:
    payload = json.loads(str(row.get("payload_json") or "{}"))
    celery.send_task(
        str(row["task_name"]),
        kwargs=payload,
        queue=str(row["queue_name"]),
    )


def _warm_celery_connection() -> None:
    with celery.connection_for_write() as conn:
        conn.ensure_connection(max_retries=1)
    celery.send_task("tasks.ping_worker", kwargs={}, queue="research_queue")


async def publish_outbox_once() -> int:
    if not CELERY_AVAILABLE:
        return 0

    rows = claim_publish_jobs(
        limit=OUTBOX_BATCH_SIZE,
        max_attempts=OUTBOX_MAX_ATTEMPTS,
        stale_after_seconds=OUTBOX_STALE_AFTER_SECONDS,
    )
    published = 0
    for row in rows:
        try:
            await asyncio.wait_for(
                asyncio.to_thread(_send_to_celery, row),
                timeout=OUTBOX_PUBLISH_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            mark_publish_job_failed(int(row["id"]), repr(exc))
        else:
            mark_publish_job_published(int(row["id"]))
            published += 1
    return published


async def _publisher_loop() -> None:
    assert _stop_event is not None
    while not _stop_event.is_set():
        try:
            published = await publish_outbox_once()
        except Exception:
            published = 0
        sleep_for = 0.05 if published > 0 else OUTBOX_POLL_INTERVAL_SECONDS
        try:
            await asyncio.wait_for(_stop_event.wait(), timeout=sleep_for)
        except asyncio.TimeoutError:
            pass


async def start_outbox_publisher() -> None:
    global _publisher_task, _stop_event
    if not CELERY_AVAILABLE:
        return
    if _publisher_task and not _publisher_task.done():
        return
    try:
        await asyncio.wait_for(
            asyncio.to_thread(_warm_celery_connection),
            timeout=OUTBOX_WARMUP_TIMEOUT_SECONDS,
        )
    except Exception:
        pass
    _stop_event = asyncio.Event()
    _publisher_task = asyncio.create_task(_publisher_loop(), name="factweaver-outbox-publisher")


async def stop_outbox_publisher() -> None:
    global _publisher_task, _stop_event
    if _stop_event is not None:
        _stop_event.set()
    if _publisher_task is not None:
        try:
            await _publisher_task
        except Exception:
            pass
    _publisher_task = None
    _stop_event = None
