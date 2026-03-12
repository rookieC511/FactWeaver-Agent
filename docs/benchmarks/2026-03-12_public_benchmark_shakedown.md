# 2026-03-12 Public Benchmark Shakedown

## Scope

This note records the first public benchmark shakedown after the SQLite outbox migration.

- Submit path: `POST /research` with SQLite outbox
- Public dataset: `google/deepsearchqa`
- Public scoring path: long-form report -> `Final Answer` extraction -> answer-level comparison against gold answer
- Judge role: auxiliary only (`FACT / RACE`), not the public headline metric

## 1. Submit Latency Smoke

Report:

- `reports/submit_latency_smoke_20260312_142716.json`

Healthy broker results:

| Metric | Value |
| --- | ---: |
| Avg submit latency | `29.4186 ms` |
| P95 submit latency | `45.3639 ms` |
| Avg publish latency | `399.6769 ms` |
| P95 publish latency | `502.8846 ms` |
| Publish failure rate | `0.0` |

Conclusion:

- The API no longer blocks on `celery.send_task(...)`.
- The gateway now returns `task_id` at millisecond scale and the broker publish happens asynchronously through the SQLite outbox.

## 2. DeepSearchQA 1-sample Shakedown

Report:

- `reports/public_benchmark_deepsearchqa_20260312_145529.json`
- `reports/public_benchmark_deepsearchqa_20260312_145529.md`

Results:

| Metric | Value |
| --- | ---: |
| Sample size | `1` |
| Task success rate | `1.0` |
| Exact Match | `0.0` |
| F1 | `0.0606` |
| Avg total cost | `0.6232 RMB` |
| Avg elapsed time | `347.2350 s` |
| Answer extraction failure rate | `0.0` |

Observation:

- The `Final Answer` section was successfully produced in the report.
- The answer extractor succeeded, but the predicted answer was still wrong because the report degraded into a "cannot determine" conclusion under missing evidence.

## 3. DeepSearchQA 3-sample Stratified Pilot

Report:

- `reports/public_benchmark_deepsearchqa_20260312_151458.json`
- `reports/public_benchmark_deepsearchqa_20260312_151458.md`

Results:

| Metric | Value |
| --- | ---: |
| Sample size | `3` |
| Task success rate | `1.0` |
| Exact Match | `0.0` |
| F1 | `0.0247` |
| Avg total cost | `0.3846 RMB` |
| Avg elapsed time | `353.5933 s` |
| Answer extraction failure rate | `0.0` |

Per-item notes:

| Category | Answer Type | EM | F1 | Cost (RMB) | Time (s) | Outcome |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| Arts | Single Answer | `0.0` | `0.0741` | `0.5734` | `370.83` | Report answered "cannot determine" due missing museum evidence |
| Arts & Entertainment | Set Answer | `0.0` | `0.0` | `0.2589` | `206.29` | Report failed to identify required episodes |
| Biology | Set Answer | `0.0` | `0.0` | `0.3214` | `483.66` | Report degraded to "无法确定" under missing sources |

## Findings

1. The public evaluation pipeline is now working end-to-end.
2. The new `Final Answer` output contract fixed the extraction-side problem, but not the retrieval-quality problem.
3. The current `medium` path can complete DeepSearchQA tasks, but it often falls back to "cannot determine" because too many benchmark questions depend on blocked or fragile sources.
4. The current system is not ready to claim a strong public QA score on `google/deepsearchqa`.

## Decision

Do not jump directly to the full `30`-question public benchmark yet.

Recommended next step:

- Improve public-benchmark retrieval for answer-centric QA before scaling sample size.
- Candidate directions:
  - add a QA-oriented retrieval mode for benchmark runs
  - prefer structured/official sources more aggressively
  - reduce noisy social-platform candidates
  - strengthen evidence gathering before writer synthesis
