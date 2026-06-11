"""
Unit tests for Knowledge Layer vulnerability detection and remediation loop.

Test cases V1–V6 from TESTING.md §5. Pure logic — uses fixture data, no live scanners.

Validates:
- V1: Reverse lookup SQL correctness
- V2: OSV-Scanner fixture → expected CVE
- V3: Trivy fixture → expected OS-layer CVE
- V4: Unreachable symbol → no issue filed
- V5: Reachable symbol → exactly one issue per affected repo
- V6: Duplicate CVE → no duplicate issue
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from .conftest import DependencyRow, FakeDependencyStore


# ---------------------------------------------------------------------------
# Reachability checker (simulates structural index lookup)
# ---------------------------------------------------------------------------


@dataclass
class FakeReachabilityChecker:
    """Determines if a vulnerable dependency's code is actually called.

    Simulates the structural index's ability to trace whether a package's
    symbols are reachable from application entry points.
    """

    # repo_id -> {package -> is_reachable}
    _reachability: dict[str, dict[str, bool]] = field(default_factory=dict)

    def set_reachable(self, repo_id: str, package: str, reachable: bool) -> None:
        self._reachability.setdefault(repo_id, {})[package] = reachable

    def is_reachable(self, repo_id: str, package: str) -> bool:
        """Check if a package's code is reachable in the given repo.

        Default: True (fail-safe — if we can't determine, assume reachable).
        """
        return self._reachability.get(repo_id, {}).get(package, True)


@pytest.fixture
def fake_reachability() -> FakeReachabilityChecker:
    return FakeReachabilityChecker()


# ---------------------------------------------------------------------------
# Triage engine (the logic under test)
# ---------------------------------------------------------------------------


@dataclass
class TriageDecision:
    """Result of the triage gate for a single (repo, vulnerability) pair."""

    should_file_issue: bool
    reason: str
    repo_id: str
    vuln_id: str


def triage_vulnerability(
    repo_id: str,
    vuln_id: str,
    package: str,
    dependency_store: FakeDependencyStore,
    reachability_checker: FakeReachabilityChecker,
) -> TriageDecision:
    """Decide whether to file an issue for a vulnerability in a repo.

    Rules:
    1. If the dependency is not reachable → no issue (false positive suppression)
    2. If an issue was already filed → no duplicate
    3. Otherwise → file an issue
    """
    # Check idempotency first
    if dependency_store.issue_already_filed(repo_id, vuln_id):
        return TriageDecision(
            should_file_issue=False,
            reason="duplicate — issue already filed",
            repo_id=repo_id,
            vuln_id=vuln_id,
        )

    # Check reachability
    if not reachability_checker.is_reachable(repo_id, package):
        return TriageDecision(
            should_file_issue=False,
            reason="unreachable — symbol not called from application code",
            repo_id=repo_id,
            vuln_id=vuln_id,
        )

    return TriageDecision(
        should_file_issue=True,
        reason="reachable and not yet reported",
        repo_id=repo_id,
        vuln_id=vuln_id,
    )


# ---------------------------------------------------------------------------
# V1: Reverse lookup — correct repo list
# ---------------------------------------------------------------------------


class TestReverseLookup:
    """V1: Reverse lookup returns exactly the repos using a specific package@version."""

    def test_exact_match_returns_seeded_repos(self, fake_dependency_store: FakeDependencyStore):
        """V1a: Lookup for a specific package+version returns only matching repos."""
        # Seed dependencies
        fake_dependency_store.add_dependency(
            DependencyRow(repo_id="org/service-a", package="pkg:pypi/requests", version="2.25.0")
        )
        fake_dependency_store.add_dependency(
            DependencyRow(repo_id="org/service-b", package="pkg:pypi/requests", version="2.25.0")
        )
        fake_dependency_store.add_dependency(
            DependencyRow(repo_id="org/service-c", package="pkg:pypi/requests", version="2.31.0")
        )

        affected = fake_dependency_store.reverse_lookup("pkg:pypi/requests", "2.25.0")

        assert set(affected) == {"org/service-a", "org/service-b"}
        assert "org/service-c" not in affected  # different version

    def test_no_match_returns_empty(self, fake_dependency_store: FakeDependencyStore):
        """V1b: Lookup for a package no one uses returns empty."""
        fake_dependency_store.add_dependency(
            DependencyRow(repo_id="org/service-a", package="pkg:pypi/flask", version="3.0.0")
        )

        affected = fake_dependency_store.reverse_lookup("pkg:pypi/nonexistent", "1.0.0")

        assert affected == []

    def test_different_versions_not_confused(self, fake_dependency_store: FakeDependencyStore):
        """V1c: Same package, different versions are distinct."""
        fake_dependency_store.add_dependency(
            DependencyRow(repo_id="org/repo-old", package="pkg:pypi/django", version="3.2.0")
        )
        fake_dependency_store.add_dependency(
            DependencyRow(repo_id="org/repo-new", package="pkg:pypi/django", version="4.2.0")
        )

        old_users = fake_dependency_store.reverse_lookup("pkg:pypi/django", "3.2.0")
        new_users = fake_dependency_store.reverse_lookup("pkg:pypi/django", "4.2.0")

        assert old_users == ["org/repo-old"]
        assert new_users == ["org/repo-new"]


# ---------------------------------------------------------------------------
# V2: OSV-Scanner fixture → expected CVE ID
# ---------------------------------------------------------------------------


@dataclass
class FakeOSVScanner:
    """Simulates OSV-Scanner output for deterministic testing.

    In production, this wraps `osv-scanner --offline` with a pinned database.
    For unit tests, we provide canned results for known fixture inputs.
    """

    # package_purl -> list of {vuln_id, severity, affected_versions}
    _database: dict[str, list[dict]] = field(default_factory=dict)

    def add_known_vuln(self, package_purl: str, vuln_id: str, severity: str = "HIGH") -> None:
        """Seed a known vulnerability into the fake database."""
        self._database.setdefault(package_purl, []).append(
            {
                "vuln_id": vuln_id,
                "severity": severity,
            }
        )

    def scan_dependencies(self, dependencies: list[dict]) -> list[dict]:
        """Scan a list of dependencies and return matching vulnerabilities.

        Each dependency: {package, version, purl}
        Returns: [{vuln_id, package, version, severity}]
        """
        findings = []
        for dep in dependencies:
            purl = dep.get("purl", f"{dep['package']}@{dep['version']}")
            if purl in self._database:
                for vuln in self._database[purl]:
                    findings.append(
                        {
                            "vuln_id": vuln["vuln_id"],
                            "package": dep["package"],
                            "version": dep["version"],
                            "severity": vuln["severity"],
                            "source": "osv",
                        }
                    )
        return findings


@pytest.fixture
def fake_osv_scanner() -> FakeOSVScanner:
    scanner = FakeOSVScanner()
    # Pre-seed with the planted CVE from our fixture
    scanner.add_known_vuln("pkg:pypi/requests@2.25.0", "CVE-2023-32681", severity="HIGH")
    return scanner


class TestOSVScannerIntegration:
    """V2: OSV-Scanner scan of fixture SBOM returns expected CVE."""

    def test_planted_vuln_detected(self, fake_osv_scanner: FakeOSVScanner):
        """V2a: Fixture with requests==2.25.0 detects CVE-2023-32681."""
        dependencies = [
            {
                "package": "pkg:pypi/requests",
                "version": "2.25.0",
                "purl": "pkg:pypi/requests@2.25.0",
            },
            {"package": "pkg:pypi/flask", "version": "3.0.0", "purl": "pkg:pypi/flask@3.0.0"},
        ]

        findings = fake_osv_scanner.scan_dependencies(dependencies)

        assert len(findings) == 1
        assert findings[0]["vuln_id"] == "CVE-2023-32681"
        assert findings[0]["package"] == "pkg:pypi/requests"
        assert findings[0]["source"] == "osv"

    def test_clean_dependencies_no_findings(self, fake_osv_scanner: FakeOSVScanner):
        """V2b: Dependencies with no known vulns produce zero findings."""
        dependencies = [
            {"package": "pkg:pypi/flask", "version": "3.0.0", "purl": "pkg:pypi/flask@3.0.0"},
        ]

        findings = fake_osv_scanner.scan_dependencies(dependencies)
        assert findings == []


# ---------------------------------------------------------------------------
# V3: Trivy fixture → expected OS-layer CVE
# ---------------------------------------------------------------------------


@dataclass
class FakeTrivyScanner:
    """Simulates Trivy SBOM scanning for OS-layer vulnerabilities.

    In production, this wraps `trivy sbom --skip-db-update <file>`.
    """

    _database: dict[str, list[dict]] = field(default_factory=dict)

    def add_known_vuln(self, package_purl: str, vuln_id: str, severity: str = "HIGH") -> None:
        self._database.setdefault(package_purl, []).append(
            {
                "vuln_id": vuln_id,
                "severity": severity,
            }
        )

    def scan_sbom(self, sbom_packages: list[dict]) -> list[dict]:
        """Scan OS-layer packages from an image SBOM."""
        findings = []
        for pkg in sbom_packages:
            purl = pkg.get("purl", f"{pkg['package']}@{pkg['version']}")
            if purl in self._database:
                for vuln in self._database[purl]:
                    findings.append(
                        {
                            "vuln_id": vuln["vuln_id"],
                            "package": pkg["package"],
                            "version": pkg["version"],
                            "severity": vuln["severity"],
                            "source": "trivy",
                        }
                    )
        return findings


@pytest.fixture
def fake_trivy_scanner() -> FakeTrivyScanner:
    scanner = FakeTrivyScanner()
    # Pre-seed with a known OS-layer CVE
    scanner.add_known_vuln("pkg:deb/debian/openssl@3.0.11-1", "CVE-2024-0727", severity="HIGH")
    return scanner


class TestTrivyScannerIntegration:
    """V3: Trivy scan of fixture image SBOM returns expected OS-layer CVE."""

    def test_os_layer_vuln_detected(self, fake_trivy_scanner: FakeTrivyScanner):
        """V3a: Fixture with vulnerable openssl detects CVE-2024-0727."""
        image_packages = [
            {
                "package": "pkg:deb/debian/openssl",
                "version": "3.0.11-1",
                "purl": "pkg:deb/debian/openssl@3.0.11-1",
            },
            {
                "package": "pkg:deb/debian/libc6",
                "version": "2.36-9",
                "purl": "pkg:deb/debian/libc6@2.36-9",
            },
        ]

        findings = fake_trivy_scanner.scan_sbom(image_packages)

        assert len(findings) == 1
        assert findings[0]["vuln_id"] == "CVE-2024-0727"
        assert findings[0]["source"] == "trivy"

    def test_clean_image_no_findings(self, fake_trivy_scanner: FakeTrivyScanner):
        """V3b: Image packages with no known vulns produce zero findings."""
        image_packages = [
            {
                "package": "pkg:deb/debian/libc6",
                "version": "2.36-9",
                "purl": "pkg:deb/debian/libc6@2.36-9",
            },
        ]

        findings = fake_trivy_scanner.scan_sbom(image_packages)
        assert findings == []


# ---------------------------------------------------------------------------
# V4: Unreachable symbol → no issue filed
# ---------------------------------------------------------------------------


class TestTriageGateUnreachable:
    """V4: A CVE in an unreachable symbol files NO issue."""

    def test_unreachable_dependency_no_issue(
        self,
        fake_dependency_store: FakeDependencyStore,
        fake_reachability: FakeReachabilityChecker,
    ):
        """V4a: Unreachable package → triage says 'do not file'."""
        repo_id = "org/service-a"
        package = "pkg:pypi/requests"
        vuln_id = "CVE-2023-32681"

        # Mark as unreachable (imported but never called)
        fake_reachability.set_reachable(repo_id, package, reachable=False)

        decision = triage_vulnerability(
            repo_id, vuln_id, package, fake_dependency_store, fake_reachability
        )

        assert decision.should_file_issue is False
        assert "unreachable" in decision.reason

    def test_dead_import_no_issue(
        self,
        fake_dependency_store: FakeDependencyStore,
        fake_reachability: FakeReachabilityChecker,
    ):
        """V4b: Package listed in requirements but zero imports → no issue."""
        repo_id = "org/service-b"
        package = "pkg:pypi/deprecated-lib"

        fake_reachability.set_reachable(repo_id, package, reachable=False)

        decision = triage_vulnerability(
            repo_id, "CVE-2024-9999", package, fake_dependency_store, fake_reachability
        )

        assert decision.should_file_issue is False


# ---------------------------------------------------------------------------
# V5: Reachable symbol → exactly one issue per affected repo
# ---------------------------------------------------------------------------


class TestTriageGateReachable:
    """V5: A reachable CVE files exactly one issue per affected repo."""

    def test_reachable_dependency_files_issue(
        self,
        fake_dependency_store: FakeDependencyStore,
        fake_reachability: FakeReachabilityChecker,
    ):
        """V5a: Reachable package → triage says 'file issue'."""
        repo_id = "org/service-a"
        package = "pkg:pypi/requests"
        vuln_id = "CVE-2023-32681"

        fake_reachability.set_reachable(repo_id, package, reachable=True)

        decision = triage_vulnerability(
            repo_id, vuln_id, package, fake_dependency_store, fake_reachability
        )

        assert decision.should_file_issue is True

    def test_multiple_repos_each_get_one_issue(
        self,
        fake_dependency_store: FakeDependencyStore,
        fake_reachability: FakeReachabilityChecker,
    ):
        """V5b: N affected repos → N separate triage decisions (one per repo)."""
        repos = ["org/service-a", "org/service-b", "org/service-c"]
        package = "pkg:pypi/requests"
        vuln_id = "CVE-2023-32681"

        for repo in repos:
            fake_reachability.set_reachable(repo, package, reachable=True)

        decisions = [
            triage_vulnerability(repo, vuln_id, package, fake_dependency_store, fake_reachability)
            for repo in repos
        ]

        assert all(d.should_file_issue is True for d in decisions)
        assert len(decisions) == 3


# ---------------------------------------------------------------------------
# V6: Duplicate CVE → no duplicate issue
# ---------------------------------------------------------------------------


class TestTriageIdempotency:
    """V6: Duplicate vulnerability detection → no duplicate issue filed."""

    def test_already_filed_skips(
        self,
        fake_dependency_store: FakeDependencyStore,
        fake_reachability: FakeReachabilityChecker,
    ):
        """V6a: If issue already filed for this (repo, CVE), don't file again."""
        repo_id = "org/service-a"
        package = "pkg:pypi/requests"
        vuln_id = "CVE-2023-32681"

        fake_reachability.set_reachable(repo_id, package, reachable=True)

        # First triage — should file
        decision1 = triage_vulnerability(
            repo_id, vuln_id, package, fake_dependency_store, fake_reachability
        )
        assert decision1.should_file_issue is True

        # Simulate filing the issue
        fake_dependency_store.file_issue(repo_id, vuln_id, {"title": "Bump requests"})

        # Second triage — should NOT file (duplicate)
        decision2 = triage_vulnerability(
            repo_id, vuln_id, package, fake_dependency_store, fake_reachability
        )
        assert decision2.should_file_issue is False
        assert "duplicate" in decision2.reason

    def test_same_vuln_different_repo_still_files(
        self,
        fake_dependency_store: FakeDependencyStore,
        fake_reachability: FakeReachabilityChecker,
    ):
        """V6b: Same CVE in a different repo → still files (per-repo idempotency)."""
        package = "pkg:pypi/requests"
        vuln_id = "CVE-2023-32681"

        fake_reachability.set_reachable("org/service-a", package, reachable=True)
        fake_reachability.set_reachable("org/service-b", package, reachable=True)

        # File for repo A
        fake_dependency_store.file_issue("org/service-a", vuln_id, {})

        # Triage for repo B — should still file (different repo)
        decision = triage_vulnerability(
            "org/service-b", vuln_id, package, fake_dependency_store, fake_reachability
        )
        assert decision.should_file_issue is True
