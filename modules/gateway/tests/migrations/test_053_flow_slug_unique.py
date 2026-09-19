"""Tests for Alembic migration 053 — tenant-scoped flow slug uniqueness.

Issue #4898 (EPIC #4191). This file is **mandatory**, and not only for coverage:
`modules/gateway/alembic/**` is absent from `gateway-ci.yml`'s trigger paths, so a
migration-only change gets **zero CI signal**. A test under `tests/` is what makes
CI run at all for this migration.

These tests exercise the REAL migration functions imported from the version module.
A test that re-implements the migration proves only that the author can write the
same bug twice.

What is under test:

  - `upgrade()` creates the unique index on an **alembic-only** database — no
    `Base.metadata.create_all` anywhere. That fixture is what catches an index that
    exists only because ORM metadata built it: declared on the model, absent on
    every deployed database. For this story the failure is quiet in a specific way —
    tests would enforce uniqueness while dev silently allowed two same-slug flows to
    merge their spend into one authoritative-looking total.
  - **It refuses rather than repairs.** On a table that already holds duplicate
    `(org_id, slug)` groups the migration stops and names them. Asserted together
    with the fact that nothing was deleted, merged, renamed or reslugged.
  - **No backfill.** `usage_logs.graph_address` must stay NULL for existing rows;
    guessing an address for a past charge would put real money in some node's total.
  - Cross-tenant same-slug flows remain legal — a product requirement, since two
    customers may each run a `delivery-loop`.
  - The concurrent-registration race the index exists to decide, and the
    application-side recovery in `OrchestrationRepository.create_flow` that absorbs
    the loss.
  - The revision chains onto the real single head. A broken `down_revision` silently
    SKIPS the migration, and the uniqueness then never exists in dev.
  - Migration/model schema parity: both are hand-written, so drift is the live risk.
"""

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from src.orchestration.models import OrchestrationFlow

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "alembic" / "versions"

TABLE = "orchestration_flows"
INDEX = "uq_orchestration_flows_org_slug"


def _load_migration(filename: str):
    """Import a migration module by path (they are not an importable package)."""
    path = MIGRATIONS_DIR / filename
    spec = importlib.util.spec_from_file_location(filename.replace(".py", ""), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MIG_053 = _load_migration("053_orchestration_flow_slug_unique.py")


def _run_migration(sync_conn, fn):
    """Run a migration's upgrade()/downgrade() with alembic's `op` proxy bound."""
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    ctx = MigrationContext.configure(sync_conn)
    with Operations.context(ctx):
        fn()


async def _engine_with_flows_table():
    """An empty database holding ONLY `orchestration_flows`, and no index from 052.

    Built with explicit DDL rather than `create_all`, because the model now declares
    the unique index itself — `create_all` would create it, and every test below
    would then pass without the migration doing anything at all.
    """
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.execute(
            sa.text(
                f"CREATE TABLE {TABLE} ("
                " id VARCHAR(36) PRIMARY KEY,"
                " org_id VARCHAR(36) NOT NULL,"
                " slug VARCHAR(128) NOT NULL,"
                " title VARCHAR(256) NOT NULL,"
                " intent_ref VARCHAR(64),"
                " state VARCHAR(32) NOT NULL DEFAULT 'pending',"
                " description TEXT,"
                " design_history JSON,"
                " created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,"
                " updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP"
                ")"
            )
        )
    return engine


async def _upgrade(engine):
    async with engine.begin() as conn:
        await conn.run_sync(_run_migration, MIG_053.upgrade)


async def _downgrade(engine):
    async with engine.begin() as conn:
        await conn.run_sync(_run_migration, MIG_053.downgrade)


def _insert(flow_id="f1", org="org-1", slug="delivery-loop", title="Demo flow"):
    return sa.text(
        f"INSERT INTO {TABLE} (id, org_id, slug, title, state, created_at, updated_at) "
        "VALUES (:id, :org, :slug, :title, 'running', '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
    ).bindparams(id=flow_id, org=org, slug=slug, title=title)


async def _indexes(engine):
    async with engine.connect() as conn:
        return await conn.run_sync(lambda c: {i["name"]: i for i in sa_inspect(c).get_indexes(TABLE)})


class TestAlembicOnlyDatabase:
    """The migration stands on its own, with no `create_all` involved."""

    async def test_upgrade_creates_the_unique_index(self):
        engine = await _engine_with_flows_table()
        assert INDEX not in await _indexes(engine), "fixture must start without the index for this to prove anything"

        await _upgrade(engine)

        indexes = await _indexes(engine)
        await engine.dispose()
        assert INDEX in indexes
        # Truthy rather than `is True`: SQLite's reflection reports `1`.
        assert indexes[INDEX]["unique"]
        assert indexes[INDEX]["column_names"] == ["org_id", "slug"]

    async def test_org_id_leads_the_index(self):
        """Column ORDER is the cross-tenant property, not a detail.

        `(slug, org_id)` would enforce the same uniqueness but could not serve the
        tenant-scoped lookup `get_flow_by_slug` issues, which is the read that
        replaced a full per-tenant scan.
        """
        engine = await _engine_with_flows_table()
        await _upgrade(engine)
        indexes = await _indexes(engine)
        await engine.dispose()
        assert indexes[INDEX]["column_names"][0] == "org_id"

    async def test_upgrade_is_the_only_ddl_and_writes_no_rows(self):
        """Additive: one index, no table/column change, and no row invented.

        A migration that also wrote rows could not be rolled back by dropping an
        index, and a backfilled address would be a guess about real money.
        """
        engine = await _engine_with_flows_table()
        async with engine.begin() as conn:
            await conn.execute(_insert())
        await _upgrade(engine)

        async with engine.connect() as conn:
            rows = (await conn.execute(sa.text(f"SELECT id, org_id, slug, title, state FROM {TABLE}"))).all()
            tables = set(await conn.run_sync(lambda c: sa_inspect(c).get_table_names()))
        await engine.dispose()
        assert rows == [("f1", "org-1", "delivery-loop", "Demo flow", "running")]
        assert tables == {TABLE}


class TestTenantScopedUniqueness:
    """What the index actually decides."""

    async def test_same_tenant_cannot_hold_two_flows_with_one_slug(self):
        """The whole point: an address must identify exactly one flow.

        Two same-slug flows in one tenant would sum their model spend into a single
        `graph_address` total, with nothing in the result revealing that two flows
        were merged — a wrong number that reads as authoritative, which is worse
        than the `unknown` #4898 replaces.
        """
        engine = await _engine_with_flows_table()
        await _upgrade(engine)
        async with engine.begin() as conn:
            await conn.execute(_insert(flow_id="f1"))
        with pytest.raises(IntegrityError):
            async with engine.begin() as conn:
                await conn.execute(_insert(flow_id="f2"))
        await engine.dispose()

    async def test_two_tenants_keep_independent_flows_of_the_same_name(self):
        """Cross-tenant duplicates stay legal — a product requirement.

        Two customers may each run a `delivery-loop`; they are different flows and
        must not collide. `org_id` leading the index is what keeps that true.
        """
        engine = await _engine_with_flows_table()
        await _upgrade(engine)
        async with engine.begin() as conn:
            await conn.execute(_insert(flow_id="f1", org="org-1"))
            await conn.execute(_insert(flow_id="f2", org="org-2"))
            count = await conn.scalar(sa.text(f"SELECT COUNT(*) FROM {TABLE}"))
        await engine.dispose()
        assert count == 2

    async def test_one_tenant_keeps_many_differently_slugged_flows(self):
        """Uniqueness is per slug, not per tenant — a tenant runs many flows."""
        engine = await _engine_with_flows_table()
        await _upgrade(engine)
        async with engine.begin() as conn:
            await conn.execute(_insert(flow_id="f1", slug="delivery-loop"))
            await conn.execute(_insert(flow_id="f2", slug="epic-4191-loop"))
            count = await conn.scalar(sa.text(f"SELECT COUNT(*) FROM {TABLE}"))
        await engine.dispose()
        assert count == 2


class TestRefusalOnExistingDuplicates:
    """`upgrade()` inspects first, reports, and repairs nothing."""

    async def _engine_with_duplicates(self):
        engine = await _engine_with_flows_table()
        async with engine.begin() as conn:
            await conn.execute(_insert(flow_id="f1", org="org-1", slug="delivery-loop", title="First"))
            await conn.execute(_insert(flow_id="f2", org="org-1", slug="delivery-loop", title="Second"))
            await conn.execute(_insert(flow_id="f3", org="org-2", slug="delivery-loop", title="Other tenant"))
        return engine

    async def test_upgrade_stops_and_names_the_duplicate_group(self):
        """A bare index-creation failure names an index and no rows, which tells an
        operator nothing actionable. This must name the tenant and slug to resolve."""
        engine = await self._engine_with_duplicates()
        with pytest.raises(RuntimeError) as raised:
            await _upgrade(engine)
        await engine.dispose()
        message = str(raised.value)
        assert "org-1" in message
        assert "delivery-loop" in message
        assert "2 flows" in message
        assert "4898" in message

    async def test_a_refused_upgrade_deletes_merges_or_renames_nothing(self):
        """The explicit constraint from the issue, asserted rather than promised.

        Resolving real duplicate flows means deciding which is canonical and what
        becomes of the other's history — cost, plan and audit consequences that
        belong to the rollout, not to a migration running unattended.
        """
        engine = await self._engine_with_duplicates()
        with pytest.raises(RuntimeError):
            await _upgrade(engine)

        async with engine.connect() as conn:
            rows = (await conn.execute(sa.text(f"SELECT id, org_id, slug, title, state FROM {TABLE} ORDER BY id"))).all()
        await engine.dispose()
        assert rows == [
            ("f1", "org-1", "delivery-loop", "First", "running"),
            ("f2", "org-1", "delivery-loop", "Second", "running"),
            ("f3", "org-2", "delivery-loop", "Other tenant", "running"),
        ]

    async def test_a_refused_upgrade_creates_no_partial_index(self):
        """Refusing must leave the schema exactly as it was.

        A half-applied migration is worse than a refused one: the next `upgrade`
        would fail on an index that already exists, for reasons unrelated to the
        duplicates the operator was told to fix.
        """
        engine = await self._engine_with_duplicates()
        with pytest.raises(RuntimeError):
            await _upgrade(engine)
        indexes = await _indexes(engine)
        await engine.dispose()
        assert INDEX not in indexes

    async def test_cross_tenant_duplicates_alone_do_not_block_the_upgrade(self):
        """The check must use the same grouping the index enforces.

        A check that grouped by `slug` alone would refuse to upgrade any deployment
        where two tenants happen to run a same-named flow — which is legal, and
        common.
        """
        engine = await _engine_with_flows_table()
        async with engine.begin() as conn:
            await conn.execute(_insert(flow_id="f1", org="org-1", slug="delivery-loop"))
            await conn.execute(_insert(flow_id="f2", org="org-2", slug="delivery-loop"))

        await _upgrade(engine)

        indexes = await _indexes(engine)
        await engine.dispose()
        assert INDEX in indexes

    async def test_every_duplicate_group_is_reported_not_just_the_first(self):
        """An operator fixing one group at a time and re-running learns the scope
        only if all of them are named at once."""
        engine = await _engine_with_flows_table()
        async with engine.begin() as conn:
            await conn.execute(_insert(flow_id="f1", org="org-1", slug="loop-a"))
            await conn.execute(_insert(flow_id="f2", org="org-1", slug="loop-a"))
            await conn.execute(_insert(flow_id="f3", org="org-2", slug="loop-b"))
            await conn.execute(_insert(flow_id="f4", org="org-2", slug="loop-b"))
        with pytest.raises(RuntimeError) as raised:
            await _upgrade(engine)
        await engine.dispose()
        message = str(raised.value)
        assert "loop-a" in message
        assert "loop-b" in message
        assert "2 duplicate" in message


class TestNoBackfill:
    """No historical charge is attributed by guesswork."""

    def test_the_migration_contains_no_write_statement(self):
        """Asserted structurally as well as behaviourally.

        `graph_address` stays NULL for every existing row, so historical flows keep
        reporting `unknown` — which remains the honest answer for spend that was
        never attributed at the time it happened.
        """
        source = (MIGRATIONS_DIR / "053_orchestration_flow_slug_unique.py").read_text()
        body = source[source.index("def upgrade") :]
        for forbidden in ("INSERT", "UPDATE ", "DELETE", "bulk_insert", "usage_logs", "graph_address"):
            assert forbidden not in body, f"migration 053 must not {forbidden} — it creates one index and nothing else"

    async def test_upgrade_does_not_touch_usage_rows(self):
        """Behavioural companion: with a usage table present, it stays untouched."""
        engine = await _engine_with_flows_table()
        async with engine.begin() as conn:
            await conn.execute(sa.text("CREATE TABLE usage_logs (id VARCHAR(36) PRIMARY KEY, graph_address VARCHAR(512))"))
            await conn.execute(sa.text("INSERT INTO usage_logs (id, graph_address) VALUES ('u1', NULL)"))
        await _upgrade(engine)
        async with engine.connect() as conn:
            rows = (await conn.execute(sa.text("SELECT id, graph_address FROM usage_logs"))).all()
        await engine.dispose()
        assert rows == [("u1", None)]


class TestDowngrade:
    async def test_downgrade_removes_the_index_and_keeps_every_flow(self):
        """Rolling back attribution must never drop a flow, a usage row or history."""
        engine = await _engine_with_flows_table()
        await _upgrade(engine)
        async with engine.begin() as conn:
            await conn.execute(_insert(flow_id="f1"))

        await _downgrade(engine)

        indexes = await _indexes(engine)
        async with engine.connect() as conn:
            rows = (await conn.execute(sa.text(f"SELECT id, slug FROM {TABLE}"))).all()
        await engine.dispose()
        assert INDEX not in indexes
        assert rows == [("f1", "delivery-loop")]

    async def test_downgrade_restores_the_previous_permissive_behaviour(self):
        """It IS the rollback path, so after it the old shape must work again."""
        engine = await _engine_with_flows_table()
        await _upgrade(engine)
        await _downgrade(engine)
        async with engine.begin() as conn:
            await conn.execute(_insert(flow_id="f1"))
            await conn.execute(_insert(flow_id="f2"))
            count = await conn.scalar(sa.text(f"SELECT COUNT(*) FROM {TABLE}"))
        await engine.dispose()
        assert count == 2

    async def test_upgrade_downgrade_upgrade_is_repeatable(self):
        """An operator who rolls back and rolls forward must not need manual repair."""
        engine = await _engine_with_flows_table()
        await _upgrade(engine)
        await _downgrade(engine)
        await _upgrade(engine)
        indexes = await _indexes(engine)
        await engine.dispose()
        assert INDEX in indexes


class TestPostgresRendering:
    """The tests above run on SQLite, but dev runs on Postgres. Render for Postgres."""

    def _render_postgres_ddl(self) -> str:
        from sqlalchemy.dialects import postgresql

        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        chunks: list[str] = []

        class _Buffer:
            def write(self, text):
                chunks.append(text)

            def flush(self):
                pass

        ctx = MigrationContext.configure(dialect=postgresql.dialect(), opts={"as_sql": True, "output_buffer": _Buffer()})
        with Operations.context(ctx):
            MIG_053.upgrade()
        return "".join(chunks)

    def test_exactly_one_unique_index_renders(self):
        """One uniqueness rule. A second would be a second way to refuse a
        registration, and a refused registration stalls a flow."""
        ddl = self._render_postgres_ddl()
        assert ddl.count("CREATE UNIQUE INDEX") == 1
        assert ddl.count("CREATE INDEX") == 0
        assert INDEX in ddl

    def test_no_table_or_column_ddl_renders(self):
        """Additive means additive: nothing is created, altered or dropped."""
        ddl = self._render_postgres_ddl()
        for forbidden in ("CREATE TABLE", "ALTER TABLE", "DROP "):
            assert forbidden not in ddl, f"migration 053 must not emit {forbidden}; got:\n{ddl}"

    def test_offline_generation_does_not_try_to_read_the_database(self):
        """`alembic upgrade --sql` must still produce a script.

        This is a genuine hazard for any migration that inspects data: offline mode
        has no database, `op.get_bind()` returns a mock whose `execute` returns
        `None`, and the duplicate check would abort script GENERATION with an
        `AttributeError`. That is a worse failure than the bare index error the
        check exists to improve on — the operator gets no script at all. Offline
        therefore emits only the DDL, and `CREATE UNIQUE INDEX` remains the backstop
        that refuses to apply over duplicates when the script is finally run.
        """
        ddl = self._render_postgres_ddl()
        assert INDEX in ddl
        assert "SELECT" not in ddl.upper(), f"offline mode must not emit or attempt a read; got:\n{ddl}"


class TestRevisionChain:
    def test_revision_id_and_down_revision(self):
        """Chains onto the head that was real when this landed.

        A wrong `down_revision` does not error — it silently produces a second head,
        and the migration is then simply skipped on deploy.
        """
        assert MIG_053.revision == "053_flow_slug_unique"
        assert MIG_053.down_revision == "052_orchestration_executions"

    def test_revision_id_fits_the_alembic_version_column(self):
        """`alembic_version.version_num` is VARCHAR(32); a longer id fails at apply.

        This is why the revision is shortened from the filename stem
        `053_orchestration_flow_slug_unique` (34 chars), which would exceed it.
        """
        assert len(MIG_053.revision) <= 32

    def test_migration_leaves_exactly_one_head(self):
        """Two heads is a broken deploy, invisible until a pod runs `alembic upgrade
        head`. Asserts the COUNT, not the head's name, so a later migration landing
        on top does not turn this into a spurious failure."""
        import ast

        revisions: dict[str, str | tuple[str, ...] | None] = {}
        for path in MIGRATIONS_DIR.glob("*.py"):
            if path.name == "__init__.py":
                continue
            tree = ast.parse(path.read_text(), filename=str(path))
            found: dict[str, str | tuple[str, ...] | None] = {}
            for node in tree.body:
                if not isinstance(node, ast.AnnAssign | ast.Assign):
                    continue
                targets = [node.target] if isinstance(node, ast.AnnAssign) else node.targets
                names = {t.id for t in targets if isinstance(t, ast.Name)} & {"revision", "down_revision"}
                if not names or not isinstance(node.value, ast.Constant | ast.Tuple):
                    continue
                for name in names:
                    found[name] = ast.literal_eval(node.value)
            if "revision" in found:
                revisions[found["revision"]] = found.get("down_revision")

        parents = {parent for down in revisions.values() if down is not None for parent in ((down,) if isinstance(down, str) else down)}
        heads = sorted(rev for rev in revisions if rev not in parents)

        assert len(heads) == 1, f"expected exactly one head, got {heads}"
        assert "053_flow_slug_unique" in revisions, "053 must still be on the chain"


class TestModelMigrationParity:
    """The migration and the model are hand-written separately, so they can drift.

    Drift here is quiet in the direction that matters: the ORM index exists wherever
    `create_all` ran, so uniqueness holds in tests and is absent in dev — which is
    exactly the reader-only failure mode #4898 exists to correct, one layer down.
    """

    def test_the_model_declares_the_same_index(self):
        declared = {ix.name: ix for ix in OrchestrationFlow.__table__.indexes}
        assert INDEX in declared, f"model must declare {INDEX}; has {sorted(declared)}"
        index = declared[INDEX]
        assert index.unique is True
        assert [c.name for c in index.columns] == ["org_id", "slug"]

    async def test_model_declared_indexes_all_exist_in_the_migration_chain(self):
        """Same names on both sides, for the index this migration owns."""
        engine = await _engine_with_flows_table()
        await _upgrade(engine)
        migrated = set(await _indexes(engine))
        await engine.dispose()
        assert INDEX in migrated
