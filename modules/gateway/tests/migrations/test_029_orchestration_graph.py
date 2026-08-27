"""Tests for Alembic migration 029 — orchestration graph store tables.

Issue #4196 (EPIC #4191). This file is **mandatory**, and not only for coverage:
`modules/gateway/alembic/**` is absent from `gateway-ci.yml`'s trigger paths
(`src/**`, `tests/**`, `cli/**`, `pyproject.toml`, `Dockerfile`, frontend,
`libs/`, `contracts/`), so a migration-only PR gets **zero CI signal**. A test
under `tests/` is what makes CI run at all for this change.

These tests exercise the REAL migration functions imported from the version
module, against SQLite. A test that re-implements the migration proves only that
the author can write the same bug twice.

What is under test:

  - `upgrade()` creates all five tables, their indexes, and the FK graph
  - `downgrade()` reverses it completely (this IS the documented rollback plan)
  - **AC-24**: the migration applies on an **alembic-only** database — no
    `Base.metadata.create_all` anywhere — proving it does not depend on the local
    dev auto-create flag. This is the case that catches a migration which "works"
    only because `create_all` already built the table.
  - **No backfill**: pre-existing rows in already-populated tables are untouched.
    This is the 025-shaped mistake the issue explicitly warns against — a
    `NOT NULL` + `server_default` column where the default IS the backfill.
  - The revision chains onto the real single head. A broken `down_revision`
    silently SKIPS the migration and live code then queries absent tables.
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

from src.orchestration.models import (
    OrchestrationAcceptedPlan,
    OrchestrationDecision,
    OrchestrationEdge,
    OrchestrationFlow,
    OrchestrationNode,
)

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "alembic" / "versions"

ORCHESTRATION_TABLES = (
    "orchestration_flows",
    "orchestration_nodes",
    "orchestration_edges",
    "orchestration_accepted_plans",
    "orchestration_decisions",
)


def _load_migration(filename: str):
    """Import a migration module by path (they are not an importable package)."""
    path = MIGRATIONS_DIR / filename
    spec = importlib.util.spec_from_file_location(filename.replace(".py", ""), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MIG_029 = _load_migration("029_orchestration_graph.py")


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
    """An engine with NO schema at all — no create_all, no models (AC-24).

    This is the important fixture. `create_all` would build the tables from the
    ORM metadata and mask a migration that never creates them itself.
    """
    return create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )


async def _upgrade(engine):
    async with engine.begin() as conn:
        await conn.run_sync(_run_migration, MIG_029.upgrade)


async def _downgrade(engine):
    async with engine.begin() as conn:
        await conn.run_sync(_run_migration, MIG_029.downgrade)


class TestAlembicOnlyDatabase:
    """AC-24: the migration stands on its own, with no `create_all` involved."""

    @pytest.mark.asyncio
    async def test_upgrade_creates_all_tables_without_create_all(self):
        """The five tables exist after upgrade() on a database that started empty.

        If this passes only when `Base.metadata.create_all` ran first, the
        migration is a no-op in dev and the tables never appear on a deployed
        database — which is exactly the `audit_logs` failure mode the issue
        cites (declared in models, no DDL, absent everywhere real).
        """
        engine = await _bare_engine()
        async with engine.connect() as conn:
            before = set(await conn.run_sync(lambda c: sa_inspect(c).get_table_names()))
        assert before == set(), "fixture must start with an empty database for this to prove anything"

        await _upgrade(engine)

        async with engine.connect() as conn:
            after = set(await conn.run_sync(lambda c: sa_inspect(c).get_table_names()))
        await engine.dispose()

        missing = set(ORCHESTRATION_TABLES) - after
        assert not missing, f"upgrade() did not create: {missing}"

    @pytest.mark.asyncio
    async def test_rows_are_insertable_after_alembic_only_upgrade(self):
        """The migration's tables are actually usable, not just present.

        Catches a table created with a shape the application cannot write to —
        e.g. a NOT NULL column the ORM never populates.
        """
        engine = await _bare_engine()
        await _upgrade(engine)

        async with engine.begin() as conn:
            await conn.execute(
                sa.text(
                    "INSERT INTO orchestration_flows (id, org_id, slug, title, state, created_at) "
                    "VALUES ('f1', 'org-1', 'flow-a', 'Flow A', 'pending', '2026-01-01 00:00:00')"
                )
            )
            await conn.execute(
                sa.text(
                    "INSERT INTO orchestration_nodes (id, org_id, flow_id, epic_ref, wave_ref, node_ref, kind, state, title, attempts, created_at) "
                    "VALUES ('n1', 'org-1', 'f1', 'epic-1', 'wave-1', 'story-1', 'story', 'pending', 'Story 1', 0, '2026-01-01 00:00:00')"
                )
            )
            await conn.execute(
                sa.text(
                    "INSERT INTO orchestration_decisions (id, org_id, flow_id, node_id, kind, actor_id, actor_role, actor_kind, created_at) "
                    "VALUES ('d1', 'org-1', 'f1', 'n1', 'gate_approved', 'user-9', 'operator', 'human', '2026-01-01 00:00:00')"
                )
            )

        async with engine.connect() as conn:
            kind = (await conn.execute(sa.text("SELECT actor_kind FROM orchestration_decisions WHERE id = 'd1'"))).scalar_one()
        await engine.dispose()

        assert kind == "human"


class TestSchema:
    @pytest.fixture
    async def engine(self):
        engine = await _bare_engine()
        await _upgrade(engine)
        yield engine
        await engine.dispose()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("table", ORCHESTRATION_TABLES)
    async def test_every_table_carries_org_id(self, engine, table):
        """Tenant isolation is a column on every table, not a convention."""
        async with engine.connect() as conn:
            cols = await conn.run_sync(lambda c: {x["name"] for x in sa_inspect(c).get_columns(table)})
        assert "org_id" in cols

    @pytest.mark.asyncio
    async def test_decisions_attribution_columns_are_non_nullable_with_no_default(self, engine):
        """`actor_role` and `actor_kind` are separate, required, and undefaulted.

        A `server_default` on `actor_kind` would let an unattributed write land
        looking attributed — the precise ambiguity that makes the existing
        `tenant_access_requests.decided_by` column unusable as an audit source,
        since it mixes real Cognito subs with synthetic `system:*` values.
        """
        async with engine.connect() as conn:
            cols = await conn.run_sync(lambda c: {x["name"]: x for x in sa_inspect(c).get_columns("orchestration_decisions")})

        for name in ("actor_id", "actor_role", "actor_kind"):
            assert name in cols, f"{name} missing — attribution is incomplete"
            assert cols[name]["nullable"] is False, f"{name} must be NOT NULL"
            assert cols[name].get("default") is None, f"{name} must have no server_default"

    @pytest.mark.asyncio
    async def test_accepted_plan_version_is_unique_per_flow(self, engine):
        """Two rows claiming one version makes "the accepted plan" ambiguous."""
        async with engine.connect() as conn:
            indexes = await conn.run_sync(lambda c: sa_inspect(c).get_indexes("orchestration_accepted_plans"))
        unique = {tuple(i["column_names"]) for i in indexes if i["unique"]}
        assert ("flow_id", "version") in unique

    @pytest.mark.asyncio
    async def test_node_graph_address_is_unique_per_flow(self, engine):
        """The graph address is an address; a duplicate double-counts in rollup."""
        async with engine.connect() as conn:
            indexes = await conn.run_sync(lambda c: sa_inspect(c).get_indexes("orchestration_nodes"))
        unique = {tuple(i["column_names"]) for i in indexes if i["unique"]}
        assert ("flow_id", "epic_ref", "wave_ref", "node_ref") in unique

    @pytest.mark.asyncio
    async def test_edge_pair_is_unique(self, engine):
        """A duplicate edge is not information; it makes fan-in/fan-out wrong."""
        async with engine.connect() as conn:
            indexes = await conn.run_sync(lambda c: sa_inspect(c).get_indexes("orchestration_edges"))
        unique = {tuple(i["column_names"]) for i in indexes if i["unique"]}
        assert ("flow_id", "from_node_id", "to_node_id") in unique

    @pytest.mark.asyncio
    async def test_optional_columns_are_nullable(self, engine):
        """018's contract: optional means nullable, not defaulted to a lie.

        `superseded_at IS NULL` is how "the plan currently in force" is
        identified, so a non-null default would mark every plan superseded.
        """
        async with engine.connect() as conn:
            plans = await conn.run_sync(lambda c: {x["name"]: x for x in sa_inspect(c).get_columns("orchestration_accepted_plans")})
            nodes = await conn.run_sync(lambda c: {x["name"]: x for x in sa_inspect(c).get_columns("orchestration_nodes")})

        assert plans["superseded_at"]["nullable"] is True
        assert plans["superseded_at"].get("default") is None
        assert plans["accepted_by_decision_id"]["nullable"] is True
        assert nodes["issue_ref"]["nullable"] is True
        assert nodes["updated_at"]["nullable"] is True

    @pytest.mark.asyncio
    async def test_foreign_keys_cascade_from_flow(self, engine):
        """Child rows are reachable from the flow and go with it.

        Without ON DELETE CASCADE, deleting a flow either fails or orphans nodes
        that the graph view then renders with no parent.
        """
        async with engine.connect() as conn:
            node_fks = await conn.run_sync(lambda c: sa_inspect(c).get_foreign_keys("orchestration_nodes"))
        flow_fk = [fk for fk in node_fks if fk["referred_table"] == "orchestration_flows"]
        assert flow_fk, "orchestration_nodes must reference orchestration_flows"
        assert flow_fk[0]["options"].get("ondelete") == "CASCADE"


class TestDowngrade:
    @pytest.mark.asyncio
    async def test_downgrade_removes_every_table(self):
        """The documented rollback plan is `alembic downgrade -1`; prove it works.

        A downgrade that half-drops leaves the database in a state neither
        revision describes, and the next upgrade fails on "table already exists".
        """
        engine = await _bare_engine()
        await _upgrade(engine)
        await _downgrade(engine)

        async with engine.connect() as conn:
            remaining = set(await conn.run_sync(lambda c: sa_inspect(c).get_table_names()))
        await engine.dispose()

        leftover = set(ORCHESTRATION_TABLES) & remaining
        assert not leftover, f"downgrade() left tables behind: {leftover}"

    @pytest.mark.asyncio
    async def test_upgrade_downgrade_upgrade_is_clean(self):
        """Rollback then re-deploy must work — that is the whole point of rollback."""
        engine = await _bare_engine()
        await _upgrade(engine)
        await _downgrade(engine)
        await _upgrade(engine)

        async with engine.connect() as conn:
            tables = set(await conn.run_sync(lambda c: sa_inspect(c).get_table_names()))
        await engine.dispose()

        assert set(ORCHESTRATION_TABLES) <= tables


class TestNoBackfill:
    """The 025-shaped mistake the issue explicitly warns against.

    025 adds `NOT NULL` + `server_default` where the default IS the backfill.
    This migration must not touch any existing table at all.
    """

    @pytest.mark.asyncio
    async def test_preexisting_rows_are_untouched(self):
        """A populated pre-existing table is bit-identical after upgrade()."""
        engine = await _bare_engine()

        # Stand up an unrelated table with rows, as a stand-in for every table
        # already live on the deployed database.
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

    @pytest.mark.asyncio
    async def test_migration_source_contains_no_alter_or_update(self):
        """Structural guard: the migration creates tables and nothing else.

        Reading the source is the only way to assert the *absence* of a
        destructive operation against tables this test does not know about. A
        future edit that adds a column to a populated table has to change this
        test, which is the review signal the issue asks for.
        """
        source = (MIGRATIONS_DIR / "029_orchestration_graph.py").read_text()
        # Strip the docstring, which legitimately discusses ALTER/UPDATE/backfill.
        body = source.split('"""', 2)[-1]
        lowered = body.lower()

        for forbidden in ("alter table", "op.alter_column", "op.add_column", "op.drop_column", "update ", "op.bulk_insert"):
            assert forbidden not in lowered, f"migration must only create new tables; found {forbidden!r}"


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
            MIG_029.upgrade()
        return "".join(chunks)

    def test_plan_document_renders_as_jsonb_on_postgres(self):
        """`plan_document` must be JSONB, not JSON.

        A bare `sa.JSON()` renders as `JSON` on Postgres — the migration applies
        cleanly and nothing fails, but the column is not GIN-indexable and the
        JSONB operator set is unavailable. This is invisible on SQLite, which is
        why it is asserted against the Postgres compiler explicitly.
        """
        ddl = self._render_postgres_ddl()
        assert "plan_document JSONB" in ddl, f"plan_document must render as JSONB on Postgres; got:\n{ddl[:2000]}"

    def test_timestamps_are_timezone_aware_on_postgres(self):
        """A naive timestamp column makes decision ordering ambiguous across zones."""
        ddl = self._render_postgres_ddl()
        assert "TIMESTAMP WITH TIME ZONE" in ddl

    def test_partial_index_and_constraints_render_on_postgres(self):
        ddl = self._render_postgres_ddl()
        assert "WHERE superseded_at IS NULL" in ddl, "the in-force partial index must survive on Postgres"
        assert ddl.count("CREATE TABLE") == 5
        assert ddl.count("CREATE UNIQUE INDEX") == 3
        assert "ON DELETE CASCADE" in ddl


class TestRevisionChain:
    def test_revision_id_and_down_revision(self):
        """Chains onto the real single head.

        Specified as 026-onto-025, but 026/027/028 landed on 025 since. Chaining
        onto 025 now would create a SECOND HEAD, and `alembic upgrade head` fails
        outright on multiple heads — the deploy breaks rather than degrading.
        """
        assert MIG_029.revision == "029_orchestration_graph"
        assert MIG_029.down_revision == "028_usage_cache_tokens"

    def test_migration_leaves_exactly_one_head(self):
        """The whole point of the renumber: assert it, do not trust it.

        Parses every version file and walks the chain. Two heads is a broken
        deploy, and it is invisible until `alembic upgrade head` runs in a pod.

        Asserts the *count*, not the head's name: the head advances with every
        migration that lands (030 in #4287), and a name-pinned assertion turns
        every future migration into a spurious failure here — which trains people
        to edit this test rather than read it. What must never change is that
        there is exactly one head, and that 029 is still on the chain.
        """
        import ast

        revisions: dict[str, str | None] = {}
        for path in MIGRATIONS_DIR.glob("*.py"):
            if path.name == "__init__.py":
                continue
            tree = ast.parse(path.read_text(), filename=str(path))
            found: dict[str, str | None] = {}
            for node in tree.body:
                if not isinstance(node, ast.AnnAssign | ast.Assign):
                    continue
                targets = [node.target] if isinstance(node, ast.AnnAssign) else node.targets
                names = {t.id for t in targets if isinstance(t, ast.Name)} & {"revision", "down_revision"}
                if not names or not isinstance(node.value, ast.Constant):
                    continue
                for name in names:
                    found[name] = node.value.value
            if "revision" in found:
                revisions[found["revision"]] = found.get("down_revision")

        parents = {down for down in revisions.values() if down is not None}
        heads = sorted(rev for rev in revisions if rev not in parents)

        assert len(heads) == 1, f"expected exactly one head, got {heads}"
        assert "029_orchestration_graph" in revisions, "029 must still be on the chain"


class TestModelMigrationParity:
    """The migration and the models are hand-written separately, so they can drift.

    The migration is what runs against dev; the models are what every test uses.
    When they disagree, tests pass and production breaks — so compare them.
    """

    @pytest.fixture
    async def migrated_columns(self):
        engine = await _bare_engine()
        await _upgrade(engine)
        async with engine.connect() as conn:
            cols = {t: await conn.run_sync(lambda c, t=t: {x["name"] for x in sa_inspect(c).get_columns(t)}) for t in ORCHESTRATION_TABLES}
        await engine.dispose()
        return cols

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "model",
        [
            OrchestrationFlow,
            OrchestrationNode,
            OrchestrationEdge,
            OrchestrationAcceptedPlan,
            OrchestrationDecision,
        ],
        ids=lambda m: m.__tablename__,
    )
    async def test_model_columns_match_migration(self, migrated_columns, model):
        model_cols = {c.name for c in model.__table__.columns}
        assert model_cols == migrated_columns[model.__tablename__], (
            f"{model.__tablename__}: model/migration column drift — "
            f"only in model: {model_cols - migrated_columns[model.__tablename__]}, "
            f"only in migration: {migrated_columns[model.__tablename__] - model_cols}"
        )
