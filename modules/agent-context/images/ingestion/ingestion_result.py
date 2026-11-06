"""Bounded child-to-worker result protocol; process exit alone proves no stages."""

from __future__ import annotations

import json
import os
from pathlib import Path

RESULT_ENV = "ADP_INGESTION_RESULT_FILE"
MAX_RESULT_BYTES = 64 * 1024
STATUSES = {"complete", "partial", "failed"}
STAGE_STATUSES = {"verified", "failed", "skipped"}


def stage_status(value) -> str:
    if value in ("ok", "complete", "uploaded", "written", "verified"):
        return "verified"
    if value in ("skipped", "skipped_verified", "disabled", "no_files", "no_chunks",
                 "no_bucket", "bucket_not_configured", "bucket_missing", "no_source",
                 "structural_stage_unavailable"):
        return "skipped"
    return "failed"


def summarize(steps: dict[str, str], *, status: str | None = None, run_id=None) -> dict:
    """Failures cannot be upgraded by an optimistic producer-level status."""
    values = set(steps.values())
    derived = ("partial" if "verified" in values else "failed") if "failed" in values else "complete"
    if not steps:
        derived = "failed"
    if status in {"failed", "refused"}:
        derived = "failed"
    elif status == "partial" and derived == "complete":
        derived = "partial"
    result = {"version": 1, "status": derived, "steps": steps}
    if run_id:
        result["run_id"] = run_id
    return result


def publish(result: dict) -> None:
    """Standalone scripts still print normally; workers use a private result file."""
    path = os.environ.get(RESULT_ENV)
    if path:
        encoded = json.dumps(result)
        if len(encoded.encode()) > MAX_RESULT_BYTES:
            raise ValueError("ingestion result too large")
        Path(path).write_text(encoded)


def read(path: str) -> dict:
    with open(path, "rb") as stream:
        data = stream.read(MAX_RESULT_BYTES + 1)
    if len(data) > MAX_RESULT_BYTES:
        raise ValueError("ingestion result too large")
    result = json.loads(data)
    if (
        not isinstance(result, dict)
        or result.get("version") != 1
        or result.get("status") not in STATUSES
        or not isinstance(result.get("steps"), dict)
        or not result["steps"]
        or any(not isinstance(k, str) or not isinstance(v, str) or v not in STAGE_STATUSES
               for k, v in result["steps"].items())
    ):
        raise ValueError("invalid ingestion result")
    # Independently reject contradictory success claims from producers.
    if summarize(result["steps"], status=result["status"])["status"] != result["status"]:
        raise ValueError("contradictory ingestion result")
    return result
