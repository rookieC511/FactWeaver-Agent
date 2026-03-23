# DeepResearch-Agent 当前项目架构与实现综述

## 1. 项目定位

`deepresearch-agent`（仓库内产品名也以 `FactWeaver-Agent` 出现）是一个面向长报告研究任务的深度研究系统。  
它的核心目标不是只回答单句 QA，而是围绕一个开放式问题完成：

- 研究任务规划
- 多轮检索与证据获取
- 来源质量筛选
- 证据门控与可追溯性校验
- 长报告生成
- 任务状态持久化、恢复与评测

当前系统已经从早期的“单条强 workflow”演进为双架构并存：

- `legacy_workflow`
- `supervisor_team`

其中：

- `legacy_workflow` 继续作为稳定基线与回退路径
- `supervisor_team` 是当前主实验方向，用于验证 team-based multi-agent research 架构

---

## 2. 技术栈总览

| 层级 | 主要技术 | 作用 |
| --- | --- | --- |
| API / 服务层 | FastAPI | 提供研究任务提交、查询、恢复、健康检查接口 |
| 异步执行层 | Celery + Redis | 任务队列、异步 worker 执行 |
| 状态存储 | SQLite | 任务状态、checkpoint、运行时元数据持久化 |
| 编排层 | LangGraph | 顶层状态图、legacy/supervisor 双路径编排、checkpointer |
| 研究运行时 | Python runtime modules | 检索策略、证据获取、agent-runtime 协议、research team 执行 |
| 检索层 | Serper、Tavily | 搜索、抽取、crawl、map 等外部研究能力 |
| 写作层 | LangGraph writer subgraph + LLM runtime | 长报告骨架、分节写作、审阅、修订 |
| 前端 | React + Vite | 用户交互界面 |
| 评测层 | 本地 benchmark scripts + local judge | DRB 风格评测、smoke A/B、成本/时间/质量对比 |

当前模型分工（由配置层统一管理）大致为：

- `llm_fast`：快速规划/轻量研究推理
- `llm_smart`：更强的策略与审阅判断
- `llm_worker`：写作 worker
- `llm_chief`：高质量收束/总编角色
- extractor / vision 模型：用于特定抽取或多模态场景

当前默认研究配置：

- 默认架构：`supervisor_team`
- 默认 research mode：`medium`

---

## 3. 当前系统的总体分层

从外到内看，当前项目可以分成五层。

### 3.1 服务与任务外壳

这一层主要位于：

- [api.py](/D:/workplace/Projects/deepresearch-agent/gateway/api.py)
- [executor.py](/D:/workplace/Projects/deepresearch-agent/gateway/executor.py)
- [state_store.py](/D:/workplace/Projects/deepresearch-agent/gateway/state_store.py)

职责：

- 接收 `POST /research`
- 生成 `task_id`
- 持久化初始任务状态
- 根据运行环境选择 Celery 或本地同步执行
- 控制 timeout / budget / node limit
- 负责 resume / checkpoint / DLQ / health

这一层是“确定性外壳”，主要关注：

- 任务可靠执行
- 运行边界
- 状态落库
- 恢复语义

### 3.2 图编排层

这一层由 LangGraph 承担，主要位于：

- [graph.py](/D:/workplace/Projects/deepresearch-agent/core/graph.py)
- [writer_graph.py](/D:/workplace/Projects/deepresearch-agent/core/writer_graph.py)

当前 `graph.py` 已经被收缩为“编排层”文件，主要只负责：

- `ResearchState`
- 两条顶层路径的组装：
  - `legacy_workflow`
  - `supervisor_team`
- 节点适配器（node adapter）
- phase/router 映射

LangGraph 在当前项目中的定位已经比较清晰：

- 它负责图装配与状态推进
- 它负责与 checkpoint 语义配合
- 它不再承载大段研究或写作主体逻辑

### 3.3 研究与写作运行时

这一层是当前项目最核心的业务层，主要位于：

- [research_supervisor_runtime.py](/D:/workplace/Projects/deepresearch-agent/core/research_supervisor_runtime.py)
- [research_team_runtime.py](/D:/workplace/Projects/deepresearch-agent/core/research_team_runtime.py)
- [writer_team_runtime.py](/D:/workplace/Projects/deepresearch-agent/core/writer_team_runtime.py)
- [multi_agent_runtime.py](/D:/workplace/Projects/deepresearch-agent/core/multi_agent_runtime.py)

这里承载的不是“图结构”，而是：

- supervisor 决策协议
- research team 协议
- writer team 协议
- 结构化 team result
- completion policy
- route trace
- artifact / digest / bundle 组织

### 3.4 证据获取层

这一层主要位于：

- [core/evidence_acquisition](/D:/workplace/Projects/deepresearch-agent/core/evidence_acquisition)

职责：

- 搜索 provider 调用
- authority-first recall
- qualification
- fetch pipeline
- blocked handling
- backfill
- strict evidence gate

这是当前系统里“最代码化”的一层，也是稳定性与可观测性最强的一层。

### 3.5 评测与实验层

这一层主要位于：

- [public_benchmark_deepresearch_bench.py](/D:/workplace/Projects/deepresearch-agent/scripts/public_benchmark_deepresearch_bench.py)
- [deepresearch_bench_scoring.py](/D:/workplace/Projects/deepresearch-agent/scripts/deepresearch_bench_scoring.py)
- [benchmark_scoring.py](/D:/workplace/Projects/deepresearch-agent/scripts/benchmark_scoring.py)

职责：

- DRB 风格本地评测
- smoke A/B
- 质量/成本/耗时对比
- legacy 与 supervisor 架构对比

---

## 4. 两条顶层路径

当前项目最重要的结构特点，就是同时保留了两条顶层路径。

## 4.1 `legacy_workflow`

这是一条更接近第一阶段的线性 workflow：

1. `Planner / Init Search`
2. `Deep Research Runtime`
3. `Writer Graph`
4. `Final Report`

它的特点是：

- 链路固定
- 可预测性强
- 成本较低
- 适合作为回退路径和对比基线

但它的问题也很明显：

- 策略层不够灵活
- `research -> writer` 切换缺少显式 team 语义
- 难以表达更复杂的多 agent 研究过程

## 4.2 `supervisor_team`

这是当前主要实验方向，目标是把项目演进成“Supervisor 驱动的 team-based multi-agent system”。

顶层主干可以概括为：

1. `Research Supervisor`
2. `Research Team`
3. `Research Supervisor`
4. `Writer Team`
5. `Final Report`

它的特点是：

- Supervisor 不直接做搜索/抓取
- Research Team 负责研究语义层
- Retrieval Runtime 仍然由代码控制
- Writer Team 负责写作与语义审阅

当前的架构思想不是“把所有东西都 agent 化”，而是：

- 代码负责底线、执行、边界与持久化
- agent 负责策略、优先级和语义验收

---

## 5. 为什么项目仍然在用 LangGraph

当前项目仍然在使用 LangGraph，而且是实质性使用。

主要原因：

- 已经有成熟的图式状态推进
- 已经和 SQLite checkpoint 语义结合
- `legacy_workflow` 与 `supervisor_team` 两条路径都已接在 LangGraph 上
- writer 子图本身也依赖 LangGraph 组织

但项目对 LangGraph 的定位已经发生了变化：

### 过去

更像“把大量业务逻辑直接塞在图节点里”。

### 现在

更像“把 LangGraph 作为薄编排壳”：

- 图负责 wiring
- runtime 负责业务逻辑
- state store 负责持久化
- evidence runtime 负责执行

因此，当前最准确的判断不是“还该不该用 LangGraph”，而是：

**短期内仍然值得保留，但只把它当作编排层，不应继续往里面堆主体业务逻辑。**

如果未来真的要替换，更可能的方向也不是换另一个更重的 agent 框架，而是：

- 自定义轻量 orchestrator
- 保留现有 runtime 和 state store
- 逐步把 graph 逻辑迁成显式 Python 状态机

但就当前阶段来看，还没有必要为了“换框架”而付出重构代价。

---

## 6. 关键状态与协议

第二阶段之后，项目内部已经形成了一套相对清晰的协议层。

## 6.1 `ResearchState`

这是顶层图运行时共享状态，位于：

- [graph.py](/D:/workplace/Projects/deepresearch-agent/core/graph.py)

它承载：

- query
- task metadata
- ledger
- research results
- writer results
- phase
- artifact refs
- route trace

## 6.2 `SupervisorDecision`

这是 `Research Supervisor` 的结构化输出，位于运行时协议层。

核心字段包括：

- `next_phase`
- `reason`
- `decision_basis`
- `replan_strategy`

它的作用不是简单替代 if/else，而是把 supervisor 的判断显式结构化：

- 是 coverage gap
- 还是 authority gap
- 还是 writer backfill
- 还是 stall recovery

并决定：

- 继续 research
- 切到 writing
- replan
- degrade
- fail hard

## 6.3 `RetrievalPlan`

这是 Research Team 中 `Evidence Scout Agent` 输出给代码 runtime 的桥梁。

核心字段包括：

- `target_clauses`
- `source_type_priority`
- `query_intents`
- `backfill_mode`
- `authority_requirement`
- `stop_after_slots`

它解决的是一个很关键的问题：

**agent 决定“查什么”，代码决定“怎么查”。**

## 6.4 `EvidenceDigest`

这是提供给 `Evidence Verifier Agent` 的摘要化证据输入。

它不会把完整 evidence bundle 全量塞给 verifier，而是只提供：

- `slot_statuses`
- `clause_statuses`
- `open_gaps`
- `authority_summary`
- `coverage_summary`
- `supporting_evidence_refs`
- `direct_answer_support_snapshot`

这样可以避免：

- verifier 上下文膨胀
- 证据正文二次污染
- digest 失控增长

## 6.5 `ResearchTeamResult`

Research Team 的结构化结果，包含：

- `status`
- `slot_statuses`
- `clause_statuses`
- `coverage_summary`
- `open_gaps`
- `bundle_ref`
- `recommended_next_step`
- `team_confidence`
- `verifier_decision`

## 6.6 `WriterTeamResult`

Writer Team 的结构化结果，包含：

- `draft_ref`
- `direct_answer`
- `coverage_report`
- `citation_support_report`
- `constraint_satisfaction`
- `analysis_gap`
- `needs_research_backfill`
- `output_mode`
- `unresolved_gaps_summary`

这些协议的作用是：

- 避免 team 之间靠长自然语言猜测彼此状态
- 让代码 gate 与 agent gate 分层
- 让 checkpoint / resume / benchmark 可以稳定消费统一结构

---

## 7. 检索与证据获取是怎么实现的

这部分仍然是当前系统最关键、也最工程化的部分。

## 7.1 三档 research mode

系统支持：

- `low`
- `medium`
- `high`

三档模式对应不同的检索成本、 provider 使用方式与抓取强度。

它的意义不是“质量越高越好”，而是：

- 给不同复杂度任务匹配不同成本结构
- 让 benchmark 可以公平比较
- 避免所有任务都走最贵的链路

## 7.2 检索 provider

当前主要外部研究能力来自：

- Serper
- Tavily

其中不同模式会组合使用：

- search
- extract
- map
- crawl

## 7.3 authority policy

authority policy 已经明确改成双层结构。

### 代码负责

- 定义哪些来源是 `high_authority`
- 定义哪些来源是 `weak`
- 禁止某些来源进入主证据集合
- 执行 host-level blocked / fetch / qualification policy

### agent 负责

- 当前轮次先追哪些来源
- clause 级别的 source priority
- 当前该走：
  - `authority_first`
  - `broad_recall`
  - `targeted_backfill`
  - `same_host_access_backfill`

也就是说：

- 代码定义“谁算高权威”
- agent 决定“这轮先追谁”

## 7.4 strict evidence gate

代码仍然承担最低门槛：

- slot 是否有支撑
- 高权威数量是否达标
- constraints 是否形式满足
- direct answer 是否有形式支撑

这是“形式 gate”。

而更高一层的“语义 gate”由 verifier 负责：

- 内容上是否真的答到了问题
- 虽然形式达标，但证据是否仍然空洞
- 是否应该继续 backfill

所以现在的 gate 已经不是单层提示，而是：

- 代码 gate 管形式下限
- agent gate 管语义是否真的够

---

## 8. 写作层是怎么实现的

写作层目前仍然保留 LangGraph 子图，但内部已经经历过收缩。

主要文件：

- [writer_graph.py](/D:/workplace/Projects/deepresearch-agent/core/writer_graph.py)
- [writer_team_runtime.py](/D:/workplace/Projects/deepresearch-agent/core/writer_team_runtime.py)

当前 Writer Team 的核心角色可以概括为：

- `Outliner`
- `Section Writer`
- `Report Verifier`

### Outliner

负责：

- 写作骨架
- 是否需要图表的判断

这里已经吸收了原本独立 `chart_scout` 的职责，因此：

**独立 chart scout 已不再是默认路径中的核心节点。**

### Section Writer

负责：

- 按 section 生成正文
- 结合 evidence bundle 做 synthesis

### Report Verifier

负责：

- `answer_coverage`
- `citation_support`
- `constraint_satisfaction`
- `analysis_gap`

并决定是否触发 revise。

### revise budget

writer 侧已经不再允许无限修稿循环，而是固定：

- 最多 1 轮 revise

如果 revise 后仍不满足：

- evidence 缺口型 -> `needs_research_backfill`
- 有限结论可写 -> `output_mode=degraded`
- 不安全 -> `FAIL_HARD`

这一步的意义很大：

- 限制 writer 子图深度
- 避免 wall-clock 被 writer 回路无限拉长
- 让写作失败信号更结构化

---

## 9. 状态持久化、恢复与运行边界

这是当前项目工程上比较强的一部分。

## 9.1 任务状态

系统会持久化：

- 任务基本状态
- 运行阶段
- 成本与耗时
- intermediate artifacts
- route trace

这使得：

- API 可以查询中间状态
- benchmark 可以读取结构化结果
- 崩溃后可以 resume

## 9.2 checkpoint / resume

恢复语义已经上移到 team / phase 级，而不是底层函数级。

这意味着恢复时关注的是：

- 当前处于 `RESEARCH` / `WRITE` / `REPLAN` 等哪一阶段
- 当前 ledger 是什么
- 哪个 team 的结果已经产出

而不是“某个 fetch 函数做到第几步”。

这是当前系统比较合理的方向，因为它更符合 research agent 的调度语义。

## 9.3 成本、节点与超时边界

执行器层会控制：

- 最大任务时长
- 最大节点数
- 最大预算

这些边界对当前系统非常重要，因为当前的主要失败模式之一就是：

- research 能一直继续，但不会及时收手

所以执行器的存在不是附属，而是系统稳定性的底线。

---

## 10. 当前 benchmark 与验证体系

当前评测不是单一指标，而是多层次的。

## 10.1 报告质量指标

主要是 DRB 风格本地兼容评测：

- `drb_report_score`
- `fact_score`

其中 `drb_report_score` 关注：

- comprehensiveness
- insight
- instruction_following
- readability

## 10.2 证据与检索侧指标

还会看：

- `direct_answer_support_rate`
- `authority_source_rate`
- `weak_source_hit_rate`
- `blocked_source_rate`
- `retrieval_failed`

这些指标对于当前项目尤其重要，因为项目的真实瓶颈更多在：

- 搜到什么
- 抓到什么
- 高权威证据是否足够

而不只是 writer 会不会写。

## 10.3 smoke A/B

目前已经建立：

- `legacy_workflow`
- `supervisor_team`

在冻结样本上的 smoke A/B 对比机制。

当前实验已经证明：

- `supervisor_team` 可以提升检索侧 evidence 指标
- 但仍存在 research 不会及时收口的问题
- 因此经常出现：
  - evidence 指标变好
  - 但最终报告分不涨
  - 时间和成本暴涨

这也意味着下一步优化重点不是“继续堆 agent 节点”，而是：

- 优化 `RESEARCH -> WRITE` 切换
- 优化低收益 backfill 终止条件
- 优化 supervisor 的收口判断

---

## 11. 当前代码结构概览

当前最值得记住的目录关系如下：

### `gateway/`

- `api.py`：HTTP 接口
- `executor.py`：任务执行主入口
- `state_store.py`：状态落库与读取

### `core/`

- `graph.py`：顶层 graph wiring
- `writer_graph.py`：writer 子图
- `research_supervisor_runtime.py`：Supervisor 决策运行时
- `research_team_runtime.py`：Research Team 运行时
- `writer_team_runtime.py`：Writer Team 运行时
- `multi_agent_runtime.py`：共享协议、ledger、trace、completion helpers
- `evidence_acquisition/`：检索/抽取/抓取/qualification/gate

### `scripts/`

- benchmark 与 scoring 脚本
- smoke A/B 入口

### `tests/`

- runtime contract tests
- benchmark/scoring tests
- architecture smoke 相关测试

---

## 12. 当前架构的优点

截至当前版本，这个项目的优点主要在以下几个方面。

### 12.1 工程壳比较稳

FastAPI + Celery + Redis + SQLite 的组合让系统具备：

- 可异步执行
- 可恢复
- 可查询状态
- 可做 benchmark

### 12.2 证据获取层很强

项目的真正护城河不是“有多少 agent 节点”，而是：

- authority-first retrieval
- blocked handling
- evidence gate
- 结构化 artifact

### 12.3 现在的 runtime 分层已经比过去清楚很多

`graph.py` 已经从“巨型业务文件”收缩成编排层。

### 12.4 双架构并存让 A/B 更可信

保留 `legacy_workflow` 的价值很大，因为：

- 可以随时回退
- 可以做 smoke A/B
- 不至于在架构实验里失去基线

---

## 13. 当前主要问题与技术债

虽然第二阶段已经完成了主要分层，但当前架构仍然存在几个明显问题。

### 13.1 `supervisor_team` 的收益尚未转化成最终交付

已有实验表明：

- `direct_answer_support_rate` 往往会上升
- 但 `drb_report_score` 未必同步上升

根因不是 writer 不行，而是：

- research 没有及时收口
- verifier 常常继续要求 backfill
- supervisor 没有在合适时机切入 writing

### 13.2 wall-clock 暴涨不只是 token 问题

当前 `supervisor_team` 的主要性能问题不是模型贵一点，而是：

- graph depth 变深
- research/backfill 串行链路变长
- supervisor 往返增加
- 写作前的收敛更慢

### 13.3 stall 机制对“低收益但仍在动”的状态识别还不够

现在的 stall 更容易识别“完全没进展”，但不够擅长识别：

- 还在找到零碎东西
- 但已经不值得继续研究

### 13.4 writer 子图虽然已收缩，但仍有进一步简化空间

writer 现在比早期好很多，但仍然值得继续压缩：

- 降低回路深度
- 让 verifier 输出更稳定

---

## 14. 下一步最合理的优化方向

结合目前的实验与代码结构，下一步最值得投入的不是“再加更多 agent”，而是：

### 14.1 优化 `RESEARCH -> WRITE` 的切换条件

这是当前 `supervisor_team` 最大的收口问题。

### 14.2 给低收益 backfill 增加更强的终止条件

不是所有“还能找到一点东西”的探索都值得继续。

### 14.3 继续缩短 `supervisor_team` 的控制流深度

重点是：

- 减少无意义往返
- 缩短 research 验收到 writing 的链路

### 14.4 长期方向：LangGraph 继续保留为薄壳，runtime 继续外提

如果未来要进一步降复杂度，更可能是：

- 保持当前 runtime 设计
- 减少图层负担
- 而不是立即替换成另一套更重的 multi-agent 框架

---

## 15. 一句话总结

当前 `deepresearch-agent` 已经不是简单的单体 research workflow，而是一套：

**确定性执行外壳 + LangGraph 编排层 + 代码化证据获取 runtime + team-based agent 判断层 + benchmark 驱动优化闭环**

它现在最大的挑战，不是“有没有多 agent”，而是：

**如何让已经改善的 research 证据质量，真正稳定地转化成更好的最终报告，同时把时间和成本控制下来。**
