# Deep Research Agent - AI Context

## 2026-03-12 Active Update

- Submit path has been upgraded from synchronous broker publish to `SQLite outbox`.
- Current API contract:
  - `POST /research` means task accepted and persisted
  - `POST /research/{task_id}/resume` uses the same outbox path
  - queue publish is asynchronous and no longer blocks the request thread
- Active task state now includes:
  - `publish_status`
  - `publish_attempt_count`
  - `publish_last_error`
  - `queued_at`
- New runtime component:
  - `gateway/outbox.py` runs an in-process outbox publisher on FastAPI startup
- Benchmark split is now explicit:
  - internal report benchmark: `scripts/benchmark_30_runs.py`
  - public QA benchmark: `scripts/public_benchmark_deepsearchqa.py`
  - submit latency validation: `scripts/submit_latency_smoke.py`
- Public benchmark direction is fixed to:
  - public dataset sample first
  - extract final answer(s) from the long report
  - compare extracted answer(s) against gold answers
- Current public benchmark default target:
  - dataset: `google/deepsearchqa`
  - first run size: `30` stratified samples

这个文件只保留当前仍然生效的项目上下文，避免把历史实验流水账继续堆进主上下文。

历史决策看：
- `docs/adr/`

benchmark 与评测摘要看：
- `docs/benchmarks/`
- `reports/`

## 当前版本

- 当前主版本：`V4.6`
- 网关层：`FastAPI + Redis/Celery`
- 执行编排：`LangGraph`
- 持久化：
  - 任务状态 / 语义缓存 / DLQ：`SQLite`
  - checkpoint：`SQLiteBackedMemorySaver`
  - 知识快照：`knowledge_snapshots` 表
- 前端：`React + Vite`
- 检索模式：`low / medium / high`
- 默认档位：`medium`

## 当前有效架构规则

- API 主路径优先走 `Redis/Celery`；只有 Celery 不可用时才回退本地执行。
- `task_id` 同时作为任务状态主键、session 隔离键、LangGraph `thread_id`。
- `KnowledgeManager` 按 `task_id/thread_id` 隔离，不同任务不共享事实块。
- Step 3 已移除“LLM 自纠闭环”：
  - 坏 JSON 先走代码修复
  - 工具失败走确定性降级，不再把错误喂回模型二次决策
- 任务状态主展示统一优先用人民币：
  - `llm_cost_rmb`
  - `external_cost_rmb_est`
  - `total_cost_rmb_est`
- 原始美元字段继续保留作审计：
  - `external_cost_usd_est`
  - `serper_cost_usd_est`
  - `tavily_cost_usd_est`

## 三档检索模式

### `low`

- 路径：`Serper basic -> scrape_jina_ai -> add_compact_document`
- 特点：
  - 不走 Tavily 主链路
  - 不走旧的长文 `aadd_document()` 抽取链
  - 只在文本完全不可用且出现 `[VLM_REQUIRED]` 时允许一次视觉兜底

### `medium`

- 路径：`Serper basic -> Tavily Extract basic -> add_extracted_chunks`
- fallback：`scrape_jina_ai -> add_compact_document`
- 定位：当前默认档，质量 / 成本 / 速度最平衡

### `high`

- 路径：`Tavily Search advanced -> Map -> Crawl -> Extract advanced`
- fallback：`crawl raw_content` 或 `scrape_jina_ai`，必要时再走 `visual_browse`
- 当前不接 `Tavily Research`

## Checkpoint 恢复链路

当前恢复能力不再只依赖 LangGraph checkpoint，还依赖持久化 KM 快照。

恢复入口：
- `POST /research/{task_id}/resume`

恢复链路：
1. 读取任务状态
2. 读取最近 checkpoint 元信息
3. 恢复最近 `KnowledgeManager` snapshot
4. 用相同 `task_id/thread_id` 从最近 checkpoint 继续执行

恢复相关状态字段：
- `resume_count`
- `resumed_from_checkpoint`
- `last_checkpoint_id`
- `last_checkpoint_node`
- `interruption_state`
- `attempt_count`

当前支持的恢复安全点设计：
- 主图 `planner` 后
- 主图 `executor` 后
- writer 子图 `section_writer` fan-in 完成后、`editor` 开始前

说明：
- 第三个恢复点依赖 writer 子图也挂上 checkpoint
- writer 子图恢复使用独立 writer thread：`{task_id}:writer`

## Judge 与评分口径

当前 benchmark 评分统一使用：
- `FACT`
- `RACE`
- `quality_score = 0.55 * FACT + 0.45 * RACE`
- `overall_score = 0.70 * quality + 0.30 * value`

本地 judge 约定：
- `BENCHMARK_JUDGE_BASE_URL=http://localhost:11434/v1`
- 默认 judge：`qwen3:8b`
- fallback judge：`llama3.1:latest`

已完成的本地 judge bakeoff 结果：
- 产物：
  - `reports/judge_bakeoff_20260311_220911.json`
  - `reports/judge_bakeoff_20260311_220911.md`
- 结论：
  - `qwen3:8b` 被选为默认 judge
  - `llama3.1:latest` 保留为 fallback
  - 两者 JSON 解析率都为 `100%`
  - `qwen3:8b` 的锚点稳定性更好，但延迟明显更高

## 当前证据脚本

### 已稳定可用

- `scripts/judge_bakeoff.py`
  - 本地 judge 对比：`llama3.1` vs `qwen3:8b`
- `scripts/benchmark_modes.py`
  - 三档 benchmark 运行 / 离线重评分
- `scripts/benchmark_scoring.py`
  - 人民币换算、质量分、综合分

### 新增证据脚本

- `scripts/recovery_benchmark.py`
  - 真实进程中断后的 checkpoint 恢复实验
- `scripts/concurrency_probe.py`
  - 真实 `FastAPI + Celery + Redis` 并发稳定性探针
- `scripts/cost_ab_experiment.py`
  - `legacy_full_context` vs `section_scoped` 成本 A/B
- `scripts/benchmark_30_runs.py`
  - `10 queries x 3 modes` 的 30-run 扩样脚本
- `scripts/run_task_process.py`
  - 恢复实验用的独立任务进程入口

## Benchmark 与预算规则

benchmark 全口径预算控制：
- `BENCHMARK_MAX_TASK_RMB`
- `BENCHMARK_MAX_TOTAL_RMB`
- `BENCHMARK_MAX_EXTERNAL_RMB_EST`
- `BENCHMARK_MAX_ALLIN_RMB_EST`

统一换算规则：
- `1 USD = 7.20 RMB`

当前 9-run 离线重评分结论：
- 默认推荐档位：`medium`
- 质量最高档位：`high`
- 性价比最高档位：`low`
- 最慢档位：`high`
- 最贵档位：`high`

## 关键文件

- `core/graph.py`
  - 主研究图
- `core/writer_graph.py`
  - writer 子图、writer checkpoint、writer context mode
- `core/memory.py`
  - session 隔离、KM snapshot / restore
- `gateway/executor.py`
  - 可恢复执行器、checkpoint + KM snapshot 对齐
- `gateway/state_store.py`
  - SQLite 任务状态、缓存、DLQ、knowledge snapshots
- `gateway/api.py`
  - `/research` 和 `/research/{task_id}/resume`
- `scripts/judge_bakeoff.py`
  - judge 选型
- `scripts/recovery_benchmark.py`
  - 恢复成功率实验

## 同步规则

任何会影响当前系统行为的关键改动，必须同时更新：

1. 本文件中的当前有效信息
2. 如属架构决策，补到 `docs/adr/`
3. 如属 benchmark / 评测结论，补到 `docs/benchmarks/` 或 `reports/`
