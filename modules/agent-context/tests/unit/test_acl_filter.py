"""Unit tests for Door ACL filter (repo-grain permission enforcement).

Validates the critical security invariants:
- Unresolved principal (None) -> empty results (fail-closed)
- Empty login + empty teams -> empty results (fail-closed)
- ACL store failure -> empty results (fail-closed)
- Public repos (sentinel "*") visible to all resolved callers
- Private repos only visible to callers in allowed_principals
- Team membership grants access
- Empty allowed_principals denies all callers

See: docs/design-1356-repo-acl-door-filter.md §11
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
# Helpers: In-memory ACL store for testing
# ---------------------------------------------------------------------------


class FakeACLStore:
    """In-memory ACL store for unit tests.

    Stores a mapping of repo_name -> set of principals (logins + team slugs).
    Implements the same lookup logic as PostgresACLStore.
    """

    def __init__(self, repo_principals: dict[str, list[str]] | None = None):
        self._repos: dict[str, list[str]] = repo_principals or {}

    def get_allowed_repos(self, principal: CallerPrincipal) -> set[str]:
        """Return repos this principal can access."""
        allowed: set[str] = set()
        for repo_name, principals in self._repos.items():
            # Public sentinel
            if PUBLIC_SENTINEL in principals:
                allowed.add(repo_name)
                continue
            # Login match
            if principal.github_login and principal.github_login in principals:
                allowed.add(repo_name)
                continue
            # Team overlap
            if any(team in principals for team in principal.github_teams):
                allowed.add(repo_name)
        return allowed


class FailingACLStore:
    """ACL store that always raises (simulates Postgres connection failure)."""

    def get_allowed_repos(self, principal: CallerPrincipal) -> set[str]:
        raise ConnectionError("Database connection failed")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def public_repo_store() -> FakeACLStore:
    """Store with one public repo and one private repo."""
    return FakeACLStore(
        {
            "org/public-lib": [PUBLIC_SENTINEL],
            "org/private-api": ["alice", "org/backend-team"],
        }
    )


@pytest.fixture
def multi_repo_store() -> FakeACLStore:
    """Store with multiple repos and diverse access patterns."""
    return FakeACLStore(
        {
            "org/public-lib": [PUBLIC_SENTINEL],
            "org/private-api": ["alice", "bob", "org/backend-team"],
            "org/secret-infra": ["charlie", "org/platform-team"],
            "org/empty-acl": [],  # No principals -> invisible to all
        }
    )


@pytest.fixture
def alice() -> CallerPrincipal:
    return CallerPrincipal(github_login="alice", github_teams=["org/backend-team"])


@pytest.fixture
def bob() -> CallerPrincipal:
    return CallerPrincipal(github_login="bob", github_teams=[])


@pytest.fixture
def charlie() -> CallerPrincipal:
    return CallerPrincipal(github_login="charlie", github_teams=["org/platform-team"])


@pytest.fixture
def unrelated_user() -> CallerPrincipal:
    return CallerPrincipal(github_login="mallory", github_teams=["other-org/hackers"])


def _make_hits(*repos: str) -> list[SearchHit]:
    """Create SearchHit objects for the given repo names."""
    return [SearchHit(repo_name=r, data={"match": f"code in {r}"}) for r in repos]


# ---------------------------------------------------------------------------
# FAIL-CLOSED invariant tests
# ---------------------------------------------------------------------------


class TestFailClosed:
    """The filter MUST return empty when principal is None or unresolved."""

    def test_none_principal_returns_empty(self, public_repo_store: FakeACLStore) -> None:
        """None caller -> zero results, even for public repos."""
        hits = _make_hits("org/public-lib", "org/private-api")
        result = filter_results(hits, None, public_repo_store)
        assert result == []

    def test_empty_login_and_teams_returns_empty(self, public_repo_store: FakeACLStore) -> None:
        """CallerPrincipal with empty login and no teams -> zero results."""
        empty_caller = CallerPrincipal(github_login="", github_teams=[])
        hits = _make_hits("org/public-lib", "org/private-api")
        result = filter_results(hits, empty_caller, public_repo_store)
        assert result == []

    def test_postgres_failure_returns_empty(self) -> None:
        """When ACL store raises, filter returns [] (not all results)."""
        caller = CallerPrincipal(github_login="alice", github_teams=[])
        hits = _make_hits("org/public-lib", "org/private-api")
        result = filter_results(hits, caller, FailingACLStore())
        assert result == []

    def test_empty_results_input_returns_empty(self, public_repo_store: FakeACLStore) -> None:
        """Empty input -> empty output (trivial but verifies no crash)."""
        caller = CallerPrincipal(github_login="alice", github_teams=[])
        result = filter_results([], caller, public_repo_store)
        assert result == []


# ---------------------------------------------------------------------------
# Public repo visibility tests
# ---------------------------------------------------------------------------


class TestPublicRepos:
    """Repos with '*' in allowed_principals are visible to all resolved callers."""

    def test_public_repo_visible_to_all(self, multi_repo_store: FakeACLStore) -> None:
        """Public repo with '*' sentinel passes for any resolved caller."""
        hits = _make_hits("org/public-lib")
        caller = CallerPrincipal(github_login="random-user", github_teams=[])
        result = filter_results(hits, caller, multi_repo_store)
        assert len(result) == 1
        assert result[0].repo_name == "org/public-lib"

    def test_public_repo_visible_alongside_private(
        self, multi_repo_store: FakeACLStore, alice: CallerPrincipal
    ) -> None:
        """Alice sees public + her private repos, but not others' private repos."""
        hits = _make_hits("org/public-lib", "org/private-api", "org/secret-infra")
        result = filter_results(hits, alice, multi_repo_store)
        result_repos = {h.repo_name for h in result}
        assert "org/public-lib" in result_repos
        assert "org/private-api" in result_repos
        assert "org/secret-infra" not in result_repos


# ---------------------------------------------------------------------------
# Private repo access tests
# ---------------------------------------------------------------------------


class TestPrivateRepoAccess:
    """Private repos are only visible to callers listed in allowed_principals."""

    def test_hit_from_allowed_repo_passes(
        self, multi_repo_store: FakeACLStore, alice: CallerPrincipal
    ) -> None:
        """Alice is in allowed_principals for private-api -> hit passes."""
        hits = _make_hits("org/private-api")
        result = filter_results(hits, alice, multi_repo_store)
        assert len(result) == 1
        assert result[0].repo_name == "org/private-api"

    def test_hit_from_disallowed_repo_dropped(
        self, multi_repo_store: FakeACLStore, alice: CallerPrincipal
    ) -> None:
        """Alice is NOT in allowed_principals for secret-infra -> hit dropped."""
        hits = _make_hits("org/secret-infra")
        result = filter_results(hits, alice, multi_repo_store)
        assert result == []

    def test_empty_principals_denies_all(
        self, multi_repo_store: FakeACLStore, alice: CallerPrincipal
    ) -> None:
        """Repo with allowed_principals=[] is invisible to ALL callers."""
        hits = _make_hits("org/empty-acl")
        result = filter_results(hits, alice, multi_repo_store)
        assert result == []

    def test_unrelated_user_sees_only_public(
        self, multi_repo_store: FakeACLStore, unrelated_user: CallerPrincipal
    ) -> None:
        """User not in any repo's principals sees only public repos."""
        hits = _make_hits("org/public-lib", "org/private-api", "org/secret-infra", "org/empty-acl")
        result = filter_results(hits, unrelated_user, multi_repo_store)
        assert len(result) == 1
        assert result[0].repo_name == "org/public-lib"


# ---------------------------------------------------------------------------
# Team membership tests
# ---------------------------------------------------------------------------


class TestTeamAccess:
    """Team slugs in allowed_principals grant access to team members."""

    def test_team_membership_grants_access(
        self, multi_repo_store: FakeACLStore, charlie: CallerPrincipal
    ) -> None:
        """Charlie is in org/platform-team -> can see secret-infra."""
        hits = _make_hits("org/secret-infra")
        result = filter_results(hits, charlie, multi_repo_store)
        assert len(result) == 1
        assert result[0].repo_name == "org/secret-infra"

    def test_team_access_without_login_match(self, multi_repo_store: FakeACLStore) -> None:
        """User with only team membership (no direct login match) still gets access."""
        # User "dave" is not directly listed, but is in "org/backend-team"
        dave = CallerPrincipal(github_login="dave", github_teams=["org/backend-team"])
        hits = _make_hits("org/private-api")
        result = filter_results(hits, dave, multi_repo_store)
        assert len(result) == 1

    def test_wrong_team_no_access(self, multi_repo_store: FakeACLStore) -> None:
        """User in a team not listed in allowed_principals -> no access."""
        user = CallerPrincipal(github_login="eve", github_teams=["org/frontend-team"])
        hits = _make_hits("org/private-api", "org/secret-infra")
        result = filter_results(hits, user, multi_repo_store)
        assert result == []


# ---------------------------------------------------------------------------
# Mixed results filtering tests
# ---------------------------------------------------------------------------


class TestMixedResults:
    """Filter correctly handles a mix of public, allowed, and disallowed results."""

    def test_mixed_public_and_private_results(
        self, multi_repo_store: FakeACLStore, bob: CallerPrincipal
    ) -> None:
        """Bob sees public + private-api (he's listed), not secret-infra."""
        hits = _make_hits("org/public-lib", "org/private-api", "org/secret-infra", "org/empty-acl")
        result = filter_results(hits, bob, multi_repo_store)
        result_repos = {h.repo_name for h in result}
        assert result_repos == {"org/public-lib", "org/private-api"}

    def test_filter_preserves_order(
        self, multi_repo_store: FakeACLStore, alice: CallerPrincipal
    ) -> None:
        """Filtered results maintain original ordering (relevance from engine)."""
        hits = _make_hits("org/private-api", "org/public-lib", "org/secret-infra")
        result = filter_results(hits, alice, multi_repo_store)
        assert [h.repo_name for h in result] == ["org/private-api", "org/public-lib"]

    def test_filter_preserves_hit_data(
        self, multi_repo_store: FakeACLStore, alice: CallerPrincipal
    ) -> None:
        """Filtering does not modify the SearchHit data payload."""
        hits = [SearchHit(repo_name="org/private-api", data={"line": 42, "content": "secret"})]
        result = filter_results(hits, alice, multi_repo_store)
        assert len(result) == 1
        assert result[0].data == {"line": 42, "content": "secret"}


# ---------------------------------------------------------------------------
# Cross-principal isolation tests
# ---------------------------------------------------------------------------


class TestCrossPrincipalIsolation:
    """Two principals searching the same index see different results."""

    def test_two_principals_cross_isolation(self, multi_repo_store: FakeACLStore) -> None:
        """Alice and Charlie searching the same hits see different repos."""
        alice = CallerPrincipal(github_login="alice", github_teams=["org/backend-team"])
        charlie = CallerPrincipal(github_login="charlie", github_teams=["org/platform-team"])

        all_hits = _make_hits(
            "org/public-lib", "org/private-api", "org/secret-infra", "org/empty-acl"
        )

        alice_results = filter_results(all_hits, alice, multi_repo_store)
        charlie_results = filter_results(all_hits, charlie, multi_repo_store)

        alice_repos = {h.repo_name for h in alice_results}
        charlie_repos = {h.repo_name for h in charlie_results}

        # Both see public
        assert "org/public-lib" in alice_repos
        assert "org/public-lib" in charlie_repos

        # Alice sees private-api, Charlie doesn't
        assert "org/private-api" in alice_repos
        assert "org/private-api" not in charlie_repos

        # Charlie sees secret-infra, Alice doesn't
        assert "org/secret-infra" in charlie_repos
        assert "org/secret-infra" not in alice_repos

        # Neither sees empty-acl
        assert "org/empty-acl" not in alice_repos
        assert "org/empty-acl" not in charlie_repos


# ---------------------------------------------------------------------------
# Header extraction tests
# ---------------------------------------------------------------------------


class TestHeaderExtraction:
    """Tests for extracting CallerPrincipal from request headers."""

    def test_both_headers_present(self) -> None:
        """Login + teams headers -> CallerPrincipal with both."""
        headers = {
            "X-GitHub-Login": "Alice",
            "X-GitHub-Teams": "org/backend-team,org/dev-team",
        }
        principal = extract_caller_principal(headers)
        assert principal is not None
        assert principal.github_login == "alice"  # lowercased
        assert principal.github_teams == ["org/backend-team", "org/dev-team"]

    def test_login_only(self) -> None:
        """Only login header -> CallerPrincipal with login, empty teams."""
        headers = {"X-GitHub-Login": "bob"}
        principal = extract_caller_principal(headers)
        assert principal is not None
        assert principal.github_login == "bob"
        assert principal.github_teams == []
        assert principal.is_resolved

    def test_teams_only(self) -> None:
        """Only teams header -> CallerPrincipal with empty login, populated teams."""
        headers = {"X-GitHub-Teams": "org/team-a"}
        principal = extract_caller_principal(headers)
        assert principal is not None
        assert principal.github_login == ""
        assert principal.github_teams == ["org/team-a"]
        assert principal.is_resolved

    def test_no_headers_returns_none(self) -> None:
        """No GitHub headers -> None (fail-closed at filter time)."""
        headers = {"Content-Type": "application/json", "Authorization": "Bearer xyz"}
        principal = extract_caller_principal(headers)
        assert principal is None

    def test_empty_headers_returns_none(self) -> None:
        """Empty string headers -> None."""
        headers = {"X-GitHub-Login": "", "X-GitHub-Teams": ""}
        principal = extract_caller_principal(headers)
        assert principal is None

    def test_case_insensitive_header_names(self) -> None:
        """Header names are matched case-insensitively."""
        headers = {"x-github-login": "Alice", "x-github-teams": "org/team"}
        principal = extract_caller_principal(headers)
        assert principal is not None
        assert principal.github_login == "alice"

    def test_whitespace_handling(self) -> None:
        """Whitespace in login and teams is stripped."""
        headers = {
            "X-GitHub-Login": "  alice  ",
            "X-GitHub-Teams": " org/team-a , org/team-b ",
        }
        principal = extract_caller_principal(headers)
        assert principal is not None
        assert principal.github_login == "alice"
        assert principal.github_teams == ["org/team-a", "org/team-b"]


# ---------------------------------------------------------------------------
# ACL derivation tests (unit — mocked HTTP)
# ---------------------------------------------------------------------------


class TestACLDerivation:
    """Tests for derive_acl_from_github (mocked HTTP responses)."""

    def test_public_repo_returns_star(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Public repo -> ['*']."""
        import requests as http_requests

        class MockResponse:
            status_code = 200

            def json(self):
                return {"visibility": "public", "full_name": "org/public-lib"}

        monkeypatch.setattr(http_requests, "get", lambda *a, **kw: MockResponse())

        from door.acl import derive_acl_from_github

        result = derive_acl_from_github("org/public-lib", "fake-token")
        assert result == [PUBLIC_SENTINEL]

    def test_private_repo_returns_principals(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Private repo -> list of logins + team slugs."""
        import requests as http_requests

        call_count = {"n": 0}

        class MockResponse:
            status_code = 200

            def __init__(self, url: str):
                self._url = url

            def json(self):
                if (
                    "/repos/org/private-api"
                    == self._url.replace("https://api.github.com", "").split("?")[0]
                ):
                    return {"visibility": "private", "full_name": "org/private-api"}
                elif "collaborators" in self._url:
                    return [
                        {"login": "Alice", "permissions": {"push": True, "admin": False}},
                        {"login": "Bob", "permissions": {"push": True, "admin": True}},
                        {"login": "Reader", "permissions": {"push": False, "admin": False}},
                    ]
                elif "teams" in self._url:
                    return [
                        {"slug": "backend-team", "permission": "push"},
                        {"slug": "viewers", "permission": "pull"},
                    ]
                return []

        def mock_get(url, **kwargs):
            call_count["n"] += 1
            return MockResponse(url)

        monkeypatch.setattr(http_requests, "get", mock_get)

        from door.acl import derive_acl_from_github

        result = derive_acl_from_github("org/private-api", "fake-token")

        # Alice and Bob have push access; Reader does not
        assert "alice" in result
        assert "bob" in result
        assert "reader" not in result
        # backend-team has push; viewers has pull (excluded)
        assert "org/backend-team" in result
        assert "org/viewers" not in result

    def test_api_failure_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """GitHub API failure -> empty list (safe default for new repos)."""
        import requests as http_requests

        class FailResponse:
            status_code = 500

            def json(self):
                return {}

        monkeypatch.setattr(http_requests, "get", lambda *a, **kw: FailResponse())

        from door.acl import derive_acl_from_github

        result = derive_acl_from_github("org/broken-repo", "fake-token")
        assert result == []
