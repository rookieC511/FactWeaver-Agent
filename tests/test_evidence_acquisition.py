import pytest

from core.evidence_acquisition.evidence_gate import (
    build_evidence_slots,
    compute_coverage_summary,
    evaluate_evidence_gate,
)
from core.evidence_acquisition.fetch_pipeline import (
    build_access_backfill_query,
    fetch_source_candidate,
    rank_access_backfill_candidates,
    should_force_access_backfill,
    should_force_non_pdf_access_backfill,
)
from core.evidence_acquisition.qualification import (
    admit_source_candidates,
    qualify_search_results,
)
from core.evidence_acquisition.retrieval import staged_candidate_recall
from langchain_core.documents import Document


class DummyKM:
    def __init__(self, docs_by_section):
        self.docs_by_section = docs_by_section

    def retrieve(self, section_id="global", k=20):
        return list(self.docs_by_section.get(section_id, []))[:k]


def test_admit_source_candidates_filters_weak_sources_from_main_path():
    candidates = qualify_search_results(
        [
            {"url": "https://reddit.com/r/demo", "title": "community", "content": "social"},
            {"url": "https://www.sec.gov/Archives/demo.pdf", "title": "Annual report PDF", "content": "official filing"},
            {"url": "https://www.reuters.com/example", "title": "Reuters", "content": "reported analysis"},
        ],
        "latest earnings and annual report",
    )
    admitted = admit_source_candidates(candidates, strict_topic=True, max_main=4)
    assert admitted
    assert all(item["source_tier"] == "high_authority" for item in admitted)


def test_admit_source_candidates_excludes_object_storage_hosts():
    admitted = admit_source_candidates(
        [
            {
                "url": "https://s3.us-west-1.wasabisys.com/p-library/books/pots/POTS.522.pdf",
                "title": "POTS",
                "content": "report",
                "host": "s3.us-west-1.wasabisys.com",
                "source_tier": "high_authority",
                "authority_score": 1.2,
                "is_official": True,
                "is_social": False,
                "is_aggregator": False,
                "is_pdf": True,
                "topic_family": "finance_business",
                "fit_score": 1.3,
            },
            {
                "url": "https://www.sec.gov/Archives/demo.pdf",
                "title": "SEC filing",
                "content": "official filing",
                "host": "www.sec.gov",
                "source_tier": "high_authority",
                "authority_score": 1.1,
                "is_official": True,
                "is_social": False,
                "is_aggregator": False,
                "is_pdf": True,
                "topic_family": "finance_business",
                "fit_score": 1.2,
            },
        ],
        strict_topic=True,
        max_main=4,
    )
    assert [item["host"] for item in admitted] == ["www.sec.gov"]


def test_pdf_hosts_force_access_backfill_queries():
    rosap = {
        "url": "https://rosap.ntl.bts.gov/view/dot/82811/dot_82811_DS1.pdf",
        "title": "Rail report",
        "host": "rosap.ntl.bts.gov",
        "is_pdf": True,
    }
    hku = {
        "url": "https://hub.hku.hk/bitstream/10722/50521/6/FullText.pdf",
        "title": "Repository thesis",
        "host": "hub.hku.hk",
        "is_pdf": True,
    }
    s3_obj = {
        "url": "https://s3.us-west-1.wasabisys.com/p-library/books/pots/POTS.522.pdf",
        "title": "POTS",
        "host": "s3.us-west-1.wasabisys.com",
        "is_pdf": True,
    }

    assert should_force_access_backfill(rosap) is True
    assert should_force_access_backfill(hku) is True
    assert should_force_access_backfill(s3_obj) is True
    assert build_access_backfill_query(rosap, "rail safety") == 'site:rosap.ntl.bts.gov/view/dot "Rail report" -filetype:pdf'
    assert build_access_backfill_query(hku, "repository research") == 'site:hub.hku.hk/handle "Repository thesis" -filetype:pdf'
    assert build_access_backfill_query(s3_obj, "shipping report").startswith('"POTS" shipping report')


def test_rank_access_backfill_candidates_prefers_non_pdf_landing_pages():
    original = {
        "url": "https://hub.hku.hk/bitstream/10722/50521/6/FullText.pdf",
        "host": "hub.hku.hk",
        "title": "Repository thesis",
        "is_pdf": True,
        "source_tier": "high_authority",
    }
    ranked = rank_access_backfill_candidates(
        original,
        [
            {
                "url": "https://hub.hku.hk/bitstream/10722/50521/6/FullText.pdf",
                "host": "hub.hku.hk",
                "source_tier": "high_authority",
                "fit_score": 1.4,
                "is_pdf": True,
            },
            {
                "url": "https://hub.hku.hk/handle/10722/50521",
                "host": "hub.hku.hk",
                "source_tier": "high_authority",
                "fit_score": 0.9,
                "is_pdf": False,
            },
            {
                "url": "https://hub.hku.hk/record/10722/50521",
                "host": "hub.hku.hk",
                "source_tier": "standard",
                "fit_score": 0.8,
                "is_pdf": False,
            },
        ],
    )
    assert ranked
    assert ranked[0]["url"] == "https://hub.hku.hk/handle/10722/50521"
    assert all(not item.get("is_pdf") for item in ranked)


def test_non_pdf_blocked_host_triggers_same_host_backfill():
    candidate = {
        "url": "https://www.wshblaw.com/adas-liability-overview",
        "host": "www.wshblaw.com",
        "title": "ADAS liability overview",
        "is_pdf": False,
        "source_tier": "high_authority",
    }
    fetched = {
        "attempts": [
            {"provider": "jina", "error_class": "http_401_403"},
            {"provider": "direct_http", "error_class": "js_only"},
        ]
    }
    assert should_force_non_pdf_access_backfill(candidate, fetched, attempted_hosts=set()) is True
    query = build_access_backfill_query(candidate, "ADAS liability allocation and case law")
    assert query.startswith('site:www.wshblaw.com')
    assert "-filetype:pdf" in query


@pytest.mark.asyncio
async def test_staged_candidate_recall_triggers_authority_query_for_strict_topics():
    calls = []

    async def fake_search(query: str, max_results: int):
        calls.append(query)
        if "official report pdf" in query:
            return [
                {"url": "https://www.sec.gov/Archives/demo.pdf", "title": "SEC filing", "content": "official filing"}
            ]
        return [{"url": "https://reddit.com/r/demo", "title": "community", "content": "social"}]

    recall = await staged_candidate_recall(
        query="NVIDIA gross margin",
        task_desc="NVIDIA gross margin and earnings guidance",
        max_results=4,
        search_fn=fake_search,
    )
    assert len(calls) >= 1
    assert recall["strict_topic"] is True
    assert any("official report pdf" in query for query in recall["search_queries"])
    assert all(item["source_tier"] == "high_authority" for item in recall["candidates"])


def test_evidence_gate_blocks_writer_when_high_authority_support_is_missing():
    km = DummyKM(
        {
            "1": [
                Document(
                    page_content="A short but usable fact block",
                    metadata={"source_tier": "standard", "authority_score": 0.7, "url": "https://example.com/a"},
                )
            ]
        }
    )
    slots = build_evidence_slots(
        task_contract={
            "must_answer_points": [
                {"id": "1", "section_id": "1", "question": "What is the main conclusion?"},
            ]
        },
        km=km,
    )
    coverage = compute_coverage_summary(
        plan=[{"section_id": "1"}],
        km=km,
        retrieval_metrics={"search_result_count": 3, "authority_hits": 0, "weak_source_hits": 1, "fetch_attempts": 1},
        evidence_slots=slots,
    )
    gate = evaluate_evidence_gate(coverage_summary=coverage, evidence_slots=slots)
    assert gate["passed"] is False
    assert gate["needs_backfill"] is True


def test_compute_coverage_summary_aggregates_blocked_breakdown():
    km = DummyKM({})
    coverage = compute_coverage_summary(
        plan=[{"section_id": "1"}],
        km=km,
        retrieval_metrics={
            "search_result_count": 4,
            "authority_hits": 2,
            "weak_source_hits": 1,
            "retrieval_recall_wall_seconds": 0.45,
            "access_backfill_wall_seconds": 0.12,
            "targeted_backfill_wall_seconds": 0.33,
            "evidence_acquisition_wall_seconds": 1.2,
        },
        evidence_slots={},
        fetch_results=[
            {
                "url": "https://a.example.com/doc",
                "provider": "jina",
                "page_type": "general_html",
                "host": "a.example.com",
                "status": "failed",
                "error_class": "http_401_403",
                "elapsed_ms": 110.0,
            },
            {
                "url": "https://a.example.com/doc",
                "provider": "direct_http",
                "page_type": "general_html",
                "host": "a.example.com",
                "status": "ok",
                "error_class": "",
                "elapsed_ms": 80.0,
            },
            {
                "url": "https://b.example.com/report.pdf",
                "provider": "pdf_parser",
                "page_type": "pdf",
                "host": "b.example.com",
                "status": "ok",
                "error_class": "",
                "elapsed_ms": 150.0,
            },
            {
                "url": "https://c.example.com/app",
                "provider": "visual_browse",
                "page_type": "js_heavy",
                "host": "c.example.com",
                "status": "ok",
                "error_class": "",
                "elapsed_ms": 200.0,
            },
        ],
    )
    assert coverage["blocked_attempt_rate"] == 0.25
    assert coverage["blocked_source_rate"] == 0.0
    assert coverage["fetch_wall_seconds"] == 0.54
    assert coverage["blocked_fetch_wall_seconds"] == 0.11
    assert coverage["retrieval_recall_wall_seconds"] == 0.45
    assert coverage["access_backfill_wall_seconds"] == 0.12
    assert coverage["targeted_backfill_wall_seconds"] == 0.33
    assert coverage["evidence_acquisition_wall_seconds"] == 1.2
    assert coverage["blocked_by_provider"] == {}
    assert coverage["blocked_by_page_type"] == {}
    assert coverage["blocked_by_host"] == {}
    assert coverage["blocked_after_jina_but_direct_ok"] == 1
    assert coverage["pdf_parser_salvage_rate"] == 1.0
    assert coverage["visual_fallback_salvage_rate"] == 1.0


def test_compute_coverage_summary_counts_unsalvaged_host_as_blocked_source():
    km = DummyKM({})
    coverage = compute_coverage_summary(
        plan=[{"section_id": "1"}],
        km=km,
        retrieval_metrics={"search_result_count": 2, "authority_hits": 1, "weak_source_hits": 0},
        evidence_slots={},
        fetch_results=[
            {
                "url": "https://arxiv.org/html/2404.17044v1",
                "provider": "direct_http",
                "page_type": "js_heavy",
                "host": "arxiv.org",
                "status": "needs_visual",
                "error_class": "js_only",
                "elapsed_ms": 100.0,
            },
            {
                "url": "https://arxiv.org/html/2404.17044v1",
                "provider": "tavily_extract",
                "page_type": "official_html",
                "host": "arxiv.org",
                "status": "ok",
                "error_class": "",
                "elapsed_ms": 60.0,
            },
            {
                "url": "https://blocked.example.com/article",
                "provider": "direct_http",
                "page_type": "official_html",
                "host": "blocked.example.com",
                "status": "failed",
                "error_class": "http_401_403",
                "elapsed_ms": 90.0,
            },
        ],
    )
    assert coverage["blocked_attempt_rate"] == 0.6667
    assert coverage["blocked_source_rate"] == 0.5
    assert coverage["blocked_by_host"] == {"blocked.example.com": 1}


@pytest.mark.asyncio
async def test_fetch_source_candidate_prefers_extract_for_arxiv_host(monkeypatch):
    import core.evidence_acquisition.fetch_pipeline as pipeline

    calls = []

    async def fake_tavily(url, **kwargs):
        calls.append("tavily_extract")
        return (
            {
                "provider": "tavily_extract",
                "status": "ok",
                "content_length": 600,
                "final_url": url,
                "error_class": "",
                "http_status": 200,
                "content_type": "text/html",
                "attempt_order": kwargs["attempt_order"],
                "page_type": kwargs["page_type"],
                "host": kwargs["host"],
                "salvaged_by_fallback": False,
                "blocked_stage": "",
                "authority_preserved": kwargs["authority_preserved"],
            },
            "paper content " * 80,
            0.0,
        )

    async def fake_direct(url, **kwargs):
        calls.append("direct_http")
        return (
            {
                "provider": "direct_http",
                "status": "failed",
                "content_length": 0,
                "final_url": url,
                "error_class": "js_only",
                "http_status": 200,
                "content_type": "text/html",
                "attempt_order": kwargs["attempt_order"],
                "page_type": kwargs["page_type"],
                "host": kwargs["host"],
                "salvaged_by_fallback": False,
                "blocked_stage": "direct_http",
                "authority_preserved": kwargs["authority_preserved"],
            },
            "",
        )

    monkeypatch.setattr(pipeline, "_attempt_tavily_extract", fake_tavily)
    monkeypatch.setattr(pipeline, "_attempt_direct_http", fake_direct)

    fetched = await fetch_source_candidate(
        {
            "url": "https://arxiv.org/html/2404.17044v1",
            "host": "arxiv.org",
            "source_tier": "high_authority",
            "is_official": True,
        },
        allow_visual=False,
        goal="ADAS liability analysis",
    )
    assert calls == ["tavily_extract"]
    assert fetched["status"] == "ok"
    assert "fetch_wall_seconds" in fetched
    assert fetched["fetch_wall_seconds"] >= 0.0
    assert "elapsed_ms" in fetched["attempts"][0]


@pytest.mark.asyncio
async def test_fetch_source_candidate_uses_pdf_first(monkeypatch):
    import core.evidence_acquisition.fetch_pipeline as pipeline

    calls = []

    async def fake_pdf(url, **kwargs):
        calls.append("pdf_parser")
        return (
            {
                "provider": "pdf_parser",
                "status": "ok",
                "content_length": 300,
                "final_url": url,
                "error_class": "",
                "http_status": 200,
                "content_type": "application/pdf",
                "attempt_order": kwargs["attempt_order"],
                "page_type": kwargs["page_type"],
                "host": kwargs["host"],
                "salvaged_by_fallback": False,
                "blocked_stage": "",
                "authority_preserved": kwargs["authority_preserved"],
            },
            "pdf text " * 40,
        )

    async def fake_tavily(url, **kwargs):
        calls.append("tavily_extract")
        return (
            {
                "provider": "tavily_extract",
                "status": "failed",
                "content_length": 0,
                "final_url": url,
                "error_class": "empty_content",
                "http_status": 0,
                "content_type": "text/plain",
                "attempt_order": kwargs["attempt_order"],
                "page_type": kwargs["page_type"],
                "host": kwargs["host"],
                "salvaged_by_fallback": False,
                "blocked_stage": "",
                "authority_preserved": kwargs["authority_preserved"],
            },
            "",
            0.0,
        )

    monkeypatch.setattr(pipeline, "_attempt_pdf_parser", fake_pdf)
    monkeypatch.setattr(pipeline, "_attempt_tavily_extract", fake_tavily)

    result = await fetch_source_candidate(
        {"url": "https://example.com/report.pdf", "is_pdf": True, "source_tier": "high_authority", "host": "example.com"},
        allow_visual=False,
        goal="extract evidence",
    )
    assert calls == ["pdf_parser"]
    assert result["provider"] == "pdf_parser"
    assert result["status"] == "ok"


@pytest.mark.asyncio
async def test_fetch_source_candidate_uses_official_direct_http_before_jina(monkeypatch):
    import core.evidence_acquisition.fetch_pipeline as pipeline

    calls = []

    async def fake_direct(url, **kwargs):
        calls.append("direct_http")
        return (
            {
                "provider": "direct_http",
                "status": "ok",
                "content_length": 320,
                "final_url": url,
                "error_class": "",
                "http_status": 200,
                "content_type": "text/html",
                "attempt_order": kwargs["attempt_order"],
                "page_type": kwargs["page_type"],
                "host": kwargs["host"],
                "salvaged_by_fallback": False,
                "blocked_stage": "",
                "authority_preserved": kwargs["authority_preserved"],
            },
            "official text " * 40,
        )

    async def fake_jina(url, **kwargs):
        calls.append("jina")
        return (
            {
                "provider": "jina",
                "status": "failed",
                "content_length": 0,
                "final_url": url,
                "error_class": "empty_content",
                "http_status": 200,
                "content_type": "text/plain",
                "attempt_order": kwargs["attempt_order"],
                "page_type": kwargs["page_type"],
                "host": kwargs["host"],
                "salvaged_by_fallback": False,
                "blocked_stage": "",
                "authority_preserved": kwargs["authority_preserved"],
            },
            "",
        )

    monkeypatch.setattr(pipeline, "_attempt_direct_http", fake_direct)
    monkeypatch.setattr(pipeline, "_attempt_jina", fake_jina)

    result = await fetch_source_candidate(
        {"url": "https://sec.gov/doc", "is_official": True, "source_tier": "high_authority", "host": "sec.gov"},
        allow_visual=False,
        goal="extract evidence",
    )
    assert calls == ["direct_http"]
    assert result["provider"] == "direct_http"


@pytest.mark.asyncio
async def test_fetch_source_candidate_does_not_visualize_pdf_candidates(monkeypatch):
    import core.evidence_acquisition.fetch_pipeline as pipeline

    calls = []

    async def fake_pdf(url, **kwargs):
        calls.append("pdf_parser")
        return (
            {
                "provider": "pdf_parser",
                "status": "failed",
                "content_length": 0,
                "final_url": url,
                "error_class": "http_401_403",
                "http_status": 403,
                "content_type": "application/pdf",
                "attempt_order": kwargs["attempt_order"],
                "page_type": kwargs["page_type"],
                "host": kwargs["host"],
                "salvaged_by_fallback": False,
                "blocked_stage": "pdf_parser",
                "authority_preserved": kwargs["authority_preserved"],
            },
            "",
        )

    async def fake_tavily(url, **kwargs):
        calls.append("tavily_extract")
        return (
            {
                "provider": "tavily_extract",
                "status": "failed",
                "content_length": 0,
                "final_url": url,
                "error_class": "empty_content",
                "http_status": 0,
                "content_type": "text/plain",
                "attempt_order": kwargs["attempt_order"],
                "page_type": kwargs["page_type"],
                "host": kwargs["host"],
                "salvaged_by_fallback": False,
                "blocked_stage": "",
                "authority_preserved": kwargs["authority_preserved"],
            },
            "",
            0.0,
        )

    async def fake_visual(url, **kwargs):
        calls.append("visual_browse")
        return (
            {
                "provider": "visual_browse",
                "status": "ok",
                "content_length": 320,
                "final_url": url,
                "error_class": "",
                "http_status": 200,
                "content_type": "text/plain",
                "attempt_order": kwargs["attempt_order"],
                "page_type": "js_heavy",
                "host": kwargs["host"],
                "salvaged_by_fallback": True,
                "blocked_stage": "",
                "authority_preserved": kwargs["authority_preserved"],
            },
            "visual text " * 30,
        )

    monkeypatch.setattr(pipeline, "_attempt_pdf_parser", fake_pdf)
    monkeypatch.setattr(pipeline, "_attempt_tavily_extract", fake_tavily)
    monkeypatch.setattr(pipeline, "_attempt_visual", fake_visual)

    result = await fetch_source_candidate(
        {
            "url": "https://rosap.ntl.bts.gov/view/dot/82811/dot_82811_DS1.pdf",
            "host": "rosap.ntl.bts.gov",
            "is_pdf": True,
            "source_tier": "high_authority",
        },
        allow_visual=True,
        goal="extract evidence",
    )

    assert calls == ["pdf_parser", "tavily_extract"]
    assert result["provider"] != "visual_browse"
