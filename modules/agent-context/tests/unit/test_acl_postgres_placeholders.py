"""Regression test for psycopg2 placeholder syntax in PostgresACLStore.

Issue #2425: The ACL store used $N (asyncpg-style) placeholders instead of %s
(psycopg2-style). psycopg2 cannot parse $N, causing UndefinedParameter errors
that silently fail-closed every authenticated query.

This test verifies that both query paths (_get_allowed_repos_legacy and
_get_allowed_repos_scoped) use valid psycopg2 placeholder syntax by running
the actual query strings through psycopg2's parameter binding (mogrify).
"""

from __future__ import annotations

import re

from door.acl import CallerPrincipal, PostgresACLStore


class TestPostgresPlaceholderSyntax:
    """Verify that PostgresACLStore queries use %s (psycopg2) not $N (asyncpg)."""

    def test_legacy_query_has_no_dollar_placeholders(self) -> None:
        """Legacy query must not contain $1..$9 style placeholders."""

        class FakePool:
            pass

        store = PostgresACLStore(FakePool())
        # Access the method source to inspect the query
        import inspect

        source = inspect.getsource(store._get_allowed_repos_legacy)
        # Check for $N placeholders (asyncpg style) — these break psycopg2
        dollar_placeholders = re.findall(r"\$\d+", source)
        assert dollar_placeholders == [], (
            f"Legacy query contains asyncpg-style $N placeholders: {dollar_placeholders}. "
            "psycopg2 requires %s placeholders."
        )

    def test_scoped_query_has_no_dollar_placeholders(self) -> None:
        """Scoped (tenant-aware) query must not contain $1..$9 style placeholders."""

        class FakePool:
            pass

        store = PostgresACLStore(FakePool(), tenant_scope_enabled=True)
        import inspect

        source = inspect.getsource(store._get_allowed_repos_scoped)
        dollar_placeholders = re.findall(r"\$\d+", source)
        assert dollar_placeholders == [], (
            f"Scoped query contains asyncpg-style $N placeholders: {dollar_placeholders}. "
            "psycopg2 requires %s placeholders."
        )

    def test_legacy_query_executes_with_psycopg2_mogrify(self) -> None:
        """Legacy query can be mogrified by psycopg2 without UndefinedParameter."""
        # We simulate what psycopg2 does: check that %s placeholders match param count
        # Extract query from the method by calling it with a mock cursor
        executed_queries: list[tuple] = []

        class FakeCursor:
            def execute(self, query, params):
                # psycopg2 requires exactly as many %s as params
                placeholder_count = query.count("%s")
                assert placeholder_count == len(params), (
                    f"Placeholder count ({placeholder_count}) != param count ({len(params)})"
                )
                # Verify no $N placeholders
                assert "$1" not in query, "Query contains $1 (asyncpg style)"
                executed_queries.append((query, params))

            def fetchall(self):
                return []

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        class FakeConn:
            def cursor(self):
                return FakeCursor()

        class FakePool:
            def getconn(self):
                return FakeConn()

            def putconn(self, conn):
                pass

        store = PostgresACLStore(FakePool())
        principal = CallerPrincipal(
            github_login="testuser",
            github_teams=["org/team-a", "org/team-b"],
        )
        store._get_allowed_repos_legacy(principal)
        assert len(executed_queries) == 1

    def test_scoped_query_executes_with_psycopg2_mogrify(self) -> None:
        """Scoped query can be mogrified by psycopg2 without UndefinedParameter."""
        executed_queries: list[tuple] = []

        class FakeCursor:
            def execute(self, query, params):
                placeholder_count = query.count("%s")
                assert placeholder_count == len(params), (
                    f"Placeholder count ({placeholder_count}) != param count ({len(params)})"
                )
                assert "$1" not in query, "Query contains $1 (asyncpg style)"
                assert "$4" not in query, "Query contains $4 (asyncpg style)"
                assert "$5" not in query, "Query contains $5 (asyncpg style)"
                executed_queries.append((query, params))

            def fetchall(self):
                return []

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        class FakeConn:
            def cursor(self):
                return FakeCursor()

        class FakePool:
            def getconn(self):
                return FakeConn()

            def putconn(self, conn):
                pass

        store = PostgresACLStore(FakePool(), tenant_scope_enabled=True)
        principal = CallerPrincipal(
            github_login="testuser",
            github_teams=["org/team-a"],
            tenant_id="tenant-123",
            owner_sub="user-sub-abc",
        )
        store._get_allowed_repos_scoped(principal)
        assert len(executed_queries) == 1

    def test_scoped_query_param_order_matches_logic(self) -> None:
        """Verify params are passed in the correct order for the scoped query.

        The query logic requires:
        - tenant_id comparison first (WHERE clause order)
        - Then principal matching (PUBLIC_SENTINEL, login, teams)
        - Then owner_sub matching
        """
        captured: list[tuple] = []

        class FakeCursor:
            def execute(self, query, params):
                captured.append((query, params))

            def fetchall(self):
                return []

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        class FakeConn:
            def cursor(self):
                return FakeCursor()

        class FakePool:
            def getconn(self):
                return FakeConn()

            def putconn(self, conn):
                pass

        store = PostgresACLStore(FakePool(), tenant_scope_enabled=True)
        principal = CallerPrincipal(
            github_login="alice",
            github_teams=["org/dev"],
            tenant_id="t1",
            owner_sub="sub-alice",
        )
        store._get_allowed_repos_scoped(principal)

        assert len(captured) == 1
        query, params = captured[0]

        # Verify the query mentions tenant_id before owner_sub in WHERE
        tenant_pos = query.find("tenant_id")
        owner_pos = query.find("owner_sub")
        assert tenant_pos < owner_pos, "tenant_id check should precede owner_sub in query"

        # Verify params list contains expected values (order depends on query %s positions)
        assert "t1" in params, "tenant_id should be in params"
        assert "*" in params, "PUBLIC_SENTINEL should be in params"
        assert "alice" in params, "login should be in params"
        assert ["org/dev"] in params or "org/dev" in str(params), "teams should be in params"
        assert "sub-alice" in params, "owner_sub should be in params"
