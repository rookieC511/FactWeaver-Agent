import logging
import traceback

from gateway.celery_app import celery
from gateway.executor import run_research_job_sync
from gateway.state_store import record_dlq, upsert_task

logger = logging.getLogger(__name__)


@celery.task(name="tasks.ping_worker")
def ping_worker():
    return {"status": "ok", "worker": "factweaver", "transport": "redis"}


def _run_task(
    task_id: str,
    query: str,
    *,
    research_mode: str,
    disable_cache: bool,
    resume_from_checkpoint: bool,
    backend: str,
) -> dict:
    return run_research_job_sync(
        task_id,
        query,
        backend=backend,
        research_mode=research_mode,
        disable_cache=disable_cache,
        resume_from_checkpoint=resume_from_checkpoint,
    )


@celery.task(
    bind=True,
    name="tasks.run_research_task",
    max_retries=3,
    default_retry_delay=10,
    acks_late=True,
)
def run_research_task(
    self,
    task_id: str,
    query: str,
    research_mode: str = "medium",
    disable_cache: bool = False,
    resume_from_checkpoint: bool = False,
):
    try:
        logger.info(
            "[Celery Worker] start task_id=%s mode=%s resume=%s query=%s",
            task_id,
            research_mode,
            resume_from_checkpoint,
            query[:80],
        )
        return _run_task(
            task_id,
            query,
            research_mode=research_mode,
            disable_cache=disable_cache,
            resume_from_checkpoint=resume_from_checkpoint,
            backend="celery",
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
            payload={
                "traceback": traceback.format_exc(),
                "research_mode": research_mode,
                "resume_from_checkpoint": resume_from_checkpoint,
            },
        )
        raise


@celery.task(
    bind=True,
    name="tasks.resume_research_task",
    max_retries=1,
    default_retry_delay=5,
    acks_late=True,
)
def resume_research_task(
    self,
    task_id: str,
    query: str,
    research_mode: str = "medium",
    disable_cache: bool = True,
):
    try:
        logger.info(
            "[Celery Worker] resume task_id=%s mode=%s query=%s",
            task_id,
            research_mode,
            query[:80],
        )
        return _run_task(
            task_id,
            query,
            research_mode=research_mode,
            disable_cache=disable_cache,
            resume_from_checkpoint=True,
            backend="celery",
        )
    except Exception as exc:
        logger.error(
            "[Celery Worker] resume failed task_id=%s error=%s\n%s",
            task_id,
            exc,
            traceback.format_exc(),
        )
        upsert_task(
            task_id,
            query,
            "FAILED",
            detail=f"恢复执行失败: {exc}",
            thread_id=task_id,
            backend="celery",
            last_error=repr(exc),
            research_mode=research_mode,
        )
        raise
