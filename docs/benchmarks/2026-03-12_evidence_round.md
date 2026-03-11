# 2026-03-12 Evidence Round

## Summary

This round completed the three evidence-first experiments under the 40 RMB envelope:

- `checkpoint recovery` formal benchmark
- real `FastAPI + Redis + Celery + SQLite` concurrency probe
- writer `legacy_full_context vs section_scoped` cost A/B

`30-run benchmark` was intentionally deferred to the next budget window.

## 1. Checkpoint Recovery

- Source: `reports/checkpoint_recovery_20260312_010652.json`
- Mode: `low`
- Query: `LangGraph sqlite checkpoint basics`
- Runs: `12`
- Overall success rate: `100%`
- Avg resume time:
  - `planner`: `367.07s`
  - `executor`: `231.95s`
  - `writer.before_editor`: `207.08s`

Cost rollup for these `12` successful recovery runs:

- LLM cost: `0.741424 RMB`
- External retrieval cost: `0.129 USD` (`0.9288 RMB`)
- Total all-in estimate: `1.670224 RMB`
- Avg elapsed time: `361.23s`

## 2. Concurrency Probe

- Source: `reports/concurrency_probe_20260312_015713.json`
- Service mode: `existing`
- Dedicated probe environment:
  - API on `127.0.0.1:8001`
  - isolated Redis broker/result DBs `10/11`
  - single worker, `concurrency=4`
- Cumulative all-in estimate: `6.934062 RMB`
- Stop reason: `qualification_failed:4`

Formal result:

- Max supported concurrency: `2`

Per-level summary:

| Concurrency | Success Rate | Failure Rate | DLQ | P50 (s) | P95 (s) | Avg Queue Wait (s) | All-in (RMB) | Qualified |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| `2` | `100%` | `0%` | `0` | `437.50` | `573.00` | `167.00` | `2.0731` | `true` |
| `4` | `50%` | `50%` | `0` | `646.50` | `901.00` | `5.25` | `4.8610` | `false` |

## 3. Cost A/B

- Source: `reports/cost_ab_20260312_031542.json`
- Mode: `medium`
- Query limit: `5`
- Cumulative all-in estimate: `10.174499 RMB`
- Stop reason: `budget_cap_reached:10.0`

Formal result:

- Total cost reduction: `17.52%`

Average comparison:

| Context Mode | Avg LLM Cost (RMB) | Avg External Cost (RMB) | Avg Total Cost (RMB) | Avg Time (s) |
| --- | ---: | ---: | ---: | ---: |
| `legacy_full_context` | `0.2266` | `0.8885` | `1.1151` | `574.91` |
| `section_scoped` | `0.1206` | `0.7992` | `0.9198` | `357.72` |

## Round Rollup

- Recovery total: `1.670224 RMB`
- Concurrency total: `6.934062 RMB`
- Cost A/B total: `10.174499 RMB`
- Total all-in estimate for this round: `18.778785 RMB`

## Key Findings

1. The durable checkpoint path is no longer just a design claim; it achieved `12/12` successful real process-level resumes.
2. On a single-node setup with one Celery worker and `concurrency=4`, the current production path only qualifies at concurrency `2`.
3. The current writer path shows a reproducible `17.52%` total cost reduction versus the legacy full-context variant.
4. A real bottleneck remains in the API submission path: `POST /research` can block for roughly one minute before returning a task ID when pushing work to Celery. This undermines the intended “submit fast, queue slow work” architecture and should be fixed before scaling claims are strengthened.
