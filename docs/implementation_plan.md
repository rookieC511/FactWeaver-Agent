# Implementation Plan - Interactive Review & Chinese Support

## Goal
Enable users to interactively modify the research outline, fix "Tofu" (missing font) issues in charts, and ensure images render correctly in the report.

## User Review Required
> [!IMPORTANT]
> This change introduces a blocking user input step in the `writer_graph.py`. The execution will pause waiting for user feedback in the console.

## Proposed Changes

### Logic & State Management
#### [MODIFY] [writer_graph.py](file:///d:/Projects/deepresearch-agent/writer_graph.py)
1.  **Update `WriterState`**: Add `user_feedback: str` field.
2.  **Update `human_review_node`**:
    - Change input prompt to accept text.
    - If text provided (not just 'q' or 'r' or empty), treat as feedback.
    - Return `{"user_feedback": input_text, "outline": [], "iteration": +1}` to trigger regeneration.
3.  **Update `skeleton_node`**:
    - Include `state['user_feedback']` in the prompt to R1 if present.
4.  **Update `chart_scout_node`**:
    - Convert absolute image paths to relative paths `public/charts/filename.png` when embedding in Markdown. This fixes the Ctrl+Click issue in VS Code.

### Visualization validity
#### [MODIFY] [charts.py](file:///d:/Projects/deepresearch-agent/charts.py)
1.  **Add Font Configuration**:
    - Detect OS or blindly set a list of fallback fonts for Chinese support (`Microsoft YaHei`, `SimHei`, `Arial Unicode MS`).
    - Set `axes.unicode_minus = False`.

## Verification Plan

### Automated Tests
- **Chart Font Test**: Create/Run `test_charts_font.py` that generates a chart with Chinese title.
    - *Action*: Check if file is created. Visual check (by user) required for "Tofu", but code will ensure config is set.

### Manual Verification
- **Run `main.py`**:
    1.  Wait for Outline Review.
    2.  Enter feedback: "Please add a section about 'Impact on Gaming'".
    3.  Verify the new outline contains the requested section.
    4.  Verify final report has correct Chinese in charts and images are click-accessible.
