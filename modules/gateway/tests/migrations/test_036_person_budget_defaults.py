"""Tests for Alembic migration 036 — person_budget_defaults.

Issue #4690 (person-limits · D1).

This file is **mandatory**, and not only for coverage: `modules/gateway/alembic/**`
is absent from `gateway-ci.yml`'s trigger paths (`src/**`, `tests/**`, `cli/**`,
`pyproject.toml`, `Dockerfile`, frontend, `libs/`, `contracts/`), so a
migration-only change gets **zero CI signal**. A test under `tests/` is what makes
CI run at all for it. Precedent: `test_034_person_budget_configs.py`.

These tests exercise the REAL migration functions imported from the version module.
A test that re-implements the migration proves only that the author can write the
same bug twice.

What is under test, and why each assertion is load-bearing rather than a
restatement of the DDL:

  - **Uniqueness survives NULL scope columns.** The single most important test in
    this file (`TestUniquenessAcrossNulls`). The issue sketched
    `UNIQUE (scope_type, scope_id_org, scope_id_team, period_type)`, but in
    Postgres NULLs compare *distinct* inside a unique constraint — so that shape
    accepts TWO platform defaults for the same period, the rung then holds two
    conflicting numbers, and which one governs depends on row order. The migration
    uses a unique expression index over `COALESCE(col, '')` instead. Asserted by
    inserting the duplicate and requiring the database to refuse it, because this
    property cannot be read off a column list.
  - **`ck_person_budget_default_scope` is enforced, not merely declared.** Each rung
    has exactly one legal column shape. Without the CHECK, an `org` row with a NULL
    `scope_id_org` is a rule matching every tenant through a NULL comparison nobody
    wrote.
  - **There is no `org_id` column and no `TenantMixin`.** Same property 034 pins,
    for a stronger reason: the platform rung has no tenant at all. `scope_id_org` is
    a scope the row *declares*, not a partition it *lives in*.
  - **Migration/model parity**, including the CHECK's text and the index's
    expressions. Both are hand-written; a `NUMERIC(10,2)` in one and `(10,6)` in the
    other is a silent rounding difference in money, and a CHECK present in only one
    means the tests pass against a schema the database does not have.
  - **`enforcement_mode` server-defaults to `hard`** — the opposite of 034's `soft`,
    deliberately. A governance default that silently does not enforce is the #4511
    inert-cap class at platform scale.
  - **Purely additive**: existing `budget_configs` and `person_budget_configs` rows
    are untouched, which is what makes the rollback "stop reading it, then drop it".
  - **The revision chains onto the real single head** and its id fits
    `alembic_version.version_num` (#4123). A dangling or duplicated `down_revision`
    creates a SECOND HEAD, and `alembic upgrade head` then fails for **everyone**,
    blocking every subsequent gateway deploy.

SQLite backs these tests, and it is a *fair* substrate for the uniqueness question
specifically: SQLite also treats NULLs as distinct in a UNIQUE constraint, so the
`COALESCE` index is doing real work here rather than passing by accident on a more
forgiving engine.
"""

import importlib.util
from decimal import Decimal
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.shared.models.budget import PersonBudgetDefault

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "alembic" / "versions"

TABLE = "person_budget_defaults"
UNIQUE_INDEX = "uq_person_budget_default"
SCOPE_CHECK = "ck_person_budget_default_scope"
THIS_MIGRATION = "036_person_budget_defaults.py"

EXPECTED_COLUMNS = {
    "id",
    "scope_type",
    "scope_id_org",
    "scope_id_team",
    "period_type",
    "budget_amount_usd",
    "enforcement_mode",
    "authored_by_user_id",
    "created_at",
    "updated_at",
}

# Only the two scope columns are nullable, and only because the platform rung has no
# tenant. Everything else has no meaningful "unknown": a NULL amount is a rule with
# no number, a NULL `authored_by_user_id` an unattributable governance decision.
EXPECTED_NULLABLE = ["scope_id_org", "scope_id_team"]


def _load_migration(filename: str):
    """Import a migration module by path (they are not an importable package)."""
    path = MIGRATIONS_DIR / filename
    spec = importlib.util.spec_from_file_location(filename.replace(".py", ""), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MIG_036 = _load_migration(THIS_MIGRATION)


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


# The pre-036 person-limit state: an org-scoped cap and one person's individual
# limit. Both must survive untouched, which is the additive-ness claim. Written out
# rather than built from ORM metadata so it is the real starting state, not a
# restatement of today's models.
_PRE_036_TABLES = (
    """
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
    """,
    """
    CREATE TABLE person_budget_configs (
        id VARCHAR(255) NOT NULL PRIMARY KEY,
        person_anchor VARCHAR(255) NOT NULL,
        period_type VARCHAR(10) NOT NULL,
        budget_amount_usd NUMERIC(10, 2) NOT NULL,
        enforcement_mode VARCHAR(10) NOT NULL DEFAULT 'soft',
        authored_by_user_id VARCHAR(255) NOT NULL,
        created_at DATETIME,
        updated_at DATETIME,
        CONSTRAINT uq_person_budget_config UNIQUE (person_anchor, period_type)
    )
    """,
)


async def _engine_at_pre_036():
    """An engine holding the pre-036 budget tables and nothing else."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        for ddl in _PRE_036_TABLES:
            await conn.execute(sa.text(ddl))
        # SQLite ignores CHECK constraints unless foreign_keys/legacy pragmas are on,
        # but it DOES enforce CHECK by default — this is here for the scope tests to
        # be meaningful, asserted by TestScopeCheckConstraint itself rather than
        # assumed.
        await conn.execute(sa.text("PRAGMA ignore_check_constraints = OFF"))
    return engine


async def _upgrade(engine):
    async with engine.begin() as conn:
        await conn.run_sync(_run_migration, MIG_036.upgrade)


async def _downgrade(engine):
    async with engine.begin() as conn:
        await conn.run_sync(_run_migration, MIG_036.downgrade)


def _tables(sync_conn):
    return set(sa_inspect(sync_conn).get_table_names())


def _columns(sync_conn):
    return {c["name"]: c for c in sa_inspect(sync_conn).get_columns(TABLE)}


def _index_sql(sync_conn) -> dict[str, str]:
    """Index name → its `CREATE INDEX` text, read from `sqlite_master`.

    Deliberately NOT `Inspector.get_indexes`: that reflector *silently skips*
    expression-based indexes ("SAWarning: Skipped unsupported reflection of
    expression-based index"), so asking it about `uq_person_budget_default` returns an
    empty dict and any assertion built on it fails whether the index is present or
    absent. Reading the stored DDL is both correct here and stricter — it lets the
    assertions below check the COALESCE expressions actually reached the database,
    not just that something with the right name exists.
    """
    rows = sync_conn.execute(
        sa.text("SELECT name, sql FROM sqlite_master WHERE type = 'index' AND tbl_name = :table"),
        {"table": TABLE},
    ).all()
    return {name: (sql or "") for name, sql in rows}


async def _insert(engine, **overrides):
    """Insert one default row, defaulting every column the caller does not name.

    Defaults to a PLATFORM row, because that is the rung whose uniqueness the
    NULL-distinctness defect hides in.
    """
    row = {
        "id": "pbd-1",
        "scope_type": "platform",
        "scope_id_org": None,
        "scope_id_team": None,
        "period_type": "monthly",
        "budget_amount_usd": "1000.00",
        "authored_by_user_id": "user-admin",
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
        engine = await _engine_at_pre_036()
        try:
            async with engine.connect() as conn:
                assert TABLE not in await conn.run_sync(_tables)
            await _upgrade(engine)
            async with engine.connect() as conn:
                assert TABLE in await conn.run_sync(_tables)
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_columns_are_exactly_the_designed_set(self):
        """No extra column sneaks in, and none of the designed ones is missing.

        In particular there is no member count and no spend column: how many people a
        rule currently governs, and what they have spent, is derived from the existing
        cross-org figures. A second copy here is the #4322 double-count family.
        """
        engine = await _engine_at_pre_036()
        try:
            await _upgrade(engine)
            async with engine.connect() as conn:
                columns = await conn.run_sync(_columns)
            assert set(columns) == EXPECTED_COLUMNS
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_table_has_no_partition_column(self):
        """No `org_id`, no `TenantMixin` — for a stronger reason than 034's.

        034 is partition-free because a person's runs execute in whichever tenant the
        work is in. THIS table additionally has a rung with no tenant at all: a
        platform default belongs to no organization. `scope_id_org` is a scope the row
        *declares*, which is not the same thing as a partition it *lives in* — a
        distinction a future "make it consistent with the other budget tables" change
        would erase, taking the platform rung with it.
        """
        engine = await _engine_at_pre_036()
        try:
            await _upgrade(engine)
            async with engine.connect() as conn:
                columns = await conn.run_sync(_columns)
            assert "org_id" not in columns, "person_budget_defaults must stay partition-free — the platform rung has no tenant"
            assert "tenant_id" not in columns
            assert "parent_tenant_id" not in columns
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_only_the_scope_columns_are_nullable(self):
        """The two scope columns are nullable; nothing else is.

        A NULL amount would be a rule with no number, a NULL `period_type` a rule
        governing no window, a NULL `authored_by_user_id` an unattributable decision
        about a whole population.
        """
        engine = await _engine_at_pre_036()
        try:
            await _upgrade(engine)
            async with engine.connect() as conn:
                columns = await conn.run_sync(_columns)
            assert sorted(name for name, spec in columns.items() if spec["nullable"]) == EXPECTED_NULLABLE
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_enforcement_mode_server_defaults_to_hard(self):
        """An insert naming no mode lands `hard`, never NULL and never `soft`.

        Deliberately the opposite of 034's `soft` default. 034 defaulted soft because
        C3's shipped UI had told those users in as many words that requests would not
        be blocked; there is no equivalent pre-enforcement generation of THESE rows to
        keep a promise to. A default authored to bound everybody that silently does not
        enforce is the #4511 inert-cap class at platform scale — the operator believes
        a population is bounded and nothing stops anyone.
        """
        engine = await _engine_at_pre_036()
        try:
            await _upgrade(engine)
            await _insert(engine)
            async with engine.connect() as conn:
                mode = (await conn.execute(sa.text(f"SELECT enforcement_mode FROM {TABLE} WHERE id='pbd-1'"))).scalar_one()
            assert mode == "hard"
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_amount_keeps_two_decimal_places(self):
        """Money round-trips at the column's precision.

        `NUMERIC(10,2)`, byte-for-byte `person_budget_configs.budget_amount_usd`. The
        ladder compares the two columns and reports whichever applies, so a precision
        difference between them would be a silent rounding difference between "your
        limit" and "the default you are held to".
        """
        engine = await _engine_at_pre_036()
        try:
            await _upgrade(engine)
            await _insert(engine, budget_amount_usd="1234.56")
            async with engine.connect() as conn:
                amount = (await conn.execute(sa.text(f"SELECT budget_amount_usd FROM {TABLE} WHERE id='pbd-1'"))).scalar_one()
            assert str(amount) == "1234.56"
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_the_unique_index_exists_and_coalesces_both_scope_columns(self):
        """The migration's index reaches the database as a UNIQUE, COALESCE'd index.

        All three properties are checked against the stored DDL because all three are
        load-bearing: drop UNIQUE and it enforces nothing, drop either COALESCE and
        that rung's NULL column goes back to comparing distinct.
        """
        engine = await _engine_at_pre_036()
        try:
            await _upgrade(engine)
            async with engine.connect() as conn:
                indexes = await conn.run_sync(_index_sql)
            assert UNIQUE_INDEX in indexes, f"{UNIQUE_INDEX} is what enforces one rule per (scope, period)"
            ddl = indexes[UNIQUE_INDEX].lower()
            assert "unique" in ddl
            assert "coalesce(scope_id_org, '')" in ddl
            assert "coalesce(scope_id_team, '')" in ddl
        finally:
            await engine.dispose()


class TestUniquenessAcrossNulls:
    """One rule per (scope, period) — INCLUDING on the rungs with NULL columns.

    The most important class in this file. The issue sketched
    `UNIQUE (scope_type, scope_id_org, scope_id_team, period_type)`, which is wrong
    in a way no column list reveals: SQL treats NULLs as DISTINCT inside a unique
    constraint, so two platform defaults for the same period both satisfy it. The
    rung then holds two conflicting numbers and which one governs depends on row
    order — an operator lowers the ceiling, the ladder keeps reading the old row, and
    nothing anywhere reports a conflict.

    The migration indexes `COALESCE(col, '')` instead. These tests insert the
    duplicates and require the DATABASE to refuse them, which is the only way to
    verify the property.
    """

    @pytest.mark.asyncio
    async def test_two_platform_defaults_for_one_period_are_rejected(self):
        """The NULL-distinctness case, head-on: both scope columns are NULL."""
        engine = await _engine_at_pre_036()
        try:
            await _upgrade(engine)
            await _insert(engine, id="pbd-a", budget_amount_usd="1000.00")
            with pytest.raises(IntegrityError):
                await _insert(engine, id="pbd-b", budget_amount_usd="9999.00")
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_two_org_defaults_for_one_org_and_period_are_rejected(self):
        """The org rung has ONE null column — the half-NULL case still collides."""
        engine = await _engine_at_pre_036()
        try:
            await _upgrade(engine)
            await _insert(engine, id="pbd-a", scope_type="org", scope_id_org="org-acme")
            with pytest.raises(IntegrityError):
                await _insert(engine, id="pbd-b", scope_type="org", scope_id_org="org-acme", budget_amount_usd="50.00")
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_two_team_defaults_for_one_team_and_period_are_rejected(self):
        engine = await _engine_at_pre_036()
        try:
            await _upgrade(engine)
            await _insert(engine, id="pbd-a", scope_type="team", scope_id_org="org-acme", scope_id_team="team-1")
            with pytest.raises(IntegrityError):
                await _insert(engine, id="pbd-b", scope_type="team", scope_id_org="org-acme", scope_id_team="team-1", budget_amount_usd="50.00")
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_platform_may_hold_one_default_per_period(self):
        """daily/weekly/monthly coexist at one scope — `period_type` is in the key.

        The mirror image of the tests above: the index must be tight enough to reject
        a duplicate and loose enough to allow the three calendar periods, or an
        operator setting a daily ceiling would silently destroy their monthly one.
        """
        engine = await _engine_at_pre_036()
        try:
            await _upgrade(engine)
            await _insert(engine, id="pbd-d", period_type="daily")
            await _insert(engine, id="pbd-w", period_type="weekly")
            await _insert(engine, id="pbd-m", period_type="monthly")
            async with engine.connect() as conn:
                count = (await conn.execute(sa.text(f"SELECT COUNT(*) FROM {TABLE}"))).scalar_one()
            assert count == 3
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_the_three_rungs_coexist_for_one_period(self):
        """A platform, an org and a team rule for the same period are all legal.

        They are not duplicates — they are the ladder. Rejecting them would make the
        fallback impossible to express.
        """
        engine = await _engine_at_pre_036()
        try:
            await _upgrade(engine)
            await _insert(engine, id="pbd-p")
            await _insert(engine, id="pbd-o", scope_type="org", scope_id_org="org-acme")
            await _insert(engine, id="pbd-t", scope_type="team", scope_id_org="org-acme", scope_id_team="team-1")
            async with engine.connect() as conn:
                count = (await conn.execute(sa.text(f"SELECT COUNT(*) FROM {TABLE}"))).scalar_one()
            assert count == 3
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_same_team_id_in_two_orgs_are_distinct_rules(self):
        """`teams.id` is unique only inside its org, so both halves are in the key.

        `teams` carries `TenantMixin`. Two tenants can legitimately hold a team with
        the same id, and each may set its own default — so this must NOT collide. The
        matching side of the same property (a rule never governing a same-id team in
        another tenant) is pinned in the ladder's own tests.
        """
        engine = await _engine_at_pre_036()
        try:
            await _upgrade(engine)
            await _insert(engine, id="pbd-a", scope_type="team", scope_id_org="org-acme", scope_id_team="team-eng")
            await _insert(engine, id="pbd-b", scope_type="team", scope_id_org="org-globex", scope_id_team="team-eng")
            async with engine.connect() as conn:
                count = (await conn.execute(sa.text(f"SELECT COUNT(*) FROM {TABLE}"))).scalar_one()
            assert count == 2
        finally:
            await engine.dispose()


class TestScopeCheckConstraint:
    """A row must describe the rung it claims — `ck_person_budget_default_scope`.

    Without this, an `org` row with a NULL `scope_id_org` is a rule matching every
    tenant's members through a NULL comparison nobody wrote, and a `platform` row
    carrying a stray org id reads as tenant-scoped to a human and platform-wide to
    the ladder. Three legal shapes, everything else refused by the database.
    """

    @pytest.mark.asyncio
    async def test_the_three_legal_shapes_are_accepted(self):
        engine = await _engine_at_pre_036()
        try:
            await _upgrade(engine)
            await _insert(engine, id="pbd-p")
            await _insert(engine, id="pbd-o", scope_type="org", scope_id_org="org-acme")
            await _insert(engine, id="pbd-t", scope_type="team", scope_id_org="org-acme", scope_id_team="team-1")
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_platform_row_carrying_an_org_id_is_rejected(self):
        """A platform rule scoped to an org is two contradictory statements."""
        engine = await _engine_at_pre_036()
        try:
            await _upgrade(engine)
            with pytest.raises(IntegrityError):
                await _insert(engine, scope_type="platform", scope_id_org="org-acme")
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_org_row_without_an_org_id_is_rejected(self):
        """The most dangerous malformed row: it would match through a NULL."""
        engine = await _engine_at_pre_036()
        try:
            await _upgrade(engine)
            with pytest.raises(IntegrityError):
                await _insert(engine, scope_type="org", scope_id_org=None)
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_org_row_carrying_a_team_id_is_rejected(self):
        engine = await _engine_at_pre_036()
        try:
            await _upgrade(engine)
            with pytest.raises(IntegrityError):
                await _insert(engine, scope_type="org", scope_id_org="org-acme", scope_id_team="team-1")
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_team_row_without_its_org_id_is_rejected(self):
        """A team rule naming only the team could govern a same-id team elsewhere."""
        engine = await _engine_at_pre_036()
        try:
            await _upgrade(engine)
            with pytest.raises(IntegrityError):
                await _insert(engine, scope_type="team", scope_id_org=None, scope_id_team="team-1")
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_team_row_without_its_team_id_is_rejected(self):
        engine = await _engine_at_pre_036()
        try:
            await _upgrade(engine)
            with pytest.raises(IntegrityError):
                await _insert(engine, scope_type="team", scope_id_org="org-acme", scope_id_team=None)
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_an_unknown_scope_type_is_rejected(self):
        """`department` is a NON-GOAL of #4690, and the CHECK is what says so.

        Adding the rung means a migration that widens this constraint AND a rung in
        `_DEFAULT_RUNG_ORDER` — the two changes that must land together. A stored row
        naming a rung the ladder does not walk is a rule an operator authored, sees on
        their screen, and which governs nobody: the #4511 inert-cap class.
        """
        engine = await _engine_at_pre_036()
        try:
            await _upgrade(engine)
            with pytest.raises(IntegrityError):
                await _insert(engine, scope_type="department", scope_id_org="org-acme")
        finally:
            await engine.dispose()


class TestModelParity:
    """The hand-written migration and the hand-written model must agree.

    They are maintained separately: the migration is what runs against dev and prod,
    the model is what the app and every other test use. Drift means the tests pass
    against a schema the database does not have.
    """

    def test_model_declares_the_same_table_name(self):
        assert PersonBudgetDefault.__tablename__ == TABLE

    def test_model_declares_the_same_columns(self):
        assert {c.name for c in PersonBudgetDefault.__table__.columns} == EXPECTED_COLUMNS

    def test_model_has_no_partition_column(self):
        """The model must not acquire `org_id` either — including via `TenantMixin`."""
        names = {c.name for c in PersonBudgetDefault.__table__.columns}
        assert "org_id" not in names
        assert "tenant_id" not in names

    def test_model_nullability_matches_the_migration(self):
        nullable = sorted(c.name for c in PersonBudgetDefault.__table__.columns if c.nullable)
        assert nullable == EXPECTED_NULLABLE

    def test_model_amount_precision_matches_the_migration(self):
        """NUMERIC(10,2) in both places — a precision mismatch is a money bug."""
        amount = PersonBudgetDefault.__table__.c.budget_amount_usd
        assert isinstance(amount.type, sa.Numeric)
        assert (amount.type.precision, amount.type.scale) == (10, 2)

    def test_model_amount_precision_matches_the_individual_cap_column(self):
        """And it matches `person_budget_configs` too.

        The ladder compares an individual row against a default and reports whichever
        applies, so these two columns holding different precision would be a silent
        rounding difference between "your limit" and "the default you are held to".
        """
        from src.shared.models.budget import PersonBudgetConfig

        default_amount = PersonBudgetDefault.__table__.c.budget_amount_usd.type
        individual_amount = PersonBudgetConfig.__table__.c.budget_amount_usd.type
        assert (default_amount.precision, default_amount.scale) == (individual_amount.precision, individual_amount.scale)

    def test_model_declares_the_scope_check_constraint(self):
        """The CHECK exists on the model, so ORM-created schemas carry it too.

        Tests that build their schema from `Base.metadata.create_all` (most of the
        budget suite) would otherwise run against a table that accepts malformed
        scopes while dev and prod reject them — the drift direction that makes a
        green suite meaningless.
        """
        checks = {c.name for c in PersonBudgetDefault.__table__.constraints if isinstance(c, sa.CheckConstraint)}
        assert SCOPE_CHECK in checks

    def test_model_declares_the_same_unique_index(self):
        """The uniqueness key is an INDEX on the model, not a `UniqueConstraint`.

        Asserted as a shape, not just a name: a future change "simplifying" it into a
        `UniqueConstraint` would compile, pass a name check, and silently reintroduce
        the two-platform-defaults defect this file's central class exists to catch.
        """
        indexes = {index.name: index for index in PersonBudgetDefault.__table__.indexes}
        assert UNIQUE_INDEX in indexes
        assert indexes[UNIQUE_INDEX].unique

        unique_constraints = {c.name for c in PersonBudgetDefault.__table__.constraints if isinstance(c, sa.UniqueConstraint)}
        assert UNIQUE_INDEX not in unique_constraints
        assert unique_constraints == set(), "uniqueness here must be the COALESCE expression index — a plain UNIQUE allows two platform defaults"

    def test_model_unique_index_coalesces_both_nullable_columns(self):
        """The index's expressions must coalesce BOTH nullable scope columns.

        Coalescing only `scope_id_org` would fix the platform rung and leave the org
        rung (one NULL column) still able to hold two conflicting rules.
        """
        index = next(index for index in PersonBudgetDefault.__table__.indexes if index.name == UNIQUE_INDEX)
        rendered = " ".join(str(expr) for expr in index.expressions).lower()
        assert "coalesce(scope_id_org, '')" in rendered
        assert "coalesce(scope_id_team, '')" in rendered
        assert "scope_type" in rendered
        assert "period_type" in rendered

    @pytest.mark.asyncio
    async def test_migrated_table_accepts_a_row_written_through_the_model(self):
        """The strongest parity check: the ORM writes into the MIGRATED table.

        Proves the migration produces the table the application actually uses, not
        merely one with matching column names.
        """
        engine = await _engine_at_pre_036()
        try:
            await _upgrade(engine)
            session_factory = async_sessionmaker(engine, expire_on_commit=False)
            async with session_factory() as session:
                session.add(
                    PersonBudgetDefault(
                        id="pbd-orm",
                        scope_type="org",
                        scope_id_org="org-acme",
                        scope_id_team=None,
                        period_type="monthly",
                        budget_amount_usd=Decimal("1000.00"),
                        enforcement_mode="hard",
                        authored_by_user_id="user-admin",
                    )
                )
                await session.commit()

                stored = await session.scalar(sa.select(PersonBudgetDefault).where(PersonBudgetDefault.id == "pbd-orm"))
            assert stored is not None
            assert stored.scope_type == "org"
            assert stored.scope_id_org == "org-acme"
            assert stored.scope_id_team is None
            assert Decimal(stored.budget_amount_usd) == Decimal("1000.00")
            assert stored.enforcement_mode == "hard"
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_the_orm_cannot_write_a_second_platform_default(self):
        """The index binds ORM writes too, not only raw SQL.

        The routes upsert through the ORM, so this is the path a racing double-PUT
        actually takes — and `_upsert_default`'s IntegrityError retry depends on the
        database raising here rather than accepting the duplicate.
        """
        engine = await _engine_at_pre_036()
        try:
            await _upgrade(engine)
            session_factory = async_sessionmaker(engine, expire_on_commit=False)

            def _row(row_id: str, amount: str) -> PersonBudgetDefault:
                return PersonBudgetDefault(
                    id=row_id,
                    scope_type="platform",
                    scope_id_org=None,
                    scope_id_team=None,
                    period_type="monthly",
                    budget_amount_usd=Decimal(amount),
                    enforcement_mode="hard",
                    authored_by_user_id="user-admin",
                )

            async with session_factory() as session:
                session.add(_row("pbd-1", "1000.00"))
                await session.commit()

            async with session_factory() as session:
                session.add(_row("pbd-2", "9999.00"))
                with pytest.raises(IntegrityError):
                    await session.commit()
        finally:
            await engine.dispose()


class TestAdditive:
    """Nothing that exists today changes meaning.

    An install with no rows in this table behaves exactly as it does today, which is
    what makes the rollback "stop reading it, then drop it".
    """

    _EXISTING_ORG_CAP = {
        "id": "bc-1",
        "org_id": "org-a",
        "entity_type": "root_user",
        "entity_id": "user-1",
        "period_type": "monthly",
        "budget_amount_usd": "500.00",
        "enforcement_mode": "hard",
    }

    _EXISTING_PERSON_CAP = {
        "id": "pbc-1",
        "person_anchor": "github:1234567",
        "period_type": "monthly",
        "budget_amount_usd": "250.00",
        "enforcement_mode": "hard",
        "authored_by_user_id": "user-self",
    }

    async def _seed(self, engine):
        async with engine.begin() as conn:
            await conn.execute(
                sa.text(
                    "INSERT INTO budget_configs (id, org_id, entity_type, entity_id, period_type, "
                    "budget_amount_usd, enforcement_mode) VALUES "
                    "(:id, :org_id, :entity_type, :entity_id, :period_type, :budget_amount_usd, :enforcement_mode)"
                ),
                self._EXISTING_ORG_CAP,
            )
            await conn.execute(
                sa.text(
                    "INSERT INTO person_budget_configs (id, person_anchor, period_type, budget_amount_usd, "
                    "enforcement_mode, authored_by_user_id) VALUES "
                    "(:id, :person_anchor, :period_type, :budget_amount_usd, :enforcement_mode, :authored_by_user_id)"
                ),
                self._EXISTING_PERSON_CAP,
            )

    @pytest.mark.asyncio
    async def test_existing_individual_person_cap_is_untouched(self):
        """A person's own limit survives byte-identical.

        The migration adds a rung BELOW the individual row; it does not rewrite,
        move, or reinterpret anybody's existing limit. If it did, somebody's ceiling
        would change on deploy without them authoring anything.
        """
        engine = await _engine_at_pre_036()
        try:
            await self._seed(engine)
            columns = "id, person_anchor, period_type, budget_amount_usd, enforcement_mode, authored_by_user_id"
            async with engine.connect() as conn:
                before = (await conn.execute(sa.text(f"SELECT {columns} FROM person_budget_configs ORDER BY id"))).all()

            await _upgrade(engine)

            async with engine.connect() as conn:
                after = (await conn.execute(sa.text(f"SELECT {columns} FROM person_budget_configs ORDER BY id"))).all()
            assert before == after
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_existing_org_scoped_cap_is_untouched(self):
        engine = await _engine_at_pre_036()
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
        """No backfill: nobody becomes subject to a default by deploying this.

        A backfilled default would be a ceiling nobody authored, applying across every
        org, appearing on people's screens as a limit they never set — and denying
        their requests. Defaults must be an explicit operator decision.
        """
        engine = await _engine_at_pre_036()
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

    The operational rollback is still "revert the PR". These tests prove the function
    is correct if it is ever run, not that anyone should run it.
    """

    @pytest.mark.asyncio
    async def test_downgrade_drops_the_table(self):
        engine = await _engine_at_pre_036()
        try:
            await _upgrade(engine)
            await _downgrade(engine)
            async with engine.connect() as conn:
                assert TABLE not in await conn.run_sync(_tables)
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_downgrade_preserves_preexisting_person_caps(self):
        """Rolling back cannot lose an individual limit.

        Safe by construction — nothing was backfilled and no existing row rewritten —
        which is exactly the claim being verified.
        """
        engine = await _engine_at_pre_036()
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    sa.text(
                        "INSERT INTO person_budget_configs (id, person_anchor, period_type, budget_amount_usd, "
                        "enforcement_mode, authored_by_user_id) VALUES "
                        "('pbc-1', 'github:1', 'monthly', 250.00, 'hard', 'user-self')"
                    )
                )
            await _upgrade(engine)
            await _downgrade(engine)
            async with engine.connect() as conn:
                amount = (await conn.execute(sa.text("SELECT budget_amount_usd FROM person_budget_configs WHERE id='pbc-1'"))).scalar_one()
            assert float(amount) == 250.00
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_upgrade_is_reapplicable_after_downgrade(self):
        """upgrade → downgrade → upgrade, proving the pair is a real inverse.

        A downgrade that left the unique INDEX behind would make re-upgrade fail on a
        duplicate name — a live risk here specifically, because the index is created
        as a separate `op.create_index` rather than inline in the table.
        """
        engine = await _engine_at_pre_036()
        try:
            await _upgrade(engine)
            await _downgrade(engine)
            await _upgrade(engine)
            async with engine.connect() as conn:
                assert TABLE in await conn.run_sync(_tables)
                assert UNIQUE_INDEX in await conn.run_sync(_index_sql)
        finally:
            await engine.dispose()


class TestRevisionChain:
    """Second-head prevention.

    A broken `down_revision` silently SKIPS the migration and live code then queries
    a table that does not exist; a duplicate one creates a second head and
    `alembic upgrade head` fails for everyone, blocking all gateway deploys.
    """

    def test_revision_id(self):
        assert MIG_036.revision == "036_person_budget_defaults"

    def test_chains_onto_the_real_single_head(self):
        """The head was resolved at implementation time, not assumed from the number.

        Unrelated merges take numbers in between, so "the one after 035" is not a
        chain — the parent is named by its revision **id**.
        """
        assert MIG_036.down_revision == "035_budget_usage_entity_key"

    def test_down_revision_names_a_real_existing_revision(self):
        """Catches a typo'd chain: the parent id must exist in some version file."""
        known = set()
        for path in MIGRATIONS_DIR.glob("*.py"):
            if path.name.startswith("__"):
                continue
            known.add(_load_migration(path.name).revision)
        assert MIG_036.down_revision in known

    def test_revision_id_is_unique(self):
        """Two version files claiming one revision id is an ambiguous chain."""
        duplicates = [
            path.name
            for path in MIGRATIONS_DIR.glob("*.py")
            if path.name != THIS_MIGRATION and not path.name.startswith("__") and _load_migration(path.name).revision == MIG_036.revision
        ]
        assert duplicates == []

    def test_revision_ids_fit_alembic_version_column(self):
        """#4123: an id over 32 chars runs upgrade() then rolls back on Postgres.

        SQLite does not enforce VARCHAR length, so CI cannot catch this at runtime —
        only a static check can. Called out explicitly in this issue's acceptance
        criteria.
        """
        assert len(MIG_036.revision) <= 32
        assert len(MIG_036.down_revision) <= 32

    def test_exactly_one_head_across_all_version_files(self):
        """`alembic heads` must report a SINGLE head.

        Computed structurally rather than trusted: a head is a revision no other
        revision names as its parent. Asserts the *count* and that 036 is still ON the
        chain — deliberately NOT that 036 IS the head, so the next migration to land
        does not turn this into a spurious failure that trains people to edit the test
        rather than read it.
        """
        revisions: set[str] = set()
        parents: set[str] = set()
        down_of: dict[str, tuple[str, ...]] = {}
        for path in MIGRATIONS_DIR.glob("*.py"):
            if path.name.startswith("__"):
                continue
            module = _load_migration(path.name)
            revisions.add(module.revision)
            down_of[module.revision] = (module.down_revision,) if isinstance(module.down_revision, str) else (module.down_revision or ())
            if module.down_revision:
                parents.update((module.down_revision,) if isinstance(module.down_revision, str) else module.down_revision)

        heads = revisions - parents
        assert len(heads) == 1, f"expected exactly one head, found: {sorted(heads)}"

        # A real reachability walk: orphaned here means "not on the down_revision path
        # from the head back to the root". Deliberately not "in parents or is the
        # head", which is a tautology given a single head.
        chain: set[str] = set()
        pending = list(heads)
        while pending:
            cursor = pending.pop()
            if cursor in chain:
                continue
            chain.add(cursor)
            pending.extend(down_of.get(cursor, ()))
        assert MIG_036.revision in chain, "036 has been orphaned off the head's down_revision chain"
