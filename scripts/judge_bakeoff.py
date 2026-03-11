from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

from openai import OpenAI

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from scripts.benchmark_scoring import LOCAL_JUDGE_BASE_URL, summarize_payload

REPORTS_DIR = ROOT_DIR / "reports"

MODELS = ["llama3.1:latest", "qwen3:8b"]
ANCHOR_SAMPLE_IDS = {"cn_good", "en_good", "cn_bad", "degraded"}

SAMPLES: list[dict[str, str]] = [
    {
        "id": "cn_good",
        "label": "中文高质量报告",
        "report": """# Deep Research Report
## 背景
[HASH:abc111] DeepSeek-R1 在 2025 年初公开了训练成本和推理表现。
## 分析
[HASH:abc111] [HASH:def222] 报告对比了 OpenAI o1 与 DeepSeek-R1 的推理能力、成本与开放性。
## 结论
在引用充分的前提下，总结了模型能力、成本和适用场景。
## References
https://example.com/a
https://example.com/b
""",
    },
    {
        "id": "en_good",
        "label": "English strong report",
        "report": """# Research Summary
## Introduction
[HASH:aa11bb] This report compares durable checkpointing approaches across LangGraph and SQLite-backed persistence.
## Analysis
[HASH:aa11bb] [HASH:cc33dd] It explains recovery semantics, trade-offs, and failure boundaries.
## Conclusion
The final recommendation is explicit and bounded by cited evidence.
## References
https://example.com/checkpoint
https://example.com/sqlite
""",
    },
    {
        "id": "cn_bad",
        "label": "中文差报告",
        "report": """# 报告
这个东西应该挺强的，网上很多人都这么说。
没有来源，也没有结构。
""",
    },
    {
        "id": "degraded",
        "label": "含降级说明",
        "report": """# Incident Review
## Overview
[HASH:degrade01] Some sources were blocked.
## Gaps
- task=earnings | reason=HTTP 403 | stage=search
- task=docs | reason=content too short or blocked
## Conclusion
The report continues with explicit confidence limits.
## References
https://example.com/gap
""",
    },
    {
        "id": "en_sparse",
        "label": "English sparse citation",
        "report": """# Research
## Intro
The model is impressive and likely better than peers.
## Findings
Only one vague source is mentioned.
https://example.com/one
""",
    },
    {
        "id": "cn_table",
        "label": "中文结构化表格",
        "report": """# 财报对比
## 关键数据
| 指标 | 数值 |
| --- | --- |
| Revenue | 10 |
| Margin | 20% |
## 分析
[HASH:table01] 表格之后补充了趋势解释和引用。
## 结论
给出保守结论。
""",
    },
    {
        "id": "en_long",
        "label": "English long-form",
        "report": """# Long Report
## Background
[HASH:l01] LangGraph supports resumable execution with checkpoints.
## Retrieval
[HASH:l02] External retrieval costs can dominate LLM cost.
## Cost
[HASH:l03] Tavily credits and Serper pricing need to be tracked separately.
## Failure Modes
[HASH:l04] Recovery only works if knowledge snapshots are durable.
## Conclusion
This report is longer, structured, and explicitly bounded.
## References
https://example.com/1
https://example.com/2
https://example.com/3
""",
    },
    {
        "id": "cn_mojibake",
        "label": "乱码样本",
        "report": """# 閳閿泑閸檤閺佺増
## 鐠у嫭
鍐呭鏈夋槑鏄剧紪鐮侀棶棰橈紝鍙兘褰卞搷 RACE 鍒嗘暟。
https://example.com/mojibake
""",
    },
    {
        "id": "en_no_conclusion",
        "label": "English missing conclusion",
        "report": """# Analysis
## Intro
[HASH:nc01] A cited introduction.
## Findings
[HASH:nc02] Details and more details.
## Appendix
https://example.com/appendix
""",
    },
    {
        "id": "cn_refs_only",
        "label": "只有引用堆砌",
        "report": """# 引用堆砌
https://example.com/1
https://example.com/2
https://example.com/3
https://example.com/4
""",
    },
    {
        "id": "en_bilingual",
        "label": "mixed bilingual",
        "report": """# Mixed Report
## Overview
[HASH:mix01] This report mixes English and 中文 but remains coherent.
## Analysis
[HASH:mix02] 它同时比较成本、质量和恢复能力。
## Conclusion
Bounded conclusion with references.
https://example.com/mix
""",
    },
    {
        "id": "cn_complete",
        "label": "中文完整报告",
        "report": """# 研究报告
## 背景
[HASH:full01] 介绍问题背景与目标。
## 检索结果
[HASH:full02] 对多个来源进行交叉验证。
## 成本分析
[HASH:full03] 对 LLM 成本与外部检索成本分别记账。
## 风险与缺口
[HASH:full04] 明确列出资料缺口和恢复边界。
## 结论
给出默认档位建议与未来优化方向。
## References
https://example.com/full1
https://example.com/full2
""",
    },
]


def _call_judge(client: OpenAI, model_name: str, report_text: str) -> tuple[dict[str, Any] | None, str, float]:
    prompt = f"""You are a strict evaluator for deep research reports.

Evaluate the report on two axes from 1 to 10:
1. FACT: citation support, traceability, unsupported-claim risk
2. RACE: report architecture, coverage, coherence, and depth

Important:
- RACE here means Report Architecture, Coverage, Coherence, and depth.
- RACE does NOT mean ethnicity, race demographics, or fairness categories.

Report:
{report_text[:9000]}

Respond with strict JSON only:
{{
  "fact_score": 8.4,
  "race_score": 7.9,
  "fact_reason": "short reason",
  "race_reason": "short reason",
  "interpreted_race_axis": "report architecture coverage coherence depth"
}}
"""
    started = time.perf_counter()
    response = client.chat.completions.create(
        model=model_name,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.1,
        response_format={"type": "json_object"},
    )
    latency = time.perf_counter() - started
    raw = response.choices[0].message.content
    try:
        return json.loads(raw), raw, latency
    except Exception:
        return None, raw, latency


def _race_misread(parsed: dict[str, Any] | None) -> bool:
    if not parsed:
        return True
    axis = str(parsed.get("interpreted_race_axis", "")).lower()
    social_keywords = ("ethnicity", "racial", "demographic", "diversity", "bias", "种族")
    architecture_keywords = ("architecture", "coverage", "coherence", "depth", "structure")
    if any(keyword in axis for keyword in social_keywords):
        return True
    return not any(keyword in axis for keyword in architecture_keywords)


def _score_out_of_range(parsed: dict[str, Any] | None) -> bool:
    if not parsed:
        return True
    try:
        fact_score = float(parsed.get("fact_score"))
        race_score = float(parsed.get("race_score"))
    except Exception:
        return True
    return not (1.0 <= fact_score <= 10.0 and 1.0 <= race_score <= 10.0)


def _ranking_signature(payload: dict[str, Any]) -> list[str]:
    ordered = sorted(
        payload.get("results", []),
        key=lambda item: float(item.get("overall_score", 0.0)),
        reverse=True,
    )
    return [f"{item.get('research_mode')}::{item.get('query')[:36]}" for item in ordered]


def _load_rescore_source(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _latest_benchmark_payload() -> Path:
    candidates = sorted(REPORTS_DIR.glob("mode_benchmark_*_scored.json"))
    if candidates:
        return candidates[-1]
    raw_candidates = sorted(REPORTS_DIR.glob("mode_benchmark_*.json"))
    if raw_candidates:
        return raw_candidates[-1]
    raise FileNotFoundError("No benchmark payload found under reports/")


def run_bakeoff(rescore_path: Path) -> dict[str, Any]:
    client = OpenAI(api_key="ollama", base_url=LOCAL_JUDGE_BASE_URL)
    source_payload = _load_rescore_source(rescore_path)
    report: dict[str, Any] = {"source_benchmark": str(rescore_path), "models": {}}

    for model_name in MODELS:
        sample_runs: list[dict[str, Any]] = []
        for sample in SAMPLES:
            repeats = 3 if sample["id"] in ANCHOR_SAMPLE_IDS else 1
            for repeat_idx in range(repeats):
                parsed, raw, latency = _call_judge(client, model_name, sample["report"])
                sample_runs.append(
                    {
                        "sample_id": sample["id"],
                        "label": sample["label"],
                        "repeat": repeat_idx + 1,
                        "parsed": parsed,
                        "raw": raw,
                        "latency_seconds": round(latency, 4),
                        "json_ok": parsed is not None,
                        "out_of_range": _score_out_of_range(parsed),
                        "race_misread": _race_misread(parsed),
                    }
                )

        grouped_anchor_scores: dict[str, list[tuple[float, float]]] = {}
        for item in sample_runs:
            if item["sample_id"] in ANCHOR_SAMPLE_IDS and item["parsed"]:
                grouped_anchor_scores.setdefault(item["sample_id"], []).append(
                    (
                        float(item["parsed"]["fact_score"]),
                        float(item["parsed"]["race_score"]),
                    )
                )

        anchor_stability = {}
        for sample_id, runs in grouped_anchor_scores.items():
            fact_scores = [fact for fact, _ in runs]
            race_scores = [race for _, race in runs]
            anchor_stability[sample_id] = {
                "fact_spread": round(max(fact_scores) - min(fact_scores), 4),
                "race_spread": round(max(race_scores) - min(race_scores), 4),
            }

        rescored = summarize_payload(source_payload, judge_model=model_name, allow_local_judge=True)
        report["models"][model_name] = {
            "sample_runs": sample_runs,
            "sample_summary": {
                "json_parse_rate": round(sum(1 for item in sample_runs if item["json_ok"]) / len(sample_runs), 4),
                "out_of_range_rate": round(sum(1 for item in sample_runs if item["out_of_range"]) / len(sample_runs), 4),
                "race_misread_rate": round(sum(1 for item in sample_runs if item["race_misread"]) / len(sample_runs), 4),
                "avg_latency_seconds": round(statistics.mean(item["latency_seconds"] for item in sample_runs), 4),
                "anchor_stability": anchor_stability,
            },
            "rescore_summary": rescored.get("mode_summary", {}),
            "rescore_ranking_signature": _ranking_signature(rescored),
        }

    llama = report["models"]["llama3.1:latest"]["sample_summary"]
    qwen = report["models"]["qwen3:8b"]["sample_summary"]
    report["decision"] = {
        "default_judge": "qwen3:8b"
        if (
            qwen["race_misread_rate"] < llama["race_misread_rate"]
            or (
                qwen["race_misread_rate"] == llama["race_misread_rate"]
                and qwen["json_parse_rate"] >= llama["json_parse_rate"]
            )
        )
        else "llama3.1:latest",
        "fallback_judge": "llama3.1:latest",
    }
    return report


def write_report(payload: dict[str, Any]) -> tuple[Path, Path]:
    stamp = time.strftime("%Y%m%d_%H%M%S")
    json_path = REPORTS_DIR / f"judge_bakeoff_{stamp}.json"
    md_path = REPORTS_DIR / f"judge_bakeoff_{stamp}.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# Judge Bakeoff",
        "",
        f"- Source benchmark: `{payload['source_benchmark']}`",
        f"- Default judge: `{payload['decision']['default_judge']}`",
        f"- Fallback judge: `{payload['decision']['fallback_judge']}`",
        "",
        "| Model | JSON Parse Rate | Out-of-Range Rate | RACE Misread Rate | Avg Latency (s) |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for model_name in MODELS:
        summary = payload["models"][model_name]["sample_summary"]
        lines.append(
            f"| {model_name} | {summary['json_parse_rate']:.2%} | {summary['out_of_range_rate']:.2%} | "
            f"{summary['race_misread_rate']:.2%} | {summary['avg_latency_seconds']:.2f} |"
        )
    lines.append("")
    for model_name in MODELS:
        lines.append(f"## {model_name}")
        lines.append("")
        lines.append(f"- Ranking signature: `{payload['models'][model_name]['rescore_ranking_signature']}`")
        lines.append(f"- Anchor stability: `{payload['models'][model_name]['sample_summary']['anchor_stability']}`")
        lines.append("")

    md_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, md_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Run local judge bakeoff for llama3.1 vs qwen3.")
    parser.add_argument("--rescore-source", type=str, default="", help="Benchmark payload to offline-rescore.")
    args = parser.parse_args()
    source = Path(args.rescore_source).resolve() if args.rescore_source else _latest_benchmark_payload()
    payload = run_bakeoff(source)
    json_path, md_path = write_report(payload)
    print(f"[judge_bakeoff] json={json_path}")
    print(f"[judge_bakeoff] markdown={md_path}")


if __name__ == "__main__":
    main()
