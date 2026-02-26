# Deep Research Agent 项目报告

## 1. 项目概述
**Deep Research Agent** 是一个基于多智能体架构的深度研究系统，旨在通过自动化规划、信息检索、视觉浏览和并行写作，生成高质量的专业研究报告。本项目融合了最新的大模型推理能力（DeepSeek-R1, V3）与多模态视觉能力（GLM-4V），解决了传统 Agent 在复杂信息整合和视觉理解方面的短板。

## 2. 核心架构与逻辑实现

系统采用 **LangGraph** 构建状态机，分为主图（Research Graph）和子图（Writer Graph），实现了从问题解析到最终报告生成的全流程闭环。

### 2.1 主流程：研究闭环 (Research Loop)
主图包含四个核心节点，形成 "规划-执行-评估" 的动态循环：

1.  **Planner (规划者)**
    *   **模型**: DeepSeek-R1 (High Temp)
    *   **逻辑**: 利用 R1 的深度思考能力，分析用户 Query，将其拆解为 2-3 个具体的搜索关键词。如果处于迭代中，会根据 Critique（批评意见）调整搜索方向。

2.  **Executor (执行者)**
    *   **混合检索**: 使用 **Tavily API** 进行广度搜索。
    *   **价值评估**: 使用 DeepSeek-V3 对搜索结果进行打分，筛选出最有技术深度的链接（Top 3）。
    *   **分级浏览策略**:
        *   **Level 1 (Jina Reader)**: 优先使用 API 抓取纯文本，速度快、成本低。
        *   **Level 2 (Visual Browser)**: 若遭遇反爬虫或内容为空，自动切换至 **Browser-Use + GLM-4V**。启动真实 Chromium 浏览器，模拟真人滚动页面，并调用视觉模型解析截图数据。

3.  **Reviewer (审计员)**
    *   **模型**: DeepSeek-R1
    *   **逻辑**: 检索知识库（Qdrant + BGE-M3），评估当前信息是否足以回答用户问题。若信息不足（INCOMPLETE），触发下一轮搜索；若足够（SUFFICIENT），进入写作阶段。

4.  **Memory (记忆系统)**
    *   **架构**: Qdrant 向量数据库。
    *   **功能**: 自动去重、分块存储、Citation Hash（引用哈希）生成，为写作提供准确的参考文献索引。

### 2.2 子流程：并行写作 (Writer Sub-Graph)
写作阶段采用 **Skeleton-of-Thought (SoT)** 范式，大幅提升生成速度与长文连贯性：

1.  **Skeleton Generator (骨架生成)**: DeepSeek-R1 生成层级化大纲，并支持 **Human-in-the-loop (交互式评审)**，允许用户在控制台实时修改大纲。
2.  **Chart Scout (图表侦察)**: 扫描大纲，识别数据可视化机会，调用 `matplotlib/seaborn` 自动生成图表（支持中文）。
3.  **Section Writers (并行写手)**: 多个 DeepSeek-V3 实例并发撰写不同章节。
    *   **RAG 增强**: 每个章节独立检索相关的知识库片段。
    *   **视觉融合**: 若知识片段中包含 `[SNAPSHOT_PATH]`（来自视觉浏览器的截图），会自动将相关截图插入报告。
4.  **Editor (编辑)**: 拼装各章节，解决格式问题，生成最终参考文献列表。

## 3. 关键技术迭代过程

本项目的开发经历了四个主要阶段的迭代与优化：

### Phase 1: 基础链路构建 (Baseline)
*   建立基于 LangGraph 的基础循环架构。
*   集成 DeepSeek 系列模型，实现文本检索与基本的报告生成。
*   **痛点**: 无法处理反爬虫严重的网站，中文乱码，报告结构单一。

### Phase 2: 本地化与工程化 (Localization & Engineering)
*   **中文化**: 全面汉化 Prompt 与日志系统，确保 Agent 用中文思考和交互。
*   **图表支持**: 解决 Matplotlib 中文显示方块（Tofu）问题，引入 `SimHei` 字体支持。
*   **人机交互**: 在大纲生成阶段引入 `input()` 断点，允许用户干预写作大纲。

### Phase 3: 视觉能力增强 (Visual Intelligence)
这是最具突破性的迭代：
*   **引入 Browser-Use**: 集成 `browser-use` 库，赋予 Agent 操控真实浏览器的能力。
*   **GLM-4V 多模态**: 利用 GLM-4V 的视觉理解能力，不仅能“看”懂页面，还能通过 Prompt 工程（"智能滚动"）获取完整页面信息。
*   **所见即所得 (WYSIWYG)**:
    *   开发了 **智能截图选择逻辑**：遍历浏览器操作历史，自动选取内容最丰富（最大）的截图。
    *   打通数据链路：将截图路径通过 `[SNAPSHOT_PATH]` 标签传递给 Writer，实现了报告中自动配图的功能。

### Phase 4: 稳健性与自测 (Robustness)
*   **混合浏览回退**: 实现了 `Jina -> Visual Browser` 的自动 Fallback 机制。
*   **Mock 测试框架**: 编写 `test_visual_pipeline.py` 等脚本，通过 Mock 外部依赖，验证了图片插入逻辑的正确性。
*   **错误恢复**: 增强了 JSON 解析的鲁棒性，修复了 R1 模型偶尔返回非 JSON 格式导致的崩溃问题。

## 4. 总结与展望
Deep Research Agent 目前已具备较强的自主研究能力，能够在中文语境下完成复杂的行业调研任务。特别是视觉浏览与截图插入功能的实现，使其超越了纯文本 Agent 的限制，能够捕捉网页中的视觉信息。未来可进一步探索 **多图表联动分析** 及 **更复杂的网页交互**（如登录后抓取）。
