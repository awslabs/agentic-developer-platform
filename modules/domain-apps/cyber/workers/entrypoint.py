"""Cyber worker entrypoint — dispatches based on WORKER_ROLE env var."""

import os
import sys

ROLE = os.environ.get("WORKER_ROLE", "").strip().lower()

if ROLE == "triage":
    from triage.handler import run
elif ROLE == "static":
    from static.handler import run
else:
    print(f"ERROR: WORKER_ROLE must be 'triage' or 'static', got '{ROLE}'", file=sys.stderr)
    sys.exit(2)

run()
