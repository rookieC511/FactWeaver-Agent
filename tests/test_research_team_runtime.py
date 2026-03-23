import pytest

from core.memory import activate_session, cleanup_session_km, get_current_km, reset_active_session
from core.research_team_runtime import build_evidence_digest, fallback_verifier_assessment


def test_fallback_verifier_assessment_distinguishes_authority_gap():
    result = fallback_verifier_assessment(
        coverage_summary={"high_authority_source_count": 0, "direct_answer_support_rate": 0.5},
        open_gaps=[{"slot_id": "1", "gap_reason": "missing_high_authority_support"}],
        code_gate_passed=False,
    )

    assert result["verifier_decision"] == "insufficient_authority"
    assert result["semantic_sufficiency"] is False


@pytest.mark.asyncio
async def test_build_evidence_digest_caps_refs_and_snippets():
    token = activate_session("research-digest")
    km = get_current_km()
    km.clear()
    long_text = "authoritative evidence " * 40
    for idx in range(4):
        km.add_compact_document(
            long_text,
            f"https://example.com/{idx}",
            f"Doc {idx}",
            section_id="1",
            extra_metadata={"source_tier": "high_authority", "authority_score": 0.99},
        )

    try:
        digest = build_evidence_digest(
            task_contract={"must_answer_points": [{"id": "1", "section_id": "1", "question": "What happened?"}]},
            evidence_slots={"1": {"question": "What happened?"}},
            slot_statuses={"1": {"status": "satisfied", "high_authority_source_count": 2}},
            clause_statuses={"1": {"status": "satisfied", "question": "What happened?"}},
            open_gaps=[],
            coverage_summary={"high_authority_source_count": 2, "direct_answer_support_rate": 1.0},
            km=km,
            max_refs_per_slot=2,
            snippet_chars=80,
        )
        refs = digest["supporting_evidence_refs"]["1"]
        assert len(refs) == 2
        assert all(len(item["snippet"]) <= 83 for item in refs)
    finally:
        cleanup_session_km("research-digest")
        reset_active_session(token)
