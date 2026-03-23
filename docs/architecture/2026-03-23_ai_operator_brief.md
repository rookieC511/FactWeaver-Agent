# DeepResearch-Agent AI 操作手册（当前版本）

> 目的：给后续 AI/实现代理快速接手项目使用。  
> 定位：不是产品介绍，而是“如何安全改动、如何快速定位、如何避免把系统改崩”的工程读本。

---

## 1. 一眼判断：这个项目现在是什么

这是一个 **长报告研究系统**，不是单轮 QA。

当前形态是混合架构：

- **确定性执行外壳**：API / queue / timeout / budget / state store
- **LangGraph 编排层**：`legacy_workflow` 与 `supervisor_team` 双路径
- **runtime 业务层**：Supervisor / Research Team / Writer Team 的协议与执行
- **代码化检索层**：provider 调用、fetch、authority boundary、evidence gate
- **agent 判断层**：策略、优先级、语义验收、写作收口

一句话：

**不是全代码写死，也不是全模型放开，而是 constrained autonomy（受限自治）。**

---

## 2. 关键入口（先看这些文件）

### 2.1 服务入口

- [gateway/api.py](/D:/workplace/Projects/deepresearch-agent/gateway/api.py)
  - `POST /research`
  - `POST /research/{task_id}/resume`
  - `GET /research/{task_id}`
  - `GET /dlq`
  - `GET /health`

### 2.2 执行主入口

- [gateway/executor.py](/D:/workplace/Projects/deepresearch-agent/gateway/executor.py)
  - `run_research_job_sync(...)`
  - 这里处理 timeout / node budget / cost budget / 最终状态落库

### 2.3 顶层图

- [core/graph.py](/D:/workplace/Projects/deepresearch-agent/core/graph.py)
  - 这是 wiring 层，不应再塞大段业务逻辑
  - 节点适配：
    - `node_supervisor` -> `run_supervisor(...)`
    - `node_deep_research` -> `run_research_team(...)`
    - `node_writer` -> `run_writer_team(...)`

### 2.4 runtime 核心

- [core/research_supervisor_runtime.py](/D:/workplace/Projects/deepresearch-agent/core/research_supervisor_runtime.py)
- [core/research_team_runtime.py](/D:/workplace/Projects/deepresearch-agent/core/research_team_runtime.py)
- [core/writer_team_runtime.py](/D:/workplace/Projects/deepresearch-agent/core/writer_team_runtime.py)
- [core/multi_agent_runtime.py](/D:/workplace/Projects/deepresearch-agent/core/multi_agent_runtime.py)

### 2.5 检索执行层

- `core/evidence_acquisition/*`
  - provider、fetch pipeline、qualification、authority gate、blocked handling

### 2.6 写作子图

- [core/writer_graph.py](/D:/workplace/Projects/deepresearch-agent/core/writer_graph.py)

---

## 3. 双架构模型（必须牢记）

## 3.1 `legacy_workflow`

作用：

- 稳定基线
- 回退路径
- A/B 对照组

路径（线性）：

- Planner -> Research Runtime -> Writer Graph -> Report

特点：

- 逻辑更固定
- 成本更可控
- 研究策略灵活性较弱

## 3.2 `supervisor_team`

作用：

- 当前主实验路径
- 验证 team-based multi-agent research

路径（控制平面）：

- Research Supervisor -> Research Team <-> Supervisor -> Writer Team -> Report

特点：

- supervisor 做策略判断
- research team 输出结构化研究结果
- writer team 做写作与语义审阅
- 运行代价更高，当前主要风险是收口速度

---

## 4. 代码 vs Agent 的边界（核心设计哲学）

## 4.1 代码负责（硬护栏）

- Retrieval Execution
- Fetch Pipeline
- Authority Boundary
- Budget / Timeout
- Checkpoint / Resume
- Artifact Management
- Fail / Degrade Boundary

## 4.2 Agent 负责（策略与语义）

- Coverage Gap Judgment
- Authority Gap Judgment
- Source Prioritization
- Backfill Strategy
- Semantic Sufficiency
- Research / Write / Degrade Decision

原则：

- 代码定义边界与执行底线
- agent 负责策略判断与语义验收
- 两者是协作，不是替代

---

## 5. 关键协议（改动前先看真值源）

## 5.1 `SupervisorDecision`

来自 supervisor runtime，关键字段：

- `next_phase`
- `reason`
- `decision_basis`
- `replan_strategy`

规则：

- `next_phase` 是 phase 决策真值源

## 5.2 `RetrievalPlan`

由 `Evidence Scout` 产出，关键作用：

- 不再是“自然语言建议”
- 是代码 runtime 可执行的结构化计划

常见字段：

- `target_clauses`
- `source_type_priority`
- `query_intents`
- `backfill_mode`
- `authority_requirement`

## 5.3 `EvidenceDigest`

给 `Evidence Verifier` 的摘要包，不是完整 evidence bundle。

目的：

- 控制上下文膨胀
- 保留可判定信息（slot/clause/gap/authority/ref）
- 避免把完整 artifacts 重新喂回 verifier

## 5.4 Team Results

- `ResearchTeamResult`
- `WriterTeamResult`

这两个是跨 team 的接口契约。  
任何 schema 改动都要同步测试和 benchmark 读取路径。

---

## 6. 当前默认配置（来自 config）

文件：

- [core/config.py](/D:/workplace/Projects/deepresearch-agent/core/config.py)

关键默认值（当前仓库）：

- `DEFAULT_ARCHITECTURE_MODE = supervisor_team`
- `DEFAULT_RESEARCH_MODE = medium`
- `MODEL_FAST = deepseek-ai/DeepSeek-V3.2`
- `MODEL_SMART = deepseek-ai/DeepSeek-R1`
- `MODEL_WORKER = deepseek-ai/DeepSeek-V3.2`
- `MODEL_CHIEF = deepseek-ai/DeepSeek-R1`
- `MAX_TASK_DURATION_SECONDS = 300`
- `MAX_TASK_NODE_COUNT = 15`
- `MAX_TASK_RMB_COST = 1.0`

外部检索依赖：

- `SERPER_API_KEY`
- `TAVILY_API_KEY`

---

## 7. 现在最常见的失败模式

## 7.1 质量没降，但时间/成本暴涨

典型原因：

- 研究阶段不会及时收口
- backfill 在低收益状态持续
- Research -> Write 切换太保守

## 7.2 Evidence 指标变好，但报告分不涨

这通常说明：

- retrieval 侧有改进
- 但没有转化成写作侧最终交付

## 7.3 stall 指标看起来正常，但实际仍在低效循环

当前 stall 检测更擅长发现“完全卡死”，不一定能识别“低收益持续探索”。

---

## 8. 开发与验证策略（避免过度跑 benchmark）

不要每次改动都跑 full smoke。

分层策略：

1. 默认：只跑受影响的 pytest
2. 改到 graph/runtime/team 边界：跑 quick smoke A/B
3. 大改收口/准备汇报：跑 full smoke A/B

相关脚本入口：

- [scripts/public_benchmark_deepresearch_bench.py](/D:/workplace/Projects/deepresearch-agent/scripts/public_benchmark_deepresearch_bench.py)
- [scripts/deepresearch_bench_scoring.py](/D:/workplace/Projects/deepresearch-agent/scripts/deepresearch_bench_scoring.py)

---

## 9. 改动守则（给 AI 的硬规则）

1. 先判断你在改哪个层级：wiring / runtime / acquisition / writer / executor
2. 若改协议字段，必须同步：
   - runtime 使用点
   - state merge
   - scoring/benchmark 读取
   - tests
3. 不要把新业务逻辑塞回 `core/graph.py`
4. 不要让 verifier 直接吃完整 evidence bundle
5. 保留 `legacy_workflow` 可运行，避免失去回退基线
6. 新增循环一定要有预算（次数、时间或成本）和退出条件

---

## 10. 快速定位指南（按问题找文件）

### “为什么 phase 没切到 WRITE？”

- [core/research_supervisor_runtime.py](/D:/workplace/Projects/deepresearch-agent/core/research_supervisor_runtime.py)
- [core/research_team_runtime.py](/D:/workplace/Projects/deepresearch-agent/core/research_team_runtime.py)
- [core/writer_team_runtime.py](/D:/workplace/Projects/deepresearch-agent/core/writer_team_runtime.py)

### “为什么 retrieval 行为和预期不一致？”

- [core/research_team_runtime.py](/D:/workplace/Projects/deepresearch-agent/core/research_team_runtime.py)
- `core/evidence_acquisition/*`

### “为什么任务被判失败/超时/超预算？”

- [gateway/executor.py](/D:/workplace/Projects/deepresearch-agent/gateway/executor.py)
- [core/config.py](/D:/workplace/Projects/deepresearch-agent/core/config.py)

### “为什么 benchmark 指标对不上？”

- [scripts/deepresearch_bench_scoring.py](/D:/workplace/Projects/deepresearch-agent/scripts/deepresearch_bench_scoring.py)
- [scripts/public_benchmark_deepresearch_bench.py](/D:/workplace/Projects/deepresearch-agent/scripts/public_benchmark_deepresearch_bench.py)

---

## 11. 当前阶段最重要的一句话

这个项目当前不缺“更多节点”，也不缺“更多模型调用”；  
最关键的是：**让 research 侧的证据改进，稳定转化成写作侧最终交付，并把收口时间与成本压回可控范围。**

