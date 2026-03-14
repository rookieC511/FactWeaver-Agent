# 2026-03-14 Non-PDF Blocked and Writer Stability Round

## Scope

This round targeted two bottlenecks exposed by the previous `3`-task DRB pilot:

- non-PDF blocked hosts, especially `www.wshblaw.com`
- `section_writer` transient failures leaking raw error text into the final report

The implementation intentionally did **not** change HTTP APIs, providers, or large-scale retrieval policy. It only:

- expanded same-host access backfill to high-value HTML and JS-heavy pages
- added one transient retry in `section_writer`
- replaced raw writer error strings with a deterministic evidence-backed fallback section

## Validation

- `python -m compileall core scripts tests`
- `pytest tests/test_evidence_acquisition.py tests/test_writer_graph.py tests/test_deepresearch_bench.py tests/test_public_benchmark.py tests/test_benchmark_scoring.py tests/test_research_modes.py -q`
- Result: `39 passed`

## Pilot Result

Pilot run:

- `reports/deepresearch_bench/drb-pilot-20260314_193708/report.md`
- `reports/deepresearch_bench/drb-pilot-20260314_193708/results.json`

Previous comparison baseline:

- `reports/deepresearch_bench/drb-pilot-20260314_001705/report.md`
- `reports/deepresearch_bench/drb-pilot-20260314_001705/results.json`

## Metric Deltas

| Metric | Previous | Current | Notes |
| --- | ---: | ---: | --- |
| success_rate | 0.3333 | 0.3333 | unchanged |
| avg_drb_report_score | 5.5603 | 7.0553 | strong report-quality lift |
| avg_fact_score | 8.4 | 8.8 | slightly better grounding |
| blocked_source_rate | 0.1667 | 0.1667 | unchanged, still above target 0.15 |
| blocked_non_pdf_rate | n/a | 0.1667 | new metric |
| authority_source_rate | 0.2352 | 0.2399 | stable guardrail |
| weak_source_hit_rate | 0.6384 | 0.2484 | major reduction in weak-source reliance |
| direct_answer_support_rate | 0.5 | 0.4 | regressed |
| retrieval_failed | 3/3 | 2/3 | improved but not enough |

## What Improved

- `www.wshblaw.com` no longer dominates blocked hosts.
- Blocked breakdown moved from PDF-centric failure into a single non-PDF host:
  - previous `blocked_by_host`: `{'www.wshblaw.com': 2}`
  - current `blocked_by_host`: `{'arxiv.org': 1}`
- `section_writer` no longer leaks raw `"Section writing failed:"` strings into the final report.
- The new writer retry and deterministic fallback path did not trigger in this sample:
  - `writer_section_retry_count = 0.0`
  - `writer_section_fallback_count = 0.0`

## What Still Blocks Us

- The round still failed the gate because:
  - `success_rate` stayed at `0.3333`
  - `blocked_source_rate` did not fall below the target
  - `retrieval_failed` remained `2/3`
  - `direct_answer_support_rate` dropped to `0.4`
- The current dominant bottleneck is no longer PDF. It is now:
  - non-PDF blocked access on `arxiv.org`
  - incomplete evidence coverage for direct answers

## Conclusion

This round successfully removed the previous `wshblaw`-driven HTML blocked bottleneck and stabilized writer failure handling.

The next bottleneck is now narrower and cleaner:

- improve non-PDF access for `arxiv.org`-style hosts
- restore `direct_answer_support_rate`
- continue reducing `retrieval_failed` without re-expanding weak-source usage
