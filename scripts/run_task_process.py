from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from gateway.executor import run_research_job_sync


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one research task in a dedicated process.")
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--query", required=True)
    parser.add_argument("--mode", default="medium")
    parser.add_argument("--backend", default="process")
    parser.add_argument("--disable-cache", action="store_true")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()

    result = run_research_job_sync(
        args.task_id,
        args.query,
        backend=args.backend,
        research_mode=args.mode,
        disable_cache=args.disable_cache,
        resume_from_checkpoint=args.resume,
    )
    sys.stdout.write(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
