"""Unit tests for sbom_parser.py — CycloneDX JSON parsing into dependency rows.

Tests cover:
- Correct purl extraction (ecosystem, name, version)
- Resolution source detection from Syft foundBy metadata
- Handling of components without purls (skipped)
- Source vs image SBOM mode
- Invalid/malformed input handling
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Add ingestion scripts to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "images" / "ingestion"))

from sbom_parser import (
    _determine_resolution_source,
    _parse_ecosystem_from_purl,
    _parse_name_from_purl,
    _parse_version_from_purl,
    parse_cyclonedx,
)


# ---------------------------------------------------------------------------
# Purl parsing tests
# ---------------------------------------------------------------------------


class TestPurlParsing:
    """Test Package URL field extraction."""

    def test_ecosystem_pypi(self):
        assert _parse_ecosystem_from_purl("pkg:pypi/requests@2.31.0") == "pypi"

    def test_ecosystem_npm(self):
        assert _parse_ecosystem_from_purl("pkg:npm/express@4.18.2") == "npm"

    def test_ecosystem_npm_scoped(self):
        assert _parse_ecosystem_from_purl("pkg:npm/%40angular/core@16.0.0") == "npm"

    def test_ecosystem_deb(self):
        assert _parse_ecosystem_from_purl("pkg:deb/debian/openssl@3.0.11-1") == "deb"

    def test_ecosystem_golang(self):
        assert _parse_ecosystem_from_purl("pkg:golang/github.com/gin-gonic/gin@1.9.1") == "golang"

    def test_ecosystem_unknown_for_empty(self):
        assert _parse_ecosystem_from_purl("") == "unknown"
        assert _parse_ecosystem_from_purl("not-a-purl") == "unknown"

    def test_name_simple(self):
        assert _parse_name_from_purl("pkg:pypi/requests@2.31.0") == "requests"

    def test_name_namespaced(self):
        assert _parse_name_from_purl("pkg:npm/%40angular/core@16.0.0") == "core"

    def test_name_golang(self):
        assert _parse_name_from_purl("pkg:golang/github.com/gin-gonic/gin@1.9.1") == "gin"

    def test_name_deb_with_namespace(self):
        assert _parse_name_from_purl("pkg:deb/debian/openssl@3.0.11") == "openssl"

    def test_version_present(self):
        assert _parse_version_from_purl("pkg:pypi/requests@2.31.0") == "2.31.0"

    def test_version_absent(self):
        assert _parse_version_from_purl("pkg:pypi/requests") is None

    def test_version_with_qualifiers(self):
        assert _parse_version_from_purl("pkg:pypi/requests@2.31.0?vcs_url=git") == "2.31.0"

    def test_version_with_subpath(self):
        assert _parse_version_from_purl("pkg:npm/foo@1.0.0#lib/bar") == "1.0.0"


# ---------------------------------------------------------------------------
# Resolution source detection tests
# ---------------------------------------------------------------------------


class TestResolutionSource:
    """Test Syft foundBy -> resolution source mapping."""

    def test_lockfile_pip(self):
        assert _determine_resolution_source("python-pip-requirements-lock-cataloger") == "lockfile"

    def test_lockfile_npm(self):
        assert _determine_resolution_source("javascript-lock-cataloger") == "lockfile"

    def test_manifest_pip(self):
        assert _determine_resolution_source("python-pip-requirements-cataloger") == "manifest"

    def test_manifest_setup(self):
        assert _determine_resolution_source("python-setup.py-cataloger") == "manifest"

    def test_os_package_dpkg(self):
        assert _determine_resolution_source("dpkg-db-cataloger") == "os-package"

    def test_os_package_apk(self):
        assert _determine_resolution_source("apk-db-cataloger") == "os-package"

    def test_os_package_rpm(self):
        assert _determine_resolution_source("rpm-db-cataloger") == "os-package"

    def test_binary(self):
        assert _determine_resolution_source("binary-classifier-cataloger") == "binary"

    def test_none_for_unknown(self):
        assert _determine_resolution_source("some-unknown-cataloger") is None

    def test_none_for_empty(self):
        assert _determine_resolution_source(None) is None
        assert _determine_resolution_source("") is None


# ---------------------------------------------------------------------------
# CycloneDX parsing tests
# ---------------------------------------------------------------------------


SAMPLE_SBOM = {
    "bomFormat": "CycloneDX",
    "specVersion": "1.6",
    "version": 1,
    "metadata": {"component": {"type": "application", "name": "test-repo", "version": "0.1.0"}},
    "components": [
        {
            "type": "library",
            "name": "requests",
            "version": "2.31.0",
            "purl": "pkg:pypi/requests@2.31.0",
            "properties": [
                {
                    "name": "syft:package:foundBy",
                    "value": "python-pip-requirements-lock-cataloger",
                }
            ],
        },
        {
            "type": "library",
            "name": "express",
            "version": "4.18.2",
            "purl": "pkg:npm/express@4.18.2",
            "properties": [{"name": "syft:package:foundBy", "value": "javascript-lock-cataloger"}],
        },
        {
            "type": "library",
            "name": "openssl",
            "version": "3.0.11-1",
            "purl": "pkg:deb/debian/openssl@3.0.11-1",
            "properties": [{"name": "syft:package:foundBy", "value": "dpkg-db-cataloger"}],
        },
        {
            "type": "library",
            "name": "no-purl-component",
            "version": "1.0.0",
            # No purl — should be skipped
        },
    ],
}


class TestParseCyclonedx:
    """Test full CycloneDX parsing pipeline."""

    def test_parse_source_sbom(self):
        records = parse_cyclonedx(SAMPLE_SBOM, source="code")
        # no-purl-component should be skipped
        assert len(records) == 3

    def test_source_field_set(self):
        records = parse_cyclonedx(SAMPLE_SBOM, source="code")
        for r in records:
            assert r.source == "code"

    def test_image_source_with_metadata(self):
        records = parse_cyclonedx(
            SAMPLE_SBOM,
            source="image",
            dockerfile_path="Dockerfile",
            base_image="python:3.13-slim",
        )
        for r in records:
            assert r.source == "image"
            assert r.dockerfile_path == "Dockerfile"
            assert r.base_image == "python:3.13-slim"

    def test_purl_extraction(self):
        records = parse_cyclonedx(SAMPLE_SBOM, source="code")
        by_name = {r.package_name: r for r in records}
        assert by_name["requests"].package_url == "pkg:pypi/requests@2.31.0"
        assert by_name["requests"].package_ecosystem == "pypi"
        assert by_name["requests"].package_version == "2.31.0"

    def test_resolution_source_lockfile(self):
        records = parse_cyclonedx(SAMPLE_SBOM, source="code")
        by_name = {r.package_name: r for r in records}
        assert by_name["requests"].resolution_source == "lockfile"
        assert by_name["express"].resolution_source == "lockfile"

    def test_resolution_source_os_package(self):
        records = parse_cyclonedx(SAMPLE_SBOM, source="code")
        by_name = {r.package_name: r for r in records}
        assert by_name["openssl"].resolution_source == "os-package"

    def test_component_type_preserved(self):
        records = parse_cyclonedx(SAMPLE_SBOM, source="code")
        for r in records:
            assert r.component_type == "library"

    def test_json_string_input(self):
        records = parse_cyclonedx(json.dumps(SAMPLE_SBOM), source="code")
        assert len(records) == 3

    def test_invalid_json(self):
        records = parse_cyclonedx("not valid json", source="code")
        assert records == []

    def test_non_cyclonedx_document(self):
        records = parse_cyclonedx({"bomFormat": "SPDX", "components": []}, source="code")
        assert records == []

    def test_empty_components(self):
        sbom = {"bomFormat": "CycloneDX", "specVersion": "1.6", "components": []}
        records = parse_cyclonedx(sbom, source="code")
        assert records == []

    def test_fixture_sbom(self, fixtures_dir):
        """Parse the existing test fixture SBOM."""
        fixture_path = fixtures_dir / "planted-vuln.cdx.json"
        if fixture_path.exists():
            with open(fixture_path) as f:
                sbom = json.load(f)
            records = parse_cyclonedx(sbom, source="code")
            assert len(records) == 4
            purls = {r.package_url for r in records}
            assert "pkg:pypi/requests@2.25.0" in purls
            assert "pkg:pypi/flask@3.0.0" in purls
