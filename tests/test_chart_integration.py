import os

from core.charts import generate_chart
from core.writer_graph import continue_to_writers


def test_generate_chart_creates_png(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    data = {
        "labels": ["A", "B", "C"],
        "datasets": [{"label": "Test", "data": [1, 2, 3]}],
    }
    filename = "test_unit_chart.png"
    path = generate_chart("bar", data, "Test Chart", filename)

    assert path.endswith(filename)
    assert os.path.exists(path)


def test_writer_flow_routes_directly_to_section_writer():
    next_step = continue_to_writers(
        {
            "outline": [{"id": "1", "title": "Intro", "description": "Discuss revenue"}],
            "iteration": 0,
            "user_feedback": "",
            "task_contract": {"must_answer_points": []},
            "required_analysis_modes": [],
        }
    )

    assert isinstance(next_step, list)
    assert next_step
    assert next_step[0].node == "section_writer"
