# 2026-03-18 项目可落地架构方向

## 目标

这份文档回答一个更实际的问题：

**在当前仓库已经具备 `FastAPI + Celery + Redis + LangGraph + SQLite + Evidence Acquisition + Writer` 的前提下，下一步最适合演进成什么架构，而不是盲目追前沿。**

## 当前项目现状

当前系统已经不是一个单纯的“调几个工具写一篇报告”的脚本，而是一套可恢复、可评测、可控成本的研究系统，当前骨架包括：

- API 外壳：`FastAPI`
- 异步执行：`Celery + Redis`
- 状态与任务持久化：`SQLite`
- 图编排：`LangGraph`
- 恢复机制：`checkpoint + task state + KnowledgeManager snapshot`
- 核心业务链：
  - `Evidence Acquisition`
  - `Writer subgraph`
  - `hard evidence gate`

当前优势：

- 已有 durable checkpoint / resume
- 已有 outbox、异步提交、状态跟踪
- 已有 retrieval / blocked / direct-answer 等指标闭环
- 已有 benchmark 驱动的优化迭代机制

当前主瓶颈：

- 不是基础设施缺失
- 不是简单的 writer 报错
- 而是高权威 evidence slot 覆盖仍不够完整

因此，下一步的架构演进重点不应该是“换一个全新的框架”，而应该是：

**把现有系统从“一个较大的图”收敛成“顶层 supervisor + 可复用子团队”的结构。**

## 推荐目标架构

推荐目标不是完全去中心化 swarm，而是：

**确定性外壳 + 分层 Supervisor + Agent-as-a-Tool 子团队 + 有限并行的 Evidence Acquisition**

可以分成四层。

### 1. 顶层：Research Supervisor

顶层 supervisor 负责：

- 维护全局任务目标
- 维护 task ledger
- 维护 progress ledger
- 决定调用哪个子团队
- 决定进入写作、继续 research、还是直接失败/恢复

它不应该直接拿所有底层工具，也不应该自己做细粒度抓取决策。

它应该只调用少数高层能力单元，例如：

- `delegate_to_evidence_team`
- `delegate_to_writing_team`
- `delegate_to_verification_team`

### 2. 中层：Evidence Acquisition Team

这是最适合演进成子团队的部分，因为当前仓库里最复杂、最容易继续演化的逻辑就在这里。

建议内部再拆成几个相对稳定的能力节点：

- `query decomposition`
- `authority-first retrieval`
- `fetch/access recovery`
- `evidence qualification`
- `coverage gate`

这里允许**有限并行**：

- clause 级并行
- domain 级并行
- host 级有限 backfill

但不建议让整个 research 路由变成全局自由并发。

### 3. 中层：Writing and Verification Team

写作和校验也应该形成一个相对独立的子图/子团队。

建议职责分离为：

- `outline / section writing`
- `editor`
- `direct answer verifier`
- `citation / fact verifier`

默认返回模式建议是：

- 只向顶层回传 `last_message` 风格结果
- 不把所有 section 级报错和草稿直接污染顶层上下文

需要调试时，再显式打开完整 trace。

### 4. 外壳：Deterministic Runtime Shell

现有系统里最不该丢掉的部分，其实是“非 LLM 控制”的工程外壳：

- outbox
- Celery/Redis queue
- timeout / retry / budget guardrail
- checkpoint / resume
- task state
- benchmark/metrics pipeline

这部分应继续由确定性代码控制，不适合让 LLM 接管。

## 为什么这是最适合当前项目的方案

### 原因 1：当前系统需要一个对最终报告负责的中心节点

你的系统交付的是：

- 一篇长报告
- 一个最终结论
- 一组 evidence gate 结果

这决定了：

- 不能让多个平级 agent 随意 handoff 到最后
- 必须有一个最终 owner

manager/supervisor 架构比 decentralized handoffs 更适合这个场景。

### 原因 2：Evidence Acquisition 已经天然像一个 team

当前仓库里最复杂、最值得继续优化的部分，不是简单写作，而是：

- 搜什么
- 抓什么
- 什么算高权威
- 什么时候 blocked
- 什么时候 evidence 不足

这本质上已经是一个小型“多角色协作子系统”，只是还没有被正式抽象成 team。

### 原因 3：你已经有了长任务系统最重要的运行时能力

很多项目在讨论多智能体时还停留在 prompt workflow，但你当前项目已经有：

- queue
- durable checkpoint
- resume
- outbox
- state store
- benchmark feedback loop

这说明下一步最该做的是**组织形态升级**，不是基础设施推倒重来。

### 原因 4：前沿的 RL/动态拓扑方案现在收益不够高

`progress.md` 里提到的 DyTopo、MetaGen、RL routing 这些方向非常有前瞻性，但当前项目阶段还不适合直接落地为主架构：

- 调试太难
- 成本太高
- 解释性变差
- 对当前 evidence-driven research 任务，收益未必高于 supervisor + subteams

## 不建议当前阶段做的事

### 1. 不建议把主链路改成完全去中心化 swarm

原因：

- 最终报告 ownership 不清晰
- evidence gate 难做
- 成本不可控
- 恢复和调试复杂度陡增

### 2. 不建议为了“前沿”引入 RL 路由训练

原因：

- 当前主问题仍然是业务逻辑质量，不是路由 policy 学不到
- benchmark 样本量、在线反馈量都不足以支撑高质量 RL 闭环

### 3. 不建议先上协议层分布式再补本地架构

比如 A2A/MCP 大规模互联更适合系统成熟之后的外部集成阶段，而不是当前仓库的第一优先级。

## 推荐的落地路线

### 阶段 1：把顶层 graph 收缩成真正的 supervisor

目标：

- 顶层只保留：
  - task ledger
  - progress ledger
  - route decisions
  - failure / resume control

具体动作：

- 继续把 `graph.py` 里的业务细节外提
- 明确顶层只调用 team/subgraph，而不是继续塞更多 fetch 细节

### 阶段 2：把 Evidence Acquisition 正式收成 team

目标：

- retrieval / qualification / fetch / gate 形成可独立复用的子图
- 支持 clause-level bounded parallelism

具体动作：

- 给 Evidence Acquisition 一个清晰输入输出协议
- 让它输出结构化 `EvidenceBundle`
- 顶层只关心 coverage / high-authority support / retrieval_failed

### 阶段 3：把 Writing / Verification 合成单独子团队

目标：

- 写作子图不再只是 section writer
- 增加 direct answer verification 和 citation verification 的 team 内闭环

具体动作：

- section writer / editor / verifier 清晰分层
- 默认 last-message 回传
- 失败信号结构化，而不是正文污染

### 阶段 4：再考虑更高级的 team-of-teams

等阶段 1-3 稳定后，再考虑：

- `research_team`
- `writing_team`
- `verification_team`
- 顶层 `top_level_supervisor`

这时再去构建真正的多级 hierarchy，会比现在直接“大拆大建”更稳。

## 一张简化的目标图

```text
User / API
  ->
Deterministic Runtime Shell
  - FastAPI
  - Celery / Redis
  - SQLite task state
  - checkpoint / resume / outbox / budgets
  ->
Research Supervisor
  - task ledger
  - progress ledger
  - route decisions
  ->
  +-- Evidence Acquisition Team
  |     - retrieval
  |     - qualification
  |     - fetch / access recovery
  |     - evidence gate
  |
  +-- Writing & Verification Team
        - section writing
        - editor
        - direct-answer verification
        - citation / fact verification
```

## 结论

适合这个项目的架构方向，不是“更自由的多智能体”，而是：

- 顶层更收敛
- 中层更模块化
- specialist 更工具化
- 并行更有限
- 状态管理更硬

也就是说，下一步最值得建设的是：

**`Supervisor-led, team-of-tools, evidence-driven research architecture`**

而不是：

- fully decentralized swarm
- RL-first routing
- protocol-first distributed agent mesh

如果把这条路线走通，后面再往更复杂的多级嵌套团队扩展，会顺得多，也更适合当前仓库的真实演进阶段。
