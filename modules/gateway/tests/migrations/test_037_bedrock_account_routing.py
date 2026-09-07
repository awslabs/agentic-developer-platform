"""Tests for Alembic migration 037 — Bedrock account routing tables.

Issue #4743 (#4692 · R2 · routing foundation).

This file is **mandatory**, and not only for coverage: `modules/gateway/alembic/**`
is absent from `gateway-ci.yml`'s trigger paths (`src/**`, `tests/**`, `cli/**`,
`pyproject.toml`, `Dockerfile`, frontend, `libs/`, `contracts/`), so a
migration-only change gets **zero CI signal**. A test under `tests/` is what makes
CI run at all for it. Precedent: `test_034_person_budget_configs.py`,
`test_036_person_budget_defaults.py`.

These tests exercise the REAL migration functions imported from the version
module. A test that re-implements the migration proves only that the author can
write the same bug twice.

What is under test, and why each assertion is load-bearing rather than a
restatement of the DDL:

  - **Uniqueness survives NULL scope columns** (`TestMappingUniquenessAcrossNulls`
    — the most important class in this file). The obvious
    `UNIQUE (scope_type, scope_id_org, scope_id_team, scope_id_user)` is wrong in a
    way no column list reveals: SQL treats NULLs as DISTINCT inside a unique
    constraint, so it accepts **two** rows for one scope. For a budget (036) that
    means two conflicting numbers; here it means **two destination accounts for one
    scope, and which one gets billed depends on row order** — i.e. the
    wrong-account bug this whole EPIC exists to prevent, installed at the schema
    level and invisible to review. The migration indexes `COALESCE(col, '')`
    instead, and the only way to verify that is to insert the duplicate and require
    the database to refuse it.
  - **`ck_bedrock_account_mapping_scope` is enforced, not merely declared.** Each
    rung has exactly one legal column shape. Without the CHECK, an `org` row with a
    NULL `scope_id_org` is a rule matching every tenant through a NULL comparison
    nobody wrote, and a `team` row naming only its team would match a same-named
    team in someone else's org.
  - **`platform` is not a legal `scope_type`.** Rung 4 is the *absence* of a
    mapping (§1.2). A stored platform row would be a second, contradictory way to
    express the fallback, and the ladder would then have two answers to one
    question.
  - **`ck_bedrock_destination_ownership` refuses the incoherent ownership
    combinations** (§4.2 req 2). This is the cross-tenant one: a NULL
    `owner_org_id` must mean "deliberately platform-wide" only when the row *says*
    `is_platform_registered`, never "the writer forgot the tenant".
  - **`routing_capable` and `is_platform_registered` server-default to FALSE.** An
    insert naming neither must land false, because no role today's AWS-connect flow
    creates can invoke Bedrock at all (§5.0). Defaulting true would advertise every
    connected account as a usable routing destination when none is.
  - **`account_id` width matches `usage_logs.bedrock_account_id`.** Shadow mode
    copies one column into the other; a width difference truncates silently at
    exactly the moment an operator is auditing where a call went.
  - **Neither table has `org_id`/`TenantMixin`.** `TenantMixin.org_id` is
    `nullable=False`, so a platform-scoped row is *unrepresentable* in a
    tenant-scoped table — which is why these tables exist instead of reusing
    `user_credentials`. `scope_id_org` is a scope a row *declares*, not a partition
    it *lives in*.
  - **Migration/model parity**, including CHECK names and the index expressions.
    Both are hand-written; a CHECK present in only one means every other test in
    the suite passes against a schema the database does not have.
  - **Nothing is added to `usage_logs`.** `bedrock_account_id` has existed since
    `001_initial_schema.py` and is already plumbed through `log_request`; it has
    simply never been written. A migration for it would be a no-op at best and a
    conflict at worst, so the absence is asserted rather than assumed.
  - **The revision chains onto the real single head** and its id fits
    `alembic_version.version_num` (#4123). A dangling or duplicated
    `down_revision` creates a SECOND HEAD, and `alembic upgrade head` then fails
    for **everyone**, blocking every subsequent gateway deploy.

SQLite backs these tests, and it is a *fair* substrate for the uniqueness question
specifically: SQLite also treats NULLs as distinct in a UNIQUE constraint, so the
`COALESCE` index is doing real work here rather than passing by accident on a more
forgiving engine.
"""

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.shared.models.bedrock_routing import (
    MAPPING_SCOPE_TYPES,
    BedrockAccountMapping,
    BedrockDestinationRegistry,
)

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "alembic" / "versions"

THIS_MIGRATION = "037_bedrock_account_routing.py"

REGISTRY_TABLE = "bedrock_destination_registry"
REGISTRY_ACCOUNT_INDEX = "ix_bedrock_destination_account_id"
REGISTRY_OWNERSHIP_CHECK = "ck_bedrock_destination_ownership"

MAPPING_TABLE = "bedrock_account_mappings"
MAPPING_UNIQUE_INDEX = "uq_bedrock_account_mapping_scope"
MAPPING_DESTINATION_INDEX = "ix_bedrock_account_mapping_destination"
MAPPING_SCOPE_CHECK = "ck_bedrock_account_mapping_scope"

EXPECTED_REGISTRY_COLUMNS = {
    "id",
    "account_id",
    "role_arn",
    "credential_id",
    "owner_org_id",
    "is_platform_registered",
    "routing_capable",
    "verified_at",
    "region",
    "label",
    "registered_by_user_id",
    "created_at",
    "updated_at",
}

# `credential_id` is NULL for an admin-registered destination, `owner_org_id` for a
# platform-registered one, `verified_at` until a real assume-role has proven it.
# Nothing else has a meaningful "unknown": a NULL account id or role ARN is a
# destination that cannot be reached, a NULL `registered_by_user_id` an
# unattributable decision about where money goes.
EXPECTED_REGISTRY_NULLABLE = ["credential_id", "owner_org_id", "verified_at"]

EXPECTED_MAPPING_COLUMNS = {
    "id",
    "scope_type",
    "scope_id_org",
    "scope_id_team",
    "scope_id_user",
    "destination_id",
    "authored_by_user_id",
    "created_at",
    "updated_at",
}

# Only the three scope columns, and only because each rung leaves the other two
# unset. `destination_id` is NOT nullable — a mapping with no destination is a rule
# pointing nowhere, which the resolver would read as a match and then fail on.
EXPECTED_MAPPING_NULLABLE = ["scope_id_org", "scope_id_team", "scope_id_user"]


def _load_migration(filename: str):
    """Import a migration module by path (they are not an importable package)."""
    path = MIGRATIONS_DIR / filename
    spec = importlib.util.spec_from_file_location(filename.replace(".py", ""), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MIG_037 = _load_migration(THIS_MIGRATION)


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


# The pre-037 state this migration lands on. `usage_logs` is written out with
# `bedrock_account_id` ALREADY PRESENT because that is the truth — the column has
# existed since 001 and is dormant, never written. Included so the "no column is
# added to usage_logs" claim is tested against a realistic starting schema rather
# than an empty database where the assertion would be vacuous.
_PRE_037_TABLES = (
    """
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
        client_tool VARCHAR(32)
    )
    """,
    """
    CREATE TABLE user_credentials (
        id VARCHAR(36) NOT NULL PRIMARY KEY,
        org_id VARCHAR(255) NOT NULL,
        service VARCHAR(255) NOT NULL
    )
    """,
)


async def _engine_at_pre_037():
    """An engine holding the pre-037 tables and nothing else."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        for ddl in _PRE_037_TABLES:
            await conn.execute(sa.text(ddl))
        # CHECK enforcement is on by default in SQLite; this makes the intent
        # explicit for the constraint classes below, which assert it themselves
        # rather than assuming it.
        await conn.execute(sa.text("PRAGMA ignore_check_constraints = OFF"))
    return engine


async def _upgrade(engine):
    async with engine.begin() as conn:
        await conn.run_sync(_run_migration, MIG_037.upgrade)


async def _downgrade(engine):
    async with engine.begin() as conn:
        await conn.run_sync(_run_migration, MIG_037.downgrade)


def _tables(sync_conn):
    return set(sa_inspect(sync_conn).get_table_names())


def _columns_of(table: str):
    def _read(sync_conn):
        return {c["name"]: c for c in sa_inspect(sync_conn).get_columns(table)}

    return _read


def _index_sql_of(table: str):
    """Index name → its `CREATE INDEX` text, read from `sqlite_master`.

    Deliberately NOT `Inspector.get_indexes`: that reflector *silently skips*
    expression-based indexes ("SAWarning: Skipped unsupported reflection of
    expression-based index"), so asking it about `uq_bedrock_account_mapping_scope`
    returns nothing and any assertion built on it passes whether the index is
    present or absent. Reading the stored DDL is both correct here and stricter —
    it lets the assertions below check the COALESCE expressions actually reached
    the database, not merely that something with the right name exists.
    """

    def _read(sync_conn):
        rows = sync_conn.execute(
            sa.text("SELECT name, sql FROM sqlite_master WHERE type = 'index' AND tbl_name = :table"),
            {"table": table},
        ).all()
        return {name: (sql or "") for name, sql in rows}

    return _read


async def _insert_destination(engine, **overrides):
    """Insert one registry row, defaulting every column the caller does not name.

    Defaults to a TENANT-OWNED, verified, routing-capable destination — the shape
    the resolver is allowed to use — so the tests that make it unusable do so
    explicitly and visibly.
    """
    row = {
        "id": "dest-1",
        "account_id": "111122223333",
        "role_arn": "arn:aws:iam::111122223333:role/adp-bedrock-routing",
        "owner_org_id": "org-acme",
        "is_platform_registered": False,
        "routing_capable": True,
        "verified_at": "2026-09-01 00:00:00",
        "label": "Acme production",
        "registered_by_user_id": "user-admin",
        **overrides,
    }
    await _raw_insert(engine, REGISTRY_TABLE, row)


async def _insert_mapping(engine, **overrides):
    """Insert one mapping row, defaulting to an ORG-rung rule.

    Org is the default rung deliberately: it has exactly ONE null scope column, so
    it is the half-NULL case where a naive unique constraint looks like it works.
    """
    row = {
        "id": "map-1",
        "scope_type": "org",
        "scope_id_org": "org-acme",
        "scope_id_team": None,
        "scope_id_user": None,
        "destination_id": "dest-1",
        "authored_by_user_id": "user-admin",
        **overrides,
    }
    await _raw_insert(engine, MAPPING_TABLE, row)


async def _raw_insert(engine, table: str, row: dict):
    columns = ", ".join(row)
    placeholders = ", ".join(f":{name}" for name in row)
    async with engine.begin() as conn:
        await conn.execute(sa.text(f"INSERT INTO {table} ({columns}) VALUES ({placeholders})"), row)


class TestUpgradeShape:
    """The two tables' shapes are the contract, not an implementation detail."""

    @pytest.mark.asyncio
    async def test_upgrade_creates_both_tables(self):
        engine = await _engine_at_pre_037()
        try:
            async with engine.connect() as conn:
                before = await conn.run_sync(_tables)
            assert REGISTRY_TABLE not in before
            assert MAPPING_TABLE not in before

            await _upgrade(engine)

            async with engine.connect() as conn:
                after = await conn.run_sync(_tables)
            assert REGISTRY_TABLE in after
            assert MAPPING_TABLE in after
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_two_tables_not_one(self):
        """A mapping and a destination stay separate objects (design ruling 4a).

        The tempting shortcut is one table with an inline `account_id`. It is wrong
        for two reasons this separation encodes: an account number alone is
        unusable (the platform needs an assumable role *in* that account), and
        "this team's mapping must not point at one person's personal credential"
        (ruling 6) is only checkable as a constraint on a *reference*. Collapsing
        the tables would turn that rule back into a convention about a column.
        """
        engine = await _engine_at_pre_037()
        try:
            await _upgrade(engine)
            async with engine.connect() as conn:
                mapping_columns = await conn.run_sync(_columns_of(MAPPING_TABLE))
            assert "destination_id" in mapping_columns
            assert "account_id" not in mapping_columns, "a mapping must reference a destination, never inline an account number (ruling 4a)"
            assert "role_arn" not in mapping_columns
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_registry_columns_are_exactly_the_designed_set(self):
        engine = await _engine_at_pre_037()
        try:
            await _upgrade(engine)
            async with engine.connect() as conn:
                columns = await conn.run_sync(_columns_of(REGISTRY_TABLE))
            assert set(columns) == EXPECTED_REGISTRY_COLUMNS
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_mapping_columns_are_exactly_the_designed_set(self):
        engine = await _engine_at_pre_037()
        try:
            await _upgrade(engine)
            async with engine.connect() as conn:
                columns = await conn.run_sync(_columns_of(MAPPING_TABLE))
            assert set(columns) == EXPECTED_MAPPING_COLUMNS
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_neither_table_has_a_partition_column(self):
        """No `org_id`, no `TenantMixin` — and here that is a hard requirement.

        `TenantMixin.org_id` is `nullable=False`, so a platform-scoped row is
        literally *unrepresentable* in a tenant-scoped table. That is the reason
        these tables exist at all rather than reusing `user_credentials`. A future
        "make it consistent with the other tables" change would take the platform
        rung with it, so the absence is pinned here.
        """
        engine = await _engine_at_pre_037()
        try:
            await _upgrade(engine)
            async with engine.connect() as conn:
                registry = await conn.run_sync(_columns_of(REGISTRY_TABLE))
                mapping = await conn.run_sync(_columns_of(MAPPING_TABLE))
            for columns, table in ((registry, REGISTRY_TABLE), (mapping, MAPPING_TABLE)):
                assert "org_id" not in columns, f"{table} must stay partition-free — platform-scoped rows have no tenant"
                assert "tenant_id" not in columns
                assert "parent_tenant_id" not in columns
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_only_the_designed_registry_columns_are_nullable(self):
        engine = await _engine_at_pre_037()
        try:
            await _upgrade(engine)
            async with engine.connect() as conn:
                columns = await conn.run_sync(_columns_of(REGISTRY_TABLE))
            assert sorted(name for name, spec in columns.items() if spec["nullable"]) == EXPECTED_REGISTRY_NULLABLE
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_only_the_scope_columns_are_nullable_on_mappings(self):
        """In particular `destination_id` is NOT nullable.

        A mapping with no destination is a rule pointing nowhere — the resolver
        would count it as a match for the scope and then have no account to name.
        """
        engine = await _engine_at_pre_037()
        try:
            await _upgrade(engine)
            async with engine.connect() as conn:
                columns = await conn.run_sync(_columns_of(MAPPING_TABLE))
            assert sorted(name for name, spec in columns.items() if spec["nullable"]) == EXPECTED_MAPPING_NULLABLE
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_account_id_width_matches_usage_logs(self):
        """`String(12)`, byte-for-byte `usage_logs.bedrock_account_id`.

        Shadow mode copies this column into that one. A width difference would
        truncate silently at exactly the moment an operator is trying to audit
        where a call went — and a truncated account id still *looks* like data.
        """
        engine = await _engine_at_pre_037()
        try:
            await _upgrade(engine)
            async with engine.connect() as conn:
                registry = await conn.run_sync(_columns_of(REGISTRY_TABLE))
                usage = await conn.run_sync(_columns_of("usage_logs"))
            assert registry["account_id"]["type"].length == 12
            assert registry["account_id"]["type"].length == usage["bedrock_account_id"]["type"].length
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_routing_capable_defaults_to_false(self):
        """An insert naming no capability lands FALSE, never NULL and never true.

        The truthful default: every role today's AWS-connect flow creates attaches
        only `ReadOnlyAccess`, which excludes `bedrock:InvokeModel` (§5.0), so no
        existing connection can serve a routed call. Defaulting true would
        advertise every connected account as a usable destination when none is —
        and once enforcement lands (#4744) that is an outage per mapped principal,
        not a cosmetic wrong default.
        """
        engine = await _engine_at_pre_037()
        try:
            await _upgrade(engine)
            await _raw_insert(
                engine,
                REGISTRY_TABLE,
                {
                    "id": "dest-bare",
                    "account_id": "111122223333",
                    "role_arn": "arn:aws:iam::111122223333:role/r",
                    "owner_org_id": "org-acme",
                    "label": "bare",
                    "registered_by_user_id": "user-admin",
                },
            )
            async with engine.connect() as conn:
                row = (
                    await conn.execute(
                        sa.text(f"SELECT routing_capable, is_platform_registered, verified_at FROM {REGISTRY_TABLE} WHERE id='dest-bare'")
                    )
                ).one()
            assert not row.routing_capable
            assert not row.is_platform_registered
            assert row.verified_at is None, "a destination is unverified until a real assume-role proves it (§4.4)"
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_region_defaults_to_us_east_1(self):
        engine = await _engine_at_pre_037()
        try:
            await _upgrade(engine)
            await _raw_insert(
                engine,
                REGISTRY_TABLE,
                {
                    "id": "dest-bare",
                    "account_id": "111122223333",
                    "role_arn": "arn:aws:iam::111122223333:role/r",
                    "owner_org_id": "org-acme",
                    "label": "bare",
                    "registered_by_user_id": "user-admin",
                },
            )
            async with engine.connect() as conn:
                region = (await conn.execute(sa.text(f"SELECT region FROM {REGISTRY_TABLE} WHERE id='dest-bare'"))).scalar_one()
            assert region == "us-east-1"
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_the_mapping_unique_index_exists_and_coalesces_all_three_scope_columns(self):
        """The index reaches the database as a UNIQUE, fully-COALESCE'd index.

        All three properties are checked against the stored DDL because all three
        are load-bearing: drop UNIQUE and it enforces nothing; drop any one
        COALESCE and that rung's NULL column goes back to comparing distinct, so
        that rung alone can hold two destination accounts.
        """
        engine = await _engine_at_pre_037()
        try:
            await _upgrade(engine)
            async with engine.connect() as conn:
                indexes = await conn.run_sync(_index_sql_of(MAPPING_TABLE))
            assert MAPPING_UNIQUE_INDEX in indexes, f"{MAPPING_UNIQUE_INDEX} is what enforces one destination per scope"
            ddl = indexes[MAPPING_UNIQUE_INDEX].lower()
            assert "unique" in ddl
            assert "coalesce(scope_id_org, '')" in ddl
            assert "coalesce(scope_id_team, '')" in ddl
            assert "coalesce(scope_id_user, '')" in ddl
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_supporting_indexes_exist(self):
        """The destination reverse-lookup index and the account-id index.

        The reverse lookup answers "does any mapping still reference this
        destination?" before a delete. Under the design's fail-closed rule (§8.3)
        deleting a referenced destination is an outage for every principal mapped
        to it, so that check must be cheap enough to always run.
        """
        engine = await _engine_at_pre_037()
        try:
            await _upgrade(engine)
            async with engine.connect() as conn:
                mapping_indexes = await conn.run_sync(_index_sql_of(MAPPING_TABLE))
                registry_indexes = await conn.run_sync(_index_sql_of(REGISTRY_TABLE))
            assert MAPPING_DESTINATION_INDEX in mapping_indexes
            assert "unique" not in mapping_indexes[MAPPING_DESTINATION_INDEX].lower()
            assert REGISTRY_ACCOUNT_INDEX in registry_indexes
            assert "unique" not in registry_indexes[REGISTRY_ACCOUNT_INDEX].lower(), (
                "one AWS account may legitimately be registered twice — platform-wide and tenant-linked — with different roles"
            )
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_credential_id_has_no_foreign_key(self):
        """No FK to `user_credentials`, so no cascade can delete a destination.

        Deliberate (§2.5, §8.3): under fail-closed, silently removing a
        destination that mappings still reference converts every mapped
        principal's traffic into an outage. A dangling reference the resolver
        skips is the safer failure, and it is the one this shape produces.
        """
        engine = await _engine_at_pre_037()
        try:
            await _upgrade(engine)
            async with engine.connect() as conn:
                fks = await conn.run_sync(lambda c: sa_inspect(c).get_foreign_keys(REGISTRY_TABLE))
                mapping_fks = await conn.run_sync(lambda c: sa_inspect(c).get_foreign_keys(MAPPING_TABLE))
            assert fks == []
            assert mapping_fks == []
        finally:
            await engine.dispose()


class TestMappingUniquenessAcrossNulls:
    """One destination per scope — INCLUDING on the rungs with NULL columns.

    The most important class in this file. The obvious
    `UNIQUE (scope_type, scope_id_org, scope_id_team, scope_id_user)` is wrong in a
    way no column list reveals: SQL treats NULLs as DISTINCT inside a unique
    constraint, so two mappings for the same scope both satisfy it. The rung then
    holds two destination accounts and **which one bills depends on row order** —
    an operator repoints a team at a new account, the ladder keeps reading the old
    row, and nothing anywhere reports a conflict.

    The migration indexes `COALESCE(col, '')` instead. These tests insert the
    duplicates and require the DATABASE to refuse them, which is the only way to
    verify the property.
    """

    @pytest.mark.asyncio
    async def test_two_user_mappings_for_one_user_are_rejected(self):
        """The two-NULLs case, head-on: `scope_id_org` and `scope_id_team` both NULL."""
        engine = await _engine_at_pre_037()
        try:
            await _upgrade(engine)
            await _insert_destination(engine, id="dest-a")
            await _insert_destination(engine, id="dest-b", account_id="444455556666")
            await _insert_mapping(engine, id="map-a", scope_type="user", scope_id_org=None, scope_id_user="user-1", destination_id="dest-a")
            with pytest.raises(IntegrityError):
                await _insert_mapping(engine, id="map-b", scope_type="user", scope_id_org=None, scope_id_user="user-1", destination_id="dest-b")
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_two_org_mappings_for_one_org_are_rejected(self):
        """The org rung has two null columns and one set — still collides."""
        engine = await _engine_at_pre_037()
        try:
            await _upgrade(engine)
            await _insert_destination(engine, id="dest-a")
            await _insert_destination(engine, id="dest-b", account_id="444455556666")
            await _insert_mapping(engine, id="map-a", destination_id="dest-a")
            with pytest.raises(IntegrityError):
                await _insert_mapping(engine, id="map-b", destination_id="dest-b")
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_two_team_mappings_for_one_team_are_rejected(self):
        engine = await _engine_at_pre_037()
        try:
            await _upgrade(engine)
            await _insert_destination(engine, id="dest-a")
            await _insert_destination(engine, id="dest-b", account_id="444455556666")
            await _insert_mapping(engine, id="map-a", scope_type="team", scope_id_org="org-acme", scope_id_team="team-eng", destination_id="dest-a")
            with pytest.raises(IntegrityError):
                await _insert_mapping(
                    engine, id="map-b", scope_type="team", scope_id_org="org-acme", scope_id_team="team-eng", destination_id="dest-b"
                )
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_the_three_rungs_coexist(self):
        """A user, a team and an org mapping are all legal at once.

        They are not duplicates — they are the ladder. Rejecting them would make
        the whole resolution order impossible to express.
        """
        engine = await _engine_at_pre_037()
        try:
            await _upgrade(engine)
            await _insert_destination(engine)
            await _insert_mapping(engine, id="map-u", scope_type="user", scope_id_org=None, scope_id_user="user-1")
            await _insert_mapping(engine, id="map-t", scope_type="team", scope_id_org="org-acme", scope_id_team="team-eng")
            await _insert_mapping(engine, id="map-o", scope_type="org", scope_id_org="org-acme")
            async with engine.connect() as conn:
                count = (await conn.execute(sa.text(f"SELECT COUNT(*) FROM {MAPPING_TABLE}"))).scalar_one()
            assert count == 3
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_same_team_id_in_two_orgs_are_distinct_rules(self):
        """`teams.id` is unique only inside its org, so both halves are in the key.

        `Team` carries `TenantMixin`. Two tenants can legitimately hold a team with
        the same id and each may point it at its own account — so this must NOT
        collide. The matching side of the same property (org A's team-T rule never
        serving a person in org B's team T) is pinned in the resolver's tests, and
        the two together are what keep this from becoming a cross-tenant routing
        bug.
        """
        engine = await _engine_at_pre_037()
        try:
            await _upgrade(engine)
            await _insert_destination(engine)
            await _insert_mapping(engine, id="map-a", scope_type="team", scope_id_org="org-acme", scope_id_team="team-eng")
            await _insert_mapping(engine, id="map-b", scope_type="team", scope_id_org="org-globex", scope_id_team="team-eng")
            async with engine.connect() as conn:
                count = (await conn.execute(sa.text(f"SELECT COUNT(*) FROM {MAPPING_TABLE}"))).scalar_one()
            assert count == 2
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_two_scopes_may_share_one_destination(self):
        """Many scopes → one destination is the normal case, not a duplicate.

        An org and one of its teams may both route to the same account. The unique
        index keys on the SCOPE, never on the destination — keying on the
        destination would forbid the overwhelmingly common configuration.
        """
        engine = await _engine_at_pre_037()
        try:
            await _upgrade(engine)
            await _insert_destination(engine)
            await _insert_mapping(engine, id="map-o", scope_type="org", scope_id_org="org-acme", destination_id="dest-1")
            await _insert_mapping(engine, id="map-t", scope_type="team", scope_id_org="org-acme", scope_id_team="team-eng", destination_id="dest-1")
            async with engine.connect() as conn:
                count = (await conn.execute(sa.text(f"SELECT COUNT(*) FROM {MAPPING_TABLE}"))).scalar_one()
            assert count == 2
        finally:
            await engine.dispose()


class TestMappingScopeCheckConstraint:
    """A row must describe the rung it claims — `ck_bedrock_account_mapping_scope`.

    Without this, an `org` row with a NULL `scope_id_org` is a rule matching every
    tenant through a NULL comparison nobody wrote, and a `user` row carrying a
    stray team id reads as team-scoped to a human and user-scoped to the ladder.
    Three legal shapes; everything else refused by the database.
    """

    @pytest.mark.asyncio
    async def test_the_three_legal_shapes_are_accepted(self):
        engine = await _engine_at_pre_037()
        try:
            await _upgrade(engine)
            await _insert_destination(engine)
            await _insert_mapping(engine, id="map-u", scope_type="user", scope_id_org=None, scope_id_user="user-1")
            await _insert_mapping(engine, id="map-t", scope_type="team", scope_id_org="org-acme", scope_id_team="team-eng")
            await _insert_mapping(engine, id="map-o", scope_type="org", scope_id_org="org-acme")
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_org_row_without_an_org_id_is_rejected(self):
        """The most dangerous malformed row: it would match through a NULL.

        An org-rung rule naming no org is a rule that, under any comparison a
        future query writes with a nullable parameter, could match a tenant that
        never authored it — and reroute their spend.
        """
        engine = await _engine_at_pre_037()
        try:
            await _upgrade(engine)
            await _insert_destination(engine)
            with pytest.raises(IntegrityError):
                await _insert_mapping(engine, scope_type="org", scope_id_org=None)
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_team_row_without_its_org_id_is_rejected(self):
        """A team rule naming only the team could govern a same-id team elsewhere.

        This is the cross-tenant case in DDL: `teams.id` is unique only within its
        org, so a team rule without its org is a rule about an ambiguous subject.
        """
        engine = await _engine_at_pre_037()
        try:
            await _upgrade(engine)
            await _insert_destination(engine)
            with pytest.raises(IntegrityError):
                await _insert_mapping(engine, scope_type="team", scope_id_org=None, scope_id_team="team-eng")
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_team_row_without_its_team_id_is_rejected(self):
        engine = await _engine_at_pre_037()
        try:
            await _upgrade(engine)
            await _insert_destination(engine)
            with pytest.raises(IntegrityError):
                await _insert_mapping(engine, scope_type="team", scope_id_org="org-acme", scope_id_team=None)
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_user_row_without_a_user_id_is_rejected(self):
        engine = await _engine_at_pre_037()
        try:
            await _upgrade(engine)
            await _insert_destination(engine)
            with pytest.raises(IntegrityError):
                await _insert_mapping(engine, scope_type="user", scope_id_org=None, scope_id_user=None)
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_user_row_carrying_an_org_id_is_rejected(self):
        """A user rule is about a person, full stop.

        A user row also naming an org reads as org-scoped to a human and
        user-scoped to the ladder — and the two disagree about who it governs.
        """
        engine = await _engine_at_pre_037()
        try:
            await _upgrade(engine)
            await _insert_destination(engine)
            with pytest.raises(IntegrityError):
                await _insert_mapping(engine, scope_type="user", scope_id_org="org-acme", scope_id_user="user-1")
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_org_row_carrying_a_team_id_is_rejected(self):
        engine = await _engine_at_pre_037()
        try:
            await _upgrade(engine)
            await _insert_destination(engine)
            with pytest.raises(IntegrityError):
                await _insert_mapping(engine, scope_type="org", scope_id_org="org-acme", scope_id_team="team-eng")
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_a_platform_scope_type_is_rejected(self):
        """Rung 4 is the ABSENCE of a mapping (§1.2), never a stored row.

        A `platform` row would be a second, contradictory way to express the
        fallback: the resolver returns the platform rung when nothing matched, so a
        row also claiming it means the ladder has two answers to one question. It
        would also be an inert row an operator authored and can see on screen while
        the resolver never walks it — the #4511 class.
        """
        engine = await _engine_at_pre_037()
        try:
            await _upgrade(engine)
            await _insert_destination(engine)
            with pytest.raises(IntegrityError):
                await _insert_mapping(engine, scope_type="platform", scope_id_org=None)
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_an_unknown_scope_type_is_rejected(self):
        """`department` is a NON-GOAL here, and the CHECK is what says so.

        Adding the rung means a migration widening this constraint AND a rung in
        the resolver's `_MAPPING_RUNG_ORDER` — the two changes that must land
        together. A stored row naming a rung the ladder does not walk is a mapping
        an operator authored, sees, and which routes nobody.
        """
        engine = await _engine_at_pre_037()
        try:
            await _upgrade(engine)
            await _insert_destination(engine)
            with pytest.raises(IntegrityError):
                await _insert_mapping(engine, scope_type="department", scope_id_org="org-acme")
        finally:
            await engine.dispose()


class TestDestinationOwnershipCheckConstraint:
    """`ck_bedrock_destination_ownership` — §4.2 requirement 2.

    The cross-tenant constraint. A NULL `owner_org_id` must mean "deliberately
    platform-wide" only when the row *says* `is_platform_registered=true`; it must
    never be reachable by a writer that simply forgot the tenant. Encoding
    platform-wideness as an absent tenant is how cross-tenant leaks get written by
    well-meaning code, because neither a reader nor a query can tell the two apart.
    """

    @pytest.mark.asyncio
    async def test_tenant_owned_destination_is_accepted(self):
        engine = await _engine_at_pre_037()
        try:
            await _upgrade(engine)
            await _insert_destination(engine, is_platform_registered=False, owner_org_id="org-acme")
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_platform_registered_destination_is_accepted(self):
        """The legitimate no-tenant row: an admin registering an unlinked account."""
        engine = await _engine_at_pre_037()
        try:
            await _upgrade(engine)
            await _insert_destination(engine, is_platform_registered=True, owner_org_id=None)
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_tenant_owned_destination_without_a_tenant_is_rejected(self):
        """The forgotten-tenant row — the one this constraint exists for.

        `is_platform_registered=false` with a NULL `owner_org_id` is a destination
        that belongs to a tenant nobody named. Accepting it would put a row in the
        table that a "platform-wide means owner_org_id IS NULL" query returns for
        every tenant.
        """
        engine = await _engine_at_pre_037()
        try:
            await _upgrade(engine)
            with pytest.raises(IntegrityError):
                await _insert_destination(engine, is_platform_registered=False, owner_org_id=None)
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_platform_registered_destination_carrying_a_tenant_is_rejected(self):
        """Two contradictory statements in one row: platform-wide AND Acme's."""
        engine = await _engine_at_pre_037()
        try:
            await _upgrade(engine)
            with pytest.raises(IntegrityError):
                await _insert_destination(engine, is_platform_registered=True, owner_org_id="org-acme")
        finally:
            await engine.dispose()


class TestUsageLogsUntouched:
    """Nothing is added to `usage_logs` — §3.5, and pre-existing-state finding.

    `bedrock_account_id` has existed since `001_initial_schema.py` and is already
    plumbed through `UsageService.log_request`; it has simply never been written by
    any path. The work of this issue is to POPULATE it. A migration adding it would
    be a no-op at best and, on an environment where 001 has run, a hard
    `DuplicateColumn` failure that blocks the deploy.
    """

    @pytest.mark.asyncio
    async def test_usage_logs_schema_is_byte_identical_after_upgrade(self):
        engine = await _engine_at_pre_037()
        try:
            async with engine.connect() as conn:
                before = await conn.run_sync(_columns_of("usage_logs"))
            await _upgrade(engine)
            async with engine.connect() as conn:
                after = await conn.run_sync(_columns_of("usage_logs"))
            assert set(before) == set(after)
            assert [str(spec["type"]) for spec in before.values()] == [str(spec["type"]) for spec in after.values()]
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_the_dormant_column_is_already_there_to_populate(self):
        """Guards the premise: if this ever fails, the issue's plan is wrong.

        The whole "no migration needed for the usage column" decision rests on the
        column pre-existing. Asserting it here means a future schema change that
        removes it fails loudly rather than making shadow-mode capture silently
        write nowhere.
        """
        from src.shared.models.usage import UsageLog

        column = UsageLog.__table__.c.bedrock_account_id
        assert column.nullable, "NULL must remain expressible — it means 'not captured'"
        assert column.type.length == 12
        assert column.default is None and column.server_default is None, "a default here would stamp a fabricated account onto every pre-feature row"

    def test_the_writer_already_accepts_the_kwarg_and_defaults_it_to_none(self):
        """`log_request(bedrock_account_id=...)` predates this issue and defaults None.

        Callers that do not pass it (the several non-proxy log sites) must keep
        producing NULL rather than a fabricated account id.
        """
        import inspect

        from src.usage.service import UsageService

        parameter = inspect.signature(UsageService.log_request).parameters["bedrock_account_id"]
        assert parameter.default is None


class TestModelParity:
    """The hand-written migration and the hand-written models must agree.

    They are maintained separately: the migration is what runs against dev and
    prod, the models are what the app and every other test use. Drift means the
    suite passes against a schema the database does not have — and for these
    tables that schema is the only thing preventing two destinations for one scope.
    """

    def test_models_declare_the_same_table_names(self):
        assert BedrockDestinationRegistry.__tablename__ == REGISTRY_TABLE
        assert BedrockAccountMapping.__tablename__ == MAPPING_TABLE

    def test_models_declare_the_same_columns(self):
        assert {c.name for c in BedrockDestinationRegistry.__table__.columns} == EXPECTED_REGISTRY_COLUMNS
        assert {c.name for c in BedrockAccountMapping.__table__.columns} == EXPECTED_MAPPING_COLUMNS

    def test_models_have_no_partition_column(self):
        """Neither model may acquire `org_id` — including via `TenantMixin`."""
        for model in (BedrockDestinationRegistry, BedrockAccountMapping):
            names = {c.name for c in model.__table__.columns}
            assert "org_id" not in names
            assert "tenant_id" not in names

    def test_model_nullability_matches_the_migration(self):
        registry_nullable = sorted(c.name for c in BedrockDestinationRegistry.__table__.columns if c.nullable)
        mapping_nullable = sorted(c.name for c in BedrockAccountMapping.__table__.columns if c.nullable)
        assert registry_nullable == EXPECTED_REGISTRY_NULLABLE
        assert mapping_nullable == EXPECTED_MAPPING_NULLABLE

    def test_model_account_id_width_matches_the_usage_column(self):
        """A money-audit column copied into another: the widths must match."""
        from src.shared.models.usage import UsageLog

        assert BedrockDestinationRegistry.__table__.c.account_id.type.length == UsageLog.__table__.c.bedrock_account_id.type.length

    def test_models_declare_the_check_constraints(self):
        """The CHECKs exist on the models, so ORM-created schemas carry them too.

        Tests that build their schema from `Base.metadata.create_all` (much of the
        suite, and `BG_DB_AUTO_CREATE=true` local dev) would otherwise run against
        tables that accept malformed scopes and incoherent ownership while dev and
        prod reject them — the drift direction that makes a green suite meaningless.
        """
        registry_checks = {c.name for c in BedrockDestinationRegistry.__table__.constraints if isinstance(c, sa.CheckConstraint)}
        mapping_checks = {c.name for c in BedrockAccountMapping.__table__.constraints if isinstance(c, sa.CheckConstraint)}
        assert REGISTRY_OWNERSHIP_CHECK in registry_checks
        assert MAPPING_SCOPE_CHECK in mapping_checks

    def test_model_check_text_matches_the_migration_text(self):
        """Same predicate, not merely the same name.

        A CHECK named identically but permitting a different set is worse than a
        missing one: it reads as verified. Compared with whitespace collapsed,
        since only the formatting legitimately differs between the two files.
        """

        def _norm(text_value: str) -> str:
            return " ".join(str(text_value).split())

        mapping_check = next(c for c in BedrockAccountMapping.__table__.constraints if getattr(c, "name", None) == MAPPING_SCOPE_CHECK)
        registry_check = next(c for c in BedrockDestinationRegistry.__table__.constraints if getattr(c, "name", None) == REGISTRY_OWNERSHIP_CHECK)
        assert _norm(mapping_check.sqltext) == _norm(MIG_037._SCOPE_SHAPE)
        assert _norm(registry_check.sqltext) == _norm(MIG_037._OWNERSHIP_SHAPE)

    def test_model_declares_the_unique_index_not_a_unique_constraint(self):
        """The uniqueness key is an INDEX, not a `UniqueConstraint`.

        Asserted as a shape rather than a name: a future change "simplifying" it
        into a `UniqueConstraint` would compile, pass a name check, and silently
        reintroduce the two-destinations-for-one-scope defect that this file's
        central class exists to catch.
        """
        indexes = {index.name: index for index in BedrockAccountMapping.__table__.indexes}
        assert MAPPING_UNIQUE_INDEX in indexes
        assert indexes[MAPPING_UNIQUE_INDEX].unique

        unique_constraints = {c.name for c in BedrockAccountMapping.__table__.constraints if isinstance(c, sa.UniqueConstraint)}
        assert MAPPING_UNIQUE_INDEX not in unique_constraints
        assert unique_constraints == set(), "uniqueness here must be the COALESCE expression index — a plain UNIQUE allows two per scope"

    def test_model_unique_index_coalesces_all_three_nullable_columns(self):
        """Coalescing only some of them fixes only some of the rungs.

        Missing `scope_id_user` would leave the user rung — the narrowest and
        therefore highest-priority one — able to hold two conflicting destinations.
        """
        index = next(i for i in BedrockAccountMapping.__table__.indexes if i.name == MAPPING_UNIQUE_INDEX)
        rendered = " ".join(str(expr) for expr in index.expressions).lower()
        assert "coalesce(scope_id_org, '')" in rendered
        assert "coalesce(scope_id_team, '')" in rendered
        assert "coalesce(scope_id_user, '')" in rendered
        assert "scope_type" in rendered

    def test_scope_types_constant_matches_the_check_constraint(self):
        """`MAPPING_SCOPE_TYPES` and the CHECK must agree, in both directions.

        The constant is what the (future) authoring API validates against; the
        CHECK is what the database enforces. A value in the constant but not the
        CHECK is a write that 500s; a value in the CHECK but not the constant is a
        rung the API refuses to author. And `platform` must be in neither.
        """
        assert set(MAPPING_SCOPE_TYPES) == {"user", "team", "org"}
        for scope_type in MAPPING_SCOPE_TYPES:
            assert f"scope_type = '{scope_type}'" in MIG_037._SCOPE_SHAPE
        assert "'platform'" not in MIG_037._SCOPE_SHAPE

    def test_scope_types_order_is_the_resolver_walk_order(self):
        """Narrowest first — the constant documents the ladder, so it must match.

        A reader (and a future authoring UI) takes the ordering as the precedence.
        If the constant said org-first while the resolver walked user-first, one of
        the two would be lying about which mapping wins.
        """
        from src.proxy.bedrock_routing import _MAPPING_RUNG_ORDER

        assert MAPPING_SCOPE_TYPES == _MAPPING_RUNG_ORDER

    def test_is_usable_for_routing_requires_both_halves(self):
        """`routing_capable AND verified_at` — never one or the other (§4.4).

        They fail for different reasons: an unverified destination has never been
        proven assumable, a non-capable one is a role that assumes fine but cannot
        invoke Bedrock (§5.0). Checking only one lets the other class through, and
        under enforcement (#4744) that is a mapped principal whose every call
        fails.
        """
        from datetime import UTC, datetime

        def _destination(*, capable: bool, verified: bool) -> BedrockDestinationRegistry:
            return BedrockDestinationRegistry(
                id="d",
                account_id="111122223333",
                role_arn="arn:aws:iam::111122223333:role/r",
                owner_org_id="org-acme",
                is_platform_registered=False,
                routing_capable=capable,
                verified_at=datetime(2026, 9, 1, tzinfo=UTC) if verified else None,
                label="d",
                registered_by_user_id="user-admin",
            )

        assert _destination(capable=True, verified=True).is_usable_for_routing
        assert not _destination(capable=True, verified=False).is_usable_for_routing
        assert not _destination(capable=False, verified=True).is_usable_for_routing
        assert not _destination(capable=False, verified=False).is_usable_for_routing

    @pytest.mark.asyncio
    async def test_migrated_tables_accept_rows_written_through_the_models(self):
        """The strongest parity check: the ORM writes into the MIGRATED tables.

        Proves the migration produces the tables the application actually uses, not
        merely ones with matching column names.
        """
        from datetime import UTC, datetime

        engine = await _engine_at_pre_037()
        try:
            await _upgrade(engine)
            session_factory = async_sessionmaker(engine, expire_on_commit=False)
            async with session_factory() as session:
                session.add(
                    BedrockDestinationRegistry(
                        id="dest-orm",
                        account_id="111122223333",
                        role_arn="arn:aws:iam::111122223333:role/adp-bedrock-routing",
                        owner_org_id="org-acme",
                        is_platform_registered=False,
                        routing_capable=True,
                        verified_at=datetime(2026, 9, 1, tzinfo=UTC),
                        label="Acme production",
                        registered_by_user_id="user-admin",
                    )
                )
                session.add(
                    BedrockAccountMapping(
                        id="map-orm",
                        scope_type="team",
                        scope_id_org="org-acme",
                        scope_id_team="team-eng",
                        destination_id="dest-orm",
                        authored_by_user_id="user-admin",
                    )
                )
                await session.commit()

                mapping = await session.scalar(sa.select(BedrockAccountMapping).where(BedrockAccountMapping.id == "map-orm"))
                destination = await session.scalar(sa.select(BedrockDestinationRegistry).where(BedrockDestinationRegistry.id == "dest-orm"))
            assert mapping is not None and destination is not None
            assert mapping.scope_type == "team"
            assert mapping.scope_id_user is None
            assert destination.account_id == "111122223333"
            assert destination.is_usable_for_routing
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_the_orm_cannot_write_a_second_mapping_for_one_scope(self):
        """The index binds ORM writes too, not only raw SQL.

        The (future) authoring API upserts through the ORM, so this is the path a
        racing double-PUT actually takes. If the database accepted it, the scope
        would hold two destination accounts and the billed one would depend on row
        order.
        """
        engine = await _engine_at_pre_037()
        try:
            await _upgrade(engine)
            session_factory = async_sessionmaker(engine, expire_on_commit=False)

            def _row(row_id: str, destination_id: str) -> BedrockAccountMapping:
                return BedrockAccountMapping(
                    id=row_id,
                    scope_type="user",
                    scope_id_user="user-1",
                    destination_id=destination_id,
                    authored_by_user_id="user-admin",
                )

            async with session_factory() as session:
                session.add(_row("map-1", "dest-a"))
                await session.commit()

            async with session_factory() as session:
                session.add(_row("map-2", "dest-b"))
                with pytest.raises(IntegrityError):
                    await session.commit()
        finally:
            await engine.dispose()


class TestAdditive:
    """Nothing that exists today changes meaning.

    An install with no rows in these tables behaves exactly as it does today —
    every call served by the platform account's ambient IRSA credentials — which is
    what makes the rollback "stop reading them, then drop them".
    """

    @pytest.mark.asyncio
    async def test_both_tables_start_empty(self):
        """No backfill: nobody's traffic is redirected by deploying this.

        A backfilled mapping (e.g. one row per existing AWS connection) would be a
        routing decision nobody authored. In shadow mode it would only mislabel the
        audit column; the moment #4744 lands it would move real spend to accounts
        no operator chose.
        """
        engine = await _engine_at_pre_037()
        try:
            await _upgrade(engine)
            async with engine.connect() as conn:
                mappings = (await conn.execute(sa.text(f"SELECT COUNT(*) FROM {MAPPING_TABLE}"))).scalar_one()
                destinations = (await conn.execute(sa.text(f"SELECT COUNT(*) FROM {REGISTRY_TABLE}"))).scalar_one()
            assert mappings == 0
            assert destinations == 0
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_existing_usage_rows_are_untouched(self):
        """Including their NULL `bedrock_account_id`, which stays NULL.

        Historic rows genuinely were not captured. Backfilling them with the
        platform account would turn "we did not look" into "we checked, and it was
        the platform account" — a claim the data cannot support.
        """
        engine = await _engine_at_pre_037()
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    sa.text(
                        "INSERT INTO usage_logs (id, org_id, department_id, team_id, user_id, account_type, model, "
                        "input_tokens, output_tokens, cost_usd, latency_ms, status_code) VALUES "
                        "('ul-1', 'org-a', 'd', 't', 'u', 'human', 'anthropic.claude-3-5-sonnet', 10, 20, 0.001, 100, 200)"
                    )
                )
            columns = "id, org_id, model, input_tokens, output_tokens, cost_usd, bedrock_account_id"
            async with engine.connect() as conn:
                before = (await conn.execute(sa.text(f"SELECT {columns} FROM usage_logs ORDER BY id"))).all()

            await _upgrade(engine)

            async with engine.connect() as conn:
                after = (await conn.execute(sa.text(f"SELECT {columns} FROM usage_logs ORDER BY id"))).all()
            assert before == after
            assert after[0].bedrock_account_id is None
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_existing_credentials_are_untouched(self):
        """No FK, no cascade, no rewrite of any AWS connection.

        A destination is *derived* from a connection; it does not replace one, and
        creating the registry must not alter the vault rows the connect flow owns.
        """
        engine = await _engine_at_pre_037()
        try:
            async with engine.begin() as conn:
                await conn.execute(sa.text("INSERT INTO user_credentials (id, org_id, service) VALUES ('cred-1', 'org-a', 'aws_role')"))
            async with engine.connect() as conn:
                before = (await conn.execute(sa.text("SELECT id, org_id, service FROM user_credentials ORDER BY id"))).all()

            await _upgrade(engine)

            async with engine.connect() as conn:
                after = (await conn.execute(sa.text("SELECT id, org_id, service FROM user_credentials ORDER BY id"))).all()
            assert before == after
        finally:
            await engine.dispose()


class TestDowngrade:
    """downgrade() is exercised, not assumed.

    The operational rollback is still "revert the PR". These tests prove the
    function is correct if it is ever run, not that anyone should run it.
    """

    @pytest.mark.asyncio
    async def test_downgrade_drops_both_tables(self):
        engine = await _engine_at_pre_037()
        try:
            await _upgrade(engine)
            await _downgrade(engine)
            async with engine.connect() as conn:
                remaining = await conn.run_sync(_tables)
            assert REGISTRY_TABLE not in remaining
            assert MAPPING_TABLE not in remaining
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_downgrade_leaves_usage_logs_alone(self):
        """Rolling back must not touch the column shadow mode was writing.

        `bedrock_account_id` predates this issue, so a downgrade that dropped it
        would destroy a column 001 owns — and any rows already captured.
        """
        engine = await _engine_at_pre_037()
        try:
            await _upgrade(engine)
            await _downgrade(engine)
            async with engine.connect() as conn:
                columns = await conn.run_sync(_columns_of("usage_logs"))
            assert "bedrock_account_id" in columns
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_upgrade_is_reapplicable_after_downgrade(self):
        """upgrade → downgrade → upgrade, proving the pair is a real inverse.

        A downgrade leaving the unique INDEX behind would make re-upgrade fail on a
        duplicate name — a live risk here specifically, because the index is
        created as a separate `op.create_index` rather than inline in the table.
        """
        engine = await _engine_at_pre_037()
        try:
            await _upgrade(engine)
            await _downgrade(engine)
            await _upgrade(engine)
            async with engine.connect() as conn:
                assert MAPPING_TABLE in await conn.run_sync(_tables)
                assert MAPPING_UNIQUE_INDEX in await conn.run_sync(_index_sql_of(MAPPING_TABLE))
                assert REGISTRY_ACCOUNT_INDEX in await conn.run_sync(_index_sql_of(REGISTRY_TABLE))
        finally:
            await engine.dispose()


class TestRevisionChain:
    """Second-head prevention.

    A broken `down_revision` silently SKIPS the migration and live code then
    queries tables that do not exist; a duplicate one creates a second head and
    `alembic upgrade head` fails for everyone, blocking all gateway deploys.
    """

    def test_revision_id(self):
        assert MIG_037.revision == "037_bedrock_account_routing"

    def test_chains_onto_the_real_single_head(self):
        """The head was resolved at implementation time, not assumed from the number.

        Unrelated merges take numbers in between, so "the one after 036" is not a
        chain — the parent is named by its revision **id**.
        """
        assert MIG_037.down_revision == "036_person_budget_defaults"

    def test_down_revision_names_a_real_existing_revision(self):
        """Catches a typo'd chain: the parent id must exist in some version file."""
        known = set()
        for path in MIGRATIONS_DIR.glob("*.py"):
            if path.name.startswith("__"):
                continue
            known.add(_load_migration(path.name).revision)
        assert MIG_037.down_revision in known

    def test_revision_id_is_unique(self):
        """Two version files claiming one revision id is an ambiguous chain."""
        duplicates = [
            path.name
            for path in MIGRATIONS_DIR.glob("*.py")
            if path.name != THIS_MIGRATION and not path.name.startswith("__") and _load_migration(path.name).revision == MIG_037.revision
        ]
        assert duplicates == []

    def test_revision_ids_fit_alembic_version_column(self):
        """#4123: an id over 32 chars runs upgrade() then rolls back on Postgres.

        SQLite does not enforce VARCHAR length, so CI cannot catch this at runtime
        — only a static check can.
        """
        assert len(MIG_037.revision) <= 32
        assert len(MIG_037.down_revision) <= 32

    def test_exactly_one_head_across_all_version_files(self):
        """`alembic heads` must report a SINGLE head.

        Computed structurally rather than trusted: a head is a revision no other
        revision names as its parent. Asserts the *count* and that 037 is still ON
        the chain — deliberately NOT that 037 IS the head, so the next migration to
        land does not turn this into a spurious failure that trains people to edit
        the test rather than read it.
        """
        revisions: set[str] = set()
        parents: set[str] = set()
        down_of: dict[str, str | None] = {}
        for path in MIGRATIONS_DIR.glob("*.py"):
            if path.name.startswith("__"):
                continue
            module = _load_migration(path.name)
            revisions.add(module.revision)
            down_of[module.revision] = module.down_revision
            if module.down_revision:
                parents.add(module.down_revision)

        heads = revisions - parents
        assert len(heads) == 1, f"expected exactly one head, found: {sorted(heads)}"

        # A real reachability walk: orphaned here means "not on the down_revision
        # path from the head back to the root". Deliberately not "in parents or is
        # the head", which is a tautology given a single head.
        chain: set[str] = set()
        cursor: str | None = next(iter(heads))
        while cursor is not None and cursor not in chain:
            chain.add(cursor)
            cursor = down_of.get(cursor)
        assert MIG_037.revision in chain, "037 has been orphaned off the head's down_revision chain"

    def test_models_are_registered_for_autogenerate_and_create_all(self):
        """Both new models must be importable from the three registries.

        `PersonBudgetDefault` (036) was omitted from `src/shared/models/__init__.py`
        and `alembic/env.py`, which makes `alembic revision --autogenerate` propose
        DROPPING the table and `create_all` local-dev stacks miss it entirely. Not
        repeating that here, and asserting it so a future refactor cannot quietly
        undo it.
        """
        import src.shared.models as models_package

        assert models_package.BedrockDestinationRegistry is BedrockDestinationRegistry
        assert models_package.BedrockAccountMapping is BedrockAccountMapping

        env_source = (Path(__file__).resolve().parents[2] / "alembic" / "env.py").read_text()
        assert "bedrock_routing" in env_source, "alembic/env.py must import the models or autogenerate proposes dropping the tables"

        app_source = (Path(__file__).resolve().parents[2] / "src" / "app.py").read_text()
        assert "bedrock_routing" in app_source, "create_all local-dev stacks need the models imported"
