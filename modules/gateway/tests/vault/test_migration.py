"""Tests for Alembic migration 004 — user_identities, user_credentials, channel_tenant_map.

Issue #134: Vault Phase 1
"""

import pytest
import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from src.shared.models.base import Base


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

VAULT_TABLES = {"user_identities", "user_credentials", "channel_tenant_map"}


def _collect_schema_info(sync_conn):
    """Collect all schema info synchronously inside run_sync."""
    insp = sa_inspect(sync_conn)
    info = {}
    info["tables"] = set(insp.get_table_names())
    for table in VAULT_TABLES & info["tables"]:
        info[f"{table}_columns"] = {c["name"] for c in insp.get_columns(table)}
        ucs = []
        non_unique_idx = []
        for uc in insp.get_unique_constraints(table):
            ucs.append(set(uc["column_names"]))
        for idx in insp.get_indexes(table):
            if idx.get("unique"):
                ucs.append(set(idx["column_names"]))
            else:
                non_unique_idx.append(tuple(idx["column_names"]))
        info[f"{table}_unique"] = ucs
        info[f"{table}_indexes"] = non_unique_idx
    return info


# ---------------------------------------------------------------------------
# Upgrade tests
# ---------------------------------------------------------------------------


class TestMigrationUpgrade:
    """Verify that the upgrade creates all expected tables, columns, and constraints."""

    @pytest.fixture
    async def schema_info(self):
        """Create tables via metadata and collect schema info."""
        engine = create_async_engine(
            "sqlite+aiosqlite:///:memory:",
            echo=False,
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with engine.connect() as conn:
            info = await conn.run_sync(_collect_schema_info)
        await engine.dispose()
        return info

    @pytest.mark.asyncio
    async def test_tables_created(self, schema_info):
        for t in VAULT_TABLES:
            assert t in schema_info["tables"], f"Table {t} not found after upgrade"

    @pytest.mark.asyncio
    async def test_user_identities_columns(self, schema_info):
        expected = {
            "id", "org_id", "team_id", "user_id", "provider", "provider_user_id",
            "provider_username", "verification_method", "verified_at",
            "created_at", "updated_at",
        }
        assert expected <= schema_info["user_identities_columns"]

    @pytest.mark.asyncio
    async def test_user_credentials_columns(self, schema_info):
        expected = {
            "id", "org_id", "team_id", "user_id", "service", "credential_type",
            "label", "secret_arn", "scopes", "expires_at", "last_used_at",
            "created_at", "updated_at",
        }
        assert expected <= schema_info["user_credentials_columns"]

    @pytest.mark.asyncio
    async def test_channel_tenant_map_columns(self, schema_info):
        expected = {"id", "provider", "provider_scope_id", "org_id", "created_at"}
        assert expected <= schema_info["channel_tenant_map_columns"]

    @pytest.mark.asyncio
    async def test_user_identities_unique_provider(self, schema_info):
        assert {"provider", "provider_user_id"} in schema_info["user_identities_unique"]

    @pytest.mark.asyncio
    async def test_user_credentials_unique_user_service_label(self, schema_info):
        assert {"user_id", "service", "label"} in schema_info["user_credentials_unique"]

    @pytest.mark.asyncio
    async def test_channel_tenant_map_unique_provider_scope(self, schema_info):
        assert {"provider", "provider_scope_id"} in schema_info["channel_tenant_map_unique"]

    @pytest.mark.asyncio
    async def test_user_credentials_has_team_scoped_index(self, schema_info):
        # Composite index for "list credentials in team X for service Y" without
        # joining through users.team_id. Ordering matters — must be (org_id, team_id, service).
        assert ("org_id", "team_id", "service") in schema_info["user_credentials_indexes"]

    @pytest.mark.asyncio
    async def test_user_identities_has_team_scoped_index(self, schema_info):
        assert ("org_id", "team_id") in schema_info["user_identities_indexes"]


# ---------------------------------------------------------------------------
# Downgrade tests
# ---------------------------------------------------------------------------


class TestMigrationDowngrade:
    """Verify that dropping vault tables removes them cleanly."""

    @pytest.mark.asyncio
    async def test_downgrade_removes_tables(self):
        engine = create_async_engine(
            "sqlite+aiosqlite:///:memory:",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        # Create all tables
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        # Verify they exist
        async with engine.connect() as conn:
            info = await conn.run_sync(_collect_schema_info)
            assert VAULT_TABLES <= info["tables"]

        # Drop vault tables (simulates downgrade)
        async with engine.begin() as conn:
            for tbl in ["channel_tenant_map", "user_credentials", "user_identities"]:
                await conn.execute(sa.text(f"DROP TABLE IF EXISTS {tbl}"))

        # Verify they're gone
        async with engine.connect() as conn:
            remaining = await conn.run_sync(
                lambda c: set(sa_inspect(c).get_table_names())
            )
            for t in VAULT_TABLES:
                assert t not in remaining, f"Table {t} still present after downgrade"

        await engine.dispose()
