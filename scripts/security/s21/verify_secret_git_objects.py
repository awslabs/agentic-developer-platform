"""Verify reviewed source-revision findings offline, without displaying values."""

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path


def require(condition, message):
    if not condition:
        raise ValueError(message)


def git(source, *args):
    result = subprocess.run(
        ["git", *args], cwd=source, capture_output=True, check=False
    )
    require(result.returncode == 0, "Required immutable Git object unavailable")
    return result.stdout


def verify_context(source_bytes, record, candidate):
    """Bind a complete value to an explicit source-revision field and line."""
    path = record["context_path"]
    require(bool(path), "Missing source-revision context")
    if record["file"].endswith(".json"):
        document = json.loads(source_bytes)
        require(
            path[-1] in {"source_sha", "preserved_worker_source", "release_source"},
            "Unsupported source-revision field",
        )
        expected_line = re.compile(
            r'\s*"'
            + re.escape(path[-1])
            + r'"\s*:\s*"'
            + re.escape(candidate)
            + r'"\s*,?\s*'
        )
    else:
        import yaml

        require(
            record["file"].endswith("superplane.lock.yaml"), "Unsupported YAML source"
        )
        require(
            path == ["image_sources", "superplane-executor", "origin", "revision"],
            "Unsupported lockfile revision context",
        )
        document = yaml.safe_load(source_bytes)
        expected_line = re.compile(r"\s*revision:\s*" + re.escape(candidate) + r"\s*")
    value = document
    for key in path:
        value = value[key]
    require(value == candidate, "Context does not identify complete candidate")
    lines = source_bytes.decode().splitlines()
    require(0 < record["line"] <= len(lines), "Original source line unavailable")
    require(
        expected_line.fullmatch(lines[record["line"] - 1]) is not None,
        "Original line is not the complete revision field",
    )


def verify(source, scan_path, audit_path, receipt_path):
    receipt = json.loads(receipt_path.read_text())
    records = receipt["verified_records"]
    require(
        records and len(records) == receipt["verified_delta"], "Receipt count mismatch"
    )
    require(len({r["selector"] for r in records}) == len(records), "Duplicate selector")
    revision = receipt["source_revision"]
    require(re.fullmatch(r"[0-9a-f]{40}", revision), "Invalid frozen revision")
    scan = json.loads(scan_path.read_text())["results"]
    audit = json.loads(audit_path.read_text())["results"]
    candidates = {}
    for group in audit:
        candidate = group["secrets"]
        digest = hashlib.sha1(candidate.encode()).hexdigest()
        for line in group["lines"]:
            candidates[group["filename"], int(line), digest] = candidate
    blobs = {}
    for record in records:
        prefix, index = record["selector"].rsplit("|ri=", 1)
        require(prefix == "detect-secrets|" + record["file"], "Selector path mismatch")
        require(index.isdecimal(), "Invalid original record index")
        original = scan[record["file"]][int(index)]
        require(
            original["line_number"] == record["line"]
            and original["type"] == record["detector"]
            and original["hashed_secret"].startswith(record["candidate_hash_prefix"])
            and len(record["candidate_hash_prefix"]) == 16,
            "Exact original scan identity mismatch",
        )
        candidate = candidates[
            record["file"], record["line"], original["hashed_secret"]
        ]
        require(re.fullmatch(r"[0-9a-f]{40}", candidate), "Not a complete Git SHA1")
        require(record["git_object_type"] == "commit", "Only source commits reviewed")
        body = git(source, "cat-file", "commit", candidate)
        actual = hashlib.sha1(
            b"commit " + str(len(body)).encode() + b"\0" + body
        ).hexdigest()
        require(
            actual == candidate, "Git commit bytes do not reproduce object identifier"
        )
        if record["file"] not in blobs:
            blobs[record["file"]] = git(source, "show", revision + ":" + record["file"])
        verify_context(blobs[record["file"]], record, candidate)
    print(
        f"Verified {len(records)} original source-commit selectors; no values emitted"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("source", "scan", "audit", "receipt"):
        parser.add_argument("--" + name, required=True, type=Path)
    args = parser.parse_args()
    try:
        verify(args.source, args.scan, args.audit, args.receipt)
    except Exception:  # noqa: BLE001 - redact every private-input failure
        # Private input exceptions can include candidate values or source lines.
        parser.exit(1, "Verification failed; private inputs and values withheld\n")
