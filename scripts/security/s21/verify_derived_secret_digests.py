"""Recompute nonsecret derived digests from immutable source and private originals."""

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

from verify_nonsecret_artifact_digests import verify_context


def at(document, path):
    for key in path:
        document = document[key]
    return document


def derive(record, load_document):
    source = load_document(record["file"])
    inputs = load_document(record["input_file"])
    kind = record["kind"]
    if kind == "record_key_projection":
        path = record["summary_path"]
        summary = at(source, path)
        records = inputs["dispositions"]
        if len(path) == 2 and path[0] == "existing_owner_mappings":
            selected = [r for r in records if r["owner"] == summary["owner"]]
        elif len(path) == 2 and path[0] == "unowned_followons":
            selected = [r for r in records if r["owner"] == summary["followon"]]
        elif path == ["open_record_partition"]:
            selected = [
                r
                for r in records
                if r["verdict"] in {"needs-followon", "routed-existing-owner"}
            ]
        else:
            raise AssertionError("Unsupported record-key projection")
        assert selected and len(selected) == summary["record_count"], (
            "Record projection count mismatch"
        )
        keys = sorted((r["tool"], r["artifact"], r["result_index"]) for r in selected)
        assert len(keys) == len(set(keys)), "Duplicate record keys"
        payload = "".join(
            json.dumps(key, separators=(",", ":")) + "\n" for key in keys
        ).encode()
        digest = hashlib.sha256(payload).hexdigest()
        assert summary["record_keys_sha256"] == digest, "Projection checksum mismatch"
        return digest
    if kind == "pricing_source_manifest":
        assert record["input_path"] == ["provenance", "claude_audit", "sources"]
        sources = at(inputs, record["input_path"])
        manifest = {
            "pricing_page_sha256": sources["pricing_page"]["sha256"],
            "token_map_sha256": sources["token_map"]["sha256"],
        }
        digest = hashlib.sha256(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        assert inputs["provenance"]["claude_audit"]["combined_source_sha256"] == digest
        return digest
    if kind == "embedded_artifact_text":
        assert record["input_file"] == record["file"]
        assert record["input_paths"], "Missing embedded artifact input"
        digests = set()
        for path in record["input_paths"]:
            assert len(path) == 2 and path[0] == "artifact_text"
            text = at(inputs, path)
            assert isinstance(text, str)
            digest = hashlib.sha256(text.encode()).hexdigest()
            declarations = [
                a for a in source["receipt"]["artifacts"] if a["path"] == path[1]
            ]
            assert len(declarations) == 1 and declarations[0]["sha256"] == digest
            digests.add(digest)
        assert len(digests) == 1, "Inputs do not identify one candidate digest"
        return digests.pop()
    raise AssertionError("Unsupported derived checksum kind")


def verify(source, scan_path, audit_path, receipt_path):
    receipt = json.loads(receipt_path.read_text())
    records = receipt["verified_records"]
    assert records and len(records) == receipt["verified_delta"]
    assert len({r["selector"] for r in records}) == len(records)
    scan = json.loads(scan_path.read_text())["results"]
    audit = json.loads(audit_path.read_text())["results"]
    candidates = {}
    for group in audit:
        candidate_hash = hashlib.sha1(group["secrets"].encode()).hexdigest()
        for line in group["lines"]:
            candidates[group["filename"], int(line), candidate_hash] = group["secrets"]
    blobs = {}

    def blob(path):
        if path not in blobs:
            blobs[path] = subprocess.check_output(
                ["git", "show", f"{receipt['source_revision']}:{path}"],
                cwd=source,
                stderr=subprocess.DEVNULL,
            )
        return blobs[path]

    def document(path):
        return json.loads(blob(path)) if path.endswith(".json") else None

    for record in records:
        assert record.get("context_evidence"), "Missing checksum context"
        originals = [
            r
            for r in scan[record["file"]]
            if r["line_number"] == record["line"]
            and r["type"] == record["detector"]
            and r["hashed_secret"].startswith(record["candidate_hash_prefix"])
        ]
        assert len(originals) == 1, "Ambiguous original selector"
        candidate = candidates[
            record["file"], record["line"], originals[0]["hashed_secret"]
        ]
        assert (
            hashlib.sha1(candidate.encode()).hexdigest()
            == originals[0]["hashed_secret"]
        )
        assert (
            candidate in blob(record["file"]).decode().splitlines()[record["line"] - 1]
        )
        assert candidate == derive(record, document), (
            "Candidate does not equal recomputed checksum"
        )
        verify_context(
            dict(record, matching_artifacts=[]), candidate, blob(record["file"])
        )
    print(
        f"Verified {len(records)} derived-digest original selectors; no values emitted"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("source", "scan", "audit", "receipt"):
        parser.add_argument("--" + name, required=True, type=Path)
    args = parser.parse_args()
    verify(args.source, args.scan, args.audit, args.receipt)
