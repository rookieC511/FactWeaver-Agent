from core.writer_graph import audit_final_doc


def test_audit_final_doc_flags_missing_direct_answer_and_weak_analysis():
    audit = audit_final_doc(
        "## Key Evidence\n- item [HASH:abc]\n\n## Analysis\nThis section summarizes facts.\n",
        "Compare A vs B and explain the risks",
        {
            "must_answer_points": [
                {"id": "1", "section_id": "1", "question": "Compare A and B"},
                {"id": "2", "section_id": "1", "question": "Explain the main risks"},
            ]
        },
        {
            "1": {"covered": True, "high_authority_source_count": 1},
            "2": {"covered": False, "high_authority_source_count": 0},
        },
    )
    assert audit["passed"] is False
    assert "missing_direct_answer" in audit["missing_requirements"]
    assert "analysis_signals_too_weak" in audit["missing_requirements"]


def test_audit_final_doc_passes_when_answer_and_analysis_are_present():
    report = """## Direct Answer / Core Conclusion
A is more resilient than B because it has lower leverage and stronger cash flow [HASH:abc].

## Key Evidence
- Evidence line [HASH:abc]

## Analysis
Compared with B, A has better margins, because its fixed-cost base is smaller. The main risk is supply volatility.

## Uncertainty / Missing Evidence
- Limited regional disclosure.
"""
    audit = audit_final_doc(
        report,
        "Compare A vs B and explain the risks",
        {
            "must_answer_points": [
                {"id": "1", "section_id": "1", "question": "Compare A and B"},
                {"id": "2", "section_id": "1", "question": "Explain the main risks"},
            ]
        },
        {
            "1": {"covered": True, "high_authority_source_count": 1},
            "2": {"covered": True, "high_authority_source_count": 1},
        },
    )
    assert audit["passed"] is True
    assert audit["direct_answer_present"] is True
    assert audit["analysis_signal_count"] >= 2


def test_audit_final_doc_counts_nested_analysis_sections():
    report = """## Direct Answer / Core Conclusion
A is stronger than B because it has lower leverage [HASH:abc].

## Key Evidence
- Evidence line [HASH:abc]

## Analysis
#### Comparative Analysis
Compared with B, A has stronger margins.

#### Causal Analysis
Because fixed costs are lower, A converts revenue to profit more efficiently.

#### Risk Analysis
The main risk is demand volatility.

## Uncertainty / Missing Evidence
- Regional disclosures are incomplete.
"""
    audit = audit_final_doc(
        report,
        "Compare A vs B and explain why and the main risks",
        {
            "must_answer_points": [
                {"id": "1", "section_id": "1", "question": "Compare A and B"},
                {"id": "2", "section_id": "1", "question": "Explain why"},
                {"id": "3", "section_id": "1", "question": "What are the main risks"},
            ]
        },
        {
            "1": {"covered": True, "high_authority_source_count": 1},
            "2": {"covered": True, "high_authority_source_count": 1},
            "3": {"covered": True, "high_authority_source_count": 1},
        },
    )
    assert audit["comparison_present"] is True
    assert audit["causal_present"] is True
    assert audit["risk_present"] is True
    assert audit["analysis_signal_count"] == 3
