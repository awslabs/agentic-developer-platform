"""Validate the owner record without treating missing output as absence proof."""

import re


def validate_boundary(value):
    boundary = value.get("preparation_boundary", {})
    entries = boundary.get("entries", {})
    backend = boundary.get("backend_observation", {})
    if (
        value.get("apply_started") is not False
        or "attempt_extension" in value
        or boundary.get("kind") != "interrupted-read-only-plan-v1"
        or boundary.get("read_only_recipe") is not True
        or not isinstance(entries, dict)
        or entries.get("review") != {"kind": "directory"}
        or any(name.startswith("review/") for name in entries)
        or backend.get("versions_absent") is not True
        or backend.get("delete_markers_absent") is not True
        or backend.get("checksum_absent") is not True
        or backend.get("versioning") != "Enabled"
        or not re.fullmatch(r"[a-f0-9]{64}", boundary.get("sha256", ""))
        or not boundary.get("qualified_source")
    ):
        raise ValueError("interrupted read-only plan evidence is incomplete")
    lock = backend.get("plan_lock")
    if lock is not None and (
        lock.get("Operation") != "OperationTypePlan"
        or lock.get("Version") != "1.9.8"
        or lock.get("Who") != "@" + value["source_pod_name"]
        or not lock.get("ID")
        or lock.get("Path") != backend.get("bucket", "") + "/" + backend.get("key", "")
        or not backend.get("plan_lock_info")
    ):
        raise ValueError("retained lock is not the original worker's plan lock")
