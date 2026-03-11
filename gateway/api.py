import os
import uuid

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator

from core.config import DEFAULT_RESEARCH_MODE
from core.costs import enrich_cost_fields
from gateway.celery_app import CELERY_AVAILABLE
from gateway.executor import run_research_job_sync
from gateway.state_store import get_cached_report, get_task, list_dlq, upsert_task

os.environ["FACTWEAVER_API_MODE"] = "1"

app = FastAPI(
    title="FactWeaver-Agent API",
    description="Deep Research Agent with queue-backed execution and durable task state.",
    version="4.6.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ResearchRequest(BaseModel):
    query: str = Field(..., min_length=2, max_length=2000, description="研究问题")
    research_mode: str = Field(default=DEFAULT_RESEARCH_MODE, description="检索档位: low|medium|high")
    disable_cache: bool = Field(default=False, description="仅用于 benchmark / stress test")

    @field_validator("research_mode")
    @classmethod
    def validate_research_mode(cls, value: str) -> str:
        mode = (value or DEFAULT_RESEARCH_MODE).strip().lower()
        if mode not in {"low", "medium", "high"}:
            raise ValueError("research_mode must be one of: low, medium, high")
        return mode


class ResearchResponse(BaseModel):
    task_id: str
    status: str = "QUEUED"
    message: str = "任务已提交至队列"
    research_mode: str = DEFAULT_RESEARCH_MODE


def _run_local_task(
    task_id: str,
    query: str,
    research_mode: str,
    disable_cache: bool,
    resume_from_checkpoint: bool = False,
) -> None:
    run_research_job_sync(
        task_id,
        query,
        backend="local",
        research_mode=research_mode,
        disable_cache=disable_cache,
        resume_from_checkpoint=resume_from_checkpoint,
    )


@app.post("/research", response_model=ResearchResponse)
async def submit_research(req: ResearchRequest, bg_tasks: BackgroundTasks):
    cached = None if req.disable_cache else get_cached_report(req.query, research_mode=req.research_mode)
    task_id = str(uuid.uuid4())

    if cached:
        metadata = cached.get("metadata") or {}
        upsert_task(
            task_id,
            req.query,
            "SUCCESS",
            detail="命中全局缓存，已跳过执行链路",
            report=cached["report"],
            thread_id=task_id,
            cache_key=cached["cache_key"],
            backend="cache",
            research_mode=req.research_mode,
            llm_cost_rmb=float(metadata.get("llm_cost_rmb", 0.0)),
            external_cost_usd_est=float(metadata.get("external_cost_usd_est", 0.0)),
            elapsed_seconds=float(metadata.get("elapsed_seconds", 0.0)),
            completed_at=int(metadata.get("completed_at") or 0) or None,
        )
        return ResearchResponse(
            task_id=task_id,
            status="SUCCESS",
            message="命中缓存",
            research_mode=req.research_mode,
        )

    backend = "celery" if CELERY_AVAILABLE else "local"
    upsert_task(
        task_id,
        req.query,
        "PENDING",
        detail="任务已入队，等待执行",
        thread_id=task_id,
        backend=backend,
        research_mode=req.research_mode,
        attempt_count=0,
        resume_count=0,
        resumed_from_checkpoint=False,
        interruption_state="queued",
    )

    if CELERY_AVAILABLE:
        from gateway.tasks import run_research_task

        run_research_task.apply_async(
            kwargs={
                "task_id": task_id,
                "query": req.query,
                "research_mode": req.research_mode,
                "disable_cache": req.disable_cache,
                "resume_from_checkpoint": False,
            },
            queue="research_queue",
        )
        return ResearchResponse(
            task_id=task_id,
            status="QUEUED",
            message="任务已提交到 Redis/Celery",
            research_mode=req.research_mode,
        )

    bg_tasks.add_task(_run_local_task, task_id, req.query, req.research_mode, req.disable_cache, False)
    return ResearchResponse(
        task_id=task_id,
        status="QUEUED",
        message="Celery 不可用，已切换本地回退执行",
        research_mode=req.research_mode,
    )


@app.post("/research/{task_id}/resume")
async def resume_research(task_id: str, bg_tasks: BackgroundTasks):
    task = get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task_id not found")
    if not task.get("last_checkpoint_id"):
        raise HTTPException(status_code=409, detail="task does not have a resumable checkpoint")
    if task.get("status") == "SUCCESS":
        raise HTTPException(status_code=409, detail="task already completed successfully")

    backend = "celery" if CELERY_AVAILABLE else "local"
    query = task.get("query") or ""
    research_mode = task.get("research_mode") or DEFAULT_RESEARCH_MODE

    upsert_task(
        task_id,
        query,
        "PENDING",
        detail="已提交恢复任务，等待从最近 checkpoint 继续执行",
        thread_id=task_id,
        backend=backend,
        research_mode=research_mode,
        llm_cost_rmb=float(task.get("llm_cost_rmb") or 0.0),
        external_cost_usd_est=float(task.get("external_cost_usd_est") or 0.0),
        serper_queries=int(task.get("serper_queries") or 0),
        serper_cost_usd_est=float(task.get("serper_cost_usd_est") or 0.0),
        tavily_credits_est=float(task.get("tavily_credits_est") or 0.0),
        tavily_cost_usd_est=float(task.get("tavily_cost_usd_est") or 0.0),
        elapsed_seconds=float(task.get("elapsed_seconds") or 0.0),
        attempt_count=int(task.get("attempt_count") or 0),
        resume_count=int(task.get("resume_count") or 0),
        resumed_from_checkpoint=True,
        started_at=task.get("started_at"),
        last_checkpoint_id=task.get("last_checkpoint_id"),
        last_checkpoint_ns=task.get("last_checkpoint_ns"),
        last_checkpoint_node=task.get("last_checkpoint_node"),
        interruption_state="queued_for_resume",
        last_km_snapshot_id=task.get("last_km_snapshot_id"),
    )

    if CELERY_AVAILABLE:
        from gateway.tasks import resume_research_task

        resume_research_task.apply_async(
            kwargs={
                "task_id": task_id,
                "query": query,
                "research_mode": research_mode,
                "disable_cache": True,
            },
            queue="research_queue",
        )
        return {"task_id": task_id, "status": "QUEUED", "message": "resume job queued"}

    bg_tasks.add_task(_run_local_task, task_id, query, research_mode, True, True)
    return {"task_id": task_id, "status": "QUEUED", "message": "resume job queued locally"}


@app.get("/research/{task_id}")
async def get_research_status(task_id: str):
    task = get_task(task_id)
    if not task:
        return {"task_id": task_id, "status": "FAILED", "detail": "无效或已过期的任务 ID"}
    payload = enrich_cost_fields(
        {
            "task_id": task["task_id"],
            "status": task["status"],
            "detail": task.get("detail") or "",
            "research_mode": task.get("research_mode") or DEFAULT_RESEARCH_MODE,
            "llm_cost_rmb": float(task.get("llm_cost_rmb") or 0.0),
            "external_cost_usd_est": float(task.get("external_cost_usd_est") or 0.0),
            "serper_queries": int(task.get("serper_queries") or 0),
            "serper_cost_usd_est": float(task.get("serper_cost_usd_est") or 0.0),
            "tavily_credits_est": float(task.get("tavily_credits_est") or 0.0),
            "tavily_cost_usd_est": float(task.get("tavily_cost_usd_est") or 0.0),
            "elapsed_seconds": float(task.get("elapsed_seconds") or 0.0),
            "attempt_count": int(task.get("attempt_count") or 0),
            "resume_count": int(task.get("resume_count") or 0),
            "resumed_from_checkpoint": bool(task.get("resumed_from_checkpoint") or 0),
            "last_checkpoint_id": task.get("last_checkpoint_id"),
            "last_checkpoint_node": task.get("last_checkpoint_node"),
            "interruption_state": task.get("interruption_state") or "",
            "created_at": task.get("created_at"),
            "started_at": task.get("started_at"),
            "completed_at": task.get("completed_at"),
        }
    )
    if task.get("report"):
        payload["report"] = task["report"]
    if task.get("last_error") and task["status"] == "FAILED":
        payload["error"] = task["last_error"]
    return payload


@app.get("/dlq")
async def get_dlq(limit: int = 50):
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 200")
    return {"items": list_dlq(limit=limit)}


@app.get("/health")
async def health_check():
    return {
        "status": "ok",
        "service": "FactWeaver-Agent",
        "version": "4.6.0",
        "queue_mode": "celery" if CELERY_AVAILABLE else "local-fallback",
        "default_research_mode": DEFAULT_RESEARCH_MODE,
    }
