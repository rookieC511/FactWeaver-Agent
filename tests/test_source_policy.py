from core.source_policy import classify_source, infer_topic_family, rank_search_results


def test_infer_topic_family_prefers_finance_business_hints():
    assert infer_topic_family("NVIDIA earnings guidance and gross margin") == "finance_business"


def test_classify_source_marks_social_and_weak():
    meta = classify_source(
        "https://www.reddit.com/r/stocks/comments/demo",
        title="Reddit discussion",
        snippet="opinion thread",
        query="company earnings analysis",
    )
    assert meta["is_social"] is True
    assert meta["source_tier"] == "weak"
    assert float(meta["authority_score"]) < 0.55


def test_rank_search_results_boosts_authority_sources():
    ranked = rank_search_results(
        [
            {
                "url": "https://reddit.com/r/example",
                "title": "community reaction",
                "content": "social chatter",
            },
            {
                "url": "https://www.sec.gov/Archives/demo.pdf",
                "title": "Annual report PDF",
                "content": "official filing and report",
            },
        ],
        "latest company annual report and earnings",
    )
    assert ranked[0]["url"].startswith("https://www.sec.gov")
    assert ranked[0]["source_tier"] == "high_authority"
