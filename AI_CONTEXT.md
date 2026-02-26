# Deep Research Agent - AI Context

This file contains critical context for AI assistants working on this project. **Read this first.**

## 1. Environment & Execution
- **Python Environment**: `agent_env`
- **Path**: `F:\Conda_Envs\agent_env` (Windows)
- **Execution Command**:
  ```powershell
  # DO NOT use 'conda run' or 'python'. Use the absolute path to avoid environment issues.
  F:\Conda_Envs\agent_env\python.exe main.py
  ```
- **Benchmark Command (Data Flywheel)**:
  ```powershell
  # Start the benchmark to generate SFT data. Running in background.
  F:\Conda_Envs\agent_env\python.exe run_benchmark.py
  ```
- **Working Directory**: `D:\Projects\deepresearch-agent`

## 2. Project Status
- **Current Phase**: Phase 4 Complete (Report Generation + Charts).
- **Localization**: Simplified Chinese (Output, Logs, Reports).
- **Key Features**:
  - **Interactive Review**: User can modify the outline during execution via console input.
  - **Chief Editor Synthesis**: `writer_graph.py` features a Chief Editor node (`editor_node`) powered by LLM to rewrite fragmented drafted sections into highly cohesive, professional academic reports with guaranteed `Abstract`, `Conclusion`, and precise `[hash]` citation preservation.
  - **Charts**: Uses `matplotlib`/`seaborn` with Chinese font support (`SimHei`).
  - **Browsing**: Uses `Jina` and `GLM-4V` for visual browsing. Supports automatic screenshot verification and insertion.

## 3. Key Files Structure
- `main.py`: Entry point. Sets up asyncio loop for Windows.
- `graph.py`: Main state graph (Planner -> Executor -> Reviewer).
- `writer_graph.py`: Sub-graph for writing reports (Skeleton -> Human Review -> Writers -> Charts).
- `charts.py`: Chart generation logic. **Crucial**: Contains Chinese font config.
- `docs/`: Contains development logs and artifacts.
- `public/charts/`: Output directory for generated charts.
- `legacy/`: Archived scripts and logs.

## 4. Critical Technical Details
- **Image Paths**: When embedding images in Markdown, use **relative paths** (e.g., `./public/charts/filename.png`) so they render in VS Code.
- **Chinese Fonts**: `charts.py` sets `plt.rcParams['font.sans-serif'] = ['SimHei', ...]` to prevent "Tofu" characters.
- **Interactive Input**: The `human_review_node` in `writer_graph.py` uses `input()` to capture user feedback.

## 5. Interaction Rules
- **Language**: All communication with the user (chat, notifications, etc.) MUST be in **Simplified Chinese (简体中文)**. Documentation and code can remain in English where standard.
- **Memory**: Always record key architectural decisions and completed tasks in this file or `task.md`.

## 6. Development Log (Condensed)

### [2026-02-03] Evaluation & Testing
- Built local QA evaluation harness using `pytest` and `deepeval`.
- Verified "Real Agent" functionality with `verify_llm.py` (DeepSeek-V3 + Tavily).

### [2026-02-14] GAIA & Optimization
- Fixed `UnicodeEncodeError` and `asyncio` loop issues on Windows.
- **GAIA Real Benchmark**: Passed 5/5 Level 2 tasks (Paper Search, Zip Code, Calc, Code, Mollusk).
- **Stress Test**: Passed LongBench (MultiNews, 180k tokens) with "Distributed Planning & Writing".

### [2026-02-16] Data Flywheel & Llama-3 SFT
- **Objective**: Generate high-quality SFT data (Llama-3 Format) via `run_benchmark.py`.
- **Optimizations**:
  - **CoT**: Wrapped Planner thoughts in `<|begin_of_thought|>`.
  - **Intent**: Annotated search queries with intent descriptions.
  - **No-Data-Left-Behind**: Saving all trajectories (`score=None`).
- **Fixes**: Resolved `ImportError` (path conflict) and `browser-use` dependency issues.
- **Status**:
  - Benchmark **COMPLETE**.
  - All 100 official tasks generated.

### [2026-02-22] SFT Data Curation & Citation Preservation
- **Objective**: Generate and filter high-quality SFT samples (DPO pos/neg pairs) via `local_judge.py` using SiliconFlow (DeepSeek-V3.2).
- **Issue**: Initial Chief Editor prompt generated high-quality structure but stripped explicit Markdown URLs (`[source](https...)`), only leaving `[hash]` labels which dropped link density.
- **Fixes**:
  - **Context Augmentation**: Modified `section_writer_node` in `writer_graph.py` to pass the actual URL to the writer LLM, instructing it to strictly output standard markdown links `[Source Name](URL)`.
  - **Prompt Hardening**: Heavily emphasized the `PRESERVE ALL URLs AND CITATIONS` rule in the `editor_node` prompt.
  - **Fallback Scripting**: Added a regex fallback in `editor_node` to globally extract `[text](url)` from the raw drafted sections and automatically append them to `## 4. References` if the LLM drops them.
- **Result**: Successfully ran an end-to-end `test_single_generation.py` using real Serper search. Scored 9.0/10.0 from the local LLM Judge with perfect citation density and Markdown URL retention. Ready for large-scale data generation.
- **Agent LLMOps 4道防线 SOP**: 构建了严密的评测拦截防线以告别“脚本时代”黑盒抓瞎：
  - **防线一：冒烟测试**。新增 `data/prompt_data/smoke_query.jsonl` 作为黄金测试集。并在 `writer_graph.py` 内部节点打印 `[Writer x 提纯结果]` -> `[Chief Editor 最终合成]` 的明确链路和字符统计。
  - **防线二：质量门禁 (Quality Gate)**。在 `run_benchmark.py` 运行时直接引入本地打分 `get_llm_structure_score`，开启熔断机制。如连续 3 题得分 < 6，脚本级立刻 `sys.exit()`，节省 API 花费。
  - **防线三：状态隔离**。对运行产生的数据进行版本控制。废旧脏数据打上 `submission_v1_stitched_bad.jsonl` 标签隔离。最新加入 Chief Editor 后的高质量数据输出为 `submission_v2_chief_editor.jsonl`。
  - **防线四：Prompt Unit Test**。新增 `test_editor.py` 测试节点对恶意重复数据的去重能力与 Markdown URL 的健壮性保留。

### [2026-02-23] Onyx 架构逆向工程深度分析

**背景**: 对 DeepResearch Bench 榜单第一的 Onyx 系统进行了深度源码剖析（核心文件：`dr_loop.py` 和 `research_agent.py`）。完整分析报告见 `_references/onyx/`（克隆的 Onyx 原始仓库，已加入 `.gitignore`）。

**核心架构三大发现**:

1. **无复杂图引擎** - Onyx 用纯 Python `for/while` 循环实现所有编排，完全不依赖 LangGraph 等框架。循环上限为 8 次 (Orchestrator) / 8 次 (Sub-Agent)，同时有严格的墙上时钟超时（Orchestrator: 30 分钟, Sub-Agent: 12 分钟）作为兜底。

2. **LLM Chat History 即 State** - 不维护复杂 State Dict。所有 Sub-Agent 返回的事实块（Fact Blocks）直接作为 `TOOL_CALL_RESPONSE` 消息 `.append()` 到主 Orchestrator 的对话历史中。依赖现代模型 128k+ 上下文窗口（入口处硬性过滤 `< 50000 token` 的模型）。Sub-Agent 的输出限制在 10000 token 以内。

3. **完美 Map-Reduce 隔离** - 多个 Sub-Agent (Map) 并行在各自的 `while` 循环内搜索网页，只向外吐出"无格式纯事实清单"（带引用标记）。当所有 Sub-Agent 完成或 Orchestrator 调用 `GENERATE_REPORT_TOOL` 时，独立的 Chief Editor (Reduce) 接管，拿到全局大纲 + 所有事实块，在 5 分钟超时 / 20000 token 额度内合成最终报告。

**Skills 机制（Onyx 的另一条产品线）**: Onyx 的 `skill` 不用于 Deep Research，而是用于其 OpenCode 沙盒智能体。每个 Skill 是一个带 YAML frontmatter 的 `SKILL.md` 文件，由 `agent_instructions.py` 扫描后拼接进全局 `AGENTS.md` System Prompt。本质是"详细人类操作手册"直接注入 LLM 上下文。

**对我们 LangGraph 架构的三条行动建议 (已修正)**:
1. ✅ 废弃传递原始文档，改传轻量 Facts（**已完成**: `section_writer_node` 已在节点内部消化文档，只向 State 吐出纯文本事实块）。
2. ⚠️ `while + time.monotonic()` 应只用于**节点内部**的密集重试循环，不应替展图级别的 Conditional Edge。我们的 LangGraph Conditional Edge 已经是实现宏观编排能力的最佳方式（支持 Checkpoint、可视化、状态层隔离）。
3. ❌ **不适用**编成 BaseMessage History。我们的 `sections: Annotated[Dict, operator.ior]` 支持多个 writer 并行 Fan-out/Fan-in，Onyx 的单线程架构做不到这一点。我们的并行写作架构更先进，不应倒退。

### [2026-02-23] Long Context RAG & Cost Optimization
- **RAG Architecture Upgrade**: 
  - Deprecated `Qdrant` vector storage for document chunking.
  - Implemented a "Long Context Fact Extraction" approach in `memory.py` using `fact_blocks`. Full scraped text is now fed to an LLM to distill high-density summary blocks (`Markdown` with preserved citations) directly.
- **Scraper Robustness (Phoenix Protocol)**:
  - Enhanced `scrape_jina_ai` in `tools.py` with a fallback mechanism using `requests` and `BeautifulSoup` to parse `<p>`, `<h1>` tags locally when Jina hits 403/429 errors or returns empty content (e.g., Bloomberg, McKinsey).
- **Cost Control & API Routing**:
  - **Token Compression**: Implemented strict 25,000 character truncation (contains executive summaries/key data) and regex whitespace removal in `memory.py` before sending HTML text to the LLM.
  - **Lightning API / Local Routing**: Recognized "Fact Extraction" as a "Blue-Collar Task". Swapped the expensive `DeepSeek-V3` model for a blazing-fast local `llama3.1:latest` via `Ollama` (`MODEL_EXTRACTOR` in `config.py`) to reduce fact extraction costs to practically $0 while maintaining <10s inference speed for 15k+ context.

### [2026-02-23] High-Low LLM Routing (模型分工路由表)

> [!IMPORTANT]
> 以下为当前生效的模型分工表，所有节点对应的模型实例定义在 `models.py`，模型名称配置在 `config.py`。

| 角色 | 变量名 | 模型 | 用途 | 成本 |
|------|--------|------|------|------|
| 🧠 Planner | `llm_smart` | `deepseek-ai/DeepSeek-R1` | 拆解搜索任务、生成大纲 | API (SiliconFlow) |
| 📖 Fact Extractor | `llm_extractor` | `llama3.1:latest` (Ollama) | 阅读网页全文，提取数据事实 | **$0 (本地)** |
| ✍️ Section Writer | `llm_worker` | `pro/zai-org/glm-4.7.online` | 并行撰写各章节初稿 | API (SiliconFlow, 低价) |
| 👔 Chief Editor | `llm_chief` | `deepseek-ai/DeepSeek-R1` | 最终合成全文、润色引用 | API (SiliconFlow) |
| 👁️ Vision | `llm_vision` | `Pro/zai-org/GLM-4.7` | 图像分析 | API (SiliconFlow) |
| ⚡ Fast (Legacy) | `llm_fast` | `deepseek-ai/DeepSeek-V3.2` | 通用快速任务 (备用) | API (SiliconFlow) |

---

## ⚠️ 强制同步规则 (Mandatory Sync Rule)

> [!CAUTION]
> **任何 AI 助手在对本项目做出关键性改动时（包括但不限于：模型切换、架构调整、新增/删除核心模块、API 端点变更、Prompt 模板修改），必须将变更摘要同步追加到本文件 (`AI_CONTEXT.md`)。**
> 这是本项目的唯一权威上下文档案 (Single Source of Truth)，用于跨会话记忆传递。未同步的改动视为未完成。
