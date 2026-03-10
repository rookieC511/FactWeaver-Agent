import argparse
import json

from gateway.celery_app import CELERY_AVAILABLE
from gateway.state_store import list_dlq


def main():
    parser = argparse.ArgumentParser(description="Inspect FactWeaver DLQ records.")
    parser.add_argument("--limit", type=int, default=20, help="Number of DLQ items to print")
    args = parser.parse_args()

    records = list_dlq(limit=args.limit)
    print(json.dumps(records, ensure_ascii=False, indent=2))
    if not CELERY_AVAILABLE:
        print("\n[info] Celery is not available in this environment; replay is disabled.")


if __name__ == "__main__":
    main()
