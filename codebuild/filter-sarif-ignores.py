#!/usr/bin/env python3
"""Post-process SARIF output to apply .grype.yaml ignore rules.

Grype's ignore mechanism only suppresses findings from exit-code and
table/JSON output — SARIF still includes all CVEs. This script reads
the ignore list from .grype.yaml and removes matching results from the
SARIF JSON before upload.

Usage:
    python3 filter-sarif-ignores.py \
        --sarif /tmp/grype-foo.sarif \
        --config .grype.yaml \
        --output /tmp/grype-foo.sarif
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml


def load_ignore_cves(config_path: str) -> set[str]:
    """Extract the set of CVE IDs from .grype.yaml ignore list."""
    path = Path(config_path)
    if not path.exists():
        return set()

    with open(path) as f:
        config = yaml.safe_load(f)

    if not config or "ignore" not in config:
        return set()

    cves: set[str] = set()
    for entry in config["ignore"]:
        vuln = entry.get("vulnerability")
        if vuln:
            cves.add(vuln)
    return cves


def filter_sarif(sarif_path: str, ignore_cves: set[str]) -> dict:
    """Remove results matching ignored CVE IDs from SARIF data."""
    with open(sarif_path) as f:
        sarif = json.load(f)

    if not ignore_cves:
        return sarif

    for run in sarif.get("runs", []):
        original_results = run.get("results", [])
        run["results"] = [
            r for r in original_results if r.get("ruleId") not in ignore_cves
        ]

    return sarif


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Filter SARIF results using .grype.yaml ignore rules"
    )
    parser.add_argument("--sarif", required=True, help="Path to SARIF file")
    parser.add_argument("--config", required=True, help="Path to .grype.yaml")
    parser.add_argument(
        "--output", required=True, help="Output path for filtered SARIF"
    )
    args = parser.parse_args()

    if not Path(args.sarif).exists():
        print(f"ERROR: SARIF file not found: {args.sarif}", file=sys.stderr)
        return 1

    ignore_cves = load_ignore_cves(args.config)
    if not ignore_cves:
        print("WARN: No ignore rules found — SARIF unchanged", file=sys.stderr)
        # Still copy input to output for idempotency
        sarif = json.loads(Path(args.sarif).read_text())
    else:
        sarif = filter_sarif(args.sarif, ignore_cves)
        # Count how many results were removed (across all runs)
        original_count = 0
        filtered_count = 0
        with open(args.sarif) as f:
            original = json.load(f)
        for run in original.get("runs", []):
            original_count += len(run.get("results", []))
        for run in sarif.get("runs", []):
            filtered_count += len(run.get("results", []))
        removed = original_count - filtered_count
        print(
            f"Filtered SARIF: removed {removed} results "
            f"({original_count} -> {filtered_count}) "
            f"using {len(ignore_cves)} ignore rules"
        )

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w") as f:
        json.dump(sarif, f, indent=2)

    return 0


if __name__ == "__main__":
    sys.exit(main())
