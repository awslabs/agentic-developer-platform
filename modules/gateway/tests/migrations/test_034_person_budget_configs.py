"""Tests for Alembic migration 034 — person_budget_configs.

Issue #4629 (#4620 · C3), design note
`docs/design-notes/4620-cross-org-person-budgets.md` §4.1.

This file is **mandatory**, and not only for coverage: `modules/gateway/alembic/**`
is absent from `gateway-ci.yml`'s trigger paths (`src/**`, `tests/**`, `cli/**`,
`pyproject.toml`, `Dockerfile`, frontend, `libs/`, `contracts/`), so a
migration-only change gets **zero CI signal**. A test under `tests/` is what makes
CI run at all for it. Precedent: `test_033_client_tool_capture.py`,
`test_031_usage_graph_address.py`.

These tests exercise the REAL migration functions imported from the version
module. A test that re-implements the migration proves only that the author can
write the same bug twice.

What is under test, and why each assertion is load-bearing rather than a
restatement of the DDL:

  - **There is no `org_id` column.** This is the entire point of the table and the
    one property a well-meaning future change is most likely to "fix". A cap with
    a partition is a cap that stops governing the moment the person's work
    executes in another tenant — which is #4620 itself. Asserted explicitly, not
    left to be inferred from the column list.
  - **`UNIQUE (person_anchor, period_type)`** — one cap per person per period, with
    no partition in the key. Asserted as a real constraint, so a duplicate insert
    is rejected by the database rather than by whichever writer happens to check.
  - **Migration/model parity.** Both are hand-written, so drift is the live risk:
    the migration is what runs in dev, the model is what the tests and the app
    use. A `NUMERIC(10,2)` in one and a `NUMERIC(10,6)` in the other is a silent
    rounding difference in money.
  - **`enforcement_mode` server-defaults to `soft`.** The person layer is
    informational in this unit (enforcement is C4 / #4630, gated on the §5.7
    ruling). A row that could land NULL would be neither soft nor hard and a
    reader would have to guess.
  - **Purely additive**: an existing `budget_configs` row is untouched, so the
    §8.4 rollback really is "stop reading it, then drop it".
  - **The revision chains onto the real single head** and its id fits
    `alembic_version.version_num` (#4123). A dangling or duplicated
    `down_revision` creates a SECOND HEAD, and `alembic upgrade head` then fails
    for **everyone**, blocking every subsequent gateway deploy.
"""

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from src.shared.models.budget import PersonBudgetConfig

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "alembic" / "versions"

TABLE = "person_budget_configs"
UNIQUE_CONSTRAINT = "uq_person_budget_config"
THIS_MIGRATION = "034_person_budget_configs.py"

EXPECTED_COLUMNS = {
    "id",
    "person_anchor",
    "period_type",
    "budget_amount_usd",
    "enforcement_mode",
    "authored_by_user_id",
    "created_at",
    "updated_at",
}


def _load_migration(filename: str):
    """Import a migration module by path (they are not an importable package)."""
    path = MIGRATIONS_DIR / filename
    spec = importlib.util.spec_from_file_location(filename.replace(".py", ""), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MIG_034 = _load_migration(THIS_MIGRATION)


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


# The pre-034 budget tables, as they exist on the deployed database. Present so the
# additive-ness of this migration can be asserted against a database that already
# holds an org-scoped cap — the row this table must NOT disturb. Written out rather
# than built from ORM metadata so it is the real starting state, not a restatement
# of today's models.
_PRE_034_BUDGET_CONFIGS = """
    CREATE TABLE budget_configs (
        id VARCHAR(255) NOT NULL PRIMARY KEY,
        org_id VARCHAR(255) NOT NULL,
        entity_type VARCHAR(20) NOT NULL,
        entity_id VARCHAR(255) NOT NULL,
        period_type VARCHAR(10) NOT NULL,
        budget_amount_usd NUMERIC(10, 2) NOT NULL,
        enforcement_mode VARCHAR(10) NOT NULL,
        created_at DATETIME,
        updated_at DATETIME
    )
"""


async def _engine_at_pre_034():
    """An engine holding the pre-034 budget_configs table and nothing else."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.execute(sa.text(_PRE_034_BUDGET_CONFIGS))
    return engine


async def _upgrade(engine):
    async with engine.begin() as conn:
        await conn.run_sync(_run_migration, MIG_034.upgrade)


async def _downgrade(engine):
    async with engine.begin() as conn:
        await conn.run_sync(_run_migration, MIG_034.downgrade)


def _tables(sync_conn):
    return set(sa_inspect(sync_conn).get_table_names())


def _columns(sync_conn):
    return {c["name"]: c for c in sa_inspect(sync_conn).get_columns(TABLE)}


def _unique_constraints(sync_conn):
    return sa_inspect(sync_conn).get_unique_constraints(TABLE)


async def _insert(engine, **overrides):
    """Insert one cap row, defaulting every column the caller does not name."""
    row = {
        "id": "pbc-1",
        "person_anchor": "github:1234567",
        "period_type": "monthly",
        "budget_amount_usd": "250.00",
        "authored_by_user_id": "user-self",
        **overrides,
    }
    columns = ", ".join(row)
    placeholders = ", ".join(f":{name}" for name in row)
    async with engine.begin() as conn:
        await conn.execute(sa.text(f"INSERT INTO {TABLE} ({columns}) VALUES ({placeholders})"), row)


class TestUpgradeShape:
    """The table's shape is the contract, not an implementation detail."""

    @pytest.mark.asyncio
    async def test_upgrade_creates_the_table(self):
        engine = await _engine_at_pre_034()
        try:
            async with engine.connect() as conn:
                assert TABLE not in await conn.run_sync(_tables)
            await _upgrade(engine)
            async with engine.connect() as conn:
                assert TABLE in await conn.run_sync(_tables)
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_table_has_no_org_id_column(self):
        """The absence of a partition is the feature (§4.1).

        Every other budget table is keyed `org_id`-first, which is exactly why a
        cap authored in one org caps nothing that runs in another (#4620). If a
        future change adds `org_id` here — even nullable, even "just for
        reporting" — this table stops being able to express "one ceiling on this
        person's total spend" and the bug it was created to fix returns. Asserted
        as its own test so that change fails loudly with a message that says why.
        """
        engine = await _engine_at_pre_034()
        try:
            await _upgrade(engine)
            async with engine.connect() as conn:
                columns = await conn.run_sync(_columns)
            assert "org_id" not in columns, "person_budget_configs must stay partition-free — see design note §4.1"
            assert "tenant_id" not in columns
            assert "parent_tenant_id" not in columns
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_columns_are_exactly_the_designed_set(self):
        """No extra column sneaks in, and none of the designed ones is missing.

        In particular there is no spend/accumulator column: person-level spend is
        derived by summing the existing cross-partition `root_user` rows in
        `budget_usage`. A second accumulator would be a denormalised duplicate of
        the same dollars — the #4322 double-count family.
        """
        engine = await _engine_at_pre_034()
        try:
            await _upgrade(engine)
            async with engine.connect() as conn:
                columns = await conn.run_sync(_columns)
            assert set(columns) == EXPECTED_COLUMNS
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_all_columns_are_not_null(self):
        """Every column is required: none of them has a meaningful "unknown".

        A NULL `person_anchor` would be a cap governing nobody, a NULL amount a
        cap with no number, a NULL `authored_by_user_id` an unattributable
        authority decision.
        """
        engine = await _engine_at_pre_034()
        try:
            await _upgrade(engine)
            async with engine.connect() as conn:
                columns = await conn.run_sync(_columns)
            nullable = sorted(name for name, spec in columns.items() if spec["nullable"])
            assert nullable == []
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_unique_constraint_is_person_anchor_and_period(self):
        """One cap per person per period — with no partition in the key.

        Asserted as a real database constraint so the invariant does not depend on
        whichever writer happens to check first.
        """
        engine = await _engine_at_pre_034()
        try:
            await _upgrade(engine)
            async with engine.connect() as conn:
                constraints = {c["name"]: c for c in await conn.run_sync(_unique_constraints)}
            assert UNIQUE_CONSTRAINT in constraints
            assert constraints[UNIQUE_CONSTRAINT]["column_names"] == ["person_anchor", "period_type"]
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_duplicate_person_and_period_is_rejected(self):
        """The constraint is enforced, not merely declared.

        Two rows for one person and period would mean two different ceilings on
        the same dollars, and which one governs would depend on row order.
        """
        engine = await _engine_at_pre_034()
        try:
            await _upgrade(engine)
            await _insert(engine)
            with pytest.raises(IntegrityError):
                await _insert(engine, id="pbc-2")
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_same_person_may_hold_one_cap_per_period(self):
        """daily/weekly/monthly coexist for one person — the key includes period."""
        engine = await _engine_at_pre_034()
        try:
            await _upgrade(engine)
            await _insert(engine, id="pbc-d", period_type="daily")
            await _insert(engine, id="pbc-w", period_type="weekly")
            await _insert(engine, id="pbc-m", period_type="monthly")
            async with engine.connect() as conn:
                count = (await conn.execute(sa.text(f"SELECT COUNT(*) FROM {TABLE}"))).scalar_one()
            assert count == 3
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_enforcement_mode_server_defaults_to_soft(self):
        """An insert that names no mode lands `soft`, never NULL.

        The person layer is informational in this unit — enforcement is C4
        (#4630), gated on the §5.7 ruling. A row inserted by anything other than
        the ORM (a migration, an operator's psql session) must still be
        unambiguously soft: NULL is neither soft nor hard, and a reader would have
        to guess which, with "guessed hard" meaning a surface tells a user their
        spend will be stopped when nothing stops it.
        """
        engine = await _engine_at_pre_034()
        try:
            await _upgrade(engine)
            await _insert(engine)
            async with engine.connect() as conn:
                mode = (await conn.execute(sa.text(f"SELECT enforcement_mode FROM {TABLE} WHERE id='pbc-1'"))).scalar_one()
            assert mode == "soft"
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_cap_amount_keeps_two_decimal_places(self):
        """Money round-trips at the column's precision.

        `NUMERIC(10,2)`, matching `budget_configs.budget_amount_usd` exactly. A
        cap stored at less precision than the column clients already render caps
        from would display a different number than the operator typed.
        """
        engine = await _engine_at_pre_034()
        try:
            await _upgrade(engine)
            await _insert(engine, budget_amount_usd="1234.56")
            async with engine.connect() as conn:
                amount = (await conn.execute(sa.text(f"SELECT budget_amount_usd FROM {TABLE} WHERE id='pbc-1'"))).scalar_one()
            assert str(amount) == "1234.56"
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_two_people_may_each_hold_a_cap_for_one_period(self):
        """The anchor discriminates: one person's cap never governs another's spend."""
        engine = await _engine_at_pre_034()
        try:
            await _upgrade(engine)
            await _insert(engine, id="pbc-a", person_anchor="github:111")
            await _insert(engine, id="pbc-b", person_anchor="github:222")
            async with engine.connect() as conn:
                anchors = (await conn.execute(sa.text(f"SELECT person_anchor FROM {TABLE} ORDER BY person_anchor"))).scalars().all()
            assert anchors == ["github:111", "github:222"]
        finally:
            await engine.dispose()


class TestModelParity:
    """The hand-written migration and the hand-written model must agree.

    They are maintained separately: the migration is what runs against dev and
    prod, the model is what the app and every other test use. Drift means the
    tests pass against a schema the database does not have.
    """

    def test_model_declares_the_same_table_name(self):
        assert PersonBudgetConfig.__tablename__ == TABLE

    def test_model_declares_the_same_columns(self):
        assert {c.name for c in PersonBudgetConfig.__table__.columns} == EXPECTED_COLUMNS

    def test_model_has_no_partition_column(self):
        """The model must not acquire `org_id` either — including via a mixin.

        `TenantMixin` injects `org_id`, so a future "make it consistent with the
        other budget models" change would be caught here as well as in the DDL.
        """
        names = {c.name for c in PersonBudgetConfig.__table__.columns}
        assert "org_id" not in names
        assert "tenant_id" not in names

    def test_model_declares_the_same_unique_constraint(self):
        constraints = {
            constraint.name: [c.name for c in constraint.columns]
            for constraint in PersonBudgetConfig.__table__.constraints
            if isinstance(constraint, sa.UniqueConstraint)
        }
        assert constraints == {UNIQUE_CONSTRAINT: ["person_anchor", "period_type"]}

    def test_model_cap_precision_matches_the_migration(self):
        """NUMERIC(10,2) in both places — a precision mismatch is a money bug."""
        amount = PersonBudgetConfig.__table__.c.budget_amount_usd
        assert isinstance(amount.type, sa.Numeric)
        assert (amount.type.precision, amount.type.scale) == (10, 2)

    @pytest.mark.asyncio
    async def test_migrated_table_accepts_a_row_written_through_the_model(self):
        """The strongest parity check: the ORM writes into the migrated table.

        Proves the migration produces the table the application actually uses, not
        merely one with matching column names.
        """
        engine = await _engine_at_pre_034()
        try:
            await _upgrade(engine)
            from decimal import Decimal

            from sqlalchemy.ext.asyncio import async_sessionmaker

            session_factory = async_sessionmaker(engine, expire_on_commit=False)
            async with session_factory() as session:
                session.add(
                    PersonBudgetConfig(
                        id="pbc-orm",
                        person_anchor="github:99",
                        period_type="monthly",
                        budget_amount_usd=Decimal("42.50"),
                        enforcement_mode="soft",
                        authored_by_user_id="user-self",
                    )
                )
                await session.commit()

                stored = await session.scalar(sa.select(PersonBudgetConfig).where(PersonBudgetConfig.id == "pbc-orm"))
            assert stored is not None
            assert stored.person_anchor == "github:99"
            assert Decimal(stored.budget_amount_usd) == Decimal("42.50")
            assert stored.enforcement_mode == "soft"
        finally:
            await engine.dispose()


class TestAdditive:
    """Nothing that exists today changes meaning (§8.1, §8.4).

    Existing single-org `root_user` caps are deliberately left in place: moving a
    cap silently changes what stops a workload.
    """

    _EXISTING_CAP = {
        "id": "bc-1",
        "org_id": "org-a",
        "entity_type": "root_user",
        "entity_id": "user-1",
        "period_type": "monthly",
        "budget_amount_usd": "500.00",
        "enforcement_mode": "hard",
    }

    async def _seed(self, engine):
        async with engine.begin() as conn:
            await conn.execute(
                sa.text(
                    "INSERT INTO budget_configs (id, org_id, entity_type, entity_id, period_type, "
                    "budget_amount_usd, enforcement_mode) VALUES "
                    "(:id, :org_id, :entity_type, :entity_id, :period_type, :budget_amount_usd, :enforcement_mode)"
                ),
                self._EXISTING_CAP,
            )

    @pytest.mark.asyncio
    async def test_existing_org_scoped_cap_is_untouched(self):
        """An org-scoped `root_user` cap survives byte-identical.

        This is what makes the rollback trivial and what keeps enforcement
        behaviour unchanged by this unit: the migration adds a table, it does not
        migrate anybody's cap into it.
        """
        engine = await _engine_at_pre_034()
        try:
            await self._seed(engine)
            columns = "id, org_id, entity_type, entity_id, period_type, budget_amount_usd, enforcement_mode"
            async with engine.connect() as conn:
                before = (await conn.execute(sa.text(f"SELECT {columns} FROM budget_configs ORDER BY id"))).all()

            await _upgrade(engine)

            async with engine.connect() as conn:
                after = (await conn.execute(sa.text(f"SELECT {columns} FROM budget_configs ORDER BY id"))).all()
            assert before == after
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_new_table_starts_empty(self):
        """No backfill: nobody acquires a person-level cap by deploying this.

        A backfilled cap would be a limit the person never authored, applying
        across orgs, in a unit that has no enforcement to make it visible first.
        """
        engine = await _engine_at_pre_034()
        try:
            await self._seed(engine)
            await _upgrade(engine)
            async with engine.connect() as conn:
                count = (await conn.execute(sa.text(f"SELECT COUNT(*) FROM {TABLE}"))).scalar_one()
            assert count == 0
        finally:
            await engine.dispose()


class TestDowngrade:
    """downgrade() is exercised, not assumed.

    The operational rollback is still "revert the PR" (§8.4). These tests prove
    the function is correct if it is ever run, not that anyone should run it.
    """

    @pytest.mark.asyncio
    async def test_downgrade_drops_the_table(self):
        engine = await _engine_at_pre_034()
        try:
            await _upgrade(engine)
            await _downgrade(engine)
            async with engine.connect() as conn:
                assert TABLE not in await conn.run_sync(_tables)
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_downgrade_preserves_preexisting_budget_rows(self):
        """Rolling back cannot lose data that predates the migration.

        Safe by construction here — nothing was backfilled and no existing row was
        rewritten — which is exactly the claim being verified.
        """
        engine = await _engine_at_pre_034()
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    sa.text(
                        "INSERT INTO budget_configs (id, org_id, entity_type, entity_id, period_type, "
                        "budget_amount_usd, enforcement_mode) VALUES "
                        "('bc-1', 'org-a', 'root_user', 'user-1', 'monthly', 500.00, 'hard')"
                    )
                )
            await _upgrade(engine)
            await _downgrade(engine)
            async with engine.connect() as conn:
                amount = (await conn.execute(sa.text("SELECT budget_amount_usd FROM budget_configs WHERE id='bc-1'"))).scalar_one()
            assert float(amount) == 500.00
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_upgrade_is_reapplicable_after_downgrade(self):
        """upgrade → downgrade → upgrade, proving the pair is a real inverse.

        A downgrade that left the table (or its unique index) behind would make
        re-upgrade fail on a duplicate name.
        """
        engine = await _engine_at_pre_034()
        try:
            await _upgrade(engine)
            await _downgrade(engine)
            await _upgrade(engine)
            async with engine.connect() as conn:
                assert TABLE in await conn.run_sync(_tables)
        finally:
            await engine.dispose()


class TestRevisionChain:
    """Second-head prevention.

    A broken `down_revision` silently SKIPS the migration and live code then
    queries a table that does not exist; a duplicate one creates a second head and
    `alembic upgrade head` fails for everyone, blocking all gateway deploys.
    """

    def test_revision_id(self):
        assert MIG_034.revision == "034_person_budget_configs"

    def test_chains_onto_the_real_single_head(self):
        """The head was resolved at implementation time, not assumed from the number.

        Unrelated merges take numbers in between, so "the one after 033" is not a
        chain — the parent is named by its revision **id**.
        """
        assert MIG_034.down_revision == "033_client_tool_capture"

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
        assert MIG_034.down_revision in known

    def test_revision_id_is_unique(self):
        """Two version files claiming one revision id is an ambiguous chain."""
        duplicates = [
            path.name
            for path in MIGRATIONS_DIR.glob("*.py")
            if path.name != THIS_MIGRATION and not path.name.startswith("__") and _load_migration(path.name).revision == MIG_034.revision
        ]
        assert duplicates == []

    def test_revision_ids_fit_alembic_version_column(self):
        """#4123: an id over 32 chars runs upgrade() then rolls back on Postgres.

        SQLite does not enforce VARCHAR length, so CI cannot catch this at
        runtime — only a static check can. Called out explicitly in this issue's
        acceptance criteria.
        """
        assert len(MIG_034.revision) <= 32
        assert len(MIG_034.down_revision) <= 32

    def test_exactly_one_head_across_all_version_files(self):
        """`alembic heads` must report a SINGLE head.

        Computed structurally rather than trusted: a head is a revision no other
        revision names as its parent. This is the executable form of "confirm a
        single head before opening the PR", so a concurrently-merged sibling
        migration fails CI here instead of breaking `alembic upgrade head` in dev.

        Asserts the *count* and that 034 is still ON the chain — deliberately NOT
        that 034 IS the head, following
        `test_029_orchestration_graph.test_migration_leaves_exactly_one_head`. The
        head advances with every migration that lands, and a name-pinned assertion
        turns the next one into a spurious failure here, which trains people to
        edit this test rather than read it. (033's copy of this test WAS pinned by
        name, and this migration is what made it fail; it was corrected in the same
        change rather than worked around.)
        """
        revisions: set[str] = set()
        parents: set[str] = set()
        for path in MIGRATIONS_DIR.glob("*.py"):
            if path.name.startswith("__"):
                continue
            module = _load_migration(path.name)
            revisions.add(module.revision)
            if module.down_revision:
                parents.update((module.down_revision,) if isinstance(module.down_revision, str) else module.down_revision)

        heads = revisions - parents
        assert len(heads) == 1, f"expected exactly one head, found: {sorted(heads)}"
        assert MIG_034.revision in revisions

        # A real reachability walk, not the tautology this used to be (review fix:
        # given one head and 034 ∈ revisions, "in parents or is the head" can never
        # be false — 033's copy has the same dead guard). Orphaned here means: not
        # on the down_revision path from the head back to the root.
        down_of = {}
        for path in MIGRATIONS_DIR.glob("*.py"):
            if path.name.startswith("__"):
                continue
            module = _load_migration(path.name)
            down_of[module.revision] = (module.down_revision,) if isinstance(module.down_revision, str) else (module.down_revision or ())
        chain = set()
        pending = list(heads)
        while pending:
            cursor = pending.pop()
            if cursor in chain:
                continue
            chain.add(cursor)
            pending.extend(down_of.get(cursor, ()))
        assert MIG_034.revision in chain, "034 has been orphaned off the head's down_revision chain"
