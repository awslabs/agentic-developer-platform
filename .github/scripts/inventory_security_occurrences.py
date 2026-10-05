#!/usr/bin/env python3
"""Inventory SARIF results without collapsing repeated package occurrences.

Write output to a private path when inspecting private scanner reports. The
report hash and run/result indexes bind each entry to its original SARIF.
"""

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

from diff_security_findings import (
    _has_accepted_suppression,
    _sarif_rule_index,
    resolve_sarif_severity,
)


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
    args = parser.parse_args()

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
    args.output.write_text(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    main()
