"""The facade: the surface consumers already declare.

Issue #5525 (w6-02), EPIC #4910, Wave 6. AC-02 (deployable API persists operations
across restart) and the port's "acts as the facade's own resolved principal, never the
request body's org_id".

Updated for #5526 (w6-03): this facade admits through `admit_operation`, so every test
here supplies an approval source and a ledger. That is not test scaffolding for its own
sake -- the facade is the port the provisioning path actually calls, and a facade that
could admit without an approval was the reproduced CXR-001 bypass.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from harness_jobs import (
    APPROVAL_PERMISSION,
    REQUIRED_PERMISSION,
    ApprovalBinding,
    ApprovalContext,
    ApprovalRecord,
    ApprovalRefused,
    ApprovalResult,
    ApproverStatus,
    ContractViolation,
    OperationFacadeService,
    OperationRefused,
    OperationState,
    OperationStore,
    OperationUnavailable,
    Reservation,
    ResolvedPrincipal,
    SpendEnvelope,
)

from .conftest import requires_postgres

pytestmark = requires_postgres

APPROVER = "user:boss"
ENVELOPE = SpendEnvelope(
    max_resource_units=4, max_runtime_seconds=3600, max_cost_micros=5_000_000
)


class ApprovingSource:
    """Approves whatever it is asked about, for the principal that asked.

    Models a configured approval store that has a current approval on file. The approval
    id is **derived from the request's idempotency key**, which is what makes the
    facade's retry-collapse tests meaningful: an approval authorizes one request, so two
    identical calls present the same approval and therefore the same derived operation
    identity. An id that varied per call would model an approval store that issues a new
    approval for every retry, and the facade would then be tested against a source no
    real deployment would have.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def approval_for(self, *, principal, request) -> ApprovalContext:
        self.calls.append(request.idempotency_key)
        return ApprovalContext(
            record=ApprovalRecord(
                approval_id=f"appr-{request.idempotency_key}",
                binding=ApprovalBinding.for_request(principal, request),
                envelope=ENVELOPE,
                result=ApprovalResult.ALLOWED_ONCE,
                approvers=frozenset({APPROVER}),
                decided_by=APPROVER,
                decided_at=datetime.now(UTC) - timedelta(minutes=5),
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            ),
            requested_envelope=ENVELOPE,
            approver_statuses={
                APPROVER: ApproverStatus(
                    subject=APPROVER,
                    is_member=True,
                    permissions=frozenset({APPROVAL_PERMISSION}),
                )
            },
        )


class UnapprovedSource:
    """Has no approval on file. Absence is not permission."""

    async def approval_for(self, *, principal, request) -> ApprovalContext:
        return ApprovalContext(
            record=None, requested_envelope=ENVELOPE, approver_statuses={}
        )


class BrokenApprovalSource:
    """Raises, modelling an unreachable approval store.

    Distinct from `UnapprovedSource` on purpose: an unanswered question is not a
    refusal, and the facade must not report one as the other.
    """

    async def approval_for(self, *, principal, request) -> ApprovalContext:
        raise ConnectionError("the approval store is unreachable")


class FakeLedger:
    """An idempotent ledger double that records what it was asked to do.

    Idempotent on `(job_id, attempt_id)` as the Protocol requires, so a retry through
    the facade gets the same reservation rather than a second hold -- which is the
    property the facade's retry tests would silently stop covering if this double minted
    a new reservation per call.
    """

    def __init__(self) -> None:
        self.reserved: dict[tuple[str, str], Reservation] = {}
        self.confirmed: list[str] = []
        self.released: list[str] = []
        self.retained: list[str] = []

    async def reserve(self, *, job_id, attempt_id, org_id, workspace_id, envelope):
        key = (job_id, attempt_id)
        if key not in self.reserved:
            self.reserved[key] = Reservation(
                reservation_id=f"res-{len(self.reserved) + 1}",
                job_id=job_id,
                attempt_id=attempt_id,
            )
        return self.reserved[key]

    async def confirm(self, *, reservation, envelope):
        self.confirmed.append(reservation.reservation_id)

    async def release(self, *, reservation, reason):
        self.released.append(reservation.reservation_id)

    async def retain(self, *, reservation, reason):
        self.retained.append(reservation.reservation_id)


class FixedResolver:
    """Resolves to one principal, ignoring its arguments.

    Ignoring them is the point: a resolver models an authentication layer, and an
    authentication layer does not take the tenant from the request. What the facade
    then does with the *disagreement* is the property under test.
    """

    def __init__(self, principal: ResolvedPrincipal | None) -> None:
        self.principal = principal
        self.calls: list[dict[str, str]] = []

    async def resolve(self, *, org_id: str, workspace_id: str, permission: str):
        self.calls.append(
            {"org_id": org_id, "workspace_id": workspace_id, "permission": permission}
        )
        return self.principal


class BrokenResolver:
    """Raises, modelling an unreachable identity provider."""

    async def resolve(self, *, org_id: str, workspace_id: str, permission: str):
        raise ConnectionError("the identity provider is unreachable")


class WrongShapeResolver:
    """Returns something that is not a `ResolvedPrincipal`."""

    async def resolve(self, *, org_id: str, workspace_id: str, permission: str):
        return {"org_id": "org-a", "workspace_id": "ws-1"}


def principal(org: str = "org-a", workspace: str = "ws-1", *, permitted: bool = True):
    return ResolvedPrincipal(
        org_id=org,
        workspace_id=workspace,
        subject="user-1",
        permissions=frozenset({REQUIRED_PERMISSION} if permitted else set()),
    )


def facade(connect, resolver, *, approvals=None, ledger=None):
    """A facade wired to an approval source and a ledger, as a real one must be.

    Both default to approving/idempotent doubles so the tests whose subject is something
    else (tenant scoping, refusal translation, payload round-tripping) read as they did
    before the gate was inserted. The tests whose subject *is* the gate pass their own.
    """
    return OperationFacadeService(
        connect=connect,
        resolver=resolver,
        approvals=approvals or ApprovingSource(),
        ledger=ledger or FakeLedger(),
    )


async def open_default(service, **overrides):
    payload = {
        "action": "provision",
        "workspace_id": "ws-1",
        "org_id": "org-a",
        "permission": REQUIRED_PERMISSION,
        "parameters": {},
    }
    payload.update(overrides)
    return await service.open_operation(**payload)


# ---------------------------------------------------------------------------
# The declared surface
# ---------------------------------------------------------------------------


async def test_open_operation_returns_a_progress_report_and_persists_the_record(
    connect, connection
):
    """The port's create path: durable record, consumer-shaped answer."""
    service = facade(connect, FixedResolver(principal()))

    progress = await open_default(service)

    assert progress.state == OperationState.PENDING.value
    assert progress.is_terminal is False
    assert progress.is_conclusive_success is False
    assert progress.is_conclusive_failure is False

    stored = await connection.fetchrow(
        "SELECT org_id, workspace_id, state FROM harness_operations WHERE"
        " operation_id = $1",
        progress.operation_id,
    )
    assert stored is not None
    assert (stored["org_id"], stored["workspace_id"]) == ("org-a", "ws-1")


async def test_report_progress_reads_the_store_not_a_cached_field(connect, connection):
    """The facade holds no status of its own; a changed row changes the report."""
    service = facade(connect, FixedResolver(principal()))
    progress = await open_default(service)

    await connection.execute(
        "UPDATE harness_operations SET state = $1, version = version + 1"
        " WHERE operation_id = $2",
        OperationState.SUCCEEDED.value,
        progress.operation_id,
    )

    again = await service.report_progress(progress.operation_id)
    assert again.state == OperationState.SUCCEEDED.value
    assert again.is_conclusive_success is True


async def test_an_absent_operation_raises_unavailable_rather_than_returning_none(
    connect,
):
    """The port's mandated unknown answer: raise, never synthesize.

    A `None` would be indistinguishable from "no such operation" and a synthesized
    report would be the facade making a claim about an operation it could not read --
    which is the "reports success while nothing was provisioned" failure the whole
    arrangement exists to prevent.
    """
    service = facade(connect, FixedResolver(principal()))
    with pytest.raises(OperationUnavailable):
        await service.report_progress("00000000-0000-0000-0000-000000000000")


async def test_a_retry_through_the_facade_returns_the_same_operation(connect):
    """Two identical calls collapse to one operation.

    This is why the key is *derived* from the payload when the caller supplies none. A
    freshly-minted UUID per call would make every retry a new operation -- precisely
    the duplicate this story exists to prevent, reintroduced by the convenience of not
    requiring a key.
    """
    service = facade(connect, FixedResolver(principal()))
    first = await open_default(service, parameters={"size": "small"})
    second = await open_default(service, parameters={"size": "small"})

    assert first.operation_id == second.operation_id


async def test_a_caller_supplied_idempotency_key_is_honoured(connect):
    service = facade(connect, FixedResolver(principal()))
    first = await open_default(service, parameters={"idempotency_key": "mine"})
    second = await open_default(service, parameters={"idempotency_key": "mine"})

    assert first.operation_id == second.operation_id


async def test_a_changed_payload_under_the_same_key_is_refused(connect):
    service = facade(connect, FixedResolver(principal()))
    await open_default(service, parameters={"idempotency_key": "mine", "size": "small"})
    with pytest.raises(OperationRefused):
        await open_default(
            service, parameters={"idempotency_key": "mine", "size": "enormous"}
        )


async def test_a_different_payload_without_a_key_is_a_different_operation(connect):
    """Derived keys must still separate genuinely different requests."""
    service = facade(connect, FixedResolver(principal()))
    small = await open_default(service, parameters={"size": "small"})
    large = await open_default(service, parameters={"size": "enormous"})

    assert small.operation_id != large.operation_id


# ---------------------------------------------------------------------------
# The facade admits through the gate (#5526 CXR-001)
# ---------------------------------------------------------------------------


async def test_the_facade_will_not_construct_without_an_approval_source(connect):
    """The bypass, closed at the constructor rather than at the request.

    CXR-001 reproduced a facade that called `store.admit` directly, so the port the
    provisioning path actually calls admitted operations with no approval and no
    reservation. The repair is not a check inside `open_operation` -- a misconfigured
    facade must fail to exist, so it fails at startup instead of on the first real
    provisioning request.
    """
    with pytest.raises(ContractViolation, match="requires an ApprovalSource"):
        OperationFacadeService(connect=connect, resolver=FixedResolver(principal()))

    with pytest.raises(ContractViolation, match="requires a BudgetLedger"):
        OperationFacadeService(
            connect=connect,
            resolver=FixedResolver(principal()),
            approvals=ApprovingSource(),
        )


async def test_an_unapproved_request_is_refused_and_writes_nothing(connect, connection):
    """No approval on file means no operation, no outbox row and no ledger call.

    Absence is not permission, and the refusal has to be *before* the durable write:
    an operation row that exists because the approval store had nothing on file is the
    unpaid row CXR-001 found being delivered.
    """
    ledger = FakeLedger()
    service = facade(
        connect, FixedResolver(principal()), approvals=UnapprovedSource(), ledger=ledger
    )

    with pytest.raises(ApprovalRefused):
        await open_default(service)

    assert await connection.fetchval("SELECT count(*) FROM harness_operations") == 0
    queued = await connection.fetchval("SELECT count(*) FROM harness_dispatch_outbox")
    assert queued == 0
    assert ledger.reserved == {}


async def test_an_unreachable_approval_store_is_unavailable_not_refused(
    connect, connection
):
    """An unanswered question is not a refusal, and it still admits nothing.

    Reported as `OperationUnavailable` because the approval may well exist -- telling
    the caller "refused" would have them stop retrying something a retry would allow.
    The same split the resolver path already makes.
    """
    service = facade(
        connect, FixedResolver(principal()), approvals=BrokenApprovalSource()
    )

    with pytest.raises(OperationUnavailable, match="could not be established"):
        await open_default(service)

    assert await connection.fetchval("SELECT count(*) FROM harness_operations") == 0


async def test_an_approved_request_reserves_confirms_and_records_consumption(
    connect, connection
):
    """The positive control: approved work is held, confirmed, written and payable.

    Deliberately paired with the refusal tests above. A control that only ever refuses
    is satisfied by a facade that refuses everything, so the repair has to be shown to
    still admit the work it is supposed to admit -- through the ledger, in order, with
    the consumption row that makes the operation claimable.
    """
    ledger = FakeLedger()
    service = facade(connect, FixedResolver(principal()), ledger=ledger)

    progress = await open_default(service)

    assert len(ledger.reserved) == 1
    assert ledger.confirmed == ["res-1"]
    assert ledger.released == [] and ledger.retained == []

    consumption = await connection.fetchrow(
        "SELECT org_id, workspace_id, reservation_state FROM"
        " harness_approval_consumption WHERE operation_id = $1",
        progress.operation_id,
    )
    assert consumption is not None
    assert (consumption["org_id"], consumption["workspace_id"]) == ("org-a", "ws-1")
    assert consumption["reservation_state"] == "confirmed"


# ---------------------------------------------------------------------------
# Authority
# ---------------------------------------------------------------------------


async def test_the_stored_tenant_is_the_resolved_one_not_the_argument(
    connect, connection
):
    """The port entry's rule, demonstrated: the arguments are not a source of truth.

    The resolver returns `org-real`; the call names `org-a`. The facade refuses rather
    than storing either silently. Refusing on disagreement -- rather than quietly
    preferring the resolved value -- means a mismatch is a visible error instead of a
    request that operated on a different workspace than the caller named.
    """
    resolver = FixedResolver(principal("org-real", "ws-real"))
    service = facade(connect, resolver)

    with pytest.raises(OperationRefused, match="does not match"):
        await open_default(service, org_id="org-a", workspace_id="ws-1")

    assert await connection.fetchval("SELECT count(*) FROM harness_operations") == 0
    # The boundary values were still passed to the resolver, so an implementation can
    # check entitlement against them -- they are an assertion to verify, not authority.
    assert resolver.calls[0]["org_id"] == "org-a"


async def test_a_principal_without_the_permission_is_refused(connect, connection):
    service = facade(connect, FixedResolver(principal(permitted=False)))
    with pytest.raises(OperationRefused, match=REQUIRED_PERMISSION):
        await open_default(service)
    assert await connection.fetchval("SELECT count(*) FROM harness_operations") == 0


async def test_an_unresolvable_principal_is_refused(connect):
    service = facade(connect, FixedResolver(None))
    with pytest.raises(OperationRefused, match="could not be resolved"):
        await open_default(service)


async def test_a_wrong_permission_string_is_refused(connect):
    """Asking for the check to be done against the wrong authority is refused."""
    service = facade(connect, FixedResolver(principal()))
    with pytest.raises(OperationRefused, match="workspace:provision"):
        await open_default(service, permission="workspace:read")


async def test_a_resolver_returning_the_wrong_shape_is_a_contract_violation(connect):
    """A dict would bypass the identifier validation the principal performs."""
    service = facade(connect, WrongShapeResolver())
    with pytest.raises(ContractViolation, match="ResolvedPrincipal"):
        await open_default(service)


async def test_an_unreachable_resolver_is_unavailable_not_refused(connect):
    """The distinction matters: refused means stop, unavailable means retry later.

    Mapping an unreachable identity provider to a refusal would tell a caller its
    request was rejected on its merits, and it would stop retrying something that will
    succeed once the provider is back.
    """
    service = facade(connect, BrokenResolver())
    with pytest.raises(OperationUnavailable):
        await open_default(service)


async def test_an_identity_asserting_parameter_is_refused_at_the_facade(connect):
    """The smuggling refusal is reachable through the public entry point."""
    service = facade(connect, FixedResolver(principal()))
    with pytest.raises(ContractViolation, match="must not assert an identity"):
        await open_default(service, parameters={"org_id": "org-b"})


# ---------------------------------------------------------------------------
# Tenant-scoped reads
# ---------------------------------------------------------------------------


async def test_the_scoped_read_hides_another_tenants_operation(connect):
    """`report_progress_for` answers cross-tenant exactly as it answers nonexistent."""
    service = facade(connect, FixedResolver(principal("org-a")))
    progress = await open_default(service)

    mine = await service.report_progress_for(principal("org-a"), progress.operation_id)
    assert mine.operation_id == progress.operation_id

    with pytest.raises(OperationUnavailable):
        await service.report_progress_for(principal("org-b"), progress.operation_id)


async def test_the_declared_read_hides_another_tenants_operation(connect):
    """F1. The *declared* `report_progress` is tenant-scoped, not just the `_for` twin.

    The test above covers `report_progress_for`, which had no callers. This covers
    `report_progress(operation_id)` -- the method the consumer's Protocol declares,
    the one `services/provisioning.py:390` calls and the one the boot capability
    probe exercises. It resolved a principal and then queried by operation id alone,
    so a caller holding another tenant's id read that operation's state and detail.

    Two services over one database, resolving to two different tenants, is the
    shape of the actual deployment: same table, different authenticated context.
    Asking org-b's facade for org-a's operation must answer exactly as it answers a
    nonexistent id -- a distinguishable reply is itself the disclosure.
    """
    mine = facade(connect, FixedResolver(principal("org-a")))
    theirs = facade(connect, FixedResolver(principal("org-b")))
    progress = await open_default(mine)

    # The owner still gets its own answer -- the scoping must not be a blanket deny.
    assert (await mine.report_progress(progress.operation_id)).operation_id == (
        progress.operation_id
    )

    with pytest.raises(OperationUnavailable):
        await theirs.report_progress(progress.operation_id)


async def test_the_declared_read_refuses_when_no_principal_resolves(connect):
    """F1, second half: no authenticated context means no answer at all.

    A resolver returning `None` models a request that reached the facade without a
    verified identity. Refused rather than unavailable: the facade is working, the
    caller simply has no entitlement to a report. Answering with the record -- which
    is what querying by operation id alone amounts to -- would make the unscoped
    read reachable by anyone who could reach the port.
    """
    service = facade(connect, FixedResolver(principal("org-a")))
    progress = await open_default(service)

    anonymous = facade(connect, FixedResolver(None))
    with pytest.raises(OperationRefused):
        await anonymous.report_progress(progress.operation_id)


async def test_the_declared_read_refuses_a_resolved_but_unpermitted_principal(connect):
    """F9. Tenant scope is not authority; the read must check both.

    The gap this closes was live: a resolver returning a correctly-scoped
    `ResolvedPrincipal` with an **empty permission set** was accepted, and
    `report_progress` returned that tenant's operation state and detail. Reproduced
    against a real database before the fix.

    The reason it read as covered is the asymmetry: `_resolve`, on the admission path,
    checks `may_provision`; `_resolve_acting_principal`, on the read path, checked only
    that the resolver returned the right *type*. The published `operation_facade`
    contract governs this port by `workspace:provision`, so belonging to the right
    tenant does not establish holding the right permission -- two questions, one asked.

    Note the resolver here is the same `FixedResolver` the permitted tests use,
    differing only in `permitted=False`. A bespoke resolver that always supplied the
    needed permission is precisely what could not have caught this.
    """
    owner = facade(connect, FixedResolver(principal("org-a")))
    progress = await open_default(owner)

    unpermitted = facade(connect, FixedResolver(principal("org-a", permitted=False)))
    with pytest.raises(OperationRefused):
        await unpermitted.report_progress(progress.operation_id)


async def test_the_declared_read_answers_a_permitted_principal(connect):
    """The control for the test above: the check must not deny everyone.

    Stated separately because a permission check that refuses unconditionally passes
    every denial test in this file while breaking the port entirely -- the same reason
    `test_a_benign_parameter_is_accepted_by_both` sits beside the blocklist tests.
    """
    service = facade(connect, FixedResolver(principal("org-a")))
    progress = await open_default(service)

    report = await service.report_progress(progress.operation_id)
    assert report.operation_id == progress.operation_id
    assert report.state == OperationState.PENDING.value


async def test_an_unreachable_resolver_on_the_read_path_is_unavailable_not_refused(
    connect,
):
    """The fourth control: "cannot ask" must stay distinct from "asked and denied".

    A caller told `refused` learns that retrying is pointless; a caller told
    `unavailable` learns the opposite. Collapsing them at the point the permission check
    was added is the easy mistake -- an `except Exception` around the resolve-then-check
    sequence would turn an identity-provider outage into a permanent denial.
    """
    service = facade(connect, BrokenResolver())
    with pytest.raises(OperationUnavailable):
        await service.report_progress("00000000-0000-0000-0000-000000000000")


async def test_the_actual_callers_payloads_round_trip_through_the_facade(connect):
    """F8, at the facade rather than at the type: the real caller's calls must work.

    `services/provisioning.py` `start_provision` sends
    `{"workspace_name", "isolation_mode"}` (plus `aws_account_id` when an account is
    named) and `start_teardown` sends `{"workspace_name"}`. Every one of those was
    refused as identity smuggling before the declared-shape exemption, so the port was
    uncallable by the only consumer it has.

    Exercised through `open_operation` -- the declared port method -- and then read back
    from the store on a **separate connection**, because "admitted" and "durably
    reconstructable" are different claims and the finding asks for the second.
    """
    service = facade(connect, FixedResolver(principal("org-a")))
    store = OperationStore()

    for action, parameters in (
        ("provision", {"workspace_name": "example", "isolation_mode": "shared"}),
        (
            "provision",
            {
                "workspace_name": "other",
                "isolation_mode": "dedicated",
                "aws_account_id": "123456789012",
            },
        ),
        ("teardown", {"workspace_name": "example"}),
    ):
        progress = await open_default(
            service, action=action, parameters=dict(parameters)
        )
        async with connect() as fresh:
            record = await store.get(fresh, principal("org-a"), progress.operation_id)
        assert record is not None
        # The admitted request, reconstructed the way a recovering dispatcher would.
        restored = record.admitted_request()
        assert restored.action == action
        assert restored.parameters == parameters, (
            "the caller's declared-shape fields must survive admission unchanged; "
            "silently dropping or renaming one would provision something else"
        )


async def test_listing_is_tenant_scoped_and_bounded(connect):
    service = facade(connect, FixedResolver(principal("org-a")))
    for index in range(3):
        await open_default(service, parameters={"idempotency_key": f"k-{index}"})

    mine = await service.list_operations(principal("org-a"))
    assert len(mine) == 3
    assert await service.list_operations(principal("org-b")) == ()

    # An unbounded limit is a way to turn one request into an arbitrarily expensive
    # read, so it is clamped rather than trusted.
    assert len(await service.list_operations(principal("org-a"), limit=10**9)) == 3


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------


async def test_an_unreachable_store_is_unavailable_not_a_synthesized_report(connect):
    """No connection means no answer -- never an invented one."""

    def broken_connect():
        raise ConnectionError("no route to database")

    service = facade(broken_connect, FixedResolver(principal()))
    with pytest.raises(OperationUnavailable):
        await open_default(service)


class FailingOnEnter:
    """A connection manager that fails when entered, not when constructed.

    This is what a real pool does: `connect()` hands back a context manager
    immediately and the work happens in `__aenter__`, where an `acquire()` timeout or
    a closed pool raises. Constructing successfully and failing on entry is therefore
    the *common* outage shape, not an exotic one.
    """

    async def __aenter__(self):
        raise ConnectionError("pool acquire timed out")

    async def __aexit__(self, *exc):
        return False


class FailingMidQuery:
    """Opens a connection whose queries fail -- a backend killed mid-read."""

    def __init__(self, connection):
        self._connection = connection

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, query, *args):
        raise ConnectionError("server closed the connection unexpectedly")

    async def fetchrow(self, query, *args):
        raise ConnectionError("server closed the connection unexpectedly")

    async def fetch(self, query, *args):
        raise ConnectionError("server closed the connection unexpectedly")

    async def fetchval(self, query, *args):
        raise ConnectionError("server closed the connection unexpectedly")

    def transaction(self):
        return self


class CancellingOnEnter:
    """Entering raises `CancelledError` -- the process is shutting down."""

    async def __aenter__(self):
        raise asyncio.CancelledError()

    async def __aexit__(self, *exc):
        return False


async def test_a_pool_that_fails_on_acquire_is_unavailable_not_a_driver_error(connect):
    """F4. Entering the connection manager is guarded, not only constructing it.

    An earlier revision wrapped only the `connect()` call, so a pool that handed back
    a manager and then failed in `__aenter__` raised a bare `ConnectionError` through
    the port. Inside the domain API that is an unhandled 500 rather than the declared
    `OperationUnavailable` the consumer's error path is written against -- the caller
    cannot tell "retry later" from "your request was wrong".
    """
    service = facade(lambda: FailingOnEnter(), FixedResolver(principal()))

    with pytest.raises(OperationUnavailable):
        await open_default(service)


async def test_a_query_failing_mid_read_is_unavailable_not_a_driver_error(connection):
    """F4. The third point: a connection that opened fine and then broke.

    A server restart looks exactly like this. The body of the `async with` has to be
    inside the translating frame, which is why `_connection` is a context manager
    rather than a function that returns one -- otherwise every call site would have
    to remember its own `except`, and the one that forgets is the one that leaks.
    """
    service = facade(
        lambda: FailingMidQuery(connection), FixedResolver(principal("org-a"))
    )

    with pytest.raises(OperationUnavailable):
        await service.report_progress_for(principal("org-a"), "op-1")


async def test_cancellation_is_not_translated_into_unavailable(connect):
    """F4's necessary limit: breadth must not swallow cancellation.

    Translating `CancelledError` into `OperationUnavailable` would stop a
    shutting-down task from shutting down -- the caller's `await` returns an ordinary
    error and the cancellation is lost. It is a `BaseException` on 3.8+ so a bare
    `except Exception` already misses it; it is named explicitly so that a later edit
    broadening the clause cannot silently reintroduce the bug.
    """
    service = facade(lambda: CancellingOnEnter(), FixedResolver(principal()))

    with pytest.raises(asyncio.CancelledError):
        await open_default(service)


async def test_a_cancelled_resolver_stays_cancelled(connect):
    """Same limit on the identity path: `_resolve` must not eat cancellation either."""

    class CancellingResolver:
        async def resolve(self, *, org_id, workspace_id, permission):
            raise asyncio.CancelledError()

    service = facade(connect, CancellingResolver())

    with pytest.raises(asyncio.CancelledError):
        await open_default(service)

    with pytest.raises(asyncio.CancelledError):
        await service.report_progress("00000000-0000-0000-0000-000000000000")


# `test_the_facade_reads_no_configuration` used to live here. It inspects source and
# needs no database, so under this module's `requires_postgres` mark it skipped on a
# developer machine without one -- a composition-root check that stops running exactly
# when nobody is watching. It is now in `test_contract_agreement.py`, which is the
# offline suite.
