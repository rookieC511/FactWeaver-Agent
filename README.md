# 🔍 FactWeaver-Agent: Industrial-Grade Deep Research System

[![LangGraph](https://img.shields.io/badge/LangGraph-Multi--Agent-blue)](https://python.langchain.com/docs/langgraph)
[![Cloud Native](https://img.shields.io/badge/Cloud_Native-SiliconFlow-blue)]()
[![Cost Optimization](https://img.shields.io/badge/API_Cost--70%25-green)]()
[![V3.0 Release](https://img.shields.io/badge/V3.0-Production_Ready-purple)]()

> A production-ready Multi-Agent Deep Research framework built on LangGraph. Fully evolved into **V3.0 Cloud-Native Architecture** featuring 10K chunking Map-Reduce and SiliconFlow dynamic routing, boosting information density by 181% and dramatically reducing end-to-end latency.
>
> 专为解决复杂长文本研判而生的工业级多智能体深度调研系统。全量演进至 **V3.0 云原生架构**，10K Map-Reduce 并发提取与 SiliconFlow 算力路由将信息密度暴增 181%，Writer 召回率提升 186%，性能全面碾压本地模型且彻底反超原文直存方案。

## 🎯 The Problem We Solve (核心痛点)

In traditional Deep Research tasks, typical Agent architectures fail due to three critical flaws:
1. **Logical Fragmentation (逻辑断层):** Vector DBs (like Qdrant) slice long articles, destroying the global context.
2. **Astronomical Costs (天价账单):** Feeding entire raw HTML DOMs into closed-source models (GPT-4/DeepSeek-V3) causes token explosions.
3. **Fragility (系统脆弱):** A single anti-scraping block can crash the entire workflow.

## 🚀 Core Architecture (核心架构突破)

FactWeaver-Agent adopts a **Planner-Actor-Critic** topology via LangGraph, with several industrial-grade optimizations:

* **Hybrid Map-Reduce Extraction (并发切片提纯):** Long documents are split into 10K-char chunks and processed concurrently using a Semaphore(4) controlled Map-Reduce pipeline. This achieves robust Needle hit rates and cuts extraction time by over 37%.
* **Dynamic Size-Model Routing (大小模型算力路由):**
  Heavy-lifting extraction and parallel drafting tasks are intelligently routed to fast, cost-effective APIs (like GLM-4.7/DeepSeek-V3 via SiliconFlow). Only the Chief Editor node utilizes the premium DeepSeek-R1 for complex reasoning and synthesis, striking the perfect balance between speed and quality.
* **Phoenix Crawler Fallback (不死鸟兜底机制):**
  Primary fetching via Jina AI. If blocked or timed out, it instantly falls back to a custom `BeautifulSoup` scraper, ensuring 100% data flow continuity.
* **Self-Correction & Traceability (防幻觉溯源):**
  The Critic Node enforces a strict "No Citation, No Generation" policy. If logic breaks, the system triggers an automatic Replanning state.

## 🛠️ Tech Stack (技术栈)

- **Orchestration:** LangGraph (State Machine & Time Travel)
- **Memory Engine:** 10K Map-Reduce Fact Extraction (`memory.py`)
- **Cloud Routing Node:** SiliconFlow APIs (DeepSeek-R1, DeepSeek-V3, GLM-4.7)
- **Extraction & Fallback:** Jina AI, BeautifulSoup, Regex Cleaning
- **Eval Pipeline:** Automated local judging aligned with DeepSearchQA standards.

## ⚡ Quick Start (极速启动)

1. Clone the repository & Install dependencies
```bash
git clone https://github.com/rookieC511/FactWeaver-Agent.git
cd FactWeaver-Agent
pip install -r requirements.txt
```

2. Configure your Environment Variables (`.env`)

```env
SILICONFLOW_API_KEY=your_key
OPENAI_API_KEY=your_key
JINA_API_KEY=your_key
```

3. Run the Evaluation or Benchmark

```bash
python run_benchmark.py
# Or run main flow: python main.py
```

## 📊 Benchmark Results: Evaluation Progression

### V1.0 → V2.0 (Rolling Snapshot Architecture)
| Metric | V1.0 (单次截断) | V2.0 (滚动快照) | Improvement |
|--------|-----------------|-----------------|-------------|
| **Recall** | 23.7% | **65.7%** | **+177% 🚀** |
| **Needle Hit Rate** | 1/5 (20%) | **4/5 (80%)** | +300% |

### V2 Local → V3.0 Cloud-Native (Extracted Fact Quality)
| Metric | V2 Local (Ollama Llama-3.1) | V3 Cloud (SiliconFlow) | Improvement |
|--------|----------------------------|------------------------|-------------|
| **Information Density** | 0.710 | **1.999** | **+181% 🚀** |
| **Writer Recall** | 16.7% | **47.8%** | **+186% 🚀** |
| **Map Extraction Time** | 42.5s | **26.9s** | **-37% ⏱️** |

> Performance measured explicitly through end-to-end evaluation scripts (`eval/v0_vs_v22_deep.py` & benchmark flows) comparing factual preservation against absolute truth texts.

## 📈 Roadmap (后续计划)

* [x] V2.0 Rolling Snapshot Compression (Recall +177%)
* [x] V2.1 Latency Optimization (Explored 10K Seq & Map-Reduce)
* [x] V2.2 Robust & Intelligent Slicing (10K Map-Reduce Concurrent Strategy)
* [x] V3.0 Cloud-Native Evolution (Migrated to SiliconFlow APIs)
* [x] DeepSearchQA authoritative benchmark alignment
* [ ] V4.0 MCP-Native Architecture (Standardized browser tool-calling via Model Context Protocol)
* [ ] Human-in-the-loop Checkpoint Integration

---

*Built with architecture in mind. Pull requests are welcome.*
