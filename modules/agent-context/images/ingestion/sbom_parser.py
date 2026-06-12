"""Parse CycloneDX JSON SBOMs into dependency rows for the Postgres reverse-index.

Shared by both Rail 1 (source SBOM) and Rail 2 (image SBOM). Extracts package
URL (purl), ecosystem, version, and resolution source from Syft's CycloneDX output.

Design ref: docs/design-notes/1358-dual-rail-sbom-generation.md (section 9)
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("sbom-parser")


@dataclass
class DependencyRecord:
    """A single dependency extracted from an SBOM, ready for Postgres upsert."""

    package_url: str  # Full purl, e.g. "pkg:pypi/requests@2.31.0"
    package_name: str  # e.g. "requests"
    package_version: str | None  # e.g. "2.31.0" or None if unresolved
    package_ecosystem: str  # e.g. "pypi", "npm", "golang", "deb"
    source: str  # "code" (from source SBOM) or "image" (from image SBOM)
    resolution_source: str | None  # "lockfile" | "manifest" | "os-package" | "binary"
    is_transitive: bool
    component_type: str  # "library" | "framework" | "application" | "operating-system"
    dockerfile_path: str | None = None  # Only for image SBOMs
    base_image: str | None = None  # Only for image SBOMs


# ---------------------------------------------------------------------------
# Resolution source detection (from Syft's foundBy metadata)
# ---------------------------------------------------------------------------

_LOCKFILE_PATTERNS = re.compile(r"lock|locked", re.IGNORECASE)
_MANIFEST_PATTERNS = re.compile(r"manifest|requirements|setup\.py|pyproject", re.IGNORECASE)
_OS_PACKAGE_PATTERNS = re.compile(r"dpkg|apk|rpm|pacman", re.IGNORECASE)
_BINARY_PATTERNS = re.compile(r"binary", re.IGNORECASE)


def _determine_resolution_source(found_by: str | None) -> str | None:
    """Map Syft's foundBy cataloger name to a resolution source category."""
    if not found_by:
        return None
    if _LOCKFILE_PATTERNS.search(found_by):
        return "lockfile"
    if _OS_PACKAGE_PATTERNS.search(found_by):
        return "os-package"
    if _BINARY_PATTERNS.search(found_by):
        return "binary"
    if _MANIFEST_PATTERNS.search(found_by):
        return "manifest"
    return None


# ---------------------------------------------------------------------------
# Purl parsing
# ---------------------------------------------------------------------------


def _parse_ecosystem_from_purl(purl: str) -> str:
    """Extract the ecosystem (type) from a Package URL.

    purl format: pkg:<type>/<namespace>/<name>@<version>?<qualifiers>#<subpath>
    Examples:
      pkg:pypi/requests@2.31.0 -> "pypi"
      pkg:npm/%40angular/core@16.0.0 -> "npm"
      pkg:deb/debian/openssl@3.0.11 -> "deb"
    """
    if not purl or not purl.startswith("pkg:"):
        return "unknown"
    # Strip "pkg:" prefix, take everything up to the first "/"
    remainder = purl[4:]
    slash_idx = remainder.find("/")
    if slash_idx == -1:
        return remainder.split("@")[0] if "@" in remainder else remainder
    return remainder[:slash_idx]


def _parse_name_from_purl(purl: str) -> str:
    """Extract the package name from a Package URL.

    Handles namespaced purls: pkg:npm/%40angular/core@16.0.0 -> "core"
    Non-namespaced: pkg:pypi/requests@2.31.0 -> "requests"
    """
    if not purl or not purl.startswith("pkg:"):
        return ""
    remainder = purl[4:]  # Strip "pkg:"
    # Remove qualifiers and subpath
    remainder = remainder.split("?")[0].split("#")[0]
    # Remove version
    remainder = remainder.split("@")[0]
    # The name is the last path segment
    parts = remainder.split("/")
    return parts[-1] if parts else ""


def _parse_version_from_purl(purl: str) -> str | None:
    """Extract version from a purl. Returns None if no version specified."""
    if not purl or "@" not in purl:
        return None
    # Version is after @ but before ? or #
    after_at = purl.split("@", 1)[1]
    version = after_at.split("?")[0].split("#")[0]
    return version if version else None


# ---------------------------------------------------------------------------
# CycloneDX component extraction
# ---------------------------------------------------------------------------


def _get_syft_property(component: dict[str, Any], prop_name: str) -> str | None:
    """Extract a Syft property from a CycloneDX component's properties array."""
    for prop in component.get("properties", []):
        if prop.get("name") == prop_name:
            return prop.get("value")
    return None


def parse_cyclonedx(
    sbom_json: str | dict,
    source: str = "code",
    dockerfile_path: str | None = None,
    base_image: str | None = None,
) -> list[DependencyRecord]:
    """Parse a CycloneDX JSON SBOM into DependencyRecord instances.

    Args:
        sbom_json: CycloneDX JSON as a string or already-parsed dict.
        source: "code" for source SBOMs, "image" for image SBOMs.
        dockerfile_path: Path to the Dockerfile (image SBOMs only).
        base_image: Base image FROM line (image SBOMs only).

    Returns:
        List of DependencyRecord instances ready for DB upsert.
    """
    if isinstance(sbom_json, str):
        try:
            data = json.loads(sbom_json)
        except json.JSONDecodeError as e:
            log.error("Failed to parse CycloneDX JSON: %s", e)
            return []
    else:
        data = sbom_json

    # Validate it's a CycloneDX document
    if data.get("bomFormat") != "CycloneDX":
        log.warning("Not a CycloneDX document (bomFormat=%s)", data.get("bomFormat"))
        return []

    components = data.get("components", [])
    records: list[DependencyRecord] = []

    for comp in components:
        purl = comp.get("purl", "")
        name = comp.get("name", "")
        version = comp.get("version")

        # Skip components without a purl — we can't index them reliably
        if not purl:
            # Try to construct a minimal purl from type + name + version
            comp_type = comp.get("type", "library")
            if name and comp_type:
                log.debug("Component without purl: %s (skipped)", name)
            continue

        # Extract fields from purl (authoritative) with fallback to component fields
        ecosystem = _parse_ecosystem_from_purl(purl)
        pkg_name = _parse_name_from_purl(purl) or name
        pkg_version = _parse_version_from_purl(purl) or version

        # Determine resolution source from Syft metadata
        found_by = _get_syft_property(comp, "syft:package:foundBy")
        resolution_source = _determine_resolution_source(found_by)

        # Determine transitivity — lockfile-resolved deps are potentially transitive
        # Syft doesn't flag transitivity directly; default to False
        is_transitive = False

        component_type = comp.get("type", "library")

        records.append(
            DependencyRecord(
                package_url=purl,
                package_name=pkg_name,
                package_version=pkg_version,
                package_ecosystem=ecosystem,
                source=source,
                resolution_source=resolution_source,
                is_transitive=is_transitive,
                component_type=component_type,
                dockerfile_path=dockerfile_path,
                base_image=base_image,
            )
        )

    log.info(
        "Parsed %d dependencies from CycloneDX SBOM (source=%s)",
        len(records),
        source,
    )
    return records
