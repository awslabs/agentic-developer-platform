"""Normalize OSV-Scanner and Trivy JSON output into a unified schema.

The normalized representation maps directly to the `vulnerabilities` table
in the agent_context database. Each scanner has a different output format;
this module provides one function per scanner that returns a list of
NormalizedVulnerability instances.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class NormalizedVulnerability:
    """Unified vulnerability finding from any scanner.

    Maps 1:1 to a row in the `vulnerabilities` Postgres table.
    Deduplication key: (cve_id, package_name, package_ecosystem).
    """

    cve_id: str
    aliases: list[str] = field(default_factory=list)
    package_name: str = ""
    package_ecosystem: str = ""
    affected_versions: str = ""
    fixed_version: str | None = None
    severity: str = "UNKNOWN"
    cvss_score: float | None = None
    summary: str = ""
    source_scanner: str = ""
    source_sbom_type: str = "source"
    raw_id: str = ""
    detected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# Severity helpers
# ---------------------------------------------------------------------------

_CVSS_SEVERITY_MAP = {
    (0.0, 0.1): "NONE",
    (0.1, 4.0): "LOW",
    (4.0, 7.0): "MEDIUM",
    (7.0, 9.0): "HIGH",
    (9.0, 10.1): "CRITICAL",
}


def _cvss_to_severity(score: float | None) -> str:
    """Map a numeric CVSS score to a severity label."""
    if score is None:
        return "UNKNOWN"
    for (lo, hi), label in _CVSS_SEVERITY_MAP.items():
        if lo <= score < hi:
            return label
    return "UNKNOWN"


def _extract_cvss_score(severity_list: list[dict[str, Any]]) -> float | None:
    """Extract numeric CVSS score from OSV severity array."""
    for entry in severity_list:
        score_str = entry.get("score", "")
        # CVSS vector string: "CVSS:3.1/AV:N/AC:L/..." — extract base score
        # from the vector by looking for the score field, or parse from database_specific
        if score_str.startswith("CVSS:"):
            # The numeric score is sometimes in a sibling field
            pass
        # Try to parse a bare float
        try:
            return float(score_str)
        except (ValueError, TypeError):
            continue
    return None


def _extract_fixed_version_osv(affected: list[dict[str, Any]]) -> str | None:
    """Extract the first fixed version from OSV affected ranges."""
    for entry in affected:
        for range_info in entry.get("ranges", []):
            for event in range_info.get("events", []):
                if "fixed" in event:
                    return event["fixed"]
    return None


def _extract_affected_range_osv(affected: list[dict[str, Any]]) -> str:
    """Build a human-readable affected-versions string from OSV ranges."""
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


def _pick_cve_id(vuln_id: str, aliases: list[str]) -> str:
    """Prefer a CVE-* identifier as the canonical ID."""
    if vuln_id.startswith("CVE-"):
        return vuln_id
    for alias in aliases:
        if alias.startswith("CVE-"):
            return alias
    # No CVE alias — use the original ID (GHSA, OSV, etc.)
    return vuln_id


# ---------------------------------------------------------------------------
# OSV-Scanner normalization
# ---------------------------------------------------------------------------


def normalize_osv(
    osv_json: dict[str, Any],
    sbom_type: str = "source",
) -> list[NormalizedVulnerability]:
    """Normalize OSV-Scanner JSON output into unified findings.

    Args:
        osv_json: Parsed JSON from `osv-scanner scan source -L ... --format json`
        sbom_type: "source" or "image" depending on which SBOM was scanned

    Returns:
        List of NormalizedVulnerability instances (one per unique vuln+package).
    """
    results: list[NormalizedVulnerability] = []
    now = datetime.now(timezone.utc)

    for result_block in osv_json.get("results", []):
        for pkg_entry in result_block.get("packages", []):
            pkg_info = pkg_entry.get("package", {})
            pkg_name = pkg_info.get("name", "")
            pkg_ecosystem = pkg_info.get("ecosystem", "").lower()

            for vuln in pkg_entry.get("vulnerabilities", []):
                vuln_id = vuln.get("id", "")
                aliases = vuln.get("aliases", [])
                cve_id = _pick_cve_id(vuln_id, aliases)

                severity_list = vuln.get("severity", [])
                cvss_score = _extract_cvss_score(severity_list)

                # Use database_specific severity if available
                db_specific = vuln.get("database_specific", {})
                severity_label = db_specific.get("severity", "").upper()
                if severity_label not in (
                    "CRITICAL",
                    "HIGH",
                    "MEDIUM",
                    "LOW",
                ):
                    severity_label = _cvss_to_severity(cvss_score)

                affected = vuln.get("affected", [])

                # Build aliases list: all IDs except the canonical cve_id
                all_ids = [vuln_id] + aliases
                deduped_aliases = [a for a in all_ids if a != cve_id]

                results.append(
                    NormalizedVulnerability(
                        cve_id=cve_id,
                        aliases=deduped_aliases,
                        package_name=pkg_name,
                        package_ecosystem=pkg_ecosystem,
                        affected_versions=_extract_affected_range_osv(affected),
                        fixed_version=_extract_fixed_version_osv(affected),
                        severity=severity_label,
                        cvss_score=cvss_score,
                        summary=vuln.get("summary", ""),
                        source_scanner="osv-scanner",
                        source_sbom_type=sbom_type,
                        raw_id=vuln_id,
                        detected_at=now,
                    )
                )

    return results


# ---------------------------------------------------------------------------
# Trivy normalization
# ---------------------------------------------------------------------------

# Trivy target class → ecosystem mapping
_TRIVY_CLASS_ECOSYSTEM_MAP = {
    "os-pkgs": None,  # derived from Target field (e.g., "debian 12")
    "lang-pkgs": None,  # derived from Target field
}


def _trivy_target_to_ecosystem(target: str, class_type: str) -> str:
    """Derive ecosystem from Trivy target string.

    Examples:
        "debian 12.5" → "debian:12"
        "alpine 3.19.1" → "alpine:3.19"
        "Node.js" → "npm"
        "Python" → "pypi"
    """
    target_lower = target.lower()

    # OS-level: extract distro + major version
    os_match = re.match(r"(debian|ubuntu|alpine|rhel|amzn|oracle|suse)\s*(\d+)", target_lower)
    if os_match:
        distro = os_match.group(1)
        major = os_match.group(2)
        return f"{distro}:{major}"

    # Language-level fallback
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

    # Fallback: use the raw target
    return target_lower.replace(" ", ":")


def normalize_trivy(
    trivy_json: dict[str, Any],
    sbom_type: str = "image",
) -> list[NormalizedVulnerability]:
    """Normalize Trivy JSON output into unified findings.

    Args:
        trivy_json: Parsed JSON from `trivy sbom ... --format json` or
                    `trivy image ... --format json`
        sbom_type: "source" or "image" depending on what was scanned

    Returns:
        List of NormalizedVulnerability instances (one per unique vuln+package).
    """
    results: list[NormalizedVulnerability] = []
    now = datetime.now(timezone.utc)

    for result_block in trivy_json.get("Results", []):
        target = result_block.get("Target", "")
        class_type = result_block.get("Class", "")
        ecosystem = _trivy_target_to_ecosystem(target, class_type)

        for vuln in result_block.get("Vulnerabilities", []):
            vuln_id = vuln.get("VulnerabilityID", "")
            pkg_name = vuln.get("PkgName", "")

            # Trivy severity is already a string label
            severity = vuln.get("Severity", "UNKNOWN").upper()
            if severity not in ("CRITICAL", "HIGH", "MEDIUM", "LOW"):
                severity = "UNKNOWN"

            # CVSS score extraction
            cvss_score = None
            cvss_data = vuln.get("CVSS", {})
            # Trivy provides CVSS from multiple sources; prefer NVD
            for source in ("nvd", "redhat", "ghsa"):
                if source in cvss_data:
                    cvss_score = cvss_data[source].get("V3Score")
                    if cvss_score is not None:
                        break

            results.append(
                NormalizedVulnerability(
                    cve_id=vuln_id,
                    aliases=[],
                    package_name=pkg_name,
                    package_ecosystem=ecosystem,
                    affected_versions=vuln.get("InstalledVersion", ""),
                    fixed_version=vuln.get("FixedVersion") or None,
                    severity=severity,
                    cvss_score=cvss_score,
                    summary=vuln.get("Title", ""),
                    source_scanner="trivy",
                    source_sbom_type=sbom_type,
                    raw_id=vuln_id,
                    detected_at=now,
                )
            )

    return results
