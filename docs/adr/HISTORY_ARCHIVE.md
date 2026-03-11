# History Archive

这份文档只保留关键里程碑，作为 `AI_CONTEXT.md` 的历史补充。

## V3 阶段

- 从单体同步式研究流程逐步演进为 LangGraph 主导的状态机结构。
- `memory.py` 从向量库依赖逐步转向长文事实抽取与轻量知识块管理。
- 引入爬虫降级、并发提取、引用保留等工程化手段，核心目标是稳定性和成本控制。

## 2026-03-10: V4.4 Runtime Alignment

- API 主路径改成优先走 Redis/Celery，而不是进程内 `BackgroundTasks`
- 任务状态、缓存、DLQ 改由 SQLite 持久化
- LangGraph checkpoint 改为 SQLite durable saver
- 去掉 Step 3 中高成本的 LLM 自纠闭环
- 会话级 `KnowledgeManager` 真正接进执行链
- 最终报告追加降级附录，显式说明缺失证据与 fallback

参考：

- `docs/architecture_alignment_v44.md`

## 2026-03-10 至 2026-03-11: 三档检索模式

- 正式引入 `low / medium / high`
- `low`
  - `Serper + scrape_jina_ai + add_compact_document`
- `medium`
  - `Serper + Tavily Extract basic + add_extracted_chunks`
- `high`
  - `Tavily Search advanced + Map + Crawl + Extract advanced`
- 三档主路径都不再依赖旧的 `aadd_document()` 长文抽取链

## 2026-03-11: benchmark 成本与评分统一

- benchmark 主展示从混合 `RMB + USD` 改为统一人民币
- 新增固定汇率：`1 USD = 7.20 RMB`
- 新增离线重评分，不重新触发模型或联网检索
- 新增质量分与综合分：
  - `FACT`
  - `RACE`
  - `Quality`
  - `Value`
  - `Overall`
- 默认档位不再只看便宜，而是加了质量门槛

## 历史指标引用说明

下面两个数字可以继续引用，但必须带上前后对照条件，否则会显得像孤立结论。

### `117s -> 55.7s`

- 指标性质：
  - 这是 `V2.2.2 -> V2.2.4` 阶段的历史性能对比
  - 主要反映的是研究 / 提取主链路在当时那套 `10K Hybrid Map-Reduce` 架构下的端到端平均耗时
- 前值 `117s` 的条件：
  - 基于 `10K Hybrid Map-Reduce` 的既有基线版本
  - 同样的均衡切片策略
  - 同样的 Needle-in-Haystack 类验证目标
  - 还没有放开核心 Map 阶段并发
- 后值 `55.7s` 的条件：
  - 仍然是同一条 `10K Hybrid Map-Reduce` 主链路
  - 在相同切片策略和同类验证目标下
  - 将核心 Map 阶段放开为 `Semaphore(4)` 并发
  - 同时补入 `3` 次重试
- 正确表述方式：
  - 在相同 `10K Hybrid Map-Reduce` 主链路和同类 Needle 验证条件下，放开 `Semaphore(4)` 并发后，端到端平均耗时从 `117s` 降到 `55.7s`
- 不要误写为：
  - 当前 `V4.6` API 全链路平均耗时就是 `55.7s`

### `¥23.0 -> ¥0.5-1.0`

- 指标性质：
  - 这是 `V4.1` 之前的高开销链路，与 `V4.2/V4.3` 降本改造后的历史账单区间对比
  - 反映的是单次复杂深度研究任务的端到端模型账单量级
- 前值 `¥23.0` 的条件：
  - 并行 Writer 仍会读取全量上下文
  - 存在明显的 token 扇出爆炸
  - 尚未接入严格的人名币熔断
  - 尚未完成基于 `section_id` 的标签化切片路由
  - 工具 / 格式异常的高成本路径还更容易触发额外消耗
- 后值 `¥0.5-1.0` 的条件：
  - Writer 改为按 `section_id` 只读取绑定事实切片
  - 接入 `CostTracker` 与 `¥1.0` 物理熔断上限
  - `llm_worker` 从 `GLM-4.7` 降到 `DeepSeek-V3.2`
  - `llm_extractor` 切到更高性价比的 `Qwen3.5-397B-A17B`
- 正确表述方式：
  - 在相同复杂度等级的单任务深度研究场景下，修复并行 Writer 全量读上下文导致的 token 扇出后，单任务模型账单从约 `¥23.0` 降到 `¥0.5-1.0`
- 必须额外说明：
  - 这是历史架构阶段之间的成本区间对比
  - 它不等同于当前 `low / medium / high` 三档 benchmark 的直接结果

## 当前稳定结论

- 默认推荐：`medium`
- 最高质量：`high`
- 最高性价比：`low`
- 最慢 / 最贵：`high`

后续如果还有新的阶段性重构，继续按“架构决策摘要”的方式追加，不要把原始实验日志整段搬回来。
