# FactWeaver-Agent

工业级 Deep Research Agent。当前版本已经完成从“单体同步调用”到“队列化网关 + 持久化状态机 + 三档检索模式”的重构。

## 当前版本

- `V4.5`
- 后端：`FastAPI + Celery/Redis + LangGraph + SQLite durable checkpoint`
- 前端：`React + Vite`
- 检索模式：`low / medium / high`
- 成本统计：区分
  - `llm_cost_rmb`
  - `external_cost_usd_est`

## 核心能力

- 队列化提交：API 只负责生成 `task_id` 并入队，不阻塞主线程
- 持久化状态：任务状态、语义缓存、DLQ、LangGraph checkpoint 全部可持久化
- 三档检索模式：
  - `low`：`Serper + 轻量抓取`
  - `medium`：`Serper + Tavily Extract`
  - `high`：`Tavily Search + Map + Crawl + Extract`
- 确定性降级：
  - 坏 JSON 先走代码修复，不再让 LLM 自纠重试
  - 工具失败走有限重试、缓存/跳过/缺口说明，不再让 LLM 决策是否重试
- 会话隔离：每个任务独立 `KnowledgeManager`
- 成本可观测：任务状态里直接返回模型成本和外部检索成本

## 项目结构

```text
deepresearch-agent/
├─ core/         # LangGraph 主流程、工具、模型、知识管理
├─ gateway/      # FastAPI、Celery、状态存储
├─ frontend/     # React 前端
├─ scripts/      # benchmark、DLQ 管理、评测脚本
├─ tests/        # 回归与模式测试
├─ docs/         # AI context、架构说明
└─ reports/      # 本地 benchmark 产物（默认忽略）
```

## 三档模式

### `low`

- 目标：最低外部检索成本
- 路径：`Serper basic -> scrape_jina_ai -> add_compact_document`
- 特点：不走 Tavily 主链路，不走旧的长文 `aadd_document()` 抽取链

### `medium`

- 目标：质量/成本平衡
- 路径：`Serper basic -> Tavily Extract basic -> add_extracted_chunks`
- fallback：`scrape_jina_ai -> add_compact_document`
- 说明：这是当前默认模式

### `high`

- 目标：更高覆盖率与更强证据质量
- 路径：`Tavily Search advanced -> Map -> Crawl -> Extract advanced`
- fallback：`crawl raw_content` 或 `scrape_jina_ai`，必要时 `visual_browse`
- 说明：当前不接 `Tavily Research`

## API

### `POST /research`

请求体：

```json
{
  "query": "DeepSeek R1 vs OpenAI o1 reasoning differences and 2025 compute cost",
  "research_mode": "medium"
}
```

### `GET /research/{task_id}`

返回任务状态与成本摘要，包括：

- `research_mode`
- `llm_cost_rmb`
- `external_cost_usd_est`
- `serper_queries`
- `tavily_credits_est`
- `elapsed_seconds`

## 环境变量

最少需要：

```env
OPENAI_API_KEY=...
SERPER_API_KEY=...
TAVILY_API_KEY=...
```

说明：

- 项目内部把 `OPENAI_API_KEY` 当作 SiliconFlow/OpenAI 兼容入口使用
- `SERPER_API_KEY` 用于 `low` 和 `medium` 的主搜索链路
- `TAVILY_API_KEY` 用于 `medium/high` 的抽取与高阶检索链路

可选：

```env
REDIS_BROKER_URL=redis://localhost:6379/0
REDIS_RESULT_BACKEND=redis://localhost:6379/1
DEFAULT_RESEARCH_MODE=medium
MAX_TASK_DURATION_SECONDS=300
MAX_TASK_NODE_COUNT=15
MAX_TASK_RMB_COST=1.0
```

## 本地启动

### 1. 安装 Python 依赖

```bash
pip install -r requirements.txt
```

### 2. 启动 Redis 和 Celery Worker

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
npm install
npm run dev
```

## 验证命令

```bash
python verify_modules.py
pytest tests/test_research_modes.py -q
pytest tests/test_review_logic.py -q
pytest tests/test_chart_integration.py -q
```

## 成本基准

基准脚本：

```bash
python scripts/benchmark_modes.py
```

为了避免一次性烧穿余额，脚本内置双层熔断：

- `BENCHMARK_MAX_TASK_RMB`
- `BENCHMARK_MAX_TOTAL_RMB`

并支持：

- `BENCHMARK_QUERY_LIMIT`
- `BENCHMARK_MODES`

示例：

```bash
BENCHMARK_MAX_TASK_RMB=0.60
BENCHMARK_MAX_TOTAL_RMB=3.00
BENCHMARK_QUERY_LIMIT=1
python scripts/benchmark_modes.py
```

## 当前结论

- 默认推荐 `medium`
- `low` 最省外部检索成本，但不一定最快
- `high` 的模型成本仍可控，但 Tavily 外部检索成本明显更高
- 全量 benchmark 必须带预算熔断，不建议裸跑

## 文档

- 架构上下文：[docs/AI_CONTEXT.md](docs/AI_CONTEXT.md)
- 本轮改造说明：[docs/architecture_alignment_v44.md](docs/architecture_alignment_v44.md)
- 面试架构表述：[docs/INTERVIEW_GRAPH_ARCHITECTURE.md](docs/INTERVIEW_GRAPH_ARCHITECTURE.md)
