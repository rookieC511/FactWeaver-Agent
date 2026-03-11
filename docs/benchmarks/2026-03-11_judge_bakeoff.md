# 2026-03-11 Judge Bakeoff

## 目的

在本地 Ollama judge 中，比较：
- `llama3.1:latest`
- `qwen3:8b`

目标是为 benchmark 选出默认 judge，而不是继续依赖拍脑袋选择。

## 测试范围

- 固定 `12` 个评分样本
- 覆盖：
  - 中英双语
  - 高质量 / 低质量
  - 引用充分 / 引用稀疏
  - 含降级说明 / 不含降级说明
- `4` 个锚点样本做 `3` 次重复，观察稳定性
- 两个模型都对现有 `9` 轮 benchmark 结果做离线重评分

## 结果

产物：
- `reports/judge_bakeoff_20260311_220911.json`
- `reports/judge_bakeoff_20260311_220911.md`

摘要：

| Model | JSON Parse Rate | Out-of-Range Rate | RACE Misread Rate | Avg Latency (s) |
| --- | ---: | ---: | ---: | ---: |
| `llama3.1:latest` | `100%` | `0%` | `0%` | `6.33` |
| `qwen3:8b` | `100%` | `0%` | `0%` | `24.20` |

## 结论

- 默认 judge：`qwen3:8b`
- fallback judge：`llama3.1:latest`

原因：
- 两者解析率都稳定
- 两者都没有出现越界分数或 `RACE` 语义误解
- `qwen3:8b` 的锚点重复稳定性更强，排序更稳定
- `llama3.1` 速度更快，因此保留为 fallback
