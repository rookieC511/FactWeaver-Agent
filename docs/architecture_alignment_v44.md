# V4.4 Architecture Alignment Report

## 1. Why this document exists

This document records the implementation work completed for the "industrial-grade architecture alignment" pass.
Its purpose is to keep the codebase, the runtime behavior, and the external/project description aligned.

This pass focused on four goals:

1. Move the API entry path back onto a queue-oriented execution model.
2. Replace in-memory-only runtime state with durable checkpointing and task state storage.
3. Remove the expensive LLM self-correction loop and replace it with deterministic code-side repair and degradation.
4. Make session isolation, DLQ behavior, and cache behavior explicit enough to describe honestly in docs and interviews.


## 2. Clarification on the previous dependency statement

A previous note said that the environment did not have `celery` and `redis` installed.
That statement was true at the time of the first environment check, but it is no longer true now.

Current verified status:

- `celery==5.6.2`
- `redis==7.3.0`
- `gateway.celery_app.CELERY_AVAILABLE == True`
- local Redis server is currently running via Docker on `127.0.0.1:6379`
- local Celery worker is currently running against that broker

Important scope note:

- The Python client dependencies are now installed.
- A live Redis server is now available locally through Docker for development validation.
- The current local Redis server is containerized, not installed as a native Windows service.
- Queue mode is implemented in code and was validated against a reachable Redis broker/backend in this pass.


## 3. What was implemented

### 3.1 API entry path now prefers Celery instead of process-local background tasks

File: `gateway/api.py`

Implemented behavior:

- `POST /research` validates input, generates a `task_id`, writes an initial task record, and then prefers `run_research_task.apply_async(...)` when Celery is available.
- The previous process-local execution model was kept only as a controlled fallback path for environments where queue infrastructure is unavailable.
- `GET /research/{task_id}` now reads task state from the shared state store instead of a process-local in-memory dictionary.
- `GET /dlq` exposes dead-letter records for inspection.
- `GET /health` reports whether the service is running in `celery` mode or `local-fallback` mode.

Why this matters:

- The web process no longer needs to own the entire research execution path.
- Task state becomes inspectable and shareable across process boundaries.
- The code now matches the architectural claim that the gateway should hand work off instead of synchronously owning long-running execution.


### 3.2 Durable task state, semantic cache, and DLQ storage were added

File: `gateway/state_store.py`

Implemented behavior:

- Added a SQLite-backed state store for:
  - task records
  - semantic cache records
  - dead-letter queue records
- Added query normalization and hashing so identical requests can reuse cached results.
- Added task upsert and fetch helpers used by both API and worker paths.
- Added DLQ persistence for exhausted failures.
- Added report caching with timestamps and metadata.
- Added optional Redis push-through hooks when Redis is available.

Why this matters:

- The system now has a single source of truth for task state outside the FastAPI process.
- Cache hits can short-circuit full research execution for repeated identical queries.
- Dead-lettered work is visible instead of disappearing into logs or generic failure states.


### 3.3 Shared job execution logic was extracted from the API layer

File: `gateway/executor.py`

Implemented behavior:

- Added `run_research_job(...)` and `run_research_job_sync(...)` so both the local fallback and the Celery worker use the same execution flow.
- Added task-state transitions during execution.
- Added final report caching on successful completion.
- Preserved circuit-breaker constraints:
  - max task duration
  - max node count
  - max RMB-equivalent cost
- Bound session activation and cleanup to task lifecycle.

Why this matters:

- There is now one canonical execution path instead of divergent logic spread across API and worker layers.
- State transition behavior is consistent regardless of whether the task is executed locally or by Celery.


### 3.4 The Celery path was completed enough to be a real runtime option

Files:

- `gateway/celery_app.py`
- `gateway/tasks.py`

Implemented behavior:

- Added a real Celery app when the package is present.
- Added a safe `_NullCelery` compatibility layer so module import does not explode in minimal environments.
- Implemented `run_research_task` as a Celery task.
- Added `ping_worker` as a lightweight broker/worker/result-backend verification task.
- Added retry handling and DLQ recording when retries are exhausted.

Why this matters:

- The queue path is no longer just conceptual scaffolding.
- Worker-mode execution can now be wired into actual Redis/Celery deployment without further structural rewrite.


### 3.5 LangGraph checkpointing is no longer memory-only

Files:

- `core/checkpoint.py`
- `core/graph.py`
- `core/config.py`

Implemented behavior:

- Added `SQLiteBackedMemorySaver` as a durable checkpoint implementation.
- Replaced the old memory-only saver with the SQLite-backed saver in the compiled graph.
- Routed checkpoint DB path through config.

Why this matters:

- Checkpoints survive process restarts.
- The project can now truthfully claim persistent checkpointing.
- This pass intentionally targets SQLite first so the implementation is real today, instead of claiming PostgreSQL before it exists.

Important wording boundary:

- The current implementation is "durable checkpointing with SQLite".
- It is **not** yet accurate to claim "PostgreSQL + JSONB checkpointing" unless a future pass really implements that stack.


### 3.6 Step 3 LLM self-correction was removed from runtime behavior

Files:

- `core/tools.py`
- `core/graph.py`
- `core/writer_graph.py`

Implemented behavior:

- `clean_json_output(...)` was rewritten into a deterministic local repair pipeline.
- The repair pipeline handles common malformed outputs without making another LLM call:
  - fenced JSON
  - mixed prose around JSON
  - smart quotes / full-width punctuation
  - trailing commas
  - bracket completion
  - Python-literal-style fallback via `ast.literal_eval`
- `strict=True` now means "raise `LLMFormatError` only after local repair has been exhausted".
- Runtime paths that previously used LLM self-correction / tool-error LLM decisions were removed from the active graph flow.

Why this matters:

- Bad JSON no longer automatically creates another expensive API call.
- Tool failures no longer loop through a second model-based "what should I do?" decision.
- Token burn becomes more predictable and easier to test.


### 3.7 Tool failures now degrade deterministically instead of escalating back into the model

File: `core/graph.py`

Implemented behavior:

- Research execution now captures failure details in structured state instead of recursively delegating the decision back to the LLM.
- Added explicit state fields:
  - `missing_sources`
  - `degraded_items`
- Failures can now be recorded, skipped, and surfaced in the report instead of killing the whole workflow or silently disappearing.
- The writer appends a degradation appendix to the final report when evidence was incomplete.

Why this matters:

- The system remains productive under partial failure.
- The final output is more honest about evidence gaps.
- The runtime is more deterministic than the previous self-correction loop.


### 3.8 Session-isolated knowledge management was connected to the real runtime path

Files:

- `core/memory.py`
- `gateway/executor.py`
- `core/graph.py`
- `core/writer_graph.py`
- `tests/adapter.py`

Implemented behavior:

- Added explicit session activation helpers.
- `KnowledgeManager` instances are now tracked per session/task instead of relying on a single global store during execution.
- Runtime nodes now read from `get_current_km()` so concurrent work can stay separated by active session context.
- Session cleanup runs at the end of task execution.
- Added a compatibility `add_document(...)` alias so older adapter paths still work while using the new KM implementation.

Why this matters:

- This is the first version where the project can honestly describe session-level data isolation in the main execution path.
- It reduces the risk of cross-task data pollution during concurrent execution.


### 3.9 Optional dependency loading was cleaned up to improve testability

Files:

- `core/tools.py`
- `tests/adapter.py`
- `verify_modules.py`

Implemented behavior:

- `browser_use` is now lazy-loaded inside `visual_browse(...)` instead of being imported at module import time.
- If `browser_use` is not installed, the system returns a controlled degradation message instead of failing import/collection.
- Test adapters were updated to the current KM API and session model.
- `verify_modules.py` was updated to check the current real implementation rather than an outdated structure.

Why this matters:

- Tests can run in text-only environments.
- Optional browsing capability no longer breaks the baseline pipeline.


### 3.10 DLQ inspection support was added

File: `scripts/manage_dlq.py`

Implemented behavior:

- Added a small management script for reviewing dead-lettered tasks.

Why this matters:

- Dead-lettering is now observable and operationally usable, not just a stored concept.


## 4. Files changed in this pass

Core runtime:

- `core/checkpoint.py`
- `core/config.py`
- `core/graph.py`
- `core/memory.py`
- `core/tools.py`
- `core/writer_graph.py`

Gateway and execution:

- `gateway/api.py`
- `gateway/celery_app.py`
- `gateway/executor.py`
- `gateway/state_store.py`
- `gateway/tasks.py`

Verification and tooling:

- `scripts/manage_dlq.py`
- `verify_modules.py`
- `tests/adapter.py`
- `tests/test_pipeline.py`
- `tests/test_writer_formatting.py`
- `scripts/run_eval.py`

Project metadata / docs:

- `.gitignore`
- `docs/AI_CONTEXT.md`


## 5. What this architecture can now claim honestly

The following statements are now supportable by the codebase:

- The gateway can hand tasks off to a queue-oriented worker path instead of owning the full long-running execution inline.
- The system persists task state, semantic cache records, DLQ records, and graph checkpoints outside process memory.
- Malformed structured output is repaired in deterministic code before failing.
- Tool failures degrade into explicit report gaps instead of triggering uncontrolled model-driven self-retry behavior.
- The runtime supports session-scoped knowledge isolation.
- Repeated identical queries can be short-circuited through a cache layer.


## 6. What this architecture should NOT claim yet

The following statements would still be overstated today:

- "The system already uses PostgreSQL + JSONB for checkpoint persistence."
- "Redis/Celery end-to-end production mode has been fully validated in this local environment."
- "The system already uses semantic vector caching."
- "The gateway uses SSE streaming today."

More precise wording today would be:

- durable checkpointing via SQLite
- queue-ready Celery integration with Redis-compatible runtime path
- exact-query hash cache, not full semantic retrieval cache
- polling-based task status API, not SSE


## 7. Verification completed

Environment and imports:

- Verified `celery` import.
- Verified `redis` import.
- Verified `CELERY_AVAILABLE=True`.
- Verified Redis broker reachability with `PING=True`.
- Verified local Redis server role as `master`.

Compilation and health checks completed earlier in this pass:

- `python -m compileall core gateway scripts tests verify_modules.py main.py`
- `pytest tests/test_review_logic.py -q`
- `pytest tests/test_chart_integration.py -q`
- `python verify_modules.py`

Functional smoke checks completed earlier in this pass:

- imported `gateway.api.app`
- imported `core.graph.app`
- verified task state store read/write
- verified cache store read/write
- verified DLQ record creation/listing
- started a dedicated Redis container named `deepresearch-redis`
- started a local Celery worker in `solo` pool mode for Windows compatibility
- confirmed worker connection to `redis://localhost:6379/0`
- confirmed worker readiness via `celery inspect ping`
- submitted `tasks.ping_worker`
- received successful result from `tasks.ping_worker`

Operational artifacts produced in this pass:

- worker log file: `runtime/celery_worker.log`
- worker pid file: `runtime/celery_worker.pid`


## 8. Dependency installation notes

Installed packages:

- `celery==5.6.2`
- `redis==7.3.0`

Local runtime components started for validation:

- Docker Desktop
- Redis container: `deepresearch-redis`
- Celery worker process bound to queue `research_queue`

Installer warnings observed during package installation:

- `streamlit 1.32.0` expects `protobuf<5,>=3.20`, but the environment currently has `protobuf 5.29.5`
- `streamlit 1.32.0` expects `rich<14,>=10.14.0`, but the environment currently has `rich 14.3.2`

These warnings were not introduced by this pass directly, but they are present in the current Python environment and should be cleaned up if Streamlit-based workflows matter for this repository.


## 9. Remaining follow-up work

Recommended next steps:

1. Add an automated integration test that verifies:
   - task submission
   - worker execution
   - state transition visibility
   - DLQ behavior after retry exhaustion
2. Add a dedicated non-LLM integration test around `tasks.ping_worker` so queue health can be validated without external model credentials.
3. If interview positioning requires PostgreSQL, implement it for real in a separate pass instead of describing it ahead of time.
4. If UX becomes a priority, add SSE or WebSocket status streaming later without changing the backend contract first.


## 10. Bottom line

This pass moved the project from "queue skeleton + in-memory graph prototype" toward a coherent, durable, and more honest runtime architecture.
The most important correction was removing the expensive LLM self-correction loop and replacing it with deterministic repair, bounded degradation, and explicit evidence-gap reporting.

The project is now substantially closer to the architecture narrative, but the narrative must still stay precise:

- Celery/Redis Python support is installed and wired.
- Redis broker-backed local validation is now complete.
- SQLite-backed durability is implemented.
- PostgreSQL is not yet implemented.
- Full research-task success still depends on valid external model/search API credentials.
