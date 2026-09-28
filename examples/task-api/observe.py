#!/usr/bin/env python3
"""Collect bounded external SSE/snapshot evidence for an already accepted task."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time

from client import Client


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task_id")
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--seconds", type=int, default=120)
    parser.add_argument("--cursor")
    args = parser.parse_args()
    args.directory.mkdir(parents=True, exist_ok=False)
    client = Client(
        os.environ["ADP_TASK_API_URL"],
        os.environ.get("ADP_TASK_TOKEN"),
        token_url=os.environ.get("ADP_TASK_TOKEN_URL"),
        client_id=os.environ.get("ADP_TASK_CLIENT_ID"),
        client_secret=os.environ.get("ADP_TASK_CLIENT_SECRET"),
    )
    started = time.monotonic()
    with (args.directory / "events.ndjson").open("w") as stream:
        for event in client.events(args.task_id, args.cursor, seconds=args.seconds):
            record = {
                "received_at": datetime.now(timezone.utc).isoformat(),
                "elapsed_seconds": time.monotonic() - started,
                "event": event,
            }
            stream.write(json.dumps(record) + "\n")
            stream.flush()
    snapshot = client.snapshot(args.task_id)
    (args.directory / "snapshot.json").write_text(json.dumps(snapshot, indent=2) + "\n")
    (args.directory / "capture.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "lane": "live-capture",
                "task_id": args.task_id,
                "elapsed_seconds": time.monotonic() - started,
                "max_seconds": args.seconds,
                "new_tasks_submitted": 0,
                "criterion_outcome": "NOT RUN",
                "note": "Captured evidence requires independent criterion evaluation.",
            },
            indent=2,
        )
        + "\n"
    )


if __name__ == "__main__":
    main()
