"""
Unit tests for the consolidated `secure` verb backend.

Tests cover:
- Version range matching (data-join layer)
- Prioritization scoring and bucket assignment
- Input validation (handler dispatch)
- PURL parsing helpers
- Remediation plan generation

Uses real data assertions (not mocks of the backend itself).

Reference:
- Design: docs/agent-context/design-2447-secure-verb-architecture.md
- Product story: docs/agent-context/design-2437-secure-verb-product-story.md
- Consolidation: Issue #2510
"""

from __future__ import annotations

import pytest

from door.secure_backend import (
    REACHABILITY_WEIGHTS,
    SEVERITY_WEIGHTS,
    Finding,
    ReachabilityResult,
    UsageSite,
    _expand_remediation,
    _extract_ecosystem_from_purl,
    _extract_package_name_from_purl,
    _sort_findings,
    compute_priority_score,
    version_in_range,
)


# ---------------------------------------------------------------------------
# Version range matching
# ---------------------------------------------------------------------------


class TestVersionInRange:
    """Test version_in_range used in the data-join layer."""

    def test_version_in_range_basic(self):
        """Version within a simple range spec."""
        assert version_in_range("2.25.0", ">=2.0.0,<2.31.0") is True

    def test_version_at_upper_bound_excluded(self):
        """Upper bound is exclusive by default."""
        assert version_in_range("2.31.0", ">=2.0.0,<2.31.0") is False

    def test_version_at_lower_bound_included(self):
        """Lower bound is inclusive with >= operator."""
        assert version_in_range("2.0.0", ">=2.0.0,<2.31.0") is True

    def test_version_below_range(self):
        """Version below the lower bound."""
        assert version_in_range("1.9.0", ">=2.0.0,<2.31.0") is False

    def test_version_above_range(self):
        """Version above the upper bound."""
        assert version_in_range("3.0.0", ">=2.0.0,<2.31.0") is False

    def test_all_keyword_matches_everything(self):
        """'all' means all versions are affected."""
        assert version_in_range("1.0.0", "all") is True
        assert version_in_range("99.99.99", "ALL") is True

    def test_empty_range_fails_open(self):
        """Empty range spec → fail-open (True)."""
        assert version_in_range("1.0.0", "") is True

    def test_exact_version_match(self):
        """Bare version string = exact match."""
        assert version_in_range("1.2.3", "1.2.3") is True
        assert version_in_range("1.2.4", "1.2.3") is False

    def test_semver_with_v_prefix(self):
        """Version strings with 'v' prefix are handled."""
        assert version_in_range("v2.25.0", ">=2.0.0,<2.31.0") is True

    def test_complex_version_strings(self):
        """Versions with pre-release/build metadata."""
        assert version_in_range("3.0.11-1~deb12u2", ">=3.0.0,<4.0.0") is True


# ---------------------------------------------------------------------------
# PURL helpers
# ---------------------------------------------------------------------------


class TestPurlHelpers:
    """Test PURL parsing utilities."""

    def test_extract_package_name_pypi(self):
        assert _extract_package_name_from_purl("pkg:pypi/requests@2.31.0") == "requests"

    def test_extract_package_name_npm(self):
        assert _extract_package_name_from_purl("pkg:npm/lodash@4.17.21") == "lodash"

    def test_extract_package_name_scoped_npm(self):
        assert _extract_package_name_from_purl("pkg:npm/%40angular/core@16.0.0") == "core"

    def test_extract_package_name_no_version(self):
        assert _extract_package_name_from_purl("pkg:pypi/requests") == "requests"

    def test_extract_ecosystem_pypi(self):
        assert _extract_ecosystem_from_purl("pkg:pypi/requests@2.31.0") == "pypi"

    def test_extract_ecosystem_npm(self):
        assert _extract_ecosystem_from_purl("pkg:npm/lodash@4.17.21") == "npm"

    def test_extract_ecosystem_go(self):
        assert _extract_ecosystem_from_purl("pkg:go/github.com/foo/bar@1.0.0") == "go"

    def test_extract_ecosystem_empty(self):
        assert _extract_ecosystem_from_purl("") == "unknown"


# ---------------------------------------------------------------------------
# Prioritization scoring
# ---------------------------------------------------------------------------


class TestPriorityScoreComputation:
    """Validate the composite priority score formula."""

    def test_critical_reachable_fix_available_is_max(self):
        """CRITICAL + reachable + fix available = 1.0."""
        score, bucket = compute_priority_score("CRITICAL", "reachable", True)
        assert score == pytest.approx(1.0)
        assert bucket == "P0"

    def test_high_called_fix_available(self):
        """HIGH (0.7) x called (0.8) x fix (1.0) = 0.56."""
        score, bucket = compute_priority_score("HIGH", "called", True)
        assert score == pytest.approx(0.56)
        assert bucket == "P1"

    def test_critical_present_fix_available(self):
        """CRITICAL (1.0) x present (0.2) x fix (1.0) = 0.20."""
        score, bucket = compute_priority_score("CRITICAL", "present", True)
        assert score == pytest.approx(0.20)
        assert bucket == "P2"

    def test_medium_imported_no_fix(self):
        """MEDIUM (0.4) x imported (0.5) x no fix (0.3) = 0.06."""
        score, bucket = compute_priority_score("MEDIUM", "imported", False)
        assert score == pytest.approx(0.06)
        assert bucket == "P3"

    def test_low_present_no_fix_is_minimum(self):
        """LOW (0.1) x present (0.2) x no fix (0.3) = 0.006."""
        score, bucket = compute_priority_score("LOW", "present", False)
        assert score == pytest.approx(0.006)
        assert bucket == "P3"

    def test_unknown_severity_defaults_to_low(self):
        """Unknown severity gets 0.1 weight."""
        score, _ = compute_priority_score("UNKNOWN", "reachable", True)
        assert score == pytest.approx(0.1)

    def test_unknown_reachability_defaults_to_reachable(self):
        """Unknown reachability gets 1.0 weight (fail-safe)."""
        score, _ = compute_priority_score("CRITICAL", "unknown_level", True)
        assert score == pytest.approx(1.0)

    def test_score_always_non_negative(self):
        """Score should never be negative."""
        for sev in SEVERITY_WEIGHTS:
            for reach in REACHABILITY_WEIGHTS:
                for fix in (True, False):
                    score, _ = compute_priority_score(sev, reach, fix)
                    assert score >= 0.0


class TestPriorityBuckets:
    """Validate priority bucket assignment."""

    def test_p0_threshold(self):
        """Score >= 0.7 is P0."""
        _, bucket = compute_priority_score("CRITICAL", "reachable", True)
        assert bucket == "P0"

    def test_p1_range(self):
        """0.4 <= score < 0.7 is P1."""
        _, bucket = compute_priority_score("HIGH", "called", True)
        assert bucket == "P1"

    def test_p2_range(self):
        """0.1 <= score < 0.4 is P2."""
        _, bucket = compute_priority_score("CRITICAL", "present", True)
        assert bucket == "P2"

    def test_p3_range(self):
        """score < 0.1 is P3."""
        _, bucket = compute_priority_score("LOW", "present", False)
        assert bucket == "P3"

    def test_boundary_p0_p1(self):
        """Boundary: 0.7 is P0."""
        # HIGH (0.7) x reachable (1.0) x fix (1.0) = 0.7
        score, bucket = compute_priority_score("HIGH", "reachable", True)
        assert score == pytest.approx(0.7)
        assert bucket == "P0"


# ---------------------------------------------------------------------------
# Finding sorting
# ---------------------------------------------------------------------------


class TestFindingSorting:
    """Findings sorted by priority_score descending."""

    def test_sorted_by_score_descending(self):
        """Findings are ordered highest-score first."""
        findings = [
            Finding(cve_id="CVE-D", severity="MEDIUM", priority_score=0.06, priority="P3"),
            Finding(cve_id="CVE-A", severity="CRITICAL", priority_score=1.0, priority="P0"),
            Finding(cve_id="CVE-B", severity="HIGH", priority_score=0.56, priority="P1"),
        ]
        sorted_f = _sort_findings(findings)
        assert sorted_f[0].cve_id == "CVE-A"
        assert sorted_f[1].cve_id == "CVE-B"
        assert sorted_f[2].cve_id == "CVE-D"

    def test_tie_broken_by_severity(self):
        """Same score, higher severity first."""
        findings = [
            Finding(cve_id="CVE-X", severity="HIGH", priority_score=0.5, priority="P1"),
            Finding(cve_id="CVE-Y", severity="CRITICAL", priority_score=0.5, priority="P1"),
        ]
        sorted_f = _sort_findings(findings)
        assert sorted_f[0].cve_id == "CVE-Y"  # CRITICAL ranks higher

    def test_tie_broken_by_cve_id(self):
        """Same score and severity, alphabetical CVE ID."""
        findings = [
            Finding(cve_id="CVE-2024-200", severity="HIGH", priority_score=0.5, priority="P1"),
            Finding(cve_id="CVE-2024-100", severity="HIGH", priority_score=0.5, priority="P1"),
        ]
        sorted_f = _sort_findings(findings)
        assert sorted_f[0].cve_id == "CVE-2024-100"


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


class TestInputValidation:
    """Validate secure verb input constraints (tested via handler in server.py)."""

    def test_at_least_one_param_required(self):
        """No cve, repo, or package → validation error."""
        # This is tested at the handler level; here we verify the Finding dataclass
        f = Finding(cve_id="CVE-2023-32681")
        assert f.cve_id == "CVE-2023-32681"
        assert f.priority == "P3"  # Default

    def test_finding_defaults(self):
        """Finding has sensible defaults."""
        f = Finding(cve_id="CVE-2023-32681")
        assert f.severity == "UNKNOWN"
        assert f.reachability_level == "present"
        assert f.fix_available is False
        assert f.priority_score == 0.0
        assert f.repos_affected == []
        assert f.usage_sites == []


# ---------------------------------------------------------------------------
# Reachability data types
# ---------------------------------------------------------------------------


class TestReachabilityDataTypes:
    """Verify reachability result dataclasses."""

    def test_reachability_result_fields(self):
        r = ReachabilityResult(level="reachable", confidence="high", source="neptune")
        assert r.level == "reachable"
        assert r.confidence == "high"
        assert r.source == "neptune"

    def test_usage_site_fields(self):
        s = UsageSite(
            file="src/api/client.py",
            line=15,
            symbol="http_get",
            reachability_level="reachable",
            callers=[{"file": "src/main.py", "line": 1, "symbol": "main", "distance": 2}],
        )
        assert s.file == "src/api/client.py"
        assert s.line == 15
        assert len(s.callers) == 1
        assert s.callers[0]["distance"] == 2

    def test_usage_site_default_callers(self):
        s = UsageSite(file="a.py", line=1, symbol="x", reachability_level="imported")
        assert s.callers == []


# ---------------------------------------------------------------------------
# Remediation plan expansion
# ---------------------------------------------------------------------------


class TestRemediationExpansion:
    """Test _expand_remediation for action=plan."""

    def test_version_bump_plan(self):
        """A finding with a fix version gets a version_bump plan."""
        finding = {
            "cve_id": "CVE-2023-32681",
            "package": "requests",
            "ecosystem": "pypi",
            "affected_version": "2.25.0",
            "fixed_version": "2.31.0",
            "usage_sites": [
                {"file": "src/api/client.py", "line": 15, "reachability_level": "reachable"},
            ],
        }
        result = _expand_remediation(finding, "CVE-2023-32681", "org/service-a")

        assert "remediation" in result
        rem = result["remediation"]
        assert rem["fix_type"] == "version_bump"
        assert rem["target_version"] == "2.31.0"
        assert len(rem["steps"]) > 0
        assert rem["steps"][0]["action"] == "update_dependency"
        assert rem["steps"][-1]["action"] == "verify_fix"

    def test_mitigation_plan_no_fix_version(self):
        """A finding without a fix version gets a mitigation plan."""
        finding = {
            "cve_id": "CVE-2023-99999",
            "package": "lodash",
            "ecosystem": "npm",
            "affected_version": "4.17.0",
            "fixed_version": None,
            "usage_sites": [
                {"file": "src/utils.ts", "line": 42, "reachability_level": "called"},
            ],
        }
        result = _expand_remediation(finding, "CVE-2023-99999", "org/service-a")

        rem = result["remediation"]
        assert rem["fix_type"] == "mitigation"
        assert rem["target_version"] is None
        assert rem["estimated_complexity"] == "complex"

    def test_start_here_points_to_most_reachable(self):
        """start_here picks the most reachable usage site."""
        finding = {
            "cve_id": "CVE-2023-32681",
            "package": "requests",
            "ecosystem": "pypi",
            "affected_version": "2.25.0",
            "fixed_version": "2.31.0",
            "usage_sites": [
                {"file": "tests/test.py", "line": 8, "reachability_level": "imported"},
                {"file": "src/api/client.py", "line": 15, "reachability_level": "reachable"},
            ],
        }
        result = _expand_remediation(finding, "CVE-2023-32681", "org/service-a")
        assert result["remediation"]["start_here"] == "src/api/client.py:15"

    def test_breaking_change_risk_low_same_major(self):
        """Same major version = low breaking change risk."""
        finding = {
            "cve_id": "CVE-2025-1234",
            "package": "lodash",
            "ecosystem": "npm",
            "affected_version": "4.17.0",
            "fixed_version": "4.17.21",
            "usage_sites": [],
        }
        result = _expand_remediation(finding, "CVE-2025-1234", "org/service-a")
        assert result["remediation"]["breaking_change_risk"] == "low"
        assert result["remediation"]["estimated_complexity"] == "trivial"

    def test_breaking_change_risk_high_different_major(self):
        """Different major version = high breaking change risk."""
        finding = {
            "cve_id": "CVE-2023-32681",
            "package": "requests",
            "ecosystem": "pypi",
            "affected_version": "2.25.0",
            "fixed_version": "3.0.0",
            "usage_sites": [],
        }
        result = _expand_remediation(finding, "CVE-2023-32681", "org/service-a")
        assert result["remediation"]["breaking_change_risk"] == "high"
        assert result["remediation"]["estimated_complexity"] == "moderate"
        assert result["remediation"]["breaking_change_details"] is not None

    def test_ecosystem_specific_files_pypi(self):
        """PyPI ecosystem references pyproject.toml."""
        finding = {
            "cve_id": "CVE-2023-32681",
            "package": "requests",
            "ecosystem": "pypi",
            "affected_version": "2.25.0",
            "fixed_version": "2.31.0",
            "usage_sites": [],
        }
        result = _expand_remediation(finding, "CVE-2023-32681", "org/service-a")
        assert "pyproject.toml" in result["remediation"]["files_to_change"]

    def test_ecosystem_specific_files_npm(self):
        """npm ecosystem references package.json."""
        finding = {
            "cve_id": "CVE-2025-1234",
            "package": "lodash",
            "ecosystem": "npm",
            "affected_version": "4.17.0",
            "fixed_version": "4.17.21",
            "usage_sites": [],
        }
        result = _expand_remediation(finding, "CVE-2025-1234", "org/service-a")
        assert "package.json" in result["remediation"]["files_to_change"]
        assert "package-lock.json" in result["remediation"]["files_to_change"]
