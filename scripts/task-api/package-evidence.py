#!/usr/bin/env python3
"""Hash bounded evidence without treating missing/not-run criteria as passing."""

import argparse
import hashlib
import json
from pathlib import Path


def package(manifest, root):
    if manifest.get("schema_version") != "1.0":
        raise ValueError("Expected contract version1.0")
    for field in (
        "environment",
        "account_id",
        "source_sha",
        "image_digest",
        "evaluator",
        "started_at",
        "finished_at",
        "bounds",
        "owned_resources",
        "cleanup",
    ):
        if not manifest.get(field):
            raise ValueError("Missing evidence field: " + field)
    if (
        not 0 < manifest["bounds"].get("max_tasks", 0) <= 3
        or not 0 < manifest["bounds"].get("max_total_usd", 0) <= 3
    ):
        raise ValueError("Maximum3 tasks and USD3 must be explicitly bounded")
    results = manifest.get("results", [])
    if not results:
        raise ValueError("Empty criterion selection cannot pass")
    for result in results:
        if result.get("outcome") not in (
            "PASS",
            "FAIL",
            "NOT RUN",
            "BLOCKED",
        ) or not result.get("criterion_id"):
            raise ValueError("Each criterion requires a valid outcome and ID")
        if result["outcome"] == "PASS" and (
            result.get("lane") != "live"
            or result.get("executed", 0) <= 0
            or result.get("exit_status") != 0
            or not result.get("command")
            or not result.get("artifacts")
        ):
            raise ValueError(
                "Live PASS requires executed checks, command, successful exit and artifacts"
            )
        hashed = {}
        for relative in result.get("artifacts", []):
            path = (root / relative).resolve()
            if (
                not path.is_relative_to(root.resolve())
                or not path.is_file()
                or path.stat().st_size > 16 * 1024 * 1024
            ):
                raise ValueError("Evidence file missing, outside root or over16MiB")
            hashed[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        result["artifact_sha256"] = hashed
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = package(json.loads(args.manifest.read_text()), args.manifest.parent)
    args.output.write_text(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    main()
