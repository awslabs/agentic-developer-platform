"""Real-database behaviour of the operation store.

Issue #5525 (w6-02), EPIC #4910, Wave 6. AC-01 and AC-02.

Covers the scenarios AC-01 names: concurrent duplicate creates, crash before and
after commit, restart, and tenant isolation. Outbox replay is in
`test_outbox_postgres.py`.

Each test states the property it establishes, because a test named
`test_admit_twice` records that something was run and not what it proved.
"""

from __future__ import annotations

import asyncio

import pytest

from harness_jobs import (
    REQUIRED_PERMISSION,
    ConcurrentUpdate,
    ContractViolation,
    OperationRefused,
    OperationRequest,
    OperationState,
    OperationStore,
    ResolvedPrincipal,
    encode_payload,
)

from .conftest import requires_postgres

pytestmark = requires_postgres


def principal(
    org: str = "org-a", workspace: str = "ws-1", *, permitted: bool = True
) -> ResolvedPrincipal:
    return ResolvedPrincipal(
        org_id=org,
        workspace_id=workspace,
        subject="user-1",
        permissions=frozenset({REQUIRED_PERMISSION} if permitted else set()),
    )


def request(key: str = "key-1", **parameters: str) -> OperationRequest:
    return OperationRequest(
        action="provision", idempotency_key=key, parameters=dict(parameters)
    )


# ---------------------------------------------------------------------------
# Durable identity
# ---------------------------------------------------------------------------


async def test_admitted_operation_is_readable_on_a_different_connection(pool):
    """Admission is durable, not connection-local.

    Read back on a *different* connection from the one that wrote it. An in-memory
    or session-cached record would pass a same-connection read and fail this.
    """
    store = OperationStore()
    async with pool.acquire() as writer:
        admitted = await store.admit(writer, principal(), request())

    async with pool.acquire() as reader:
        found = await store.get(reader, principal(), admitted.record.operation_id)

    assert found is not None
    assert found.operation_id == admitted.record.operation_id
    assert found.state is OperationState.PENDING
    # The server-resolved tenant is what was stored, not anything the caller named.
    assert (found.org_id, found.workspace_id) == ("org-a", "ws-1")


async def test_identity_survives_pool_restart(pool, schema_name):
    """AC-02: the record survives losing every connection.

    The closest a test can get to a process restart without forking: close the entire
    pool, open a brand-new one against the same schema, and read. Nothing in the
    original process's memory is available to answer.

    Deliberately does *not* take the `connection` fixture: that fixture holds a
    connection checked out for the whole test, and `pool.close()` waits for every
    connection to be released, so requesting it deadlocks the close against the
    fixture's own teardown.
    """
    asyncpg = pytest.importorskip("asyncpg")
    from .conftest import postgres_url

    store = OperationStore()
    async with pool.acquire() as writer:
        admitted = await store.admit(writer, principal(), request("survives"))
    operation_id = admitted.record.operation_id

    await pool.close()

    fresh = await asyncpg.create_pool(
        postgres_url(),
        min_size=1,
        max_size=2,
        server_settings={"search_path": schema_name},
    )
    try:
        async with fresh.acquire() as reader:
            found = await store.get(reader, principal(), operation_id)
            outbox = await reader.fetchval(
                "SELECT count(*) FROM harness_dispatch_outbox WHERE operation_id = $1",
                operation_id,
            )
    finally:
        await fresh.close()

    assert found is not None, "the operation did not survive losing all connections"
    assert found.operation_id == operation_id
    # The outbox row survived too: a surviving admission with a lost dispatch row
    # would be an operation that exists and will never run.
    assert outbox == 1


async def test_restart_reconstructs_the_exact_admitted_request(pool, schema_name):
    """F2 + AC-02. Surviving an identifier is not surviving the work.

    The test above proves the record is findable after every connection is lost. That
    is necessary and not sufficient: the store kept only a one-way digest of the
    request, so a dispatcher recovering from the crash the outbox exists to survive
    could *identify* an admitted operation and could not *perform* it. Every accepted
    request was unexecutable across exactly the failure the design is for.

    A digest can only ever answer "is this the same request?" -- which is what
    idempotency needs and the opposite of what recovery needs. So the request is
    stored beside the digest, and this asserts the reconstruction is exact, field by
    field, on a pool that shares nothing with the writer.
    """
    asyncpg = pytest.importorskip("asyncpg")
    from .conftest import postgres_url

    store = OperationStore()
    original = request("replay", region="eu-west-1", size="large")
    async with pool.acquire() as writer:
        admitted = await store.admit(writer, principal(), original)
    operation_id = admitted.record.operation_id

    await pool.close()

    fresh = await asyncpg.create_pool(
        postgres_url(),
        min_size=1,
        max_size=2,
        server_settings={"search_path": schema_name},
    )
    try:
        async with fresh.acquire() as reader:
            found = await store.get(reader, principal(), operation_id)
            assert found is not None
            replayed = found.admitted_request()
    finally:
        await fresh.close()

    assert replayed.action == original.action
    assert replayed.idempotency_key == original.idempotency_key
    assert replayed.parameters == original.parameters
    assert replayed.contract_version == original.contract_version
    # And it is the *same* request by the store's own definition of sameness, not
    # merely a similar-looking one: re-admitting it is recognised as the retry it is.
    async with await asyncpg.create_pool(
        postgres_url(),
        min_size=1,
        max_size=2,
        server_settings={"search_path": schema_name},
    ) as verifier:
        async with verifier.acquire() as reader:
            again = await store.admit(reader, principal(), replayed)
    assert again.created is False
    assert again.record.operation_id == operation_id


async def test_a_tampered_payload_is_refused_rather_than_executed(connection):
    """F2's necessary guard: the digest is the witness for the stored payload.

    Storing the request makes recovery possible and creates a second thing that can
    be wrong. If a payload were edited in place -- a bad migration, a manual UPDATE,
    a corrupted write -- a recovering dispatcher would faithfully perform a request
    nobody admitted, under an operation id that vouches for it.

    The digest was committed in the same transaction as the payload, so they can only
    disagree if one was altered afterwards. Every read re-verifies, and a mismatch is
    a refusal to answer: an unexecutable operation is recoverable by an operator, a
    silently substituted one is not.
    """
    store = OperationStore()
    admitted = await store.admit(connection, principal(), request("tamper"))

    # A payload that is itself perfectly *valid* -- it decodes, and every field
    # passes the request's own validation. Only the digest can tell that it is not
    # the one admitted, which is precisely why the digest is still stored.
    substituted = encode_payload(request("tamper", region="somewhere-else"))
    await connection.execute(
        "UPDATE harness_operations SET request_payload = $1 WHERE operation_id = $2",
        substituted,
        admitted.record.operation_id,
    )

    # Every read path, not just the obvious one. A verification that one entry point
    # performs is a verification a caller can route around by choosing another; the
    # guarantee is that no read hands back an unverified payload, so each reader is
    # named here. A new reader added without the check fails this test.
    with pytest.raises(ContractViolation, match="does not match the plan digest"):
        await store.get(connection, principal(), admitted.record.operation_id)

    with pytest.raises(ContractViolation, match="does not match the plan digest"):
        await store.get_by_idempotency_key(connection, principal(), "tamper")

    with pytest.raises(ContractViolation, match="does not match the plan digest"):
        await store.list_for_tenant(connection, principal())

    # And admission itself: the conflict path reads the stored row to decide whether a
    # retry is the same request. Answering from an unverified row would let a tampered
    # payload be confirmed as "already admitted, identical".
    with pytest.raises(ContractViolation, match="does not match the plan digest"):
        await store.admit(connection, principal(), request("tamper"))


async def test_the_job_and_attempt_identity_is_persisted_and_stable(connection):
    """F3. The published budget key is `(job_id, attempt_id)`; both must be durable.

    `INTEGRATION-CONTRACT.md:296,368` (per #4912) specifies the domain budget hooks as
    idempotent on `(job_id, attempt_id)`. An operation store that persists neither
    cannot supply that key, so #5526 would have to invent a second identity or migrate
    a live table -- and backfilling an identifier other rows are already keyed on is
    the expensive kind of migration.

    Stability across an idempotent retry is the part that matters for the hook: if a
    retry minted a new job id, the same admitted work would reserve budget twice
    against two keys. It is stable because the retry returns the *stored* row, not
    because anything recomputes it.
    """
    store = OperationStore()
    admitted = await store.admit(connection, principal(), request("identity"))

    assert admitted.record.job_id
    assert admitted.record.attempt_id

    retried = await store.admit(connection, principal(), request("identity"))
    assert retried.created is False
    assert retried.record.job_id == admitted.record.job_id
    assert retried.record.attempt_id == admitted.record.attempt_id

    # Present in the outbox row too, denormalized: a worker reporting against the
    # budget key must be told the job, and a worker that had to join to
    # `harness_operations` would need read access to every operation's full record.
    row = await connection.fetchrow(
        "SELECT job_id, attempt_id FROM harness_dispatch_outbox"
        " WHERE operation_id = $1",
        admitted.record.operation_id,
    )
    assert row["job_id"] == admitted.record.job_id
    assert row["attempt_id"] == admitted.record.attempt_id


async def test_admission_and_outbox_row_are_both_written(connection):
    """The admission record and its dispatch row are both present after commit."""
    store = OperationStore()
    admitted = await store.admit(connection, principal(), request())

    operations = await connection.fetchval("SELECT count(*) FROM harness_operations")
    outbox = await connection.fetchrow(
        "SELECT operation_id, org_id, workspace_id, action, delivered_at"
        "  FROM harness_dispatch_outbox"
    )
    assert operations == 1
    assert outbox["operation_id"] == admitted.record.operation_id
    # Denormalized so a delivery worker never joins to the operations table.
    assert (outbox["org_id"], outbox["workspace_id"]) == ("org-a", "ws-1")
    assert outbox["delivered_at"] is None


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


async def test_concurrent_duplicate_creates_produce_exactly_one_operation(pool):
    """AC-01: genuine concurrency yields one operation, and both callers get it.

    Each coroutine uses its own connection, so they are actually concurrent rather
    than serialized by one connection. This is the test a SELECT-then-INSERT
    implementation fails: both would see nothing and both would insert.
    """
    store = OperationStore()

    async def admit():
        async with pool.acquire() as connection:
            return await store.admit(connection, principal(), request("race-key"))

    results = await asyncio.gather(*(admit() for _ in range(8)))

    async with pool.acquire() as reader:
        count = await reader.fetchval("SELECT count(*) FROM harness_operations")
        outbox = await reader.fetchval("SELECT count(*) FROM harness_dispatch_outbox")

    assert count == 1, f"expected one operation, found {count}"
    assert outbox == 1, f"expected one dispatch row, found {outbox}"
    # Every caller was handed the same operation -- not an error, and not a different
    # one. A retry must be able to proceed as if it had won.
    ids = {result.record.operation_id for result in results}
    assert len(ids) == 1
    # Exactly one call reports having created it; the rest report a landed retry.
    assert sum(1 for result in results if result.created) == 1


async def test_retry_with_same_payload_returns_the_same_operation(connection):
    """A sequential retry returns the original and reports created=False."""
    store = OperationStore()
    first = await store.admit(connection, principal(), request("k", size="small"))
    second = await store.admit(connection, principal(), request("k", size="small"))

    assert second.record.operation_id == first.record.operation_id
    assert first.created is True
    assert second.created is False, (
        "created=False is how a caller distinguishes 'the retry landed' from "
        "'this call wrote a second row'"
    )


async def test_retry_with_changed_payload_is_refused(connection):
    """The case this check exists for: a bigger envelope under an accepted key.

    Refused rather than honoured (which would be a silent budget increase) and
    refused rather than answered with the original (which would tell the caller its
    *new* request was accepted).
    """
    store = OperationStore()
    await store.admit(connection, principal(), request("k", size="small"))

    with pytest.raises(OperationRefused, match="different payload"):
        await store.admit(connection, principal(), request("k", size="enormous"))

    # And nothing was written by the refused attempt.
    assert await connection.fetchval("SELECT count(*) FROM harness_operations") == 1


async def test_changed_action_under_the_same_key_is_refused(connection):
    """A teardown must never be answered with a provision, or the reverse."""
    store = OperationStore()
    await store.admit(connection, principal(), request("k"))

    teardown = OperationRequest(action="teardown", idempotency_key="k")
    with pytest.raises(OperationRefused):
        await store.admit(connection, principal(), teardown)


async def test_two_tenants_may_hold_the_same_idempotency_key(pool):
    """The tenant is in the unique key, so a name collision is not a denial.

    A globally-unique key would let one tenant's row refuse another tenant's
    operation -- a cross-tenant denial of service through a derived name like
    `sp-aws-a100-1` that contains no tenant at all.
    """
    store = OperationStore()
    async with pool.acquire() as connection:
        first = await store.admit(connection, principal("org-a"), request("shared"))
        second = await store.admit(connection, principal("org-b"), request("shared"))

    assert first.record.operation_id != second.record.operation_id
    assert first.created and second.created


# ---------------------------------------------------------------------------
# Atomicity: crash before and after commit
# ---------------------------------------------------------------------------


async def test_crash_before_commit_leaves_nothing(pool):
    """AC-01: a failure inside the admission transaction writes neither row.

    Simulated by failing the transaction after both INSERTs have been issued but
    before the commit -- which is precisely the window a process death occupies. The
    property under test is that there is no state where the operation exists without
    its dispatch row, or vice versa.
    """
    store = OperationStore()

    class Boom(RuntimeError):
        pass

    async with pool.acquire() as connection:
        with pytest.raises(Boom):
            async with connection.transaction():
                await store.admit(connection, principal(), request("doomed"))
                # Inside the caller's wider transaction, so the store's own
                # transaction is a savepoint and this rolls back both its INSERTs.
                raise Boom("process died before commit")

    async with pool.acquire() as reader:
        operations = await reader.fetchval("SELECT count(*) FROM harness_operations")
        outbox = await reader.fetchval("SELECT count(*) FROM harness_dispatch_outbox")

    assert operations == 0, "an uncommitted admission left an operation row"
    assert outbox == 0, "an uncommitted admission left a dispatch row"


async def test_outbox_insert_failure_rolls_back_the_admission(pool):
    """If the outbox row cannot be written, the operation is not admitted either.

    Forced by dropping the outbox table's foreign-key target relationship -- here,
    by making the outbox INSERT fail with a NOT NULL violation via a trigger-free
    route: the table is renamed away, so the second statement errors. The point is
    the direction of the guarantee: a failure at (3b) must undo (3a).
    """
    store = OperationStore()
    async with pool.acquire() as connection:
        await connection.execute(
            "ALTER TABLE harness_dispatch_outbox RENAME TO harness_dispatch_outbox_x"
        )
        try:
            with pytest.raises(Exception) as caught:
                await store.admit(connection, principal(), request("no-outbox"))
            # Not a unique violation -- a real failure, propagated rather than
            # mistaken for a duplicate and answered with a stale record.
            assert not isinstance(caught.value, OperationRefused)
            operations = await connection.fetchval(
                "SELECT count(*) FROM harness_operations"
            )
            assert operations == 0, (
                "the admission survived a failed outbox insert: an operation that "
                "exists and will never be dispatched"
            )
        finally:
            await connection.execute(
                "ALTER TABLE harness_dispatch_outbox_x RENAME TO "
                "harness_dispatch_outbox"
            )


async def test_committed_admission_is_visible_after_the_writer_disconnects(pool):
    """AC-01, the other side of the crash: after commit, the work is not lost.

    The writing connection is closed without ceremony once `admit` returns. A crash
    immediately after commit must leave a findable, deliverable operation.
    """
    store = OperationStore()
    asyncpg = pytest.importorskip("asyncpg")
    from .conftest import postgres_url

    schema = await pool.fetchval("SELECT current_schema()")
    writer = await asyncpg.connect(
        postgres_url(), server_settings={"search_path": schema}
    )
    admitted = await store.admit(writer, principal(), request("committed"))
    await writer.close()  # the "crash"

    async with pool.acquire() as reader:
        found = await store.get(reader, principal(), admitted.record.operation_id)
        pending = await reader.fetchval(
            "SELECT count(*) FROM harness_dispatch_outbox WHERE delivered_at IS NULL"
        )

    assert found is not None
    assert pending == 1, "the committed operation has no deliverable dispatch row"


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------


async def test_another_tenant_cannot_read_the_operation(connection):
    """AC-01: cross-tenant reads return not-found, not forbidden.

    The same answer as a nonexistent operation, deliberately: a distinguishable
    "exists but forbidden" reply confirms the existence of another tenant's operation
    to someone who should not learn it.
    """
    store = OperationStore()
    admitted = await store.admit(connection, principal("org-a", "ws-1"), request())

    for other in (
        principal("org-b", "ws-1"),
        principal("org-a", "ws-2"),
        principal("org-b", "ws-2"),
    ):
        assert (
            await store.get(connection, other, admitted.record.operation_id) is None
        ), f"{other.org_id}/{other.workspace_id} could read another tenant's operation"


async def test_listing_and_key_lookup_are_tenant_scoped(connection):
    """The other two read paths are scoped the same way as `get`."""
    store = OperationStore()
    await store.admit(connection, principal("org-a"), request("a-key"))
    await store.admit(connection, principal("org-b"), request("b-key"))

    mine = await store.list_for_tenant(connection, principal("org-a"))
    assert [record.idempotency_key for record in mine] == ["a-key"]

    assert (
        await store.get_by_idempotency_key(connection, principal("org-a"), "b-key")
        is None
    )


async def test_principal_without_the_permission_is_refused(connection):
    """Admission requires the permission the port declares."""
    store = OperationStore()
    with pytest.raises(OperationRefused, match=REQUIRED_PERMISSION):
        await store.admit(connection, principal(permitted=False), request())

    assert await connection.fetchval("SELECT count(*) FROM harness_operations") == 0


# ---------------------------------------------------------------------------
# State transitions
# ---------------------------------------------------------------------------


async def test_transition_requires_the_version_it_read(connection):
    """Optimistic concurrency: a stale writer loses rather than overwrites.

    Without this, a stale `running` can overwrite a terminal `succeeded` -- which is
    how a finished operation gets retried, and for these operations a retry is
    duplicated cloud spend.
    """
    store = OperationStore()
    admitted = await store.admit(connection, principal(), request())
    record = admitted.record

    updated = await store.transition(
        connection,
        record.operation_id,
        expected_version=record.version,
        state=OperationState.RUNNING,
    )
    assert updated.state is OperationState.RUNNING
    assert updated.version == record.version + 1

    # The stale writer, still holding the original version.
    with pytest.raises(ConcurrentUpdate):
        await store.transition(
            connection,
            record.operation_id,
            expected_version=record.version,
            state=OperationState.SUCCEEDED,
        )

    # And the earlier conclusion stands.
    current = await store.get(connection, principal(), record.operation_id)
    assert current is not None and current.state is OperationState.RUNNING


async def test_unknown_is_terminal_but_not_a_failure(connection):
    """A poll loop must terminate on UNKNOWN without reading it as failure.

    Collapsing the two either leaks resources believed never created, or retries a
    provision that actually succeeded.
    """
    store = OperationStore()
    admitted = await store.admit(connection, principal(), request())
    concluded = await store.transition(
        connection,
        admitted.record.operation_id,
        expected_version=admitted.record.version,
        state=OperationState.UNKNOWN,
    )

    assert concluded.is_terminal is True
    assert concluded.state is OperationState.UNKNOWN
    assert concluded.state is not OperationState.FAILED
