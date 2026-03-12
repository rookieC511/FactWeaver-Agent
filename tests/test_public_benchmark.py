from scripts.public_benchmark_deepsearchqa import (
    evaluate_prediction,
    extract_answer_from_report,
    stratified_sample,
)


def test_single_answer_evaluation_matches_normalized_text():
    result = evaluate_prediction({"final_answer": "New Zealand", "final_answers": []}, "new zealand", "Single Answer")
    assert result["exact_match"] == 1.0
    assert result["f1"] == 1.0


def test_set_answer_evaluation_matches_normalized_set():
    result = evaluate_prediction(
        {"final_answer": "Combat, Utility, Skilling", "final_answers": ["Combat", "Utility", "Skilling"]},
        "Combat, Skilling, Utility",
        "Set Answer",
    )
    assert result["exact_match"] == 1.0
    assert result["f1"] == 1.0


def test_heuristic_extractor_reads_final_answer_heading():
    report = """# Report

## Analysis
Lots of details.

## Final Answer
Austria, Switzerland, Singapore
"""
    extracted = extract_answer_from_report(report, answer_type="Set Answer", extractor_model="")
    assert extracted["final_answers"] == ["Austria", "Switzerland", "Singapore"]


def test_stratified_sample_round_robins_categories():
    rows = [
        {"problem_category": "A", "problem": "1"},
        {"problem_category": "A", "problem": "2"},
        {"problem_category": "B", "problem": "3"},
        {"problem_category": "B", "problem": "4"},
        {"problem_category": "C", "problem": "5"},
    ]
    sample = stratified_sample(rows, sample_size=4, seed=7)
    categories = {row["problem_category"] for row in sample}
    assert len(sample) == 4
    assert categories == {"A", "B", "C"}
