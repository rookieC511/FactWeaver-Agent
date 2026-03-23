# 2026-03-18 前沿多智能体架构研究综述

## 目的

这份综述不讨论某个框架的 API 细节，而是整理 2025-2026 官方资料里已经相对收敛的多智能体架构共识，回答两个问题：

1. 最新主流多智能体系统到底在往什么方向演进
2. 这些方向里，哪些适合长报告 Deep Research 系统，哪些只是“看起来前沿”

## 核心结论

截至 2026 年，主流官方资料呈现出四个稳定趋势：

1. 顶层控制越来越偏向 `Supervisor / Manager`，而不是去中心化 swarm。
2. “Agent-as-a-Tool” 正在成为主流工程模式，即顶层只调用高层能力单元，不直接拿所有底层工具。
3. 多智能体真正有价值的场景不是“随便多开几个 agent”，而是对可分解、可并行、上下文会爆炸的复杂任务做结构化拆分。
4. 长任务系统越来越强调状态管理、恢复能力、记忆隔离、失败熔断和人类监督，而不是只比谁会写 prompt。

## 一、Anthropic：Research 系统中的 Orchestrator-Worker

Anthropic 官方工程文章展示了一套用于开放式 research 的多智能体系统：一个 lead agent 负责任务拆分与整合，多个 subagents 并行探索不同子问题。它的核心不是“多 agent 更酷”，而是：

- research 任务天然适合按子问题拆分
- 并行探索可以提高 breadth-first coverage
- 多 agent 代价是真实存在的：上下文和 token 成本会明显上升

这类架构的价值在于：

- 顶层只维护问题分解和最终综合
- 子 agent 负责局部探索
- 系统用并行换覆盖率，而不是用并行换“智能感”

这对长报告系统尤其重要，因为开放式 research 的瓶颈往往不是“写作”，而是“有没有把关键证据搜全”。

适合借鉴的点：

- 并行只用于可独立分解的子问题
- 顶层必须有一个对最终答案负责的 lead agent
- 不要默认全局并行，否则成本和上下文复杂度会失控

来源：

- Anthropic, *How we built our multi-agent research system*  
  <https://www.anthropic.com/engineering/built-multi-agent-research-system>

## 二、OpenAI：Manager Pattern 与 Handoffs 的边界

OpenAI 官方对多智能体给出的建议非常务实：

- 先把单 agent 做强
- 只有当工具太多、指令太杂、上下文太大、任务可明确分工时，才值得拆成多 agent

官方把多智能体模式分成两大类：

1. `Manager pattern`
   - 一个 agent 作为主控
   - 其他 agent 被当成工具或 specialist 使用
   - 顶层工作流更可预测、更容易控制
2. `Handoffs`
   - 让不同 specialist 接管对话
   - 更灵活，但一致性和可控性更难

这套区分很关键。对一个需要最终交付完整报告、还要做 evidence gate、成本控制、恢复执行的系统来说，manager pattern 明显更适合。

适合借鉴的点：

- 把 specialist agent 当成高层工具
- 保持一个中心节点对用户负责
- 能用代码保证确定性的地方，尽量不要完全交给 LLM 动态决定

来源：

- OpenAI, *A practical guide to building AI agents*  
  <https://openai.com/business/guides-and-resources/a-practical-guide-to-building-ai-agents/>

## 三、LangGraph：Supervisor 与多级层级结构

LangGraph 官方已经把 “Supervisor 管理多 agent” 做成了明确模式。它最重要的工程贡献不是多 agent 本身，而是把这些结构做成了可组合的图：

- 一个 supervisor 负责分发任务
- worker 可以是 agent，也可以是子图
- 支持 multi-level hierarchies
- 支持 `last_message` 风格的降噪回传

这意味着：

- 一个 team 可以被封装成另一个 team 的 tool
- 顶层不需要知道底层有多少具体步骤
- 可以自然形成 team-of-teams

这套模式特别适合大系统，因为它天然支持：

- 上下文隔离
- 子流程复用
- 长链路调试
- 可恢复执行

适合借鉴的点：

- 顶层 supervisor 只看结论，不看所有底层草稿
- 子团队默认用“最后消息”回传，避免上下文中毒
- 把复杂功能做成可嵌套子图，而不是把所有逻辑塞进一个 giant graph

来源：

- LangGraph Supervisor 参考文档  
  <https://langchain-ai.github.io/langgraphjs/reference/modules/langgraph-supervisor.html>

## 四、Microsoft Magentic-One：账本式动态控制

Magentic-One 的价值在于，它把多智能体从“多个角色一起聊天”推进成了“有状态的动态控制系统”。

它的关键设计有三点：

1. `Orchestrator + Specialists`
   - 顶层 orchestrator 不直接拿底层动作工具
   - specialist 负责网页、文件、代码、终端等具体能力
2. `Task Ledger + Progress Ledger`
   - 一份账本记录宏观目标、已知事实、待验证问题、教育性猜测
   - 一份账本记录当前微观进度、是否停滞、是否要改路由
3. `Outer Loop + Inner Loop`
   - 内循环负责战术执行
   - 外循环负责反思、修正计划、状态清洗

它真正重要的地方不只是“多 agent”，而是：

- 系统知道自己是否卡住了
- 系统知道什么时候该放弃当前路径
- 系统知道什么时候应该重写计划

适合借鉴的点：

- 不把当前步骤和全局计划混成一坨上下文
- 明确记录“已知事实 / 缺口 / 下一步”
- 允许 stall detection、反思、memory reset 成为系统内建能力

来源：

- Microsoft AutoGen, *Magentic-One*  
  <https://microsoft.github.io/autogen/stable/user-guide/agentchat-user-guide/magentic-one.html>

## 五、Google ADK：工作流原语与协议层

Google ADK 提供的价值更偏“工作流积木”：

- `SequentialAgent`
- `ParallelAgent`
- `LoopAgent`

同时，A2A 更像 agent 间互操作协议，而不是单个应用的最佳内部控制架构。

这类系统适合：

- 明确工作流建模
- 把一组 agent 变成可复用 runtime primitive
- 跨系统互联

但对于单个 Deep Research 应用来说，ADK/A2A 更适合作为参考层，而不是当前阶段最需要直接复制的顶层设计。

来源：

- Google ADK Multi-agents  
  <https://google.github.io/adk-docs/agents/multi-agents/>
- Google A2A  
  <https://google.github.io/adk-docs/a2a/>

## 六、前沿方向里，哪些是真趋势，哪些不适合立即落地

### 适合立即吸收的方向

- `Supervisor / Manager` 作为中心控制节点
- `Agent-as-a-Tool`
- team-of-teams / multi-level hierarchy
- task/progress ledger
- bounded parallelism
- context isolation
- checkpoint / resume / reflection / stall recovery

### 目前不建议优先做的方向

- 完全去中心化 handoff mesh
- RL 驱动的动态拓扑学习
- 纯 swarm 式 agent 网络
- 先上协议层分布式，再补本地业务逻辑

原因不是这些方向不先进，而是：

- 研发成本高
- 调试难度高
- 对当前单应用 Deep Research 项目来说，收益不成比例

## 七、对 Deep Research 系统最重要的架构共识

把这些官方资料合在一起，最值得保留的共识是：

1. 顶层必须有一个“对最终答案负责”的节点。
2. specialist 应该被封装成高层能力，而不是把所有低层工具都暴露给顶层。
3. 并行应该有边界，只在子问题可独立拆分时使用。
4. 失败恢复和状态账本不是附加功能，而是长生命周期系统的主干能力。
5. 评估一个多智能体系统，不能只看最后能不能出答案，还要看：
   - 是否会空转
   - 是否会上下文中毒
   - 是否会草率交差
   - 是否能恢复
   - 是否能验证关键结论

## 结论

前沿多智能体架构的真正演进方向，不是“让更多 agent 自由聊天”，而是：

- 用中心控制节点维持目标一致性
- 用 specialist subagents 承担局部复杂性
- 用层级结构和上下文隔离控制系统规模
- 用 ledger、checkpoint、stall recovery 把系统从 prompt workflow 提升到可恢复的长任务 runtime

对长报告 Deep Research 应用来说，最有价值的不是最激进的去中心化，而是：

**`Supervisor + Agent-as-a-Tool + Bounded Parallelism + Strong State Management`**
