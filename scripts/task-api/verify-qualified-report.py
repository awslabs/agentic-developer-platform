#!/usr/bin/env python3
"""Verify frozen qualification evidence; never substitutes for live collection."""

import argparse
import hashlib
import json
from pathlib import Path
from xml.etree import ElementTree

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--report", type=Path, required=True)
args = parser.parse_args()
report = json.loads(args.report.read_text())
assert report["criteria"] and all(
    row["status"] == "PASS" for row in report["criteria"].values()
), "Qualification is incomplete"
assert report.get("artifact_sha256"), "Qualification must bind nonempty evidence artifacts"
root = args.report.parent.resolve()
for name, expected in report["artifact_sha256"].items():
    path = (root / name).resolve()
    path.relative_to(root)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == expected, name
for run in report.get("test_runs", []):
    suites = ElementTree.parse(root / run["report"]).getroot().iter("testsuite")
    tests = failed = skipped = 0
    for suite in suites:
        tests += int(suite.attrib["tests"])
        failed += int(suite.attrib.get("failures", 0)) + int(
            suite.attrib.get("errors", 0)
        )
        skipped += int(suite.attrib.get("skipped", 0))
    assert tests == run["passed"] and failed == skipped == 0, run["report"]
print(
    json.dumps(
        {
            "lane": "frozen evidence integrity and recorded verdict verification; no live collection",
            "report": str(args.report),
            "criteria": len(report["criteria"]),
            "artifacts": len(report["artifact_sha256"]),
            "status": "PASS",
        }
    )
)
