#!/usr/bin/env python3
"""Diff current security findings against committed baselines.

Produces a summary JSON with new, resolved, and stable findings per tool.
Optionally updates baseline files (for nightly runs).
"""

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path


def _sarif_rule_index(run: dict) -> dict:
    """Map ruleId -> rule object for severity lookups."""
    driver = run.get("tool", {}).get("driver", {})
    return {r.get("id"): r for r in driver.get("rules", [])}


RATED_SEVERITIES = ("critical", "high", "medium", "low", "negligible")

# Severity we report when the only signal available is a SARIF `level`. A level
# is a *document-classification* default, not a severity judgement, so it is
# deliberately NOT mapped onto critical/high/medium/low. See
# `resolve_sarif_severity` for why.
UNRATED = "unrated"


def _cvss_to_severity(score: float) -> str:
    """Map a CVSS base score onto a severity band (CVSS v3 qualitative ratings)."""
    if score >= 9.0:
        return "critical"
    if score >= 7.0:
        return "high"
    if score >= 4.0:
        return "medium"
    return "low"


def resolve_sarif_severity(result: dict, rules: dict) -> tuple[str, str]:
    """Resolve (severity, source) for a SARIF result.

    `source` records WHAT established the severity, so the gate can distinguish
    "a scanner rated this high" from "we had nothing to go on":

      "native"        -- the scanner's own qualitative rating. Preferred over a
                         numeric score: a feed maintainer's rating accounts for
                         how the package is actually built/shipped, which a raw
                         CVSS base score does not. CVE-2020-15778 is the
                         canonical case -- native `low`, CVSS `7.8`. Reading the
                         number first reports a low-rated CVE as high and fails
                         the gate on it.
      "cvss"          -- numeric `security-severity` only; used when the scanner
                         published no qualitative rating of its own.
      "default-level" -- only a SARIF `level` was available (set on the result,
                         inherited from the rule's defaultConfiguration, or
                         absent and implied by the format). Many tools stamp
                         EVERY result `error` by default, so treating that as an
                         explicit "high" promotes a whole tool's output into the
                         gate. Reported as `unrated` instead -- still counted and
                         still printed, never silently dropped.
    """
    rule = rules.get(result.get("ruleId"), {})
    props = rule.get("properties", {})
    raw_ss = str(props.get("security-severity", "")).strip().lower()

    # --- Native (qualitative) ratings, in order of specificity -------------
    if raw_ss in RATED_SEVERITIES:
        return raw_ss, "native"

    # bandit puts issue_severity on the result properties
    isev = str(result.get("properties", {}).get("issue_severity", "")).strip().lower()
    if isev in RATED_SEVERITIES:
        return isev, "native"

    # grype embeds "Severity: <x>" in the rule help text
    help_text = rule.get("help", {}).get("text", "")
    m = re.search(r"Severity:\s*(\w+)", help_text)
    if m and m.group(1).lower() in RATED_SEVERITIES:
        return m.group(1).lower(), "native"

    # --- Numeric CVSS, only when no native rating was published ------------
    if raw_ss:
        try:
            return _cvss_to_severity(float(raw_ss)), "cvss"
        except ValueError:
            pass

    # --- Nothing but a level: not a severity judgement ---------------------
    return UNRATED, "default-level"


def _severity_of_sarif_result(result: dict, rules: dict) -> str:
    """Severity for a SARIF result, discarding the provenance. See `resolve_sarif_severity`."""
    return resolve_sarif_severity(result, rules)[0]


TOOL_BASELINE_MAP = {
    "checkov": "checkov-baseline.json",
    "semgrep": "semgrep-baseline.sarif",
    "detect-secrets": ".secrets.baseline",
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


def _has_accepted_suppression(result: dict) -> bool:
    suppressions = result.get("suppressions", [])
    return isinstance(suppressions, list) and any(
        isinstance(suppression, dict) and suppression.get("status") == "accepted"
        for suppression in suppressions
    )


def _locationless_sarif_fingerprint(result: dict, rule_id: str) -> str:
    provided = {
        key: value
        for key in ("fingerprints", "partialFingerprints")
        if isinstance((value := result.get(key)), dict) and value
    }
    identity = provided or {
        key: value
        for key, value in result.items()
        if key not in {"baselineState", "level", "rank", "suppressions"}
    }
    encoded = json.dumps(
        identity,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    return f"{rule_id}:locationless:{hashlib.sha256(encoded).hexdigest()}"


def extract_sarif_fingerprints(
    sarif_data: dict,
    severities: dict | None = None,
    sources: dict | None = None,
    namespace: str = "",
) -> set[str]:
    """Extract unique finding identifiers from SARIF data.

    If `severities` is provided, it is populated as {fingerprint: severity}
    so callers can gate on new critical/high findings. If `sources` is provided,
    it is populated as {fingerprint: source} recording what established each
    severity ("native" | "cvss" | "default-level"). `namespace` identifies
    the report that produced the finding when one tool emits multiple reports.
    """
    fingerprints = set()
    if not isinstance(sarif_data, dict):
        return fingerprints

    for run in sarif_data.get("runs", []):
        rules = _sarif_rule_index(run)
        for result in run.get("results", []):
            # Accepted inline suppressions such as `nosemgrep` are not active
            # findings. Rejected, under-review, missing, or malformed statuses
            # have not been accepted and must remain visible to the gate.
            if _has_accepted_suppression(result):
                continue
            rule_id = result.get("ruleId", "unknown")
            sev, sev_source = resolve_sarif_severity(result, rules)
            locations = result.get("locations", [])
            location_fingerprints = set()
            for loc in locations if isinstance(locations, list) else []:
                if not isinstance(loc, dict):
                    continue
                phys = loc.get("physicalLocation", {})
                artifact = phys.get("artifactLocation", {}).get("uri", "")
                region = phys.get("region", {})
                line = region.get("startLine", 0)
                location_fingerprints.add(f"{rule_id}:{artifact}:{line}")
            if not location_fingerprints:
                location_fingerprints.add(
                    _locationless_sarif_fingerprint(result, rule_id)
                )
            for raw_fp in location_fingerprints:
                fp = f"{namespace}:{raw_fp}" if namespace else raw_fp
                fingerprints.add(fp)
                if severities is not None:
                    severities[fp] = sev
                if sources is not None:
                    sources[fp] = sev_source

    return fingerprints


def extract_json_fingerprints(
    data: dict | list,
    severities: dict | None = None,
    sources: dict | None = None,
    namespace: str = "",
) -> set[str]:
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
                raw_severity = (
                    val.get("severity", "unknown")
                    if isinstance(val, dict)
                    else "unknown"
                )
                severity = str(raw_severity).strip().lower()
                if severity not in {
                    "critical",
                    "high",
                    "moderate",
                    "medium",
                    "low",
                    "info",
                    "negligible",
                }:
                    severity = "unknown"
                prefix = f"{namespace}:" if namespace else ""
                fingerprint = f"{prefix}{key}:{severity}"
                fingerprints.add(fingerprint)
                if severity != "unknown":
                    if severities is not None:
                        severities[fingerprint] = severity
                    if sources is not None:
                        sources[fingerprint] = "native"

    return fingerprints


def extract_detect_secrets_fingerprints(
    data: dict,
    severities: dict | None = None,
    sources: dict | None = None,
) -> set[str]:
    """Extract the same fingerprints from scan-baseline and audit-report JSON."""
    fingerprints = set()
    if not isinstance(data, dict):
        return fingerprints

    results = data.get("results", {})
    if isinstance(results, dict):
        records = []
        for result_path, entries in results.items():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict) or entry.get("is_secret") is False:
                    continue
                records.append(
                    (
                        entry.get("filename") or result_path,
                        [entry.get("type", "unknown")],
                        [entry.get("line_number", 0)],
                    )
                )
    elif isinstance(results, list):
        records = []
        for entry in results:
            if not isinstance(entry, dict) or entry.get("category") == "FALSE_POSITIVE":
                continue
            finding_types = entry.get("types", [entry.get("type", "unknown")])
            if isinstance(finding_types, str):
                finding_types = [finding_types]
            lines = entry.get("lines", {})
            line_numbers = (
                list(lines)
                if isinstance(lines, dict) and lines
                else [entry.get("line_number", 0)]
            )
            records.append((entry.get("filename", ""), finding_types, line_numbers))
    else:
        return fingerprints

    for filename, finding_types, line_numbers in records:
        for finding_type in finding_types:
            for line_number in line_numbers:
                fingerprint = f"{finding_type}:{filename}:{line_number}"
                fingerprints.add(fingerprint)
                if severities is not None:
                    severities[fingerprint] = UNRATED
                if sources is not None:
                    sources[fingerprint] = "tool-unrated"

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


def _match_legacy_grype_baseline(
    legacy_fingerprints: set[str], current_fingerprints: set[str]
) -> set[str]:
    """Namespace a legacy baseline only when its image attribution is unambiguous."""
    current_by_legacy: dict[str, set[str]] = {}
    for current_fingerprint in current_fingerprints:
        _, separator, legacy_fingerprint = current_fingerprint.partition(":")
        if separator:
            current_by_legacy.setdefault(legacy_fingerprint, set()).add(
                current_fingerprint
            )

    matched = set()
    for legacy_fingerprint in legacy_fingerprints:
        occurrences = current_by_legacy.get(legacy_fingerprint, set())
        matched.update(occurrences if len(occurrences) == 1 else {legacy_fingerprint})
    return matched


def process_tool_findings(
    tool: str, findings_dir: Path, baseline_dir: Path
) -> dict:
    """Process findings for a single tool."""
    baseline_file = baseline_dir / TOOL_BASELINE_MAP.get(tool, f"{tool}-baseline.json")
    baseline_data = load_json_safe(baseline_file)

    # Find current findings files for this tool
    current_fingerprints: set[str] = set()
    severities: dict[str, str] = {}
    sev_sources: dict[str, str] = {}
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
        if tool == "detect-secrets":
            current_fingerprints |= extract_detect_secrets_fingerprints(
                data, severities, sev_sources
            )
        elif isinstance(data, dict) and "runs" in data:
            namespace = path.stem if tool == "grype" else ""
            current_fingerprints |= extract_sarif_fingerprints(
                data, severities, sev_sources, namespace
            )
        else:
            namespace = ""
            if tool == "npm-audit":
                namespace = path.stem.removeprefix("npm-audit-")
            current_fingerprints |= extract_json_fingerprints(
                data, severities, sev_sources, namespace
            )

    # Extract baseline fingerprints
    if tool == "detect-secrets":
        baseline_fingerprints = extract_detect_secrets_fingerprints(baseline_data)
    elif isinstance(baseline_data, dict) and "runs" in baseline_data:
        baseline_fingerprints = extract_sarif_fingerprints(baseline_data)
        if tool == "grype":
            baseline_fingerprints = _match_legacy_grype_baseline(
                baseline_fingerprints, current_fingerprints
            )
    elif baseline_data:
        baseline_fingerprints = extract_json_fingerprints(baseline_data)
    else:
        baseline_fingerprints = set()

    result = diff_findings(current_fingerprints, baseline_fingerprints)
    result["files_scanned"] = [str(f) for f in found_files]
    # Severity of each NEW finding (unknown when not resolvable, e.g. JSON tools)
    result["new_severities"] = {fp: severities.get(fp, "unknown") for fp in result["new"]}
    # What established each severity -- lets a reader (and S21's triage) tell a
    # scanner-rated high from a finding we declined to rate. Findings whose only
    # signal was a SARIF level land here as "default-level"/`unrated`: they stay
    # counted and reported, they just do not fail the gate on their own.
    result["new_severity_sources"] = {
        fp: sev_sources.get(fp, "unknown") for fp in result["new"]
    }
    result["new_unrated_count"] = sum(
        1 for fp in result["new"] if severities.get(fp, "unknown") in (UNRATED, "unknown")
    )
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
                    if (
                        tool == "detect-secrets"
                        and path.name != "detect-secrets-results.json"
                    ):
                        continue
                    # Copy the latest findings as the new baseline
                    data = load_json_safe(path)
                    if data:
                        baseline_file.write_text(json.dumps(data, indent=2))
                        break

    # Print summary to stdout
    total_new = sum(v["new_count"] for v in summary.values())
    total_resolved = sum(v["resolved_count"] for v in summary.values())
    total_unrated = sum(v.get("new_unrated_count", 0) for v in summary.values())
    print(f"Summary: {total_new} new findings, {total_resolved} resolved")

    # Unrated findings do not fail the gate, so report aggregate counts. The
    # private summary retains the per-finding fingerprints and provenance; logs
    # must not disclose repository paths, rule ids, or secret locations.
    if total_unrated:
        print(
            f"\n{total_unrated} new finding(s) carried no scanner severity rating "
            f"(reported as '{UNRATED}', not gated). See the private summary for triage."
        )
        for tool, res in sorted(summary.items()):
            count = res.get("new_unrated_count", 0)
            if count:
                print(f"  {tool}: {count} unrated finding(s)")

    # Hard gate: fail if any NEW finding matches a --fail-on severity.
    # Baseline-refresh runs pass no --fail-on and stay advisory.
    fail_sevs = {s.strip().lower() for s in args.fail_on.split(",") if s.strip()}
    if fail_sevs:
        offender_counts = {}
        for tool, res in summary.items():
            for sev in res.get("new_severities", {}).values():
                if sev in fail_sevs:
                    key = (tool, sev)
                    offender_counts[key] = offender_counts.get(key, 0) + 1
        if offender_counts:
            offender_total = sum(offender_counts.values())
            print(
                f"\n::error::Security gate FAILED — {offender_total} new "
                f"{'/'.join(sorted(fail_sevs))} finding(s). "
                "See the private summary for details."
            )
            for (tool, sev), count in sorted(offender_counts.items()):
                print(f"  {tool}: {count} {sev} finding(s)")
            sys.exit(1)
        print(f"Security gate PASSED — no new {'/'.join(sorted(fail_sevs))} findings.")


if __name__ == "__main__":
    main()
