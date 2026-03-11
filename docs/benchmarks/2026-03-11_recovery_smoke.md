# 2026-03-11 Checkpoint Recovery Smoke

## Scope

- Goal: validate real process-level resume on the three planned safe points before running the full 12-run recovery benchmark.
- Mode: `low`
- Query used for the final successful smoke samples: `LangGraph sqlite checkpoint basics`
- Budget guard:
  - `MAX_TASK_RMB_COST=0.40`
  - `MAX_TASK_DURATION_SECONDS=600`
- Resume path: same `task_id` and `thread_id`, explicit `--resume`

## Interruption Points

1. Main graph after `planner`
2. Main graph after `executor`
3. Writer subgraph at `writer.before_editor`

Interruption was injected at the worker / executor layer after task state, checkpoint metadata, and KM snapshot had been flushed.

## Results

| Point | Status | LLM Cost (RMB) | External Cost (USD) | External Cost (RMB est.) | Total Cost (RMB est.) | Elapsed (s) | Attempts | Resume Count | Resumed From Checkpoint |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| `planner` | `SUCCESS` | `0.085207` | `0.013` | `0.0936` | `0.178807` | `458.62` | `2` | `1` | `true` |
| `executor` | `SUCCESS` | `0.071334` | `0.006` | `0.0432` | `0.114534` | `333.84` | `2` | `1` | `true` |
| `writer.before_editor` | `SUCCESS` | `0.061662` | `0.011` | `0.0792` | `0.140862` | `404.17` | `2` | `1` | `true` |

## Aggregate

- Successful resume samples: `3 / 3`
- Aggregate LLM cost: `0.218203 RMB`
- Aggregate external cost: `0.030 USD`
- Aggregate external cost estimate: `0.2160 RMB`
- Aggregate all-in estimate: `0.434203 RMB`
- Average elapsed time: `398.88 s`

## Notes

- An earlier `planner` smoke used a longer query and hit the old `300s` task timeout after resume. It was not counted as a success sample.
- During the runs, `CostTracker` printed GBK encoding warnings to the console, but persisted cost fields in SQLite were correct.
- A real bug was found during these runs: `gateway/state_store.py` had one fewer SQL placeholder than columns in `tasks` upsert. This was fixed before the final successful smoke set.
