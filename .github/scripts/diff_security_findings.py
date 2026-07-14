#!/usr/bin/env python3
"""Diff current security findings against committed baselines.

Produces a summary JSON with new, resolved, and stable findings per tool.
Optionally updates baseline files (for nightly runs).
"""

import argparse
import json
import re
import sys
from pathlib import Path


def _sarif_rule_index(run: dict) -> dict:
    """Map ruleId -> rule object for severity lookups."""
    driver = run.get("tool", {}).get("driver", {})
    return {r.get("id"): r for r in driver.get("rules", [])}


def _severity_of_sarif_result(result: dict, rules: dict) -> str:
    """Best-effort severity ('critical'|'high'|'medium'|'low'|'unknown') for a SARIF result.

    Handles: numeric `security-severity` (CVSS), string severity, grype's
    Severity: line in rule help text, and SARIF `level` as a last resort.
    """
    rule = rules.get(result.get("ruleId"), {})
    props = rule.get("properties", {})
    ss = str(props.get("security-severity", "")).strip().lower()
    if ss in ("critical", "high", "medium", "low"):
        return ss
    if ss:
        try:
            v = float(ss)
            return (
                "critical" if v >= 9.0
                else "high" if v >= 7.0
                else "medium" if v >= 4.0
                else "low"
            )
        except ValueError:
            pass
    # bandit puts issue_severity on the result properties
    rprops = result.get("properties", {})
    isev = str(rprops.get("issue_severity", "")).strip().lower()
    if isev in ("critical", "high", "medium", "low"):
        return isev
    # grype embeds "Severity: <x>" in the rule help text
    help_text = rule.get("help", {}).get("text", "")
    m = re.search(r"Severity:\s*(\w+)", help_text)
    if m and m.group(1).lower() in ("critical", "high", "medium", "low", "negligible"):
        return m.group(1).lower()
    return {"error": "high", "warning": "medium", "note": "low"}.get(
        result.get("level", ""), "unknown"
    )


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


def extract_sarif_fingerprints(sarif_data: dict, severities: dict | None = None) -> set[str]:
    """Extract unique finding identifiers from SARIF data.

    If `severities` is provided, it is populated as {fingerprint: severity}
    so callers can gate on new critical/high findings.
    """
    fingerprints = set()
    if not isinstance(sarif_data, dict):
        return fingerprints

    for run in sarif_data.get("runs", []):
        rules = _sarif_rule_index(run)
        for result in run.get("results", []):
            # Skip findings the source has explicitly suppressed (inline
            # `nosemgrep` etc.). Semgrep still emits these as results carrying a
            # non-empty `suppressions` array rather than dropping them, so a
            # gate that counts every result would fail forever on accepted,
            # annotated findings. Treat any suppressed result as not-a-finding.
            if result.get("suppressions"):
                continue
            # Use ruleId + location as fingerprint
            rule_id = result.get("ruleId", "unknown")
            sev = _severity_of_sarif_result(result, rules)
            locations = result.get("locations", [])
            for loc in locations:
                phys = loc.get("physicalLocation", {})
                artifact = phys.get("artifactLocation", {}).get("uri", "")
                region = phys.get("region", {})
                line = region.get("startLine", 0)
                fp = f"{rule_id}:{artifact}:{line}"
                fingerprints.add(fp)
                if severities is not None:
                    severities[fp] = sev

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
    severities: dict[str, str] = {}
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
            current_fingerprints |= extract_sarif_fingerprints(data, severities)
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
    # Severity of each NEW finding (unknown when not resolvable, e.g. JSON tools)
    result["new_severities"] = {fp: severities.get(fp, "unknown") for fp in result["new"]}
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Diff security findings against baselines")
    parser.add_argument("--findings-dir", required=True, help="Directory with current findings")
    parser.add_argument("--baseline-dir", required=True, help="Directory with baseline files")
    parser.add_argument("--output", required=True, help="Output summary JSON path")
    parser.add_argument("--update-baselines", action="store_true", help="Update baseline files with current findings")
    parser.add_argument(
        "--fail-on",
        default="",
        help="Comma-separated severities that fail the run when NEW (e.g. 'critical,high'). "
        "Empty = advisory only (never fail).",
    )
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

    # Hard gate: fail if any NEW finding matches a --fail-on severity.
    # Baseline-refresh runs pass no --fail-on and stay advisory.
    fail_sevs = {s.strip().lower() for s in args.fail_on.split(",") if s.strip()}
    if fail_sevs:
        offenders = []
        for tool, res in summary.items():
            for fp, sev in res.get("new_severities", {}).items():
                if sev in fail_sevs:
                    offenders.append((sev, tool, fp))
        if offenders:
            print(
                f"\n::error::Security gate FAILED — {len(offenders)} new "
                f"{'/'.join(sorted(fail_sevs))} finding(s):"
            )
            for sev, tool, fp in sorted(offenders):
                print(f"  [{sev.upper()}] {tool}: {fp}")
            sys.exit(1)
        print(f"Security gate PASSED — no new {'/'.join(sorted(fail_sevs))} findings.")


if __name__ == "__main__":
    main()
