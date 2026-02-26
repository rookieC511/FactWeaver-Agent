import unittest
from unittest.mock import MagicMock, patch
import os
import io

# Mock dependencies before importing writer_graph
sys_modules_patch = patch.dict('sys.modules', {
    'models': MagicMock(),
    'memory': MagicMock(),
    'tools': MagicMock(),
    'langgraph': MagicMock(),
    'langgraph.graph': MagicMock()
})
sys_modules_patch.start()

# Now import the node to test
# We need to manually define the function since we can't easily import from writer_graph 
# due to its heavy dependencies (unless we mock them all perfectly).
# A better approach given the environment: Copy the node logic to test it, 
# OR import carefully.
# Let's try to import `charts` and test `generate_chart` first, 
# then mock the node logic flow.

from charts import generate_chart

class TestChartGen(unittest.TestCase):
    def test_generate_chart(self):
        print("\nTesting charts.py...")
        data = {
            "labels": ["A", "B", "C"],
            "datasets": [{"label": "Test", "data": [1, 2, 3]}]
        }
        filename = "test_unit_chart.png"
        path = generate_chart("bar", data, "Test Chart", filename)
        
        self.assertTrue(path.endswith(filename))
        self.assertTrue(os.path.exists(path))
        print(f"✅ Generated: {path}")

    @patch('models.llm_smart')
    def test_chart_scout_logic(self, mock_llm):
        print("\nTesting Chart Scout Logic match...")
        # Simulate the logic inside chart_scout_node
        
        # 1. Mock LLM Response
        mock_response = MagicMock()
        mock_response.content = """
        ```json
        {
            "charts": [
                {
                    "target_section_id": "1",
                    "type": "line",
                    "title": "Mock Revenue",
                    "filename": "mock_rev.png",
                    "data": {
                        "labels": ["2020", "2021"],
                        "datasets": [{"label": "Rev", "data": [100, 200]}]
                    }
                }
            ]
        }
        ```
        """
        # We can't easily mock the clean_json_output import without reloading module
        # So we will verify the chart generation part mainly.
        
        # Manually invoke generate_chart based on this mock data
        import json
        res = json.loads(mock_response.content.replace("```json", "").replace("```", ""))
        chart = res['charts'][0]
        
        path = generate_chart(chart['type'], chart['data'], chart['title'], chart['filename'])
        self.assertTrue(os.path.exists(path))
        
        # Verify outline update logic
        outline = [{"id": "1", "title": "Intro", "description": "Discuss revenue"}]
        sec_id = chart['target_section_id']
        for sec in outline:
            if sec['id'] == sec_id:
                sec['description'] += f"\n[IMPORTANT] Must embed generated chart: ![{chart['title']}]({path})"
        
        self.assertIn("Must embed generated chart", outline[0]['description'])
        print("✅ Outline updated successfully")

if __name__ == '__main__':
    unittest.main()
