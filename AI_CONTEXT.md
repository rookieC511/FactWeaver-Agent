# Deep Research Agent - AI Context

This file contains critical context for AI assistants working on this project. **Read this first.**

## 1. Environment & Execution
- **Python**: `F:\Conda_Envs\agent_env\python.exe`（**必须用绝对路径**，不要用 `conda run` 或 `python`）
- **Working Directory**: `D:\Projects\deepresearch-agent`
- **运行**: `F:\Conda_Envs\agent_env\python.exe main.py`
- **Benchmark**: `F:\Conda_Envs\agent_env\python.exe run_benchmark.py`

## 2. Project Status
- **Phase**: Phase 4 Complete (Report Generation + Charts)
- **Localization**: Simplified Chinese (输出、日志、报告)
- **GitHub**: [rookieC511/FactWeaver-Agent](https://github.com/rookieC511/FactWeaver-Agent) (Public, `main` branch)

## 3. Key Files
| 文件 | 职责 |
|------|------|
| `main.py` | 入口，Windows asyncio 兼容 |
| `graph.py` | 主状态图 (Planner → Executor → Reviewer) |
| `writer_graph.py` | 写作子图 (Skeleton → Human Review → Writers → Chief Editor → Charts) |
| `charts.py` | 图表生成，含中文字体配置 (`SimHei`) |
| `memory.py` | Long Context Fact Extraction（已弃用 Qdrant，改用 LLM 直接提取事实块） |
| `tools.py` | 搜索/爬虫，含 Jina + BeautifulSoup Phoenix 降级 |
| `models.py` | 所有 LLM 实例定义 |
| `config.py` | 模型名称与 API 配置 |

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
| 📖 Fact Extractor | `llm_extractor` | `llama3.1:latest` (Ollama) | 阅读网页提取事实 | **$0 (本地)** |
| ✍️ Section Writer | `llm_worker` | `pro/zai-org/glm-4.7.online` | 并行撰写章节初稿 | API (低价) |
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
- **[02-28]** **V2.0 滚动快照压缩 ✅**: 重构 `memory.py` — `aadd_document()` 改为 4-chunk 滚动窗口提取（6K 字符/块 + memory_snapshot 累积压缩）。A/B 对比: Recall **23.7% → 65.7% (+177%)**, Needle 命中率 **1/5 → 4/5**, VRAM 峰值略降至 6.4GB。零侵入 `graph.py`。

---

## ⚠️ 强制同步规则 (Mandatory Sync Rule)

> [!CAUTION]
> **任何 AI 助手在对本项目做出关键性改动时（包括但不限于：模型切换、架构调整、新增/删除核心模块、API 端点变更、Prompt 模板修改），必须将变更摘要同步追加到本文件 (`AI_CONTEXT.md`)。**
> 这是本项目的唯一权威上下文档案 (Single Source of Truth)，用于跨会话记忆传递。未同步的改动视为未完成。
