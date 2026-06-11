"""Unit tests for vulnerability scanner normalization.

Tests that OSV-Scanner and Trivy JSON outputs are correctly normalized into
the unified NormalizedVulnerability schema.
"""

from __future__ import annotations

from datetime import datetime, timezone

from pipeline.vuln_scanner.normalize import (
    NormalizedVulnerability,
    normalize_osv,
    normalize_trivy,
    _cvss_to_severity,
    _extract_fixed_version_osv,
    _extract_affected_range_osv,
    _pick_cve_id,
    _trivy_target_to_ecosystem,
)


# ---------------------------------------------------------------------------
# Fixtures: realistic scanner output
# ---------------------------------------------------------------------------

OSV_SINGLE_VULN = {
    "results": [
        {
            "source": {"path": "/sboms/repo-a.cdx.json", "type": "sbom"},
            "packages": [
                {
                    "package": {
                        "name": "lodash",
                        "version": "4.17.20",
                        "ecosystem": "npm",
                    },
                    "vulnerabilities": [
                        {
                            "id": "GHSA-jf85-cpcp-j695",
                            "aliases": ["CVE-2021-23337"],
                            "summary": "Command Injection in lodash",
                            "severity": [{"type": "CVSS_V3", "score": "7.2"}],
                            "affected": [
                                {
                                    "ranges": [
                                        {
                                            "type": "SEMVER",
                                            "events": [
                                                {"introduced": "0"},
                                                {"fixed": "4.17.21"},
                                            ],
                                        }
                                    ]
                                }
                            ],
                            "database_specific": {"severity": "HIGH"},
                        }
                    ],
                    "groups": [{"ids": ["GHSA-jf85-cpcp-j695", "CVE-2021-23337"]}],
                }
            ],
        }
    ]
}

OSV_MULTIPLE_VULNS = {
    "results": [
        {
            "source": {"path": "/sboms/repo-b.cdx.json", "type": "sbom"},
            "packages": [
                {
                    "package": {
                        "name": "requests",
                        "version": "2.25.0",
                        "ecosystem": "PyPI",
                    },
                    "vulnerabilities": [
                        {
                            "id": "CVE-2023-32681",
                            "aliases": ["GHSA-j8r2-6x86-q33q"],
                            "summary": "Unintended leak of Proxy-Authorization header",
                            "severity": [{"type": "CVSS_V3", "score": "6.1"}],
                            "affected": [
                                {
                                    "ranges": [
                                        {
                                            "type": "SEMVER",
                                            "events": [
                                                {"introduced": "2.3.0"},
                                                {"fixed": "2.31.0"},
                                            ],
                                        }
                                    ]
                                }
                            ],
                            "database_specific": {"severity": "MEDIUM"},
                        }
                    ],
                    "groups": [{"ids": ["CVE-2023-32681", "GHSA-j8r2-6x86-q33q"]}],
                },
                {
                    "package": {
                        "name": "urllib3",
                        "version": "1.26.5",
                        "ecosystem": "PyPI",
                    },
                    "vulnerabilities": [
                        {
                            "id": "GHSA-v845-jxx5-vc9f",
                            "aliases": [],
                            "summary": "urllib3 cookie header leak on redirect",
                            "severity": [],
                            "affected": [
                                {
                                    "ranges": [
                                        {
                                            "type": "SEMVER",
                                            "events": [
                                                {"introduced": "0"},
                                                {"fixed": "1.26.17"},
                                            ],
                                        }
                                    ]
                                }
                            ],
                            "database_specific": {},
                        }
                    ],
                    "groups": [{"ids": ["GHSA-v845-jxx5-vc9f"]}],
                },
            ],
        }
    ]
}

OSV_EMPTY = {"results": []}

TRIVY_OS_VULNS = {
    "Results": [
        {
            "Target": "debian 12.5",
            "Class": "os-pkgs",
            "Vulnerabilities": [
                {
                    "VulnerabilityID": "CVE-2024-2511",
                    "PkgName": "openssl",
                    "InstalledVersion": "3.0.11-1~deb12u2",
                    "FixedVersion": "3.0.13-1~deb12u1",
                    "Severity": "HIGH",
                    "Title": "Unbounded memory growth processing TLSv1.3 sessions",
                    "CVSS": {
                        "nvd": {"V3Score": 7.5},
                    },
                },
                {
                    "VulnerabilityID": "CVE-2024-0727",
                    "PkgName": "openssl",
                    "InstalledVersion": "3.0.11-1~deb12u2",
                    "FixedVersion": "3.0.13-1~deb12u1",
                    "Severity": "MEDIUM",
                    "Title": "NULL pointer dereference in PKCS12 parsing",
                    "CVSS": {
                        "nvd": {"V3Score": 5.5},
                        "redhat": {"V3Score": 5.3},
                    },
                },
            ],
        }
    ]
}

TRIVY_LANG_VULNS = {
    "Results": [
        {
            "Target": "Python",
            "Class": "lang-pkgs",
            "Vulnerabilities": [
                {
                    "VulnerabilityID": "CVE-2023-43804",
                    "PkgName": "urllib3",
                    "InstalledVersion": "1.26.5",
                    "FixedVersion": "1.26.18",
                    "Severity": "HIGH",
                    "Title": "Cookie header not stripped on cross-origin redirect",
                    "CVSS": {},
                },
            ],
        }
    ]
}

TRIVY_EMPTY = {"Results": []}

TRIVY_NO_FIX = {
    "Results": [
        {
            "Target": "alpine 3.19.1",
            "Class": "os-pkgs",
            "Vulnerabilities": [
                {
                    "VulnerabilityID": "CVE-2024-99999",
                    "PkgName": "musl",
                    "InstalledVersion": "1.2.4-r2",
                    "FixedVersion": "",
                    "Severity": "LOW",
                    "Title": "Hypothetical musl issue",
                    "CVSS": {},
                },
            ],
        }
    ]
}


# ---------------------------------------------------------------------------
# Tests: OSV-Scanner normalization
# ---------------------------------------------------------------------------


class TestNormalizeOsv:
    """Tests for normalize_osv()."""

    def test_single_vuln_basic_fields(self):
        results = normalize_osv(OSV_SINGLE_VULN)
        assert len(results) == 1

        v = results[0]
        assert v.cve_id == "CVE-2021-23337"  # Prefers CVE alias over GHSA
        assert v.package_name == "lodash"
        assert v.package_ecosystem == "npm"
        assert v.fixed_version == "4.17.21"
        assert v.severity == "HIGH"
        assert v.source_scanner == "osv-scanner"
        assert v.source_sbom_type == "source"
        assert v.raw_id == "GHSA-jf85-cpcp-j695"

    def test_single_vuln_aliases(self):
        results = normalize_osv(OSV_SINGLE_VULN)
        v = results[0]
        # CVE is the canonical ID, GHSA should be in aliases
        assert "GHSA-jf85-cpcp-j695" in v.aliases
        assert "CVE-2021-23337" not in v.aliases

    def test_single_vuln_affected_versions(self):
        results = normalize_osv(OSV_SINGLE_VULN)
        v = results[0]
        assert v.affected_versions == ">=0,<4.17.21"

    def test_multiple_packages(self):
        results = normalize_osv(OSV_MULTIPLE_VULNS)
        assert len(results) == 2

        names = {v.package_name for v in results}
        assert names == {"requests", "urllib3"}

    def test_multiple_vulns_severity(self):
        results = normalize_osv(OSV_MULTIPLE_VULNS)
        requests_vuln = next(v for v in results if v.package_name == "requests")
        urllib3_vuln = next(v for v in results if v.package_name == "urllib3")

        assert requests_vuln.severity == "MEDIUM"
        assert urllib3_vuln.severity == "UNKNOWN"  # No severity data

    def test_cve_preferred_over_ghsa(self):
        results = normalize_osv(OSV_MULTIPLE_VULNS)
        requests_vuln = next(v for v in results if v.package_name == "requests")
        assert requests_vuln.cve_id == "CVE-2023-32681"

    def test_ghsa_used_when_no_cve(self):
        results = normalize_osv(OSV_MULTIPLE_VULNS)
        urllib3_vuln = next(v for v in results if v.package_name == "urllib3")
        assert urllib3_vuln.cve_id == "GHSA-v845-jxx5-vc9f"

    def test_empty_results(self):
        results = normalize_osv(OSV_EMPTY)
        assert results == []

    def test_sbom_type_propagated(self):
        results = normalize_osv(OSV_SINGLE_VULN, sbom_type="image")
        assert results[0].source_sbom_type == "image"

    def test_detected_at_is_recent(self):
        results = normalize_osv(OSV_SINGLE_VULN)
        # Should be within the last few seconds
        delta = datetime.now(timezone.utc) - results[0].detected_at
        assert delta.total_seconds() < 5

    def test_ecosystem_lowercase(self):
        results = normalize_osv(OSV_MULTIPLE_VULNS)
        requests_vuln = next(v for v in results if v.package_name == "requests")
        assert requests_vuln.package_ecosystem == "pypi"  # Normalized to lowercase

    def test_cvss_score_extracted(self):
        results = normalize_osv(OSV_SINGLE_VULN)
        assert results[0].cvss_score == 7.2


# ---------------------------------------------------------------------------
# Tests: Trivy normalization
# ---------------------------------------------------------------------------


class TestNormalizeTrivy:
    """Tests for normalize_trivy()."""

    def test_os_vulns_basic_fields(self):
        results = normalize_trivy(TRIVY_OS_VULNS)
        assert len(results) == 2

        v = results[0]
        assert v.cve_id == "CVE-2024-2511"
        assert v.package_name == "openssl"
        assert v.package_ecosystem == "debian:12"
        assert v.fixed_version == "3.0.13-1~deb12u1"
        assert v.severity == "HIGH"
        assert v.source_scanner == "trivy"
        assert v.source_sbom_type == "image"

    def test_os_vuln_cvss_score(self):
        results = normalize_trivy(TRIVY_OS_VULNS)
        assert results[0].cvss_score == 7.5
        assert results[1].cvss_score == 5.5  # NVD preferred over Red Hat

    def test_lang_vulns_ecosystem(self):
        results = normalize_trivy(TRIVY_LANG_VULNS)
        assert len(results) == 1
        assert results[0].package_ecosystem == "pypi"

    def test_empty_results(self):
        results = normalize_trivy(TRIVY_EMPTY)
        assert results == []

    def test_no_fix_version(self):
        results = normalize_trivy(TRIVY_NO_FIX)
        assert len(results) == 1
        assert results[0].fixed_version is None  # Empty string → None

    def test_alpine_ecosystem(self):
        results = normalize_trivy(TRIVY_NO_FIX)
        assert results[0].package_ecosystem == "alpine:3"

    def test_sbom_type_override(self):
        results = normalize_trivy(TRIVY_OS_VULNS, sbom_type="source")
        assert all(v.source_sbom_type == "source" for v in results)

    def test_installed_version_as_affected(self):
        results = normalize_trivy(TRIVY_OS_VULNS)
        # Trivy records the installed (affected) version directly
        assert results[0].affected_versions == "3.0.11-1~deb12u2"

    def test_severity_labels_normalized(self):
        results = normalize_trivy(TRIVY_OS_VULNS)
        assert results[0].severity == "HIGH"
        assert results[1].severity == "MEDIUM"


# ---------------------------------------------------------------------------
# Tests: helper functions
# ---------------------------------------------------------------------------


class TestHelpers:
    """Tests for internal helper functions."""

    def test_cvss_to_severity_critical(self):
        assert _cvss_to_severity(9.8) == "CRITICAL"
        assert _cvss_to_severity(9.0) == "CRITICAL"

    def test_cvss_to_severity_high(self):
        assert _cvss_to_severity(7.0) == "HIGH"
        assert _cvss_to_severity(8.9) == "HIGH"

    def test_cvss_to_severity_medium(self):
        assert _cvss_to_severity(4.0) == "MEDIUM"
        assert _cvss_to_severity(6.9) == "MEDIUM"

    def test_cvss_to_severity_low(self):
        assert _cvss_to_severity(0.1) == "LOW"
        assert _cvss_to_severity(3.9) == "LOW"

    def test_cvss_to_severity_none(self):
        assert _cvss_to_severity(None) == "UNKNOWN"

    def test_pick_cve_id_prefers_cve(self):
        assert _pick_cve_id("GHSA-xxxx", ["CVE-2024-1234"]) == "CVE-2024-1234"

    def test_pick_cve_id_uses_original_when_no_cve(self):
        assert _pick_cve_id("GHSA-xxxx", ["OSV-2024-001"]) == "GHSA-xxxx"

    def test_pick_cve_id_already_cve(self):
        assert _pick_cve_id("CVE-2024-5678", ["GHSA-yyyy"]) == "CVE-2024-5678"

    def test_extract_fixed_version_found(self):
        affected = [
            {"ranges": [{"type": "SEMVER", "events": [{"introduced": "0"}, {"fixed": "1.2.3"}]}]}
        ]
        assert _extract_fixed_version_osv(affected) == "1.2.3"

    def test_extract_fixed_version_not_found(self):
        affected = [{"ranges": [{"type": "SEMVER", "events": [{"introduced": "0"}]}]}]
        assert _extract_fixed_version_osv(affected) is None

    def test_extract_affected_range(self):
        affected = [
            {"ranges": [{"type": "SEMVER", "events": [{"introduced": "1.0"}, {"fixed": "2.0"}]}]}
        ]
        assert _extract_affected_range_osv(affected) == ">=1.0,<2.0"

    def test_extract_affected_range_no_fix(self):
        affected = [{"ranges": [{"type": "SEMVER", "events": [{"introduced": "1.0"}]}]}]
        assert _extract_affected_range_osv(affected) == ">=1.0"

    def test_trivy_target_debian(self):
        assert _trivy_target_to_ecosystem("debian 12.5", "os-pkgs") == "debian:12"

    def test_trivy_target_alpine(self):
        assert _trivy_target_to_ecosystem("alpine 3.19.1", "os-pkgs") == "alpine:3"

    def test_trivy_target_ubuntu(self):
        assert _trivy_target_to_ecosystem("ubuntu 22.04", "os-pkgs") == "ubuntu:22"

    def test_trivy_target_python(self):
        assert _trivy_target_to_ecosystem("Python", "lang-pkgs") == "pypi"

    def test_trivy_target_nodejs(self):
        assert _trivy_target_to_ecosystem("Node.js", "lang-pkgs") == "npm"


# ---------------------------------------------------------------------------
# Tests: edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Edge cases and malformed inputs."""

    def test_osv_missing_fields_graceful(self):
        """Minimal valid OSV output with missing optional fields."""
        minimal = {
            "results": [
                {
                    "source": {"path": "test.cdx.json", "type": "sbom"},
                    "packages": [
                        {
                            "package": {"name": "pkg", "version": "1.0", "ecosystem": "Go"},
                            "vulnerabilities": [
                                {
                                    "id": "GO-2024-001",
                                    "aliases": [],
                                    "summary": "",
                                    "severity": [],
                                    "affected": [],
                                    "database_specific": {},
                                }
                            ],
                            "groups": [],
                        }
                    ],
                }
            ]
        }
        results = normalize_osv(minimal)
        assert len(results) == 1
        v = results[0]
        assert v.cve_id == "GO-2024-001"
        assert v.severity == "UNKNOWN"
        assert v.fixed_version is None
        assert v.affected_versions == "all"

    def test_trivy_missing_cvss(self):
        """Trivy vuln with no CVSS data."""
        data = {
            "Results": [
                {
                    "Target": "debian 11",
                    "Class": "os-pkgs",
                    "Vulnerabilities": [
                        {
                            "VulnerabilityID": "CVE-2024-00001",
                            "PkgName": "curl",
                            "InstalledVersion": "7.74.0-1.3+deb11u10",
                            "FixedVersion": "",
                            "Severity": "LOW",
                            "Title": "Test vuln",
                            "CVSS": {},
                        }
                    ],
                }
            ]
        }
        results = normalize_trivy(data)
        assert len(results) == 1
        assert results[0].cvss_score is None

    def test_trivy_invalid_severity_defaults_unknown(self):
        """Trivy with an unexpected severity string."""
        data = {
            "Results": [
                {
                    "Target": "debian 12",
                    "Class": "os-pkgs",
                    "Vulnerabilities": [
                        {
                            "VulnerabilityID": "CVE-2024-00002",
                            "PkgName": "zlib",
                            "InstalledVersion": "1.2.13",
                            "FixedVersion": "1.2.14",
                            "Severity": "NEGLIGIBLE",
                            "Title": "Test",
                            "CVSS": {},
                        }
                    ],
                }
            ]
        }
        results = normalize_trivy(data)
        assert results[0].severity == "UNKNOWN"

    def test_normalized_vulnerability_is_dataclass(self):
        """Verify NormalizedVulnerability has expected fields."""
        v = NormalizedVulnerability(
            cve_id="CVE-2024-TEST",
            package_name="test-pkg",
            package_ecosystem="npm",
            source_scanner="osv-scanner",
        )
        assert v.cve_id == "CVE-2024-TEST"
        assert v.aliases == []
        assert v.fixed_version is None
        assert v.severity == "UNKNOWN"
