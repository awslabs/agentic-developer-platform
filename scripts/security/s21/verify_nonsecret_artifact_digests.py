"""Verify reviewed digest candidates against private originals without emitting values."""

import argparse
import hashlib
import json
import subprocess
from pathlib import Path


def verify(source, scan_path, audit_path, receipt_path):
    receipt = json.loads(receipt_path.read_text())
    revision = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=source, text=True
    ).strip()
    assert revision == receipt["source_revision"], "Wrong frozen source revision"
    scan = json.loads(scan_path.read_text())["results"]
    audit = json.loads(audit_path.read_text())["results"]
    audited = {}
    for group in audit:
        candidate = group["secrets"]
        candidate_sha1 = hashlib.sha1(candidate.encode()).hexdigest()
        for line in group["lines"]:
            audited[(group["filename"], int(line), candidate_sha1)] = candidate
    records = receipt["verified_records"]
    assert records, "Empty verification receipt"
    selectors = [record["selector"] for record in records]
    assert len(selectors) == len(set(selectors)), "Duplicate original selectors"
    assert len(records) == receipt["verified_delta"], "Receipt count mismatch"
    for record in records:
        assert record.get("matching_artifacts"), "Missing artifact evidence"
        assert record.get("context_evidence"), "Missing checksum context evidence"
    frozen = {}

    def frozen_bytes(path):
        if path not in frozen:
            # Read committed blobs, never mutable checkout files. Missing or
            # untracked evidence refuses verification rather than falling back.
            frozen[path] = subprocess.check_output(
                ["git", "show", f"{revision}:{path}"],
                cwd=source,
                stderr=subprocess.DEVNULL,
            )
        return frozen[path]

    documents, digests = {}, {}
    for record in receipt["verified_records"]:
        # Join private full candidate hashes, not prefix/path heuristics.
        originals = [
            r
            for r in scan[record["file"]]
            if r["line_number"] == record["line"]
            and r["type"] == record["detector"]
            and r["hashed_secret"].startswith(record["candidate_hash_prefix"])
        ]
        assert len(originals) == 1, "Ambiguous original scan selector"
        original = originals[0]
        candidate = audited[(record["file"], record["line"], original["hashed_secret"])]
        assert hashlib.sha1(candidate.encode()).hexdigest() == original["hashed_secret"]
        lines = frozen_bytes(record["file"]).decode().splitlines()
        assert candidate in lines[record["line"] - 1], (
            "Candidate missing at exact frozen line"
        )
        for artifact in record["matching_artifacts"]:
            if artifact not in digests:
                digests[artifact] = hashlib.sha256(frozen_bytes(artifact)).hexdigest()
            assert candidate == digests[artifact], (
                "Candidate is not the artifact SHA256"
            )
        if record["file"] not in documents:
            documents[record["file"]] = json.loads(frozen_bytes(record["file"]))
        for evidence in record["context_evidence"]:
            path = evidence["json_path"]
            assert path, "Empty JSON context path"
            value = documents[record["file"]]
            for key in path:
                value = value[key]
            assert value == candidate, "JSON context does not identify candidate"
            explicit = any(
                isinstance(k, str)
                and ("sha256" in k.lower() or k.lower() == "file_hashes")
                for k in path
            )
            linked = isinstance(path[-1], str) and any(
                artifact == path[-1] or artifact.endswith("/" + path[-1])
                for artifact in record["matching_artifacts"]
            )
            assert explicit or linked, "No decisive checksum context"
    print(
        f"Verified {len(receipt['verified_records'])} original selectors; no candidate values emitted"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--scan", required=True, type=Path)
    parser.add_argument("--audit", required=True, type=Path)
    parser.add_argument("--receipt", required=True, type=Path)
    args = parser.parse_args()
    verify(args.source, args.scan, args.audit, args.receipt)
