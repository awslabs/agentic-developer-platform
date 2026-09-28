"""Tests for Alembic migration 032 — budget_usage entity_type 'organization' -> 'org'.

Issue #4322. This file is **mandatory** for the same reason 029's is: the paths
that trigger `gateway-ci.yml` do not include `modules/gateway/alembic/**`, so a
migration-only change gets **zero CI signal**. A test under `tests/` is what
makes CI run at all for the migration half of this PR.

These tests exercise the REAL `upgrade()` imported from the version module,
against SQLite. A test that re-implements the merge SQL proves only that the
author can write the same bug twice.

What is under test, and why each case earns its place:

  - **Rename-only.** The common case: an `"organization"` row with no `"org"`
    counterpart becomes an `"org"` row with its totals intact. If this is wrong
    the whole migration is pointless.
  - **Merge.** Both spellings present for one key — which really happens, because
    the tracker Lambda wrote `"organization"` while `src/budget/service.py` and
    `enforcement_service.py` both write `entity_type.value` == `"org"`. The
    result must be ONE row holding the SUM. This is the issue's named
    double-count hazard: two rows the reader sums as one means the org cap fires
    at half its true headroom and throttles legitimate work platform-wide.
  - **Idempotency.** `run-gateway-migrations.yml` is operator-dispatched, so a
    re-run is a realistic operator action. A second `upgrade()` must not double
    the ledger it just corrected.
  - **Rename and merge are disjoint.** The migration's `NOT EXISTS` guard is what
    keeps the two statements off each other's rows; without it the rename
    collides on `uq_budget_usage`. Only a fixture holding both row kinds at once
    can catch that, and this suite was checked against the mutation (guard
    removed → 5 failures; delete moved first → 9).
  - **Other entity types untouched.** `user`/`team`/`agent`/`root_user` literals
    already agreed with the reader's enum, so they must be byte-identical
    afterwards. This is the issue's explicit regression requirement and the scope
    guard on a migration that rewrites rows in a populated billing table.
  - **The reader can find the result.** The surviving rows are keyed on the exact
    string `EntityType.ORGANIZATION.value`, read off the enum rather than
    hard-coded, so the test tracks the enum if it ever moves.
"""

import importlib.util
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from src.shared.models.base import Base
from src.shared.models.budget import BudgetUsage
from src.shared.schemas.budget import EntityType

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "alembic" / "versions"

# The value enforcement queries with. Read off the enum, never re-typed.
ORG = EntityType.ORGANIZATION.value
# The literal the tracker Lambda wrote pre-#4322.
STALE = "organization"

PERIOD_START = date(2026, 8, 1)


def _load_migration(filename: str):
    """Import a migration module by path (they are not an importable package)."""
    path = MIGRATIONS_DIR / filename
    spec = importlib.util.spec_from_file_location(filename.replace(".py", ""), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MIG_032 = _load_migration("032_budget_usage_org_entity_type.py")


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


@pytest.fixture
async def engine():
    """An engine with the ORM schema — 032 is data-only and creates no tables.

    `create_all` is correct here (unlike 029's bare-engine fixture): this
    migration must run against an already-populated `budget_usage`, so the table
    has to exist before `upgrade()` and the migration is not what creates it.
    """
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


async def _seed(engine, rows: list[dict]) -> None:
    """Insert `budget_usage` rows. Each dict overrides the defaults below.

    Goes through the mapped table rather than a raw `text()` INSERT so
    SQLAlchemy's `Numeric` type adapts `Decimal` for the SQLite driver, which
    cannot bind one directly. The migration under test is unaffected either way —
    it only ever reads these rows back.
    """
    table = BudgetUsage.__table__
    async with engine.begin() as conn:
        for i, row in enumerate(rows):
            await conn.execute(
                table.insert().values(
                    id=row.get("id", f"row-{i}"),
                    org_id=row.get("org_id", "org-acme"),
                    entity_type=row["entity_type"],
                    entity_id=row.get("entity_id", "org-acme"),
                    period_start=row.get("period_start", PERIOD_START),
                    period_type=row.get("period_type", "monthly"),
                    total_cost_usd=Decimal(row.get("total_cost_usd", "0")),
                    total_tokens=row.get("total_tokens", 0),
                    request_count=row.get("request_count", 0),
                )
            )


async def _upgrade(engine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(_run_migration, MIG_032.upgrade)


async def _rows(engine) -> list[dict]:
    """Every `budget_usage` row as a plain dict, ordered for stable comparison.

    Selected through the mapped table so `total_cost_usd` comes back as a
    `Decimal` — a raw `text()` SELECT yields a float on SQLite, which would make
    the sub-cent precision assertions compare the wrong thing.
    """
    table = BudgetUsage.__table__
    async with engine.connect() as conn:
        result = await conn.execute(table.select().order_by(table.c.entity_type, table.c.entity_id, table.c.period_type))
        return [dict(r._mapping) for r in result]


async def _org_rows(engine, entity_id: str = "org-acme", period_type: str = "monthly") -> list[dict]:
    return [r for r in await _rows(engine) if r["entity_type"] == ORG and r["entity_id"] == entity_id and r["period_type"] == period_type]


class TestRenameOnly:
    """A stale row with nothing to merge into is relabelled in place."""

    @pytest.mark.asyncio
    async def test_stale_row_becomes_an_org_row(self, engine):
        await _seed(engine, [{"entity_type": STALE, "total_cost_usd": "12.500000", "total_tokens": 400, "request_count": 7}])

        await _upgrade(engine)

        rows = await _rows(engine)
        assert len(rows) == 1, "a rename must not create a second row"
        assert rows[0]["entity_type"] == ORG
        assert Decimal(str(rows[0]["total_cost_usd"])) == Decimal("12.500000")
        assert rows[0]["total_tokens"] == 400
        assert rows[0]["request_count"] == 7

    @pytest.mark.asyncio
    async def test_rename_preserves_the_row_id(self, engine):
        """In-place UPDATE, not insert+delete — anything holding the id still resolves."""
        await _seed(engine, [{"id": "keep-me", "entity_type": STALE, "total_cost_usd": "1.000000"}])

        await _upgrade(engine)

        rows = await _rows(engine)
        assert rows[0]["id"] == "keep-me"

    @pytest.mark.asyncio
    async def test_no_stale_rows_survive(self, engine):
        """The old spelling is gone entirely — a survivor is invisible to the reader."""
        await _seed(
            engine,
            [
                {"entity_type": STALE, "period_type": "daily", "total_cost_usd": "1.000000"},
                {"entity_type": STALE, "period_type": "weekly", "total_cost_usd": "2.000000"},
                {"entity_type": STALE, "period_type": "monthly", "total_cost_usd": "3.000000"},
            ],
        )

        await _upgrade(engine)

        assert not [r for r in await _rows(engine) if r["entity_type"] == STALE]


class TestMergeIsNotADoubleCount:
    """Both spellings for one key collapse to ONE row holding their SUM."""

    @pytest.mark.asyncio
    async def test_single_row_equal_to_the_sum(self, engine):
        """The issue's named hazard: duplication, not summation, halves the cap's headroom."""
        await _seed(
            engine,
            [
                {"id": "stale", "entity_type": STALE, "total_cost_usd": "10.250000", "total_tokens": 100, "request_count": 3},
                {"id": "fresh", "entity_type": ORG, "total_cost_usd": "5.750000", "total_tokens": 40, "request_count": 2},
            ],
        )

        await _upgrade(engine)

        merged = await _org_rows(engine)
        assert len(merged) == 1, "the merge must not leave two rows the reader sums as one"
        assert Decimal(str(merged[0]["total_cost_usd"])) == Decimal("16.000000")
        assert merged[0]["total_tokens"] == 140
        assert merged[0]["request_count"] == 5

    @pytest.mark.asyncio
    async def test_the_surviving_row_is_the_org_row(self, engine):
        """The pre-existing `"org"` row survives; the stale one is deleted.

        Pinned because the reverse (keep the stale id, rename it, delete the org
        row) would also produce a correct total — but it would silently change
        the id of a row the application may already reference.
        """
        await _seed(
            engine,
            [
                {"id": "stale", "entity_type": STALE, "total_cost_usd": "1.000000"},
                {"id": "fresh", "entity_type": ORG, "total_cost_usd": "2.000000"},
            ],
        )

        await _upgrade(engine)

        merged = await _org_rows(engine)
        assert [r["id"] for r in merged] == ["fresh"]

    @pytest.mark.asyncio
    async def test_sub_cent_precision_survives_the_merge(self, engine):
        """Six decimal places, per migration 030 — the merge must not round.

        030 widened this column precisely because 2dp rounded sub-cent spend to
        zero. A merge that round-trips through a narrower type would undo it.
        """
        await _seed(
            engine,
            [
                {"id": "stale", "entity_type": STALE, "total_cost_usd": "0.000004"},
                {"id": "fresh", "entity_type": ORG, "total_cost_usd": "0.000003"},
            ],
        )

        await _upgrade(engine)

        merged = await _org_rows(engine)
        assert Decimal(str(merged[0]["total_cost_usd"])) == Decimal("0.000007")


class TestRenameAndMergeAreDisjoint:
    """The rename and the merge must not touch the same row.

    Both row kinds present in ONE run is what distinguishes a correct migration
    from one whose rename is unguarded — the latter either aborts on
    `uq_budget_usage` or loses the merged total, and no single-row test can see
    it. Verified to catch a dropped `NOT EXISTS` guard and a mis-placed delete.
    """

    @pytest.mark.asyncio
    async def test_rename_and_merge_in_one_run_do_not_interfere(self, engine):
        await _seed(
            engine,
            [
                # Key A: stale only -> pure rename, must stay 4.000000.
                {"id": "a-stale", "entity_type": STALE, "entity_id": "org-a", "total_cost_usd": "4.000000", "request_count": 1},
                # Key B: both -> merge to 3.000000.
                {"id": "b-stale", "entity_type": STALE, "entity_id": "org-b", "total_cost_usd": "1.000000", "request_count": 1},
                {"id": "b-fresh", "entity_type": ORG, "entity_id": "org-b", "total_cost_usd": "2.000000", "request_count": 1},
            ],
        )

        await _upgrade(engine)

        a = await _org_rows(engine, entity_id="org-a")
        b = await _org_rows(engine, entity_id="org-b")
        assert len(a) == 1
        assert Decimal(str(a[0]["total_cost_usd"])) == Decimal("4.000000"), "the rename and the merge overlapped on one row"
        assert a[0]["request_count"] == 1
        assert len(b) == 1
        assert Decimal(str(b[0]["total_cost_usd"])) == Decimal("3.000000")
        assert b[0]["request_count"] == 2


class TestIdempotency:
    """A second `upgrade()` changes nothing. The workflow is operator-dispatched."""

    @pytest.mark.asyncio
    async def test_second_run_is_a_no_op_after_a_merge(self, engine):
        await _seed(
            engine,
            [
                {"id": "stale", "entity_type": STALE, "total_cost_usd": "10.000000", "total_tokens": 10, "request_count": 1},
                {"id": "fresh", "entity_type": ORG, "total_cost_usd": "5.000000", "total_tokens": 5, "request_count": 1},
            ],
        )

        await _upgrade(engine)
        after_first = await _rows(engine)
        await _upgrade(engine)
        after_second = await _rows(engine)

        assert after_second == after_first, "re-running doubled the ledger it had just corrected"
        assert Decimal(str(after_second[0]["total_cost_usd"])) == Decimal("15.000000")

    @pytest.mark.asyncio
    async def test_second_run_is_a_no_op_after_a_rename(self, engine):
        await _seed(engine, [{"entity_type": STALE, "total_cost_usd": "7.000000", "request_count": 2}])

        await _upgrade(engine)
        after_first = await _rows(engine)
        await _upgrade(engine)

        assert await _rows(engine) == after_first

    @pytest.mark.asyncio
    async def test_upgrade_on_a_clean_database_is_a_no_op(self, engine):
        """An environment deployed after the writer fix has no stale rows at all."""
        await _seed(engine, [{"entity_type": ORG, "total_cost_usd": "9.000000", "request_count": 4}])
        before = await _rows(engine)

        await _upgrade(engine)

        assert await _rows(engine) == before

    @pytest.mark.asyncio
    async def test_upgrade_on_an_empty_table_succeeds(self, engine):
        await _upgrade(engine)

        assert await _rows(engine) == []


class TestScopeIsNarrow:
    """Everything the migration must NOT touch."""

    @pytest.mark.asyncio
    async def test_other_entity_types_are_byte_identical(self, engine):
        """`user`/`team`/`agent`/`root_user` literals already matched the reader.

        The issue's explicit regression requirement, and the scope guard on a
        migration that rewrites rows in a populated billing table.
        """
        others = [
            {"id": "u", "entity_type": "user", "entity_id": "cognito-sub-1", "total_cost_usd": "1.000000", "request_count": 1},
            {"id": "t", "entity_type": "team", "entity_id": "team-7", "total_cost_usd": "2.000000", "request_count": 2},
            {"id": "a", "entity_type": "agent", "entity_id": "agent-9", "total_cost_usd": "3.000000", "request_count": 3},
            {"id": "r", "entity_type": "root_user", "entity_id": "users-id-alice", "total_cost_usd": "4.000000", "request_count": 4},
            {"id": "s", "entity_type": "service_account", "entity_id": "svc-1", "total_cost_usd": "5.000000", "request_count": 5},
            {"id": "d", "entity_type": "department", "entity_id": "dept-1", "total_cost_usd": "6.000000", "request_count": 6},
        ]
        await _seed(engine, [*others, {"id": "stale", "entity_type": STALE, "total_cost_usd": "99.000000"}])
        before = {r["id"]: r for r in await _rows(engine) if r["entity_type"] != STALE}

        await _upgrade(engine)

        after = {r["id"]: r for r in await _rows(engine) if r["entity_type"] != ORG or r["id"] != "stale"}
        for row_id in before:
            assert after[row_id] == before[row_id], f"{row_id} was modified"

    @pytest.mark.asyncio
    async def test_rows_in_other_tenants_are_not_merged_together(self, engine):
        """`org_id` is part of the conflict key — tenant isolation on the merge.

        Merging across tenants would attribute one org's spend to another's cap:
        a cross-tenant billing leak, not just a wrong number.
        """
        await _seed(
            engine,
            [
                {"id": "t1", "entity_type": STALE, "org_id": "org-1", "entity_id": "org-1", "total_cost_usd": "1.000000"},
                {"id": "t2", "entity_type": STALE, "org_id": "org-2", "entity_id": "org-2", "total_cost_usd": "2.000000"},
            ],
        )

        await _upgrade(engine)

        by_org = {r["org_id"]: r for r in await _rows(engine)}
        assert Decimal(str(by_org["org-1"]["total_cost_usd"])) == Decimal("1.000000")
        assert Decimal(str(by_org["org-2"]["total_cost_usd"])) == Decimal("2.000000")

    @pytest.mark.asyncio
    async def test_periods_are_merged_independently(self, engine):
        """`period_start`/`period_type` are part of the key — no cross-period bleed."""
        await _seed(
            engine,
            [
                {"id": "aug-stale", "entity_type": STALE, "period_start": date(2026, 8, 1), "total_cost_usd": "1.000000"},
                {"id": "jul-stale", "entity_type": STALE, "period_start": date(2026, 7, 1), "total_cost_usd": "2.000000"},
                {"id": "day-stale", "entity_type": STALE, "period_type": "daily", "total_cost_usd": "3.000000"},
            ],
        )

        await _upgrade(engine)

        rows = await _rows(engine)
        assert len(rows) == 3
        assert {Decimal(str(r["total_cost_usd"])) for r in rows} == {Decimal("1.000000"), Decimal("2.000000"), Decimal("3.000000")}


class TestReaderCanFindTheResult:
    """The point of the whole migration, asserted against the reader's own query."""

    @pytest.mark.asyncio
    async def test_surviving_rows_match_the_enforcement_query(self, engine):
        """Reproduces `_check_entity_budget`'s BudgetUsage filter verbatim.

        Pre-#4322 this SELECT returned nothing for an org that had been spending
        for months, so `current_spend` was `Decimal("0")` and the cap passed
        every request.
        """
        await _seed(engine, [{"entity_type": STALE, "total_cost_usd": "42.000000"}])

        await _upgrade(engine)

        table = BudgetUsage.__table__
        async with engine.connect() as conn:
            found = await conn.execute(
                sa.select(table.c.total_cost_usd).where(
                    sa.and_(
                        table.c.org_id == "org-acme",
                        table.c.entity_type == EntityType.ORGANIZATION.value,
                        table.c.entity_id == "org-acme",
                        table.c.period_type == "monthly",
                        table.c.period_start == PERIOD_START,
                    )
                )
            )
            spend = found.scalar_one_or_none()

        assert spend is not None, "enforcement still cannot see the org's settled spend"
        assert Decimal(str(spend)) == Decimal("42.000000")


class TestRevisionChain:
    def test_chains_onto_031(self):
        """A broken `down_revision` silently SKIPS the migration, leaving the
        mislabelled rows in place while the writer fix makes new rows correct —
        the org's history would stay invisible with nothing saying so."""
        assert MIG_032.revision == "032_budget_usage_org_type"
        assert MIG_032.down_revision == "031_usage_graph_address"

    def test_revision_ids_fit_alembic_version_column(self):
        """#4123: an id over 32 chars runs upgrade() then rolls back on Postgres.

        SQLite does not enforce VARCHAR length, so CI cannot catch this at
        runtime — only a static check can.
        """
        assert len(MIG_032.revision) <= 32
        assert len(MIG_032.down_revision) <= 32

    def test_downgrade_exists_and_is_documented_as_lossy(self):
        """Parity requires a downgrade; honesty requires it be labelled.

        The merge destroys the per-spelling split, so the revision cannot be
        faithfully reversed. An operator reading only the signature would assume
        otherwise.
        """
        assert callable(MIG_032.downgrade)
        assert "LOSSY" in (MIG_032.downgrade.__doc__ or "")

    def test_downgrade_leaves_the_merged_rows_alone(self, engine):
        """The documented no-op, exercised rather than only asserted in prose.

        Re-splitting would re-hide spend from the reader — which is the bug this
        migration fixes, so "harmless to leave" is the correct rollback.
        """
        MIG_032.downgrade()  # no `op` binding needed: the body is empty by design


class TestNoSchemaChange:
    """`entity_type` is `String(20)` with no CHECK — `"org"` fits, no DDL needed."""

    def test_org_value_fits_the_column(self):
        column = BudgetUsage.__table__.c.entity_type
        assert column.type.length == 20
        assert len(ORG) <= column.type.length

    def test_migration_issues_no_ddl(self):
        """Data-only: the module must not reach for schema operations.

        A stray `alter_column`/`add_column` here would be a schema change the
        issue explicitly rules out, and would need the DDL-safety reasoning
        migrations 028/031 document for populated tables.
        """
        source = (MIGRATIONS_DIR / "032_budget_usage_org_entity_type.py").read_text()
        body = source.split('"""', 2)[-1]  # skip the module docstring
        for ddl in ("op.add_column", "op.alter_column", "op.drop_column", "op.create_table", "op.drop_table", "op.create_index"):
            assert ddl not in body, f"032 is data-only but calls {ddl}"
