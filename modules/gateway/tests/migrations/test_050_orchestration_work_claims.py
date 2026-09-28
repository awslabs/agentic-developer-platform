"""Tests for Alembic migration 050 — the shared issue-ownership claims table.

Issue #5127 (EPIC #4191). This file is **mandatory**, and not only for coverage:
`modules/gateway/alembic/**` is absent from `gateway-ci.yml`'s trigger paths
(`src/**`, `tests/**`, `cli/**`, `pyproject.toml`, `Dockerfile`, frontend,
`libs/`, `contracts/`), so a migration-only change gets **zero CI signal**. A
test under `tests/` is what makes CI run at all for this migration.

These tests exercise the REAL migration functions imported from the version
module. A test that re-implements the migration proves only that the author can
write the same bug twice.

What is under test:

  - `upgrade()` creates the table and its indexes on an **alembic-only**
    database — no `Base.metadata.create_all` anywhere. This is the case that
    catches a migration which "works" only because `create_all` already built
    the table, and it is the failure mode where a table is declared in models,
    has no DDL, and is absent on every deployed database.
  - `downgrade()` reverses it completely. Unlike 049 this downgrade is
    deliberately unguarded, because it IS the documented rollback path for the
    story — so it has to actually work.
  - **No backfill**: the migration creates one table and touches nothing else.
  - The revision chains onto the real single head. A broken `down_revision`
    silently SKIPS the migration, and live code then queries an absent table.
  - **The unique index covers the binding and nothing else.** This is the
    invariant the whole story rests on: without it two concurrent transactions
    can both pass an application-level "is this issue free?" check and both
    insert, which is the double-execution the issue exists to prevent.
  - Postgres rendering, because the tests run on SQLite but dev runs on
    Postgres. `provider_repository_id` in particular must render as `BIGINT` —
    an `INTEGER` there would overflow on a provider id past 2^31 and the
    migration would still apply cleanly.
  - Migration/model schema parity: both are hand-written, so drift is the live
    risk — the migration is what runs in dev, the models are what the tests use.
"""

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from src.orchestration.models import OrchestrationWorkClaim

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "alembic" / "versions"

TABLE = "orchestration_work_claims"
BINDING_INDEX = "uq_orchestration_work_claims_binding"


def _load_migration(filename: str):
    """Import a migration module by path (they are not an importable package)."""
    path = MIGRATIONS_DIR / filename
    spec = importlib.util.spec_from_file_location(filename.replace(".py", ""), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MIG_050 = _load_migration("050_orchestration_work_claims.py")


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
    """An engine with NO schema at all — no create_all, no models.

    This is the important fixture. `create_all` would build the table from the
    ORM metadata and mask a migration that never creates it itself.
    """
    return create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )


async def _upgrade(engine):
    async with engine.begin() as conn:
        await conn.run_sync(_run_migration, MIG_050.upgrade)


async def _downgrade(engine):
    async with engine.begin() as conn:
        await conn.run_sync(_run_migration, MIG_050.downgrade)


def _insert(claim_id="c1", org="org-1", repo=987_654_321, issue=5127, ref="flow-a"):
    return sa.text(
        f"INSERT INTO {TABLE} "
        "(id, org_id, provider_repository_id, issue_number, owner_kind, owner_ref, state, generation, claimed_at, created_at) "
        "VALUES (:id, :org, :repo, :issue, 'engine_flow', :ref, 'held', 1, '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
    ).bindparams(id=claim_id, org=org, repo=repo, issue=issue, ref=ref)


class TestAlembicOnlyDatabase:
    """The migration stands on its own, with no `create_all` involved."""

    async def test_upgrade_creates_the_table_without_create_all(self):
        """The table exists after upgrade() on a database that started empty."""
        engine = await _bare_engine()
        async with engine.connect() as conn:
            before = set(await conn.run_sync(lambda c: sa_inspect(c).get_table_names()))
        assert before == set(), "fixture must start with an empty database for this to prove anything"

        await _upgrade(engine)

        async with engine.connect() as conn:
            after = set(await conn.run_sync(lambda c: sa_inspect(c).get_table_names()))
        await engine.dispose()

        assert TABLE in after, f"upgrade() did not create {TABLE}"

    async def test_rows_are_insertable_after_alembic_only_upgrade(self):
        """The table is actually usable, not just present.

        Catches a table created with a shape the application cannot write to —
        e.g. a NOT NULL column the admission service never populates.
        """
        engine = await _bare_engine()
        await _upgrade(engine)

        async with engine.begin() as conn:
            await conn.execute(_insert())

        async with engine.connect() as conn:
            row = (await conn.execute(sa.text(f"SELECT state, generation, provider_repository_id FROM {TABLE}"))).one()
        await engine.dispose()

        assert row == ("held", 1, 987_654_321)


class TestBindingUniqueness:
    """The one invariant the story cannot survive without."""

    @pytest.fixture
    async def engine(self):
        engine = await _bare_engine()
        await _upgrade(engine)
        yield engine
        await engine.dispose()

    async def test_binding_index_exists_and_is_unique(self, engine):
        """`(org_id, provider_repository_id, issue_number)`, unique.

        Asserted on the migration's own DDL rather than the model's, because the
        migration is what runs against a deployed database. If this index is
        missing there, the admission service is advisory: two transactions that
        both read "no owner" both insert, and the issue runs twice.
        """
        async with engine.connect() as conn:
            indexes = await conn.run_sync(lambda c: sa_inspect(c).get_indexes(TABLE))

        unique = {i["name"]: tuple(i["column_names"]) for i in indexes if i["unique"]}
        assert BINDING_INDEX in unique, f"binding uniqueness index missing; got {sorted(unique)}"
        assert unique[BINDING_INDEX] == ("org_id", "provider_repository_id", "issue_number")

    async def test_duplicate_binding_is_rejected_by_the_database(self, engine):
        """Two owners for one issue is refused below the application layer."""
        async with engine.begin() as conn:
            await conn.execute(_insert(claim_id="c1"))

        with pytest.raises(sa.exc.IntegrityError):
            async with engine.begin() as conn:
                await conn.execute(_insert(claim_id="c2", ref="flow-b"))

    async def test_the_unique_index_does_not_include_state(self, engine):
        """A released row must still occupy its binding.

        If `state` were part of the unique key, a released claim would stop
        blocking a new row and the next admission would insert a *competing*
        claim with `generation` reset to 1 — which makes a stale worker's
        generation look current again. Ordered reuse depends on the released row
        remaining the one row for that binding.
        """
        async with engine.connect() as conn:
            indexes = await conn.run_sync(lambda c: sa_inspect(c).get_indexes(TABLE))
        binding = next(i for i in indexes if i["name"] == BINDING_INDEX)
        assert "state" not in binding["column_names"]

        async with engine.begin() as conn:
            await conn.execute(_insert(claim_id="c1"))
            await conn.execute(sa.text(f"UPDATE {TABLE} SET state = 'released' WHERE id = 'c1'"))

        with pytest.raises(sa.exc.IntegrityError):
            async with engine.begin() as conn:
                await conn.execute(_insert(claim_id="c2", ref="flow-b"))

    async def test_separate_issues_and_tenants_are_independent(self, engine):
        """A0-2: distinct bindings coexist; ownership is scoped, not global."""
        async with engine.begin() as conn:
            await conn.execute(_insert(claim_id="c1", issue=5127))
            await conn.execute(_insert(claim_id="c2", issue=5128))
            await conn.execute(_insert(claim_id="c3", org="org-2", issue=5127))
            await conn.execute(_insert(claim_id="c4", repo=111, issue=5127))

        async with engine.connect() as conn:
            count = (await conn.execute(sa.text(f"SELECT count(*) FROM {TABLE}"))).scalar_one()
        assert count == 4


class TestSchema:
    @pytest.fixture
    async def engine(self):
        engine = await _bare_engine()
        await _upgrade(engine)
        yield engine
        await engine.dispose()

    @pytest.fixture
    async def columns(self, engine):
        async with engine.connect() as conn:
            return await conn.run_sync(lambda c: {x["name"]: x for x in sa_inspect(c).get_columns(TABLE)})

    async def test_table_carries_org_id(self, columns):
        """Tenant isolation is a column, not a convention."""
        assert "org_id" in columns
        assert columns["org_id"]["nullable"] is False

    async def test_binding_columns_are_required(self, columns):
        """A claim with a NULL repository or issue owns nothing identifiable."""
        for name in ("provider_repository_id", "issue_number", "owner_kind", "owner_ref", "state", "generation"):
            assert columns[name]["nullable"] is False, f"{name} must be NOT NULL"

    async def test_generation_has_no_server_default(self, columns):
        """The admission service sets the generation; the database must not.

        A `server_default` would let a row that skipped the compare-and-set path
        land looking like a legitimate generation 1, which is precisely the
        ambiguity the generation exists to remove.
        """
        assert columns["generation"].get("default") is None

    async def test_lifecycle_columns_are_nullable(self, columns):
        """Optional means nullable, not defaulted to a lie.

        `active_run_id` is NULL between admission and run binding, and
        `released_at`/`release_reason` are NULL while the claim is held. A
        non-null default on any of them would assert a fact nobody established —
        a `lease_expires_at` defaulted to now() would mark every claim lapsed.
        """
        for name in ("active_run_id", "claim_event_id", "heartbeat_at", "lease_expires_at", "release_reason", "released_at", "updated_at"):
            assert columns[name]["nullable"] is True, f"{name} must be nullable"
            assert columns[name].get("default") is None, f"{name} must have no server_default"

    async def test_operator_and_duplicate_event_indexes_exist(self, engine):
        """Admission reads by binding; replay lookup reads by event id.

        The duplicate-event index is tenant-scoped on purpose: an event id is
        only unique within the tenant that produced it.
        """
        async with engine.connect() as conn:
            indexes = await conn.run_sync(lambda c: {i["name"]: tuple(i["column_names"]) for i in sa_inspect(c).get_indexes(TABLE)})

        assert indexes.get("ix_orchestration_work_claims_org_id_state") == ("org_id", "state")
        assert indexes.get("ix_orchestration_work_claims_claim_event_id") == ("org_id", "claim_event_id")
        assert "ix_orchestration_work_claims_org_id" in indexes

    async def test_no_foreign_keys(self, engine):
        """Deliberate: a claim outlives the runs and flows that pass through it.

        An FK to a run or flow would cascade-delete ownership history, and the
        release reason on a completed claim is what explains why the issue is
        free. Nothing referencing this table by FK is also what makes the
        unguarded downgrade safe.
        """
        async with engine.connect() as conn:
            fks = await conn.run_sync(lambda c: sa_inspect(c).get_foreign_keys(TABLE))
        assert fks == []


class TestDowngrade:
    async def test_downgrade_removes_the_table(self):
        """The documented rollback plan; prove it works.

        This downgrade is intentionally unguarded — the story's rollback plan is
        "disable new admissions, reconcile in-flight work, then downgrade", so a
        raise here would block the very path the plan prescribes.
        """
        engine = await _bare_engine()
        await _upgrade(engine)
        await _downgrade(engine)

        async with engine.connect() as conn:
            remaining = set(await conn.run_sync(lambda c: sa_inspect(c).get_table_names()))
        await engine.dispose()

        assert TABLE not in remaining, "downgrade() left the table behind"

    async def test_downgrade_drops_the_table_even_with_rows_present(self):
        """Unguarded means unguarded: held claims do not block rollback.

        Asserted explicitly because the neighbouring migration (049) raises on
        non-empty tables, and someone copying that pattern here would break the
        rollback. If this behavior is ever meant to change, this test is the
        review signal.
        """
        engine = await _bare_engine()
        await _upgrade(engine)
        async with engine.begin() as conn:
            await conn.execute(_insert())

        await _downgrade(engine)

        async with engine.connect() as conn:
            remaining = set(await conn.run_sync(lambda c: sa_inspect(c).get_table_names()))
        await engine.dispose()
        assert TABLE not in remaining

    async def test_upgrade_downgrade_upgrade_is_clean(self):
        """Rollback then re-deploy must work — that is the whole point of rollback.

        A downgrade that leaves an index behind fails the next upgrade on
        "index already exists", which is a broken deploy rather than a
        degraded one.
        """
        engine = await _bare_engine()
        await _upgrade(engine)
        await _downgrade(engine)
        await _upgrade(engine)

        async with engine.connect() as conn:
            tables = set(await conn.run_sync(lambda c: sa_inspect(c).get_table_names()))
            indexes = await conn.run_sync(lambda c: {i["name"] for i in sa_inspect(c).get_indexes(TABLE)})
        await engine.dispose()

        assert TABLE in tables
        assert BINDING_INDEX in indexes


class TestNoBackfill:
    """The migration creates one table and touches nothing else."""

    async def test_preexisting_rows_are_untouched(self):
        """A populated pre-existing table is bit-identical after upgrade()."""
        engine = await _bare_engine()

        async with engine.begin() as conn:
            await conn.execute(sa.text("CREATE TABLE preexisting (id VARCHAR(36) PRIMARY KEY, payload VARCHAR(64))"))
            await conn.execute(sa.text("INSERT INTO preexisting (id, payload) VALUES ('a', 'original-a'), ('b', 'original-b')"))
            before = (await conn.execute(sa.text("SELECT id, payload FROM preexisting ORDER BY id"))).all()

        await _upgrade(engine)

        async with engine.connect() as conn:
            after = (await conn.execute(sa.text("SELECT id, payload FROM preexisting ORDER BY id"))).all()
            cols = await conn.run_sync(lambda c: {x["name"] for x in sa_inspect(c).get_columns("preexisting")})
        await engine.dispose()

        assert after == before == [("a", "original-a"), ("b", "original-b")]
        assert cols == {"id", "payload"}, "upgrade() must not add columns to existing tables"

    async def test_upgrade_creates_no_claim_rows(self):
        """No invented owners.

        Ownership is forward-looking: a claim asserts that a specific launch path
        was admitted, so fabricating rows for work already in flight would assert
        an owner nobody verified — and could block the run that is genuinely
        executing. Work in flight at deploy time is reconciled at its next
        admission, not here.
        """
        engine = await _bare_engine()
        await _upgrade(engine)

        async with engine.connect() as conn:
            count = (await conn.execute(sa.text(f"SELECT count(*) FROM {TABLE}"))).scalar_one()
        await engine.dispose()
        assert count == 0

    def test_migration_source_contains_no_alter_or_update(self):
        """Structural guard: the migration creates a table and nothing else.

        Reading the source is the only way to assert the *absence* of a
        destructive operation against tables this test does not know about. A
        future edit that alters a populated table has to change this test, which
        is the review signal.
        """
        source = (MIGRATIONS_DIR / "050_orchestration_work_claims.py").read_text()
        # Strip the docstring, which legitimately discusses backfill and ALTER.
        body = source.split('"""', 2)[-1]
        lowered = body.lower()

        for forbidden in ("alter table", "op.alter_column", "op.add_column", "op.drop_column", "update ", "op.bulk_insert", "op.execute"):
            assert forbidden not in lowered, f"migration must only create the new table; found {forbidden!r}"


class TestPostgresRendering:
    """The tests run on SQLite, but dev runs on Postgres. Render for Postgres.

    Nothing else in this file would notice a Postgres-only DDL problem, because
    SQLite is more permissive — it does not even enforce integer width. Rendering
    the migration in alembic's offline (`--sql`) mode against the Postgres
    dialect exercises the compiler that actually matters, with no live database.
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
            MIG_050.upgrade()
        return "".join(chunks)

    def test_provider_repository_id_renders_as_bigint(self):
        """`BIGINT`, not `INTEGER`.

        GitHub repository ids are 64-bit provider integers. An `INTEGER` column
        renders and applies without complaint, then rejects or truncates a value
        past 2^31 — and SQLite would never reveal it, because SQLite ignores the
        declared integer width entirely.
        """
        ddl = self._render_postgres_ddl()
        assert "provider_repository_id BIGINT" in ddl, f"must render as BIGINT on Postgres; got:\n{ddl[:2000]}"

    def test_timestamps_are_timezone_aware_on_postgres(self):
        """A naive timestamp makes lease and release ordering ambiguous across zones.

        It also has a concrete failure mode in this module: the admission service
        compares `lease_expires_at` against an aware `now()`, and a naive column
        turns that comparison into a TypeError at admission — which fails closed
        and stops dispatch for the whole tenant.
        """
        ddl = self._render_postgres_ddl()
        assert "TIMESTAMP WITH TIME ZONE" in ddl
        assert ddl.count("TIMESTAMP WITHOUT TIME ZONE") == 0

    def test_one_table_and_one_unique_index_render(self):
        ddl = self._render_postgres_ddl()
        assert ddl.count("CREATE TABLE") == 1
        assert ddl.count("CREATE UNIQUE INDEX") == 1
        assert BINDING_INDEX in ddl


class TestRevisionChain:
    def test_revision_id_and_down_revision(self):
        """Chains onto the head that was real when this landed."""
        assert MIG_050.revision == "050_orchestration_work_claims"
        assert MIG_050.down_revision == "049_bedrock_connection_grants"

    def test_revision_id_fits_the_alembic_version_column(self):
        """`alembic_version.version_num` is VARCHAR(32); a longer id fails at apply."""
        assert len(MIG_050.revision) <= 32

    def test_migration_leaves_exactly_one_head(self):
        """Two heads is a broken deploy, and it is invisible until a pod runs
        `alembic upgrade head`.

        Asserts the *count*, not the head's name: the head advances with every
        migration that lands, and a name-pinned assertion turns every future
        migration into a spurious failure here — which trains people to edit
        this test rather than read it. What must never change is that there is
        exactly one head, and that 050 is still on the chain.
        """
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
        assert "050_orchestration_work_claims" in revisions, "050 must still be on the chain"


class TestModelMigrationParity:
    """The migration and the model are hand-written separately, so they can drift.

    The migration is what runs against dev; the model is what every test uses.
    When they disagree, tests pass and production breaks — so compare them.
    """

    @pytest.fixture
    async def migrated_columns(self):
        engine = await _bare_engine()
        await _upgrade(engine)
        async with engine.connect() as conn:
            cols = await conn.run_sync(lambda c: {x["name"]: x for x in sa_inspect(c).get_columns(TABLE)})
        await engine.dispose()
        return cols

    async def test_column_names_match(self, migrated_columns):
        model_cols = {c.name for c in OrchestrationWorkClaim.__table__.columns}
        assert model_cols == set(migrated_columns), (
            "model/migration column drift — "
            f"only in model: {model_cols - set(migrated_columns)}, "
            f"only in migration: {set(migrated_columns) - model_cols}"
        )

    async def test_nullability_matches(self, migrated_columns):
        """A column the model calls optional and the migration calls NOT NULL
        fails only on the deployed database, where nothing tests it."""
        model = {c.name: c.nullable for c in OrchestrationWorkClaim.__table__.columns}
        assert model == {name: col["nullable"] for name, col in migrated_columns.items()}

    async def test_column_types_match(self, migrated_columns):
        """Rendered types, not Python types: `Integer` vs `BigInteger` is the
        drift that matters here and both are `int` in the model annotation."""
        from sqlalchemy.dialects import sqlite

        dialect = sqlite.dialect()
        model = {c.name: str(c.type.compile(dialect=dialect)) for c in OrchestrationWorkClaim.__table__.columns}
        migrated = {name: str(col["type"].compile(dialect=dialect)) for name, col in migrated_columns.items()}
        assert model == migrated
