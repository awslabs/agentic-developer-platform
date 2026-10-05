#!/usr/bin/env python3
"""Record or verify provisional #6998 candidate SARIF without closing findings."""

import argparse
import hashlib
import importlib.util
import json
from collections import defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PREFIX = "modules/agent-factory/"


def severity_resolver():
    script = ROOT / ".github/scripts/diff_security_findings.py"
    spec = importlib.util.spec_from_file_location("diff_security_findings", script)
    if not spec or not spec.loader:
        raise RuntimeError("severity resolver unavailable")
    resolver = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(resolver)
    return resolver


def candidate_hits(sarif, catalog):
    resolver = severity_resolver()
    results = defaultdict(list)
    for run in sarif.get("runs", []):
        rules = resolver._sarif_rule_index(run)
        for result in run.get("results", []):
            if not result.get("ruleId", "").endswith(catalog["rule"]):
                continue
            severity, source = resolver.resolve_sarif_severity(result, rules)
            if resolver._has_accepted_suppression(result):
                suppression = "accepted"
            elif result.get("suppressions"):
                suppression = "inSource-unaccepted"
            else:
                suppression = "none"
            for location in result.get("locations", []):
                physical = location.get("physicalLocation", {})
                path = physical.get("artifactLocation", {}).get("uri", "")
                line = physical.get("region", {}).get("startLine")
                if not path.startswith(PREFIX) or not isinstance(line, int):
                    raise ValueError("assigned rule reported a location outside scanned source")
                results[path.removeprefix(PREFIX)].append({
                    "line": line, "severity": severity, "severitySource": source,
                    "suppression": suppression,
                })
    return {path: sorted(hits, key=lambda hit: hit["line"]) for path, hits in results.items()}


def map_records(catalog, hits):
    records = defaultdict(list)
    for record in catalog["records"]:
        records[record["file"]].append(record)
    if set(records) != set(hits):
        raise ValueError("candidate rule has new, missing, or unmapped files")
    matched = []
    for path, expected in records.items():
        actual = hits[path]
        if len(expected) != len(actual):
            raise ValueError(f"candidate rule count changed at {path}")
        for record, hit in zip(sorted(expected, key=lambda item: item["line"]), actual):
            if record["disposition"] != "unresolved":
                raise ValueError("candidate scan cannot resolve a record without original review")
            matched.append((record, hit))
    if len(matched) != 18:
        raise ValueError("candidate rule must reconcile all 18 assigned records")
    return matched


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sarif", required=True, type=Path)
    parser.add_argument("--catalog", type=Path, default=ROOT / "data/security/issue-6998-http-destinations.json")
    parser.add_argument("--record", action="store_true", help="write candidate metadata, never dispositions")
    args = parser.parse_args()
    raw = args.sarif.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    catalog = json.loads(args.catalog.read_text())
    matched = map_records(catalog, candidate_hits(json.loads(raw), catalog))
    if args.record:
        catalog["candidateRawSarifSha256"] = digest
        for record, hit in matched:
            record["candidateEvidence"]["result"] = hit
        args.catalog.write_text(json.dumps(catalog, indent=2) + "\n")
    else:
        if catalog.get("candidateRawSarifSha256") != digest:
            raise ValueError("candidate raw SARIF hash differs from recorded evidence")
        for record, hit in matched:
            if record["candidateEvidence"].get("result") != hit:
                raise ValueError(f"candidate result differs for {record['file']}:{record['line']}")
    counts = defaultdict(int)
    for _, hit in matched:
        counts[hit["suppression"]] += 1
    print(f"candidate mapped: {len(matched)} unresolved; "
          f"in-source unaccepted: {counts['inSource-unaccepted']}; "
          f"unsuppressed: {counts['none']}; accepted: {counts['accepted']}")


if __name__ == "__main__":
    main()
