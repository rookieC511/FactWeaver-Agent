# Three-Mode Benchmark Summary

## Scope

- 日期：`2026-03-11`
- 样本：`3 queries x 3 modes = 9 runs`
- 结果来源：
  - 原始结果：`reports/mode_benchmark_20260311_021143.json`
  - 离线重评分：`reports/mode_benchmark_20260311_021143_scored.json`
- 说明：
  - 离线重评分不重新触发模型或检索
  - 统一汇率：`1 USD = 7.20 RMB`
  - 本批评分使用了 `heuristic_fallback`，因为当时本地 judge 不可用

## Scoring

- `fact_score`
  - 引用支撑、可追溯性、未证实断言风险
- `race_score`
  - 结构完整度、覆盖度、逻辑连贯性、内容厚度
- `quality_score = 0.55 * fact_score + 0.45 * race_score`
- `cost_efficiency_score`
  - 在同批 benchmark 内按 `quality_score / total_cost_rmb_est` 归一化
- `overall_score = 0.70 * quality_score + 0.30 * cost_efficiency_score`

## Mode Summary

| Mode | Avg LLM Cost (RMB) | Avg External Cost (RMB) | Avg Total Cost (RMB) | Avg Quality | Avg Value | Avg Overall | Avg Time (s) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| low | 0.0804 | 0.1008 | 0.1812 | 7.49 | 8.68 | 7.85 | 574.25 |
| medium | 0.1119 | 0.7416 | 0.8535 | 8.90 | 2.14 | 6.87 | 434.85 |
| high | 0.1467 | 2.9568 | 3.1035 | 8.90 | 0.80 | 6.47 | 682.87 |

## Key Conclusions

- 默认推荐档位：`medium`
- 质量最高档位：`high`
- 性价比最高档位：`low`
- 最慢档位：`high`
- 最贵档位：`high`

## Why `medium` Is Default

- `low` 的综合分更高，但平均质量分只有 `7.49`
- 当前规则要求默认档必须先跨过质量门槛，再比较综合表现
- `medium` 与 `high` 的平均质量分几乎相同，但 `medium` 明显更便宜也更快

## Total Cost

- `actual_total_llm_cost_rmb = 1.0170`
- `actual_total_external_cost_rmb_est = 11.3976`
- `actual_total_cost_rmb_est = 12.4146`

## Notes

- `low` 最便宜，但不一定最快
- `high` 质量最强，但 Tavily 外部检索成本很高
- 当前策略上最值得继续优化的是：
  - 让 `low` 更轻、更早停
  - 让 `high` 更克制，减少不必要的 `Map/Crawl`
