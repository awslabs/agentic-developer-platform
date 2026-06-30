"""
Fixture data for the `secure` verb test suite.

Provides seeded repos, dependencies, and vulnerabilities for fixture-based
validation (real SQL against SQLite, not mocks). Matches the data shape used
by the production schema (001_knowledge_layer_schema migration).

Design:
- Two repos share a vulnerable package (requests@2.25.0 -> CVE-2023-32681)
- One repo has the patched version (requests@2.31.0) to test verify scenarios
- One repo has a CRITICAL lodash vuln for prioritization testing
- ACL setup: two repos public, one restricted to team/backend
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

# ---------------------------------------------------------------------------
# Repositories
# ---------------------------------------------------------------------------

FIXTURE_REPOS = [
    {
        "id": "repo-sa",
        "repo_name": "org/service-a",
        "git_url": "https://github.com/org/service-a.git",
        "owner": "org",
        "allowed_principals": '["*"]',
        "sbom_status": "complete",
        "indexed_at": (datetime.now(UTC) - timedelta(hours=2)).isoformat(),
    },
    {
        "id": "repo-sb",
        "repo_name": "org/service-b",
        "git_url": "https://github.com/org/service-b.git",
        "owner": "org",
        "allowed_principals": '["*"]',
        "sbom_status": "complete",
        "indexed_at": (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
    },
    {
        "id": "repo-lib",
        "repo_name": "org/lib-internal",
        "git_url": "https://github.com/org/lib-internal.git",
        "owner": "org",
        "allowed_principals": '["team/backend"]',
        "sbom_status": "complete",
        "indexed_at": (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
    },
    {
        "id": "repo-stale",
        "repo_name": "org/stale-repo",
        "git_url": "https://github.com/org/stale-repo.git",
        "owner": "org",
        "allowed_principals": '["*"]',
        "sbom_status": "complete",
        "indexed_at": (datetime.now(UTC) - timedelta(hours=48)).isoformat(),
    },
]

# ---------------------------------------------------------------------------
# Dependencies (SBOM data)
# ---------------------------------------------------------------------------

FIXTURE_DEPS = [
    # service-a: vulnerable requests + vulnerable lodash
    {
        "id": "dep-sa-req",
        "repo_id": "repo-sa",
        "package_coordinate": "pkg:pypi/requests@2.25.0",
        "version": "2.25.0",
        "source": "code",
    },
    {
        "id": "dep-sa-lodash",
        "repo_id": "repo-sa",
        "package_coordinate": "pkg:npm/lodash@4.17.0",
        "version": "4.17.0",
        "source": "code",
    },
    # service-b: patched requests + flask (not vulnerable)
    {
        "id": "dep-sb-req",
        "repo_id": "repo-sb",
        "package_coordinate": "pkg:pypi/requests@2.31.0",
        "version": "2.31.0",
        "source": "code",
    },
    {
        "id": "dep-sb-flask",
        "repo_id": "repo-sb",
        "package_coordinate": "pkg:pypi/flask@3.0.0",
        "version": "3.0.0",
        "source": "code",
    },
    # lib-internal: vulnerable requests (same as service-a)
    {
        "id": "dep-lib-req",
        "repo_id": "repo-lib",
        "package_coordinate": "pkg:pypi/requests@2.25.0",
        "version": "2.25.0",
        "source": "code",
    },
    # stale-repo: vulnerable requests
    {
        "id": "dep-stale-req",
        "repo_id": "repo-stale",
        "package_coordinate": "pkg:pypi/requests@2.25.0",
        "version": "2.25.0",
        "source": "code",
    },
]

# ---------------------------------------------------------------------------
# Vulnerabilities
# ---------------------------------------------------------------------------

FIXTURE_VULNS = [
    {
        "id": "vuln-requests",
        "cve_id": "CVE-2023-32681",
        "package": "requests",
        "affected_versions": ">=2.0.0,<2.31.0",
        "safe_version": "2.31.0",
        "severity": "HIGH",
        "details": "Unintended leak of Proxy-Authorization header to destination servers.",
    },
    {
        "id": "vuln-lodash",
        "cve_id": "CVE-2025-1234",
        "package": "lodash",
        "affected_versions": ">=4.0.0,<4.17.21",
        "safe_version": "4.17.21",
        "severity": "CRITICAL",
        "details": "Prototype pollution in lodash.merge and related functions.",
    },
]
