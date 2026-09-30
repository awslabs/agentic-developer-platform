#!/usr/bin/env python3
"""Adopt completed filing on retry without rewriting its issue links."""
import argparse
from pathlib import Path

from join_barrier import load_markers
from security_traceability import TRACEABILITY_SCHEMA_VERSION, TraceabilityError, assert_fully_traced, read_ledger, severity_by_finding


def can_resume(ledger_dir, new_findings, source, run_date):
    directory = Path(ledger_dir)
    path = directory / f"traceability.{source}.json"
    markers = load_markers(directory)
    marker = markers.get(source)
    if not path.exists():
        if marker and marker["fields"].get("story_ids"):
            raise ValueError("Filed issues exist without traceability; restore the ledger before retrying.")
        return False
    trace = read_ledger(path)
    if not isinstance(trace, dict) or trace.get("schema_version") != TRACEABILITY_SCHEMA_VERSION or trace.get("stage") not in {"grouping", "filed"}:
        raise ValueError("Unrecognized traceability format; restore the ledger before retrying.")
    if trace.get("stage") != "filed":
        if marker and marker["fields"].get("story_ids"):
            raise ValueError("Filing is incomplete; reconcile the existing issues before retrying.")
        return False
    assert_fully_traced(trace)
    if trace.get("run_date") != run_date or trace.get("source") != source:
        raise ValueError("Filed traceability belongs to a different date or scanner.")
    expected = severity_by_finding(new_findings, source)
    actual = {fid: row["severity"] for fid, row in trace["findings_index"].items()}
    if expected != actual:
        raise ValueError("Findings differ from the already-filed ledger. Preserve it and use a new scan date; do not overwrite issue links.")
    if not marker or marker["run_date"] != run_date:
        raise ValueError("Completed filing has no matching completion marker; restore it before retrying.")
    if set(marker["fields"].get("findings_covered", [])) != set(expected):
        raise ValueError("Completion marker does not cover the filed findings.")
    issues = {group["issue_number"] for group in trace["groups"]}
    if any(type(number) is not int or number <= 0 for number in issues):
        raise ValueError("Filed ledger contains invalid issue numbers.")
    if len(issues) != len(trace["groups"]) or marker["fields"].get("stories_created") != len(issues):
        raise ValueError("Completion marker has inconsistent filing counts.")
    if set(marker["fields"].get("story_ids", [])) != issues:
        raise ValueError("Completion marker and traceability disagree on filed issues.")
    for group in trace["groups"]:
        if any(trace["findings_index"][fid]["issue_number"] != group["issue_number"] for fid in group["finding_ids"]):
            raise ValueError("Traceability has conflicting finding-to-issue links.")
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger-dir", required=True)
    parser.add_argument("--new-findings", required=True)
    parser.add_argument("--source", choices=["code-review", "pentest"], required=True)
    parser.add_argument("--run-date", required=True)
    args = parser.parse_args()
    try:
        resume = can_resume(args.ledger_dir, args.new_findings, args.source, args.run_date)
    except (TraceabilityError, ValueError, KeyError, TypeError, OSError) as exc:
        # No finding descriptions or provider detail in public CI output.
        print(f"::error title=Security triage recovery::{exc}")
        return 1
    print(f"resume={'true' if resume else 'false'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
