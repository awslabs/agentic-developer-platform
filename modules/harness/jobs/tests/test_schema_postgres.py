"""Schema upgrade, rollback and the refusal to run against the wrong one.

Issue #5525 (w6-02), EPIC #4910, Wave 6. Design requirement 3's "explicit schema
upgrade and rollback/compatibility behavior".
"""

from __future__ import annotations

import pytest

from harness_jobs import (
    SCHEMA_VERSION,
    SchemaMismatch,
    apply,
    check_schema_version,
    current_version,
    downgrade,
)

from .conftest import requires_postgres

pytestmark = requires_postgres


async def test_a_fresh_database_reports_version_zero(pool, schema_name):
    """Not-installed is 0, not an exception: it is the normal pre-install state."""
    asyncpg = pytest.importorskip("asyncpg")
    from .conftest import postgres_url

    empty = "harness_jobs_empty_" + schema_name.split("_")[-1]
    admin = await asyncpg.connect(
        postgres_url(), server_settings={"search_path": empty}
    )
    try:
        await admin.execute(f'CREATE SCHEMA "{empty}"')
        await admin.execute(f'SET search_path TO "{empty}"')
        assert await current_version(admin) == 0

        # And the store refuses to operate, rather than failing mid-transaction on a
        # column that does not exist.
        with pytest.raises(SchemaMismatch, match="not installed"):
            await check_schema_version(admin)
    finally:
        await admin.execute(f'DROP SCHEMA IF EXISTS "{empty}" CASCADE')
        await admin.close()


async def test_apply_is_idempotent(connection):
    """An installer that cannot be retried leaves a half-applied schema on a drop."""
    assert await current_version(connection) == SCHEMA_VERSION
    assert await apply(connection) == SCHEMA_VERSION
    assert await apply(connection) == SCHEMA_VERSION
    assert await check_schema_version(connection) is None

    rows = await connection.fetchval("SELECT count(*) FROM harness_jobs_schema_version")
    assert rows == 1, "a second apply inserted a second version row"


async def test_the_version_table_cannot_hold_two_rows(connection):
    """The CHECK is what makes it single-row.

    Without it, two concurrent installers produce two rows and the version becomes
    ambiguous exactly when it matters most.
    """
    with pytest.raises(Exception):
        await connection.execute(
            "INSERT INTO harness_jobs_schema_version (id, version) VALUES (2, 1)"
        )


async def test_a_stored_version_of_zero_is_unconstructible(connection):
    """0 means "not installed", so it must not also be a storable version.

    Otherwise a corrupted row claiming 0 is indistinguishable from an absent store, and
    the two need opposite responses: install the fresh one, refuse to touch the
    corrupted one. Making the ambiguous value unwritable is cheaper than teaching every
    reader to disambiguate it.
    """
    with pytest.raises(Exception):
        await connection.execute("UPDATE harness_jobs_schema_version SET version = 0")


async def test_an_older_schema_is_refused(connection, monkeypatch):
    """Code newer than the schema would reference columns that do not exist.

    Simulated by advancing the *code's* required version rather than lowering the
    database's, because at `SCHEMA_VERSION = 1` there is no valid lower value to store
    -- see the test above. Patching the code models the real situation exactly: a
    deployment whose new code landed before its migration ran.
    """
    from harness_jobs import schema as schema_module

    monkeypatch.setattr(schema_module, "SCHEMA_VERSION", SCHEMA_VERSION + 1)

    with pytest.raises(SchemaMismatch, match="requires") as caught:
        await check_schema_version(connection)
    assert "Apply the pending upgrade" in str(caught.value), (
        "the refusal must tell an operator which direction to move"
    )


async def test_a_newer_schema_is_refused(connection):
    """The rollback case, and the one usually got wrong.

    Rolling the code back while leaving the newer schema in place means old code
    writing rows that violate an invariant the new schema added but the old code does
    not know to maintain. A refusal to start is a visible outage; silently writing bad
    rows is not -- so this direction must refuse too, not just the older one.
    """
    await connection.execute(
        "UPDATE harness_jobs_schema_version SET version = $1", SCHEMA_VERSION + 5
    )
    with pytest.raises(SchemaMismatch) as caught:
        await check_schema_version(connection)
    message = str(caught.value)
    assert "Roll the schema back BEFORE the code" in message, (
        "the refusal must state the safe ordering; an operator reading this message "
        "is deciding what to do next"
    )


async def test_downgrade_removes_the_store_and_apply_restores_it(connection):
    """Rollback is a real path, not a docstring.

    Destructive by design -- it drops the admission records -- which is why the test
    also asserts the tables are gone rather than merely that the version changed.
    """
    await downgrade(connection, target=0)

    assert await current_version(connection) == 0
    for table in (
        "harness_operations",
        "harness_dispatch_outbox",
        "harness_jobs_schema_version",
    ):
        exists = await connection.fetchval(f"SELECT to_regclass('{table}') IS NOT NULL")
        assert not exists, f"{table} survived the downgrade"

    assert await apply(connection) == SCHEMA_VERSION
    assert await check_schema_version(connection) is None


async def test_the_uniqueness_constraint_exists_in_the_schema_itself(connection):
    """The duplicate refusal is a constraint, so its absence must fail a test.

    Asserted against the catalog rather than inferred from behaviour: a future change
    that drops the constraint and adds an application-level pre-check would keep the
    behavioural tests green under low concurrency and fail this one.
    """
    constraint = await connection.fetchval(
        """
        SELECT conname FROM pg_constraint
         WHERE conname = 'harness_operations_idempotent' AND contype = 'u'
        """
    )
    assert constraint == "harness_operations_idempotent"

    columns = await connection.fetch(
        """
        SELECT a.attname
          FROM pg_constraint c
          JOIN unnest(c.conkey) AS k(attnum) ON TRUE
          JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
         WHERE c.conname = 'harness_operations_idempotent'
        """
    )
    names = {row["attname"] for row in columns}
    assert names == {"org_id", "workspace_id", "idempotency_key"}, (
        "the tenant must be inside the key, or one tenant's key collision denies "
        "another tenant's operation"
    )


async def test_one_outbox_row_per_operation_is_enforced(connection):
    """A UNIQUE on `operation_id`, so a re-inserted row cannot double-dispatch."""
    unique = await connection.fetchval(
        """
        SELECT count(*) FROM pg_constraint c
          JOIN pg_attribute a ON a.attrelid = c.conrelid
                             AND a.attnum = ANY (c.conkey)
         WHERE c.conrelid = 'harness_dispatch_outbox'::regclass
           AND c.contype = 'u'
           AND a.attname = 'operation_id'
        """
    )
    assert unique == 1


async def test_v1_carries_the_replay_and_ownership_columns(connection):
    """The columns the repairs depend on are in v1, asserted against the catalog.

    They were added to the v1 statements rather than as a v2 migration, because v1 has
    never been applied to any database -- there is no deployed schema for a migration to
    move, and a `NOT NULL` column added to a table that has never existed needs no
    backfill. The README states that; this makes it checkable.

    Nullability is part of the assertion, not decoration. `request_payload` nullable
    would mean an operation that cannot be replayed is representable, which is the F2
    defect expressed as a schema that permits it. `abandoned_at` is the one column that
    must stay nullable -- it is the "not abandoned" state of most rows.
    """
    expected = {
        ("harness_operations", "job_id", "NO"),
        ("harness_operations", "attempt_id", "NO"),
        ("harness_operations", "request_payload", "NO"),
        ("harness_dispatch_outbox", "job_id", "NO"),
        ("harness_dispatch_outbox", "attempt_id", "NO"),
        ("harness_dispatch_outbox", "request_payload", "NO"),
        ("harness_dispatch_outbox", "claim_generation", "NO"),
        ("harness_dispatch_outbox", "abandoned_at", "YES"),
    }
    for table, column, nullable in sorted(expected):
        actual = await connection.fetchval(
            """
            SELECT is_nullable FROM information_schema.columns
             WHERE table_name = $1 AND column_name = $2
               AND table_schema = current_schema()
            """,
            table,
            column,
        )
        assert actual is not None, f"{table}.{column} is missing from v1"
        assert actual == nullable, (
            f"{table}.{column} nullability is {actual}, expected {nullable}"
        )


async def test_the_job_identity_is_unique_in_the_schema_itself(connection):
    """One job, one operation -- as a constraint, because the comment promises one.

    The v1 relationship is one job per admitted operation, and `(job_id, attempt_id)`
    is the key the published budget hooks are idempotent on. Two operations claiming
    one job would make that key ambiguous exactly where a hook must be exact: two
    operations' spend reserved against one key.

    Asserted against the catalog for the same reason the idempotency constraint is. A
    comment that promises an enforced invariant while the DDL enforces nothing is worse
    than no comment -- it reads as covered. If a later story needs many operations per
    job, dropping a UNIQUE is a safe migration; discovering the invariant was never
    enforced is not.
    """
    unique = await connection.fetchval(
        """
        SELECT count(*) FROM pg_constraint c
          JOIN pg_attribute a ON a.attrelid = c.conrelid
                             AND a.attnum = ANY (c.conkey)
         WHERE c.conrelid = 'harness_operations'::regclass
           AND c.contype = 'u'
           AND a.attname = 'job_id'
           AND cardinality(c.conkey) = 1
        """
    )
    assert unique == 1, "job_id is not unique; the one-job-one-operation claim is prose"


async def test_the_tenant_columns_are_not_nullable(connection):
    """A NULL tenant is an operation belonging to nobody, and it must be unwritable."""
    for table in ("harness_operations", "harness_dispatch_outbox"):
        for column in ("org_id", "workspace_id"):
            nullable = await connection.fetchval(
                """
                SELECT is_nullable FROM information_schema.columns
                 WHERE table_name = $1 AND column_name = $2
                   AND table_schema = current_schema()
                """,
                table,
                column,
            )
            assert nullable == "NO", f"{table}.{column} is nullable"
