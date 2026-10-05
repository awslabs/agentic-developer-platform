#!/usr/bin/env python3
"""Inventory legacy chat ownership; opt-in conditional artifact backfill.

The stdout summary is unchanged. ``--report`` additionally writes one JSON line per
quarantined or conflicting record ({"table", "key", "reason", "category"}) so the
quarantine is durable and reviewable; it never contains record contents.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.orchestration.chat_data_migration import inventory


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context-table", required=True)
    parser.add_argument("--artifacts-table", required=True)
    parser.add_argument("--memory-table", required=True)
    parser.add_argument("--apply", action="store_true", help="Conditionally annotate corroborated artifact catalog rows")
    parser.add_argument("--report", type=Path, default=None, help="Write quarantined/conflicting record keys and reasons as JSON lines")
    parser.add_argument("--page-size", type=int, default=None, help="Scan page size (rows per DynamoDB request); default lets DynamoDB choose")
    args = parser.parse_args()
    if args.page_size is not None and args.page_size < 1:
        parser.error("--page-size must be a positive integer")

    import boto3

    dynamodb = boto3.resource("dynamodb")
    tables = (dynamodb.Table(args.context_table), dynamodb.Table(args.artifacts_table), dynamodb.Table(args.memory_table))
    if args.report is None:
        counts = inventory(*tables, apply=args.apply, page_size=args.page_size)
    else:
        with args.report.open("w", encoding="utf-8") as handle:

            def record(entry: dict) -> None:
                handle.write(json.dumps(entry, sort_keys=True) + "\n")

            counts = inventory(*tables, apply=args.apply, report=record, page_size=args.page_size)
    print(json.dumps({"mode": "apply" if args.apply else "dry_run", "counts": counts}, sort_keys=True))


if __name__ == "__main__":
    main()
