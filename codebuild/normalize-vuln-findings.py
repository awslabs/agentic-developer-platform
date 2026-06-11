#!/usr/bin/env python3
"""Normalize OSV-Scanner + Trivy JSON findings into a unified format.

Called by bs-vuln-scan.yml during CodeBuild. Reads raw scanner JSON from
two directories and writes per-SBOM normalized JSON files for downstream
database ingestion.

Output format (one file per scanned SBOM):
{
  "slug": "modules-gateway",
  "scanned_at": "2026-06-11T22:00:00Z",
  "scanners": ["osv-scanner", "trivy"],
  "findings": [
    {
      "cve_id": "CVE-2024-12345",
      "aliases": ["GHSA-xxxx"],
      "package_name": "lodash",
      "package_ecosystem": "npm",
      "affected_versions": ">=0,<4.17.21",
      "fixed_version": "4.17.21",
      "severity": "HIGH",
      "cvss_score": 7.2,
      "summary": "Command injection in lodash",
      "source_scanner": "osv-scanner",
      "source_sbom_type": "source",
      "raw_id": "GHSA-jf85-cpcp-j695"
    }
  ]
}
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# Normalization logic (self-contained — no imports from pipeline package
# because CodeBuild doesn't have the agent-context package installed)
# ---------------------------------------------------------------------------


def _pick_cve_id(vuln_id: str, aliases: list[str]) -> str:
    """Prefer a CVE-* identifier as the canonical ID."""
    if vuln_id.startswith("CVE-"):
        return vuln_id
    for alias in aliases:
        if alias.startswith("CVE-"):
            return alias
    return vuln_id


def _extract_fixed_version_osv(affected: list[dict]) -> str | None:
    """Extract the first fixed version from OSV affected ranges."""
    for entry in affected:
        for range_info in entry.get("ranges", []):
            for event in range_info.get("events", []):
                if "fixed" in event:
                    return event["fixed"]
    return None


def _extract_affected_range_osv(affected: list[dict]) -> str:
    """Build a version-constraint string from OSV ranges."""
    constraints: list[str] = []
    for entry in affected:
        for range_info in entry.get("ranges", []):
            introduced = None
            fixed = None
            for event in range_info.get("events", []):
                if "introduced" in event:
                    introduced = event["introduced"]
                if "fixed" in event:
                    fixed = event["fixed"]
            if introduced and fixed:
                constraints.append(f">={introduced},<{fixed}")
            elif introduced:
                constraints.append(f">={introduced}")
    return " || ".join(constraints) if constraints else "all"


def _extract_cvss_score_osv(severity_list: list[dict]) -> float | None:
    """Extract numeric CVSS score from OSV severity array."""
    for entry in severity_list:
        score_str = entry.get("score", "")
        try:
            return float(score_str)
        except (ValueError, TypeError):
            continue
    return None


def _trivy_target_to_ecosystem(target: str) -> str:
    """Derive ecosystem from Trivy target string."""
    target_lower = target.lower()
    os_match = re.match(r"(debian|ubuntu|alpine|rhel|amzn|oracle|suse)\s*(\d+)", target_lower)
    if os_match:
        return f"{os_match.group(1)}:{os_match.group(2)}"
    lang_map = {
        "node.js": "npm",
        "python": "pypi",
        "go": "go",
        "rust": "crates.io",
        "java": "maven",
        "ruby": "rubygems",
        ".net": "nuget",
    }
    for key, ecosystem in lang_map.items():
        if key in target_lower:
            return ecosystem
    return target_lower.replace(" ", ":")


def normalize_osv_file(path: Path) -> list[dict]:
    """Normalize a single OSV-Scanner JSON file."""
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return []

    findings: list[dict] = []
    for result_block in data.get("results", []):
        for pkg_entry in result_block.get("packages", []):
            pkg_info = pkg_entry.get("package", {})
            pkg_name = pkg_info.get("name", "")
            pkg_ecosystem = pkg_info.get("ecosystem", "").lower()

            for vuln in pkg_entry.get("vulnerabilities", []):
                vuln_id = vuln.get("id", "")
                aliases = vuln.get("aliases", [])
                cve_id = _pick_cve_id(vuln_id, aliases)
                affected = vuln.get("affected", [])
                severity_list = vuln.get("severity", [])
                cvss_score = _extract_cvss_score_osv(severity_list)

                db_specific = vuln.get("database_specific", {})
                severity = db_specific.get("severity", "").upper()
                if severity not in ("CRITICAL", "HIGH", "MEDIUM", "LOW"):
                    severity = "UNKNOWN"

                findings.append(
                    {
                        "cve_id": cve_id,
                        "aliases": [a for a in aliases if a != cve_id],
                        "package_name": pkg_name,
                        "package_ecosystem": pkg_ecosystem,
                        "affected_versions": _extract_affected_range_osv(affected),
                        "fixed_version": _extract_fixed_version_osv(affected),
                        "severity": severity,
                        "cvss_score": cvss_score,
                        "summary": vuln.get("summary", ""),
                        "source_scanner": "osv-scanner",
                        "source_sbom_type": "source",
                        "raw_id": vuln_id,
                    }
                )
    return findings


def normalize_trivy_file(path: Path) -> list[dict]:
    """Normalize a single Trivy JSON file."""
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return []

    findings: list[dict] = []
    for result_block in data.get("Results", []):
        target = result_block.get("Target", "")
        ecosystem = _trivy_target_to_ecosystem(target)

        for vuln in result_block.get("Vulnerabilities", []):
            vuln_id = vuln.get("VulnerabilityID", "")
            severity = vuln.get("Severity", "UNKNOWN").upper()
            if severity not in ("CRITICAL", "HIGH", "MEDIUM", "LOW"):
                severity = "UNKNOWN"

            cvss_score = None
            for source in ("nvd", "redhat", "ghsa"):
                if source in vuln.get("CVSS", {}):
                    cvss_score = vuln["CVSS"][source].get("V3Score")
                    if cvss_score is not None:
                        break

            fixed = vuln.get("FixedVersion") or None

            findings.append(
                {
                    "cve_id": vuln_id,
                    "aliases": [],
                    "package_name": vuln.get("PkgName", ""),
                    "package_ecosystem": ecosystem,
                    "affected_versions": vuln.get("InstalledVersion", ""),
                    "fixed_version": fixed,
                    "severity": severity,
                    "cvss_score": cvss_score,
                    "summary": vuln.get("Title", ""),
                    "source_scanner": "trivy",
                    "source_sbom_type": "image",
                    "raw_id": vuln_id,
                }
            )
    return findings


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="Normalize vulnerability scan findings")
    parser.add_argument("--osv-dir", required=True, help="Directory with OSV-Scanner JSON files")
    parser.add_argument("--trivy-dir", required=True, help="Directory with Trivy JSON files")
    parser.add_argument("--output-dir", required=True, help="Output directory for normalized JSON")
    args = parser.parse_args()

    osv_dir = Path(args.osv_dir)
    trivy_dir = Path(args.trivy_dir)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    total_findings = 0

    # Collect all slugs from both scanner directories
    slugs: set[str] = set()
    for f in osv_dir.glob("*.json"):
        slugs.add(f.stem)
    for f in trivy_dir.glob("*.json"):
        slugs.add(f.stem)

    for slug in sorted(slugs):
        findings: list[dict] = []
        scanners_used: list[str] = []

        osv_file = osv_dir / f"{slug}.json"
        if osv_file.exists():
            osv_findings = normalize_osv_file(osv_file)
            findings.extend(osv_findings)
            if osv_findings:
                scanners_used.append("osv-scanner")

        trivy_file = trivy_dir / f"{slug}.json"
        if trivy_file.exists():
            trivy_findings = normalize_trivy_file(trivy_file)
            findings.extend(trivy_findings)
            if trivy_findings:
                scanners_used.append("trivy")

        # Deduplicate by (cve_id, package_name, package_ecosystem)
        seen: set[tuple[str, str, str]] = set()
        deduped: list[dict] = []
        for f_item in findings:
            key = (f_item["cve_id"], f_item["package_name"], f_item["package_ecosystem"])
            if key not in seen:
                seen.add(key)
                deduped.append(f_item)

        output = {
            "slug": slug,
            "scanned_at": now,
            "scanners": scanners_used,
            "finding_count": len(deduped),
            "findings": deduped,
        }

        out_path = output_dir / f"{slug}.json"
        out_path.write_text(json.dumps(output, indent=2, default=str))
        total_findings += len(deduped)

    print(f"Normalized {total_findings} findings across {len(slugs)} SBOMs")
    print(f"Output: {output_dir}/")


if __name__ == "__main__":
    main()
