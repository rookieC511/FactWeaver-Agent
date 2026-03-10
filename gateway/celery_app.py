from types import SimpleNamespace

from core.config import REDIS_BROKER_URL, REDIS_RESULT_BACKEND

CELERY_AVAILABLE = False

try:
    from celery import Celery  # type: ignore

    celery = Celery(
        "factweaver",
        broker=REDIS_BROKER_URL,
        backend=REDIS_RESULT_BACKEND,
    )
    celery.conf.update(
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        task_time_limit=1800,
        task_soft_time_limit=1500,
        task_default_retry_delay=10,
        task_max_retries=3,
        worker_concurrency=2,
        worker_prefetch_multiplier=1,
        result_expires=86400,
        task_routes={"tasks.run_research_task": {"queue": "research_queue"}},
        task_default_queue="research_queue",
    )
    CELERY_AVAILABLE = True
except Exception:
    class _LocalAsyncResult:
        def __init__(self, task_id: str):
            self.id = task_id
            self.status = "PENDING"
            self.info = None

    class _NullCelery:
        def __init__(self):
            self.conf = SimpleNamespace(
                broker_url=REDIS_BROKER_URL,
                result_backend=REDIS_RESULT_BACKEND,
                task_default_queue="research_queue",
                task_max_retries=3,
                task_acks_late=True,
                task_time_limit=1800,
                task_soft_time_limit=1500,
                worker_concurrency=2,
                worker_prefetch_multiplier=1,
            )

        def task(self, *args, **kwargs):
            def decorator(func):
                return func

            return decorator

        def AsyncResult(self, task_id: str):
            return _LocalAsyncResult(task_id)

    celery = _NullCelery()
