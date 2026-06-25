"""Unit tests for the Door project filter (E9 #1728, Story B).

Validates:
- resolve_project_repos() narrows correctly (ownership, UUID, name lookup)
- Invalid project returns error (ProjectFilterError)
- Unspecified project = passthrough (no narrowing)
- Cross-user access blocked (ownership check)
- apply_project_filter() intersection semantics
- Feature flag gating (_resolve_project_scope)

See: design-1728-project-scoping.md §3, Issue #1785.
"""

from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest

from door.acl import CallerPrincipal, SearchHit
from door.project_filter import (
    ProjectFilterError,
    ProjectScope,
    apply_project_filter,
    resolve_project_repos,
)


# ---------------------------------------------------------------------------
# Fake database pool for testing
# ---------------------------------------------------------------------------


class FakeDBPool:
    """In-memory fake for psycopg2 connection pool.

    Simulates the projects + project_repositories + repositories tables.
    """

    def __init__(
        self,
        projects: list[dict] | None = None,
        project_repos: list[dict] | None = None,
    ):
        """Initialize with project and membership data.

        projects: [{"id": str, "owner_sub": str, "name": str, "tenant_id": str|None}]
        project_repos: [{"project_id": str, "repo_name": str}]
        """
        self._projects = projects or []
        self._project_repos = project_repos or []
        self._conn = FakeConnection(self._projects, self._project_repos)

    def getconn(self):
        return self._conn

    def putconn(self, conn):
        pass


class FakeConnection:
    """Fake connection that returns a FakeCursor."""

    def __init__(self, projects: list[dict], project_repos: list[dict]):
        self._projects = projects
        self._project_repos = project_repos

    def cursor(self):
        return FakeCursorContext(self._projects, self._project_repos)


class FakeCursorContext:
    """Context manager wrapper for FakeCursor."""

    def __init__(self, projects: list[dict], project_repos: list[dict]):
        self._cursor = FakeCursor(projects, project_repos)

    def __enter__(self):
        return self._cursor

    def __exit__(self, *args):
        pass


class FakeCursor:
    """Fake cursor that interprets SQL queries against in-memory data."""

    def __init__(self, projects: list[dict], project_repos: list[dict]):
        self._projects = projects
        self._project_repos = project_repos
        self._results: list[tuple] = []

    def execute(self, query: str, params: tuple = ()) -> None:
        """Route queries to appropriate handler based on SQL pattern."""
        if "FROM projects" in query and "id = %s" in query:
            # _resolve_by_id: look up project by UUID with ownership check
            project_uuid = params[0]
            caller_owner_sub = params[1]
            caller_tenant_id = params[2]
            self._results = []
            for p in self._projects:
                if p["id"] == project_uuid:
                    # Personal project: owner_sub must match
                    if p.get("tenant_id") is None and p["owner_sub"] == caller_owner_sub:
                        self._results.append((p["id"],))
                    # Team project: tenant must match
                    elif p.get("tenant_id") is not None and p["tenant_id"] == caller_tenant_id:
                        self._results.append((p["id"],))

        elif "FROM projects" in query and "name = %s" in query:
            # _resolve_by_name: look up project by name + owner_sub
            project_name = params[0]
            caller_owner_sub = params[1]
            self._results = []
            for p in self._projects:
                if p["name"] == project_name and p["owner_sub"] == caller_owner_sub:
                    self._results.append((p["id"],))

        elif "FROM project_repositories" in query:
            # _fetch_project_repo_names: get repo names for a project
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
# Fixtures
# ---------------------------------------------------------------------------

ALICE_SUB = "sub-alice-001"
BOB_SUB = "sub-bob-002"
ACME_TENANT = "acme"

PROJECT_A_ID = str(uuid.uuid4())
PROJECT_B_ID = str(uuid.uuid4())
PROJECT_TEAM_ID = str(uuid.uuid4())


@pytest.fixture
def db_pool() -> FakeDBPool:
    """DB pool with standard test data."""
    projects = [
        {
            "id": PROJECT_A_ID,
            "owner_sub": ALICE_SUB,
            "name": "client-A",
            "tenant_id": None,
        },
        {
            "id": PROJECT_B_ID,
            "owner_sub": BOB_SUB,
            "name": "bob-project",
            "tenant_id": None,
        },
        {
            "id": PROJECT_TEAM_ID,
            "owner_sub": ALICE_SUB,
            "name": "team-project",
            "tenant_id": ACME_TENANT,
        },
    ]
    project_repos = [
        {"project_id": PROJECT_A_ID, "repo_name": "org/repo-1"},
        {"project_id": PROJECT_A_ID, "repo_name": "org/repo-2"},
        {"project_id": PROJECT_B_ID, "repo_name": "org/repo-3"},
        {"project_id": PROJECT_B_ID, "repo_name": "org/repo-1"},
        {"project_id": PROJECT_TEAM_ID, "repo_name": "org/repo-1"},
        {"project_id": PROJECT_TEAM_ID, "repo_name": "org/repo-4"},
    ]
    return FakeDBPool(projects=projects, project_repos=project_repos)


@pytest.fixture
def empty_db_pool() -> FakeDBPool:
    """DB pool with no projects."""
    return FakeDBPool(projects=[], project_repos=[])


@pytest.fixture
def alice_caller() -> CallerPrincipal:
    """Alice: owns PROJECT_A_ID."""
    return CallerPrincipal(
        github_login="alice",
        github_teams=["org/backend-team"],
        tenant_id=ACME_TENANT,
        owner_sub=ALICE_SUB,
    )


@pytest.fixture
def bob_caller() -> CallerPrincipal:
    """Bob: owns PROJECT_B_ID."""
    return CallerPrincipal(
        github_login="bob",
        github_teams=[],
        tenant_id=ACME_TENANT,
        owner_sub=BOB_SUB,
    )


def _make_hits(*repos: str) -> list[SearchHit]:
    """Create SearchHit objects for given repo names."""
    return [SearchHit(repo_name=r, data={"match": f"code in {r}"}) for r in repos]


# ---------------------------------------------------------------------------
# resolve_project_repos() — happy path
# ---------------------------------------------------------------------------


class TestResolveProjectRepos:
    """Tests for resolve_project_repos() successful resolution."""

    def test_resolve_by_uuid(self, db_pool: FakeDBPool) -> None:
        """Resolves project by UUID when owner_sub matches."""
        result = resolve_project_repos(PROJECT_A_ID, ALICE_SUB, db_pool)
        assert result.project_id == PROJECT_A_ID
        assert result.repo_names == frozenset({"org/repo-1", "org/repo-2"})

    def test_resolve_by_name(self, db_pool: FakeDBPool) -> None:
        """Resolves project by name scoped to caller's owner_sub."""
        result = resolve_project_repos("client-A", ALICE_SUB, db_pool)
        assert result.project_id == PROJECT_A_ID
        assert result.repo_names == frozenset({"org/repo-1", "org/repo-2"})

    def test_resolve_team_project_by_tenant(self, db_pool: FakeDBPool) -> None:
        """Team project (tenant_id not null) accessible by same-tenant caller."""
        result = resolve_project_repos(
            PROJECT_TEAM_ID, BOB_SUB, db_pool, caller_tenant_id=ACME_TENANT
        )
        assert result.project_id == PROJECT_TEAM_ID
        assert result.repo_names == frozenset({"org/repo-1", "org/repo-4"})

    def test_empty_project_returns_empty_set(self) -> None:
        """Project with no repos returns empty frozenset."""
        project_id = str(uuid.uuid4())
        pool = FakeDBPool(
            projects=[
                {"id": project_id, "owner_sub": ALICE_SUB, "name": "empty", "tenant_id": None}
            ],
            project_repos=[],
        )
        result = resolve_project_repos(project_id, ALICE_SUB, pool)
        assert result.project_id == project_id
        assert result.repo_names == frozenset()


# ---------------------------------------------------------------------------
# resolve_project_repos() — error cases
# ---------------------------------------------------------------------------


class TestResolveProjectErrors:
    """Tests for resolve_project_repos() error handling."""

    def test_nonexistent_project_raises(self, db_pool: FakeDBPool) -> None:
        """Non-existent project UUID raises ProjectFilterError."""
        fake_id = str(uuid.uuid4())
        with pytest.raises(ProjectFilterError) as exc_info:
            resolve_project_repos(fake_id, ALICE_SUB, db_pool)
        assert exc_info.value.code == "project_not_found"

    def test_nonexistent_name_raises(self, db_pool: FakeDBPool) -> None:
        """Non-existent project name raises ProjectFilterError."""
        with pytest.raises(ProjectFilterError) as exc_info:
            resolve_project_repos("no-such-project", ALICE_SUB, db_pool)
        assert exc_info.value.code == "project_not_found"

    def test_empty_project_id_raises(self, db_pool: FakeDBPool) -> None:
        """Empty project_id raises ProjectFilterError."""
        with pytest.raises(ProjectFilterError) as exc_info:
            resolve_project_repos("", ALICE_SUB, db_pool)
        assert exc_info.value.code == "project_invalid"

    def test_whitespace_project_id_raises(self, db_pool: FakeDBPool) -> None:
        """Whitespace-only project_id raises ProjectFilterError."""
        with pytest.raises(ProjectFilterError) as exc_info:
            resolve_project_repos("   ", ALICE_SUB, db_pool)
        assert exc_info.value.code == "project_invalid"

    def test_no_owner_sub_raises(self, db_pool: FakeDBPool) -> None:
        """Missing caller_owner_sub raises ProjectFilterError."""
        with pytest.raises(ProjectFilterError) as exc_info:
            resolve_project_repos(PROJECT_A_ID, "", db_pool)
        assert exc_info.value.code == "project_no_identity"


# ---------------------------------------------------------------------------
# Ownership check — cross-user access blocked
# ---------------------------------------------------------------------------


class TestOwnershipCheck:
    """Tests that project ownership prevents cross-user access."""

    def test_cross_user_uuid_blocked(self, db_pool: FakeDBPool) -> None:
        """Bob cannot resolve Alice's personal project by UUID."""
        with pytest.raises(ProjectFilterError) as exc_info:
            resolve_project_repos(PROJECT_A_ID, BOB_SUB, db_pool)
        assert exc_info.value.code == "project_not_found"

    def test_cross_user_name_blocked(self, db_pool: FakeDBPool) -> None:
        """Bob cannot resolve Alice's project by name."""
        with pytest.raises(ProjectFilterError) as exc_info:
            resolve_project_repos("client-A", BOB_SUB, db_pool)
        assert exc_info.value.code == "project_not_found"

    def test_cross_tenant_team_project_blocked(self, db_pool: FakeDBPool) -> None:
        """Caller from different tenant cannot access team project."""
        with pytest.raises(ProjectFilterError) as exc_info:
            resolve_project_repos(
                PROJECT_TEAM_ID, BOB_SUB, db_pool, caller_tenant_id="other-tenant"
            )
        assert exc_info.value.code == "project_not_found"

    def test_own_project_allowed(self, db_pool: FakeDBPool) -> None:
        """Owner can resolve their own project."""
        result = resolve_project_repos(PROJECT_B_ID, BOB_SUB, db_pool)
        assert result.project_id == PROJECT_B_ID
        assert "org/repo-3" in result.repo_names


# ---------------------------------------------------------------------------
# apply_project_filter() — intersection semantics
# ---------------------------------------------------------------------------


class TestApplyProjectFilter:
    """Tests for apply_project_filter() intersection logic."""

    def test_none_scope_passthrough(self) -> None:
        """None project_scope = no narrowing (passthrough)."""
        hits = _make_hits("org/repo-1", "org/repo-2", "org/repo-3")
        result = apply_project_filter(hits, None)
        assert len(result) == 3

    def test_narrows_to_project_repos(self) -> None:
        """Only hits in the project's repo set survive."""
        hits = _make_hits("org/repo-1", "org/repo-2", "org/repo-3")
        scope = ProjectScope(
            project_id="test-project",
            repo_names=frozenset({"org/repo-1", "org/repo-3"}),
        )
        result = apply_project_filter(hits, scope)
        assert len(result) == 2
        assert {h.repo_name for h in result} == {"org/repo-1", "org/repo-3"}

    def test_empty_project_returns_empty(self) -> None:
        """Project with no repos → empty results (intersection with empty = empty)."""
        hits = _make_hits("org/repo-1", "org/repo-2")
        scope = ProjectScope(project_id="empty-project", repo_names=frozenset())
        result = apply_project_filter(hits, scope)
        assert result == []

    def test_empty_hits_stays_empty(self) -> None:
        """Empty hits + any project → still empty."""
        scope = ProjectScope(
            project_id="test-project",
            repo_names=frozenset({"org/repo-1"}),
        )
        result = apply_project_filter([], scope)
        assert result == []

    def test_preserves_hit_data(self) -> None:
        """Filtered hits preserve their data payload."""
        hits = [
            SearchHit(repo_name="org/repo-1", data={"line": 42, "content": "fn main()"}),
            SearchHit(repo_name="org/repo-2", data={"line": 1, "content": "# README"}),
        ]
        scope = ProjectScope(
            project_id="test-project",
            repo_names=frozenset({"org/repo-1"}),
        )
        result = apply_project_filter(hits, scope)
        assert len(result) == 1
        assert result[0].data == {"line": 42, "content": "fn main()"}

    def test_preserves_order(self) -> None:
        """Filtered results maintain original ordering."""
        hits = _make_hits("org/repo-3", "org/repo-1", "org/repo-2")
        scope = ProjectScope(
            project_id="test-project",
            repo_names=frozenset({"org/repo-1", "org/repo-3"}),
        )
        result = apply_project_filter(hits, scope)
        assert [h.repo_name for h in result] == ["org/repo-3", "org/repo-1"]


# ---------------------------------------------------------------------------
# _resolve_project_scope() — feature flag and dispatch integration
# ---------------------------------------------------------------------------

# door.server has heavy dependencies (fastapi, mcp) that may not be available
# in all test environments. We test the integration logic by importing conditionally.
_server_import_error: str | None = None
try:
    from door.server import _resolve_project_scope

    _has_server = True
except ImportError as e:
    _has_server = False
    _server_import_error = str(e)

_skip_server = pytest.mark.skipif(
    not _has_server,
    reason=f"door.server not importable (missing dep: {_server_import_error})",
)


@_skip_server
class TestResolveProjectScopeIntegration:
    """Tests for the _resolve_project_scope helper in server.py."""

    def test_no_project_arg_returns_none(self) -> None:
        """No 'project' in arguments → None (passthrough)."""
        caller = CallerPrincipal(
            github_login="alice", github_teams=[], tenant_id="acme", owner_sub=ALICE_SUB
        )
        result = _resolve_project_scope({"query": "test"}, caller)
        assert result is None

    def test_empty_project_arg_returns_none(self) -> None:
        """Empty 'project' string → None (passthrough)."""
        caller = CallerPrincipal(
            github_login="alice", github_teams=[], tenant_id="acme", owner_sub=ALICE_SUB
        )
        result = _resolve_project_scope({"query": "test", "project": ""}, caller)
        assert result is None

    def test_feature_flag_disabled_returns_none(self) -> None:
        """When PROJECT_FILTER_ENABLED=false, returns None even with project arg."""
        caller = CallerPrincipal(
            github_login="alice", github_teams=[], tenant_id="acme", owner_sub=ALICE_SUB
        )
        with patch("door.server.config") as mock_config:
            mock_config.project_filter_enabled = False
            result = _resolve_project_scope({"project": PROJECT_A_ID}, caller)
        assert result is None

    def test_no_caller_returns_none(self) -> None:
        """None caller → None (ACL will fail-close anyway)."""
        with patch("door.server.config") as mock_config:
            mock_config.project_filter_enabled = True
            result = _resolve_project_scope({"project": PROJECT_A_ID}, None)
        assert result is None

    def test_no_owner_sub_returns_none(self) -> None:
        """Caller without owner_sub → None."""
        caller = CallerPrincipal(
            github_login="alice", github_teams=[], tenant_id="acme", owner_sub=""
        )
        with patch("door.server.config") as mock_config:
            mock_config.project_filter_enabled = True
            result = _resolve_project_scope({"project": PROJECT_A_ID}, caller)
        assert result is None

    def test_no_db_pool_returns_none(self) -> None:
        """No db_pool → None (graceful degradation)."""
        caller = CallerPrincipal(
            github_login="alice", github_teams=[], tenant_id="acme", owner_sub=ALICE_SUB
        )
        with patch("door.server.config") as mock_config, patch("door.server.state") as mock_state:
            mock_config.project_filter_enabled = True
            mock_state.db_pool = None
            result = _resolve_project_scope({"project": PROJECT_A_ID}, caller)
        assert result is None

    def test_resolution_error_returns_error_dict(self) -> None:
        """ProjectFilterError → returns error dict (not None)."""
        caller = CallerPrincipal(
            github_login="alice", github_teams=[], tenant_id="acme", owner_sub=ALICE_SUB
        )
        fake_pool = FakeDBPool(projects=[], project_repos=[])

        with patch("door.server.config") as mock_config, patch("door.server.state") as mock_state:
            mock_config.project_filter_enabled = True
            mock_state.db_pool = fake_pool
            result = _resolve_project_scope({"project": "nonexistent"}, caller)

        assert isinstance(result, dict)
        assert result["code"] == "project_not_found"

    def test_successful_resolution_returns_scope(self) -> None:
        """Valid project → returns ProjectScope."""
        caller = CallerPrincipal(
            github_login="alice", github_teams=[], tenant_id=ACME_TENANT, owner_sub=ALICE_SUB
        )
        pool = FakeDBPool(
            projects=[
                {"id": PROJECT_A_ID, "owner_sub": ALICE_SUB, "name": "client-A", "tenant_id": None}
            ],
            project_repos=[
                {"project_id": PROJECT_A_ID, "repo_name": "org/repo-1"},
            ],
        )

        with patch("door.server.config") as mock_config, patch("door.server.state") as mock_state:
            mock_config.project_filter_enabled = True
            mock_state.db_pool = pool
            result = _resolve_project_scope({"project": PROJECT_A_ID}, caller)

        assert isinstance(result, ProjectScope)
        assert result.project_id == PROJECT_A_ID
        assert "org/repo-1" in result.repo_names


# ---------------------------------------------------------------------------
# Integration: project filter can only narrow, never widen
# ---------------------------------------------------------------------------


class TestNarrowingInvariant:
    """Project filter can only NARROW results — never add repos."""

    def test_project_cannot_add_repos_not_in_hits(self) -> None:
        """Even if project contains org/repo-3, it won't appear if not in hits."""
        hits = _make_hits("org/repo-1", "org/repo-2")
        scope = ProjectScope(
            project_id="test",
            repo_names=frozenset({"org/repo-1", "org/repo-3"}),
        )
        result = apply_project_filter(hits, scope)
        # org/repo-3 is in project but was NOT in ACL-filtered hits
        assert {h.repo_name for h in result} == {"org/repo-1"}

    def test_project_intersects_not_unions(self) -> None:
        """Project scope is intersection, not union."""
        # Hits from ACL: repo-1, repo-2, repo-3
        # Project repos: repo-2, repo-4
        # Expected: only repo-2 (intersection)
        hits = _make_hits("org/repo-1", "org/repo-2", "org/repo-3")
        scope = ProjectScope(
            project_id="test",
            repo_names=frozenset({"org/repo-2", "org/repo-4"}),
        )
        result = apply_project_filter(hits, scope)
        assert len(result) == 1
        assert result[0].repo_name == "org/repo-2"
