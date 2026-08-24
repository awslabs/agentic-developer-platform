"""Tests for Alembic migrations 026 + 027 — installation_id column and uniqueness.

Issue #4070 (sub-EPIC #4068 ·A0).

These tests exercise the REAL migration functions (imported from the version
modules) against a seeded SQLite database, rather than re-implementing the
backfill in the test. A test that reimplements the migration proves only that
the test author can write the same bug twice.

The hard sequencing rule under test: the dedup/quarantine in 026 must complete
before 027's unique index is built, or the index fails to build against existing
duplicate rows and the deploy is blocked.
"""

import importlib.util
import json
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from src.shared.models.base import Base
from src.shared.models.organization import Organization

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "alembic" / "versions"
INSTALL_X = "5550001"
INSTALL_Y = "5550002"


def _load_migration(filename: str):
    """Import a migration module by path (they are not an importable package)."""
    path = MIGRATIONS_DIR / filename
    spec = importlib.util.spec_from_file_location(filename.replace(".py", ""), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MIG_026 = _load_migration("026_channel_tenant_map_installation_id.py")
MIG_027 = _load_migration("027_installation_tenant_uniqueness.py")


def _run_migration(sync_conn, fn):
    """Run a migration's upgrade()/downgrade() with alembic's `op` proxy bound.

    The version modules call the module-level `op` proxy, so it must be pointed
    at a real Operations object for the duration. This runs the migration code
    as written rather than a paraphrase of it.
    """
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    ctx = MigrationContext.configure(sync_conn)
    with Operations.context(ctx):
        fn()


async def _seed_engine():
    """Engine with the CURRENT model schema, then columns dropped to simulate pre-026."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Simulate the pre-026 schema: no installation_id, no dispute flag, no
        # quarantine table. Everything 026 is responsible for creating.
        # The index must go first — SQLite refuses to drop a column an existing
        # index references.
        await conn.execute(sa.text("DROP INDEX IF EXISTS ix_channel_tenant_map_installation_id"))
        await conn.execute(sa.text("ALTER TABLE channel_tenant_map DROP COLUMN installation_id"))
        await conn.execute(sa.text("ALTER TABLE channel_tenant_map DROP COLUMN ownership_disputed"))
        await conn.execute(sa.text("DROP TABLE installation_ownership_conflicts"))
    return engine


async def _add_org(conn, org_id, *, github_org_id=None, installation_ids=None):
    """Insert an organization via the ORM table so column defaults apply.

    Hand-written INSERT ... VALUES would have to enumerate every NOT NULL column
    on `organizations` and would break each time one is added.
    """
    await conn.execute(
        Organization.__table__.insert().values(
            id=org_id,
            name=org_id,
            github_org_id=github_org_id,
            github_installation_ids=installation_ids or [],
        )
    )


async def _add_mapping(conn, row_id, org_id, *, scope_id, installation_id=None):
    """Insert a pre-026 channel_tenant_map row (ownership only in the metadata blob)."""
    meta = json.dumps({"installation_id": int(installation_id)}) if installation_id else None
    await conn.execute(
        sa.text(
            "INSERT INTO channel_tenant_map (id, provider, provider_scope_id, org_id, metadata, created_at) "
            "VALUES (:id, 'github', :scope, :org, :meta, :ts)"
        ),
        {"id": row_id, "scope": scope_id, "org": org_id, "meta": meta, "ts": f"2026-01-0{row_id[-1]} 00:00:00"},
    )


async def _upgrade(engine, module):
    async with engine.begin() as conn:
        await conn.run_sync(_run_migration, module.upgrade)


class TestSchema:
    @pytest.mark.asyncio
    async def test_026_adds_column_index_and_quarantine_table(self):
        engine = await _seed_engine()
        await _upgrade(engine, MIG_026)

        def _inspect(sync_conn):
            insp = sa_inspect(sync_conn)
            return (
                {c["name"] for c in insp.get_columns("channel_tenant_map")},
                {i["name"] for i in insp.get_indexes("channel_tenant_map")},
                set(insp.get_table_names()),
            )

        async with engine.connect() as conn:
            cols, indexes, tables = await conn.run_sync(_inspect)
        await engine.dispose()

        assert "installation_id" in cols
        assert "ownership_disputed" in cols
        assert "ix_channel_tenant_map_installation_id" in indexes
        assert "installation_ownership_conflicts" in tables

    @pytest.mark.asyncio
    async def test_027_chains_onto_026_which_chains_onto_current_head(self):
        """A broken chain silently SKIPS the migration; the resolver then reads a
        column that does not exist. 025_org_created_via is the current head."""
        assert MIG_026.down_revision == "025_org_created_via"
        assert MIG_027.down_revision == MIG_026.revision


class TestBackfill:
    @pytest.mark.asyncio
    async def test_backfills_from_metadata_blob(self):
        """install_callback has populated metadata->installation_id since 013."""
        engine = await _seed_engine()
        async with engine.begin() as conn:
            await _add_org(conn, "org-a", github_org_id="1111")
            await _add_mapping(conn, "row-1", "org-a", scope_id="1111", installation_id=INSTALL_X)

        await _upgrade(engine, MIG_026)

        async with engine.connect() as conn:
            got = (await conn.execute(sa.text("SELECT installation_id FROM channel_tenant_map WHERE id='row-1'"))).scalar_one()
        await engine.dispose()

        assert got == INSTALL_X

    @pytest.mark.asyncio
    async def test_backfill_covers_the_org_json_source_too(self):
        """Ownership recorded only in organizations.github_installation_ids is
        still recognised — it is the second writer's representation, so ignoring
        it would make the resolver report NOT_FOUND for a live install."""
        engine = await _seed_engine()
        async with engine.begin() as conn:
            await _add_org(conn, "org-a", github_org_id="1111", installation_ids=[INSTALL_X])
            await _add_org(conn, "org-b", github_org_id="2222", installation_ids=[INSTALL_Y])
            await _add_mapping(conn, "row-1", "org-b", scope_id="2222", installation_id=INSTALL_Y)

        await _upgrade(engine, MIG_026)

        # org-a claims X only via JSON and org-b claims Y via both. Neither is a
        # cross-tenant conflict, so nothing should be quarantined.
        async with engine.connect() as conn:
            conflicts = (await conn.execute(sa.text("SELECT count(*) FROM installation_ownership_conflicts"))).scalar_one()
        await engine.dispose()

        assert conflicts == 0

    @pytest.mark.asyncio
    async def test_non_github_rows_are_left_alone(self):
        """Slack/WhatsApp rows have no installation — they must stay NULL."""
        engine = await _seed_engine()
        async with engine.begin() as conn:
            await _add_org(conn, "org-a", github_org_id="1111")
            await conn.execute(
                sa.text(
                    "INSERT INTO channel_tenant_map (id, provider, provider_scope_id, org_id, created_at) "
                    "VALUES ('row-s', 'slack', 'T123', 'org-a', '2026-01-01 00:00:00')"
                )
            )

        await _upgrade(engine, MIG_026)

        async with engine.connect() as conn:
            got = (await conn.execute(sa.text("SELECT installation_id FROM channel_tenant_map WHERE id='row-s'"))).scalar_one()
        await engine.dispose()

        assert got is None


class TestDedupCollapsesOnlyRedundantRows:
    @pytest.mark.asyncio
    async def test_collapses_same_tenant_duplicates_without_changing_owner(self):
        """Two rows, one installation, SAME tenant: safe to collapse.

        This is the keyspace-split artefact — one row keyed by account id, the
        other by installation id, both owned by org-a. Collapsing changes no
        owner, so no data is lost in any meaningful sense.
        """
        engine = await _seed_engine()
        async with engine.begin() as conn:
            await _add_org(conn, "org-a", github_org_id="1111")
            await _add_mapping(conn, "row-1", "org-a", scope_id="1111", installation_id=INSTALL_X)
            await _add_mapping(conn, "row-2", "org-a", scope_id=INSTALL_X, installation_id=INSTALL_X)

        await _upgrade(engine, MIG_026)

        async with engine.connect() as conn:
            rows = (await conn.execute(sa.text("SELECT org_id FROM channel_tenant_map WHERE installation_id = :i"), {"i": INSTALL_X})).fetchall()
            conflicts = (await conn.execute(sa.text("SELECT count(*) FROM installation_ownership_conflicts"))).scalar_one()
        await engine.dispose()

        assert len(rows) == 1, "redundant same-tenant duplicate should be collapsed"
        assert rows[0][0] == "org-a", "the owner must not change"
        assert conflicts == 0, "a same-tenant duplicate is not an ownership conflict"


class TestCrossTenantConflictsAreQuarantinedNotDeleted:
    @pytest.mark.asyncio
    async def test_quarantines_and_keeps_both_rows(self):
        """The D3 rule: never auto-pick a winner; nothing silently dropped.

        Only GitHub can settle this tie and Alembic cannot ask, so any heuristic
        would be a coin flip on a customer's tenant — and a DELETE is not covered
        by the migration's rollback.
        """
        engine = await _seed_engine()
        async with engine.begin() as conn:
            await _add_org(conn, "org-a", github_org_id="1111")
            await _add_org(conn, "org-b", github_org_id="2222", installation_ids=[INSTALL_X])
            await _add_mapping(conn, "row-1", "org-a", scope_id="1111", installation_id=INSTALL_X)

        await _upgrade(engine, MIG_026)

        async with engine.connect() as conn:
            claims = (
                await conn.execute(
                    sa.text("SELECT org_id, source FROM installation_ownership_conflicts WHERE installation_id = :i ORDER BY org_id"),
                    {"i": INSTALL_X},
                )
            ).fetchall()
            surviving = (await conn.execute(sa.text("SELECT org_id FROM channel_tenant_map WHERE installation_id = :i"), {"i": INSTALL_X})).fetchall()
            org_b_ids = (await conn.execute(sa.text("SELECT github_installation_ids FROM organizations WHERE id='org-b'"))).scalar_one()
            disputed = (
                (await conn.execute(sa.text("SELECT ownership_disputed FROM channel_tenant_map WHERE installation_id = :i"), {"i": INSTALL_X}))
                .scalars()
                .all()
            )
        await engine.dispose()

        assert [c[0] for c in claims] == ["org-a", "org-b"], "both claimants recorded"
        # Nothing silently dropped — the issue's explicit requirement.
        assert [r[0] for r in surviving] == ["org-a"], "org-a's mapping row is preserved"
        assert INSTALL_X in json.loads(org_b_ids), "org-b's JSON claim is preserved"
        assert all(bool(d) for d in disputed), "rows flagged so 027's index skips them"

    @pytest.mark.asyncio
    async def test_027_still_applies_when_a_conflict_exists(self):
        """The constraint must build even on a deployment that already has a
        conflict — otherwise a blocked deploy is the only way to learn of one."""
        engine = await _seed_engine()
        async with engine.begin() as conn:
            await _add_org(conn, "org-a", github_org_id="1111")
            await _add_org(conn, "org-b", github_org_id="2222", installation_ids=[INSTALL_X])
            await _add_mapping(conn, "row-1", "org-a", scope_id="1111", installation_id=INSTALL_X)

        await _upgrade(engine, MIG_026)
        await _upgrade(engine, MIG_027)  # must not raise
        await engine.dispose()


class TestIdempotence:
    @pytest.mark.asyncio
    async def test_026_and_027_are_idempotent_on_rerun(self):
        """Matches the 005/021 precedent: a partial prior apply must re-run cleanly."""
        engine = await _seed_engine()
        async with engine.begin() as conn:
            await _add_org(conn, "org-a", github_org_id="1111")
            await _add_org(conn, "org-b", github_org_id="2222", installation_ids=[INSTALL_Y])
            await _add_mapping(conn, "row-1", "org-a", scope_id="1111", installation_id=INSTALL_X)
            await _add_mapping(conn, "row-2", "org-b", scope_id="2222", installation_id=INSTALL_Y)

        for _ in range(2):
            await _upgrade(engine, MIG_026)
            await _upgrade(engine, MIG_027)

        async with engine.connect() as conn:
            rows = (await conn.execute(sa.text("SELECT count(*) FROM channel_tenant_map"))).scalar_one()
            conflicts = (await conn.execute(sa.text("SELECT count(*) FROM installation_ownership_conflicts"))).scalar_one()
        await engine.dispose()

        assert rows == 2, "a re-run must not duplicate or drop mapping rows"
        assert conflicts == 0, "a re-run must not manufacture conflicts"


class TestDowngrade:
    @pytest.mark.asyncio
    async def test_026_downgrade_drops_column_and_quarantine_table(self):
        """Reversible: the backfill is derivable from sources 026 leaves untouched."""
        engine = await _seed_engine()
        async with engine.begin() as conn:
            await _add_org(conn, "org-a", github_org_id="1111")
            await _add_mapping(conn, "row-1", "org-a", scope_id="1111", installation_id=INSTALL_X)

        await _upgrade(engine, MIG_026)

        async with engine.begin() as conn:
            await conn.run_sync(_run_migration, MIG_026.downgrade)

        def _inspect(sync_conn):
            insp = sa_inspect(sync_conn)
            return {c["name"] for c in insp.get_columns("channel_tenant_map")}, set(insp.get_table_names())

        async with engine.connect() as conn:
            cols, tables = await conn.run_sync(_inspect)
            # The metadata blob is untouched, so the backfill can be redone.
            meta = (await conn.execute(sa.text("SELECT metadata FROM channel_tenant_map WHERE id='row-1'"))).scalar_one()
        await engine.dispose()

        assert "installation_id" not in cols
        assert "ownership_disputed" not in cols
        assert "installation_ownership_conflicts" not in tables
        assert json.loads(meta)["installation_id"] == int(INSTALL_X), "source data preserved"
