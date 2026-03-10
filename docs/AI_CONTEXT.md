# Deep Research Agent - AI Context

This file contains critical context for AI assistants working on this project. **Read this first.**

## 0. 项目目录结构 (Project Structure)

```
deepresearch-agent/
├── core/                       # 核心运行时
│   ├── __init__.py
│   ├── graph.py                # 主状态图
│   ├── writer_graph.py         # 写作子图
│   ├── memory.py               # 知识库 + OCC
│   ├── tools.py                # 工具链 + 异常
│   ├── models.py               # LLM 实例
│   ├── config.py               # 环境配置
│   └── charts.py               # 图表生成
│
├── gateway/                    # 云原生网关层
│   ├── __init__.py
│   ├── api.py                  # FastAPI 入口
│   ├── celery_app.py           # Celery 配置
│   └── tasks.py                # Celery 任务
│
├── scripts/                    # 工具脚本
│   ├── run_benchmark.py        # 官方 Benchmark 运行器
│   ├── run_eval.py             # 三维评测
│   ├── evaluate_dsqa.py        # DeepSearchQA 评测
│   ├── local_judge.py          # 本地 LLM 裁判
│   ├── fetch_real_data.py      # 数据集下载
│   └── training_recorder.py    # SFT 轨迹录制
│
├── tests/                      # 测试
├── eval/                       # 评测结果
├── data/                       # 数据文件
├── scores/                     # 评分输出
│
├── docs/                       # 文档
│   ├── AI_CONTEXT.md           # 权威上下文 (本文件)
│   ├── PROMPTS.md              # Prompt 模板
│   └── INTERVIEW_GRAPH_ARCHITECTURE.md
│
├── reports/                    # 生成的研究报告
├── legacy/                     # 遗留代码
├── benchmark/                  # 基准对比
│
├── main.py                     # CLI 入口
├── verify_modules.py           # 模块验证
├── README.md
├── .env
└── .gitignore
```

> [!IMPORTANT]
> 所有模块使用**包内绝对导入**：`from core.tools import ...`、`from gateway.celery_app import ...`。
> `scripts/` 内脚本通过 `sys.path.insert(0, project_root)` 指向项目根目录。

## 1. Environment & Execution
- **Python**: `F:\Conda_Envs\agent_env\python.exe`（**必须用绝对路径**，不要用 `conda run` 或 `python`）
- **Working Directory**: `D:\Projects\deepresearch-agent`
- **运行**: `F:\Conda_Envs\agent_env\python.exe main.py`
- **Benchmark**: `F:\Conda_Envs\agent_env\python.exe run_benchmark.py`

## 2. Project Status
- **Phase**: V3.0 Cloud-Native Evolution Complete (Production Ready)
- **Localization**: Simplified Chinese (输出、日志、报告)
- **GitHub**: [rookieC511/FactWeaver-Agent](https://github.com/rookieC511/FactWeaver-Agent) (Public, `main` branch)

## 3. Key Files
| 文件 | 职责 |
|------|------|
| `main.py` | 入口，Windows asyncio 兼容 |
| `core/graph.py` | 主状态图 (Planner → Executor → Reviewer) |
| `core/writer_graph.py` | 写作子图 (Skeleton → Human Review → Writers → Chief Editor → Charts) |
| `core/charts.py` | 图表生成，含中文字体配置 (`SimHei`) |
| `core/memory.py` | Long Context Fact Extraction（已弃用 Qdrant，改用 LLM 直接提取事实块） |
| `core/tools.py` | 搜索/爬虫，含 Jina + BeautifulSoup Phoenix 降级 |
| `core/models.py` | 所有 LLM 实例定义 |
| `core/config.py` | 模型名称与 API 配置 |
| `gateway/api.py` | FastAPI 异步网关 |
| `gateway/celery_app.py` | Celery + Redis 队列配置 |
| `gateway/tasks.py` | Celery 任务定义 + DLQ 降级 |

## 4. Critical Technical Details
- **Image Paths**: Markdown 中用**相对路径** (`./public/charts/filename.png`)
- **Chinese Fonts**: `charts.py` 设 `plt.rcParams['font.sans-serif'] = ['SimHei', ...]`
- **Interactive Input**: `writer_graph.py` 的 `human_review_node` 使用 `input()` 交互

## 5. Interaction Rules
- **语言**: 与用户的所有沟通**必须使用简体中文**。代码和文档可保持英文。
- **记忆同步**: 关键架构决策必须追加到本文件。

## 6. LLM Routing (模型分工路由表)

> [!IMPORTANT]
> 模型实例定义在 `models.py`，名称配置在 `config.py`。

| 角色 | 变量名 | 模型 | 用途 | 成本 |
|------|--------|------|------|------|
| 🧠 Planner | `llm_smart` | `deepseek-ai/DeepSeek-R1` | 拆解任务、生成大纲 | API (SiliconFlow) |
| 📖 Fact Extractor | `llm_extractor` | `Qwen/Qwen3.5-397B-A17B` | 10K 均衡切片 + Map-Reduce 提纯 (Tag绑点) | API (SiliconFlow) |
| ✍️ Section Writer | `llm_worker` | `deepseek-ai/DeepSeek-V3.2` | 并行撰写章节初稿 (单章节切片消费) | API (低价) |
| 👔 Chief Editor | `llm_chief` | `deepseek-ai/DeepSeek-R1` | 全文合成、润色引用 | API |
| 👁️ Vision | `llm_vision` | `Pro/zai-org/GLM-4.7` | 图像分析 | API |
| ⚡ Fast (Legacy) | `llm_fast` | `deepseek-ai/DeepSeek-V3.2` | 通用快速任务 (备用) | API |

## 7. Development Log (Condensed)

- **[02-03]** 构建本地 QA 评测 (`pytest` + `deepeval`)，验证 DeepSeek-V3 + Tavily 基本能力。
- **[02-14]** 修复 Windows `UnicodeEncodeError`/`asyncio` 问题。GAIA Benchmark 5/5 Level 2 通过；LongBench 压力测试 180k tokens 通过。
- **[02-16]** Data Flywheel: 通过 `run_benchmark.py` 生成全部 100 题 Llama-3 格式 SFT 数据（CoT 标注 + Intent 标注）。
- **[02-22]** Citation Preservation: 修复 Chief Editor 丢链接问题（Context Augmentation + Prompt Hardening + Regex Fallback）。LLM Judge 评分 9.0/10.0。构建 4 道 LLMOps 防线 SOP（冒烟测试 → 质量门禁熔断 → 状态隔离 → Prompt Unit Test）。
- **[02-23]** **Onyx 逆向分析**: 榜一系统用纯 Python 循环编排（无 LangGraph），Chat History 即 State，Map-Reduce 隔离。**我们的结论**: ✅ 改传轻量 Facts（已完成）；⚠️ 保留 LangGraph Conditional Edge 做宏观编排；❌ 不改用 BaseMessage History（我们的并行 Fan-out/Fan-in 更强）。
- **[02-23]** **架构升级**: 弃用 Qdrant → Long Context Fact Extraction (`memory.py`)；Phoenix Protocol 爬虫降级 (`tools.py`)；Fact Extraction 路由到本地 Llama-3 (成本 $0)。
- **[02-27]** **GitHub Push ✅**: 品牌名 FactWeaver-Agent。3 轮 `filter-branch` 清除历史敏感/大文件后 force push 成功。**⚠️ 教训**: Git 历史清理前先 `git rev-list --objects --all` 全量扫描，做完清单再一刀切。
- **[02-27]** **V1.0 Baseline 体检报告 ✅**: 新增独立 `eval/` 评测模块（5 组合成 Smoke Test，三维指标: Recall / Precision / Latency+VRAM）。结果: Recall **23.7%**, Needle 命中率 **1/5 (20%)**（Lost-in-the-Middle 量化确认），Precision **70%**, VRAM 峰值 **6.6GB** (RTX 4070 8GB 接近 OOM)。此数据直接驱动 V2.0 滚动窗口重构决策。
- **[02-28]** **V2.0 滚动快照压缩 ✅**: 重构 `memory.py` — `aadd_document()` 改为 4-chunk 滚动窗口提取。A/B 对比: Recall **23.7% → 65.7% (+177%)**, Needle 命中率 **1/5 → 4/5**, VRAM 峰值略降至 6.4GB。
- **[02-28]** **V2.1 延迟优化实验 ✅**: 
  - **Exp 1 (10K Seq)**: 延迟从 290s 降至 **120s**，Precision 升至 **81%**，Recall 略损 (55%)。
  - **Exp 2 (6K Map-Reduce)**: 并发提取耗时 ~80s，Needle 命中率 **100%**，但 Reduce 阶段存在幻觉风险 (Precision 56%)。
  - **结论**: 推荐 **10K Hybrid Map-Reduce (V2.2.2)** 作为最终平衡方案。
- **[02-28]** **V2.2.4 并发优化发布 ✅**: 
    - **改进**: 在均衡切片基础上放开核心并发 (`Semaphore=4`)，引入 3 次重试。
    - **效果**: Map 阶段提速 **33%~46%**；端到端平均耗时从 117s 降至 **55.7s**；Needle 命中率保持 100%。
    - **结论**: 确立了 10K 分块 + Map-Reduce + Semaphore(4) 的高性能生产配置。
- **[02-28]** **V3.0 云端 Extractor 切换 ✅**: 
    - **改动**: `models.py` 一行代码 — `llm_extractor` 从本地 Ollama (llama3.1:8B) → SiliconFlow DeepSeek-V3.2。
    - **效果**: 信息密度 0.710 → **1.999** (+181%)；Writer Recall 16.7% → **47.8%** (+186%)；Map 耗时 42.5s → **26.9s** (-37%)。
    - **结论**: 架构不变，只换引擎，全面碾压本地方案且 Writer Recall 反超原文直存 (V0)。
- **[03-01]** **V3.0 面试级防御性架构升级 ✅**:
    - **改动**: `memory.py` 和 `graph.py` 补充四大核心防御机制。
    - **1. 切片防断裂 (Chunk Overlap)**: 显式回退 500 字符保护边界上下文。优势：保留语义文脉防止截断；代价：增加部分 Token 冗余。
    - **2. 高并发 API 防雪崩 (Jittered Backoff)**: 引入带随机抖动的指数退避与 `<FETCH_FAILED>` 降级。优势：打散并发风暴，保证局部失败不崩盘；代价：可能触发长条尾延迟 (Long Tail Latency)。
    - **3. 数据血缘追踪 (Data Lineage)**: Map 强制绑定 `[Source | Chunk_ID]` 引用。优势：防幻觉强溯源；代价：提示词空间及输出 Token 占用。
    - **4. 认知熔断 (Conflict Routing)**: Reduce 不妥协原则，捕获 `[CONFLICT_DETECTED]` 并通过 LangGraph 路由回退重规划。优势：系统具备高阶自省与认知对齐能力；副作用：面临客观数据冲撞时可能有死循环 (Livelock) 风险，需配合状态机指标兜底。
- **[03-05]** **V3.1 面试级高阶工程防御落地（MapReduce 与 L2/L3 降级探针）✅**:
    - **背景**: 针对复杂 Agent 架构中的大模型合并与多模态调用成本的两大盲点进行防御性重构，**已全面转化为核心运行时代码**。
    - **5. 级联依赖声明的 MapReduce (Fan-out & Fan-in)**: 摒弃暴力的文本拼接。已修改 `writer_graph.py` 的 Worker Prompt 显式输出 `### Dependency Declaration`；并重构了 Chief Editor 装配逻辑，授权 R1 节点先解析声明，跨 Chunk 进行智能去重与上下文连接构建，解决逻辑震荡。
    - **6. 启发式 L2->L3 多模态探针 (Heuristic DOM Probes)**: 已在 `tools.py` 注入零模型消耗的 `heuristic_dom_probe()` 探针。依据标签嗅探、拦截报错、文本密度边界这三条规则主动抛出 `[VLM_REQUIRED]`；并在 `graph.py` (Executor) 建立动态路由触发 `visual_browse` L3 视觉调用，完美平衡系统 SLA 与多模态 Token 成本。
- **[03-10]** **V4.0 云原生机甲 Step 3: 自动纠错循环 (Self-Correction Loop) ✅**:
    - **背景**: 消除 Agent 因 LLM 格式崩溃或工具 HTTP 碰壁导致的全盘失败。将异常信息回传大模型，令其"自省 → 重决策"。
    - **7. LLM 格式自纠偏 (Scene A)**: `tools.py` 新增 `LLMFormatError` 异常 + `clean_json_output(strict=True)` 模式。`graph.py` 新增通用 `llm_json_self_correct()` 循环（错误栈回传 LLM，最多 2 次重格式化）。覆盖 Planner / QueryGen / Skeleton / ChartScout 四个 JSON 消费点。
    - **8. 工具碰壁智能决策 (Scene B)**: `tools.py` 新增 `ToolExecutionError` 异常，`scrape_jina_ai` 和 `SerperClient.asearch` 遇 403/429/5xx 主动抛异常。`graph.py` 新增 `llm_tool_error_decision()` → LLM 自主决策"换词重试 / 跳过 / 用缓存"三选一。
    - **向后兼容**: `strict=False` 默认值保持旧行为不变，`ToolExecutionError` 透传不影响原有 `except Exception` 兜底。
- **[03-10]** **V4.2/V4.3 极致降本与防爆架构 (Cost Control) ✅**:
    - **背景**: 修复并行 Writer 因读取全量上下文而导致的 Token 扇出爆炸问题，以及防止死循环导致的账单超支。整体链路成本从 ¥23.0 暴降至 ¥0.5-1.0。
    - **9. 标签化数据血缘 (Tag-Based Slicing)**: 贯穿全局的数据路由。Planner 指定 `section_id` → Executor 传递给 Extractor → 存入 KnowledgeManager 的 `metadata`。Writer 不再读取全局知识，而是调用 `km.retrieve(section_id=sec_id)` 拿取绝对绑定的事实切片，输入 Token 骤降 90%。
    - **10. 实时法币熔断器 (CostTracker)**: `models.py` 注入全局 LangChain BaseCallbackHandler，截获每笔 token 并计算人民币账单。`gateway/api.py` 设置 ¥1.0 物理上限，超标即强杀任务 (`FAILED`)。
    - **11. 矿工模型替换**: `llm_worker` (Writer) 从 GLM-4.7 降级为 `DeepSeek-V3.2`。`llm_extractor` 层替换为极具长文本性价比的 `Qwen3.5-397B-A17B`。

---

## ⚠️ 强制同步规则 (Mandatory Sync Rule)

> [!CAUTION]
> **任何 AI 助手在对本项目做出关键性改动时（包括但不限于：模型切换、架构调整、新增/删除核心模块、API 端点变更、Prompt 模板修改），必须将变更摘要同步追加到本文件 (`AI_CONTEXT.md`)。**
> 这是本项目的唯一权威上下文档案 (Single Source of Truth)，用于跨会话记忆传递。未同步的改动视为未完成。
## [2026-03-10] V4.4 Runtime Alignment

- API routing now prefers Redis/Celery dispatch and only falls back to local background execution when Celery is unavailable in the current environment.
- Task state, semantic cache, and DLQ records are persisted in SQLite via `gateway/state_store.py`.
- LangGraph checkpointing is now durable through `core/checkpoint.py` using a SQLite-backed saver instead of pure in-memory checkpoints.
- Session isolation is enforced through `core.memory.activate_session()` and `get_current_km()`, so graph and writer nodes use task-scoped knowledge managers.
- Step 3 no longer performs LLM self-correction loops for broken JSON or tool failures. JSON is repaired in code first; tool failures deterministically degrade to cache/skip/missing-source records.
- Final reports now append an explicit degradation appendix when evidence is missing or fallback paths were used.
- `browser_use` is now lazy-loaded inside `visual_browse()` so text-only tests and imports do not fail when the optional browser dependency is absent.

## [2026-03-10] Tavily 三档检索策略（中文摘要）

- 现有爬虫链路不能删除。
- 正确定位应改为：
  - `low` 档主路径
  - `medium` / `high` 档 fallback
  - JS-heavy、反爬页、图表页、视觉型页面的最后兜底
- 前端建议新增 `research_mode`，取值：
  - `low`
  - `medium`
  - `high`

### 档位建议

- `low`
  - 目标：最低外部检索成本、最快返回
  - 建议路径：`Serper + 当前爬虫`
  - 不主动启用 Tavily 重能力
  - 注意：如果仍保留当前 `memory.py` 的长文 `llm_extractor` 抽取链，`low` 不一定是最低总成本，只是最低检索层成本

- `medium`
  - 目标：质量与成本平衡
  - 建议路径：`Serper/Tavily Search + Tavily Extract`
  - 现有爬虫只做 fallback
  - 这档最适合用 Tavily 替换当前长文抓取后的 LLM 抽取链

- `high`
  - 目标：最高质量与来源覆盖
  - 建议路径：`Tavily Search advanced + Tavily Extract advanced + Tavily Map/Crawl`
  - `Tavily Research` 仅作为高级可选能力，不替代 LangGraph 主编排

### Tavily 功能定位

- `Search`
  - 作用：找候选来源 URL
  - 适合：召回阶段
  - 结论：可以替代一部分搜索，但不直接替代后续证据处理

- `Extract`
  - 作用：对已知 URL 抽取正文和相关片段
  - 适合：替换当前 `memory.py` 里“切块 -> 多次 `llm_extractor` -> reduce”的 retrieval-side LLM 抽取链
  - 价值：最有机会直接降成本、降延迟

- `Map`
  - 作用：站点级发现 URL，只返回链接清单，不返回正文
  - 适合：文档站、IR 站、政策站、官方站
  - 价值：先摸清站点结构，再决定抓哪些页，适合“先找目录”

- `Crawl`
  - 作用：从一个起始 URL 往下遍历，并直接把页面正文一起抽回来
  - 适合：`high` 档深度扫站
  - 价值：适合“边找链接，边拿内容”，但成本高于只做 `Map`

- `Research`
  - 作用：Tavily 托管式研究流
  - 适合：高级模式或 premium 模式
  - 结论：不建议替代我们自己的 LangGraph 主状态机

### 成本判断

- Tavily 不能去掉全部模型成本，因为 Planner、Writer、Editor 仍然要走大模型。
- 但如果用 `Tavily Extract` 替代当前 retrieval 侧的长文 map-reduce 抽取，通常可以明显降低这部分 LLM 成本。
- 因此最合理的策略是：
  - `low` 保持现有低成本链路
  - `medium/high` 用 Tavily 提升证据获取效率

### 落地方向

- API 请求增加 `research_mode`
- `core/graph.py` 按档位切换搜索 / 抽取 / fallback 策略
- `core/memory.py` 增加“外部抽取结果直接入库”接口
- 保留现有爬虫链路作为 `legacy_fallback_path`

参考：
- Tavily Search API: https://docs.tavily.com/documentation/api-reference/endpoint/search
- Tavily Extract API: https://docs.tavily.com/documentation/api-reference/endpoint/extract
- Tavily Map API: https://docs.tavily.com/documentation/api-reference/endpoint/map
- Tavily Research API: https://docs.tavily.com/documentation/api-reference/endpoint/research
- Tavily Credits & Pricing: https://docs.tavily.com/documentation/api-credits

## [2026-03-11] V4.5 三档检索模式与成本基准补齐

### 已落地实现

- 后端请求体与任务状态已新增 `research_mode`，取值为 `low | medium | high`，默认 `medium`。
- 前端已提供模式选择器，并在任务状态区展示 `research_mode`、`llm_cost_rmb`、`external_cost_usd_est`、`tavily_credits_est`。
- 语义缓存键已升级为 `normalized_query + research_mode`，避免不同档位互相命中缓存。
- `KnowledgeManager` 新增：
  - `add_compact_document(...)`
  - `add_extracted_chunks(...)`
- 三档主路径都不再依赖旧的 `aadd_document()` 长文 `llm_extractor` 抽取链，`aadd_document()` 仅保留为遗留兼容路径。

### 三档模式的当前真实行为

- `low`
  - 搜索：`Serper basic`
  - 内容获取：`scrape_jina_ai`
  - 入库：`add_compact_document(...)`
  - 说明：默认不走 Tavily，只有在文本完全不可用且检测到 `[VLM_REQUIRED]` 时才允许一次视觉兜底

- `medium`
  - 搜索：`Serper basic`
  - 内容获取：优先 `Tavily Extract basic`
  - 入库：`add_extracted_chunks(...)`
  - 失败回退：`scrape_jina_ai -> add_compact_document(...)`

- `high`
  - 搜索：`Tavily Search advanced`
  - 站点扩展：若同域名结果出现至少 2 次，则触发 `Tavily Map`
  - 深挖：当 `Map` 返回足够页面时触发 `Tavily Crawl`
  - 内容抽取：`Tavily Extract advanced`
  - 失败回退：`crawl raw_content` 或 `scrape_jina_ai`，必要时再走 `visual_browse`
  - 说明：本轮明确不接 `Tavily Research`

### 成本统计与熔断

- 任务状态已拆分记录两类成本：
  - `llm_cost_rmb`
  - `external_cost_usd_est`
- 额外记录的检索侧指标包括：
  - `serper_queries`
  - `serper_cost_usd_est`
  - `tavily_credits_est`
  - `tavily_cost_usd_est`
  - `elapsed_seconds`
- benchmark 脚本已升级为双层熔断：
  - `BENCHMARK_MAX_TASK_RMB`：单任务模型成本上限
  - `BENCHMARK_MAX_TOTAL_RMB`：整批 benchmark 的模型总成本上限
- benchmark 脚本还支持：
  - `BENCHMARK_QUERY_LIMIT`
  - `BENCHMARK_MODES`
  用于只跑部分 query 或部分档位，避免一次性烧穿预算。

### 2026-03-11 代表性真实跑测结果

本次为了控风险，只对第 1 个 benchmark query 做了三档真实实测，并启用了：

- 单任务熔断上限：`0.60 RMB`
- 批次总熔断上限：`3.00 RMB`

测试 query：

- `DeepSeek R1 vs OpenAI o1 reasoning differences and 2025 compute cost`

实际结果：

- `low`
  - `llm_cost_rmb = 0.1091`
  - `external_cost_usd_est = 0.0140`
  - `serper_queries = 14`
  - `elapsed_seconds = 794.62`

- `medium`
  - `llm_cost_rmb = 0.1406`
  - `external_cost_usd_est = 0.1000`
  - `serper_queries = 12`
  - `tavily_credits_est = 11.0`
  - `elapsed_seconds = 716.67`

- `high`
  - `llm_cost_rmb = 0.1926`
  - `external_cost_usd_est = 0.6240`
  - `serper_queries = 0`
  - `tavily_credits_est = 78.0`
  - `elapsed_seconds = 749.67`

- 本次三档合计模型成本：`0.4423 RMB`

### 当前判断

- `medium` 仍然是默认档位的最佳选择，质量/成本最平衡。
- `high` 档的模型成本仍可控，但外部检索成本明显偏高，当前不适合做默认档位。
- `low` 档虽然最省外部检索成本，但总耗时并不一定最短。
- 如果后续继续跑全量 `3 queries x 3 modes`，必须保留总额熔断，不建议去掉。
