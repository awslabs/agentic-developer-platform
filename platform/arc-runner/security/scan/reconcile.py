#!/usr/bin/env python3
"""Inventory assigned Grype SARIF occurrences without silently dropping records.

Run against original private reports. Keep the output private: it includes
installed paths and original scanner help text. This does not dispose findings.
"""

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path


REPORTS = {
    "platform-arc-runner": {
        "sha256": "66bc1035a6b11549986fb0efd7537111bba5d610cb7cdd8738c5414a4465b26f",
        "critical": 0,
        "high": 12,
    },
    "platform-automation-infra": {
        "sha256": "9341b839afea3a448774b2c94360954e2d9e6646a89bfc1acf2e77125f76ae44",
        "critical": 64,
        "high": 478,
    },
}


def load_resolver(path):
    spec = importlib.util.spec_from_file_location("diff_security_findings", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.resolve_sarif_severity


def inventory(path, target, resolve_severity, expected=None):
    expected = REPORTS[target] if expected is None else expected
    content = path.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    if digest != expected["sha256"]:
        raise ValueError(f"{target}: SARIF checksum mismatch: {digest}")
    report = json.loads(content)
    findings = []
    counts = {"critical": 0, "high": 0}
    for run_index, run in enumerate(report["runs"]):
        rules = {rule["id"]: rule for rule in run["tool"]["driver"]["rules"]}
        for result_index, result in enumerate(run.get("results", [])):
            severity, source = resolve_severity(result, rules)
            if severity not in counts:
                continue
            rule_id = result["ruleId"]
            if rule_id not in rules:
                raise ValueError(f"{target}: missing rule {rule_id}")
            locations = [
                location.get("physicalLocation", {}).get("artifactLocation", {}).get("uri", "")
                for location in result.get("locations", [])
            ]
            if not locations or any(not location for location in locations):
                raise ValueError(f"{target}: {run_index}/{result_index} lacks an installed path")
            counts[severity] += 1
            findings.append({
                "key": f"{target}:{run_index}:{result_index}",
                "rule_id": rule_id,
                "severity": severity,
                "severity_source": source,
                "installed_paths": locations,
                "message": result.get("message", {}),
                "rule_help": rules[rule_id].get("help", {}),
                "suppressions": result.get("suppressions", []),
                "disposition": "unresolved",
            })
    if counts != {severity: expected[severity] for severity in counts}:
        raise ValueError(f"{target}: unexpected rated occurrence counts {counts}")
    if len({finding["key"] for finding in findings}) != sum(counts.values()):
        raise ValueError(f"{target}: duplicate occurrence key")
    return {"target": target, "sarif_sha256": digest, "counts": counts, "findings": findings}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arc", type=Path, required=True, help="Original effective ARC SARIF")
    parser.add_argument("--automation", type=Path, required=True, help="Original effective automation SARIF")
    parser.add_argument("--resolver", type=Path, required=True, help="Severity resolver at scanned revision")
    parser.add_argument("--output", type=Path, required=True, help="PRIVATE output file (not in the repository)")
    args = parser.parse_args()
    resolve_severity = load_resolver(args.resolver)
    results = [
        inventory(args.arc, "platform-arc-runner", resolve_severity),
        inventory(args.automation, "platform-automation-infra", resolve_severity),
    ]
    if args.output.resolve().is_relative_to(Path.cwd().resolve()):
        raise ValueError("Write private finding details outside the repository")
    args.output.write_text(json.dumps({"reports": results}, indent=2) + "\n")
    print("Validated 0 critical / 12 high ARC and 64 critical / 478 high automation occurrences")


if __name__ == "__main__":
    main()
