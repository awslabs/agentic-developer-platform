"""Tests for Alembic migration 039 — orchestration_flows design capture.

Issue #4885 (child of #4869, EPIC #4191). This file is **mandatory**, and not
only for coverage: `modules/gateway/alembic/**` is absent from `gateway-ci.yml`'s
trigger paths, so a migration-only change gets **zero CI signal**. A test under
`tests/` is what makes CI run at all for it. Precedent:
`test_031_usage_graph_address.py`, `test_029_orchestration_graph.py`.

These tests exercise the REAL migration functions imported from the version
module, against SQLite. A test that re-implements the migration proves only that
the author can write the same bug twice.

What is under test, worst-consequence first:

  - **No backfill.** A pre-existing flow row comes out with both columns NULL and
    every other value byte-identical. This is the issue's headline guardrail:
    `NULL` means "we do not know", which is the honest value for every flow that
    exists today, and a fabricated design history is worse than an absent one
    because it renders as a real record of gates that never happened.
  - **Both columns nullable with no server default.** A `NOT NULL` +
    `server_default` column (025's shape) would make the default BE the backfill.
    Nullability also keeps registration writable during rollout: pre-039 gateway
    pods INSERT into `orchestration_flows` without these columns.
  - `downgrade()` reverses it. This is exercised rather than assumed because it is
    the documented rollback path.
  - The revision chains onto the real single head, **038**. A stale
    `down_revision` creates a SECOND HEAD and `alembic upgrade head` then fails
    outright for everyone.
  - The revision id fits `alembic_version.version_num` VARCHAR(32). SQLite does
    not enforce VARCHAR length, so an over-long id passes every other test here
    and overflows on Postgres only.
  - Migration/model parity: both are hand-written, so drift is the live risk. The
    migration is what runs in dev; the models are what the tests use.

Round-tripping the JSON payload — `skipped` vs `not_reached` staying distinct
through the column, and the five canonical stage names — is asserted here at the
storage layer, and at the validation layer in `tests/orchestration/`.
"""

import ast
import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from src.orchestration.models import OrchestrationFlow

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "alembic" / "versions"

TABLE = "orchestration_flows"
NEW_COLUMNS = ("description", "design_history")

# Alembic's default alembic_version.version_num width.
MAX_REVISION_ID_LEN = 32


def _load_migration(filename: str):
    """Import a migration module by path (they are not an importable package)."""
    path = MIGRATIONS_DIR / filename
    spec = importlib.util.spec_from_file_location(filename.replace(".py", ""), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MIG_039 = _load_migration("039_flow_design_capture.py")


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


# The pre-039 orchestration_flows table, as it exists on the deployed database.
# Written out rather than built from the ORM metadata on purpose: the model now
# DECLARES both columns, so `create_all` would create them and `upgrade()` would
# fail with "duplicate column" — masking nothing but breaking the test. This is
# the real starting state the migration must apply to.
_PRE_039_FLOWS = """
    CREATE TABLE orchestration_flows (
        id VARCHAR(36) NOT NULL PRIMARY KEY,
        org_id VARCHAR(255) NOT NULL,
        slug VARCHAR(128) NOT NULL,
        title VARCHAR(512) NOT NULL,
        intent_ref VARCHAR(64),
        state VARCHAR(32) NOT NULL,
        created_at DATETIME NOT NULL,
        updated_at DATETIME
    )
"""

# One flow registered before this migration existed. Its design history is
# genuinely unknown, and the whole point of the no-backfill contract is that it
# stays that way.
_EXISTING_FLOW = {
    "id": "76fea8c6-8e16-4279-aab2-e7b30989be68",
    "org_id": "aws-e",
    "slug": "aidlc-delivery-loop-4645",
    "title": "Delivery loop for #4645",
    "intent_ref": "4645",
    "state": "pending",
    "created_at": "2026-09-02 00:59:33",
    "updated_at": None,
}


async def _engine_at_pre_039(*, with_row: bool = False):
    """An engine holding the pre-039 orchestration_flows table and nothing else."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.execute(sa.text(_PRE_039_FLOWS))
        if with_row:
            await conn.execute(
                sa.text(
                    "INSERT INTO orchestration_flows "
                    "(id, org_id, slug, title, intent_ref, state, created_at, updated_at) "
                    "VALUES (:id, :org_id, :slug, :title, :intent_ref, :state, :created_at, :updated_at)"
                ),
                _EXISTING_FLOW,
            )
    return engine


async def _upgrade(engine):
    async with engine.begin() as conn:
        await conn.run_sync(_run_migration, MIG_039.upgrade)


async def _downgrade(engine):
    async with engine.begin() as conn:
        await conn.run_sync(_run_migration, MIG_039.downgrade)


def _columns(sync_conn):
    return {c["name"]: c for c in sa_inspect(sync_conn).get_columns(TABLE)}


class TestNoBackfill:
    """The headline guardrail: existing flows read NULL, not a fabricated history.

    Placed first because it is the failure with the worst consequence. A synthetic
    default would make every historic flow claim a design history it never had —
    and unlike an absent one, a fabricated record renders on the card as real and
    looks authoritative.
    """

    @pytest.mark.asyncio
    async def test_existing_row_reads_null_for_both_columns(self):
        engine = await _engine_at_pre_039(with_row=True)
        try:
            await _upgrade(engine)
            async with engine.connect() as conn:
                row = (await conn.execute(sa.text("SELECT description, design_history FROM orchestration_flows"))).one()
            assert row.description is None, "description was backfilled; it must read as 'we do not know'"
            assert row.design_history is None, "design_history was backfilled with a history this flow never had"
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_existing_row_is_otherwise_byte_identical(self):
        """Every pre-existing value survives untouched.

        A migration that "helpfully" normalised a title or stamped `updated_at`
        would rewrite history on a table whose whole purpose is to be the durable
        record of what was accepted.
        """
        engine = await _engine_at_pre_039(with_row=True)
        try:
            async with engine.connect() as conn:
                before = (await conn.execute(sa.text("SELECT * FROM orchestration_flows"))).mappings().one()
            await _upgrade(engine)
            async with engine.connect() as conn:
                after = (await conn.execute(sa.text("SELECT * FROM orchestration_flows"))).mappings().one()
            for key, value in before.items():
                assert after[key] == value, f"{key} changed: {value!r} -> {after[key]!r}"
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_upgrade_contains_no_update_statement(self):
        """The no-backfill contract, asserted on the migration's own source.

        The two runtime tests above prove no *row* changed; this proves the
        migration has no mechanism to change one. A later edit that adds an
        `op.execute("UPDATE ...")` for a table with no rows in this fixture would
        pass those tests and still ship a backfill.
        """
        source = (MIGRATIONS_DIR / "039_flow_design_capture.py").read_text()
        tree = ast.parse(source)
        upgrade = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "upgrade")
        # Only `op.*` calls — those are the DDL/DML operations. `sa.Column(...)` and
        # `sa.Text()` are type constructions passed *into* them and say nothing
        # about what the migration does to rows.
        operations = {
            node.func.attr
            for node in ast.walk(upgrade)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "op"
        }
        assert operations == {"add_column"}, f"upgrade() performs more than add_column: {sorted(operations)}"


class TestUpgradeShape:
    """The columns' shape is the contract, not an implementation detail."""

    @pytest.mark.asyncio
    async def test_upgrade_adds_both_columns(self):
        engine = await _engine_at_pre_039()
        try:
            async with engine.connect() as conn:
                existing = await conn.run_sync(_columns)
            for column in NEW_COLUMNS:
                assert column not in existing
            await _upgrade(engine)
            async with engine.connect() as conn:
                migrated = await conn.run_sync(_columns)
            for column in NEW_COLUMNS:
                assert column in migrated
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("column", NEW_COLUMNS)
    async def test_column_is_nullable_with_no_server_default(self, column):
        """Nullable and defaultless is load-bearing twice over.

        Semantically, NULL is the honest value for every flow registered before
        this migration. Operationally, a NOT NULL column with no default would
        fail every INSERT from a pre-039 gateway pod mid-rollout — turning a
        routine deploy into a registration outage.
        """
        engine = await _engine_at_pre_039()
        try:
            await _upgrade(engine)
            async with engine.connect() as conn:
                spec = (await conn.run_sync(_columns))[column]
            assert spec["nullable"] is True, f"{column} is NOT NULL; NULL must stay reachable"
            assert spec["default"] is None, f"{column} has a server default, which would BE the backfill"
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_no_index_is_created_on_either_column(self):
        """Neither column is a lookup key — both ride a row already being fetched.

        An index here would cost every write to serve a query nobody issues, and
        `description` is deliberately not searchable (`q` filters title / slug /
        intent_ref).
        """
        engine = await _engine_at_pre_039()
        try:
            async with engine.connect() as conn:
                before = await conn.run_sync(lambda c: sa_inspect(c).get_indexes(TABLE))
            await _upgrade(engine)
            async with engine.connect() as conn:
                after = await conn.run_sync(lambda c: sa_inspect(c).get_indexes(TABLE))
            assert after == before
        finally:
            await engine.dispose()


class TestRoundTrip:
    """The stored JSON survives the column, distinctions intact."""

    @pytest.mark.asyncio
    async def test_skipped_and_not_reached_round_trip_as_distinct_values(self):
        """The two states must not merge in storage.

        `skipped` means scope decided this stage never runs; `not_reached` means it
        will run and the loop has not got there. Rendering a skipped stage as
        pending reads as unfinished work that is never coming.
        """
        engine = await _engine_at_pre_039()
        try:
            await _upgrade(engine)
            history = {
                "scope": "poc",
                "stages": [
                    {"name": "intent-capture", "state": "approved", "approved_at": "2026-09-02T00:13:13Z"},
                    {"name": "reverse-engineering", "state": "skipped"},
                    {"name": "requirements-analysis", "state": "open"},
                    {"name": "delivery-planning", "state": "not_reached"},
                ],
            }
            async with engine.begin() as conn:
                await conn.execute(
                    sa.text(
                        "INSERT INTO orchestration_flows "
                        "(id, org_id, slug, title, state, created_at, description, design_history) "
                        "VALUES ('f1', 'aws-e', 'slug', 'Title', 'pending', '2026-09-10 00:00:00', :d, :h)"
                    ),
                    {"d": "A one-line use case.", "h": sa.JSON().bind_processor(conn.dialect)(history)},
                )
                stored = (await conn.execute(sa.text("SELECT description, design_history FROM orchestration_flows"))).one()

            import json

            read_back = json.loads(stored.design_history)
            states = {stage["name"]: stage["state"] for stage in read_back["stages"]}
            assert states["reverse-engineering"] == "skipped"
            assert states["delivery-planning"] == "not_reached"
            assert states["reverse-engineering"] != states["delivery-planning"], "the two states collapsed in storage"
            assert stored.description == "A one-line use case."
        finally:
            await engine.dispose()


class TestDowngrade:
    """The documented rollback path, exercised rather than assumed."""

    @pytest.mark.asyncio
    async def test_downgrade_removes_both_columns(self):
        engine = await _engine_at_pre_039()
        try:
            await _upgrade(engine)
            await _downgrade(engine)
            async with engine.connect() as conn:
                remaining = await conn.run_sync(_columns)
            for column in NEW_COLUMNS:
                assert column not in remaining
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_downgrade_preserves_a_pre_existing_row(self):
        """Safe by construction: nothing was backfilled, so nothing is lost."""
        engine = await _engine_at_pre_039(with_row=True)
        try:
            await _upgrade(engine)
            await _downgrade(engine)
            async with engine.connect() as conn:
                after = (await conn.execute(sa.text("SELECT * FROM orchestration_flows"))).mappings().one()
            assert after["id"] == _EXISTING_FLOW["id"]
            assert after["title"] == _EXISTING_FLOW["title"]
            assert after["intent_ref"] == _EXISTING_FLOW["intent_ref"]
        finally:
            await engine.dispose()


class TestRevisionChain:
    """A stale down_revision creates a second head and breaks upgrade for everyone."""

    def test_revises_the_real_single_head(self):
        assert MIG_039.revision == "039_flow_design_capture"
        assert MIG_039.down_revision == "038_cli_auth_requests"

    def test_the_chain_still_has_exactly_one_head(self):
        """Parsed statically across every version file, so a rebase collision fails here."""
        revisions: dict[str, str | None] = {}
        for path in MIGRATIONS_DIR.glob("*.py"):
            tree = ast.parse(path.read_text(), filename=str(path))
            found: dict[str, str | None] = {}
            for node in tree.body:
                targets = [node.target] if isinstance(node, ast.AnnAssign) else getattr(node, "targets", [])
                names = {t.id for t in targets if isinstance(t, ast.Name)} & {"revision", "down_revision"}
                if not names or not isinstance(node.value, ast.Constant) or not isinstance(node.value.value, str | None):
                    continue
                for name in names:
                    found[name] = node.value.value
            if "revision" in found:
                revisions[found["revision"]] = found.get("down_revision")

        parents = {down for down in revisions.values() if down is not None}
        heads = sorted(revision for revision in revisions if revision not in parents)
        # Assert *one* head, not that 039 is it. Both #4840 (040_team_memberships)
        # and #4843 (041-043, re-chained onto 040 after #4917 merged first) chained
        # later migrations onto 039 — the normal, healthy case. Pinning the head
        # name here made this test fail on every subsequent migration, which trains
        # people to edit the assertion rather than read it; so it follows 033's
        # shape instead (see test_033_client_tool_capture.py): exactly one head,
        # and 039 is either that head or a link something else chained onto.
        assert len(heads) == 1, f"expected exactly one head, found: {heads}"
        assert MIG_039.revision in parents or heads == [MIG_039.revision], "039 has been orphaned off the chain"

    def test_revision_id_fits_the_alembic_version_column(self):
        """SQLite does not enforce VARCHAR length; Postgres does.

        An over-long id runs `upgrade()` to completion and then overflows on the
        version write, rolling the whole migration back and leaving the schema one
        revision behind live code. Invisible in every other test in this file.
        """
        assert len(MIG_039.revision) <= MAX_REVISION_ID_LEN
        assert len(MIG_039.down_revision) <= MAX_REVISION_ID_LEN


class TestModelMigrationParity:
    """The migration and the models are hand-written separately, so they can drift.

    The migration is what runs against dev; the models are what every test uses.
    When they disagree, tests pass and production breaks.
    """

    def test_the_model_declares_both_columns(self):
        model_columns = {column.name for column in OrchestrationFlow.__table__.columns}
        for column in NEW_COLUMNS:
            assert column in model_columns

    @pytest.mark.asyncio
    async def test_migrated_columns_match_the_model(self):
        engine = await _engine_at_pre_039()
        try:
            await _upgrade(engine)
            async with engine.connect() as conn:
                migrated = set(await conn.run_sync(_columns))
            assert {column.name for column in OrchestrationFlow.__table__.columns} == migrated
        finally:
            await engine.dispose()

    def test_the_migration_uses_the_same_json_variant_as_the_model(self):
        """JSONB on Postgres, JSON on SQLite — declared identically in both places.

        A bare `sa.JSON()` in the migration renders as `JSON` on Postgres too and
        would quietly forfeit JSONB, disagreeing with the model. Compared by
        dialect-compiled type rather than by identity, because they are two
        separate declarations by design.
        """
        from sqlalchemy.dialects import postgresql, sqlite

        from src.orchestration.models import JSON_DOC as MODEL_JSON_DOC

        for dialect in (postgresql.dialect(), sqlite.dialect()):
            assert MIG_039.JSON_DOC.compile(dialect) == MODEL_JSON_DOC.compile(dialect)
