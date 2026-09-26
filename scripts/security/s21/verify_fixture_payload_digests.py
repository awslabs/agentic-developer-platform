"""Recompute fixture content digests using immutable bytes and private originals."""

import argparse
import base64
import hashlib
import json
import re
from pathlib import Path

from verify_secret_git_objects import git, require

PREFIX = "docs/task-api/contracts/v1/fixtures/"
# Each invalid fixture is bound to the exact field it deliberately changes.
VARIANTS = {
    "artifact-read-response-invalid-byte_length.json": (
        "artifact-read-response.json",
        "byte_length",
    ),
    "artifact-read-response-invalid-content_base64.json": (
        "artifact-read-response.json",
        "content_base64",
    ),
    "artifact-read-response-invalid-url.json": ("artifact-read-response.json", "url"),
    "process-artifact-chunk-credential.json": (
        "process-artifact-chunk-frame.json",
        "run_credential",
    ),
    "process-artifact-chunk-invalid-base64.json": (
        "process-artifact-chunk-frame.json",
        "data_base64",
    ),
    "process-artifact-chunk-oversize.json": (
        "process-artifact-chunk-frame.json",
        "total_bytes",
    ),
    "process-artifact-chunk-sequence-zero.json": (
        "process-artifact-chunk-frame.json",
        "sequence",
    ),
}
READ = "artifact-read-response.json"
CHUNK = "process-artifact-chunk-frame.json"
START = "process-start-artifact-reference-frame.json"


def derive(record, document):
    source = document(record["file"])
    input_file = record["input_file"]
    require(
        input_file in {PREFIX + "valid/" + READ, PREFIX + "valid/" + CHUNK},
        "Unsupported canonical fixture",
    )
    canonical = document(input_file)
    name = input_file.rsplit("/", 1)[1]
    payload_field, length_field, schema = (
        (
            "content_base64",
            "byte_length",
            "internal-adapters.schema.json#/$defs/artifact_response",
        )
        if name == READ
        else (
            "data_base64",
            "total_bytes",
            "process-protocol.schema.json#/$defs/artifact_chunk_frame",
        )
    )
    require(
        canonical["$fixture"]["expect"] == "valid"
        and canonical["$fixture"]["schema"] == schema,
        "Canonical fixture contract mismatch",
    )
    content = base64.b64decode(canonical[payload_field], validate=True)
    require(
        content and len(content) == canonical[length_field],
        "Canonical payload length mismatch",
    )
    digest = hashlib.sha256(content).hexdigest()
    require(canonical["content_sha256"] == digest, "Canonical digest mismatch")
    if record["file"] == PREFIX + "valid/" + START:
        require(name == CHUNK, "Artifact reference uses wrong canonical family")
        require(
            source["$fixture"]["expect"] == "valid"
            and source["$fixture"]["schema"]
            == "process-protocol.schema.json#/$defs/start_frame",
            "Artifact reference contract mismatch",
        )
        require(
            record["output_path"] == ["artifacts", 0, "content_sha256"],
            "Artifact reference path mismatch",
        )
        require(len(source["artifacts"]) == 1, "Ambiguous artifact reference")
        target = source["artifacts"][0]
        require(
            target
            == {
                "artifact_id": canonical["artifact_id"],
                "content_type": canonical["content_type"],
                "content_sha256": digest,
                "byte_length": len(content),
            },
            "Artifact reference identity mismatch",
        )
    else:
        require(record["output_path"] == ["content_sha256"], "Wrong checksum field")
        if record["file"] != input_file:
            filename = record["file"].rsplit("/", 1)[1]
            require(
                record["file"] == PREFIX + "invalid/" + filename
                and filename in VARIANTS,
                "Unsupported fixture variant",
            )
            expected_name, mutation = VARIANTS[filename]
            require(expected_name == name, "Variant uses wrong canonical family")
            require(
                source["$fixture"]["expect"] == "invalid"
                and source["$fixture"]["schema"] == schema,
                "Variant contract mismatch",
            )
            changed = {
                k
                for k in set(source) | set(canonical)
                if source.get(k) != canonical.get(k)
            }
            require(changed == {"$fixture", mutation}, "Unexpected fixture mutation")
        require(source["content_sha256"] == digest, "Fixture digest mismatch")
    return digest


def verify(source, scan_path, audit_path, receipt_path):
    receipt = json.loads(receipt_path.read_text())
    records = receipt["verified_records"]
    require(
        records and len(records) == receipt["verified_delta"], "Receipt count mismatch"
    )
    require(
        len({r["selector"] for r in records}) == len(records),
        "Duplicate original selector",
    )
    revision = receipt["source_revision"]
    require(re.fullmatch(r"[0-9a-f]{40}", revision), "Invalid frozen revision")
    scan = json.loads(scan_path.read_text())["results"]
    audit = json.loads(audit_path.read_text())["results"]
    candidates = {}
    for group in audit:
        digest = hashlib.sha1(group["secrets"].encode()).hexdigest()
        for line in group["lines"]:
            candidates[group["filename"], int(line), digest] = group["secrets"]
    blobs = {}

    def blob(path):
        if path not in blobs:
            blobs[path] = git(source, "show", revision + ":" + path)
        return blobs[path]

    def document(path):
        return json.loads(blob(path))

    for record in records:
        prefix, index = record["selector"].rsplit("|ri=", 1)
        require(
            prefix == "detect-secrets|" + record["file"] and index.isdecimal(),
            "Original selector mismatch",
        )
        original = scan[record["file"]][int(index)]
        require(
            original["line_number"] == record["line"]
            and original["type"] == record["detector"]
            and len(record["candidate_hash_prefix"]) == 16
            and original["hashed_secret"].startswith(record["candidate_hash_prefix"]),
            "Original scan identity mismatch",
        )
        candidate = candidates[
            record["file"], record["line"], original["hashed_secret"]
        ]
        require(
            candidate == derive(record, document),
            "Candidate differs from payload digest",
        )
        line = blob(record["file"]).decode().splitlines()[record["line"] - 1]
        require(
            re.fullmatch(
                r'\s*"content_sha256"\s*:\s*"' + re.escape(candidate) + r'"\s*,?\s*',
                line,
            ),
            "Exact source-line field mismatch",
        )
    print(
        f"Verified {len(records)} original fixture payload digests; no values emitted"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("source", "scan", "audit", "receipt"):
        parser.add_argument("--" + name, required=True, type=Path)
    args = parser.parse_args()
    try:
        verify(args.source, args.scan, args.audit, args.receipt)
    except Exception:  # noqa: BLE001 - redact every private-input failure
        parser.exit(1, "Verification failed; private inputs and values withheld\n")
