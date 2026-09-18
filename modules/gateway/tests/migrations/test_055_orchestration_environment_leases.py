"""Tests for Alembic migration 054 — environment leases.

Issue #5150 (ENGINE-D1, parent #5131). This file is **mandatory**, and not only
for coverage: `modules/gateway/alembic/**` is absent from `gateway-ci.yml`'s
trigger paths, so a migration-only change gets **zero CI signal**. A test under
`tests/` is what makes CI run at all for this migration.

These tests exercise the REAL migration functions imported from the version module.
A test that re-implements the migration proves only that the author can write the
same bug twice.

What is under test:

  - `upgrade()` creates the table and its indexes on an **alembic-only** database —
    no `Base.metadata.create_all` anywhere. This catches a migration that "works"
    only because `create_all` already built the table, which is the failure mode
    where a table is declared in models, has no DDL, and is absent on every
    deployed database.
  - `upgrade / downgrade / upgrade` round-trips, because the story's rollback plan
    prescribes a downgrade and a rollback path that has never been run is not a
    rollback path. (Against a *disposable* database only — a shared environment is
    never downgraded as a validation step.)
  - **The unique index is GLOBAL, not tenant-scoped.** This is the single most
    consequential assertion in the file. Every other unique index in this chain
    leads with `org_id`, so this one looks like an omission and will invite a
    "fix". Adding `org_id` would let two tenants' aliases for one physical cluster
    both be held at once — the precise defect the table exists to prevent — and it
    would pass every store-level test, because the store never asks the database to
    enforce tenancy here. It is asserted twice: by inserting the cross-tenant
    duplicate and requiring an `IntegrityError`, and by reading the index columns.
    The insert is the real proof (an index can exist, be named exactly right, and
    cover the wrong columns); the column read is what names the intent for whoever
    reads the failure.
  - Postgres rendering, because the tests run on SQLite but dev runs on Postgres:
    timezone-aware timestamps and the expected index shape. SQLite would not notice
    either.
  - The revision chains onto the real single head, and the chain still has exactly
    one head. A broken `down_revision` silently SKIPS the migration, and live code
    then queries an absent table.
  - Migration/model schema parity: both files are hand-written, so drift is the live
    risk — the migration is what runs in the deployed database, the models are what
    the tests use. A mismatch passes every test and raises `UndefinedColumn` in dev.
  - **No backfill**: the migration creates one table and touches nothing else.
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

from src.orchestration.models import OrchestrationEnvironmentLease

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "alembic" / "versions"

LEASES = "orchestration_environment_leases"
TARGET_INDEX = "uq_orchestration_environment_leases_target"
OWNER_INDEX = "ix_orchestration_environment_leases_owner"
EXPIRY_INDEX = "ix_orchestration_environment_leases_expiry"

ORG_A = "org-alpha"
ORG_B = "org-beta"
KEY = "v1:" + "a" * 64
TS = "2026-09-18 00:00:00"


def _load_migration(filename: str):
    """Import a migration module by path (they are not an importable package)."""
    path = MIGRATIONS_DIR / filename
    spec = importlib.util.spec_from_file_location(filename.replace(".py", ""), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MIG_054 = _load_migration("055_orchestration_environment_leases.py")


def _run_migration(sync_conn, fn):
    """Run a migration's upgrade()/downgrade() with alembic's `op` proxy bound.

    The version module calls the module-level `op` proxy, so it must point at a real
    Operations object for the duration. This runs the migration as written rather
    than a paraphrase of it.
    """
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    ctx = MigrationContext.configure(sync_conn)
    with Operations.context(ctx):
        fn()


async def _bare_engine():
    """An engine with NOTHING pre-created.

    The lease table has no foreign keys, so nothing needs to exist first — and
    deliberately nothing does. `create_all` on the full metadata would build the
    table from the ORM and mask a migration that never creates it itself, which is
    exactly the class of bug this file exists to catch.
    """
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    return engine


async def _upgrade(engine):
    async with engine.begin() as conn:
        await conn.run_sync(_run_migration, MIG_054.upgrade)


async def _downgrade(engine):
    async with engine.begin() as conn:
        await conn.run_sync(_run_migration, MIG_054.downgrade)


def _insert_lease(
    *,
    lease_id="lease-1",
    key=KEY,
    org=ORG_A,
    action="action-1",
    state="held",
    generation=1,
):
    return sa.text(
        f"INSERT INTO {LEASES} "
        "(id, canonical_target_key, evidence_source, evidence_verified_at, state, "
        " owner_org_id, owner_action_id, owner_generation, manifest_entry_id, revision, created_at) "
        "VALUES (:id, :key, 'verified-aws-connection:conn-1', :ts, :state, "
        " :org, :action, :generation, 'entry-1', 1, :ts)"
    ).bindparams(id=lease_id, key=key, org=org, action=action, state=state, generation=generation, ts=TS)


class TestAlembicOnlyDatabase:
    """The table stands up from the migration alone, with no `create_all`."""

    async def test_upgrade_creates_the_table(self):
        engine = await _bare_engine()
        async with engine.connect() as conn:
            before = set(await conn.run_sync(lambda c: sa_inspect(c).get_table_names()))
        assert LEASES not in before, "fixture must not pre-create the table under test"

        await _upgrade(engine)

        async with engine.connect() as conn:
            after = set(await conn.run_sync(lambda c: sa_inspect(c).get_table_names()))
        await engine.dispose()
        assert LEASES in after, f"upgrade() did not create {LEASES}"

    async def test_upgrade_creates_all_three_indexes(self):
        engine = await _bare_engine()
        await _upgrade(engine)
        async with engine.connect() as conn:
            names = {index["name"] for index in await conn.run_sync(lambda c: sa_inspect(c).get_indexes(LEASES))}
        await engine.dispose()
        assert {TARGET_INDEX, OWNER_INDEX, EXPIRY_INDEX} <= names

    async def test_rows_are_insertable_after_alembic_only_upgrade(self):
        """The table is usable, not merely present.

        Catches a shape the application cannot write to — e.g. a NOT NULL column no
        store method populates, which applies cleanly and then fails on the first
        real write.
        """
        engine = await _bare_engine()
        await _upgrade(engine)

        async with engine.begin() as conn:
            await conn.execute(_insert_lease())

        async with engine.connect() as conn:
            row = (await conn.execute(sa.text(f"SELECT canonical_target_key, state, owner_org_id, owner_generation, revision FROM {LEASES}"))).one()
        await engine.dispose()
        assert row == (KEY, "held", ORG_A, 1, 1)

    async def test_lifecycle_columns_accept_null(self):
        """A lease row exists in states where most timestamps are absent.

        A freed lease has no expiry and no heartbeat; an unreconciled one has no
        terminal evidence. A NOT NULL on any of these would force the store to invent
        a value, and an invented reconciliation timestamp is exactly the false
        evidence the takeover gate must never see.
        """
        engine = await _bare_engine()
        await _upgrade(engine)

        async with engine.begin() as conn:
            await conn.execute(
                sa.text(
                    f"INSERT INTO {LEASES} "
                    "(id, canonical_target_key, evidence_source, evidence_verified_at, state, "
                    " owner_generation, revision, created_at) "
                    "VALUES ('lease-free', :key, 'verified-aws-connection:conn-1', :ts, 'free', 0, 1, :ts)"
                ).bindparams(key=KEY, ts=TS)
            )

        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    sa.text(
                        f"SELECT owner_org_id, owner_action_id, manifest_entry_id, release_ref, acquired_at, "
                        f"heartbeat_at, lease_expires_at, reconciled_terminal_evidence, reconciled_at, "
                        f"release_reason, released_at, evidence_detail, updated_at FROM {LEASES}"
                    )
                )
            ).one()
        await engine.dispose()
        assert row == (None,) * 13

    async def test_evidence_columns_are_required(self):
        """An unevidenced canonicalization is a guess.

        And the guess decides whether two aliases are the same cluster, so the
        database refuses a row that cannot say what proved its identity.
        """
        engine = await _bare_engine()
        await _upgrade(engine)
        async with engine.begin() as conn:
            await conn.execute(sa.text("PRAGMA foreign_keys=ON"))
        with pytest.raises(IntegrityError):
            async with engine.begin() as conn:
                await conn.execute(
                    sa.text(
                        f"INSERT INTO {LEASES} (id, canonical_target_key, state, owner_generation, revision, created_at) "
                        "VALUES ('lease-x', :key, 'held', 1, 1, :ts)"
                    ).bindparams(key=KEY, ts=TS)
                )
        await engine.dispose()


class TestUniqueIndexIsGlobal:
    """THE assertion of this migration.

    Asserted by attempting the duplicate insert rather than only by reading the index
    definition: an index can exist, be named exactly right, and cover the wrong
    columns. Only a refused insert proves the constraint.
    """

    async def test_two_tenants_cannot_both_hold_one_physical_target(self):
        """The defect an org-scoped index would reintroduce.

        Two connections in two tenants naming one cluster produce one canonical key.
        If the index led with `org_id`, both rows would be accepted and two pipelines
        would deploy incompatible releases onto that cluster. Every store-level test
        would still pass, because the store never asks the database to enforce
        tenancy here.
        """
        engine = await _bare_engine()
        await _upgrade(engine)

        async with engine.begin() as conn:
            await conn.execute(_insert_lease(lease_id="lease-a", org=ORG_A, action="action-a"))

        with pytest.raises(IntegrityError):
            async with engine.begin() as conn:
                await conn.execute(_insert_lease(lease_id="lease-b", org=ORG_B, action="action-b"))
        await engine.dispose()

    async def test_the_same_tenant_cannot_hold_one_target_twice(self):
        """The intra-tenant half: two nodes of one plan targeting one namespace."""
        engine = await _bare_engine()
        await _upgrade(engine)

        async with engine.begin() as conn:
            await conn.execute(_insert_lease(lease_id="lease-a", action="action-a"))

        with pytest.raises(IntegrityError):
            async with engine.begin() as conn:
                await conn.execute(_insert_lease(lease_id="lease-b", action="action-b"))
        await engine.dispose()

    async def test_distinct_targets_are_allowed(self):
        """The other half of the invariant: uniqueness must not be too broad.

        Two independent surfaces must each get a row, or unrelated deployments would
        block each other — and a lease that blocks unrelated work gets bypassed.
        """
        engine = await _bare_engine()
        await _upgrade(engine)

        async with engine.begin() as conn:
            await conn.execute(_insert_lease(lease_id="lease-a", key="v1:" + "a" * 64))
            await conn.execute(_insert_lease(lease_id="lease-b", key="v1:" + "b" * 64))

        async with engine.connect() as conn:
            count = (await conn.execute(sa.text(f"SELECT COUNT(*) FROM {LEASES}"))).scalar_one()
        await engine.dispose()
        assert count == 2

    async def test_unique_index_columns_are_exactly_the_canonical_key(self):
        """Named as intent, so a future "fix" fails with an explanation.

        The `assert` message is the point: somebody adding `org_id` to match the
        neighbouring tables should learn *why* it is absent from the failure itself,
        not have to find this docstring.
        """
        engine = await _bare_engine()
        await _upgrade(engine)
        async with engine.connect() as conn:
            indexes = {index["name"]: index for index in await conn.run_sync(lambda c: sa_inspect(c).get_indexes(LEASES))}
        await engine.dispose()

        target = indexes[TARGET_INDEX]
        # `bool(...)`: SQLite's inspector reports uniqueness as 1/0, Postgres as
        # True/False. An identity check would pass on one backend and fail on the other.
        assert bool(target["unique"]) is True
        assert target["column_names"] == ["canonical_target_key"], (
            "The unique index must be on canonical_target_key ALONE. Adding org_id would let "
            "two tenants' aliases for one physical cluster both be held at once, which is the "
            "exact double-deploy this table prevents. Tenant isolation lives in the store's "
            "opaque conflict responses, not in this index."
        )

    async def test_table_is_not_tenant_scoped(self):
        """No `org_id` column and no tenant index — `owner_org_id` is not a scoping key.

        A future `TenantMixin` added for consistency with its neighbours would bring
        an `org_id` column and its index, and would then invite scoping the unique
        index by it.
        """
        engine = await _bare_engine()
        await _upgrade(engine)
        async with engine.connect() as conn:
            columns = {column["name"] for column in await conn.run_sync(lambda c: sa_inspect(c).get_columns(LEASES))}
            indexes = {index["name"] for index in await conn.run_sync(lambda c: sa_inspect(c).get_indexes(LEASES))}
        await engine.dispose()
        assert "org_id" not in columns
        assert "owner_org_id" in columns
        assert not any("org_id" in name and name != OWNER_INDEX for name in indexes)


class TestRoundTrip:
    """The rollback the story prescribes has actually been run."""

    async def test_upgrade_downgrade_upgrade(self):
        """A rollback path that has never been executed is not a rollback path.

        Against a disposable database only; a shared environment is never downgraded
        as a validation step.
        """
        engine = await _bare_engine()
        await _upgrade(engine)
        await _downgrade(engine)

        async with engine.connect() as conn:
            after_down = set(await conn.run_sync(lambda c: sa_inspect(c).get_table_names()))
        assert LEASES not in after_down, "downgrade() did not drop the table"

        await _upgrade(engine)
        async with engine.connect() as conn:
            after_up = set(await conn.run_sync(lambda c: sa_inspect(c).get_table_names()))
            indexes = {index["name"] for index in await conn.run_sync(lambda c: sa_inspect(c).get_indexes(LEASES))}
        await engine.dispose()

        assert LEASES in after_up
        # Re-upgrading must restore the constraint too. A downgrade that dropped the
        # table and a re-upgrade that forgot the index would leave a schema which
        # accepts the double-hold silently.
        assert TARGET_INDEX in indexes

    async def test_downgrade_leaves_no_orphan_indexes(self):
        engine = await _bare_engine()
        await _upgrade(engine)
        await _downgrade(engine)
        async with engine.connect() as conn:
            names = set(await conn.run_sync(lambda c: sa_inspect(c).get_table_names()))
        await engine.dispose()
        assert names == set()


class TestNoBackfill:
    """The migration creates one table and touches nothing else."""

    def test_upgrade_creates_only_the_lease_table(self):
        """No UPDATE, no INSERT, no ALTER of an existing table.

        A lease row asserts that a physical target is currently held. Synthesizing
        one for work already in flight would either claim a hold nobody took or
        declare free a target something is actively deploying to — and it would be
        *trusted*.
        """
        source = (MIGRATIONS_DIR / "055_orchestration_environment_leases.py").read_text()
        tree = ast.parse(source)
        upgrade = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "upgrade")
        # Only `op.*` calls are schema operations. `sa.Column(...)` and friends are
        # column *descriptions* passed to create_table, not operations of their own.
        called = {
            node.func.attr
            for node in ast.walk(upgrade)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "op"
        }
        assert called, "no op.* calls found — the AST walk is not looking at the migration"
        allowed = {"create_table", "create_index"}
        assert called <= allowed, f"upgrade() must only create; it also calls {sorted(called - allowed)}"

    def test_migration_declares_no_dependencies(self):
        assert MIG_054.depends_on is None
        assert MIG_054.branch_labels is None


class TestPostgresRendering:
    """The tests run on SQLite, but dev runs on Postgres. Render for Postgres.

    Nothing else in this file would notice a Postgres-only DDL problem, because
    SQLite is more permissive. Rendering the migration in alembic's offline (`--sql`)
    mode against the Postgres dialect exercises the compiler that actually matters,
    with no live database.
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
            MIG_054.upgrade()
        return "".join(chunks)

    def test_timestamps_are_timezone_aware_on_postgres(self):
        """A naive timestamp makes the expiry comparison ambiguous across zones.

        Concretely: the store compares `lease_expires_at` against an aware `now()`, so
        a naive column raises `TypeError` at the moment a takeover decision is made —
        and an exception there is a stuck target rather than a wrong answer, but only
        because the store fails closed.
        """
        ddl = self._render_postgres_ddl()
        assert "TIMESTAMP WITH TIME ZONE" in ddl
        assert ddl.count("TIMESTAMP WITHOUT TIME ZONE") == 0

    def test_unique_index_renders_on_the_key_alone(self):
        """Read the generated SQL, since that is what runs against dev."""
        ddl = self._render_postgres_ddl()
        assert f"CREATE UNIQUE INDEX {TARGET_INDEX} ON {LEASES} (canonical_target_key)" in ddl, ddl

    def test_one_table_and_one_unique_index_render(self):
        ddl = self._render_postgres_ddl()
        assert ddl.count("CREATE TABLE") == 1
        assert ddl.count("CREATE UNIQUE INDEX") == 1
        assert ddl.count("CREATE INDEX") == 2

    def test_no_foreign_keys_render(self):
        """The lease references no parent, by design.

        An FK to a connection or an action would make the lease's lifetime depend on
        another row's, and a cascade delete would silently free a held target.
        """
        ddl = self._render_postgres_ddl()
        assert "FOREIGN KEY" not in ddl


class TestRevisionChain:
    def test_revision_id_and_down_revision(self):
        """Chains onto the head that was real when this landed.

        Authored as `053` onto `052_orchestration_executions`; renumbered to `054`
        onto `053_flow_slug_unique` when that revision reached `main` first and took
        the same parent; renumbered again to `055` onto `054_execution_tenant_guards`
        (#5142) when *that* revision reached `main` and took the same parent in turn.
        Pinning the parent here is deliberate even though
        `test_migration_leaves_exactly_one_head` already asserts the chain is linear:
        the head-count check passes for *any* linear arrangement, including one where
        a later rebase silently re-points this revision at a different parent.

        Updating this pin is therefore a reviewed act, not a formality — it is the
        assertion that failed and made the second renumber explicit rather than
        silent. Change it only together with `down_revision`, having confirmed the
        new parent's migration is disjoint from this one.
        """
        assert MIG_054.revision == "055_orch_environment_leases"
        assert MIG_054.down_revision == "054_execution_tenant_guards"

    def test_revision_id_fits_the_alembic_version_column(self):
        """`alembic_version.version_num` is VARCHAR(32); a longer id fails at apply."""
        assert len(MIG_054.revision) <= 32

    def test_migration_leaves_exactly_one_head(self):
        """Two heads is a broken deploy, invisible until a pod runs `alembic upgrade head`.

        Asserts the *count*, not the head's name: the head advances with every
        migration that lands, and a name-pinned assertion turns every future migration
        into a spurious failure here. What must never change is that there is exactly
        one head and that this revision is still on the chain.

        This is the guard that caught the `053` collision with #5342 before merge, so
        it is load-bearing rather than decorative: without it the branched history
        would have surfaced as a failed `alembic upgrade head` in a deployed
        database, taking down an unrelated story's deploy too.
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
        assert "055_orch_environment_leases" in revisions, "this revision must still be on the chain"


class TestModelMigrationParity:
    """The migration and the model are hand-written separately, so they can drift.

    The migration is what runs against dev; the model is what every test uses. When
    they disagree, the tests pass and the deployed database raises `UndefinedColumn`
    on the first real query — so compare them directly.
    """

    async def _migrated_columns(self):
        engine = await _bare_engine()
        await _upgrade(engine)
        async with engine.connect() as conn:
            columns = await conn.run_sync(lambda c: sa_inspect(c).get_columns(LEASES))
        await engine.dispose()
        return {column["name"]: column for column in columns}

    async def test_column_names_match_the_model(self):
        migrated = set(await self._migrated_columns())
        declared = {column.name for column in OrchestrationEnvironmentLease.__table__.columns}
        assert migrated == declared, f"drift — migration-only: {sorted(migrated - declared)}, model-only: {sorted(declared - migrated)}"

    async def test_column_types_match_the_model(self):
        from sqlalchemy.dialects import sqlite

        dialect = sqlite.dialect()
        migrated_columns = await self._migrated_columns()
        declared = {column.name: str(column.type.compile(dialect=dialect)) for column in OrchestrationEnvironmentLease.__table__.columns}
        migrated = {name: str(column["type"].compile(dialect=dialect)) for name, column in migrated_columns.items()}
        assert migrated == declared, "column types drifted between migration and model"

    async def test_nullability_matches_the_model(self):
        """Nullability drift is the dangerous half of parity.

        A column the model believes optional but the database requires fails only on
        the write path that omits it — which here is `release_lease`, a path first
        exercised when a deployment finishes.
        """
        migrated_columns = await self._migrated_columns()
        declared = {column.name: column.nullable for column in OrchestrationEnvironmentLease.__table__.columns}
        migrated = {name: column["nullable"] for name, column in migrated_columns.items()}
        assert migrated == declared, "nullability drifted between migration and model"

    async def test_index_shape_matches_the_model(self):
        """Including uniqueness, which is where a drift would be invisible and fatal.

        A model declaring a unique index that the migration created as non-unique
        passes every ORM-built test — `create_all` uses the model — and admits the
        double-hold on every deployed database.
        """
        engine = await _bare_engine()
        await _upgrade(engine)
        async with engine.connect() as conn:
            migrated = {
                index["name"]: (index["unique"], list(index["column_names"]))
                for index in await conn.run_sync(lambda c: sa_inspect(c).get_indexes(LEASES))
            }
        await engine.dispose()

        declared = {
            index.name: (bool(index.unique), [column.name for column in index.columns]) for index in OrchestrationEnvironmentLease.__table__.indexes
        }
        assert migrated == declared, f"index drift — migration: {migrated}, model: {declared}"
