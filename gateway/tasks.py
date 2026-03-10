import logging
import traceback

from gateway.celery_app import celery
from gateway.executor import run_research_job_sync
from gateway.state_store import record_dlq, upsert_task

logger = logging.getLogger(__name__)


@celery.task(name="tasks.ping_worker")
def ping_worker():
    return {"status": "ok", "worker": "factweaver", "transport": "redis"}


@celery.task(
    bind=True,
    name="tasks.run_research_task",
    max_retries=3,
    default_retry_delay=10,
    acks_late=True,
)
def run_research_task(self, query: str, research_mode: str = "medium"):
    task_id = getattr(self.request, "id", None) or getattr(self, "request", {}).get("id")
    task_id = task_id or "local-task"
    try:
        logger.info(
            "[Celery Worker] start task_id=%s mode=%s query=%s",
            task_id,
            research_mode,
            query[:80],
        )
        return run_research_job_sync(
            task_id,
            query,
            backend="celery",
            research_mode=research_mode,
        )
    except Exception as exc:
        retry_count = getattr(self.request, "retries", 0)
        logger.error(
            "[Celery Worker] task failed task_id=%s retry=%s error=%s\n%s",
            task_id,
            retry_count + 1,
            exc,
            traceback.format_exc(),
        )
        upsert_task(
            task_id,
            query,
            "FAILED",
            detail=f"任务执行失败: {exc}",
            thread_id=task_id,
            backend="celery",
            last_error=repr(exc),
            research_mode=research_mode,
        )

        max_retries = getattr(self, "max_retries", 3)
        if retry_count < max_retries:
            countdown = 10 * (2**retry_count)
            raise self.retry(exc=exc, countdown=countdown)

        record_dlq(
            task_id,
            query,
            thread_id=task_id,
            retries=retry_count + 1,
            error=repr(exc),
            payload={"traceback": traceback.format_exc(), "research_mode": research_mode},
        )
        raise
