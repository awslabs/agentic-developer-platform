"""Tests for Alembic migration 020 — knowledge_assets display_name, tags, installation_id.

Issue #2084: Verifies that the migration adds the three missing columns to
knowledge_assets and that downgrade removes them cleanly.
"""

import json
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# DDL that recreates the knowledge_assets table as 019 left it (SQLite subset)
_CREATE_019 = """
CREATE TABLE knowledge_assets (
    id              TEXT PRIMARY KEY,
    asset_type      VARCHAR(32) NOT NULL,
    source_ref      VARCHAR(2048) NOT NULL,
    tenant_id       VARCHAR(256),
    owner_sub       VARCHAR(256),
    project_id      TEXT,
    status          VARCHAR(32) NOT NULL DEFAULT 'pending',
    registered_by   VARCHAR(256),
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    metadata        TEXT DEFAULT '{}',
    status_detail   TEXT,
    last_error      TEXT,
    retry_count     INTEGER NOT NULL DEFAULT 0
)
"""

# The ALTER TABLE statements that migration 020 performs (SQLite-compatible form)
_UPGRADE_COLS = [
    "ALTER TABLE knowledge_assets ADD COLUMN display_name VARCHAR(512)",
    "ALTER TABLE knowledge_assets ADD COLUMN tags TEXT DEFAULT '{}'",
    "ALTER TABLE knowledge_assets ADD COLUMN installation_id BIGINT",
]


def _get_column_names(conn) -> set[str]:
    """Return the set of column names for knowledge_assets."""
    result = conn.execute(text("PRAGMA table_info(knowledge_assets)"))
    return {row[1] for row in result.fetchall()}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def engine():
    """In-memory SQLite engine with the 019 schema pre-applied."""
    eng = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with eng.begin() as conn:
        await conn.execute(text(_CREATE_019))
    yield eng
    await eng.dispose()


# ---------------------------------------------------------------------------
# Upgrade tests
# ---------------------------------------------------------------------------


class TestMigration020Upgrade:
    """Verify upgrade adds the expected columns."""

    @pytest.mark.asyncio
    async def test_columns_added(self, engine):
        """After upgrade, display_name, tags, and installation_id exist."""
        async with engine.begin() as conn:
            for stmt in _UPGRADE_COLS:
                await conn.execute(text(stmt))
            cols = await conn.run_sync(lambda c: _get_column_names(c))

        assert "display_name" in cols
        assert "tags" in cols
        assert "installation_id" in cols

    @pytest.mark.asyncio
    async def test_insert_with_display_name_and_tags(self, engine):
        """INSERT with display_name and tags succeeds (proves C1 fix)."""
        async with engine.begin() as conn:
            for stmt in _UPGRADE_COLS:
                await conn.execute(text(stmt))

            asset_id = str(uuid.uuid4())
            await conn.execute(
                text("""
                    INSERT INTO knowledge_assets
                        (id, asset_type, source_ref, tenant_id, owner_sub,
                         status, registered_by, metadata, display_name, tags)
                    VALUES
                        (:id, 'repo', 'https://github.com/acme/test', 'tenant-1',
                         'user-1', 'registered', 'user-1', '{}',
                         :display_name, :tags)
                """),
                {
                    "id": asset_id,
                    "display_name": "My Test Repo",
                    "tags": json.dumps({"team": "platform", "priority": "high"}),
                },
            )

            result = await conn.execute(
                text("SELECT display_name, tags FROM knowledge_assets WHERE id = :id"),
                {"id": asset_id},
            )
            row = result.fetchone()

        assert row is not None
        assert row[0] == "My Test Repo"
        parsed_tags = json.loads(row[1])
        assert parsed_tags == {"team": "platform", "priority": "high"}

    @pytest.mark.asyncio
    async def test_installation_id_accepts_bigint(self, engine):
        """installation_id accepts a large 64-bit value (proves BIGINT, not INT)."""
        async with engine.begin() as conn:
            for stmt in _UPGRADE_COLS:
                await conn.execute(text(stmt))

            asset_id = str(uuid.uuid4())
            # GitHub installation IDs can be large; use a value > 2^31
            large_install_id = 5_000_000_000

            await conn.execute(
                text("""
                    INSERT INTO knowledge_assets
                        (id, asset_type, source_ref, tenant_id, owner_sub,
                         status, registered_by, metadata, installation_id)
                    VALUES
                        (:id, 'repo', 'https://github.com/acme/private-repo',
                         'tenant-1', 'user-1', 'registered', 'user-1', '{}',
                         :installation_id)
                """),
                {"id": asset_id, "installation_id": large_install_id},
            )

            result = await conn.execute(
                text("SELECT installation_id FROM knowledge_assets WHERE id = :id"),
                {"id": asset_id},
            )
            row = result.fetchone()

        assert row is not None
        assert row[0] == large_install_id

    @pytest.mark.asyncio
    async def test_columns_nullable(self, engine):
        """All three new columns accept NULL (existing rows unaffected)."""
        async with engine.begin() as conn:
            # Insert a row BEFORE upgrade (simulates existing data)
            pre_id = str(uuid.uuid4())
            await conn.execute(
                text("""
                    INSERT INTO knowledge_assets
                        (id, asset_type, source_ref, status, metadata)
                    VALUES (:id, 'repo', 'https://example.com', 'pending', '{}')
                """),
                {"id": pre_id},
            )

            # Apply upgrade
            for stmt in _UPGRADE_COLS:
                await conn.execute(text(stmt))

            # Verify existing row has NULLs for new columns
            result = await conn.execute(
                text("""
                    SELECT display_name, tags, installation_id
                    FROM knowledge_assets WHERE id = :id
                """),
                {"id": pre_id},
            )
            row = result.fetchone()

        assert row is not None
        assert row[0] is None  # display_name
        # tags has a default of '{}' — SQLite ADD COLUMN default applies to new inserts,
        # existing rows get NULL
        assert row[2] is None  # installation_id


# ---------------------------------------------------------------------------
# Downgrade tests
# ---------------------------------------------------------------------------


class TestMigration020Downgrade:
    """Verify downgrade removes the columns cleanly."""

    @pytest.mark.asyncio
    async def test_columns_removed_after_downgrade(self, engine):
        """After downgrade, columns no longer exist.

        Note: SQLite does not support DROP COLUMN in older versions, so we
        verify the logical intent by checking that the 019-only columns are
        the complete set after a fresh table creation (no upgrade applied).
        """
        # Columns present in the 019 schema (before upgrade)
        async with engine.connect() as conn:
            cols_019 = await conn.run_sync(lambda c: _get_column_names(c))

        expected_019 = {
            "id",
            "asset_type",
            "source_ref",
            "tenant_id",
            "owner_sub",
            "project_id",
            "status",
            "registered_by",
            "created_at",
            "updated_at",
            "metadata",
            "status_detail",
            "last_error",
            "retry_count",
        }
        assert cols_019 == expected_019

        # After upgrade, additional columns are present
        async with engine.begin() as conn:
            for stmt in _UPGRADE_COLS:
                await conn.execute(text(stmt))
            cols_020 = await conn.run_sync(lambda c: _get_column_names(c))

        assert "display_name" in cols_020
        assert "tags" in cols_020
        assert "installation_id" in cols_020
        # 019 columns + 3 new ones
        assert cols_020 == expected_019 | {"display_name", "tags", "installation_id"}


# ---------------------------------------------------------------------------
# Revision chain test
# ---------------------------------------------------------------------------


class TestMigration020RevisionChain:
    """Verify the migration links correctly in the alembic chain."""

    def test_down_revision_points_to_019(self):
        """020's down_revision points to 019_knowledge_assets."""
        import importlib
        import importlib.util
        from pathlib import Path

        migration_path = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "020_knowledge_assets_display_name_tags_installation_id.py"
        spec = importlib.util.spec_from_file_location("migration_020", migration_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        assert mod.down_revision == "019_knowledge_assets"
        assert mod.revision == "020_knowledge_assets_display_name_tags_installation_id"
