
# Deep Research Agent - Prompt Engineering Documentation

此文档汇总了 Deep Research Agent 及其 Baseline 系统中所有核心节点的 Prompt 设计。

---

## 🚀 1. 核心流程 Prompt (Main Graph)

### 📌 Top-Level Planner (Pre-Search)
**功能**: `graph.py` -> `node_init_search`
**模型**: DeepSeek-R1
**作用**: 基于初步扫描结果 (Top-3 Search)，生成一份经用户审核的详细调研大纲。

```markdown
# [C] Context
User wants to research a complex topic: "{query}".
Current Env: {context[:2000]}
User Feedback: {feedback_context}

# [O] Objective
Break down the user's query into 4-6 atomic search sub-tasks.

# [S] Style
Logical, MECE (Mutually Exclusive, Collectively Exhaustive).

# [R] Response Format (CRITICAL)
Strict JSON format only. No markdown fences.
Target Structure: {{"plan": [{{"task": "...", "reason": "..."}}]}}

# [E] Examples (Few-Shot)
Input: "DeepSeek vs OpenAI revenue"
Output: {{"plan": [{{"task": "Search DeepSeek 2024 revenue report", "reason": "Official data"}}, {{"task": "Search OpenAI 2024 revenue breakdown", "reason": "Comparison logic"}}]}}
```

### 🕵️ Executor: Search Query Generator
**功能**: `graph.py` -> `execute_node` (Refactored)
**模型**: DeepSeek-V3 (Fast)
**作用**: 将大纲中的抽象任务转化为精准的搜索引擎关键词。

```markdown
# [C] Context
We are executing a research plan. Current Task: "{task_description}"

# [R] Role
You are a Search Engine Optimization (SEO) expert and Google Search Power User.

# [O] Objective
Generate 3-5 precise search queries to gather necessary information.

# [K] Key Constraints
1. **Keywords only**. Do not use full sentences. (Bad: "How do I find..." | Good: "DeepSeek vs OpenAI revenue 2024")
2. Use operators if helpful (site: , filetype:pdf).
3. Return JSON: {{"queries": ["query1", "query2", ...]}}

# [E] Examples
Input: "Find DeepSeek's technical report"
Output: {{"queries": ["DeepSeek-V3 technical report pdf", "DeepSeek architecture whitepaper", "DeepSeek-R1 arxiv"]}}
```

### ⚖️ Reviewer (Audit)
**功能**: `graph.py` -> `review_node` (Legacy / Concept)
**模型**: DeepSeek-R1
**作用**: 审计已有信息是否足够回答用户问题。

```markdown
问题: {state['query']}
现有信息: {context}
信息是否足够写深度报告？JSON: {{'status': 'SUFFICIENT'/'INCOMPLETE', 'critique': '...'}}
```

---

## ✍️ 2. 写作子图 Prompt (Writer Graph)

### 🧠 Writer Planner (Skeleton)
**功能**: `writer_graph.py` -> `skeleton_node`
**模型**: DeepSeek-R1
**作用**: 即使有上游大纲，Writer 内部也会尝试构建或复用一个层级化的骨架结构。

```markdown
你是专业的技术报告架构师。
任务: 为 "{state['query']}" 设计一个详细的写作大纲。

用户反馈 (如有):
{state.get('user_feedback', '无')}

背景片段:
{context_preview}

要求:
1. 结构清晰，包含 3-5 个主要章节，每个章节包含 1-2 个子章节。
2. **标题严禁包含数字编号** (例如: 不要写 "1. 引言"，只写 "引言")。章节编号仅在 JSON 的 "id" 字段中体现。
3. 返回 JSON 列表格式...
```

### 📊 Chart Scout
**功能**: `writer_graph.py` -> `chart_scout_node`
**模型**: DeepSeek-R1
**作用**: 分析数据，判断是否需要插入可视化图表。

```markdown
你是数据可视化专家。
任务: 分析以下写作大纲和背景信息，判断是否需要生成图表 (Line/Bar) 来增强报告的说服力。

大纲: {outline}
背景片段: {context_str}

要求:
1. 最多生成 1-2 个最关键的图表...
...
3. 返回 JSON 格式:
   {{
       "charts": [
           {{
               "target_section_id": "...",
               "type": "line/bar",
               ...
           }}
       ]
   }}
```

### ✍️ Section Writer (Parallel)
**功能**: `writer_graph.py` -> `section_writer_node`
**模型**: DeepSeek-V3 (Fast)
**作用**: 并行撰写各个章节，这是内容生成的**核心 Prompt**，包含了严格的引用（Citation）要求。

```markdown
# [C] Context
We have gathered raw data about "{title}".
Raw Data: {context_str}

# [O] Objective
Write a deep-dive technical analysis section for "{title}".
Description: {desc}

# [S] Style
McKinsey Report style, data-driven, MECE principle.
Structure the content logically with clear arguments.

# [T] Tone
Professional, Objective, Insightful. 
**Avoid marketing buzzwords** (e.g., "Game changer", "Revolutionary" -> use specific metrics instead).

# [A] Audience
Technical Leaders and Investors. Assume they know basic concepts but want deep insights.

# [R] Response
Format: Markdown.
Constraints:
1. **Strictly follow the title**.
2. **Citation**: Use `[citation_hash]` at the end of every factual sentence.
3. If data is missing, state it clearly.
```

### 📄 Editor (Assembler)
**功能**: `writer_graph.py` -> `editor_node`
**模型**: Python (Logic)
**作用**: 将所有生成的章节按顺序拼接，并解析文中的引用哈希 (Citation Hahs) 生成文末参考文献列表。此节点主要为**逻辑拼接**，不涉及 LLM 生成。

```markdown
(Pure Python Logic: Concatenation + Regex Citation Resolution)
```

---

## 🏆 3. 评测基准 Prompts (Baseline)

### System A: Naive RAG
**功能**: `benchmark/naive_rag.py`
**模型**: DeepSeek-R1
**作用**: 模拟简单的“查一次就写”的 RAG 系统。

```markdown
Question: {query}

Retrieved Context:
{context}

Instructions:
Answer the question based ONLY on the provided context. 
If the context is insufficient, state that you don't know.
Do not make up information.
```

### System B: ReAct Agent
**功能**: `benchmark/react_agent.py`
**模型**: DeepSeek-R1
**作用**: 模拟经典的 ReAct (Reason+Act) 循环。

```markdown
Answer the following questions as best you can. You have access to the following tools:

{tool_desc}

Use the following format:

Question: the input question you must answer
Thought: you should always think about what to do
Action: the action to take, should be one of [{tool_names}]
Action Input: the input to the action
Observation: the result of the action
... (this Thought/Action/Action Input/Observation can repeat N times)
Thought: I now know the final answer
Final Answer: the final answer to the original input question

Begin!

Question: {query}
```

### ⚖️ LLM-as-a-Judge
**功能**: `benchmark/evaluate.py`
**模型**: DeepSeek-R1
**作用**: 扮演公正裁判，打分。

```markdown
You are an impartial and expert judge evaluating three AI systems answering a complex research query.

Query: {query}

[System A: Naive RAG (Baseline)]
{baseline_resp}

[System B: Standard ReAct Agent (Strong Baseline)]
{react_resp}

[System C: Deep Research Agent (Ours)]
{agent_resp}

---

Please evaluate three systems based on the following three criteria:

1. **Breadth**: Coverage of aspects.
2. **Depth**: Technical depth and data support.
3. **Faithfulness**: Citation practices and logic.

Output Format:
{{
    "analysis": {{ ... }},
    "scores": {{ "system_a": <0-10>, ... }},
    "winner": "...",
    "reason": "..."
}}
```
