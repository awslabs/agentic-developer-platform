"""Execute production list/count SQL against an isolated mixed-owner database."""

import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.knowledge import routes


class SQLSession:
    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("""
            CREATE TABLE knowledge_assets (
                id TEXT, asset_type TEXT DEFAULT 'repo', source_ref TEXT DEFAULT 'https://github.com/acme/repo',
                display_name TEXT, tags TEXT, metadata TEXT, tenant_id TEXT, owner_sub TEXT,
                project_id TEXT, status TEXT DEFAULT 'complete', last_error TEXT,
                retry_count INTEGER DEFAULT 0, registered_by TEXT,
                created_at TEXT DEFAULT '2026-09-25T00:00:00Z', updated_at TEXT)
        """)
        self.connection.executemany(
            "INSERT INTO knowledge_assets(id, tenant_id, owner_sub) VALUES (?, ?, ?)",
            [
                ("own", "team-a", "canonical-alice"),
                ("tenant", "team-a", None),
                ("shared", None, None),
                ("own-unbound", None, "canonical-alice"),
                ("other-personal", "team-a", "canonical-bob"),
                ("other-unbound", None, "canonical-bob"),
                ("raw-identity", "team-a", "external-alice"),
                ("other-tenant", "team-b", None),
                ("own-other-tenant", "team-b", "canonical-alice"),
            ],
        )
        self.connection.execute(
            "INSERT INTO knowledge_assets(id, tenant_id, owner_sub, status) VALUES (?, ?, ?, ?)",
            ("removed", "team-a", "canonical-alice", "removed"),
        )
        self.executed = []

    async def execute(self, query, params):
        self.executed.append((str(query), dict(params)))
        rows = self.connection.execute(str(query), params).fetchall()
        return SimpleNamespace(
            scalar=lambda: rows[0][0],
            fetchall=lambda: [SimpleNamespace(**dict(row)) for row in rows],
        )


@pytest.fixture
def visibility(monkeypatch):
    session = SQLSession()
    identity = AsyncMock(return_value="canonical-alice")
    monkeypatch.setattr(routes, "resolve_canonical_user_id", identity)
    monkeypatch.setattr(routes, "_get_quota_info", AsyncMock(return_value=None))
    user = SimpleNamespace(user_id="external-alice", org_id="team-a", is_admin=False)

    async def query(**overrides):
        options = {"scope": None, "asset_type": None, "status": None, "page": 1, "page_size": 100}
        options.update(overrides)
        return await routes.list_assets(session, object(), user, **options)

    yield session, user, identity, query
    session.connection.close()


async def test_default_list_excludes_other_personal_assets_and_counts_only_visible(visibility):
    session, _, identity, query = visibility
    result = await query()
    assert {item.id for item in result.items} == {"own", "tenant", "shared", "own-unbound"}
    assert result.total == 4
    assert result.has_more is False
    identity.assert_awaited_once()
    assert all(params["sub"] == "canonical-alice" for _, params in session.executed)


@pytest.mark.parametrize("scope,expected", [("personal", {"own"}), ("tenant", {"tenant"})])
async def test_explicit_scopes_keep_existing_visibility(visibility, scope, expected):
    _, _, _, query = visibility
    result = await query(scope=scope)
    assert {item.id for item in result.items} == expected
    assert result.total == len(expected)


async def test_admin_retains_tenant_personal_access_without_other_tenants(visibility):
    _, user, _, query = visibility
    user.is_admin = True
    result = await query()
    found = {item.id for item in result.items}
    assert {"own", "other-personal", "tenant", "shared"} <= found
    assert not {"other-tenant", "own-other-tenant", "removed"} & found
    assert result.total == len(found)


async def test_pagination_count_does_not_leak_hidden_rows(visibility):
    _, _, _, query = visibility
    first = await query(page_size=2)
    second = await query(page_size=2, page=2)
    assert first.total == second.total == 4
    assert first.has_more is True
    assert second.has_more is False
    assert {item.id for item in first.items + second.items} == {"own", "tenant", "shared", "own-unbound"}


async def test_sql_filter_payload_remains_bound_data(visibility):
    session, _, _, query = visibility
    payload = "repo' OR 1=1 --"
    result = await query(asset_type=payload)
    assert result.total == 0
    assert result.items == []
    assert all(payload not in sql for sql, _ in session.executed)
    assert all(params["atype"] == payload for _, params in session.executed)
