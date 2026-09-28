"""Tests for Alembic migration 052 — the execution/action delivery ledger.

Issue #5142 (ENGINE-K1, parent #5122). This file is **mandatory**, and not only
for coverage: `modules/gateway/alembic/**` is absent from `gateway-ci.yml`'s
trigger paths, so a migration-only change gets **zero CI signal**. A test under
`tests/` is what makes CI run at all for this migration.

These tests exercise the REAL migration functions imported from the version
module. A test that re-implements the migration proves only that the author can
write the same bug twice.

What is under test:

  - `upgrade()` creates both tables and their indexes on an **alembic-only**
    database — no `Base.metadata.create_all` anywhere. This is the case that
    catches a migration which "works" only because `create_all` already built the
    tables, which is the failure mode where a table is declared in models, has no
    DDL, and is absent on every deployed database.
  - `upgrade / downgrade / upgrade` round-trips, because the story's rollback plan
    prescribes a downgrade and a rollback path that has never been run is not a
    rollback path. (Against a *disposable* database only — a shared environment is
    never downgraded as a validation step.)
  - **The two uniqueness invariants actually refuse a duplicate.** These are the
    whole point of the ledger: without the execution index, two concurrent starts
    both insert and the work has two identities; without the action index, a retry
    after a crash opens a second pull request. Asserted by inserting the duplicate
    and requiring an `IntegrityError`, not by reading the index definition —
    an index can exist with the wrong columns and still be named correctly.
  - **Cross-tenant keys cannot collide into one row**: the same node id under two
    org ids is two distinct executions, which is what makes the unique index
    tenant-scoped rather than global.
  - Postgres rendering, because the tests run on SQLite but dev runs on Postgres:
    timezone-aware timestamps, `JSONB` for `detail`, and the expected table/index
    counts. SQLite would not notice any of these.
  - The revision chains onto the real single head, and the chain still has exactly
    one head. A broken `down_revision` silently SKIPS the migration, and live code
    then queries absent tables.
  - Migration/model schema parity for both tables: both files are hand-written, so
    drift is the live risk — the migration is what runs in the deployed database,
    the models are what the tests use. A mismatch passes every test and raises
    `UndefinedColumn` in dev.
  - **No backfill**: the migration creates two tables and touches nothing else.
"""

import ast
import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from src.orchestration.models import (
    OrchestrationAcceptedPlan,
    OrchestrationAction,
    OrchestrationExecution,
    OrchestrationFlow,
    OrchestrationNode,
)

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "alembic" / "versions"

EXECUTIONS = "orchestration_executions"
ACTIONS = "orchestration_actions"
EXEC_INDEX = "uq_orchestration_executions_cycle"
ACTION_INDEX = "uq_orchestration_actions_operation"
DUE_INDEX = "ix_orchestration_executions_due"

ORG_A = "org-alpha"
ORG_B = "org-beta"
FLOW = "flow-1"
NODE = "node-1"
TS = "2026-09-18 00:00:00"


def _load_migration(filename: str):
    """Import a migration module by path (they are not an importable package)."""
    path = MIGRATIONS_DIR / filename
    spec = importlib.util.spec_from_file_location(filename.replace(".py", ""), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MIG_052 = _load_migration("052_orchestration_executions.py")


def _run_migration(sync_conn, fn):
    """Run a migration's upgrade()/downgrade() with alembic's `op` proxy bound.

    The version module calls the module-level `op` proxy, so it must point at a
    real Operations object for the duration. This runs the migration as written
    rather than a paraphrase of it.
    """
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    ctx = MigrationContext.configure(sync_conn)
    with Operations.context(ctx):
        fn()


async def _bare_engine():
    """An engine carrying ONLY the FK parents the migration needs.

    The two tables under test have foreign keys to `orchestration_flows` and
    `orchestration_nodes`, so those must exist for the DDL to apply — but neither
    table under test is created by `create_all` here. That is the point: `create_all`
    on the full metadata would build them from the ORM and mask a migration that
    never creates them itself.

    SQLite does not enforce foreign keys unless `PRAGMA foreign_keys` is on, which
    is left off deliberately: this file tests the migration's DDL, and FK
    enforcement behavior belongs with the store's own tests.
    """
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(OrchestrationFlow.__table__.create)
        await conn.run_sync(OrchestrationAcceptedPlan.__table__.create)
        await conn.run_sync(OrchestrationNode.__table__.create)
    return engine


async def _upgrade(engine):
    async with engine.begin() as conn:
        await conn.run_sync(_run_migration, MIG_052.upgrade)


async def _downgrade(engine):
    async with engine.begin() as conn:
        await conn.run_sync(_run_migration, MIG_052.downgrade)


def _insert_execution(
    *,
    exec_id="exec-1",
    org=ORG_A,
    node=NODE,
    cycle=1,
    status="runnable",
    phase="admitted",
):
    return sa.text(
        f"INSERT INTO {EXECUTIONS} "
        "(id, org_id, flow_id, node_id, cycle, phase, status, revision, "
        " accepted_plan_version, claim_id, claim_generation, attempts, created_at) "
        "VALUES (:id, :org, :flow, :node, :cycle, :phase, :status, 1, 3, 'claim-1', 1, 0, :ts)"
    ).bindparams(id=exec_id, org=org, flow=FLOW, node=node, cycle=cycle, phase=phase, status=status, ts=TS)


def _insert_action(*, action_id="act-1", org=ORG_A, execution="exec-1", key="open_pr:node-1:cycle-1", status="prepared"):
    return sa.text(
        f"INSERT INTO {ACTIONS} "
        "(id, org_id, execution_id, operation_key, kind, status, attempt, created_at) "
        "VALUES (:id, :org, :execution, :key, 'open_pr', :status, 1, :ts)"
    ).bindparams(id=action_id, org=org, execution=execution, key=key, status=status, ts=TS)


class TestAlembicOnlyDatabase:
    """Both tables stand up from the migration alone, with no `create_all`."""

    async def test_upgrade_creates_both_tables(self):
        engine = await _bare_engine()
        async with engine.connect() as conn:
            before = set(await conn.run_sync(lambda c: sa_inspect(c).get_table_names()))
        assert EXECUTIONS not in before and ACTIONS not in before, "fixture must not pre-create the tables under test"

        await _upgrade(engine)

        async with engine.connect() as conn:
            after = set(await conn.run_sync(lambda c: sa_inspect(c).get_table_names()))
        await engine.dispose()

        assert EXECUTIONS in after, f"upgrade() did not create {EXECUTIONS}"
        assert ACTIONS in after, f"upgrade() did not create {ACTIONS}"

    async def test_rows_are_insertable_after_alembic_only_upgrade(self):
        """The tables are usable, not merely present.

        Catches a shape the application cannot write to — e.g. a NOT NULL column
        no store method populates, which applies cleanly and then fails on the
        first real write.
        """
        engine = await _bare_engine()
        await _upgrade(engine)

        async with engine.begin() as conn:
            await conn.execute(_insert_execution())
            await conn.execute(_insert_action())

        async with engine.connect() as conn:
            execution = (await conn.execute(sa.text(f"SELECT phase, status, revision, attempts FROM {EXECUTIONS}"))).one()
            action = (await conn.execute(sa.text(f"SELECT operation_key, status FROM {ACTIONS}"))).one()
        await engine.dispose()

        assert execution == ("admitted", "runnable", 1, 0)
        assert action == ("open_pr:node-1:cycle-1", "prepared")

    async def test_nullable_reference_columns_accept_null(self):
        """The sanitized reference columns are optional.

        `pending_action_key` / `notification_receipt_ref` / `handoff_receipt_ref`
        are NULL for an execution with nothing outstanding, which is the ordinary
        case. A NOT NULL there would force every caller to invent a value.
        """
        engine = await _bare_engine()
        await _upgrade(engine)

        async with engine.begin() as conn:
            await conn.execute(_insert_execution())

        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    sa.text(f"SELECT pending_action_key, notification_receipt_ref, handoff_receipt_ref, block_code, next_check_at FROM {EXECUTIONS}")
                )
            ).one()
        await engine.dispose()

        assert row == (None, None, None, None, None)


class TestUniquenessInvariants:
    """The two indexes the ledger's correctness rests on.

    Asserted by attempting the duplicate insert rather than by reading the index
    definition: an index can exist, be named exactly right, and cover the wrong
    columns. Only a refused insert proves the constraint.
    """

    async def test_one_execution_per_node_per_cycle(self):
        """A second start for the same (org, node, cycle) is refused.

        Without this, two concurrent starts both pass an application-level "is
        there one already?" read and both insert — and the work then has two
        durable identities, each unaware of the other's actions.
        """
        engine = await _bare_engine()
        await _upgrade(engine)

        async with engine.begin() as conn:
            await conn.execute(_insert_execution(exec_id="exec-1"))

        with pytest.raises(IntegrityError):
            async with engine.begin() as conn:
                await conn.execute(_insert_execution(exec_id="exec-2"))
        await engine.dispose()

    async def test_a_new_cycle_of_the_same_node_is_allowed(self):
        """Cycle is part of the key, so a repair cycle gets its own row.

        This is the other half of the invariant: uniqueness must not be so broad
        that re-delivery is impossible. A second cycle is new work with its own
        attempts and actions, and overwriting the first would destroy the record of
        what the first cycle already did externally.
        """
        engine = await _bare_engine()
        await _upgrade(engine)

        async with engine.begin() as conn:
            await conn.execute(_insert_execution(exec_id="exec-1", cycle=1))
            await conn.execute(_insert_execution(exec_id="exec-2", cycle=2))

        async with engine.connect() as conn:
            count = (await conn.execute(sa.text(f"SELECT COUNT(*) FROM {EXECUTIONS}"))).scalar_one()
        await engine.dispose()

        assert count == 2

    async def test_cross_tenant_node_ids_do_not_collide(self):
        """The same node id under two tenants is two executions, not a conflict.

        `org_id` leads the unique index, so uniqueness is tenant-scoped. A global
        index would make one tenant's insert refuse another tenant's — a
        cross-tenant interference that the story explicitly requires cannot happen.
        """
        engine = await _bare_engine()
        await _upgrade(engine)

        async with engine.begin() as conn:
            await conn.execute(_insert_execution(exec_id="exec-a", org=ORG_A))
            await conn.execute(_insert_execution(exec_id="exec-b", org=ORG_B))

        async with engine.connect() as conn:
            orgs = sorted((await conn.execute(sa.text(f"SELECT org_id FROM {EXECUTIONS}"))).scalars())
        await engine.dispose()

        assert orgs == [ORG_A, ORG_B]

    async def test_one_action_per_operation_key(self):
        """A repeated operation key is refused — this is the idempotency backstop.

        The store returns the original record for a duplicate key, but that check
        is a read that two concurrent preparers can both pass. This index is what
        makes "one pull request, not two" hold when they do.
        """
        engine = await _bare_engine()
        await _upgrade(engine)

        async with engine.begin() as conn:
            await conn.execute(_insert_execution())
            await conn.execute(_insert_action(action_id="act-1"))

        with pytest.raises(IntegrityError):
            async with engine.begin() as conn:
                await conn.execute(_insert_action(action_id="act-2"))
        await engine.dispose()

    async def test_different_operation_keys_coexist(self):
        """One execution takes many distinct steps."""
        engine = await _bare_engine()
        await _upgrade(engine)

        async with engine.begin() as conn:
            await conn.execute(_insert_execution())
            await conn.execute(_insert_action(action_id="act-1", key="push_branch:node-1:cycle-1"))
            await conn.execute(_insert_action(action_id="act-2", key="open_pr:node-1:cycle-1"))

        async with engine.connect() as conn:
            count = (await conn.execute(sa.text(f"SELECT COUNT(*) FROM {ACTIONS}"))).scalar_one()
        await engine.dispose()

        assert count == 2


class TestIndexes:
    async def _indexes(self, table):
        engine = await _bare_engine()
        await _upgrade(engine)
        async with engine.connect() as conn:
            indexes = await conn.run_sync(lambda c: sa_inspect(c).get_indexes(table))
        await engine.dispose()
        return {index["name"]: index for index in indexes}

    async def test_execution_unique_index_covers_the_identity_and_nothing_else(self):
        """Exactly (org_id, node_id, cycle).

        A narrower index would refuse a legitimate second cycle; a wider one would
        admit a second identity for the same cycle. Both are silent until
        production, so the column list is pinned.
        """
        indexes = await self._indexes(EXECUTIONS)
        assert EXEC_INDEX in indexes, f"{EXEC_INDEX} is missing; the identity invariant would be advisory"
        # SQLite's inspector reports `unique` as 1 rather than True, so this is a
        # truthiness check by necessity, not by looseness.
        assert indexes[EXEC_INDEX]["unique"]
        assert indexes[EXEC_INDEX]["column_names"] == ["org_id", "node_id", "cycle"]

    async def test_action_unique_index_covers_the_operation_key(self):
        indexes = await self._indexes(ACTIONS)
        assert ACTION_INDEX in indexes
        assert indexes[ACTION_INDEX]["unique"]
        assert indexes[ACTION_INDEX]["column_names"] == ["org_id", "execution_id", "operation_key"]

    async def test_due_work_index_exists_and_is_not_unique(self):
        """The runner's read path.

        Without it, "what is due?" scans every execution the tenant has ever had,
        which degrades as concluded rows accumulate — and it must NOT be unique,
        since many executions share a status.
        """
        indexes = await self._indexes(EXECUTIONS)
        assert DUE_INDEX in indexes
        assert not indexes[DUE_INDEX]["unique"]
        assert indexes[DUE_INDEX]["column_names"] == ["org_id", "status", "next_check_at"]

    async def test_tenant_index_from_tenant_mixin_is_created(self):
        """`TenantMixin` declares `org_id` with `index=True`.

        The migration must create it or the deployed schema and the models
        disagree — the exact drift class this file exists to catch.
        """
        for table in (EXECUTIONS, ACTIONS):
            indexes = await self._indexes(table)
            assert f"ix_{table}_org_id" in indexes, f"{table} is missing its org_id index"


class TestDowngrade:
    """The rollback path the story prescribes, actually run.

    Against a disposable in-memory database only. A shared environment is never
    downgraded as a validation step — the story says so explicitly, and this test
    is not evidence that it is safe to do there.
    """

    async def test_downgrade_removes_both_tables(self):
        engine = await _bare_engine()
        await _upgrade(engine)
        await _downgrade(engine)

        async with engine.connect() as conn:
            tables = set(await conn.run_sync(lambda c: sa_inspect(c).get_table_names()))
        await engine.dispose()

        assert EXECUTIONS not in tables
        assert ACTIONS not in tables

    async def test_downgrade_leaves_the_pre_existing_tables_alone(self):
        """Rollback must not take the graph with it.

        The tables this migration depends on are not its to drop; dropping a
        parent would make the rollback destructive far beyond its own scope.
        """
        engine = await _bare_engine()
        await _upgrade(engine)
        await _downgrade(engine)

        async with engine.connect() as conn:
            tables = set(await conn.run_sync(lambda c: sa_inspect(c).get_table_names()))
        await engine.dispose()

        assert "orchestration_flows" in tables
        assert "orchestration_nodes" in tables

    async def test_upgrade_downgrade_upgrade_round_trips(self):
        """The sequence the story names as validation.

        A downgrade that leaves an index behind makes the next upgrade fail with
        "already exists" — which is only ever discovered by running the cycle.
        """
        engine = await _bare_engine()
        await _upgrade(engine)
        await _downgrade(engine)
        await _upgrade(engine)

        async with engine.begin() as conn:
            await conn.execute(_insert_execution())

        async with engine.connect() as conn:
            count = (await conn.execute(sa.text(f"SELECT COUNT(*) FROM {EXECUTIONS}"))).scalar_one()
        await engine.dispose()

        assert count == 1, "the table must be usable after a full upgrade/downgrade/upgrade cycle"


class TestNoBackfill:
    async def test_migration_creates_two_tables_and_writes_no_rows(self):
        """Additive only.

        No backfill is possible here and inventing one would be wrong, not merely
        unnecessary: an execution row asserts an accepted plan version and a claim
        generation that authorized it, and neither is recoverable for work already
        in flight. Rows invented at deploy time would assert authority nobody
        verified.
        """
        engine = await _bare_engine()
        async with engine.connect() as conn:
            before = set(await conn.run_sync(lambda c: sa_inspect(c).get_table_names()))

        await _upgrade(engine)

        async with engine.connect() as conn:
            after = set(await conn.run_sync(lambda c: sa_inspect(c).get_table_names()))
            executions = (await conn.execute(sa.text(f"SELECT COUNT(*) FROM {EXECUTIONS}"))).scalar_one()
            actions = (await conn.execute(sa.text(f"SELECT COUNT(*) FROM {ACTIONS}"))).scalar_one()
        await engine.dispose()

        assert after - before == {EXECUTIONS, ACTIONS}, "the migration must add exactly these two tables"
        assert executions == 0
        assert actions == 0


class TestPostgresRendering:
    """The tests run on SQLite, but dev runs on Postgres. Render for Postgres.

    Nothing else in this file would notice a Postgres-only DDL problem, because
    SQLite is more permissive. Rendering the migration in alembic's offline
    (`--sql`) mode against the Postgres dialect exercises the compiler that
    actually matters, with no live database.
    """

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

        ctx = MigrationContext.configure(
            dialect=postgresql.dialect(),
            opts={"as_sql": True, "output_buffer": _Buffer()},
        )
        with Operations.context(ctx):
            MIG_052.upgrade()
        return "".join(chunks)

    def test_timestamps_are_timezone_aware_on_postgres(self):
        """A naive timestamp makes scheduling ambiguous across zones.

        It has a concrete failure mode here: the store compares `next_check_at` and
        `deadline_at` against an aware `now()`, and a naive column turns that into a
        `TypeError` at the moment work is picked up — failing closed for the whole
        tenant's due work.
        """
        ddl = self._render_postgres_ddl()
        assert "TIMESTAMP WITH TIME ZONE" in ddl
        assert ddl.count("TIMESTAMP WITHOUT TIME ZONE") == 0

    def test_detail_renders_as_jsonb(self):
        """`JSONB`, not `JSON`.

        A bare `sa.JSON()` renders as `JSON` on Postgres and forfeits indexing and
        the binary representation. SQLite would never reveal the difference, since
        it stores both as text.
        """
        ddl = self._render_postgres_ddl()
        assert "detail JSONB" in ddl, f"detail must render as JSONB on Postgres; got:\n{ddl[:3000]}"

    def test_two_tables_and_two_unique_indexes_render(self):
        ddl = self._render_postgres_ddl()
        assert ddl.count("CREATE TABLE") == 2
        assert ddl.count("CREATE UNIQUE INDEX") == 2
        assert EXEC_INDEX in ddl
        assert ACTION_INDEX in ddl

    def test_foreign_keys_render_with_cascade(self):
        """Ledger rows do not outlive the graph they describe.

        An execution whose node was deleted is unreadable bookkeeping that would
        still be returned by a tenant-wide read model query.
        """
        ddl = self._render_postgres_ddl()
        assert "REFERENCES orchestration_nodes (id) ON DELETE CASCADE" in ddl
        assert "REFERENCES orchestration_executions (id) ON DELETE CASCADE" in ddl


class TestRevisionChain:
    def test_revision_id_and_down_revision(self):
        """Chains onto the head that was real when this landed.

        `051_orch_pr_bindings` was the single head at authoring time.
        `044_ratelimit_org_type` and `045_pricing_seed_2026_09_12_1` are not heads:
        `046_merge_pricing_ratelimit` merges both through a tuple `down_revision`.
        """
        assert MIG_052.revision == "052_orchestration_executions"
        assert MIG_052.down_revision == "051_orch_pr_bindings"

    def test_revision_id_fits_the_alembic_version_column(self):
        """`alembic_version.version_num` is VARCHAR(32); a longer id fails at apply."""
        assert len(MIG_052.revision) <= 32

    def test_migration_leaves_exactly_one_head(self):
        """Two heads is a broken deploy, invisible until a pod runs `alembic upgrade head`.

        Asserts the *count*, not the head's name: the head advances with every
        migration that lands, and a name-pinned assertion turns every future
        migration into a spurious failure here. What must never change is that
        there is exactly one head and that 052 is still on the chain.
        """
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
        assert "052_orchestration_executions" in revisions, "052 must still be on the chain"


class TestModelMigrationParity:
    """The migration and the models are hand-written separately, so they can drift.

    The migration is what runs against dev; the models are what every test uses.
    When they disagree, the tests pass and the deployed database raises
    `UndefinedColumn` on the first real query — so compare them directly.
    """

    async def _migrated_columns(self, table):
        engine = await _bare_engine()
        await _upgrade(engine)
        async with engine.connect() as conn:
            columns = await conn.run_sync(lambda c: sa_inspect(c).get_columns(table))
        await engine.dispose()
        return {column["name"]: column for column in columns}

    @pytest.mark.parametrize(
        ("table", "model"),
        [(EXECUTIONS, OrchestrationExecution), (ACTIONS, OrchestrationAction)],
    )
    async def test_column_names_match_the_model(self, table, model):
        migrated = set(await self._migrated_columns(table))
        declared = {column.name for column in model.__table__.columns}
        assert migrated == declared, f"{table} drift — migration-only: {sorted(migrated - declared)}, model-only: {sorted(declared - migrated)}"

    @pytest.mark.parametrize(
        ("table", "model"),
        [(EXECUTIONS, OrchestrationExecution), (ACTIONS, OrchestrationAction)],
    )
    async def test_column_types_match_the_model(self, table, model):
        from sqlalchemy.dialects import sqlite

        dialect = sqlite.dialect()
        migrated_columns = await self._migrated_columns(table)
        declared = {column.name: str(column.type.compile(dialect=dialect)) for column in model.__table__.columns}
        migrated = {name: str(column["type"].compile(dialect=dialect)) for name, column in migrated_columns.items()}
        assert migrated == declared, f"{table} column types drifted between migration and model"

    @pytest.mark.parametrize(
        ("table", "model"),
        [(EXECUTIONS, OrchestrationExecution), (ACTIONS, OrchestrationAction)],
    )
    async def test_nullability_matches_the_model(self, table, model):
        """Nullability drift is the dangerous half of parity.

        A column the model believes optional but the database requires fails only
        on the write path that omits it — which may be a recovery path exercised
        for the first time during an incident.
        """
        migrated_columns = await self._migrated_columns(table)
        declared = {column.name: column.nullable for column in model.__table__.columns}
        migrated = {name: column["nullable"] for name, column in migrated_columns.items()}
        assert migrated == declared, f"{table} nullability drifted between migration and model"
