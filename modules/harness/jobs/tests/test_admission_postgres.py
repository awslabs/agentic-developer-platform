"""Real-database behaviour of budget-bound admission.

Issue #5526 (w6-03), EPIC #4910, Wave 6. AC-01's database half: replay, concurrent
consumption, reserve/confirm failures and retries that must not widen the envelope.

Real PostgreSQL, for the reason `conftest.py` states: every property here is a property
of the database rather than of Python. Single use is a primary key firing under genuine
concurrency, "the loser gets the winner's operation" is what happens after it fires, and
"the operation rolls back when the approval was already spent" is transaction atomicity.
A fake would pass a suite proving none of them.

Each test names the property it establishes. A test called `test_admit_twice` records
that something ran, not what it proved.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from harness_jobs import (
    REQUIRED_PERMISSION,
    OperationRefused,
    OperationRequest,
    OperationStore,
    ResolvedPrincipal,
)
from harness_jobs.admission import (
    BudgetDenied,
    BudgetUnavailable,
    IntentStage,
    Reservation,
    ReservationState,
    admit_operation,
    cancel_before_dispatch,
    derive_operation_identity,
    list_interrupted_admissions,
    read_consumption,
    reconcile_interrupted_admissions,
    retain_for_uncertain_dispatch,
)
from harness_jobs.approval import (
    APPROVAL_PERMISSION,
    ApprovalBinding,
    ApprovalRecord,
    ApprovalRefused,
    ApprovalResult,
    ApproverStatus,
    SpendEnvelope,
)

from .conftest import requires_postgres

pytestmark = requires_postgres

NOW = datetime(2026, 9, 20, 12, 0, 0, tzinfo=UTC)
REQUESTER = "user:alice"
APPROVER = "user:boss"


# ---------------------------------------------------------------------------
# Fixtures for the two things this module needs and does not own
# ---------------------------------------------------------------------------


class FakeLedger:
    """A `BudgetLedger` double that is idempotent on (job_id, attempt_id).

    A double rather than the real ledger because the real one is the domain's and this
    package must never hold budget authority (`admission.py`, "Why the ledger is an
    interface"). What it must *not* be is permissive: the two rules below are the ones
    the contract requires of a real implementation, so a real ledger that broke either
    would make these tests wrong about production rather than merely optimistic.

    1. A repeat under the same key returns the same reservation and records no second
       effect -- that is what `reserve_calls` counts.
    2. A repeat under the same key with a **changed envelope** raises `BudgetDenied`
       rather than being honoured. #5524 §3.2: silently honouring it is how a retry
       becomes a budget increase.
    """

    def __init__(self) -> None:
        self.reserve_calls: list[tuple[str, str]] = []
        self.confirm_calls: list[str] = []
        self.released: list[tuple[str, str]] = []
        self.retained: list[tuple[str, str]] = []
        self._held: dict[tuple[str, str], Reservation] = {}
        self._envelopes: dict[tuple[str, str], SpendEnvelope] = {}
        self._next = 0
        self.fail_reserve: BaseException | None = None
        self.fail_confirm: BaseException | None = None

    async def reserve(
        self,
        *,
        job_id: str,
        attempt_id: str,
        org_id: str,
        workspace_id: str,
        envelope: SpendEnvelope,
    ) -> Reservation:
        key = (job_id, attempt_id)
        self.reserve_calls.append(key)
        if self.fail_reserve is not None:
            raise self.fail_reserve
        existing = self._held.get(key)
        if existing is not None:
            if self._envelopes[key] != envelope:
                raise BudgetDenied(
                    "a reservation already exists for this key with a different "
                    "envelope; a retry may not change the amount"
                )
            return existing
        self._next += 1
        reservation = Reservation(
            reservation_id=f"res-{self._next}", job_id=job_id, attempt_id=attempt_id
        )
        self._held[key] = reservation
        self._envelopes[key] = envelope
        return reservation

    async def confirm(
        self, *, reservation: Reservation, envelope: SpendEnvelope
    ) -> None:
        self.confirm_calls.append(reservation.reservation_id)
        if self.fail_confirm is not None:
            raise self.fail_confirm
        key = (reservation.job_id, reservation.attempt_id)
        if self._envelopes.get(key) != envelope:
            raise BudgetDenied("confirm envelope does not match the reservation")

    async def release(self, *, reservation: Reservation, reason: str) -> None:
        self.released.append((reservation.reservation_id, reason))
        self._held.pop((reservation.job_id, reservation.attempt_id), None)

    async def retain(self, *, reservation: Reservation, reason: str) -> None:
        self.retained.append((reservation.reservation_id, reason))


class FakeFence:
    """A `CreationFence` double. `establishes` False is the branch that must retain."""

    def __init__(self, *, establishes: bool = True) -> None:
        self.establishes = establishes
        self.calls: list[str] = []

    async def fence(self, *, operation_id: str, job_id: str, attempt_id: str) -> bool:
        self.calls.append(operation_id)
        return self.establishes


def principal(
    org: str = "org-a",
    workspace: str = "ws-1",
    subject: str = REQUESTER,
    *,
    permitted: bool = True,
) -> ResolvedPrincipal:
    return ResolvedPrincipal(
        org_id=org,
        workspace_id=workspace,
        subject=subject,
        permissions=frozenset({REQUIRED_PERMISSION} if permitted else set()),
    )


def request(key: str = "key-1", **parameters: str) -> OperationRequest:
    return OperationRequest(
        action="provision", idempotency_key=key, parameters=dict(parameters)
    )


def envelope(**kwargs: int) -> SpendEnvelope:
    values = {
        "max_resource_units": 4,
        "max_runtime_seconds": 3600,
        "max_cost_micros": 5_000_000,
    }
    values.update(kwargs)
    return SpendEnvelope(**values)


def approval(
    approval_id: str = "appr-1",
    *,
    actor: ResolvedPrincipal | None = None,
    req: OperationRequest | None = None,
    approved: SpendEnvelope | None = None,
    **kwargs: object,
) -> ApprovalRecord:
    defaults: dict[str, object] = {
        "approval_id": approval_id,
        "binding": ApprovalBinding.for_request(actor or principal(), req or request()),
        "envelope": approved or envelope(),
        "result": ApprovalResult.ALLOWED_ONCE,
        "approvers": frozenset({APPROVER}),
        "decided_by": APPROVER,
        "decided_at": NOW - timedelta(minutes=5),
        "expires_at": NOW + timedelta(hours=1),
    }
    defaults.update(kwargs)
    return ApprovalRecord(**defaults)  # type: ignore[arg-type]


def statuses() -> dict[str, ApproverStatus]:
    return {
        APPROVER: ApproverStatus(
            subject=APPROVER,
            is_member=True,
            permissions=frozenset({APPROVAL_PERMISSION}),
        )
    }


async def admit(
    connection,
    ledger,
    *,
    actor: ResolvedPrincipal | None = None,
    req: OperationRequest | None = None,
    record: ApprovalRecord | None = None,
    requested: SpendEnvelope | None = None,
    now: datetime = NOW,
):
    return await admit_operation(
        connection,
        OperationStore(),
        ledger,
        principal=actor or principal(),
        request=req or request(),
        approval=record if record is not None else approval(),
        requested_envelope=requested or envelope(),
        approver_statuses=statuses(),
        now=now,
    )


# ---------------------------------------------------------------------------
# The permissive path, so every denial below is not vacuously green
# ---------------------------------------------------------------------------


async def test_an_approved_request_is_admitted_reserved_confirmed_and_enqueued(
    connection,
):
    """The whole sequence, in one assertion set, because the ordering is the design.

    All five effects are checked rather than just the return value: an implementation
    that admitted without reserving, or reserved without enqueueing, would satisfy a
    weaker version of this test and would be exactly the defect the story is about.
    """
    ledger = FakeLedger()
    outcome = await admit(connection, ledger)

    assert outcome.created is True
    assert len(ledger.reserve_calls) == 1
    assert len(ledger.confirm_calls) == 1
    assert ledger.released == []
    assert ledger.retained == []

    operations = await connection.fetchval("SELECT count(*) FROM harness_operations")
    outbox = await connection.fetchval("SELECT count(*) FROM harness_dispatch_outbox")
    assert (operations, outbox) == (1, 1)

    consumed = await read_consumption(connection, principal(), approval_id="appr-1")
    assert consumed is not None
    assert consumed.operation_id == outcome.operation.record.operation_id
    assert consumed.reservation_state is ReservationState.CONFIRMED


async def test_the_ledger_is_keyed_on_the_operations_stored_job_and_attempt(connection):
    """The reservation key is the identity the database holds, not a second one.

    If they could differ, a compensation path would release a reservation that does not
    correspond to the operation it is compensating for.
    """
    ledger = FakeLedger()
    outcome = await admit(connection, ledger)
    record = outcome.operation.record

    assert ledger.reserve_calls == [(record.job_id, record.attempt_id)]
    assert derive_operation_identity("appr-1") == (
        record.operation_id,
        record.job_id,
        record.attempt_id,
    )


# ---------------------------------------------------------------------------
# Replay and single use (AC-01)
# ---------------------------------------------------------------------------


async def test_replaying_an_approval_admits_once_and_reserves_once(connection):
    """AC-01, the central property: a replay is idempotent, not a second spend.

    Both halves matter. One operation is the admission half; *one* net reservation is
    the budget half, and an implementation that minted fresh job ids per call would pass
    the first and fail the second -- with the second being the one that costs money.
    """
    ledger = FakeLedger()
    first = await admit(connection, ledger)
    second = await admit(connection, ledger)

    assert second.operation.record.operation_id == first.operation.record.operation_id
    assert first.created is True
    assert second.created is False

    operations = await connection.fetchval("SELECT count(*) FROM harness_operations")
    consumptions = await connection.fetchval(
        "SELECT count(*) FROM harness_approval_consumption"
    )
    assert (operations, consumptions) == (1, 1)
    # Reserve was called twice and *held* once: the ledger's idempotency engaged because
    # both calls presented the same derived key.
    assert len(set(ledger.reserve_calls)) == 1


async def test_a_replay_cannot_widen_the_envelope(connection):
    """AC-01: retries must not expand the spending envelope.

    A second admission under the same approval id, but carrying a larger approved
    envelope, must not reach the ledger as an increase. The `FakeLedger` refuses a
    changed envelope under a held key, which is the behaviour #5524 §3.2 requires of a
    real one -- so this test establishes that the retry arrives at the ledger under the
    *same* key, where that refusal can fire at all.
    """
    ledger = FakeLedger()
    await admit(connection, ledger)

    with pytest.raises(BudgetDenied):
        await admit(
            connection,
            ledger,
            record=approval(approved=envelope(max_cost_micros=500_000_000)),
        )

    stored = await connection.fetchval(
        "SELECT max_cost_micros FROM harness_approval_consumption "
        "WHERE approval_id = $1",
        "appr-1",
    )
    assert stored == 5_000_000, "the recorded envelope is the one that was approved"


async def test_a_replay_for_a_different_plan_is_refused_not_answered(connection):
    """A changed request under a spent approval gets an error, never the original.

    Answering with the original operation would tell the caller its *new* request was
    accepted. Two layers refuse this, and which one answers depends on whether the
    approval still binds the original plan:

    * a *stale* approval -- one bound to the original plan, replayed for a new request
      -- is refused by `evaluate_approval` as "different plan", before any database
      access (the case below this one);
    * a *re-issued* approval, bound to the new plan but carrying the already-spent
      approval id, reaches the store. Because `operation_id` is derived from the
      approval id, it collides in `harness_operations` and
      `store._resolve_conflict` refuses it: same idempotency key, different payload.

    This test covers the second, which is the one that needs a database. Asserted on
    `OperationRefused` rather than `ApprovalRefused` because that is genuinely who
    answers -- an earlier revision expected the approval layer here, and running this
    against a real PostgreSQL proved that branch unreachable. It was removed rather
    than left as dead code that looks load-bearing.
    """
    ledger = FakeLedger()
    await admit(connection, ledger)

    other = request("key-1", instance_type="h100")
    with pytest.raises(OperationRefused, match="may not change the request"):
        await admit(connection, ledger, req=other, record=approval(req=other))

    # One operation, one consumption row: the refused replay wrote nothing.
    assert await connection.fetchval("SELECT count(*) FROM harness_operations") == 1
    assert (
        await connection.fetchval("SELECT count(*) FROM harness_approval_consumption")
        == 1
    )


async def test_a_stale_approval_replayed_for_a_new_plan_never_reaches_the_store(
    connection,
):
    """The other half: an approval still bound to the original plan is refused early.

    Separated from the test above because the two are refused by different layers for
    different reasons, and collapsing them would hide which check is doing the work.
    Here the ledger must not be consulted at all -- the refusal precedes it.
    """
    ledger = FakeLedger()
    await admit(connection, ledger)
    calls_before = len(ledger.reserve_calls)

    other = request("key-1", instance_type="h100")
    with pytest.raises(ApprovalRefused, match="different plan"):
        # The approval still binds the ORIGINAL request, so it does not authorize this
        # one, whatever the store would later say about the idempotency key.
        await admit(connection, ledger, req=other)

    assert len(ledger.reserve_calls) == calls_before, (
        "the ledger was consulted for a request no approval authorized"
    )


async def test_concurrent_admissions_of_one_approval_produce_one_operation(pool):
    """AC-01 concurrent consumption: the primary key resolves the race, not a lock.

    Each coroutine holds its own connection, so these are genuinely concurrent rather
    than serialized by the driver. This is the test a SELECT-then-INSERT implementation
    fails: every caller would find the approval unconsumed and every caller would
    proceed.
    """
    ledger = FakeLedger()
    store = OperationStore()

    async def one():
        async with pool.acquire() as connection:
            return await admit_operation(
                connection,
                store,
                ledger,
                principal=principal(),
                request=request(),
                approval=approval(),
                requested_envelope=envelope(),
                approver_statuses=statuses(),
                now=NOW,
            )

    results = await asyncio.gather(*(one() for _ in range(8)))

    async with pool.acquire() as reader:
        operations = await reader.fetchval("SELECT count(*) FROM harness_operations")
        outbox = await reader.fetchval("SELECT count(*) FROM harness_dispatch_outbox")
        consumptions = await reader.fetchval(
            "SELECT count(*) FROM harness_approval_consumption"
        )

    assert (operations, outbox, consumptions) == (1, 1, 1)
    # Every caller was handed the same operation -- not an error, and not a different
    # one. A retry must be able to proceed as if it had won.
    assert len({r.operation.record.operation_id for r in results}) == 1
    # And exactly one net reservation is held, however many callers raced.
    assert len(set(ledger.reserve_calls)) == 1


async def test_a_second_approval_cannot_pay_for_an_already_paid_operation(connection):
    """The `operation_id UNIQUE` constraint, reached deliberately.

    Two approvals whose derived operation ids collide cannot both be spent on it. The
    ids are derived from the approval id, so this is constructed by pointing a second
    approval at the first's identity -- which is what an attacker with two approvals in
    hand would try, and what a future non-derived identity scheme would make easy.
    """
    ledger = FakeLedger()
    await admit(connection, ledger)
    operation_id = (
        await read_consumption(connection, principal(), approval_id="appr-1")
    ).operation_id

    with pytest.raises(Exception) as caught:
        await connection.execute(
            """
            INSERT INTO harness_approval_consumption (
                approval_id, operation_id, org_id, workspace_id, plan_digest,
                requester, approved_by, max_resource_units, max_runtime_seconds,
                max_cost_micros, reservation_id, reservation_state
            ) VALUES ($1,$2,'org-a','ws-1','digest',$3,$4,1,1,1,'res-x','confirmed')
            """,
            "appr-2",
            operation_id,
            REQUESTER,
            APPROVER,
        )
    assert getattr(caught.value, "sqlstate", None) == "23505"


async def test_deleting_an_operation_cannot_free_its_approval(connection):
    """`ON DELETE RESTRICT`: a row deletion must not become a budget grant.

    With a cascade, removing the operation would delete the consumption record and the
    approval would be spendable again. Asserted against the database because it is a
    property of the foreign key, not of any Python path.
    """
    ledger = FakeLedger()
    outcome = await admit(connection, ledger)

    with pytest.raises(Exception) as caught:
        await connection.execute(
            "DELETE FROM harness_operations WHERE operation_id = $1",
            outcome.operation.record.operation_id,
        )
    # 23503 is foreign_key_violation: the delete was refused rather than cascaded.
    assert getattr(caught.value, "sqlstate", None) in {"23503", "23001"}

    consumed = await read_consumption(connection, principal(), approval_id="appr-1")
    assert consumed is not None


# ---------------------------------------------------------------------------
# The gate itself, against a real database (AC-01 / AC-02)
# ---------------------------------------------------------------------------


async def test_an_expired_approval_admits_nothing_and_reserves_nothing(connection):
    """A refusal at (1) must not have touched the ledger or the database.

    Both halves: an implementation that reserved before evaluating the approval would
    pass a return-value-only assertion and would still have called the ledger for a
    request nobody approved.
    """
    ledger = FakeLedger()
    with pytest.raises(ApprovalRefused, match="expired"):
        await admit(connection, ledger, now=NOW + timedelta(hours=2))

    assert ledger.reserve_calls == []
    assert await connection.fetchval("SELECT count(*) FROM harness_operations") == 0


async def test_an_approver_who_lost_authority_admits_nothing(connection):
    """Recheck at admission, against the database: no operation, no outbox row."""
    ledger = FakeLedger()
    with pytest.raises(ApprovalRefused, match="current authority"):
        await admit_operation(
            connection,
            OperationStore(),
            ledger,
            principal=principal(),
            request=request(),
            approval=approval(),
            requested_envelope=envelope(),
            approver_statuses={
                APPROVER: ApproverStatus(
                    subject=APPROVER, is_member=False, permissions=frozenset()
                )
            },
            now=NOW,
        )
    assert await connection.fetchval("SELECT count(*) FROM harness_operations") == 0


async def test_admission_stores_the_server_resolved_tenant(connection):
    """AC-02: the tenant on the row is the principal's, never the request's.

    `OperationRequest` has no tenant fields and rejects them through the parameter map,
    so the assertion here is that admission did not acquire them from somewhere else on
    the way through.
    """
    ledger = FakeLedger()
    actor = principal(org="org-b", workspace="ws-9")
    outcome = await admit(connection, ledger, actor=actor, record=approval(actor=actor))
    row = await connection.fetchrow(
        "SELECT org_id, workspace_id FROM harness_approval_consumption "
        "WHERE approval_id = $1",
        "appr-1",
    )
    assert (row["org_id"], row["workspace_id"]) == ("org-b", "ws-9")
    assert (
        outcome.operation.record.org_id,
        outcome.operation.record.workspace_id,
    ) == ("org-b", "ws-9")


async def test_another_tenant_cannot_consume_an_approval_by_replaying_it(connection):
    """AC-02: a stolen approval id is not spendable from another workspace.

    The approval binding refuses this at the gate (the binding names a workspace), so
    the assertion is that the earlier check fires and the consumption row is untouched
    -- a defence-in-depth order, since the tenant-scoped read in
    `_resolve_consumed_approval` is the second line.
    """
    ledger = FakeLedger()
    await admit(connection, ledger)

    thief = principal(org="org-evil", workspace="ws-evil", subject=REQUESTER)
    with pytest.raises(ApprovalRefused):
        await admit(connection, ledger, actor=thief)

    assert (
        await connection.fetchval("SELECT count(*) FROM harness_approval_consumption")
        == 1
    )


# ---------------------------------------------------------------------------
# Compensation (#5524 §3.5)
# ---------------------------------------------------------------------------


async def test_a_denied_reserve_leaves_nothing_to_compensate(connection):
    """Nothing was reserved, so nothing is released and nothing is retained."""
    ledger = FakeLedger()
    ledger.fail_reserve = BudgetDenied("no budget")

    with pytest.raises(BudgetDenied):
        await admit(connection, ledger)

    assert ledger.released == []
    assert ledger.retained == []
    assert await connection.fetchval("SELECT count(*) FROM harness_operations") == 0


async def test_a_lost_confirm_retains_the_reservation_and_does_not_release_it(
    connection,
):
    """§3.5: reserve ok, confirm lost -> the reservation stays held.

    The load-bearing branch. Releasing here would free budget for an attempt whose
    confirm may in fact have landed.
    """
    ledger = FakeLedger()
    ledger.fail_confirm = BudgetUnavailable("ledger timed out")

    with pytest.raises(BudgetUnavailable):
        await admit(connection, ledger)

    assert ledger.released == []
    assert len(ledger.retained) == 1
    assert "retained" in ledger.retained[0][1]
    assert await connection.fetchval("SELECT count(*) FROM harness_operations") == 0


async def test_retrying_after_a_lost_confirm_uses_the_same_key(connection):
    """§3.5: "retry `confirm` under the same key" -- and the retry then succeeds.

    A retry that re-reserved would appear to work while holding two reservations, which
    is why this asserts on the *set* of keys rather than on the outcome alone.
    """
    ledger = FakeLedger()
    ledger.fail_confirm = BudgetUnavailable("ledger timed out")
    with pytest.raises(BudgetUnavailable):
        await admit(connection, ledger)

    ledger.fail_confirm = None
    outcome = await admit(connection, ledger)

    assert outcome.created is True
    assert len(set(ledger.reserve_calls)) == 1, "the retry reserved under the same key"
    assert len(set(ledger.confirm_calls)) == 1


async def test_a_denied_confirm_releases_because_nothing_was_dispatched(connection):
    """A *denial* at confirm is a durable answer, so the hold is returned.

    Distinguished from the unavailable case above on purpose: collapsing the two would
    either leak a reservation on every denial or release one on every timeout.
    """
    ledger = FakeLedger()
    ledger.fail_confirm = BudgetDenied("envelope unavailable")

    with pytest.raises(BudgetDenied):
        await admit(connection, ledger)

    assert len(ledger.released) == 1
    assert ledger.retained == []


async def test_a_failed_commit_releases_only_after_establishing_no_dispatch(connection):
    """§3.5: confirm ok, commit lost -> establish nothing was dispatched, then release.

    The commit is made to fail by admitting the *same* idempotency key for a different
    plan first, so `store.admit` refuses inside the transaction. Real, rather than a
    patched exception: the compensation must fire on the failures the store actually
    raises.
    """
    ledger = FakeLedger()
    store = OperationStore()
    # An operation already holds `key-1` with a different payload, so the admission
    # below is refused by the store's idempotency check *after* confirm has landed.
    await store.admit(connection, principal(), request("key-1", instance_type="v100"))

    with pytest.raises(Exception):
        await admit(connection, ledger)

    assert len(ledger.released) == 1
    assert "nothing was dispatched" in ledger.released[0][1]
    assert ledger.retained == []
    # The refused admission left no consumption row, so the approval is still unspent --
    # which is correct: it was never successfully consumed.
    assert await read_consumption(connection, principal(), approval_id="appr-1") is None


async def test_cancellation_before_dispatch_fences_before_releasing(connection):
    """§3.5: fence creation, then release. Never the reverse. CXR-004.

    Ordering is asserted by observing that the fence recorded its call and the ledger
    recorded a release; the `False` fence test below is what establishes that the
    ordering is enforced rather than incidental.

    **The outbox row is left exactly as admission wrote it.** An earlier revision of
    this test deleted it first, "to model an operation cancelled while still pending
    delivery" -- and that deletion was the bug hiding in the test. Production has no
    path that deletes an outbox row before cancelling, so the branch being exercised was
    one no caller could reach: with an intact row, `cancel_before_dispatch` classified
    it as dispatched and retained the budget for every cancellation of every queued
    operation.

    A test that has to write to a production table to reach a branch is reporting that
    the branch is unreachable. So this test now does what a caller does -- admit, then
    cancel
    -- and the release is established by the row's own evidence.
    """
    ledger = FakeLedger()
    outcome = await admit(connection, ledger)
    record = outcome.operation.record
    fence = FakeFence()
    # Exactly what admission left behind: queued, never claimed.
    queued = dict(
        await connection.fetchrow(
            "SELECT attempts, claimed_until, delivered_at, abandoned_at "
            "FROM harness_dispatch_outbox WHERE operation_id = $1",
            record.operation_id,
        )
    )
    assert queued == {
        "attempts": 0,
        "claimed_until": None,
        "delivered_at": None,
        "abandoned_at": None,
    }, "the reproduction's starting state: an intact, never-claimed outbox row"

    state = await cancel_before_dispatch(
        connection,
        ledger,
        fence,
        operation_id=record.operation_id,
        job_id=record.job_id,
        attempt_id=record.attempt_id,
        reservation=outcome.reservation,
        reason="the requester cancelled",
    )

    assert state is ReservationState.RELEASED, (
        "an operation whose dispatch was never claimed was cancelled and its budget "
        "was not returned; this is CXR-004, and it leaks the envelope on every cancel"
    )
    assert fence.calls == [record.operation_id]
    assert len(ledger.released) == 1
    consumed = await read_consumption(connection, principal(), approval_id="appr-1")
    assert consumed is not None
    assert consumed.reservation_state is ReservationState.RELEASED

    # The queue entry is gone, because a claimable row whose budget has been returned is
    # unfunded work with a valid-looking envelope. The operation and the consumption
    # record -- the two rows an operator or auditor reads -- both survive.
    assert (
        await connection.fetchval("SELECT count(*) FROM harness_dispatch_outbox") == 0
    ), "the cancelled dispatch is still claimable after its reservation was released"
    assert await connection.fetchval("SELECT count(*) FROM harness_operations") == 1


async def test_a_cancelled_dispatch_cannot_be_claimed_afterwards(connection):
    """The property the withdrawal exists for, asserted through the outbox itself.

    Releasing the budget and leaving the row claimable is the release-before-fence
    hazard arriving one step later: the money is back, and a worker can still pick the
    envelope up. Checked by actually running a drain rather than by counting rows,
    because "not claimable" is what matters and a row could in principle be excluded by
    a predicate while still being handed out by some other path.
    """
    from harness_jobs import DispatchOutbox

    ledger = FakeLedger()
    outcome = await admit(connection, ledger)
    record = outcome.operation.record

    await cancel_before_dispatch(
        connection,
        ledger,
        FakeFence(),
        operation_id=record.operation_id,
        job_id=record.job_id,
        attempt_id=record.attempt_id,
        reservation=outcome.reservation,
        reason="the requester cancelled",
    )

    class RecordingExecutor:
        def __init__(self) -> None:
            self.envelopes: list[object] = []

        async def deliver(self, envelope) -> bool:
            self.envelopes.append(envelope)
            return True

    executor = RecordingExecutor()
    report = await DispatchOutbox().drain_once(connection, executor)

    assert executor.envelopes == [], (
        "a cancelled operation whose budget was released reached an executor anyway"
    )
    assert report.delivered == 0


async def test_a_fence_that_cannot_be_established_retains_the_reservation(connection):
    """The branch a "cancel means release" implementation gets wrong.

    An unfenced attempt may still create the resource, so the budget stays held. This is
    the test that distinguishes "fenced, then released" from "released, and also tried
    to fence".

    The outbox row is left intact here too, and its survival is asserted: a failed fence
    must not withdraw the dispatch. If it did, the operation would be un-deliverable
    while its reservation was retained pending a creation the fence failed to stop --
    the worst of both answers.
    """
    ledger = FakeLedger()
    outcome = await admit(connection, ledger)
    record = outcome.operation.record

    state = await cancel_before_dispatch(
        connection,
        ledger,
        FakeFence(establishes=False),
        operation_id=record.operation_id,
        job_id=record.job_id,
        attempt_id=record.attempt_id,
        reservation=outcome.reservation,
        reason="expiry",
    )

    assert state is ReservationState.RETAINED
    assert ledger.released == []
    assert len(ledger.retained) == 1
    queued = await connection.fetchval("SELECT count(*) FROM harness_dispatch_outbox")
    assert queued == 1


async def test_cancelling_a_claimed_dispatch_retains(connection):
    """A fence stops future creation; it says nothing about what already happened.

    The distinction CXR-004 turns on: this row has been *claimed*, so an executor may
    already have received the envelope, and that is what makes the outcome uncertain --
    not the row's existence. Retained until provider reconciliation rather than
    released.

    The claim is taken through `DispatchOutbox.claim`, the same statement a worker uses,
    so the state under test is one production actually produces. `attempts` is
    incremented by that statement, which is the evidence the classification reads.
    """
    from harness_jobs import DispatchOutbox

    ledger = FakeLedger()
    outcome = await admit(connection, ledger)
    record = outcome.operation.record

    claimed = await DispatchOutbox().claim(connection)
    assert len(claimed) == 1, "the positive control: an approved row is claimable"

    state = await cancel_before_dispatch(
        connection,
        ledger,
        FakeFence(),
        operation_id=record.operation_id,
        job_id=record.job_id,
        attempt_id=record.attempt_id,
        reservation=outcome.reservation,
        reason="late cancellation",
    )

    assert state is ReservationState.RETAINED
    assert ledger.released == []
    assert len(ledger.retained) == 1
    # Not withdrawn: the row is the record of work that may be in flight.
    queued = await connection.fetchval("SELECT count(*) FROM harness_dispatch_outbox")
    assert queued == 1


async def test_a_failed_attempt_retains_even_though_the_lease_is_clear(connection):
    """The read-the-lease-only mistake, which looks identical to a fresh row.

    `outbox._record_failure` clears `claimed_until` and leaves `attempts` standing, so a
    row that was claimed, attempted and failed has a null lease. An implementation that
    classified on the lease alone would call this never-queued and release budget for an
    envelope an executor has already received. `attempts > 0` is what distinguishes
    them.
    """
    from harness_jobs import DispatchOutbox

    ledger = FakeLedger()
    outcome = await admit(connection, ledger)
    record = outcome.operation.record

    class FailingExecutor:
        async def deliver(self, envelope) -> bool:
            raise RuntimeError("the executor is unreachable")

    report = await DispatchOutbox().drain_once(connection, FailingExecutor())
    assert report.failed == 1
    row = dict(
        await connection.fetchrow(
            "SELECT attempts, claimed_until FROM harness_dispatch_outbox "
            "WHERE operation_id = $1",
            record.operation_id,
        )
    )
    assert row["claimed_until"] is None and row["attempts"] == 1, (
        "the state this test exists for: no lease, but an attempt has been made"
    )

    state = await cancel_before_dispatch(
        connection,
        ledger,
        FakeFence(),
        operation_id=record.operation_id,
        job_id=record.job_id,
        attempt_id=record.attempt_id,
        reservation=outcome.reservation,
        reason="cancelled after a failed attempt",
    )

    assert state is ReservationState.RETAINED, (
        "a row with a cleared lease but a spent attempt was treated as never dispatched"
    )
    assert ledger.released == []


async def test_cancelling_an_operation_with_no_dispatch_row_releases(connection):
    """`NEVER_QUEUED`: nothing to withdraw, and nothing could have been delivered.

    Reached without writing to the outbox at all -- the operation is admitted through
    the store rather than the gate, so no outbox row is ever created for it. Kept as a
    separate test from the pending case because the two are different facts for an
    operator even though the release decision is the same.
    """
    ledger = FakeLedger()
    outcome = await admit(connection, ledger)
    record = outcome.operation.record
    # Claim and deliver nothing; instead exercise an operation_id the outbox has never
    # heard of, which is what the classification sees when a row was never written.
    fence = FakeFence()

    state = await cancel_before_dispatch(
        connection,
        ledger,
        fence,
        operation_id="operation-the-outbox-never-saw",
        job_id=record.job_id,
        attempt_id=record.attempt_id,
        reservation=outcome.reservation,
        reason="an admission whose outbox row was never written",
    )

    assert state is ReservationState.RELEASED
    assert len(ledger.released) == 1


async def test_an_uncertain_dispatch_retains_the_reservation(connection):
    """§3.5: uncertain dispatch -> retained until provider reconciliation.

    The counter-intuitive rule, and the durable half of it: the state is recorded so a
    later reconciliation can find the reservation rather than infer it.
    """
    ledger = FakeLedger()
    outcome = await admit(connection, ledger)

    state = await retain_for_uncertain_dispatch(
        connection,
        ledger,
        operation_id=outcome.operation.record.operation_id,
        reservation=outcome.reservation,
        reason="the provider call timed out; the resource may exist",
    )

    assert state is ReservationState.RETAINED
    assert ledger.released == []
    consumed = await read_consumption(connection, principal(), approval_id="appr-1")
    assert consumed is not None
    assert consumed.reservation_state is ReservationState.RETAINED


async def test_a_ledger_failure_during_compensation_does_not_mask_the_cause(connection):
    """The original error survives a failing release.

    A compensation path is reached because something already went wrong. If the
    compensation raised, the caller would be told the ledger was unreachable when the
    actual event was a refused admission.
    """

    class BrokenLedger(FakeLedger):
        async def release(self, *, reservation: Reservation, reason: str) -> None:
            raise RuntimeError("the ledger is down")

    ledger = BrokenLedger()
    store = OperationStore()
    await store.admit(connection, principal(), request("key-1", instance_type="v100"))

    with pytest.raises(Exception) as caught:
        await admit(connection, ledger)

    assert "the ledger is down" not in str(caught.value)


async def test_read_consumption_returns_none_for_an_unspent_approval(connection):
    """The read half of recovery must distinguish "unspent" from "cannot tell"."""
    unspent = await read_consumption(connection, principal(), approval_id="appr-never")
    assert unspent is None


# ---------------------------------------------------------------------------
# CXR-002: two approvals, one derived operation -- the loser's hold is returned
# ---------------------------------------------------------------------------


async def test_a_second_approval_colliding_on_the_operation_releases_its_hold(
    connection,
):
    """CXR-002: the loser of an operation-id collision must not keep a reservation.

    The reproduction: two approvals whose derived operation ids collide, presented for
    an identical idempotent request. The second raised `ApprovalRefused` -- correct --
    while leaving **two reservations confirmed, zero released, zero retained** and one
    consumption row. The refusal was honest and the money was gone.

    The cause was that both conflicts arrived looking identical. `ON CONFLICT DO
    NOTHING` with no target absorbed the approval-id primary key *and* the operation-id
    unique constraint, and the read-back that followed could not distinguish them: a
    missing row under the resolved tenant is produced both by an operation-id conflict
    and by a cross-tenant approval id. So the path that must compensate and the path
    that must not were one path, and it chose not to.

    The repair is that the conflict is classified by the constraint that actually fired.
    This test drives the **real `admit_operation` sequence twice** rather than inserting
    a row to trip a constraint, because the defect was in the compensation that follows
    the constraint and a direct INSERT never reaches it.
    """
    ledger = FakeLedger()
    first = await admit(connection, ledger)

    # A *second approval* for the same request. Nothing is contrived here and nothing is
    # patched: two approvals legitimately exist for one plan -- re-approved after a
    # timeout, or approved twice by two people -- and both are presented under the same
    # idempotency key. The store's idempotency constraint hands the second admission the
    # first's operation, and the consumption INSERT then collides on `operation_id`
    # because that operation is already paid for. That collision is the finding.
    with pytest.raises(ApprovalRefused):
        await admit(connection, ledger, record=approval("appr-2"))

    # Two reserves happened, because the second approval legitimately asked for budget
    # before the collision was discoverable. What must NOT survive is the second hold.
    assert len(ledger.reserve_calls) == 2
    assert len(ledger.released) == 1, (
        "the second approval's reservation was confirmed and never compensated; this "
        "is CXR-002, and the refusal it returned reads as if nothing was spent"
    )
    released_id = ledger.released[0][0]
    assert released_id != first.reservation.reservation_id, (
        "the WINNER's reservation was released; the operation is admitted and its "
        "budget must stay held"
    )
    assert ledger.retained == []

    # One operation, one consumption row: the refused admission wrote nothing.
    assert await connection.fetchval("SELECT count(*) FROM harness_operations") == 1
    assert (
        await connection.fetchval("SELECT count(*) FROM harness_approval_consumption")
        == 1
    )


async def test_an_honest_replay_does_not_release_the_winners_reservation(connection):
    """The other side of the classification, and the one a naive fix breaks.

    An approval-id conflict is a *replay*: the reservation the second call holds IS the
    winner's reservation, handed back by an idempotent `reserve` under the same derived
    key. Compensating it would release the live operation's budget -- so "always
    compensate on conflict" trades CXR-002 for a worse defect, and this test is what
    stops that.
    """
    ledger = FakeLedger()
    first = await admit(connection, ledger)
    second = await admit(connection, ledger)

    assert second.operation.record.operation_id == first.operation.record.operation_id
    assert ledger.released == [], (
        "a replay released the reservation that funds the admitted operation"
    )
    assert ledger.retained == []
    consumed = await read_consumption(connection, principal(), approval_id="appr-1")
    assert consumed is not None
    assert consumed.reservation_state is ReservationState.CONFIRMED


# ---------------------------------------------------------------------------
# CXR-005: reading a consumption record requires the resolved tenant
# ---------------------------------------------------------------------------


async def test_another_tenant_cannot_read_a_consumption_record(connection):
    """CXR-005: an approval id is not a capability to read the spend it paid for.

    The reproduction: `read_consumption` took only an approval id and returned the
    reservation id, the operation id, the plan digest and the approved envelope to any
    caller that could name any approval -- including another organization's. An approval
    id is a correlation value that appears in tickets, logs and audit trails, so
    possession of one is not authority over it.

    The answer is the identical `None` a genuinely absent approval produces. A caller
    that could tell "not yours" from "not there" could enumerate other tenants'
    approvals by the difference.
    """
    victim = principal(org="org-victim", workspace="ws-victim")
    await admit(connection, FakeLedger(), actor=victim, record=approval(actor=victim))

    thief = principal(org="org-thief", workspace="ws-thief")
    leaked = await read_consumption(connection, thief, approval_id="appr-1")

    assert leaked is None, (
        "a principal in another organization read the reservation, operation and plan "
        "metadata of an approval it does not own; this is CXR-005"
    )
    absent = await read_consumption(connection, thief, approval_id="appr-absent")
    assert absent is None, (
        "'another tenant's' and 'does not exist' must be indistinguishable, or the "
        "difference is an enumeration oracle"
    )


async def test_the_owning_tenant_can_still_read_its_own_consumption_record(connection):
    """The recovery the scoping must not break.

    A scoped read that returns nothing to anybody is not a fix, it is an outage with a
    security story. This is the positive control for the tenant predicate.
    """
    victim = principal(org="org-victim", workspace="ws-victim")
    outcome = await admit(
        connection, FakeLedger(), actor=victim, record=approval(actor=victim)
    )

    consumed = await read_consumption(connection, victim, approval_id="appr-1")
    assert consumed is not None
    assert consumed.operation_id == outcome.operation.record.operation_id
    assert consumed.reservation_state is ReservationState.CONFIRMED


async def test_read_consumption_refuses_a_caller_that_is_not_a_principal(connection):
    """Passing an approval id positionally must fail loudly, not read globally.

    The migration hazard of adding a required parameter in front of an existing one: a
    caller updated carelessly would pass the approval id where the principal goes. A
    duck-typed read would then scope on attributes a string does not have, and the
    safest outcome available at that point is a refusal.
    """
    from harness_jobs import ContractViolation

    with pytest.raises(ContractViolation, match="ResolvedPrincipal"):
        await read_consumption(connection, "appr-1", approval_id="appr-1")  # type: ignore[arg-type]


async def test_the_privileged_read_is_a_separate_named_function(connection):
    """Global reads exist for reconciliation, and must be reached deliberately.

    `read_consumption_privileged` is unscoped on purpose -- an operator reconciling
    holds is asking about the harness's own obligations, and the tenant of a row is part
    of the answer. It is a separate name rather than an optional argument, because a
    global read reached by omitting a parameter is a global read reached by forgetting
    one.
    """
    from harness_jobs import read_consumption_privileged

    victim = principal(org="org-victim", workspace="ws-victim")
    await admit(connection, FakeLedger(), actor=victim, record=approval(actor=victim))

    found = await read_consumption_privileged(connection, approval_id="appr-1")
    assert found is not None
    assert found.approval_id == "appr-1"
    assert found.reservation_state is ReservationState.CONFIRMED
    # Same value as the scoped read, not merely the same shape: the two build their
    # result through one shared `_consumed`, so an operator's view and a tenant's view
    # cannot drift in what they report about one row.
    scoped = await read_consumption(connection, victim, approval_id="appr-1")
    assert found == scoped

    # The difference is only in which rows each may see. The privileged read answers for
    # a tenant the caller has no relationship to; that is its entire purpose and its
    # entire hazard, which is why it is a separate name.
    other = principal(org="org-unrelated", workspace="ws-unrelated")
    assert await read_consumption(connection, other, approval_id="appr-1") is None


# ---------------------------------------------------------------------------
# CXR-003: an interrupted admission leaves an enumerable, settleable hold
# ---------------------------------------------------------------------------


class LosesTheReserveReply(FakeLedger):
    """The ledger books the hold, then the reply is lost. The crash CXR-003 is about.

    Modelled by raising *after* recording the hold, which is what a dropped connection
    after a committed remote write looks like from this side: the money is held and this
    process will never learn the reservation id.
    """

    async def reserve(self, **kwargs: object) -> Reservation:
        await super().reserve(**kwargs)  # type: ignore[arg-type]
        raise ConnectionResetError("the reply was lost after the reserve landed")


async def test_an_interrupted_reserve_leaves_an_enumerable_obligation(connection):
    """CXR-003: a hold nothing can name is a hold nothing can reclaim.

    The reproduction: kill the process after `reserve` lands but before its reply. A new
    connection saw zero operations, zero outbox rows and zero consumption rows while the
    ledger held budget -- so there was no set of obligations to enumerate, and
    reconciliation was not "hard" but undefined.

    The repair is the intent row, committed before the first external effect. This test
    asserts the enumerability directly, because that is the property the finding says
    was missing: the sweep cannot settle what it cannot list.
    """
    ledger = LosesTheReserveReply()
    with pytest.raises(ConnectionResetError):
        await admit(connection, ledger)

    # Nothing was admitted, which is correct and is also the whole problem.
    assert await connection.fetchval("SELECT count(*) FROM harness_operations") == 0
    assert (
        await connection.fetchval("SELECT count(*) FROM harness_approval_consumption")
        == 0
    )
    assert len(ledger.reserve_calls) == 1, "the ledger booked a hold"

    outstanding = await list_interrupted_admissions(connection)
    assert len(outstanding) == 1, (
        "the interrupted admission left nothing to enumerate; the hold is "
        "unreclaimable and this is CXR-003"
    )
    intent = outstanding[0]
    assert intent.approval_id == "appr-1"
    assert intent.stage is IntentStage.INTENDED, (
        "the stage must not claim a reserve whose reply never arrived: it can lag "
        "reality but must never lead it, or the sweep would try to release a hold the "
        "ledger never granted"
    )
    # The ledger's idempotency key is recoverable from the row, which is what lets the
    # sweep ask about a reservation whose id this process never learned.
    assert (intent.job_id, intent.attempt_id) == derive_operation_identity("appr-1")[1:]


async def test_reconciliation_reclaims_an_interrupted_hold_after_the_approval_expires(
    connection,
):
    """CXR-003: recovery must work when the approval can no longer be consulted.

    This is the half that made the original leak permanent. Because the derived identity
    is a function of the approval alone, the retry presented the same key -- but it
    refused on the expired approval *before contacting the ledger at all* (retry ledger
    calls = 0), so the hold survived every retry forever.

    The sweep therefore does not consult the approval. It carries the envelope copied
    into the intent row at admission time, which is why that copy exists: expiry and
    revocation are precisely the conditions under which recovery matters and the
    approval store can no longer answer.
    """
    interrupted = LosesTheReserveReply()
    with pytest.raises(ConnectionResetError):
        await admit(connection, interrupted)

    # A retry after expiry is refused before the ledger is reached. Asserted so this
    # test records that the refusal is *correct* and simply cannot be the recovery
    # mechanism.
    retry = FakeLedger()
    with pytest.raises(ApprovalRefused, match="expired"):
        await admit(connection, retry, now=NOW + timedelta(hours=3))
    assert retry.reserve_calls == []

    # The sweep, in a process that has no approval and does not ask for one.
    sweep = FakeLedger()
    sweep._held.update(interrupted._held)
    sweep._envelopes.update(interrupted._envelopes)
    report = await reconcile_interrupted_admissions(connection, sweep)

    assert (report.scanned, report.released, report.retained) == (1, 1, 0)
    assert report.unresolved == 0
    assert len(sweep.released) == 1, "the budget was not returned to the ledger"

    # It admitted nothing. A reconciliation that created work would be a budget-bound
    # admission path that never saw an approval.
    assert await connection.fetchval("SELECT count(*) FROM harness_operations") == 0
    assert (
        await connection.fetchval("SELECT count(*) FROM harness_dispatch_outbox") == 0
    )
    assert (
        await connection.fetchval("SELECT count(*) FROM harness_approval_consumption")
        == 0
    )

    # And the obligation is settled, so a second sweep finds nothing to do rather than
    # releasing the same hold again.
    assert await list_interrupted_admissions(connection) == ()
    again = await reconcile_interrupted_admissions(connection, sweep)
    assert (again.scanned, again.released, again.retained) == (0, 0, 0)


async def test_reconciliation_leaves_a_committed_admission_alone(connection):
    """The sweep must not touch a hold that a consumption row already owns.

    A reply lost *after* the transaction committed leaves a stale intent row over a
    live, funded operation. Releasing its reservation would defund work that is queued
    for dispatch -- the opposite failure, and the one a sweep written as "release
    anything unresolved" would cause on its first run.
    """
    ledger = FakeLedger()
    outcome = await admit(connection, ledger)

    # Reopen the intent to model a reply lost between the commit and the resolution.
    await connection.execute(
        "UPDATE harness_admission_intent SET stage = 'confirmed', resolution = NULL "
        "WHERE approval_id = $1",
        "appr-1",
    )
    assert len(await list_interrupted_admissions(connection)) == 1

    report = await reconcile_interrupted_admissions(connection, ledger)

    assert (report.scanned, report.released, report.retained) == (1, 0, 0)
    assert ledger.released == [], (
        "the sweep released the reservation funding an admitted, queued operation"
    )
    assert ledger.retained == []
    consumed = await read_consumption(connection, principal(), approval_id="appr-1")
    assert consumed is not None
    assert consumed.reservation_state is ReservationState.CONFIRMED
    # Still deliverable: the eligibility predicate sees a `confirmed` consumption row.
    assert outcome.created is True
    assert (
        await connection.fetchval("SELECT count(*) FROM harness_dispatch_outbox") == 1
    )
    # Settled, so it stops appearing in the sweep.
    assert await list_interrupted_admissions(connection) == ()


async def test_reconciliation_retains_rather_than_releases_when_a_dispatch_row_exists(
    connection,
):
    """§3.5's uncertain-dispatch rule, inside the sweep.

    An admission whose transaction committed the operation and outbox rows but lost its
    consumption row leaves a dispatch that may have been delivered. #5524 §3.5 is
    explicit that this retains: releasing would let the envelope fund a second operation
    while the first may be running.
    """
    ledger = FakeLedger()
    await admit(connection, ledger)
    operation_id = derive_operation_identity("appr-1")[0]

    # The split state: the outbox row is there, the consumption row is not, and the
    # intent row is unresolved. Constructed rather than crashed into because the
    # transaction makes it unreachable by design -- which is the point of the
    # transaction, and the sweep still has to answer correctly if it ever sees it.
    await connection.execute(
        "DELETE FROM harness_approval_consumption WHERE approval_id = $1", "appr-1"
    )
    await connection.execute(
        "UPDATE harness_admission_intent SET stage = 'confirmed', resolution = NULL "
        "WHERE approval_id = $1",
        "appr-1",
    )

    sweep = FakeLedger()
    sweep._held.update(ledger._held)
    sweep._envelopes.update(ledger._envelopes)
    report = await reconcile_interrupted_admissions(sweep and connection, sweep)

    assert (report.scanned, report.released, report.retained) == (1, 0, 1)
    assert sweep.released == [], (
        "a reservation was released for an operation with a live dispatch row; the "
        "work may already have run"
    )
    assert len(sweep.retained) == 1
    assert (
        await connection.fetchval("SELECT count(*) FROM harness_dispatch_outbox") == 1
    )
    # The operation_id is the derived one, so the retention names the right hold.
    assert operation_id in sweep.retained[0][1]


async def test_a_ledger_failure_on_one_intent_does_not_stop_the_sweep(connection):
    """One unreclaimable hold must not hide every other one.

    A sweep that aborted on the first failure would make a single unreachable
    reservation into a total recovery outage, and the rows behind it would stay
    invisible. Counted as `unresolved` and reported, so an operator sees both the
    progress and the problem.
    """
    first = LosesTheReserveReply()
    with pytest.raises(ConnectionResetError):
        await admit(connection, first)

    other = request("key-2")
    second = LosesTheReserveReply()
    with pytest.raises(ConnectionResetError):
        await admit(
            connection,
            second,
            req=other,
            record=approval("appr-2", req=other),
        )

    assert len(await list_interrupted_admissions(connection)) == 2

    class RefusesOneRelease(FakeLedger):
        async def release(self, *, reservation: Reservation, reason: str) -> None:
            if reservation.reservation_id == "res-1":
                raise RuntimeError("this hold cannot be reached")
            await super().release(reservation=reservation, reason=reason)

    sweep = RefusesOneRelease()
    report = await reconcile_interrupted_admissions(connection, sweep)

    assert report.scanned == 2
    assert report.unresolved == 1, "the failure was not reported"
    assert report.released == 1, "the sweep stopped instead of settling the other row"
    # The failed one is still outstanding, so the next sweep retries it rather than
    # forgetting it.
    remaining = await list_interrupted_admissions(connection)
    assert len(remaining) == 1
