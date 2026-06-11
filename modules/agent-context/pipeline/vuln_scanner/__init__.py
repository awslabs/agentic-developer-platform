"""Vulnerability matching engine — OSV-Scanner + Trivy normalization.

Scans CycloneDX SBOMs for known vulnerabilities using two complementary
Apache-2.0 scanners:
  - OSV-Scanner (Google): ecosystem packages (npm, PyPI, Go, Cargo, etc.)
  - Trivy (Aqua Security): OS/base-image layer (Debian, Alpine, RHEL, etc.)

Both replace Grype for license cleanliness (commercial product).
"""

from .normalize import NormalizedVulnerability, normalize_osv, normalize_trivy
from .scanner import scan_sbom_osv, scan_sbom_trivy

__all__ = [
    "NormalizedVulnerability",
    "normalize_osv",
    "normalize_trivy",
    "scan_sbom_osv",
    "scan_sbom_trivy",
]
