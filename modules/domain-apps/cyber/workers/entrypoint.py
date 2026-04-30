"""Cyber worker entrypoint — dispatches based on WORKER_ROLE env var."""

import os
import sys

ROLE = os.environ.get("WORKER_ROLE", "").strip().lower()

if ROLE == "manifest":
    # Dump the build-time manifest and exit — useful for debugging version skew.
    manifest_path = "/opt/worker-manifest.json"
    try:
        with open(manifest_path) as f:
            print(f.read())
    except FileNotFoundError:
        print(f"ERROR: manifest not found at {manifest_path}", file=sys.stderr)
        sys.exit(1)
    sys.exit(0)
elif ROLE == "triage":
    from triage.handler import run
elif ROLE == "static":
    from static.handler import run
else:
    print(f"ERROR: WORKER_ROLE must be 'triage', 'static', or 'manifest', got '{ROLE}'", file=sys.stderr)
    sys.exit(2)

run()
