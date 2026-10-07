#!/usr/bin/env python3
"""Inventory SARIF results without collapsing repeated package occurrences.

Write output to a private path when inspecting private scanner reports. The
report hash and run/result indexes bind each entry to its original SARIF.
"""

import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path

from diff_security_findings import (
    _has_accepted_suppression,
    _sarif_rule_index,
    resolve_sarif_severity,
)
from reconcile_security_scan import load_json, validate_coverage


SOURCE_SHA = re.compile(r"[0-9a-f]{40}\Z")
IMAGE_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
FILE_SHA = re.compile(r"[0-9a-f]{64}\Z")
COMPANIONS = {
    "raw_artifact_sha256": ".raw.sarif",
    "suppression_summary_sha256": ".suppression-summary.json",
    "scanner_metadata_sha256": ".scanner-metadata.json",
}


def verify_scan_provenance(report_path: Path, coverage_path: Path, provenance_path: Path,
                           source_revision: str, target: str, report_sha256: str) -> dict:
    if not SOURCE_SHA.fullmatch(source_revision) or report_path.name != f"{target}.sarif":
        raise ValueError("invalid source revision or target report")
    coverage = load_json(coverage_path)
    targets = coverage.get("targets") if isinstance(coverage, dict) else None
    if not isinstance(targets, list) or any(not isinstance(item, dict) for item in targets):
        raise ValueError("invalid scan coverage targets")
    names = [item.get("name") for item in targets]
    if any(not isinstance(name, str) for name in names) or len(names) != len(set(names)):
        raise ValueError("duplicate or invalid scan coverage target")
    by_name = validate_coverage(coverage, "grype", source_revision, set(names))
    if target not in by_name:
        raise ValueError("assigned target absent from scan coverage")
    entry = by_name[target]
    image_digest = entry.get("digest")
    if not isinstance(image_digest, str) or not IMAGE_DIGEST.fullmatch(image_digest):
        raise ValueError("assigned image has no immutable digest")
    build_args = entry.get("build_args")
    if not isinstance(build_args, dict) or any(
        not isinstance(key, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", key)
        or not isinstance(value, str)
        or not re.fullmatch(r"[a-zA-Z0-9./:_-]+@sha256:[a-f0-9]{64}", value)
        for key, value in build_args.items()
    ):
        raise ValueError("invalid immutable scan build inputs")
    expected = {
        "artifact_sha256": report_sha256,
        "digest": image_digest,
        "name": target,
        "source_revision": source_revision,
        "tool": "grype",
        "build_args": build_args,
    }
    if entry.get("artifact_sha256") != report_sha256:
        raise ValueError("coverage does not bind the assigned SARIF bytes")
    for field, suffix in COMPANIONS.items():
        checksum = entry.get(field)
        companion = report_path.with_name(f"{target}{suffix}")
        if not isinstance(checksum, str) or not FILE_SHA.fullmatch(checksum) or not companion.is_file():
            raise ValueError(f"missing {field} companion")
        if hashlib.sha256(companion.read_bytes()).hexdigest() != checksum:
            raise ValueError(f"{field} companion hash mismatch")
        expected[field] = checksum
    provenance = load_json(provenance_path)
    if provenance != expected:
        raise ValueError("scan provenance does not match coverage and report")
    return {
        "coverage_sha256": hashlib.sha256(coverage_path.read_bytes()).hexdigest(),
        "provenance_sha256": hashlib.sha256(provenance_path.read_bytes()).hexdigest(),
        "source_revision": source_revision,
        "image_digest": image_digest,
        "build_args": build_args,
        "companion_sha256": {field: expected[field] for field in COMPANIONS},
    }


def inventory(report: dict, report_sha256: str) -> dict:
    if not isinstance(report, dict) or report.get("version") != "2.1.0":
        raise ValueError("expected a SARIF 2.1.0 report")
    runs = report.get("runs")
    if not isinstance(runs, list) or not runs:
        raise ValueError("SARIF report has no runs")

    occurrences = []
    for run_index, run in enumerate(runs):
        if not isinstance(run, dict) or not isinstance(run.get("results"), list):
            raise ValueError(f"run {run_index} has no results list")
        rules = _sarif_rule_index(run)
        for result_index, result in enumerate(run["results"]):
            if not isinstance(result, dict):
                raise ValueError(f"result {run_index}:{result_index} is not an object")
            rule_id = result.get("ruleId")
            if not rule_id or rule_id not in rules:
                raise ValueError(f"result {run_index}:{result_index} has no matching rule")
            locations = result.get("locations", [])
            if not isinstance(locations, list):
                raise ValueError(f"result {run_index}:{result_index} has invalid locations")
            paths = []
            for location in locations:
                if not isinstance(location, dict):
                    raise ValueError(f"result {run_index}:{result_index} has invalid location")
                path = (
                    location.get("physicalLocation", {})
                    .get("artifactLocation", {})
                    .get("uri")
                )
                paths.append(path)
            severity, source = resolve_sarif_severity(result, rules)
            rule = rules[rule_id]
            occurrences.append(
                {
                    "id": f"{report_sha256}:{run_index}:{result_index}",
                    "run_index": run_index,
                    "result_index": result_index,
                    "advisory": rule_id,
                    "severity": severity,
                    "severity_source": source,
                    "accepted_suppression": _has_accepted_suppression(result),
                    "paths": paths,
                    "rule_help": rule.get("help", {}).get("text", ""),
                    "rule_properties": rule.get("properties", {}),
                    "message": result.get("message", {}).get("text", ""),
                    "result_properties": result.get("properties", {}),
                }
            )

    active = Counter(
        entry["severity"]
        for entry in occurrences
        if not entry["accepted_suppression"]
    )
    return {
        "report_sha256": report_sha256,
        "active_counts": dict(sorted(active.items())),
        "accepted_suppression_count": sum(
            entry["accepted_suppression"] for entry in occurrences
        ),
        "occurrences": occurrences,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--expected-critical", type=int, required=True)
    parser.add_argument("--expected-high", type=int, required=True)
    parser.add_argument("--coverage", type=Path)
    parser.add_argument("--provenance", type=Path)
    parser.add_argument("--source-revision")
    parser.add_argument("--target")
    args = parser.parse_args()
    scan_inputs = (args.coverage, args.provenance, args.source_revision, args.target)
    if any(value is not None for value in scan_inputs) and not all(value is not None for value in scan_inputs):
        parser.error("coverage, provenance, source revision and target must be provided together")

    raw = args.report.read_bytes()
    report_sha256 = hashlib.sha256(raw).hexdigest()
    if report_sha256 != args.expected_sha256:
        parser.error("SARIF SHA-256 does not match the assigned report")
    result = inventory(json.loads(raw), report_sha256)
    for severity, expected in (
        ("critical", args.expected_critical),
        ("high", args.expected_high),
    ):
        if result["active_counts"].get(severity, 0) != expected:
            parser.error(f"{severity} active occurrence count does not match")
    if args.coverage:
        result["scan_provenance"] = verify_scan_provenance(
            args.report, args.coverage, args.provenance, args.source_revision,
            args.target, report_sha256,
        )
    args.output.write_text(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    main()
