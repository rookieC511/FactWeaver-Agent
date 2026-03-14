# FactWeaver-Agent

面向长报告研究任务的 Deep Research Agent。

当前版本已经从早期的单体同步脚本，演进为一套可恢复、可排队、可评测、可控成本的研究系统，核心目标是：

- 生成有引用支撑的长报告
- 支持 `low / medium / high` 三档检索模式
- 支持任务排队、状态跟踪、恢复执行
- 区分模型成本与外部检索成本
- 用公开 benchmark 和内部 benchmark 持续做质量回归

## 当前架构

- 后端：`FastAPI + Celery + Redis + LangGraph`
- 状态持久化：`SQLite`
- 前端：`React + Vite`
- 任务提交：`SQLite task state + SQLite publish_outbox + async outbox publisher`
- 恢复能力：`checkpoint + task state + KnowledgeManager snapshot`
- 默认检索档位：`medium`

## 核心能力

### 1. 队列化提交

`POST /research` 不再在请求线程里同步调用 `celery.send_task(...)`。

现在的提交语义是：

- 任务已接受并持久化
- 异步发布到 Celery/Redis
- 前端可以立即拿到 `task_id`

任务详情里会返回：

- `publish_status`
- `publish_attempt_count`
- `publish_last_error`
- `queued_at`

### 2. 可恢复执行

支持显式恢复入口：

- `POST /research/{task_id}/resume`

恢复不再只依赖 LangGraph checkpoint，而是同时恢复：

- task state
- latest checkpoint metadata
- `KnowledgeManager` snapshot

恢复相关状态字段包括：

- `resume_count`
- `resumed_from_checkpoint`
- `last_checkpoint_id`
- `last_checkpoint_node`
- `interruption_state`

### 3. 三档检索模式

#### `low`

低成本优先：

- `Serper basic`
- 轻量抓取
- 不走 retrieval 侧长文抽取链

适合预算最敏感场景。

#### `medium`

默认平衡档：

- `Serper`
- `Tavily Extract`
- authority-first 检索与证据门控

这是当前默认推荐模式。

#### `high`

高覆盖高成本档：

- `Tavily Search`
- `Map`
- `Crawl`
- `Extract`

适合追求覆盖率和报告质量，但外部检索成本明显更高。

## Evidence Acquisition 现状

检索链已经从“搜到什么抓什么”收敛成显式的 Evidence Acquisition 流程：

- 候选来源召回
- 来源质量判断
- 抓取编排与失败分类
- 证据门控

当前重点特性：

- authority-first 检索
- 高价值来源优先进入主证据集合
- PDF blocked host 与非 PDF blocked host 分开处理
- `retrieval_failed` 显式区分“没搜到”和“搜到了但抓不下来”

当前已完成的第一轮专项优化：

- PDF blocked 专项
- `arxiv.org` / 学术 HTML host 可达性修复
- `direct_answer_support_rate` 统计口径与 authority 判定修复

## API

### `POST /research`

请求示例：

```json
{
  "query": "DeepSeek R1 vs OpenAI o1 reasoning differences and 2025 compute cost",
  "research_mode": "medium"
}
```

### `GET /research/{task_id}`

返回内容包括：

- `status`
- `research_mode`
- `llm_cost_rmb`
- `external_cost_rmb_est`
- `total_cost_rmb_est`
- `serper_queries`
- `tavily_credits_est`
- `elapsed_seconds`
- `publish_status`
- `resume_count`

审计字段里仍保留：

- `external_cost_usd_est`
- `serper_cost_usd_est`
- `tavily_cost_usd_est`

## 前端

前端提供：

- 查询输入
- 三档模式选择
- 任务状态轮询
- 成本与耗时摘要展示

入口文件：

- [frontend/src/App.tsx](frontend/src/App.tsx)
- [frontend/src/components/SearchBar.tsx](frontend/src/components/SearchBar.tsx)
- [frontend/src/components/StatusMonitor.tsx](frontend/src/components/StatusMonitor.tsx)

## 本地启动

### 1. 安装依赖

Python：

```bash
pip install -r requirements.txt
```

前端：

```bash
cd frontend
npm install
```

### 2. 启动 Redis 和 Celery

```bash
celery -A gateway.tasks worker -l info
```

### 3. 启动后端

```bash
uvicorn gateway.api:app --host 0.0.0.0 --port 8000 --reload
```

### 4. 启动前端

```bash
cd frontend
npm run dev
```

## 环境变量

至少需要：

```env
OPENAI_API_KEY=...
SERPER_API_KEY=...
TAVILY_API_KEY=...
```

常用可选项：

```env
REDIS_BROKER_URL=redis://localhost:6379/0
REDIS_RESULT_BACKEND=redis://localhost:6379/1
DEFAULT_RESEARCH_MODE=medium
MAX_TASK_DURATION_SECONDS=300
MAX_TASK_NODE_COUNT=15
MAX_TASK_RMB_COST=1.0
```

## 评测与 Benchmark

项目当前分两条评测线：

### 1. 内部 benchmark

用于模式、成本、稳定性和长报告质量回归。

典型脚本：

- `python scripts/benchmark_modes.py`
- `python scripts/judge_bakeoff.py`
- `python scripts/recovery_benchmark.py`
- `python scripts/concurrency_probe.py`
- `python scripts/cost_ab_experiment.py`

### 2. 公开 benchmark

当前主线是本地兼容的 `DeepResearch Bench` 风格长报告评测，不是官方 leaderboard 成绩。

核心脚本：

- `python scripts/public_benchmark_deepresearch_bench.py`
- `python scripts/deepresearch_bench_scoring.py`

评分关注：

- `drb_report_score`
- `fact_score`
- `comprehensiveness`
- `insight`
- `instruction_following`
- `readability`

额外诊断指标：

- `blocked_source_rate`
- `blocked_attempt_rate`
- `authority_source_rate`
- `weak_source_hit_rate`
- `direct_answer_support_rate`
- `retrieval_failed`

## 最新小样本结论

以下不是官方榜单成绩，而是当前本地兼容公开 benchmark 的最新 `3` 题 pilot：

- `avg_drb_report_score = 7.0553`
- `avg_fact_score = 8.4`
- `direct_answer_support_rate = 0.8333`
- `blocked_source_rate = 0.0`
- `retrieval_failed = 1/3`

这说明：

- 第一轮 blocked/access 优化是有效的
- 系统已经不再主要卡在 PDF 或 `arxiv` 可达性
- 当前剩余瓶颈开始收敛到“高权威证据覆盖仍不够完整”

## 验证命令

常用回归：

```bash
python verify_modules.py
pytest tests/test_research_modes.py -q
pytest tests/test_benchmark_scoring.py -q
pytest tests/test_deepresearch_bench.py -q
pytest tests/test_evidence_acquisition.py -q
pytest tests/test_writer_graph.py -q
```

## 文档

- 架构上下文：[docs/AI_CONTEXT.md](docs/AI_CONTEXT.md)
- Benchmark 归档：[docs/benchmarks](docs/benchmarks)
- 历史决策归档：[docs/adr](docs/adr)

## 当前判断

- 默认档位仍然是 `medium`
- 第一轮检索可达性优化已经完成
- 当前下一阶段主瓶颈不是 writer 报错，也不是 PDF blocked
- 下一步更适合继续打：
  - 高权威 evidence slot 覆盖
  - `retrieval_failed`
  - `authority_source_rate`
  - `weak_source_hit_rate`
