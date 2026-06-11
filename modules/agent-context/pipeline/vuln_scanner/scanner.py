"""Scanner orchestration — invoke OSV-Scanner and Trivy against SBOMs.

This module wraps the CLI invocations and returns parsed JSON output.
It's used by the CodeBuild buildspec (via subprocess) and by the
indexing worker (for per-repo on-ingest scanning).
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

from .normalize import NormalizedVulnerability, normalize_osv, normalize_trivy

log = logging.getLogger(__name__)


class ScanError(Exception):
    """Raised when a scanner invocation fails non-recoverably."""


def scan_sbom_osv(
    sbom_path: str | Path,
    sbom_type: str = "source",
    timeout_seconds: int = 300,
) -> list[NormalizedVulnerability]:
    """Run OSV-Scanner against a CycloneDX SBOM and return normalized findings.

    Args:
        sbom_path: Path to the .cdx.json file (CycloneDX JSON format).
        sbom_type: "source" or "image" — recorded in each finding.
        timeout_seconds: Maximum time for the scanner process.

    Returns:
        List of normalized vulnerability findings.

    Raises:
        ScanError: If OSV-Scanner fails to execute (not if it finds no vulns).
    """
    sbom_path = Path(sbom_path)
    if not sbom_path.exists():
        raise ScanError(f"SBOM file not found: {sbom_path}")

    cmd = [
        "osv-scanner",
        "scan",
        "source",
        "-L",
        str(sbom_path),
        "--format",
        "json",
    ]

    log.info("Running OSV-Scanner: %s", " ".join(cmd))

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        raise ScanError(f"OSV-Scanner timed out after {timeout_seconds}s on {sbom_path}")
    except FileNotFoundError:
        raise ScanError("osv-scanner binary not found in PATH")

    # OSV-Scanner exits 0 for no vulns, 1 for vulns found, >1 for errors
    if result.returncode > 1:
        raise ScanError(f"OSV-Scanner failed (rc={result.returncode}): {result.stderr[:500]}")

    if not result.stdout.strip():
        log.info("OSV-Scanner: no findings for %s", sbom_path.name)
        return []

    try:
        osv_json = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise ScanError(f"OSV-Scanner produced invalid JSON: {e}")

    findings = normalize_osv(osv_json, sbom_type=sbom_type)
    log.info(
        "OSV-Scanner: %d findings for %s",
        len(findings),
        sbom_path.name,
    )
    return findings


def scan_sbom_trivy(
    sbom_path: str | Path,
    sbom_type: str = "image",
    timeout_seconds: int = 300,
) -> list[NormalizedVulnerability]:
    """Run Trivy against a CycloneDX SBOM and return normalized findings.

    Args:
        sbom_path: Path to the .cdx.json file (CycloneDX JSON format).
        sbom_type: "source" or "image" — recorded in each finding.
        timeout_seconds: Maximum time for the scanner process.

    Returns:
        List of normalized vulnerability findings.

    Raises:
        ScanError: If Trivy fails to execute.
    """
    sbom_path = Path(sbom_path)
    if not sbom_path.exists():
        raise ScanError(f"SBOM file not found: {sbom_path}")

    cmd = [
        "trivy",
        "sbom",
        str(sbom_path),
        "--format",
        "json",
        "--scanners",
        "vuln",
    ]

    log.info("Running Trivy: %s", " ".join(cmd))

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        raise ScanError(f"Trivy timed out after {timeout_seconds}s on {sbom_path}")
    except FileNotFoundError:
        raise ScanError("trivy binary not found in PATH")

    # Trivy exits 0 on success (even with findings)
    if result.returncode != 0:
        raise ScanError(f"Trivy failed (rc={result.returncode}): {result.stderr[:500]}")

    if not result.stdout.strip():
        log.info("Trivy: no output for %s", sbom_path.name)
        return []

    try:
        trivy_json = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise ScanError(f"Trivy produced invalid JSON: {e}")

    findings = normalize_trivy(trivy_json, sbom_type=sbom_type)
    log.info("Trivy: %d findings for %s", len(findings), sbom_path.name)
    return findings


def scan_image_trivy(
    image_ref: str,
    sbom_type: str = "image",
    timeout_seconds: int = 600,
) -> list[NormalizedVulnerability]:
    """Run Trivy directly against a container image.

    Use this when scanning built images (not SBOMs). Trivy handles
    pulling/inspecting the image and scanning all layers.

    Args:
        image_ref: Docker image reference (e.g., "myapp:latest" or
                   "123456789.dkr.ecr.us-east-1.amazonaws.com/app:sha").
        sbom_type: Always "image" for this path.
        timeout_seconds: Maximum time (image pull + scan).

    Returns:
        List of normalized vulnerability findings.
    """
    cmd = [
        "trivy",
        "image",
        "--format",
        "json",
        "--scanners",
        "vuln",
        image_ref,
    ]

    log.info("Running Trivy image scan: %s", image_ref)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        raise ScanError(f"Trivy image scan timed out after {timeout_seconds}s: {image_ref}")
    except FileNotFoundError:
        raise ScanError("trivy binary not found in PATH")

    if result.returncode != 0:
        raise ScanError(f"Trivy image scan failed (rc={result.returncode}): {result.stderr[:500]}")

    if not result.stdout.strip():
        return []

    try:
        trivy_json = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise ScanError(f"Trivy produced invalid JSON: {e}")

    return normalize_trivy(trivy_json, sbom_type=sbom_type)
