"""Unit tests for scan-vulns.py — vulnerability scan CronJob logic.

Tests:
- Deduplication by cve_id (keeps highest severity)
- Upsert SQL generation (ON CONFLICT behavior)
- Repo extraction from S3 keys
- Empty SBOM list handling
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add the ingestion scripts to the path for import
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "images" / "ingestion"))


# ---------------------------------------------------------------------------
# Import the module under test (scan-vulns.py has a hyphen — use importlib)
# ---------------------------------------------------------------------------

import importlib.util

_spec = importlib.util.spec_from_file_location(
    "scan_vulns",
    Path(__file__).parent.parent.parent / "images" / "ingestion" / "scan-vulns.py",
)
scan_vulns = importlib.util.module_from_spec(_spec)

# Patch settings before loading the module to avoid pydantic validation
with patch.dict("sys.modules", {"config": MagicMock()}):
    _spec.loader.exec_module(scan_vulns)


# ---------------------------------------------------------------------------
# Fixture: normalized vulnerability findings
# ---------------------------------------------------------------------------


class FakeNormalizedVuln:
    """Mimics NormalizedVulnerability dataclass for testing."""

    def __init__(
        self,
        cve_id: str,
        package_name: str = "lodash",
        package_ecosystem: str = "npm",
        affected_versions: str = ">=0,<4.17.21",
        fixed_version: str | None = "4.17.21",
        severity: str = "HIGH",
        cvss_score: float | None = 7.2,
        summary: str = "Prototype Pollution",
        source_scanner: str = "osv-scanner",
        source_sbom_type: str = "source",
        raw_id: str = "",
        aliases: list | None = None,
        detected_at: datetime | None = None,
    ):
        self.cve_id = cve_id
        self.package_name = package_name
        self.package_ecosystem = package_ecosystem
        self.affected_versions = affected_versions
        self.fixed_version = fixed_version
        self.severity = severity
        self.cvss_score = cvss_score
        self.summary = summary
        self.source_scanner = source_scanner
        self.source_sbom_type = source_sbom_type
        self.raw_id = raw_id or cve_id
        self.aliases = aliases or []
        self.detected_at = detected_at or datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Tests: deduplicate_findings
# ---------------------------------------------------------------------------


class TestDeduplicateFindings:
    """Tests for the deduplication logic."""

    def test_no_duplicates_passes_through(self):
        """Non-duplicate findings are all kept."""
        findings = [
            FakeNormalizedVuln("CVE-2023-0001", severity="HIGH"),
            FakeNormalizedVuln("CVE-2023-0002", severity="MEDIUM"),
            FakeNormalizedVuln("CVE-2023-0003", severity="LOW"),
        ]
        result = scan_vulns.deduplicate_findings(findings)
        assert len(result) == 3

    def test_duplicate_keeps_highest_severity(self):
        """When same CVE appears twice, keep the higher severity."""
        findings = [
            FakeNormalizedVuln("CVE-2023-0001", severity="MEDIUM"),
            FakeNormalizedVuln("CVE-2023-0001", severity="CRITICAL"),
        ]
        result = scan_vulns.deduplicate_findings(findings)
        assert len(result) == 1
        assert result[0].severity == "CRITICAL"

    def test_duplicate_same_severity_keeps_first(self):
        """When same CVE at same severity, keeps first encountered."""
        findings = [
            FakeNormalizedVuln("CVE-2023-0001", severity="HIGH", source_scanner="osv-scanner"),
            FakeNormalizedVuln("CVE-2023-0001", severity="HIGH", source_scanner="trivy"),
        ]
        result = scan_vulns.deduplicate_findings(findings)
        assert len(result) == 1
        assert result[0].source_scanner == "osv-scanner"

    def test_empty_list(self):
        """Empty input returns empty output."""
        result = scan_vulns.deduplicate_findings([])
        assert result == []

    def test_multiple_duplicates_mixed(self):
        """Multiple CVEs with duplicates are all deduped correctly."""
        findings = [
            FakeNormalizedVuln("CVE-2023-0001", severity="LOW"),
            FakeNormalizedVuln("CVE-2023-0002", severity="HIGH"),
            FakeNormalizedVuln("CVE-2023-0001", severity="HIGH"),
            FakeNormalizedVuln("CVE-2023-0002", severity="CRITICAL"),
            FakeNormalizedVuln("CVE-2023-0003", severity="MEDIUM"),
        ]
        result = scan_vulns.deduplicate_findings(findings)
        assert len(result) == 3
        by_cve = {f.cve_id: f for f in result}
        assert by_cve["CVE-2023-0001"].severity == "HIGH"
        assert by_cve["CVE-2023-0002"].severity == "CRITICAL"
        assert by_cve["CVE-2023-0003"].severity == "MEDIUM"


# ---------------------------------------------------------------------------
# Tests: _extract_repo_from_key
# ---------------------------------------------------------------------------


class TestExtractRepoFromKey:
    """Tests for S3 key → org/repo extraction."""

    def test_standard_key(self):
        """Standard SBOM S3 key extracts org/repo correctly."""
        key = "sbom/repos/my-org/my-repo/source.cdx.json"
        assert scan_vulns._extract_repo_from_key(key) == "my-org/my-repo"

    def test_nested_prefix(self):
        """Prefixed SBOM key still works."""
        key = "tenants/acme/sbom/repos/org/repo/source.cdx.json"
        assert scan_vulns._extract_repo_from_key(key) == "org/repo"

    def test_no_repos_segment(self):
        """Key without 'repos' segment returns the full key."""
        key = "some/other/path.cdx.json"
        assert scan_vulns._extract_repo_from_key(key) == key

    def test_key_with_extra_segments(self):
        """Key with extra path segments after repo still works."""
        key = "sbom/repos/owner/project/images/source.cdx.json"
        assert scan_vulns._extract_repo_from_key(key) == "owner/project"


# ---------------------------------------------------------------------------
# Tests: upsert_vulnerabilities (mock DB)
# ---------------------------------------------------------------------------


class TestUpsertVulnerabilities:
    """Tests for the database upsert logic."""

    def test_empty_findings_returns_zero(self):
        """No findings = no DB interaction."""
        conn = MagicMock()
        result = scan_vulns.upsert_vulnerabilities(conn, [])
        assert result == 0
        conn.cursor.assert_not_called()

    def test_single_finding_upserts(self):
        """A single finding is upserted successfully."""
        conn = MagicMock()
        cursor = MagicMock()
        conn.cursor.return_value = cursor

        findings = [FakeNormalizedVuln("CVE-2023-0001")]
        result = scan_vulns.upsert_vulnerabilities(conn, findings)

        assert result == 1
        cursor.execute.assert_called_once()
        conn.commit.assert_called_once()
        cursor.close.assert_called_once()

    def test_multiple_findings_upsert(self):
        """Multiple findings are all upserted."""
        conn = MagicMock()
        cursor = MagicMock()
        conn.cursor.return_value = cursor

        findings = [
            FakeNormalizedVuln("CVE-2023-0001"),
            FakeNormalizedVuln("CVE-2023-0002"),
            FakeNormalizedVuln("CVE-2023-0003"),
        ]
        result = scan_vulns.upsert_vulnerabilities(conn, findings)

        assert result == 3
        assert cursor.execute.call_count == 3
        conn.commit.assert_called_once()

    def test_db_error_rolls_back(self):
        """On DB error, transaction is rolled back."""
        conn = MagicMock()
        cursor = MagicMock()
        conn.cursor.return_value = cursor
        cursor.execute.side_effect = Exception("DB connection lost")

        findings = [FakeNormalizedVuln("CVE-2023-0001")]

        with pytest.raises(Exception, match="DB connection lost"):
            scan_vulns.upsert_vulnerabilities(conn, findings)

        conn.rollback.assert_called_once()
        cursor.close.assert_called_once()

    def test_package_purl_format(self):
        """Upserted package column uses purl format."""
        conn = MagicMock()
        cursor = MagicMock()
        conn.cursor.return_value = cursor

        findings = [
            FakeNormalizedVuln("CVE-2023-0001", package_name="requests", package_ecosystem="pypi")
        ]
        scan_vulns.upsert_vulnerabilities(conn, findings)

        # Check the SQL parameters — package should be "pkg:pypi/requests"
        call_args = cursor.execute.call_args[0]
        params = call_args[1]
        assert params[2] == "pkg:pypi/requests"  # package column (index 2)


# ---------------------------------------------------------------------------
# Tests: list_sbom_keys
# ---------------------------------------------------------------------------


class TestListSbomKeys:
    """Tests for S3 SBOM key listing."""

    def test_filters_cdx_json_only(self):
        """Only .cdx.json files are returned."""
        s3_client = MagicMock()
        paginator = MagicMock()
        s3_client.get_paginator.return_value = paginator
        paginator.paginate.return_value = [
            {
                "Contents": [
                    {"Key": "sbom/repos/org/repo/source.cdx.json"},
                    {"Key": "sbom/repos/org/repo/metadata.json"},
                    {"Key": "sbom/repos/org/other/source.cdx.json"},
                ]
            }
        ]

        keys = scan_vulns.list_sbom_keys(s3_client, "my-bucket", "sbom/repos/")
        assert len(keys) == 2
        assert all(k.endswith(".cdx.json") for k in keys)

    def test_empty_bucket_returns_empty(self):
        """Empty S3 prefix returns empty list."""
        s3_client = MagicMock()
        paginator = MagicMock()
        s3_client.get_paginator.return_value = paginator
        paginator.paginate.return_value = [{"Contents": []}]

        keys = scan_vulns.list_sbom_keys(s3_client, "my-bucket", "sbom/repos/")
        assert keys == []

    def test_no_contents_key(self):
        """Page without Contents key doesn't crash."""
        s3_client = MagicMock()
        paginator = MagicMock()
        s3_client.get_paginator.return_value = paginator
        paginator.paginate.return_value = [{}]

        keys = scan_vulns.list_sbom_keys(s3_client, "my-bucket", "sbom/repos/")
        assert keys == []
