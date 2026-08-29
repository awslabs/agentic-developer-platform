"""Tests for Alembic migration 033 — usage_logs.client_tool.

Issue #4398 (EPIC #4324, FR-6.1–6.3). This file is **mandatory**, and not only
for coverage: `modules/gateway/alembic/**` is absent from `gateway-ci.yml`'s
trigger paths (`src/**`, `tests/**`, `cli/**`, `pyproject.toml`, `Dockerfile`,
frontend, `libs/`, `contracts/`), so a migration-only PR gets **zero CI signal**.
A test under `tests/` is what makes CI run at all for this change. Precedent:
`test_031_usage_graph_address.py`, `test_029_orchestration_graph.py`.

These tests exercise the REAL migration functions imported from the version
module, against SQLite. A test that re-implements the migration proves only that
the author can write the same bug twice.

What is under test:

  - `upgrade()` adds a **nullable** column with **no default**, plus a partial
    index. The shape is the whole point: a `NOT NULL` + `server_default` column
    (025's shape) would make the default BE the backfill, stamping a fabricated
    tool name onto every historical usage row. That is strictly worse than NULL —
    NULL reads as "not captured" (FR-6.2), a fabricated value reads as a real
    tool and lands those rows in a future per-tool breakdown.
  - **No backfill**: pre-existing rows come out with NULL `client_tool` and are
    otherwise byte-identical.
  - `downgrade()` reverses it. Note this is deliberately NOT the operational
    rollback plan (that is "revert the PR"); it is exercised so `alembic
    downgrade` can be trusted to walk past this revision.
  - The revision chains onto the real single head, **`032_budget_usage_org_type`**
    — the revision *id*, which differs from the filename
    (`032_budget_usage_org_entity_type.py`). A stale or dangling `down_revision`
    creates a SECOND HEAD, and `alembic upgrade head` then fails for **everyone**,
    blocking every subsequent gateway deploy.
  - Migration/model parity: both are hand-written, so drift is the live risk —
    the migration is what runs in dev, the models are what the tests use.
"""

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from src.shared.models.usage import UsageLog

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "alembic" / "versions"

COLUMN = "client_tool"
INDEX = "ix_usage_logs_client_tool"
THIS_MIGRATION = "033_client_tool_capture.py"


def _load_migration(filename: str):
    """Import a migration module by path (they are not an importable package)."""
    path = MIGRATIONS_DIR / filename
    spec = importlib.util.spec_from_file_location(filename.replace(".py", ""), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MIG_033 = _load_migration(THIS_MIGRATION)


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


# The pre-033 usage_logs table, as it exists on the deployed database. Written out
# rather than built from the ORM metadata on purpose: the model now DECLARES
# client_tool, so `create_all` would create the column and `upgrade()` would fail
# with "duplicate column" — masking nothing but breaking the test. This is the
# real starting state the migration must apply to.
_PRE_033_USAGE_LOGS = """
    CREATE TABLE usage_logs (
        id VARCHAR(255) NOT NULL PRIMARY KEY,
        org_id VARCHAR(255) NOT NULL,
        timestamp DATETIME,
        department_id VARCHAR(255) NOT NULL,
        team_id VARCHAR(255) NOT NULL,
        user_id VARCHAR(255) NOT NULL,
        account_type VARCHAR(20) NOT NULL,
        model VARCHAR(255) NOT NULL,
        input_tokens INTEGER NOT NULL,
        output_tokens INTEGER NOT NULL,
        cost_usd NUMERIC(10, 6) NOT NULL,
        latency_ms INTEGER NOT NULL,
        status_code INTEGER NOT NULL,
        request_id VARCHAR(255),
        bedrock_account_id VARCHAR(12),
        agent_run_id VARCHAR(255),
        chat_log_s3_key VARCHAR(1024),
        cache_read_input_tokens INTEGER,
        cache_creation_input_tokens INTEGER,
        graph_address VARCHAR(512)
    )
"""


async def _engine_at_pre_033():
    """An engine holding the pre-033 usage_logs table and nothing else."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.execute(sa.text(_PRE_033_USAGE_LOGS))
    return engine


async def _upgrade(engine):
    async with engine.begin() as conn:
        await conn.run_sync(_run_migration, MIG_033.upgrade)


async def _downgrade(engine):
    async with engine.begin() as conn:
        await conn.run_sync(_run_migration, MIG_033.downgrade)


def _columns(sync_conn):
    return {c["name"]: c for c in sa_inspect(sync_conn).get_columns("usage_logs")}


def _indexes(sync_conn):
    return sa_inspect(sync_conn).get_indexes("usage_logs")


class TestUpgradeShape:
    """The column's shape is the contract, not an implementation detail."""

    @pytest.mark.asyncio
    async def test_upgrade_adds_the_column(self):
        engine = await _engine_at_pre_033()
        try:
            async with engine.connect() as conn:
                assert COLUMN not in await conn.run_sync(_columns)
            await _upgrade(engine)
            async with engine.connect() as conn:
                assert COLUMN in await conn.run_sync(_columns)
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_column_is_nullable(self):
        """Nullable is load-bearing twice over.

        It keeps the usage hot path writable during rollout — pre-033 pods INSERT
        without this column, and a NOT NULL column with no default fails every one
        of those in-flight INSERTs. Because `_log_usage` swallows exceptions, that
        failure returns HTTP 200 with no usage row at all: unmetered and unbilled,
        with no alarm. It is also semantically required: NULL is the representation
        of "not captured" (FR-6.2).
        """
        engine = await _engine_at_pre_033()
        try:
            await _upgrade(engine)
            async with engine.connect() as conn:
                assert (await conn.run_sync(_columns))[COLUMN]["nullable"] is True
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_column_has_no_server_default(self):
        """025's `NOT NULL` + `server_default` makes the default BE the backfill.

        This migration follows 018/031 instead. A default here would stamp a
        fabricated tool name onto every historical usage row, and those rows would
        then be counted into a future per-tool breakdown as though they belonged
        to it — the exact "NULL treated as unknown tool" failure the issue's
        impact table names.
        """
        engine = await _engine_at_pre_033()
        try:
            await _upgrade(engine)
            async with engine.connect() as conn:
                column = (await conn.run_sync(_columns))[COLUMN]
            assert column.get("default") is None
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_partial_index_exists_on_the_column(self):
        """018/031's shape: a per-tool rollup only ever scans captured rows."""
        engine = await _engine_at_pre_033()
        try:
            await _upgrade(engine)
            async with engine.connect() as conn:
                indexes = await conn.run_sync(_indexes)
            by_name = {idx["name"]: idx for idx in indexes}
            assert INDEX in by_name
            assert by_name[INDEX]["column_names"] == [COLUMN]
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_index_is_actually_partial(self):
        """The `WHERE ... IS NOT NULL` predicate must survive into the DDL.

        `postgresql_where` alone would silently produce a FULL index on SQLite,
        so the predicate is asserted against the emitted SQL rather than trusted.
        """
        engine = await _engine_at_pre_033()
        try:
            await _upgrade(engine)
            async with engine.connect() as conn:
                sql = (
                    await conn.execute(
                        sa.text("SELECT sql FROM sqlite_master WHERE type='index' AND name=:n"),
                        {"n": INDEX},
                    )
                ).scalar_one()
            assert "WHERE" in sql.upper()
            assert "NOT NULL" in sql.upper()
        finally:
            await engine.dispose()


class TestNoBackfill:
    """The explicit no-backfill contract: existing rows stay NULL.

    This is the FR-6.2 guarantee at the storage layer. Validation item 4 of the
    issue: rows written before the migration read back as not-captured.
    """

    _ROW = {
        "id": "usage-1",
        "org_id": "org-a",
        "department_id": "dept-a",
        "team_id": "team-a",
        "user_id": "user-a",
        "account_type": "human",
        "model": "anthropic.claude-3-5-sonnet",
        "input_tokens": 100,
        "output_tokens": 50,
        "cost_usd": "0.001234",
        "latency_ms": 900,
        "status_code": 200,
        "agent_run_id": "evt-abc-123",
    }

    async def _seed(self, engine):
        async with engine.begin() as conn:
            await conn.execute(
                sa.text(
                    "INSERT INTO usage_logs (id, org_id, department_id, team_id, user_id, account_type, model, "
                    "input_tokens, output_tokens, cost_usd, latency_ms, status_code, agent_run_id) VALUES "
                    "(:id, :org_id, :department_id, :team_id, :user_id, :account_type, :model, "
                    ":input_tokens, :output_tokens, :cost_usd, :latency_ms, :status_code, :agent_run_id)"
                ),
                self._ROW,
            )

    @pytest.mark.asyncio
    async def test_preexisting_row_has_null_client_tool(self):
        """AC (validation 4): after upgrade(), a pre-existing row is NULL.

        NULL means "not captured", which is the truth for every pre-feature row.
        Any non-null value here would be invented.
        """
        engine = await _engine_at_pre_033()
        try:
            await self._seed(engine)
            await _upgrade(engine)
            async with engine.connect() as conn:
                value = (await conn.execute(sa.text("SELECT client_tool FROM usage_logs WHERE id='usage-1'"))).scalar_one()
            assert value is None
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_preexisting_row_is_otherwise_byte_identical(self):
        """Every other column survives the migration unchanged.

        Cost data in particular: a migration that rewrote `cost_usd` would corrupt
        the ledger this column is being added to.
        """
        engine = await _engine_at_pre_033()
        try:
            await self._seed(engine)
            columns = ", ".join(
                [
                    "id",
                    "org_id",
                    "department_id",
                    "team_id",
                    "user_id",
                    "account_type",
                    "model",
                    "input_tokens",
                    "output_tokens",
                    "cost_usd",
                    "latency_ms",
                    "status_code",
                    "agent_run_id",
                ]
            )
            async with engine.connect() as conn:
                before = (await conn.execute(sa.text(f"SELECT {columns} FROM usage_logs ORDER BY id"))).all()

            await _upgrade(engine)

            async with engine.connect() as conn:
                after = (await conn.execute(sa.text(f"SELECT {columns} FROM usage_logs ORDER BY id"))).all()
            assert before == after
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_row_count_is_unchanged(self):
        """No row is added or removed — the migration is DDL only."""
        engine = await _engine_at_pre_033()
        try:
            await self._seed(engine)
            await _upgrade(engine)
            async with engine.connect() as conn:
                count = (await conn.execute(sa.text("SELECT COUNT(*) FROM usage_logs"))).scalar_one()
            assert count == 1
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_migrated_table_accepts_a_captured_value(self):
        """The column is actually writable with a normalised value after upgrade.

        Proves the migration produces a column the writer can use, not just one
        that exists — the smoke test in the issue depends on this.
        """
        engine = await _engine_at_pre_033()
        try:
            await _upgrade(engine)
            async with engine.begin() as conn:
                await conn.execute(
                    sa.text(
                        "INSERT INTO usage_logs (id, org_id, department_id, team_id, user_id, account_type, model, "
                        "input_tokens, output_tokens, cost_usd, latency_ms, status_code, client_tool) VALUES "
                        "('u2', 'org-a', 'd', 't', 'u', 'human', 'm', 1, 2, 0.5, 10, 200, 'claude_code')"
                    )
                )
            async with engine.connect() as conn:
                value = (await conn.execute(sa.text("SELECT client_tool FROM usage_logs WHERE id='u2'"))).scalar_one()
            assert value == "claude_code"
        finally:
            await engine.dispose()


class TestDowngrade:
    """downgrade() is exercised, not assumed.

    Note: per the issue and `delivery-plan.md`, downgrade is deliberately NOT the
    operational rollback (that is "revert the PR to stop population", leaving the
    additive nullable column in place). These tests prove the function is correct
    if it is ever run, not that anyone should run it.
    """

    @pytest.mark.asyncio
    async def test_downgrade_removes_column_and_index(self):
        engine = await _engine_at_pre_033()
        try:
            await _upgrade(engine)
            await _downgrade(engine)
            async with engine.connect() as conn:
                assert COLUMN not in await conn.run_sync(_columns)
                assert INDEX not in {idx["name"] for idx in await conn.run_sync(_indexes)}
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_downgrade_preserves_preexisting_rows(self):
        """Rolling back cannot lose data that predates the migration.

        Safe by construction here — nothing was backfilled, so there is nothing in
        the dropped column that existed before it.
        """
        engine = await _engine_at_pre_033()
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    sa.text(
                        "INSERT INTO usage_logs (id, org_id, department_id, team_id, user_id, account_type, model, "
                        "input_tokens, output_tokens, cost_usd, latency_ms, status_code) VALUES "
                        "('u1', 'org-a', 'd', 't', 'u', 'human', 'm', 1, 2, 0.5, 10, 200)"
                    )
                )
            await _upgrade(engine)
            await _downgrade(engine)
            async with engine.connect() as conn:
                cost = (await conn.execute(sa.text("SELECT cost_usd FROM usage_logs WHERE id='u1'"))).scalar_one()
            assert float(cost) == 0.5
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_upgrade_is_reapplicable_after_downgrade(self):
        """upgrade → downgrade → upgrade, proving the pair is a real inverse.

        A downgrade that leaves the index behind passes the column assertion above
        but makes re-upgrade fail on a duplicate index name.
        """
        engine = await _engine_at_pre_033()
        try:
            await _upgrade(engine)
            await _downgrade(engine)
            await _upgrade(engine)
            async with engine.connect() as conn:
                assert COLUMN in await conn.run_sync(_columns)
        finally:
            await engine.dispose()


class TestRevisionChain:
    """Second-head prevention (issue validation item 5).

    A broken `down_revision` silently SKIPS the migration and live code then
    queries a column that does not exist; a duplicate one creates a second head
    and `alembic upgrade head` fails for everyone, blocking all gateway deploys.
    """

    def test_revision_id(self):
        assert MIG_033.revision == "033_client_tool_capture"

    def test_chains_onto_the_real_single_head(self):
        """The head was resolved at implementation time, not hard-coded.

        The issue said "the next one after 032" precisely because unrelated merges
        take numbers in between. `032_budget_usage_org_type` is the revision **id**
        of `032_budget_usage_org_entity_type.py` — the id and the filename differ,
        and naming the filename here would be a dangling reference.
        """
        assert MIG_033.down_revision == "032_budget_usage_org_type"

    def test_down_revision_names_a_real_existing_revision(self):
        """Catches a typo'd chain: the parent id must exist in some version file.

        Stronger than checking a filename exists — it resolves the actual
        `revision` declared by each module, which is what alembic links on.
        """
        known = set()
        for path in MIGRATIONS_DIR.glob("*.py"):
            if path.name.startswith("__"):
                continue
            known.add(_load_migration(path.name).revision)
        assert MIG_033.down_revision in known

    def test_revision_id_is_unique(self):
        """Two version files claiming one revision id is an ambiguous chain."""
        duplicates = [
            path.name
            for path in MIGRATIONS_DIR.glob("*.py")
            if path.name not in (THIS_MIGRATION,) and not path.name.startswith("__") and _load_migration(path.name).revision == MIG_033.revision
        ]
        assert duplicates == []

    def test_revision_ids_fit_alembic_version_column(self):
        """#4123: an id over 32 chars runs upgrade() then rolls back on Postgres.

        SQLite does not enforce VARCHAR length, so CI cannot catch this at
        runtime — only a static check can.
        """
        assert len(MIG_033.revision) <= 32
        assert len(MIG_033.down_revision) <= 32

    def test_exactly_one_head_across_all_version_files(self):
        """`alembic heads` must report a SINGLE head — the issue's hard gate.

        Computed structurally rather than trusted: a head is a revision that no
        other revision names as its parent. After this migration lands there must
        be exactly one, and it must be this revision. This is the executable form
        of "confirm `alembic current` reports a single head before opening the PR",
        so a concurrently-merged sibling migration fails CI here instead of
        breaking `alembic upgrade head` in dev.
        """
        revisions: set[str] = set()
        parents: set[str] = set()
        for path in MIGRATIONS_DIR.glob("*.py"):
            if path.name.startswith("__"):
                continue
            module = _load_migration(path.name)
            revisions.add(module.revision)
            if module.down_revision:
                parents.add(module.down_revision)

        heads = revisions - parents
        assert heads == {MIG_033.revision}, f"expected a single head ({MIG_033.revision}), found: {sorted(heads)}"


class TestModelParity:
    """Migration and model are both hand-written, so drift is the live risk."""

    def test_model_declares_the_column(self):
        assert hasattr(UsageLog, COLUMN)

    def test_model_column_is_nullable_with_no_default(self):
        """The model must agree with the migration, or local dev `create_all`
        builds a different table than the one that runs in dev."""
        column = UsageLog.__table__.columns[COLUMN]
        assert column.nullable is True
        assert column.default is None
        assert column.server_default is None

    def test_model_column_length_matches_migration(self):
        """A shorter model column would truncate values the migration accepts."""
        assert UsageLog.__table__.columns[COLUMN].type.length == MIG_033._CLIENT_TOOL_LEN

    def test_column_is_wide_enough_for_every_known_tool(self):
        """The closed set must fit the column, or a real capture silently fails.

        Ties the schema width to the actual normaliser vocabulary rather than to a
        number someone eyeballed — adding a longer tool name to the closed set
        fails here instead of erroring on INSERT in production.
        """
        from src.proxy.client_tool import KNOWN_CLIENT_TOOLS

        longest = max(KNOWN_CLIENT_TOOLS, key=len)
        assert len(longest) <= MIG_033._CLIENT_TOOL_LEN
