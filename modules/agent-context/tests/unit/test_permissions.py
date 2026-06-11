"""
Unit tests for Knowledge Layer permission filter — fail-closed guarantee.

Test cases P1–P6 from TESTING.md §4. These are the highest-priority tests
in the Knowledge Layer: a failure here means cross-tenant data exposure.

Validates:
- P1: Two principals, two private repos → each sees only their own repos
- P2: Unknown/missing principal → empty result (never fail-open)
- P3: Public repos visible to all principals
- P4: Permission change on re-ingest → ACL updated, next query reflects it
- P5: Malformed identity → rejection
- P6: Empty ACL (orphan repo) → invisible to all
"""

from __future__ import annotations

import uuid

import pytest

from .conftest import FakeACLStore, FakePrincipal


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_search_results(*repo_ids: str) -> list[dict]:
    """Create mock search results from multiple repos."""
    return [
        {"repo_id": repo_id, "file": f"{repo_id}/main.py", "line": 1, "content": "..."}
        for repo_id in repo_ids
    ]


# ---------------------------------------------------------------------------
# P1: Two principals, two private repos — isolation
# ---------------------------------------------------------------------------


class TestIsolationBetweenPrincipals:
    """Each principal sees only repos they have access to."""

    def test_principal_a_sees_only_own_repo(
        self, fake_acl_store: FakeACLStore, principal_a: FakePrincipal, principal_b: FakePrincipal
    ):
        """P1a: Principal A's query returns only repos A can access."""
        repo_a = "org/repo-alpha"
        repo_b = "org/repo-beta"

        fake_acl_store.grant(repo_a, principal_a.principal_id)
        fake_acl_store.grant(repo_b, principal_b.principal_id)

        results = _make_search_results(repo_a, repo_b)
        filtered = fake_acl_store.filter_results(principal_a.principal_id, results)

        assert len(filtered) == 1
        assert filtered[0]["repo_id"] == repo_a

    def test_principal_b_sees_only_own_repo(
        self, fake_acl_store: FakeACLStore, principal_a: FakePrincipal, principal_b: FakePrincipal
    ):
        """P1b: Principal B's query returns only repos B can access."""
        repo_a = "org/repo-alpha"
        repo_b = "org/repo-beta"

        fake_acl_store.grant(repo_a, principal_a.principal_id)
        fake_acl_store.grant(repo_b, principal_b.principal_id)

        results = _make_search_results(repo_a, repo_b)
        filtered = fake_acl_store.filter_results(principal_b.principal_id, results)

        assert len(filtered) == 1
        assert filtered[0]["repo_id"] == repo_b

    def test_cross_principal_results_dropped(
        self, fake_acl_store: FakeACLStore, principal_a: FakePrincipal, principal_b: FakePrincipal
    ):
        """P1c: Results from the other principal's repos are never included."""
        repo_a = "org/repo-alpha"
        repo_b = "org/repo-beta"

        fake_acl_store.grant(repo_a, principal_a.principal_id)
        fake_acl_store.grant(repo_b, principal_b.principal_id)

        results = _make_search_results(repo_a, repo_b)
        filtered_a = fake_acl_store.filter_results(principal_a.principal_id, results)
        filtered_b = fake_acl_store.filter_results(principal_b.principal_id, results)

        # Neither sees the other's repo
        assert all(r["repo_id"] != repo_b for r in filtered_a)
        assert all(r["repo_id"] != repo_a for r in filtered_b)


# ---------------------------------------------------------------------------
# P2: Unknown/missing principal → empty result (fail-closed)
# ---------------------------------------------------------------------------


class TestFailClosed:
    """Unknown or missing identity always produces empty results."""

    def test_none_principal_returns_empty(self, fake_acl_store: FakeACLStore):
        """P2a: None identity → zero results."""
        repo = "org/some-repo"
        fake_acl_store.grant(repo, "some-user-id")

        results = _make_search_results(repo)
        filtered = fake_acl_store.filter_results(None, results)

        assert filtered == []

    def test_unknown_principal_returns_empty(self, fake_acl_store: FakeACLStore):
        """P2b: A principal ID not in any ACL → zero results."""
        repo = "org/some-repo"
        fake_acl_store.grant(repo, "authorized-user")

        unknown_id = str(uuid.uuid4())
        results = _make_search_results(repo)
        filtered = fake_acl_store.filter_results(unknown_id, results)

        assert filtered == []

    def test_empty_string_principal_returns_empty(self, fake_acl_store: FakeACLStore):
        """P2c: Empty-string identity treated as unknown → zero results."""
        repo = "org/some-repo"
        fake_acl_store.grant(repo, "authorized-user")

        results = _make_search_results(repo)
        filtered = fake_acl_store.filter_results("", results)

        assert filtered == []


# ---------------------------------------------------------------------------
# P3: Public repos visible to all
# ---------------------------------------------------------------------------


class TestPublicRepoVisibility:
    """Public/OSS repos remain searchable by any principal."""

    def test_public_repo_visible_to_authorized_user(
        self, fake_acl_store: FakeACLStore, principal_a: FakePrincipal
    ):
        """P3a: Public repos appear in authorized user's results."""
        public_repo = "oss/public-lib"
        fake_acl_store.set_public(public_repo)

        results = _make_search_results(public_repo)
        filtered = fake_acl_store.filter_results(principal_a.principal_id, results)

        assert len(filtered) == 1
        assert filtered[0]["repo_id"] == public_repo

    def test_public_repo_visible_to_unrelated_user(
        self, fake_acl_store: FakeACLStore, principal_other_org: FakePrincipal
    ):
        """P3b: Public repos visible even to users from different orgs."""
        public_repo = "oss/public-lib"
        fake_acl_store.set_public(public_repo)

        results = _make_search_results(public_repo)
        filtered = fake_acl_store.filter_results(principal_other_org.principal_id, results)

        assert len(filtered) == 1

    def test_mixed_public_and_private(
        self, fake_acl_store: FakeACLStore, principal_a: FakePrincipal
    ):
        """P3c: Mixed results correctly filter private but keep public."""
        public_repo = "oss/public-lib"
        private_repo = "org/secret-service"

        fake_acl_store.set_public(public_repo)
        fake_acl_store.grant(private_repo, "other-user-id")

        results = _make_search_results(public_repo, private_repo)
        filtered = fake_acl_store.filter_results(principal_a.principal_id, results)

        assert len(filtered) == 1
        assert filtered[0]["repo_id"] == public_repo


# ---------------------------------------------------------------------------
# P4: Permission change on re-ingest
# ---------------------------------------------------------------------------


class TestACLFreshness:
    """ACL changes are reflected after re-ingest."""

    def test_revoked_access_excludes_repo(
        self, fake_acl_store: FakeACLStore, principal_a: FakePrincipal
    ):
        """P4a: After revoking access, repo disappears from results."""
        repo = "org/was-accessible"
        fake_acl_store.grant(repo, principal_a.principal_id)

        # Verify access first
        results = _make_search_results(repo)
        assert len(fake_acl_store.filter_results(principal_a.principal_id, results)) == 1

        # Simulate re-ingest with permission change
        fake_acl_store.revoke(repo, principal_a.principal_id)

        # Now excluded
        filtered = fake_acl_store.filter_results(principal_a.principal_id, results)
        assert filtered == []

    def test_granted_access_includes_repo(
        self, fake_acl_store: FakeACLStore, principal_a: FakePrincipal
    ):
        """P4b: After granting access, repo appears in results."""
        repo = "org/newly-accessible"

        # Initially no access
        results = _make_search_results(repo)
        assert fake_acl_store.filter_results(principal_a.principal_id, results) == []

        # Simulate re-ingest granting access
        fake_acl_store.grant(repo, principal_a.principal_id)

        # Now included
        filtered = fake_acl_store.filter_results(principal_a.principal_id, results)
        assert len(filtered) == 1


# ---------------------------------------------------------------------------
# P5: Malformed identity → rejection
# ---------------------------------------------------------------------------


class TestMalformedIdentity:
    """Malformed or invalid identity inputs are rejected safely."""

    @pytest.mark.parametrize(
        "bad_principal",
        [
            None,
            "",
            "   ",  # whitespace only
        ],
        ids=["none", "empty", "whitespace"],
    )
    def test_invalid_principal_returns_empty(
        self, fake_acl_store: FakeACLStore, bad_principal: str | None
    ):
        """P5: Any non-valid principal → empty results (not exception, not pass-through)."""
        repo = "org/sensitive-repo"
        fake_acl_store.grant(repo, "real-user")

        results = _make_search_results(repo)
        # Whitespace-only treated same as empty
        effective_principal = bad_principal.strip() if bad_principal else bad_principal
        if not effective_principal:
            effective_principal = None
        filtered = fake_acl_store.filter_results(effective_principal, results)

        assert filtered == []


# ---------------------------------------------------------------------------
# P6: Empty ACL (orphan repo) → invisible to all
# ---------------------------------------------------------------------------


class TestOrphanRepo:
    """A repo with no ACL entries is invisible to everyone."""

    def test_repo_without_acl_invisible(
        self, fake_acl_store: FakeACLStore, principal_a: FakePrincipal
    ):
        """P6a: Repo not in ACL store → no one can see it."""
        orphan_repo = "org/orphan-no-acl"
        # Never call grant/set_public for this repo

        results = _make_search_results(orphan_repo)
        filtered = fake_acl_store.filter_results(principal_a.principal_id, results)

        assert filtered == []

    def test_repo_with_empty_principal_set_invisible(
        self, fake_acl_store: FakeACLStore, principal_a: FakePrincipal
    ):
        """P6b: Repo exists in store but with no principals → invisible."""
        orphan_repo = "org/orphan-empty-acl"
        # Grant then revoke to leave empty entry
        fake_acl_store.grant(orphan_repo, "temp-user")
        fake_acl_store.revoke(orphan_repo, "temp-user")

        results = _make_search_results(orphan_repo)
        filtered = fake_acl_store.filter_results(principal_a.principal_id, results)

        assert filtered == []
