#!/usr/bin/env python3
"""Diff current security findings against committed baselines.

Produces a summary JSON with new, resolved, and stable findings per tool.
Optionally updates baseline files (for nightly runs).
"""

import argparse
import json
import os
import sys
from pathlib import Path


TOOL_BASELINE_MAP = {
    "checkov": "checkov-baseline.json",
    "semgrep": "semgrep-baseline.sarif",
    "grype": "grype-baseline.json",
    "bandit": "bandit-baseline.json",
    "cfn-nag": "cfn-nag-baseline.json",
    "npm-audit": "npm-audit-baseline.json",
}


def load_json_safe(path: Path) -> dict | list:
    """Load JSON, returning empty dict if file missing or invalid."""
    if not path.exists():
        return {}
    try:
        content = path.read_text().strip()
        if not content:
            return {}
        return json.loads(content)
    except (json.JSONDecodeError, OSError):
        return {}


def extract_sarif_fingerprints(sarif_data: dict) -> set[str]:
    """Extract unique finding identifiers from SARIF data."""
    fingerprints = set()
    if not isinstance(sarif_data, dict):
        return fingerprints

    for run in sarif_data.get("runs", []):
        for result in run.get("results", []):
            # Use ruleId + location as fingerprint
            rule_id = result.get("ruleId", "unknown")
            locations = result.get("locations", [])
            for loc in locations:
                phys = loc.get("physicalLocation", {})
                artifact = phys.get("artifactLocation", {}).get("uri", "")
                region = phys.get("region", {})
                line = region.get("startLine", 0)
                fingerprints.add(f"{rule_id}:{artifact}:{line}")

    return fingerprints


def extract_json_fingerprints(data: dict | list) -> set[str]:
    """Extract fingerprints from JSON findings (cfn-nag, npm-audit)."""
    fingerprints = set()

    if isinstance(data, list):
        for item in data:
            fp = json.dumps(item, sort_keys=True)
            fingerprints.add(fp)
    elif isinstance(data, dict):
        # npm-audit format: advisories or vulnerabilities key
        vulns = data.get("vulnerabilities", data.get("advisories", {}))
        if isinstance(vulns, dict):
            for key, val in vulns.items():
                fingerprints.add(f"{key}:{val.get('severity', 'unknown')}")

    return fingerprints


def diff_findings(
    current_fingerprints: set[str], baseline_fingerprints: set[str]
) -> dict:
    """Compare current findings against baseline."""
    new = current_fingerprints - baseline_fingerprints
    resolved = baseline_fingerprints - current_fingerprints
    stable = current_fingerprints & baseline_fingerprints

    return {
        "new": sorted(new),
        "resolved": sorted(resolved),
        "stable": sorted(stable),
        "new_count": len(new),
        "resolved_count": len(resolved),
        "stable_count": len(stable),
    }


def process_tool_findings(
    tool: str, findings_dir: Path, baseline_dir: Path
) -> dict:
    """Process findings for a single tool."""
    baseline_file = baseline_dir / TOOL_BASELINE_MAP.get(tool, f"{tool}-baseline.json")
    baseline_data = load_json_safe(baseline_file)

    # Find current findings files for this tool
    current_fingerprints: set[str] = set()
    found_files = []

    for path in findings_dir.rglob("*"):
        if not path.is_file():
            continue
        if tool not in path.parent.name and tool not in path.name:
            continue
        found_files.append(path)

        data = load_json_safe(path)
        if not data:
            continue

        # Detect format: SARIF vs plain JSON
        if isinstance(data, dict) and "runs" in data:
            current_fingerprints |= extract_sarif_fingerprints(data)
        else:
            current_fingerprints |= extract_json_fingerprints(data)

    # Extract baseline fingerprints
    if isinstance(baseline_data, dict) and "runs" in baseline_data:
        baseline_fingerprints = extract_sarif_fingerprints(baseline_data)
    elif baseline_data:
        baseline_fingerprints = extract_json_fingerprints(baseline_data)
    else:
        baseline_fingerprints = set()

    result = diff_findings(current_fingerprints, baseline_fingerprints)
    result["files_scanned"] = [str(f) for f in found_files]
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Diff security findings against baselines")
    parser.add_argument("--findings-dir", required=True, help="Directory with current findings")
    parser.add_argument("--baseline-dir", required=True, help="Directory with baseline files")
    parser.add_argument("--output", required=True, help="Output summary JSON path")
    parser.add_argument("--update-baselines", action="store_true", help="Update baseline files with current findings")
    args = parser.parse_args()

    findings_dir = Path(args.findings_dir)
    baseline_dir = Path(args.baseline_dir)

    summary = {}
    for tool in TOOL_BASELINE_MAP:
        summary[tool] = process_tool_findings(tool, findings_dir, baseline_dir)

    # Write summary
    output_path = Path(args.output)
    output_path.write_text(json.dumps(summary, indent=2))

    # Optionally update baselines
    if args.update_baselines:
        for tool in TOOL_BASELINE_MAP:
            baseline_file = baseline_dir / TOOL_BASELINE_MAP[tool]
            # Collect all current findings into the baseline
            for path in findings_dir.rglob("*"):
                if not path.is_file():
                    continue
                if tool in path.parent.name or tool in path.name:
                    # Copy the latest findings as the new baseline
                    data = load_json_safe(path)
                    if data:
                        baseline_file.write_text(json.dumps(data, indent=2))
                        break

    # Print summary to stdout
    total_new = sum(v["new_count"] for v in summary.values())
    total_resolved = sum(v["resolved_count"] for v in summary.values())
    print(f"Summary: {total_new} new findings, {total_resolved} resolved")


if __name__ == "__main__":
    main()
