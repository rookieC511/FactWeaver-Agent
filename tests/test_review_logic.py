
import unittest
from unittest.mock import MagicMock, patch
import sys

# Mock dependencies to avoid importing heavy modules or connecting to real LLMs
sys.modules['models'] = MagicMock()
sys.modules['memory'] = MagicMock()
sys.modules['tools'] = MagicMock()
sys.modules['charts'] = MagicMock()

# Import the code to test (assuming writer_graph.py is in same dir)
from writer_graph import human_review_node, continue_to_writers, WriterState

class TestReviewInteraction(unittest.TestCase):
    
    @patch('builtins.input', return_value="Add gaming section")
    def test_feedback_loop(self, mock_input):
        print("\nTesting Feedback Entry...")
        state = {"outline": [{"id":"1", "title":"Old", "description": "desc"}], "iteration": 0}
        
        # Test human_review_node returning feedback state
        result = human_review_node(state)
        
        self.assertEqual(result['user_feedback'], "Add gaming section")
        self.assertEqual(result['iteration'], 1)
        self.assertEqual(result['outline'], [])
        print("✅ node captured feedback correctly")
        
        # Test routing
        # If feedback exists + empty outline -> should go to skeleton_generator
        next_step = continue_to_writers(result)
        self.assertEqual(next_step, "skeleton_generator")
        print("✅ routing logic confirms return to planner")

    @patch('builtins.input', return_value="") # User hits enter
    def test_approval(self, mock_input):
        print("\nTesting Approval...")
        state = {"outline": [{"id":"1", "title":"Good", "description": "desc"}], "iteration": 0}
        
        result = human_review_node(state)
        
        self.assertEqual(result['user_feedback'], "")
        print("✅ node confirmed approval")
        
        # Test routing
        # Empty feedback + existing outline -> chart_scout
        # We need to simulate the state that 'continue_to_writers' sees.
        # human_review returns partial update. The graph merges it.
        # So full state would serve outline intact.
        full_state_simulated = {**state, **result} 
        next_step = continue_to_writers(full_state_simulated)
        self.assertEqual(next_step, "chart_scout")
        print("✅ routing logic confirms proceed to scout")

if __name__ == '__main__':
    unittest.main()
