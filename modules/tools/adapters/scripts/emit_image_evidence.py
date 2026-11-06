"""Retain bounded image evidence in the existing nonpublishing build log."""

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

PREFIX = "ADP_ADAPTER_IMAGE_EVIDENCE_V1 "
FILES = ("image-smoke.json", "source-files.json", "sbom.cdx.json")
MAX_FILE_BYTES = 96 * 1024
MAX_RECORD_BYTES = 128 * 1024


def emit(*, source_revision: str, evidence_dir: Path, output) -> None:
    if re.fullmatch(r"[0-9a-f]{40}", source_revision) is None:
        raise ValueError("Immutable source revision required")
    values = {}
    records = []
    for name in FILES:
        with (evidence_dir / name).open("rb") as stream:
            raw = stream.read(MAX_FILE_BYTES + 1)
        if len(raw) > MAX_FILE_BYTES:
            raise ValueError("Image evidence exceeds bound")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("Invalid image evidence")
        values[name] = value
    report = values["image-smoke.json"]
    if report.get("source_revision") != source_revision:
        raise ValueError("Image evidence source mismatch")
    source_hashes = values["source-files.json"]
    encoded_hashes = json.dumps(source_hashes, sort_keys=True).encode()
    if report.get("source_files_sha256") != hashlib.sha256(encoded_hashes).hexdigest():
        raise ValueError("Image evidence source inventory mismatch")
    if values["sbom.cdx.json"].get("bomFormat") != "CycloneDX":
        raise ValueError("Image evidence inventory missing")
    for name in FILES:
        data = values[name]
        content = json.dumps(data, sort_keys=True, separators=(",", ":"), allow_nan=False)
        record = {
            "source_revision": source_revision,
            "file": name,
            "sha256": hashlib.sha256(content.encode()).hexdigest(),
            "data": data,
        }
        # One bounded JSON record per file, below the CloudWatch event limit.
        encoded = PREFIX + json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
        if len(encoded.encode()) > MAX_RECORD_BYTES:
            raise ValueError("Image evidence log record exceeds bound")
        records.append(encoded)
    for encoded in records:
        output.write(encoded)
    if report.get("passed") is not True or report.get("cleanup_confirmed") is not True:
        raise ValueError("Image qualification or cleanup failed")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        emit(source_revision=args.source_revision, evidence_dir=args.evidence_dir, output=sys.stdout)
    except (OSError, ValueError, TypeError):
        # Never dump file bodies or build environment when qualification fails.
        print("adapter_image_evidence_incomplete", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
