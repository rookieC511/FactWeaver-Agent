"""
Debug Script for GAIA Task Execution
====================================
Runs a single GAIA task with verbose logging to file, trapping 500 errors.
Goal: Determine if failure is in Agent (SiliconFlow) or Judge (Ollama).
"""
import sys
import os
import traceback
import time
import logging

# Configure logging
LOG_FILE = "d:/Projects/deepresearch-agent/debug_gaia.log"
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout)
    ]
)
log = logging.getLogger(__name__)

# Fix Windows encoding
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Add project root
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, PROJECT_ROOT)

from tests.adapter import invoke_agent

def debug_gaia_task():
    task_id = "c61d22de-5f6c-4958-a7f6-5e9707bd3466"
    question = (
        "A paper about AI regulation that was originally submitted to arXiv.org in June 2022 "
        "shows a figure with three axes... Which of these words is used to describe a type of "
        "society in a Physics and Society article submitted to arXiv.org on August 11, 2016?"
    )
    
    log.info(f"🚀 Starting Debug Run for Task {task_id}")
    log.info(f"❓ Question: {question}")
    
    start_time = time.time()
    try:
        log.info("▶️ Invoking Agent...")
        result = invoke_agent(question)
        
        duration = time.time() - start_time
        log.info(f"✅ Agent finished in {duration:.2f}s")
        log.info(f"📄 Report Length: {len(result.get('actual_output', ''))} chars")
        log.info(f"🔗 Citations: {len(result.get('citations', []))}")
        
    except Exception as e:
        duration = time.time() - start_time
        log.error(f"❌ FAILED after {duration:.2f}s")
        log.error(f"Error Type: {type(e).__name__}")
        log.error(f"Error Message: {e}")
        log.error(traceback.format_exc())
        
        # Check for 500 specific details
        if "500" in str(e):
            log.critical("🚨 CAUGHT 500 ERROR! Likely API or Server issue.")

if __name__ == "__main__":
    debug_gaia_task()
