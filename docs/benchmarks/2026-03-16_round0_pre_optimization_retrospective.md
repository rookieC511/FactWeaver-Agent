# 第0轮复盘：第一轮优化之前的验证与基线阶段

## 1. 这份文档在回答什么

这份文档梳理的是：**在“第一轮检索与证据质量优化”正式开始之前，我们已经做过哪些测试、这些测试发生在什么时候、分别得出了什么结论。**

如果按阶段命名，这一段更适合叫：

- **第0轮：基线验证与证据补齐阶段**

而不是第一轮优化本身。

原因很简单：

- 这一阶段的重点不是“把系统质量拉高”
- 而是先回答：
  - 系统成本结构是什么
  - 恢复能力是不是真的成立
  - 并发能到什么程度
  - 旧路径和新路径的成本差多少
  - 提交链路是否真的秒回
  - 公共 benchmark 管线是否能跑通
- 这些工作都发生在真正的 retrieval / evidence acquisition 重构之前

因此，把它定义成 **第0轮** 是合理的。

---

## 2. 时间范围

这一轮主要集中在：

- **2026-03-11**
- **2026-03-12**

对应的主要资料来源：

- [2026-03-11_three_mode_benchmark.md](/D:/workplace/Projects/deepresearch-agent/docs/benchmarks/2026-03-11_three_mode_benchmark.md)
- [2026-03-11_judge_bakeoff.md](/D:/workplace/Projects/deepresearch-agent/docs/benchmarks/2026-03-11_judge_bakeoff.md)
- [2026-03-11_recovery_smoke.md](/D:/workplace/Projects/deepresearch-agent/docs/benchmarks/2026-03-11_recovery_smoke.md)
- [2026-03-12_evidence_round.md](/D:/workplace/Projects/deepresearch-agent/docs/benchmarks/2026-03-12_evidence_round.md)
- [2026-03-12_public_benchmark_shakedown.md](/D:/workplace/Projects/deepresearch-agent/docs/benchmarks/2026-03-12_public_benchmark_shakedown.md)

---

## 3. 第0轮当时在验证什么

这一阶段主要在验证 6 类问题：

1. 三档检索模式 `low / medium / high` 的成本、速度、综合质量大概是什么样
2. 本地 judge 应该选哪个模型
3. checkpoint 恢复是否真的成立，而不是只有架构描述
4. 单机单 worker 下，真实并发能力到哪里
5. writer 新旧路径的成本差异是否可复现
6. 提交链路和最早的 public benchmark 管线是否能跑通

换句话说，第0轮的目标不是“把 DRB 分数打上去”，而是先做 **系统画像**。

---

## 4. 第0轮的指标是怎么构建的

### 4.1 三档 benchmark 指标

在三档模式 benchmark 里，我们构建的是：

- `llm_cost_rmb`
- `external_cost_rmb_est`
- `total_cost_rmb_est`
- `elapsed_seconds`
- `fact_score`
- `race_score`
- `quality_score`
- `cost_efficiency_score`
- `overall_score`

其中：

- `fact_score`
  - 看引用支撑、可追溯性、unsupported claim 风险
- `race_score`
  - 看结构完整度、覆盖度、逻辑连贯性、内容厚度
- `quality_score = 0.55 * fact_score + 0.45 * race_score`
- `overall_score = 0.70 * quality_score + 0.30 * cost_efficiency_score`

这套指标当时的作用是：

- 不只是看“谁更便宜”
- 而是看“谁更像默认模式”

### 4.2 恢复 / 并发 / A/B 指标

第0轮里，恢复、并发和成本 A/B 用的是更工程化的指标：

- 恢复：
  - `success_rate`
  - `avg_resume_elapsed_seconds`
  - `resumed_from_checkpoint`
  - `last_checkpoint_id`
  - `last_checkpoint_node`
- 并发：
  - `success_rate`
  - `failure_rate`
  - `DLQ`
  - `P50 / P95`
  - `avg_queue_wait_seconds`
- 成本 A/B：
  - `avg_llm_cost_rmb`
  - `avg_external_cost_rmb_est`
  - `avg_total_cost_rmb_est`
  - `avg_elapsed_seconds`

### 4.3 Public benchmark 指标

在最早的 public QA benchmark 阶段，我们用的是：

- `Exact Match`
- `F1`
- `task_success_rate`
- `answer_extraction_failure_rate`

这一步后来证明：**QA 型 benchmark 并不适合作为这个项目的主成绩。**

---

## 5. 第0轮具体做了什么

### 阶段 A：三档检索 benchmark

时间：

- **2026-03-11**

资料：

- [mode_benchmark_20260311_021143_scored.md](/D:/workplace/Projects/deepresearch-agent/reports/mode_benchmark_20260311_021143_scored.md)

当时跑的是：

- `3 queries x 3 modes = 9 runs`

核心结果：

- 总 LLM 成本：`1.0170 RMB`
- 总外部成本：`11.3976 RMB`
- 总成本：`12.4146 RMB`

模式均值：

| Mode | Avg Total Cost (RMB) | Avg Quality | Avg Overall | Avg Time (s) |
| --- | ---: | ---: | ---: | ---: |
| `low` | `0.1812` | `8.18` | `8.27` | `574.25` |
| `medium` | `0.8535` | `8.18` | `6.25` | `434.85` |
| `high` | `3.1035` | `8.18` | `5.93` | `682.87` |

这一阶段得到的结论：

- `high` 最贵，也最慢
- `low` 最便宜，综合分当时最好
- `medium` 更接近产品默认档，因为质量和速度更平衡

这一步的意义是：

- 先把三档模式的成本结构摸清楚
- 为后面所有优化提供 baseline

### 阶段 B：judge bakeoff

时间：

- **2026-03-11**

资料：

- [judge_bakeoff_20260311_220911.md](/D:/workplace/Projects/deepresearch-agent/reports/judge_bakeoff_20260311_220911.md)

候选：

- `llama3.1:latest`
- `qwen3:8b`

结果：

| Model | JSON Parse Rate | Out-of-Range Rate | RACE Misread Rate | Avg Latency (s) |
| --- | ---: | ---: | ---: | ---: |
| `llama3.1:latest` | `100%` | `0%` | `0%` | `6.33` |
| `qwen3:8b` | `100%` | `0%` | `0%` | `24.20` |

最终选择：

- 默认 judge：`qwen3:8b`
- fallback：`llama3.1:latest`

原因不是单纯看速度，而是：

- `qwen3:8b` 的锚点稳定性更好
- 更适合做“评分尺子”

### 阶段 C：checkpoint recovery smoke 与正式恢复实验

时间：

- **2026-03-11** 先 smoke
- **2026-03-12** 正式 12 次

资料：

- [2026-03-11_recovery_smoke.md](/D:/workplace/Projects/deepresearch-agent/docs/benchmarks/2026-03-11_recovery_smoke.md)
- [checkpoint_recovery_20260312_010652.md](/D:/workplace/Projects/deepresearch-agent/reports/checkpoint_recovery_20260312_010652.md)

正式结果：

- `12/12` 成功
- query：`LangGraph sqlite checkpoint basics`
- 平均恢复耗时：
  - `planner`: `367.07s`
  - `executor`: `231.95s`
  - `writer.before_editor`: `207.08s`
- 总 all-in 成本：`1.670224 RMB`

这一步的意义是：

- 把“checkpoint 可恢复”从口头描述变成了实验结论

### 阶段 D：真实并发探针

时间：

- **2026-03-12**

资料：

- [concurrency_probe_20260312_015713.md](/D:/workplace/Projects/deepresearch-agent/reports/concurrency_probe_20260312_015713.md)
- [2026-03-12_evidence_round.md](/D:/workplace/Projects/deepresearch-agent/docs/benchmarks/2026-03-12_evidence_round.md)

环境口径：

- `FastAPI + Redis + Celery + SQLite`
- 单节点
- 单 worker
- `concurrency=4`

结果：

- 最大合格并发：`2`
- `4` 并发时失败率上升到 `50%`

这一步的意义是：

- 说明当前系统的“真实并发能力”还很有限
- 也证明队列和状态管理不是摆设，但还没到高并发成熟阶段

### 阶段 E：writer 成本 A/B

时间：

- **2026-03-12**

资料：

- [cost_ab_20260312_031542.md](/D:/workplace/Projects/deepresearch-agent/reports/cost_ab_20260312_031542.md)
- [2026-03-12_evidence_round.md](/D:/workplace/Projects/deepresearch-agent/docs/benchmarks/2026-03-12_evidence_round.md)

比较对象：

- `legacy_full_context`
- `section_scoped`

结果：

- 总成本下降：`17.52%`
- 平均耗时：
  - `legacy_full_context`: `574.91s`
  - `section_scoped`: `357.72s`

这一阶段就是你说的 **A/B test**，发生在：

- **2026-03-12**

它的意义是：

- 证明“旧写法 vs 新写法”的成本下降不是口头描述，而是可复现实验

### 阶段 F：提交阻塞与 submit latency

时间：

- **2026-03-12**

资料：

- [submit_latency_smoke_20260312_142716.md](/D:/workplace/Projects/deepresearch-agent/reports/submit_latency_smoke_20260312_142716.md)
- [2026-03-12_public_benchmark_shakedown.md](/D:/workplace/Projects/deepresearch-agent/docs/benchmarks/2026-03-12_public_benchmark_shakedown.md)

背景：

- 当时 `POST /research` 会卡在同步 `celery.send_task(...)`
- 后来引入 SQLite outbox，把提交和 broker publish 解耦

结果：

- 平均 submit latency：`29.4186 ms`
- P95 submit latency：`45.3639 ms`
- 平均 publish latency：`399.6769 ms`
- publish failure rate：`0.0`

这一步的意义是：

- 把“网关应该快速返回 task_id”从设计目标变成真实结果

### 阶段 G：最早的 public benchmark shakedown

时间：

- **2026-03-12**

资料：

- [2026-03-12_public_benchmark_shakedown.md](/D:/workplace/Projects/deepresearch-agent/docs/benchmarks/2026-03-12_public_benchmark_shakedown.md)
- [public_benchmark_deepsearchqa_20260312_145529.md](/D:/workplace/Projects/deepresearch-agent/reports/public_benchmark_deepsearchqa_20260312_145529.md)
- [public_benchmark_deepsearchqa_20260312_151458.md](/D:/workplace/Projects/deepresearch-agent/reports/public_benchmark_deepsearchqa_20260312_151458.md)

当时先接的是：

- `google/deepsearchqa`

1 题试跑：

- `EM = 0.0`
- `F1 = 0.0606`

3 题分层试跑：

- `EM = 0.0`
- `F1 = 0.0247`
- `task_success_rate = 1.0`
- `answer_extraction_failure_rate = 0.0`

这一阶段最重要的结论是：

- public QA 评测链跑通了
- `Final Answer` 能抽出来
- 但这个 benchmark 不适合做项目主成绩
- 也第一次清楚暴露了 **retrieval 质量不够**

---

## 6. 第0轮的核心结论

这一轮结束后，我们真正搞清楚了几件事：

### 6.1 系统的工程能力边界

- 恢复能力成立
- 提交链路能做到毫秒级返回
- 单机真实并发能力目前只有 `2`

### 6.2 成本结构已经摸清

- `high` 很贵
- `section_scoped` 相比旧 writer 路径确实能省钱
- 预算熔断和全口径成本跟踪是必要的

### 6.3 public QA benchmark 不适合作为主方向

- 它能用来辅助暴露 retrieval 问题
- 但不能代表长报告系统的核心价值

### 6.4 真正的主问题开始浮现

虽然第0轮还没有正式进入第一轮优化，但它已经把后面的问题暴露出来了：

- retrieval 质量不够
- blocked source 太多
- 高权威来源抓取成功率太低
- evidence gate 需要更硬

也正因为这些问题已经被第0轮暴露出来，后面才会进入：

- **第一轮：检索与证据质量优化**

---

## 7. 为什么这确实应该叫“第0轮”

我建议把这段历史正式命名为：

- **第0轮：系统基线、工程能力与评测管线验证**

而把后面的 retrieval / evidence acquisition 重构叫：

- **第一轮：检索与证据质量优化**

原因是：

- 第0轮主要在建立 baseline 和实验框架
- 第一轮才是真正开始系统性优化质量瓶颈

按这个划分，时间线会很清楚：

1. **第0轮**
   - 三档 benchmark
   - judge bakeoff
   - recovery
   - concurrency
   - cost A/B
   - submit latency
   - 最早 public benchmark
2. **第一轮**
   - DRB 本地兼容评测
   - retrieval / evidence acquisition 重构
   - blocked / PDF / non-PDF / arxiv / direct-answer support 系列优化

---

## 8. 一句话总结

第0轮不是“优化阶段”，而是“把系统真实能力和真实问题看清楚的阶段”。

它做的事情包括：

- 建立三档模式基线
- 跑 judge bakeoff
- 验证恢复
- 压并发
- 做 writer A/B
- 修提交阻塞
- 试跑最早 public benchmark

而它最大的价值不是分数本身，而是帮我们看清楚：

**后面真正该打的，不是 writer 花活，而是 retrieval 和 evidence acquisition。**
