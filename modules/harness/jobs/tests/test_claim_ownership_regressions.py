"""Claim-ownership regressions, written against the pre-repair public API only.

Issue #5525 (w6-02), EPIC #4910, Wave 6. Companions to the F5/F6 tests in
`test_outbox_postgres.py`, kept separate because of a deliberate constraint: **not
one line here names `claim_generation`, `abandoned_at` or `recover_abandoned`.**

That constraint is what makes these tests evidence. A test that asserts a new
column exists fails on any revision lacking the column, which shows the test is new
and not that anything was broken. These use only the API the reviewed head already
had, so the failures they produced at 6c3e6a29 were behavioural:

* an expired claim marked its successor's row delivered -- erasing an in-flight
  delivery obligation on behalf of a worker that delivered nothing;
* an expired claim concluded an operation UNKNOWN while another worker was actively
  delivering it;
The third finding, F5's abandoned final claim, is *not* here. The same API-only
probe showed the harm at the reviewed head -- a row at the attempt cap, unsettled
and unclaimable, with its operation PENDING and no process that would ever conclude
it -- but the fix is deliberately a separate maintenance sweep rather than something
a routine drain does, so the property cannot be stated without naming the sweep. Its
permanent test is `test_a_crashed_final_claim_reaches_a_durable_unknown` in
`test_outbox_postgres.py`; keeping a copy here that asserts a plain drain resolves it
would encode the opposite of the intended design.

They also outlive the mechanism. If a later story replaces the generation counter
with some other fence, these still describe the property that must hold, while the
mechanism-specific tests would need rewriting.
"""

from __future__ import annotations

from harness_jobs import DispatchOutbox, OperationState, OperationStore

from .conftest import requires_postgres
from .test_store_postgres import principal, request

pytestmark = requires_postgres


async def _expire(connection):
    await connection.execute(
        "UPDATE harness_dispatch_outbox SET claimed_until = now() - interval '1 hour'"
        " WHERE delivered_at IS NULL"
    )


async def test_an_expired_claim_must_not_mark_its_successors_row_delivered(connection):
    store = OperationStore()
    outbox = DispatchOutbox(store=store, max_attempts=5)
    await store.admit(connection, principal(), request("f6a"))

    stale = (await outbox.claim(connection))[0]
    await _expire(connection)
    fresh = (await outbox.claim(connection))[0]
    assert fresh.outbox_id == stale.outbox_id

    await outbox._mark_delivered(connection, stale)

    delivered_at = await connection.fetchval(
        "SELECT delivered_at FROM harness_dispatch_outbox WHERE id = $1",
        stale.outbox_id,
    )
    assert delivered_at is None, (
        "an EXPIRED claim marked the row delivered; worker B's in-flight delivery "
        "obligation was erased by a worker that delivered nothing"
    )


async def test_an_expired_claim_must_not_conclude_an_active_operation(connection):
    store = OperationStore()
    outbox = DispatchOutbox(store=store, max_attempts=5)
    admitted = await store.admit(connection, principal(), request("f6b"))

    stale = (await outbox.claim(connection))[0]
    await _expire(connection)
    fresh = (await outbox.claim(connection))[0]
    assert fresh.outbox_id == stale.outbox_id

    await outbox._mark_undeliverable(connection, stale)

    current = await store.get(connection, principal(), admitted.record.operation_id)
    assert current is not None
    assert current.state is OperationState.PENDING, (
        f"an expired worker concluded the operation as {current.state.name} while "
        "worker B was actively delivering it"
    )
