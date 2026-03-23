# `legacy_workflow` vs `supervisor_team` Smoke A/B 分析

## 1. 分析对象与输入来源

本分析面向下一轮内部迭代，不是对外汇报稿。

- 分析对象：
  - `legacy_workflow`
  - `supervisor_team`
- 基准对比产物：
  - `reports/deepresearch_bench/drb-smoke-ab-20260319_195122/comparison.md`
  - `reports/deepresearch_bench/drb-smoke-ab-20260319_195122/comparison.json`
  - `reports/deepresearch_bench/drb-smoke-ab-20260319_195122/legacy/results.json`
  - `reports/deepresearch_bench/drb-smoke-ab-20260319_195122/supervisor/results.json`
- 固定样本：
  - `[22, 37, 90]`
- 运行口径：
  - frozen fixture
  - `research_mode=medium`
  - `judge_model=qwen3:8b`
  - strict success 口径

---

## 2. 结论先行

- `supervisor_team` 已经开始改善检索侧质量，不再只是“多一层包装”的空壳。
- 改善主要体现在 `direct_answer_support_rate`、`authority_source_rate`、`weak_source_hit_rate` 这些 evidence 层指标上。
- 但这些收益没有成功转化成最终报告质量，`avg_drb_report_score` 和 `avg_fact_score` 与 `legacy_workflow` 持平。
- 根因不是 writer 写得更差，而是 research 阶段没有及时收敛并切入有效 writing。
- 当前 `supervisor_team` 的主要失败点是“不会及时停”，不是“完全不会找”。
- 结果表现为：质量持平，时间和成本显著放大，最终 smoke 结论只能是 `inconclusive`。

---

## 3. Headline 指标对比

| 指标 | `legacy_workflow` | `supervisor_team` | 变化 |
| --- | ---: | ---: | ---: |
| `avg_drb_report_score` | `7.0553` | `7.0553` | `0.0` |
| `avg_fact_score` | `8.4` | `8.4` | `0.0` |
| `success_rate` | `0.0` | `0.0` | `0.0` |
| `direct_answer_support_rate` | `0.5833` | `0.7333` | `+0.15` |
| `avg_total_cost_rmb_est` | `0.3010` | `1.0083` | `+234.98%` |
| `avg_elapsed_seconds` | `299.6548` | `1110.6914` | `+270.66%` |

这组结果说明：

- `direct_answer_support_rate` 上升，说明 `supervisor_team` 在 evidence/coverage 层已经产生收益。
- `drb_report_score` 没有上升，说明 evidence 层的收益还没有穿透到 final report 层。
- `success_rate` 仍然是 `0`，不是因为报告质量更差，而是因为严格成功口径下，两边都没有形成真正完成态交付。
- 当前问题不在“有没有找到更多东西”，而在“找到之后能不能及时收口并转入写作”。

---

## 4. 逐题分析

### Sample `22`

| 维度 | `legacy` | `supervisor` |
| --- | ---: | ---: |
| `direct_answer_support_rate` | `1.0` | `1.0` |
| `authority_source_rate` | `0.6364` | `0.4675` |
| `blocked_source_rate` | `0.0` | `0.6` |
| `total_cost_rmb_est` | `0.326355` | `0.94254` |
| `elapsed_seconds` | `606.80` | `1232.40` |

本题最值得记住的现象：

- 两边的 `direct_answer_support_rate` 都已经很高，说明 supervisor 没有换来决定性的新增收益。
- 但 `supervisor_team` 仍然继续做了更多 retrieval/fetch/backfill，成本和耗时都明显放大。
- `research_team_result.verifier_decision=needs_backfill`，而 `writer_team_result` 为空，说明系统始终停在 research，没有收口。

对下一轮的启示：

- 这是“过度 research”的典型样本。
- 当 direct answer 支撑已经足够时，应该允许系统更早停止 research，而不是继续因为局部 gap 去追更多来源。

### Sample `37`

| 维度 | `legacy` | `supervisor` |
| --- | ---: | ---: |
| `direct_answer_support_rate` | `0.5` | `0.4` |
| `authority_source_rate` | `0.125` | `0.1183` |
| `weak_source_hit_rate` | `0.8438` | `0.7312` |
| `total_cost_rmb_est` | `0.349555` | `0.806977` |
| `elapsed_seconds` | `156.78` | `1066.76` |

本题最值得记住的现象：

- supervisor 反而在 `direct_answer_support_rate` 上退化。
- `weak_source_hit_rate` 有所下降，说明它不是完全瞎搜，但探索方向并没有转化成更好的 direct answer 支撑。
- 最终仍然停在 `RESEARCH`，且超时结束。

对下一轮的启示：

- 这是“探索方向低效”的典型样本。
- 不能只看“有没有更多路径”，还要控制 supervisor/scout 的探索方向是否真的提高关键 slot 的价值。

### Sample `90`

| 维度 | `legacy` | `supervisor` |
| --- | ---: | ---: |
| `direct_answer_support_rate` | `0.25` | `0.8` |
| `authority_source_rate` | `0.0714` | `0.3707` |
| `weak_source_hit_rate` | `0.8929` | `0.2672` |
| `total_cost_rmb_est` | `0.227139` | `1.275408` |
| `elapsed_seconds` | `135.38` | `1032.91` |

本题最值得记住的现象：

- 这是 supervisor 最有价值的样本。
- retrieval 侧明显改善，`direct_answer_support_rate`、`authority_source_rate` 都大幅提升，`weak_source_hit_rate` 也显著下降。
- 但系统还是没能把这些收益转成 final delivery，依旧停在 `RESEARCH` 并 timeout。

对下一轮的启示：

- 这题证明了 `supervisor_team` 的 research 方向不是错的。
- 当前失败不是“找不到”，而是“research 有收益，但不会收口”。

---

## 5. 为什么会这样

### 5.1 `supervisor_team` 改善的是 evidence 层，不是 final report 层

- `direct_answer_support_rate` 是 evidence/coverage 指标，衡量的是 direct answer 的高权威证据支撑程度。
- `drb_report_score` 是最终报告指标，衡量的是交付出来的报告质量。
- evidence improvement 要真正转化成 report improvement，中间必须经过 `research -> writer` 的闭环。
- 当前 `supervisor_team` 在 research 层已经拿到一部分收益，但这些收益没有成功进入 writer 输出。

### 5.2 supervisor 大多停在 `RESEARCH`，没有真正进入有效写作

从 `supervisor/results.json` 看，3 个样本都有共同特征：

- `current_phase=RESEARCH`
- `writer_team_result={}`
- `research_team_result.verifier_decision=needs_backfill`

这说明：

- 问题不是 writer 节点失效
- 而是系统在 research 阶段一直判定“还要补”
- 最后不是“写得差”，而是“根本没写成”

### 5.3 时间暴涨不只是 token 问题，而是控制流深度问题

- 成本上涨 `+234.98%`
- 耗时上涨 `+270.66%`

如果只是 token 变多，成本和时间通常会一起涨，但不一定会有这么大的剪刀差。当前更像是：

- graph depth 变深
- supervisor 往返增多
- research/backfill 串行链路拉长
- timeout 前仍停留在 `RESEARCH`

也就是说，wall-clock 放大不只是模型更贵，而是控制流被拉长了。

### 5.4 stall 机制没有识别“低收益但仍在动”的状态

这轮 supervisor 的诊断里：

- `team_stall_count = 0`
- `global_stall_count = 0`

这不代表系统没有问题，而代表当前 stall 判定更擅长识别“完全停滞”，不擅长识别：

- 还在 backfill
- 还在继续 fetch
- 但已经没有足够收益

所以系统会持续探索，直到 timeout，而不是提早收口。

### 5.5 这轮 A/B 不能解读成“supervisor_team 无价值”

- `sample 90` 已经证明 retrieval 方向是有效的。
- 当前失败点是“收口策略”失败，不是“探索方向”完全失败。
- 如果后续把 `RESEARCH -> WRITE` 的切换和低收益 backfill 的终止条件调对，`supervisor_team` 仍然有机会把 research 层收益转成最终报告收益。

---

## 6. 对下一轮迭代的直接启示

- 重点优化 `RESEARCH -> WRITE` 的切换条件，不要让 semantic verifier 一直把系统锁在 `needs_backfill`。
- 重点优化低收益 backfill 的终止条件，让系统能识别“虽然还在动，但继续下去不值”。
- 重点优化 supervisor 的“何时停止 research”判断，而不是继续堆新节点或继续增加 graph 深度。

---

## 7. 这份分析怎么使用

这份文档是下一轮 architecture / runtime 迭代的输入。后续无论跑 `quick` 还是 `full` smoke，都应优先对照这 3 个核心问题：

1. evidence improvement 是否真的转化为 report improvement  
2. 系统是否仍然卡在 `RESEARCH`  
3. 时间暴涨是否仍主要来自串行控制流而不是单纯 token 成本

如果这三件事没有改善，那么即使 evidence 指标继续变好，也不应该认为 `supervisor_team` 已经具备替代 `legacy_workflow` 的条件。
