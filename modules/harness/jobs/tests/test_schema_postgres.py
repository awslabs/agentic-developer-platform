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
from harness_jobs.schema import UPGRADES

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


# ---------------------------------------------------------------------------
# Version 2: the approval-consumption table (#5526, w6-03)
# ---------------------------------------------------------------------------


async def test_version_two_is_reachable_one_step_at_a_time(connection):
    """v1 then v2, rather than only the all-at-once path the fixture takes.

    The `pool` fixture applies straight to `SCHEMA_VERSION`, so without this test the
    stepwise upgrade -- the one a database already running v1 will actually take -- is
    never executed. #5535 and #5538 may already have applied v1, and for those databases
    `apply()` runs the v2 statements alone, against tables that already hold rows.
    """
    await downgrade(connection, target=0)

    assert await apply(connection, target=1) == 1
    absent = await connection.fetchval(
        "SELECT to_regclass('harness_approval_consumption') IS NULL"
    )
    assert absent, "v1 created the v2 table; the version boundary is not where it says"

    assert await apply(connection, target=2) == 2
    present = await connection.fetchval(
        "SELECT to_regclass('harness_approval_consumption') IS NOT NULL"
    )
    assert present, "v2 did not create the consumption table"

    assert await apply(connection) == SCHEMA_VERSION
    assert await check_schema_version(connection) is None


async def test_rolling_back_to_v1_drops_only_the_consumption_table(connection):
    """The targeted rollback, and the reason it is dangerous is in `DOWNGRADES[2]`.

    Asserted because a `DROP TABLE` that took the operations table with it would turn a
    documented partial rollback into a full data loss, and because the v1 tables
    surviving is what makes "revoke outstanding approvals first" a sufficient
    precaution rather than an incomplete one.
    """
    assert await downgrade(connection, target=1) == 1

    gone = await connection.fetchval(
        "SELECT to_regclass('harness_approval_consumption') IS NULL"
    )
    assert gone, "the consumption table survived the rollback to v1"

    for table in ("harness_operations", "harness_dispatch_outbox"):
        survived = await connection.fetchval(
            f"SELECT to_regclass('{table}') IS NOT NULL"
        )
        assert survived, f"rolling back to v1 dropped {table}"

    # And the recorded version is v1, so `check_schema_version` refuses this code
    # against it rather than letting v2 code write into a v1 schema.
    assert await current_version(connection) == 1
    with pytest.raises(SchemaMismatch, match="Apply the pending upgrade"):
        await check_schema_version(connection)


async def test_the_consumption_table_restricts_deleting_a_paid_operation(connection):
    """`ON DELETE RESTRICT`, at the catalog, not inferred from an error message.

    A cascade here would mean deleting an operation silently frees its approval for
    reuse -- a row deletion becoming a budget grant. The behavioural half of this is in
    `test_admission_postgres.py`; this asserts the schema is what produces it, so a
    future edit to `ON DELETE CASCADE` fails here even if no behavioural test covers
    that path.
    """
    # Cast to text in SQL rather than comparing in Python: `confdeltype` is PostgreSQL's
    # internal `"char"` type, which asyncpg decodes to *bytes* (`b'r'`), so a bare
    # `== "r"` fails against a schema that is actually correct. Casting makes the
    # assertion about the constraint rather than about the driver's decoding.
    action = await connection.fetchval(
        """
        SELECT confdeltype::text FROM pg_constraint
         WHERE conrelid = 'harness_approval_consumption'::regclass
           AND contype = 'f'
        """
    )
    assert action == "r", (
        f"the foreign key's delete action is {action!r}, not 'r' (RESTRICT); a cascade "
        "makes deleting an operation a way to reuse its approval"
    )


async def test_the_approval_and_operation_are_both_unique(connection):
    """Both single-use rules are constraints: one approval, one paid operation.

    `approval_id` as the primary key stops a second admission under one approval;
    `operation_id` UNIQUE stops a second approval paying for one operation. The second
    is the less obvious one and is what makes "budget renewal through retries"
    unreachable even if a caller supplies a fresh approval for the same operation.
    """
    primary = await connection.fetchval(
        """
        SELECT a.attname FROM pg_constraint c
          JOIN pg_attribute a ON a.attrelid = c.conrelid
                             AND a.attnum = ANY (c.conkey)
         WHERE c.conrelid = 'harness_approval_consumption'::regclass
           AND c.contype = 'p'
        """
    )
    assert primary == "approval_id"

    unique = await connection.fetchval(
        """
        SELECT count(*) FROM pg_constraint c
          JOIN pg_attribute a ON a.attrelid = c.conrelid
                             AND a.attnum = ANY (c.conkey)
         WHERE c.conrelid = 'harness_approval_consumption'::regclass
           AND c.contype = 'u'
           AND a.attname = 'operation_id'
        """
    )
    assert unique == 1, "operation_id is not unique; a second approval could pay twice"


async def test_the_reservation_state_column_refuses_an_unknown_state(connection):
    """The CHECK naming the four states, exercised rather than read.

    `ReservationState` and the column must agree: a state the enum can produce and the
    column rejects is a crash on a compensation path, which is the worst place to
    discover a typo. `tests/test_admission_postgres.py` covers the agreement in the
    other direction.
    """
    from harness_jobs.admission import ReservationState

    # Every CHECK on the table, concatenated, rather than `fetchval`'s first row: a
    # second CHECK added later would otherwise make this assert against whichever one
    # the planner returned first. `contype = 'c'` is the check type; note PostgreSQL 17
    # reports NOT NULL separately as 'n', so this cannot pick those up.
    states = await connection.fetchval(
        """
        SELECT string_agg(pg_get_constraintdef(oid), ' ') FROM pg_constraint
         WHERE conrelid = 'harness_approval_consumption'::regclass
           AND contype = 'c'
        """
    )
    assert states is not None, "the reservation_state CHECK constraint is missing"
    for state in ReservationState:
        assert f"'{state.value}'" in states, (
            f"ReservationState.{state.name} is not permitted by the column's CHECK; "
            "the enum and the schema disagree"
        )


# ---------------------------------------------------------------------------
# Version 3: the durable admission-intent table (#5526 CXR-003)
# ---------------------------------------------------------------------------


async def test_version_three_is_reachable_one_step_at_a_time(connection):
    """v1, v2, then v3 -- the path a database already running v2 will take.

    The `pool` fixture applies straight to `SCHEMA_VERSION`, so the all-at-once path is
    the only one the rest of the suite exercises. A database already admitting
    operations under v2 runs the v3 statements alone, against tables that hold rows,
    and that is the upgrade most likely to be run for real.

    Each boundary is asserted in both directions -- absent before, present after --
    because a table created one version too early is invisible to a test that only
    checks it exists by the end.
    """
    await downgrade(connection, target=0)

    assert await apply(connection, target=1) == 1
    absent = await connection.fetchval(
        "SELECT to_regclass('harness_admission_intent') IS NULL"
    )
    assert absent, "v1 created the v3 table; the version boundary is not where it says"

    assert await apply(connection, target=2) == 2
    still_absent = await connection.fetchval(
        "SELECT to_regclass('harness_admission_intent') IS NULL"
    )
    assert still_absent, "v2 created the v3 table"

    assert await apply(connection, target=3) == 3
    present = await connection.fetchval(
        "SELECT to_regclass('harness_admission_intent') IS NOT NULL"
    )
    assert present, "v3 did not create the admission-intent table"

    assert await apply(connection) == SCHEMA_VERSION
    assert await check_schema_version(connection) is None


async def test_rolling_back_to_v2_drops_only_the_intent_table(connection):
    """The targeted rollback, and its hazard runs the opposite way to `DOWNGRADES[2]`.

    Rolling back to v2 makes nothing reusable -- but any hold still outstanding becomes
    unreclaimable, because this table is the only record naming the
    `(job_id, attempt_id)` a recovering process would ask the ledger about. So the
    assertion that the *other* tables survive matters for a different reason than it
    does at the v1 boundary: the consumption table surviving is what keeps single-use
    enforcement intact across the rollback, and losing it as collateral would convert a
    narrow reconciliation hazard into a budget-reuse one.
    """
    assert await downgrade(connection, target=2) == 2

    gone = await connection.fetchval(
        "SELECT to_regclass('harness_admission_intent') IS NULL"
    )
    assert gone, "the admission-intent table survived the rollback to v2"

    for table in (
        "harness_operations",
        "harness_dispatch_outbox",
        "harness_approval_consumption",
    ):
        survived = await connection.fetchval(
            f"SELECT to_regclass('{table}') IS NOT NULL"
        )
        assert survived, f"rolling back to v2 dropped {table}"

    assert await current_version(connection) == 2
    with pytest.raises(SchemaMismatch, match="Apply the pending upgrade"):
        await check_schema_version(connection)


async def test_the_intent_table_is_keyed_per_approval_and_per_operation(connection):
    """One intent row per approval, one per operation -- both as constraints.

    `approval_id` as the primary key is what makes `_record_intent` an upsert rather
    than a source of duplicate rows under retry: a retry of the same admission finds
    the row it wrote last time. `operation_id` UNIQUE is the stronger claim -- two
    approvals cannot register intent against one derived operation, so the sweep can
    never find two rows naming one ledger key and release a hold twice.
    """
    primary = await connection.fetchval(
        """
        SELECT a.attname FROM pg_constraint c
          JOIN pg_attribute a ON a.attrelid = c.conrelid
                             AND a.attnum = ANY (c.conkey)
         WHERE c.conrelid = 'harness_admission_intent'::regclass
           AND c.contype = 'p'
        """
    )
    assert primary == "approval_id"

    unique = await connection.fetchval(
        """
        SELECT count(*) FROM pg_constraint c
          JOIN pg_attribute a ON a.attrelid = c.conrelid
                             AND a.attnum = ANY (c.conkey)
         WHERE c.conrelid = 'harness_admission_intent'::regclass
           AND c.contype = 'u'
           AND a.attname = 'operation_id'
           AND cardinality(c.conkey) = 1
        """
    )
    assert unique == 1, (
        "operation_id is not unique in the intent table; two approvals could register "
        "intent against one ledger key"
    )


async def test_the_intent_table_has_no_foreign_key(connection):
    """Deliberately unreferenced, and the absence has to be asserted.

    An intent row exists *before* the operation and consumption rows do -- that is the
    entire point of the table -- so a foreign key to either would make the row
    unwritable at the only moment it is needed. This reads like a missing constraint,
    which is exactly why a test states it: the next person to notice the omission
    should find this rather than add the key and discover the failure in production.
    """
    keys = await connection.fetch(
        """
        SELECT conname, pg_get_constraintdef(oid) AS definition FROM pg_constraint
         WHERE conrelid = 'harness_admission_intent'::regclass AND contype = 'f'
        """
    )
    assert keys == [], (
        "the intent table gained a foreign key; it is written before the rows it "
        f"would reference exist: {[dict(row) for row in keys]}"
    )


async def test_the_stage_column_refuses_an_unknown_stage(connection):
    """`IntentStage` and the column's CHECK must name the same four values.

    A stage the enum can produce and the column rejects is a crash while recording
    intent -- which happens *before* the ledger is contacted, so the admission would
    fail closed, but the one written just after a reserve reply would leave exactly the
    orphaned hold this table exists to prevent. Asserted per-member rather than as a
    string compare so the failure names the offending stage.
    """
    from harness_jobs.admission import IntentStage

    stages = await connection.fetchval(
        """
        SELECT string_agg(pg_get_constraintdef(oid), ' ') FROM pg_constraint
         WHERE conrelid = 'harness_admission_intent'::regclass AND contype = 'c'
        """
    )
    assert stages is not None, "the stage CHECK constraint is missing"
    for stage in IntentStage:
        assert f"'{stage.value}'" in stages, (
            f"IntentStage.{stage.name} is not permitted by the column's CHECK; the "
            "enum and the schema disagree"
        )


async def test_the_recovery_columns_are_not_nullable(connection):
    """What recovery needs is on every row; what it learns later may be null.

    The envelope columns are the ones that make reconciliation work *after* the
    approval has expired or been revoked -- which is when it matters. A nullable
    ceiling would mean a recoverable row that cannot be recovered, indistinguishable
    from one that can. `reservation_id` is the one column that must stay nullable: a
    row at stage `intended` exists precisely because the ledger has not named a
    reservation yet, and that is why recovery keys on `(job_id, attempt_id)` instead.
    """
    expected = {
        ("operation_id", "NO"),
        ("job_id", "NO"),
        ("attempt_id", "NO"),
        ("org_id", "NO"),
        ("workspace_id", "NO"),
        ("max_resource_units", "NO"),
        ("max_runtime_seconds", "NO"),
        ("max_cost_micros", "NO"),
        ("stage", "NO"),
        ("reservation_id", "YES"),
        ("resolution", "YES"),
    }
    for column, nullable in sorted(expected):
        actual = await connection.fetchval(
            """
            SELECT is_nullable FROM information_schema.columns
             WHERE table_name = 'harness_admission_intent' AND column_name = $1
               AND table_schema = current_schema()
            """,
            column,
        )
        assert actual is not None, (
            f"harness_admission_intent.{column} is missing from v3"
        )
        assert actual == nullable, (
            f"harness_admission_intent.{column} nullability is {actual}, expected "
            f"{nullable}"
        )


async def test_the_sweep_index_is_partial_on_unresolved_rows(connection):
    """The sweep's only query has an index, and it covers just the unresolved rows.

    Asserted against the catalog because the partiality is the property that keeps the
    sweep cheap: every healthy admission resolves in milliseconds, so the interesting
    set is permanently tiny relative to the table. An index over all rows would still
    make the query correct -- and would make the sweep expensive enough to schedule
    rarely, which turns a leaked hold from a minutes-long problem into an hours-long
    one. That is a regression no behavioural test can see.
    """
    definition = await connection.fetchval(
        """
        SELECT indexdef FROM pg_indexes
         WHERE tablename = 'harness_admission_intent'
           AND indexname = 'harness_admission_intent_unresolved_idx'
           AND schemaname = current_schema()
        """
    )
    assert definition is not None, "the unresolved-rows index is missing"
    assert "stage <> 'resolved'" in definition, (
        f"the sweep index is not partial on unresolved rows: {definition}"
    )
    assert "created_at" in definition, (
        "the sweep index does not order by created_at; the sweep reclaims oldest-first"
    )


# Every table v7 introduces. Named once so the boundary and rollback tests cannot
# disagree about the set: the completeness proofs (`harness_allocation_enumeration`,
# `harness_allocation_seal`) arrived with the same migration as the membership they
# certify, and a rollback that took membership but left a seal behind would leave a
# sealed allocation with nothing to seal.
_V7_TABLES = (
    "harness_provider_report",
    "harness_allocation_resource",
    "harness_allocation_enumeration",
    "harness_allocation_seal",
)


async def test_version_seven_is_reachable_one_step_at_a_time(connection):
    """v6 then v7 -- the path a database already holding operations will take.

    Same reasoning as the v3 case: the `pool` fixture applies straight to
    `SCHEMA_VERSION`, so without this the single-step upgrade most likely to be run for
    real is the one nothing exercises. Both boundaries are asserted in both directions,
    because a table created one version too early is invisible to a test that only
    checks it exists by the end.
    """
    await downgrade(connection, target=0)

    assert await apply(connection, target=6) == 6
    for table in _V7_TABLES:
        absent = await connection.fetchval(f"SELECT to_regclass('{table}') IS NULL")
        assert absent, f"v6 created {table}; the version boundary is not where it says"

    assert await apply(connection, target=7) == 7
    for table in _V7_TABLES:
        present = await connection.fetchval(
            f"SELECT to_regclass('{table}') IS NOT NULL"
        )
        assert present, f"v7 did not create {table}"

    assert await apply(connection) == SCHEMA_VERSION
    assert await check_schema_version(connection) is None


async def test_rolling_back_to_v6_drops_only_the_inventory_tables(connection):
    """The targeted rollback, whose hazard is written above `DOWNGRADES[7]`.

    The assertion that the *other* tables survive carries the weight here. Losing
    `harness_allocation_resource` makes every release assessment UNRESOLVED, which
    is the safe direction -- but if the rollback also took the operation or call-intent
    tables as collateral, the money would additionally become unreclaimable, because
    nothing would name the resources or the provider calls to ask about.
    """
    assert await downgrade(connection, target=6) == 6

    for table in _V7_TABLES:
        gone = await connection.fetchval(f"SELECT to_regclass('{table}') IS NULL")
        assert gone, f"{table} survived the rollback to v6"

    for table in (
        "harness_operations",
        "harness_dispatch_outbox",
        "harness_approval_consumption",
        "harness_provider_call_intent",
        "harness_operation_leases",
    ):
        survived = await connection.fetchval(
            f"SELECT to_regclass('{table}') IS NOT NULL"
        )
        assert survived, f"rolling back to v6 dropped {table}"

    assert await current_version(connection) == 6
    with pytest.raises(SchemaMismatch, match="Apply the pending upgrade"):
        await check_schema_version(connection)


async def test_v7_adds_the_allocation_column_to_the_existing_intent_table(connection):
    """v7's only change to a pre-existing table, which the table tests above cannot see.

    `harness_provider_call_intent` is a v4 table, so every assertion around it is about
    survival rather than about shape -- and a column added to it at v7 would be
    invisible to all of them. It is load-bearing: `creating_calls_unaccounted_for`
    finds another operation's in-flight creating calls by `allocation_id`, and without
    the column the seal-time accounting check silently matches nothing, which reopens
    the F1 defect while every behavioural test about a single operation still passes.

    Nullable deliberately: "this call names no allocation" is a real answer for the
    operations that are not allocation-bound, and a NOT NULL column would have forced a
    sentinel value that the accounting query would then have to know to ignore.
    """
    await downgrade(connection, target=6)
    column = "SELECT is_nullable FROM information_schema.columns WHERE table_name = "
    absent = await connection.fetchval(
        column + "'harness_provider_call_intent' AND column_name = 'allocation_id' "
        "AND table_schema = current_schema()"
    )
    assert absent is None, "v6 already carries allocation_id; the boundary is wrong"

    assert await apply(connection, target=7) == 7
    assert (
        await connection.fetchval(
            column + "'harness_provider_call_intent' AND column_name = "
            "'allocation_id' AND table_schema = current_schema()"
        )
        == "YES"
    )
    assert await apply(connection) == SCHEMA_VERSION


async def test_the_allocation_accounting_index_is_partial(connection):
    """The seal-time accounting query has an index, covering only allocation-bound rows.

    Partiality for the same reason as the sweep index above: most provider calls name no
    allocation, so indexing the NULLs would make the index track the whole table to
    answer a question only about the rest of it. This query runs under the allocation
    lock on the release path, where a sequential scan does not merely cost time -- it
    holds the lock that every membership write and every seal is waiting behind.
    """
    definition = await connection.fetchval(
        """
        SELECT indexdef FROM pg_indexes
         WHERE tablename = 'harness_provider_call_intent'
           AND indexname = 'harness_provider_call_intent_allocation_idx'
           AND schemaname = current_schema()
        """
    )
    assert definition is not None, "the allocation accounting index is missing"
    assert "allocation_id IS NOT NULL" in definition, (
        f"the accounting index is not partial on allocation-bound rows: {definition}"
    )
    assert "created_at" in definition, (
        "the accounting index does not carry created_at; the check reads oldest-first"
    )


async def test_rolling_back_to_v6_removes_the_allocation_column(connection):
    """The rollback is complete, and the hazard of that completeness is the point.

    Dropping the column removes the denormalized allocation binding until v7 is
    reapplied; membership and report deletion remains irreversible. The hazard is
    documented above `DOWNGRADES[7]`; asserted here so the rollback cannot quietly
    become partial, which would leave a v6 schema carrying a v7 column that the v7
    upgrade's `ADD COLUMN IF NOT EXISTS` would then decline to re-add.
    """
    assert await downgrade(connection, target=6) == 6
    assert (
        await connection.fetchval(
            "SELECT count(*) FROM information_schema.columns WHERE table_name = "
            "'harness_provider_call_intent' AND column_name = 'allocation_id' "
            "AND table_schema = current_schema()"
        )
        == 0
    )
    assert (
        await connection.fetchval(
            "SELECT count(*) FROM pg_indexes WHERE indexname = "
            "'harness_provider_call_intent_allocation_idx' "
            "AND schemaname = current_schema()"
        )
        == 0
    )
    assert await apply(connection) == SCHEMA_VERSION


async def test_the_v7_upgrade_and_rollback_round_trips(connection):
    """Upgrade then downgrade leaves the schema as it started, and again after.

    A migration that is not reversible is one an operator cannot back out of during an
    incident, and the second round trip is what distinguishes "the DROP worked" from
    "the CREATE is idempotent enough to re-run".
    """
    for _ in range(2):
        assert await downgrade(connection, target=6) == 6
        assert await apply(connection, target=7) == 7
    assert await apply(connection) == SCHEMA_VERSION
    assert await check_schema_version(connection) is None


async def test_a_report_row_cannot_carry_a_zero_fence(connection):
    """The CHECK is the schema's own refusal of a never-granted fence.

    `fence_token = 0` means "never granted" (`leases.py`), so a report claiming one is a
    report from a holder that never held anything. Enforced in the column rather than
    only in Python, because the table is what a future writer will INSERT into.
    """
    asyncpg = pytest.importorskip("asyncpg")
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await connection.execute(
            """
            INSERT INTO harness_provider_report (
                report_digest, operation_id, org_id, workspace_id, attempt_id,
                executor_id, fence_token, allocation_id, observations,
                sealed_revision, enumeration_binding
            ) VALUES ('d','op','org','ws','att','holder',0,'alloc','{}','rev','l')
            """
        )


async def test_membership_is_keyed_per_allocation_and_resource(connection):
    """One row per resource per allocation, per tenant, as a constraint.

    The composite primary key is what makes a duplicate enumeration an upsert rather
    than a second row: two rows naming one resource would let a release reconcile the
    one it happened to read and leave the other unaccounted.
    """
    definition = await connection.fetchval(
        """
        SELECT indexdef FROM pg_indexes
         WHERE tablename = 'harness_allocation_resource'
           AND indexname = 'harness_allocation_resource_pkey'
           AND schemaname = current_schema()
        """
    )
    assert definition is not None, "the membership primary key is missing"
    for column in ("org_id", "workspace_id", "allocation_id", "resource_id"):
        assert column in definition, (
            f"the membership key does not include {column}: {definition}"
        )


async def test_membership_and_attestations_have_no_cascade_from_operations(connection):
    """F3, asserted where it was wrong: in the constraint, not in a convention.

    Both tables' `operation_id` is PROVENANCE -- a value, not a parent. Membership
    carried `REFERENCES harness_operations ON DELETE CASCADE`, so retiring an operation
    made the DATABASE shrink an inventory whose entire contract is that it only grows:
    a still-billing resource went unenumerated and the remainder read as a complete
    allocation.

    Asserted against `pg_constraint` rather than by deleting a row, because the
    behavioural test (`test_inventory_postgres.py`) proves the rows survive today while
    this proves nothing can reintroduce the cascade. A future `ON DELETE SET NULL`
    would also pass the behavioural test and would still lose the provenance an
    operator needs.
    """
    for table in ("harness_allocation_resource", "harness_provider_report"):
        references = await connection.fetch(
            """
            SELECT conname, confdeltype FROM pg_constraint
             WHERE conrelid = $1::regclass AND contype = 'f'
            """,
            table,
        )
        assert references == [], (
            f"{table}.operation_id is a foreign key again: {references}"
        )


async def test_an_attestation_is_keyed_by_its_full_grant(connection):
    """F4. The report key must be the attestation binding, not the digest alone.

    A digest-only key was wrong in both directions at once: identical canonical
    observations are routine -- a successor attempt re-querying an unchanged provider
    produces byte-identical bytes -- so legitimate publications were refused and
    cleanup became permanently unavailable; and two publications of the same bytes
    under different grants both saw no row, one INSERT was discarded, and both callers
    were told they had succeeded.

    `attempt_id` and `fence_token` being IN the key is what makes a predecessor's row
    simply not found for a successor's grant, rather than found and hopefully rejected.
    """
    definition = await connection.fetchval(
        """
        SELECT indexdef FROM pg_indexes
         WHERE tablename = 'harness_provider_report'
           AND indexname = 'harness_provider_report_pkey'
           AND schemaname = current_schema()
        """
    )
    assert definition is not None, "the attestation primary key is missing"
    for column in (
        "report_digest",
        "operation_id",
        "org_id",
        "workspace_id",
        "attempt_id",
        "executor_id",
        "fence_token",
    ):
        assert column in definition, (
            f"the attestation key does not include {column}: {definition}"
        )


async def test_an_attestation_must_name_the_revision_it_was_taken_against(connection):
    """F1, at the column: a report with no sealed revision is not storable at all.

    The ordering guarantee reduces to this constraint. `sealed_revision` is where a
    report says WHEN it was taken -- the membership revision the allocation was sealed
    over at publication -- and `_verify_report` requires it to equal the revision sealed
    now. A nullable column would give a caller a way to store a report that matches
    every seal by matching none of them, which is exactly the pre-creation report the
    repair exists to refuse.

    Asserted as `NOT NULL` in the table rather than only through the service, because
    the table is what a future writer will INSERT into, and a report row is durable
    evidence that outlives the code that wrote it.
    """
    asyncpg = pytest.importorskip("asyncpg")
    with pytest.raises(asyncpg.exceptions.NotNullViolationError):
        await connection.execute(
            """
            INSERT INTO harness_provider_report (
                report_digest, operation_id, org_id, workspace_id, attempt_id,
                executor_id, fence_token, allocation_id, observations
            ) VALUES ('d','op','org','ws','att','holder',1,'alloc','{}')
            """
        )
    nullable = await connection.fetchval(
        """
        SELECT is_nullable FROM information_schema.columns
         WHERE table_name = 'harness_provider_report'
           AND column_name = 'sealed_revision'
           AND table_schema = current_schema()
        """
    )
    assert nullable == "NO"


async def test_the_same_bytes_from_two_grants_are_two_attestations(connection):
    """F4, at the constraint: the key admits both rows instead of discarding one."""
    await connection.execute(
        """
        INSERT INTO harness_provider_report (
            report_digest, operation_id, org_id, workspace_id, attempt_id,
            executor_id, fence_token, allocation_id, observations, sealed_revision,
            enumeration_binding
        ) VALUES
            ('d','op-1','org','ws','att-1','holder-1',1,'alloc','{}','rev','l'),
            ('d','op-2','org','ws','att-1','holder-2',1,'alloc','{}','rev','l')
        """
    )
    assert (
        await connection.fetchval(
            "SELECT count(*) FROM harness_provider_report WHERE report_digest='d'"
        )
        == 2
    )


async def test_a_seal_is_one_row_per_allocation(connection):
    """F2. One seal per allocation, so "is this closed?" has a single answer.

    A history of seals would let a reader pick the convenient one, and the question the
    release path asks is about now rather than about what was ever sealed.
    """
    definition = await connection.fetchval(
        """
        SELECT indexdef FROM pg_indexes
         WHERE tablename = 'harness_allocation_seal'
           AND indexname = 'harness_allocation_seal_pkey'
           AND schemaname = current_schema()
        """
    )
    assert definition is not None, "the seal primary key is missing"
    for column in ("org_id", "workspace_id", "allocation_id"):
        assert column in definition, (
            f"the seal key does not include {column}: {definition}"
        )
    assert "sealed_revision" not in definition, (
        "the seal is keyed by its revision, so an allocation can be sealed twice over "
        "different membership"
    )


async def test_a_provider_listing_is_one_row_per_provider_per_authority(connection):
    """F1 and F5. A current listing per provider, per authority, replaced not
    accumulated.

    Per-provider because one allocation can hold resources from several and a listing
    from one says nothing about another's; a single allocation-wide row would let one
    provider's answer vouch for all of them.

    Per-AUTHORITY because a listing has only ever been evidence for the attempt, holder
    and fence that took it -- `inventory._completeness` filters on all four. With the
    grant outside the key the rows were additionally mutually exclusive: two operations
    naming one allocation, or a successor alongside its predecessor's record, overwrote
    each other, so whichever asked the provider last silently destroyed the other's
    proof and left its report unpublishable. That is a release outage for the loser
    caused by a legitimate act it has no visibility into.
    """
    definition = await connection.fetchval(
        """
        SELECT indexdef FROM pg_indexes
         WHERE tablename = 'harness_allocation_enumeration'
           AND indexname = 'harness_allocation_enumeration_pkey'
           AND schemaname = current_schema()
        """
    )
    assert definition is not None, "the enumeration primary key is missing"
    for column in (
        "org_id",
        "workspace_id",
        "allocation_id",
        "provider",
        "operation_id",
        "attempt_id",
        "executor_id",
        "fence_token",
    ):
        assert column in definition, (
            f"the enumeration key does not include {column}: {definition}"
        )


async def test_two_authorities_listings_for_one_provider_coexist(connection):
    """F5, at the constraint: the key admits both rows instead of discarding one.

    The counterpart of the key test above, asserted as behaviour. Written directly
    because the point is what the TABLE permits -- a future writer inserting here must
    not be able to erase another authority's proof by asking the provider itself.
    """
    await connection.execute(
        """
        INSERT INTO harness_allocation_enumeration (
            org_id, workspace_id, allocation_id, provider, enumerated_digest,
            handle_count, operation_id, attempt_id, executor_id, fence_token
        ) VALUES
            ('org','ws','alloc','aws','h',1,'op-1','att-1','holder-1',1),
            ('org','ws','alloc','aws','h',1,'op-2','att-1','holder-2',1)
        """
    )
    assert (
        await connection.fetchval(
            "SELECT count(*) FROM harness_allocation_enumeration "
            "WHERE allocation_id='alloc' AND provider='aws'"
        )
        == 2
    )


async def test_a_listing_generation_starts_at_one_and_cannot_go_below_it(connection):
    """F5. The generation is what makes a listing identifiable rather than just
    present.

    A report names the listing generation it was published against and verification
    requires that to still be current, so re-asking the provider invalidates every
    report taken before the question. A generation of 0 -- or a nullable one -- would be
    a listing that no report can be distinguished by, which is the state this column
    exists to remove.
    """
    asyncpg = pytest.importorskip("asyncpg")
    await connection.execute(
        """
        INSERT INTO harness_allocation_enumeration (
            org_id, workspace_id, allocation_id, provider, enumerated_digest,
            handle_count, operation_id, attempt_id, executor_id, fence_token
        ) VALUES ('org','ws','alloc','aws','h',1,'op','att','holder',1)
        """
    )
    assert (
        await connection.fetchval(
            "SELECT generation FROM harness_allocation_enumeration "
            "WHERE allocation_id='alloc'"
        )
        == 1
    )
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await connection.execute(
            "UPDATE harness_allocation_enumeration SET generation = 0 "
            "WHERE allocation_id='alloc'"
        )


async def test_an_attestation_must_name_the_listings_it_was_taken_against(connection):
    """**F5**, at the column: a report with no listing binding is not storable.

    `sealed_revision` proves a report came after membership was FINAL. This proves it
    came after the provider was last ASKED, which is a different claim and the one that
    was missing -- publication required a seal, and a seal requires a listing, but the
    listing stayed replaceable afterwards and nothing tied a report to the one current
    when it was published. So a successor could publish "the cluster is absent", record
    its required listing afterwards, and have that later listing satisfy completeness
    for the very read that honoured the earlier report. When the listing said the handle
    was
    PRESENT, the contradicting evidence was what made the report usable.

    Asserted as `NOT NULL` in the table and not only through the service, for the reason
    `sealed_revision` is: a nullable column gives a future writer a report that matches
    every listing state by matching none of them.
    """
    asyncpg = pytest.importorskip("asyncpg")
    with pytest.raises(asyncpg.exceptions.NotNullViolationError):
        await connection.execute(
            """
            INSERT INTO harness_provider_report (
                report_digest, operation_id, org_id, workspace_id, attempt_id,
                executor_id, fence_token, allocation_id, observations, sealed_revision
            ) VALUES ('d','op','org','ws','att','holder',1,'alloc','{}','rev')
            """
        )
    nullable = await connection.fetchval(
        """
        SELECT is_nullable FROM information_schema.columns
         WHERE table_name = 'harness_provider_report'
           AND column_name = 'enumeration_binding'
           AND table_schema = current_schema()
        """
    )
    assert nullable == "NO"


@pytest.mark.parametrize("completed_statements", range(len(UPGRADES[7]) + 1))
async def test_v7_upgrade_recovers_every_interruption_boundary(
    connection, completed_statements
):
    """Disconnect after any committed DDL, including the column AND trigger.

    The schema-version row is deliberately left at six. This models the public
    apply contract without a caller-owned transaction, rather than reconstructing
    only a subset of the schema that could not occur at the stated interruption.
    """
    await downgrade(connection, target=6)
    for statement in UPGRADES[7][:completed_statements]:
        await connection.execute(statement)
    assert await current_version(connection) == 6
    assert await apply(connection, target=7) == 7
    assert await apply(connection, target=7) == 7
    assert (
        await connection.fetchval(
            "SELECT count(*) FROM pg_trigger "
            "WHERE tgname = 'harness_provider_call_allocation_epoch' "
            "AND tgrelid = 'harness_provider_call_intent'::regclass"
        )
        == 1
    )
    await check_schema_version(connection)
