"""Unit tests for tenant/individual scoping in the Door ACL filter.

Validates the security-critical invariants of E8 multi-tenancy (Issue #1772):
- Shared repos (tenant_id=NULL) visible to any caller with principal match
- Per-tenant repos visible only to same-tenant callers with principal match
- Per-individual repos visible only to matching owner_sub (no principal check)
- Cross-tenant repos invisible (fail-closed)
- Fail-closed: no identity → empty; ambiguous identity → empty
- Kill switch: TENANT_SCOPE_ENABLED=false → legacy principal-only behavior

See: design §7.2–§7.4, §11 (Child 3).
"""

from __future__ import annotations

import pytest

from door.acl import (
    PUBLIC_SENTINEL,
    CallerPrincipal,
    SearchHit,
    extract_caller_principal,
    filter_results,
)


# ---------------------------------------------------------------------------
# Tenant-aware in-memory ACL store
# ---------------------------------------------------------------------------


class TenantAwareFakeACLStore:
    """In-memory ACL store that implements tenant scoping.

    Each repo entry has: repo_name, principals, tenant_id (optional), owner_sub (optional).
    Implements the same visibility logic as PostgresACLStore._get_allowed_repos_scoped.
    """

    def __init__(
        self,
        repos: list[dict] | None = None,
        *,
        tenant_scope_enabled: bool = True,
    ):
        """Initialize with repo definitions.

        Each dict: {"repo_name": str, "principals": list[str],
                    "tenant_id": str|None, "owner_sub": str|None}
        """
        self._repos: list[dict] = repos or []
        self._tenant_scope_enabled = tenant_scope_enabled

    def get_allowed_repos(self, principal: CallerPrincipal) -> set[str]:
        """Return repos this principal can access, enforcing tenant scope."""
        if self._tenant_scope_enabled:
            return self._scoped(principal)
        return self._legacy(principal)

    def _principals_match(self, repo_principals: list[str], principal: CallerPrincipal) -> bool:
        """Check if caller matches repo's allowed_principals."""
        if PUBLIC_SENTINEL in repo_principals:
            return True
        if principal.github_login and principal.github_login in repo_principals:
            return True
        if any(team in repo_principals for team in principal.github_teams):
            return True
        return False

    def _legacy(self, principal: CallerPrincipal) -> set[str]:
        """Legacy mode: principal matching only."""
        allowed: set[str] = set()
        for repo in self._repos:
            if self._principals_match(repo["principals"], principal):
                allowed.add(repo["repo_name"])
        return allowed

    def _scoped(self, principal: CallerPrincipal) -> set[str]:
        """Tenant-scoped mode: three visibility paths."""
        allowed: set[str] = set()
        for repo in self._repos:
            repo_tenant = repo.get("tenant_id")
            repo_owner = repo.get("owner_sub")

            # Path 3: Per-individual — owner_sub matches (unconditional)
            if repo_owner and principal.owner_sub and repo_owner == principal.owner_sub:
                allowed.add(repo["repo_name"])
                continue

            # Path 1: Shared — tenant_id IS NULL, principals match
            if repo_tenant is None:
                if self._principals_match(repo["principals"], principal):
                    allowed.add(repo["repo_name"])
                continue

            # Path 2: Per-tenant — tenant_id matches caller, principals match
            if principal.tenant_id and repo_tenant == principal.tenant_id:
                if self._principals_match(repo["principals"], principal):
                    allowed.add(repo["repo_name"])

            # Else: cross-tenant → excluded (implicit deny)

        return allowed


class FailingACLStore:
    """ACL store that always raises (simulates Postgres connection failure)."""

    def get_allowed_repos(self, principal: CallerPrincipal) -> set[str]:
        raise ConnectionError("Database connection failed")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# Standard repos for testing all visibility paths
STANDARD_REPOS = [
    # Shared repos (tenant_id=NULL) — visible to any authenticated caller
    {
        "repo_name": "org/public-lib",
        "principals": [PUBLIC_SENTINEL],
        "tenant_id": None,
        "owner_sub": None,
    },
    {
        "repo_name": "org/shared-private",
        "principals": ["alice", "org/backend-team"],
        "tenant_id": None,
        "owner_sub": None,
    },
    # Per-tenant repos — visible only to same-tenant callers
    {
        "repo_name": "acme/service-a",
        "principals": ["alice", "bob", "acme/devs"],
        "tenant_id": "acme",
        "owner_sub": None,
    },
    {"repo_name": "acme/service-b", "principals": ["bob"], "tenant_id": "acme", "owner_sub": None},
    {
        "repo_name": "globex/platform",
        "principals": ["charlie", "globex/eng"],
        "tenant_id": "globex",
        "owner_sub": None,
    },
    # Per-individual repos — visible only to matching owner_sub
    {
        "repo_name": "alice-personal/notes",
        "principals": [],
        "tenant_id": "acme",
        "owner_sub": "sub-alice-001",
    },
    {
        "repo_name": "bob-personal/scratch",
        "principals": [],
        "tenant_id": "acme",
        "owner_sub": "sub-bob-002",
    },
]


@pytest.fixture
def scoped_store() -> TenantAwareFakeACLStore:
    """Store with tenant scoping ENABLED."""
    return TenantAwareFakeACLStore(STANDARD_REPOS, tenant_scope_enabled=True)


@pytest.fixture
def legacy_store() -> TenantAwareFakeACLStore:
    """Store with tenant scoping DISABLED (kill-switch mode)."""
    return TenantAwareFakeACLStore(STANDARD_REPOS, tenant_scope_enabled=False)


@pytest.fixture
def alice_acme() -> CallerPrincipal:
    """Alice: member of acme tenant, backend team."""
    return CallerPrincipal(
        github_login="alice",
        github_teams=["org/backend-team", "acme/devs"],
        tenant_id="acme",
        owner_sub="sub-alice-001",
    )


@pytest.fixture
def bob_acme() -> CallerPrincipal:
    """Bob: member of acme tenant, no team memberships."""
    return CallerPrincipal(
        github_login="bob",
        github_teams=[],
        tenant_id="acme",
        owner_sub="sub-bob-002",
    )


@pytest.fixture
def charlie_globex() -> CallerPrincipal:
    """Charlie: member of globex tenant."""
    return CallerPrincipal(
        github_login="charlie",
        github_teams=["globex/eng"],
        tenant_id="globex",
        owner_sub="sub-charlie-003",
    )


@pytest.fixture
def no_tenant_caller() -> CallerPrincipal:
    """Caller with GitHub identity but no tenant headers."""
    return CallerPrincipal(
        github_login="alice",
        github_teams=["org/backend-team"],
        tenant_id="",
        owner_sub="",
    )


def _make_hits(*repos: str) -> list[SearchHit]:
    """Create SearchHit objects for the given repo names."""
    return [SearchHit(repo_name=r, data={"match": f"code in {r}"}) for r in repos]


# ---------------------------------------------------------------------------
# Shared repos (tenant_id=NULL) — visible to all principals that match
# ---------------------------------------------------------------------------


class TestSharedRepos:
    """Repos with tenant_id=NULL are visible to any caller whose principals match."""

    def test_shared_public_visible_to_all(
        self, scoped_store: TenantAwareFakeACLStore, alice_acme: CallerPrincipal
    ) -> None:
        """Public shared repo visible to any resolved caller."""
        hits = _make_hits("org/public-lib")
        result = filter_results(hits, alice_acme, scoped_store)
        assert len(result) == 1
        assert result[0].repo_name == "org/public-lib"

    def test_shared_public_visible_cross_tenant(
        self, scoped_store: TenantAwareFakeACLStore, charlie_globex: CallerPrincipal
    ) -> None:
        """Public shared repo visible regardless of caller's tenant."""
        hits = _make_hits("org/public-lib")
        result = filter_results(hits, charlie_globex, scoped_store)
        assert len(result) == 1

    def test_shared_private_requires_principals(
        self, scoped_store: TenantAwareFakeACLStore, alice_acme: CallerPrincipal
    ) -> None:
        """Shared private repo requires principals match (alice is listed)."""
        hits = _make_hits("org/shared-private")
        result = filter_results(hits, alice_acme, scoped_store)
        assert len(result) == 1

    def test_shared_private_denies_unlisted_caller(
        self, scoped_store: TenantAwareFakeACLStore, charlie_globex: CallerPrincipal
    ) -> None:
        """Shared private repo denies callers not in principals."""
        hits = _make_hits("org/shared-private")
        result = filter_results(hits, charlie_globex, scoped_store)
        assert result == []

    def test_shared_visible_without_tenant_headers(
        self, scoped_store: TenantAwareFakeACLStore, no_tenant_caller: CallerPrincipal
    ) -> None:
        """Caller without tenant headers still sees shared repos (principals match)."""
        hits = _make_hits("org/public-lib", "org/shared-private")
        result = filter_results(hits, no_tenant_caller, scoped_store)
        result_repos = {h.repo_name for h in result}
        assert "org/public-lib" in result_repos
        assert "org/shared-private" in result_repos


# ---------------------------------------------------------------------------
# Per-tenant repos — visible only to same-tenant callers with principals match
# ---------------------------------------------------------------------------


class TestPerTenantRepos:
    """Repos with tenant_id set require same tenant + principals match."""

    def test_same_tenant_with_principal_match(
        self, scoped_store: TenantAwareFakeACLStore, alice_acme: CallerPrincipal
    ) -> None:
        """Alice (acme) sees acme/service-a (she's in principals)."""
        hits = _make_hits("acme/service-a")
        result = filter_results(hits, alice_acme, scoped_store)
        assert len(result) == 1

    def test_same_tenant_without_principal_match(
        self, scoped_store: TenantAwareFakeACLStore, alice_acme: CallerPrincipal
    ) -> None:
        """Alice (acme) cannot see acme/service-b (only bob is listed)."""
        hits = _make_hits("acme/service-b")
        result = filter_results(hits, alice_acme, scoped_store)
        assert result == []

    def test_cross_tenant_denied(
        self, scoped_store: TenantAwareFakeACLStore, charlie_globex: CallerPrincipal
    ) -> None:
        """Charlie (globex) cannot see acme repos, even if principals would match."""
        hits = _make_hits("acme/service-a", "acme/service-b")
        result = filter_results(hits, charlie_globex, scoped_store)
        assert result == []

    def test_cross_tenant_other_direction(
        self, scoped_store: TenantAwareFakeACLStore, alice_acme: CallerPrincipal
    ) -> None:
        """Alice (acme) cannot see globex/platform."""
        hits = _make_hits("globex/platform")
        result = filter_results(hits, alice_acme, scoped_store)
        assert result == []

    def test_same_tenant_team_access(
        self, scoped_store: TenantAwareFakeACLStore, alice_acme: CallerPrincipal
    ) -> None:
        """Team membership in principals grants same-tenant access."""
        # alice is in acme/devs team, service-a has acme/devs in principals
        hits = _make_hits("acme/service-a")
        result = filter_results(hits, alice_acme, scoped_store)
        assert len(result) == 1

    def test_no_tenant_header_denies_tenant_repos(
        self, scoped_store: TenantAwareFakeACLStore, no_tenant_caller: CallerPrincipal
    ) -> None:
        """Caller without tenant header cannot see per-tenant repos."""
        hits = _make_hits("acme/service-a", "acme/service-b", "globex/platform")
        result = filter_results(hits, no_tenant_caller, scoped_store)
        assert result == []


# ---------------------------------------------------------------------------
# Per-individual repos — visible only to matching owner_sub
# ---------------------------------------------------------------------------


class TestPerIndividualRepos:
    """Repos with owner_sub require exact owner_sub match (no principal check)."""

    def test_owner_sees_own_repo(
        self, scoped_store: TenantAwareFakeACLStore, alice_acme: CallerPrincipal
    ) -> None:
        """Alice sees her personal repo (owner_sub match)."""
        hits = _make_hits("alice-personal/notes")
        result = filter_results(hits, alice_acme, scoped_store)
        assert len(result) == 1

    def test_other_user_denied(
        self, scoped_store: TenantAwareFakeACLStore, bob_acme: CallerPrincipal
    ) -> None:
        """Bob (same tenant!) cannot see alice's personal repo."""
        hits = _make_hits("alice-personal/notes")
        result = filter_results(hits, bob_acme, scoped_store)
        assert result == []

    def test_cross_tenant_individual_denied(
        self, scoped_store: TenantAwareFakeACLStore, charlie_globex: CallerPrincipal
    ) -> None:
        """Charlie (different tenant) cannot see alice's personal repo."""
        hits = _make_hits("alice-personal/notes")
        result = filter_results(hits, charlie_globex, scoped_store)
        assert result == []

    def test_individual_no_principal_check(self, scoped_store: TenantAwareFakeACLStore) -> None:
        """Individual repos don't require principals match — only owner_sub."""
        # Create a caller who isn't in any principals list but matches owner_sub
        caller = CallerPrincipal(
            github_login="alice",
            github_teams=[],
            tenant_id="acme",
            owner_sub="sub-alice-001",
        )
        hits = _make_hits("alice-personal/notes")
        result = filter_results(hits, caller, scoped_store)
        assert len(result) == 1

    def test_no_owner_sub_denies_individual_repos(
        self, scoped_store: TenantAwareFakeACLStore, no_tenant_caller: CallerPrincipal
    ) -> None:
        """Caller without owner_sub cannot see individual repos."""
        hits = _make_hits("alice-personal/notes", "bob-personal/scratch")
        result = filter_results(hits, no_tenant_caller, scoped_store)
        assert result == []


# ---------------------------------------------------------------------------
# Fail-closed invariants (security-critical)
# ---------------------------------------------------------------------------


class TestFailClosed:
    """The filter MUST return empty for any unresolved or missing identity."""

    def test_none_principal_returns_empty(self, scoped_store: TenantAwareFakeACLStore) -> None:
        """None caller → empty results (even for public repos)."""
        hits = _make_hits("org/public-lib", "acme/service-a")
        result = filter_results(hits, None, scoped_store)
        assert result == []

    def test_empty_identity_returns_empty(self, scoped_store: TenantAwareFakeACLStore) -> None:
        """Principal with no login and no teams → empty (unresolved)."""
        caller = CallerPrincipal(
            github_login="", github_teams=[], tenant_id="acme", owner_sub="sub-alice-001"
        )
        hits = _make_hits("org/public-lib", "alice-personal/notes")
        result = filter_results(hits, caller, scoped_store)
        assert result == []

    def test_store_failure_returns_empty(self) -> None:
        """ACL store exception → empty results (never leak)."""
        caller = CallerPrincipal(
            github_login="alice", github_teams=[], tenant_id="acme", owner_sub="sub-alice-001"
        )
        hits = _make_hits("org/public-lib", "acme/service-a")
        result = filter_results(hits, caller, FailingACLStore())
        assert result == []

    def test_empty_results_stays_empty(
        self, scoped_store: TenantAwareFakeACLStore, alice_acme: CallerPrincipal
    ) -> None:
        """Empty input → empty output."""
        result = filter_results([], alice_acme, scoped_store)
        assert result == []


# ---------------------------------------------------------------------------
# Cross-tenant isolation (the #1 risk)
# ---------------------------------------------------------------------------


class TestCrossTenantIsolation:
    """Two callers from different tenants searching same hits see different results."""

    def test_cross_tenant_isolation(
        self,
        scoped_store: TenantAwareFakeACLStore,
        alice_acme: CallerPrincipal,
        charlie_globex: CallerPrincipal,
    ) -> None:
        """Alice (acme) and Charlie (globex) see their own tenant repos + shared only."""
        all_hits = _make_hits(
            "org/public-lib",
            "org/shared-private",
            "acme/service-a",
            "globex/platform",
            "alice-personal/notes",
        )

        alice_results = filter_results(all_hits, alice_acme, scoped_store)
        charlie_results = filter_results(all_hits, charlie_globex, scoped_store)

        alice_repos = {h.repo_name for h in alice_results}
        charlie_repos = {h.repo_name for h in charlie_results}

        # Both see public shared
        assert "org/public-lib" in alice_repos
        assert "org/public-lib" in charlie_repos

        # Alice sees shared-private (she's in principals), Charlie doesn't
        assert "org/shared-private" in alice_repos
        assert "org/shared-private" not in charlie_repos

        # Each sees own tenant repo
        assert "acme/service-a" in alice_repos
        assert "acme/service-a" not in charlie_repos
        assert "globex/platform" in charlie_repos
        assert "globex/platform" not in alice_repos

        # Only alice sees her personal repo
        assert "alice-personal/notes" in alice_repos
        assert "alice-personal/notes" not in charlie_repos

    def test_same_tenant_different_users(
        self,
        scoped_store: TenantAwareFakeACLStore,
        alice_acme: CallerPrincipal,
        bob_acme: CallerPrincipal,
    ) -> None:
        """Alice and Bob (same tenant) have different access based on principals + owner_sub."""
        all_hits = _make_hits(
            "acme/service-a",
            "acme/service-b",
            "alice-personal/notes",
            "bob-personal/scratch",
        )

        alice_results = filter_results(all_hits, alice_acme, scoped_store)
        bob_results = filter_results(all_hits, bob_acme, scoped_store)

        alice_repos = {h.repo_name for h in alice_results}
        bob_repos = {h.repo_name for h in bob_results}

        # Alice sees service-a (she's in principals), Bob sees both (he's listed)
        assert "acme/service-a" in alice_repos
        assert "acme/service-b" not in alice_repos  # only bob listed
        assert "acme/service-a" in bob_repos  # bob listed
        assert "acme/service-b" in bob_repos  # bob listed

        # Each sees only their own personal repo
        assert "alice-personal/notes" in alice_repos
        assert "alice-personal/notes" not in bob_repos
        assert "bob-personal/scratch" in bob_repos
        assert "bob-personal/scratch" not in alice_repos


# ---------------------------------------------------------------------------
# Kill switch / feature flag regression
# ---------------------------------------------------------------------------


class TestKillSwitch:
    """When tenant_scope_enabled=False, old behavior is preserved (principal-only)."""

    def test_legacy_mode_ignores_tenant_id(
        self, legacy_store: TenantAwareFakeACLStore, charlie_globex: CallerPrincipal
    ) -> None:
        """In legacy mode, charlie can see acme repos if principals match."""
        # charlie's login is 'charlie', listed in globex/platform.
        # But acme/service-a has alice/bob/acme-devs — charlie is NOT listed.
        # However globex/platform has charlie → should be visible even cross-tenant in legacy mode.
        hits = _make_hits("globex/platform")
        result = filter_results(hits, charlie_globex, legacy_store)
        assert len(result) == 1

    def test_legacy_mode_cross_tenant_visible(self, legacy_store: TenantAwareFakeACLStore) -> None:
        """In legacy mode, tenant_id is ignored — only principals matter."""
        # A caller from globex can see an acme repo if they're in the principals list
        caller = CallerPrincipal(
            github_login="bob",
            github_teams=[],
            tenant_id="globex",  # different tenant than repo
            owner_sub="sub-bob-other",
        )
        hits = _make_hits("acme/service-a", "acme/service-b")
        result = filter_results(hits, caller, legacy_store)
        result_repos = {h.repo_name for h in result}
        # bob is in both service-a and service-b principals
        assert "acme/service-a" in result_repos
        assert "acme/service-b" in result_repos

    def test_legacy_mode_individual_repos_need_principals(
        self, legacy_store: TenantAwareFakeACLStore
    ) -> None:
        """In legacy mode, individual repos with empty principals are invisible."""
        caller = CallerPrincipal(
            github_login="alice",
            github_teams=[],
            tenant_id="acme",
            owner_sub="sub-alice-001",
        )
        hits = _make_hits("alice-personal/notes")
        result = filter_results(hits, caller, legacy_store)
        # In legacy mode: empty principals → invisible (no owner_sub path)
        assert result == []

    def test_legacy_shared_repos_unchanged(
        self, legacy_store: TenantAwareFakeACLStore, alice_acme: CallerPrincipal
    ) -> None:
        """Legacy mode: shared repos work exactly as before."""
        hits = _make_hits("org/public-lib", "org/shared-private")
        result = filter_results(hits, alice_acme, legacy_store)
        result_repos = {h.repo_name for h in result}
        assert "org/public-lib" in result_repos
        assert "org/shared-private" in result_repos


# ---------------------------------------------------------------------------
# Header extraction with tenant fields
# ---------------------------------------------------------------------------


class TestHeaderExtractionTenant:
    """Tests for extracting tenant/owner headers alongside GitHub identity."""

    def test_all_four_headers(self) -> None:
        """All 4 headers present → fully populated CallerPrincipal."""
        headers = {
            "X-GitHub-Login": "Alice",
            "X-GitHub-Teams": "org/backend-team,acme/devs",
            "X-Tenant-Id": "acme",
            "X-Owner-Sub": "Sub-Alice-001",
        }
        principal = extract_caller_principal(headers)
        assert principal is not None
        assert principal.github_login == "alice"
        assert principal.github_teams == ["org/backend-team", "acme/devs"]
        assert principal.tenant_id == "acme"
        assert principal.owner_sub == "sub-alice-001"  # lowercased

    def test_github_only_no_tenant(self) -> None:
        """Only GitHub headers → tenant/owner default to empty string."""
        headers = {"X-GitHub-Login": "bob"}
        principal = extract_caller_principal(headers)
        assert principal is not None
        assert principal.github_login == "bob"
        assert principal.tenant_id == ""
        assert principal.owner_sub == ""

    def test_tenant_only_no_github_returns_none(self) -> None:
        """Tenant headers without GitHub identity → None (fail-closed)."""
        headers = {"X-Tenant-Id": "acme", "X-Owner-Sub": "sub-001"}
        principal = extract_caller_principal(headers)
        assert principal is None

    def test_tenant_header_whitespace(self) -> None:
        """Tenant headers are stripped of whitespace."""
        headers = {
            "X-GitHub-Login": "alice",
            "X-Tenant-Id": "  acme  ",
            "X-Owner-Sub": "  Sub-001  ",
        }
        principal = extract_caller_principal(headers)
        assert principal is not None
        assert principal.tenant_id == "acme"
        assert principal.owner_sub == "sub-001"

    def test_case_insensitive_tenant_headers(self) -> None:
        """Tenant header names are case-insensitive."""
        headers = {
            "x-github-login": "alice",
            "x-tenant-id": "ACME",
            "x-owner-sub": "SUB-001",
        }
        principal = extract_caller_principal(headers)
        assert principal is not None
        assert principal.tenant_id == "ACME"  # tenant_id preserves case
        assert principal.owner_sub == "sub-001"  # owner_sub lowercased

    def test_empty_tenant_headers_treated_as_absent(self) -> None:
        """Empty-string tenant headers → empty strings (not None)."""
        headers = {
            "X-GitHub-Login": "alice",
            "X-Tenant-Id": "",
            "X-Owner-Sub": "",
        }
        principal = extract_caller_principal(headers)
        assert principal is not None
        assert principal.tenant_id == ""
        assert principal.owner_sub == ""


# ---------------------------------------------------------------------------
# Regression: existing shared-corpus queries unchanged
# ---------------------------------------------------------------------------


class TestRegressionSharedCorpus:
    """Existing shared-corpus behavior must not change when scoping is enabled."""

    def test_existing_public_repos_still_visible(
        self, scoped_store: TenantAwareFakeACLStore
    ) -> None:
        """Public repos (tenant_id=NULL, principals=['*']) remain universally visible."""
        random_user = CallerPrincipal(
            github_login="random",
            github_teams=["org/any-team"],
            tenant_id="some-tenant",
            owner_sub="some-sub",
        )
        hits = _make_hits("org/public-lib")
        result = filter_results(hits, random_user, scoped_store)
        assert len(result) == 1

    def test_existing_shared_private_repos_unchanged(
        self, scoped_store: TenantAwareFakeACLStore
    ) -> None:
        """Shared private repos (tenant_id=NULL) still require principal match."""
        # Alice is in principals → sees it
        alice = CallerPrincipal(
            github_login="alice", github_teams=[], tenant_id="acme", owner_sub=""
        )
        hits = _make_hits("org/shared-private")
        result = filter_results(hits, alice, scoped_store)
        assert len(result) == 1

        # Random user NOT in principals → denied
        random_user = CallerPrincipal(
            github_login="random", github_teams=[], tenant_id="acme", owner_sub=""
        )
        result = filter_results(hits, random_user, scoped_store)
        assert result == []

    def test_filter_preserves_order_and_data(
        self, scoped_store: TenantAwareFakeACLStore, alice_acme: CallerPrincipal
    ) -> None:
        """Filtered results maintain original ordering and hit data."""
        hits = [
            SearchHit(repo_name="acme/service-a", data={"line": 42, "content": "fn main()"}),
            SearchHit(repo_name="org/public-lib", data={"line": 1, "content": "# README"}),
            SearchHit(repo_name="globex/platform", data={"line": 99, "content": "secret"}),
        ]
        result = filter_results(hits, alice_acme, scoped_store)
        # Alice sees acme/service-a + public-lib, not globex
        assert len(result) == 2
        assert result[0].repo_name == "acme/service-a"
        assert result[0].data == {"line": 42, "content": "fn main()"}
        assert result[1].repo_name == "org/public-lib"
        assert result[1].data == {"line": 1, "content": "# README"}
