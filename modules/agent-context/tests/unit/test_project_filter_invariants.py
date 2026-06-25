"""Story F invariant tests for the Door project filter (E9 #1728, #1789).

Validates the *soft-scope invariants* — the properties that guarantee the
project filter model is correct and safe:

1. Soft narrowing: project filter can only RESTRICT results (subset semantics)
2. M:N correctness: a repo in 2 projects shows in both when queried separately
3. Ownership invariant: cross-owner references blocked (personal + team)
4. Ungrouped passthrough: no project param = all ACL-visible repos returned
5. ACL-still-enforced: project scope never widens beyond what ACL allows

See: design-1728-project-scoping.md §11 (Issue F).
"""

from __future__ import annotations

import uuid

import pytest

from door.acl import SearchHit
from door.project_filter import (
    ProjectFilterError,
    ProjectScope,
    apply_project_filter,
    resolve_project_repos,
)


# ---------------------------------------------------------------------------
# Fake database pool (reused from test_project_filter.py pattern)
# ---------------------------------------------------------------------------


class FakeDBPool:
    """In-memory fake for psycopg2 connection pool."""

    def __init__(
        self,
        projects: list[dict] | None = None,
        project_repos: list[dict] | None = None,
    ):
        self._projects = projects or []
        self._project_repos = project_repos or []
        self._conn = _FakeConnection(self._projects, self._project_repos)

    def getconn(self):
        return self._conn

    def putconn(self, conn):
        pass


class _FakeConnection:
    def __init__(self, projects: list[dict], project_repos: list[dict]):
        self._projects = projects
        self._project_repos = project_repos

    def cursor(self):
        return _FakeCursorContext(self._projects, self._project_repos)


class _FakeCursorContext:
    def __init__(self, projects: list[dict], project_repos: list[dict]):
        self._cursor = _FakeCursor(projects, project_repos)

    def __enter__(self):
        return self._cursor

    def __exit__(self, *args):
        pass


class _FakeCursor:
    def __init__(self, projects: list[dict], project_repos: list[dict]):
        self._projects = projects
        self._project_repos = project_repos
        self._results: list[tuple] = []

    def execute(self, query: str, params: tuple = ()) -> None:
        if "FROM projects" in query and "id = %s" in query:
            project_uuid = params[0]
            caller_owner_sub = params[1]
            caller_tenant_id = params[2]
            self._results = []
            for p in self._projects:
                if p["id"] == project_uuid:
                    if p.get("tenant_id") is None and p["owner_sub"] == caller_owner_sub:
                        self._results.append((p["id"],))
                    elif p.get("tenant_id") is not None and p["tenant_id"] == caller_tenant_id:
                        self._results.append((p["id"],))

        elif "FROM projects" in query and "name = %s" in query:
            project_name = params[0]
            caller_owner_sub = params[1]
            self._results = []
            for p in self._projects:
                if p["name"] == project_name and p["owner_sub"] == caller_owner_sub:
                    self._results.append((p["id"],))

        elif "FROM project_repositories" in query:
            project_id = params[0]
            self._results = []
            for pr in self._project_repos:
                if pr["project_id"] == project_id:
                    self._results.append((pr["repo_name"],))

    def fetchone(self) -> tuple | None:
        return self._results[0] if self._results else None

    def fetchall(self) -> list[tuple]:
        return self._results


# ---------------------------------------------------------------------------
# Test constants
# ---------------------------------------------------------------------------

ALICE_SUB = "sub-alice-invariant-001"
BOB_SUB = "sub-bob-invariant-002"
CAROL_SUB = "sub-carol-invariant-003"
TENANT_ACME = "acme-corp"
TENANT_GLOBEX = "globex-inc"

# Projects
PROJECT_FRONTEND = str(uuid.uuid4())
PROJECT_BACKEND = str(uuid.uuid4())
PROJECT_FULLSTACK = str(uuid.uuid4())  # overlaps with both
PROJECT_TEAM_SHARED = str(uuid.uuid4())  # team project (ACME)
PROJECT_EMPTY = str(uuid.uuid4())  # project with zero repos

# Repos
REPO_UI = "org/ui-components"
REPO_API = "org/api-server"
REPO_SHARED_LIB = "org/shared-lib"  # in BOTH frontend + backend projects (M:N)
REPO_INFRA = "org/infra"
REPO_SECRET = "org/secret-ops"  # not in any project, ACL-only


def _make_hits(*repos: str) -> list[SearchHit]:
    """Create SearchHit objects for given repo names."""
    return [SearchHit(repo_name=r, data={"file": "src/main.py", "line": 1}) for r in repos]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mn_pool() -> FakeDBPool:
    """DB pool configured for M:N testing.

    Repo layout:
    - PROJECT_FRONTEND: ui-components, shared-lib
    - PROJECT_BACKEND: api-server, shared-lib
    - PROJECT_FULLSTACK: ui-components, api-server, shared-lib, infra
    - PROJECT_TEAM_SHARED: shared-lib, infra (team project, ACME tenant)
    - PROJECT_EMPTY: (no repos)

    Note: shared-lib is in 3 projects (M:N relationship).
    """
    projects = [
        {
            "id": PROJECT_FRONTEND,
            "owner_sub": ALICE_SUB,
            "name": "frontend",
            "tenant_id": None,
        },
        {
            "id": PROJECT_BACKEND,
            "owner_sub": ALICE_SUB,
            "name": "backend",
            "tenant_id": None,
        },
        {
            "id": PROJECT_FULLSTACK,
            "owner_sub": ALICE_SUB,
            "name": "fullstack",
            "tenant_id": None,
        },
        {
            "id": PROJECT_TEAM_SHARED,
            "owner_sub": ALICE_SUB,
            "name": "team-shared",
            "tenant_id": TENANT_ACME,
        },
        {
            "id": PROJECT_EMPTY,
            "owner_sub": ALICE_SUB,
            "name": "empty-project",
            "tenant_id": None,
        },
    ]
    project_repos = [
        # Frontend project repos
        {"project_id": PROJECT_FRONTEND, "repo_name": REPO_UI},
        {"project_id": PROJECT_FRONTEND, "repo_name": REPO_SHARED_LIB},
        # Backend project repos
        {"project_id": PROJECT_BACKEND, "repo_name": REPO_API},
        {"project_id": PROJECT_BACKEND, "repo_name": REPO_SHARED_LIB},
        # Fullstack project repos (superset)
        {"project_id": PROJECT_FULLSTACK, "repo_name": REPO_UI},
        {"project_id": PROJECT_FULLSTACK, "repo_name": REPO_API},
        {"project_id": PROJECT_FULLSTACK, "repo_name": REPO_SHARED_LIB},
        {"project_id": PROJECT_FULLSTACK, "repo_name": REPO_INFRA},
        # Team shared project repos
        {"project_id": PROJECT_TEAM_SHARED, "repo_name": REPO_SHARED_LIB},
        {"project_id": PROJECT_TEAM_SHARED, "repo_name": REPO_INFRA},
        # PROJECT_EMPTY: deliberately no entries
    ]
    return FakeDBPool(projects=projects, project_repos=project_repos)


@pytest.fixture
def bob_pool() -> FakeDBPool:
    """DB pool with Bob's projects (separate from Alice's)."""
    bob_project_id = str(uuid.uuid4())
    return FakeDBPool(
        projects=[
            {
                "id": bob_project_id,
                "owner_sub": BOB_SUB,
                "name": "bob-private",
                "tenant_id": None,
            },
        ],
        project_repos=[
            {"project_id": bob_project_id, "repo_name": REPO_API},
            {"project_id": bob_project_id, "repo_name": REPO_INFRA},
        ],
    )


# ---------------------------------------------------------------------------
# 1. Soft narrowing invariant
# ---------------------------------------------------------------------------


class TestSoftNarrowing:
    """Project filter can only NARROW results — never add repos not in hits.

    Invariant: result ⊆ hits (set intersection semantics).
    """

    def test_result_is_subset_of_input_hits(self, mn_pool: FakeDBPool) -> None:
        """Filtered result is always a subset of the input hits."""
        scope = resolve_project_repos(PROJECT_FRONTEND, ALICE_SUB, mn_pool)
        all_hits = _make_hits(REPO_UI, REPO_API, REPO_SHARED_LIB, REPO_INFRA, REPO_SECRET)
        result = apply_project_filter(all_hits, scope)

        result_repos = {h.repo_name for h in result}
        input_repos = {h.repo_name for h in all_hits}
        assert result_repos <= input_repos, "Filter must never add repos not in input"

    def test_project_repos_not_in_hits_are_excluded(self, mn_pool: FakeDBPool) -> None:
        """Project repos absent from hits do NOT appear in results."""
        # Frontend project has ui-components and shared-lib
        # But hits only contain api-server → result should be empty
        scope = resolve_project_repos(PROJECT_FRONTEND, ALICE_SUB, mn_pool)
        hits = _make_hits(REPO_API, REPO_INFRA)  # neither in frontend project
        result = apply_project_filter(hits, scope)
        assert result == [], "Repos in project but not in hits must not appear"

    def test_narrowing_never_exceeds_hit_count(self, mn_pool: FakeDBPool) -> None:
        """Result length is always ≤ input length."""
        scope = resolve_project_repos(PROJECT_FULLSTACK, ALICE_SUB, mn_pool)
        hits = _make_hits(REPO_UI, REPO_API)
        result = apply_project_filter(hits, scope)
        assert len(result) <= len(hits)

    def test_empty_project_always_returns_empty(self, mn_pool: FakeDBPool) -> None:
        """A project with zero repos → always empty result (∩ with ∅ = ∅)."""
        scope = resolve_project_repos(PROJECT_EMPTY, ALICE_SUB, mn_pool)
        hits = _make_hits(REPO_UI, REPO_API, REPO_SHARED_LIB)
        result = apply_project_filter(hits, scope)
        assert result == [], "Intersection with empty project must be empty"

    def test_disjoint_project_and_hits_yields_empty(self, mn_pool: FakeDBPool) -> None:
        """Completely disjoint project repos and hits → empty."""
        # Backend has api-server, shared-lib; hits have only ui, infra, secret
        scope = resolve_project_repos(PROJECT_BACKEND, ALICE_SUB, mn_pool)
        hits = _make_hits(REPO_UI, REPO_INFRA, REPO_SECRET)
        result = apply_project_filter(hits, scope)
        assert result == []


# ---------------------------------------------------------------------------
# 2. M:N correctness — a repo in multiple projects shows in each
# ---------------------------------------------------------------------------


class TestManyToManyCorrectness:
    """A repo belonging to N projects must appear when querying any of those N.

    Key invariant: project membership is non-exclusive. shared-lib is in
    frontend, backend, and fullstack — querying any of those returns it.
    """

    def test_shared_repo_visible_from_frontend_project(self, mn_pool: FakeDBPool) -> None:
        """shared-lib (in frontend + backend + fullstack) shows via frontend."""
        scope = resolve_project_repos(PROJECT_FRONTEND, ALICE_SUB, mn_pool)
        hits = _make_hits(REPO_SHARED_LIB, REPO_API, REPO_UI)
        result = apply_project_filter(hits, scope)
        result_repos = {h.repo_name for h in result}
        assert REPO_SHARED_LIB in result_repos

    def test_shared_repo_visible_from_backend_project(self, mn_pool: FakeDBPool) -> None:
        """shared-lib (in frontend + backend + fullstack) shows via backend."""
        scope = resolve_project_repos(PROJECT_BACKEND, ALICE_SUB, mn_pool)
        hits = _make_hits(REPO_SHARED_LIB, REPO_API, REPO_UI)
        result = apply_project_filter(hits, scope)
        result_repos = {h.repo_name for h in result}
        assert REPO_SHARED_LIB in result_repos

    def test_shared_repo_visible_from_fullstack_project(self, mn_pool: FakeDBPool) -> None:
        """shared-lib shows via the fullstack superset project."""
        scope = resolve_project_repos(PROJECT_FULLSTACK, ALICE_SUB, mn_pool)
        hits = _make_hits(REPO_SHARED_LIB, REPO_SECRET)
        result = apply_project_filter(hits, scope)
        result_repos = {h.repo_name for h in result}
        assert REPO_SHARED_LIB in result_repos

    def test_shared_repo_visible_from_team_project(self, mn_pool: FakeDBPool) -> None:
        """shared-lib shows via team project when tenant matches."""
        scope = resolve_project_repos(
            PROJECT_TEAM_SHARED, BOB_SUB, mn_pool, caller_tenant_id=TENANT_ACME
        )
        hits = _make_hits(REPO_SHARED_LIB, REPO_UI, REPO_API)
        result = apply_project_filter(hits, scope)
        result_repos = {h.repo_name for h in result}
        assert REPO_SHARED_LIB in result_repos

    def test_exclusive_repos_only_in_their_project(self, mn_pool: FakeDBPool) -> None:
        """ui-components is ONLY in frontend + fullstack, not in backend."""
        scope = resolve_project_repos(PROJECT_BACKEND, ALICE_SUB, mn_pool)
        hits = _make_hits(REPO_UI, REPO_API, REPO_SHARED_LIB)
        result = apply_project_filter(hits, scope)
        result_repos = {h.repo_name for h in result}
        # Backend only has api-server + shared-lib
        assert REPO_UI not in result_repos
        assert REPO_API in result_repos
        assert REPO_SHARED_LIB in result_repos

    def test_mn_repo_sets_are_independent(self, mn_pool: FakeDBPool) -> None:
        """Frontend and backend return different subsets despite sharing shared-lib."""
        hits = _make_hits(REPO_UI, REPO_API, REPO_SHARED_LIB, REPO_INFRA)

        fe_scope = resolve_project_repos(PROJECT_FRONTEND, ALICE_SUB, mn_pool)
        be_scope = resolve_project_repos(PROJECT_BACKEND, ALICE_SUB, mn_pool)

        fe_result = {h.repo_name for h in apply_project_filter(hits, fe_scope)}
        be_result = {h.repo_name for h in apply_project_filter(hits, be_scope)}

        # Frontend: ui + shared-lib
        assert fe_result == {REPO_UI, REPO_SHARED_LIB}
        # Backend: api + shared-lib
        assert be_result == {REPO_API, REPO_SHARED_LIB}
        # They share shared-lib but differ on the rest
        assert fe_result & be_result == {REPO_SHARED_LIB}
        assert fe_result != be_result

    def test_superset_project_includes_all_sub_repos(self, mn_pool: FakeDBPool) -> None:
        """Fullstack project (superset) includes repos from both sub-projects."""
        hits = _make_hits(REPO_UI, REPO_API, REPO_SHARED_LIB, REPO_INFRA, REPO_SECRET)
        scope = resolve_project_repos(PROJECT_FULLSTACK, ALICE_SUB, mn_pool)
        result = apply_project_filter(hits, scope)
        result_repos = {h.repo_name for h in result}
        # Fullstack has: ui, api, shared-lib, infra — all present in hits
        assert result_repos == {REPO_UI, REPO_API, REPO_SHARED_LIB, REPO_INFRA}
        # secret is NOT in the project
        assert REPO_SECRET not in result_repos


# ---------------------------------------------------------------------------
# 3. Ownership invariant — cross-owner references blocked
# ---------------------------------------------------------------------------


class TestOwnershipInvariant:
    """Cross-owner access is unconditionally blocked.

    Even if a caller knows the UUID, they cannot access another user's
    personal project. Team projects require tenant match.
    """

    def test_bob_cannot_resolve_alice_project_by_uuid(self, mn_pool: FakeDBPool) -> None:
        """Bob (different owner_sub) cannot resolve Alice's personal project."""
        with pytest.raises(ProjectFilterError) as exc_info:
            resolve_project_repos(PROJECT_FRONTEND, BOB_SUB, mn_pool)
        assert exc_info.value.code == "project_not_found"

    def test_bob_cannot_resolve_alice_project_by_name(self, mn_pool: FakeDBPool) -> None:
        """Bob cannot use Alice's project name to resolve it."""
        with pytest.raises(ProjectFilterError) as exc_info:
            resolve_project_repos("frontend", BOB_SUB, mn_pool)
        assert exc_info.value.code == "project_not_found"

    def test_wrong_tenant_cannot_access_team_project(self, mn_pool: FakeDBPool) -> None:
        """Caller from wrong tenant is blocked from team project."""
        with pytest.raises(ProjectFilterError) as exc_info:
            resolve_project_repos(
                PROJECT_TEAM_SHARED, CAROL_SUB, mn_pool, caller_tenant_id=TENANT_GLOBEX
            )
        assert exc_info.value.code == "project_not_found"

    def test_no_tenant_cannot_access_team_project(self, mn_pool: FakeDBPool) -> None:
        """Caller with empty tenant_id is blocked from team project."""
        with pytest.raises(ProjectFilterError) as exc_info:
            resolve_project_repos(PROJECT_TEAM_SHARED, CAROL_SUB, mn_pool, caller_tenant_id="")
        assert exc_info.value.code == "project_not_found"

    def test_correct_tenant_can_access_team_project(self, mn_pool: FakeDBPool) -> None:
        """Same-tenant caller CAN access team project (positive control)."""
        scope = resolve_project_repos(
            PROJECT_TEAM_SHARED, CAROL_SUB, mn_pool, caller_tenant_id=TENANT_ACME
        )
        assert scope.repo_names == frozenset({REPO_SHARED_LIB, REPO_INFRA})

    def test_owner_can_access_own_project(self, mn_pool: FakeDBPool) -> None:
        """Alice can resolve her own project (positive control)."""
        scope = resolve_project_repos(PROJECT_BACKEND, ALICE_SUB, mn_pool)
        assert REPO_API in scope.repo_names

    def test_cross_owner_enumeration_blocked_by_name(self, mn_pool: FakeDBPool) -> None:
        """Name-based resolution is scoped to caller, preventing enumeration."""
        # Alice has "frontend" project; Bob asking for "frontend" gets not-found
        with pytest.raises(ProjectFilterError):
            resolve_project_repos("frontend", BOB_SUB, mn_pool)

        # Alice CAN resolve it
        scope = resolve_project_repos("frontend", ALICE_SUB, mn_pool)
        assert scope.project_id == PROJECT_FRONTEND


# ---------------------------------------------------------------------------
# 4. Ungrouped passthrough — no project = all repos pass
# ---------------------------------------------------------------------------


class TestUngroupedPassthrough:
    """When no project filter is active (scope=None), all hits pass through.

    This ensures that the project filter is purely additive narrowing —
    not applying it doesn't break the pipeline.
    """

    def test_none_scope_returns_all_hits(self) -> None:
        """None scope = passthrough, all hits returned unchanged."""
        hits = _make_hits(REPO_UI, REPO_API, REPO_SHARED_LIB, REPO_INFRA, REPO_SECRET)
        result = apply_project_filter(hits, None)
        assert len(result) == 5
        assert result == hits

    def test_none_scope_preserves_order(self) -> None:
        """Passthrough preserves original hit ordering."""
        hits = _make_hits(REPO_SECRET, REPO_INFRA, REPO_UI)
        result = apply_project_filter(hits, None)
        assert [h.repo_name for h in result] == [REPO_SECRET, REPO_INFRA, REPO_UI]

    def test_none_scope_preserves_data(self) -> None:
        """Passthrough preserves all hit data fields."""
        hits = [
            SearchHit(repo_name=REPO_UI, data={"file": "app.tsx", "line": 42, "score": 0.95}),
            SearchHit(repo_name=REPO_API, data={"file": "main.py", "line": 1, "score": 0.87}),
        ]
        result = apply_project_filter(hits, None)
        assert result[0].data == {"file": "app.tsx", "line": 42, "score": 0.95}
        assert result[1].data == {"file": "main.py", "line": 1, "score": 0.87}

    def test_empty_hits_passthrough(self) -> None:
        """Empty hits + None scope = empty (no crash)."""
        result = apply_project_filter([], None)
        assert result == []

    def test_passthrough_identity(self) -> None:
        """Passthrough returns the exact same list object semantics (not a copy)."""
        hits = _make_hits(REPO_UI)
        result = apply_project_filter(hits, None)
        # Exact same list returned (reference equality)
        assert result is hits


# ---------------------------------------------------------------------------
# 5. ACL-still-enforced — project never widens visibility
# ---------------------------------------------------------------------------


class TestACLStillEnforced:
    """Project filter applied AFTER ACL → can never widen what ACL restricted.

    Pipeline order: ACL filter → project filter (intersection).
    Even if a project contains a repo, if ACL excluded it from hits,
    project filter cannot bring it back.
    """

    def test_project_cannot_resurrect_acl_denied_repo(self, mn_pool: FakeDBPool) -> None:
        """Repo in project but removed by ACL stays excluded.

        Scenario: fullstack project has infra, but ACL denied infra access.
        Hits from ACL: [ui, api, shared-lib] (no infra).
        Project filter: fullstack has [ui, api, shared-lib, infra].
        Result: infra is NOT in output (ACL won).
        """
        scope = resolve_project_repos(PROJECT_FULLSTACK, ALICE_SUB, mn_pool)
        # Simulate ACL already excluded REPO_INFRA from hits
        acl_filtered_hits = _make_hits(REPO_UI, REPO_API, REPO_SHARED_LIB)
        result = apply_project_filter(acl_filtered_hits, scope)
        result_repos = {h.repo_name for h in result}
        # infra is in the project but not in ACL hits → must not appear
        assert REPO_INFRA not in result_repos

    def test_project_result_always_subset_of_acl_result(self, mn_pool: FakeDBPool) -> None:
        """Final result ⊆ ACL result (formal subset property).

        For any project scope, the output of apply_project_filter must be
        a subset of the input hits (which are already ACL-filtered).
        """
        acl_hits = _make_hits(REPO_UI, REPO_API, REPO_SHARED_LIB)

        for project_id in [
            PROJECT_FRONTEND,
            PROJECT_BACKEND,
            PROJECT_FULLSTACK,
            PROJECT_EMPTY,
        ]:
            scope = resolve_project_repos(project_id, ALICE_SUB, mn_pool)
            result = apply_project_filter(acl_hits, scope)
            result_repos = {h.repo_name for h in result}
            acl_repos = {h.repo_name for h in acl_hits}
            assert result_repos <= acl_repos, (
                f"Project {project_id}: result must be subset of ACL hits"
            )

    def test_acl_empty_means_project_empty(self) -> None:
        """If ACL returns no hits, project filter gets nothing → empty.

        Even a project with 100 repos cannot produce results if ACL is empty.
        """
        scope = ProjectScope(
            project_id="big-project",
            repo_names=frozenset({REPO_UI, REPO_API, REPO_SHARED_LIB, REPO_INFRA, REPO_SECRET}),
        )
        # ACL returned nothing (fail-closed or user has no access)
        result = apply_project_filter([], scope)
        assert result == []

    def test_acl_single_repo_project_has_more(self, mn_pool: FakeDBPool) -> None:
        """ACL only allows 1 repo; project has 4 repos → only 1 in result.

        Proves project cannot widen beyond ACL even partially.
        """
        scope = resolve_project_repos(PROJECT_FULLSTACK, ALICE_SUB, mn_pool)
        # ACL only passed through shared-lib (everything else denied)
        acl_hits = _make_hits(REPO_SHARED_LIB)
        result = apply_project_filter(acl_hits, scope)
        assert len(result) == 1
        assert result[0].repo_name == REPO_SHARED_LIB

    def test_combined_pipeline_narrows_both_ways(self, mn_pool: FakeDBPool) -> None:
        """Both ACL and project filter contribute to narrowing.

        ACL allows: ui, api, shared-lib, secret
        Project (frontend) has: ui, shared-lib
        Result: ui, shared-lib (intersection of both constraints)
        """
        scope = resolve_project_repos(PROJECT_FRONTEND, ALICE_SUB, mn_pool)
        acl_hits = _make_hits(REPO_UI, REPO_API, REPO_SHARED_LIB, REPO_SECRET)
        result = apply_project_filter(acl_hits, scope)
        result_repos = {h.repo_name for h in result}
        assert result_repos == {REPO_UI, REPO_SHARED_LIB}

    def test_monotonic_narrowing_property(self, mn_pool: FakeDBPool) -> None:
        """Adding more ACL-denied repos to hits cannot increase project output.

        Formally: if H1 ⊆ H2, then filter(H1, P) ⊆ filter(H2, P).
        (Monotonicity — more input can only add to output, never subtract.)
        """
        scope = resolve_project_repos(PROJECT_FRONTEND, ALICE_SUB, mn_pool)

        # H1: smaller set
        h1 = _make_hits(REPO_UI)
        r1 = {h.repo_name for h in apply_project_filter(h1, scope)}

        # H2: superset of H1
        h2 = _make_hits(REPO_UI, REPO_API, REPO_SHARED_LIB, REPO_INFRA)
        r2 = {h.repo_name for h in apply_project_filter(h2, scope)}

        assert r1 <= r2, "Monotonicity violated: more input reduced output"
