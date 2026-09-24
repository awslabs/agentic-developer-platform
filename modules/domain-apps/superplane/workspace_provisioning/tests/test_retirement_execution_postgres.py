"""The composed retirement plan, executed through the REAL facade — AC-01 and AC-02.

`test_retirement_plan.py` asserts the plan is ordered and narrow. That is a property of
the composer alone, and it leaves the load-bearing question open: does the plan this
package composes actually *govern* execution once it reaches the shared machinery? A
plan that approval never bound, or that execution would accept extensions to, is a
reviewable document rather than a control.

So nothing here re-implements admission or dispatch. Real:
`compose_retirement_plan`, `OperationFacadeService.open_operation` (through the real
approval gate and budget reservation), `OperationStore`, `leases.acquire`,
`ExecutionRPCServer.dispatch`, `OperationExecutor.execute_provider`, the advisory lock,
the fence token, `harness_provider_call_intent`'s primary key, `step_key`,
`confirmed_plan_progress`, `sweep_expired_leases`, `request_cancellation`, and a real
PostgreSQL carrying the harness schema.

Doubled: the **cloud transport only** — `_Cloud` below is the provider hook, and it
records every deletion it is asked to perform. Every assertion about what was deleted
reads that recording, which is a list of actual calls, so "nothing was deleted twice" is
a statement about calls made rather than about an absence of exceptions.

## What AC-01 needs and how it is produced

A teardown's dangerous failures are not "the delete failed" — that is recoverable and
visible. They are:

* **the lost reply.** The delete may or may not have happened. `_Cloud` can be told to
  raise at a chosen step, which the executor maps to `UNKNOWN`/`RETAIN`, leaving the
  intent recoverable and the budget held. The test then asserts the outcome stays
  unresolved rather than being read as done or as never-attempted, and that resuming
  does not reissue it under a fresh key.
* **the crash between steps.** Resumption must continue from the confirmed prefix. The
  test expires the lease, sweeps, re-acquires as a *different* holder with a *new*
  attempt id, and asserts the provider is not called again for the confirmed prefix —
  which is what `step_key` being attempt-independent buys.
* **cancellation mid-teardown.** It must stop further deletions and must NOT claim the
  earlier ones were undone. Asserted on both halves, because the second is the one a
  hurried implementation gets wrong.

## What these tests do NOT establish

No AWS or Kubernetes API is called and nothing is deleted anywhere. That a real
retirement against a real cluster behaves as described is a LIVE criterion requiring
separate authorization. Offline evidence — however real the database — never closes it.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

# The authoritative harness, imported by the tests only (see `tests/__init__.py`).
from harness_jobs import (
    APPROVAL_PERMISSION,
    REQUIRED_PERMISSION,
    ApprovalBinding,
    ApprovalContext,
    ApprovalRecord,
    ApprovalResult,
    ApproverStatus,
    OperationFacadeService,
    OperationRefused,
    OperationStore,
    Reservation,
    ResolvedPrincipal,
    SpendEnvelope,
)
from harness_jobs.execution import CallOutcome, CallStage, ProviderCallRefused
from harness_jobs.execution_plan import (
    PlanProgress,
    admitted_steps,
    confirmed_plan_progress,
    step_key,
)
from harness_jobs.execution_rpc import ExecutionGrant, ExecutionRPCServer
from harness_jobs.recovery import request_cancellation, sweep_expired_leases
from harness_jobs.store import stored_outcome

from workspace_provisioning.retirement_plan import (
    DELETE_COMPONENT,
    DELETE_NAMESPACE,
    UNREGISTER,
    compose_retirement_plan,
)

from .postgres_bridge import Harness, requires_harness_postgres
from .test_retirement_plan import inventory

pytestmark = requires_harness_postgres

APPROVER = "user:boss"
ENVELOPE = SpendEnvelope(
    max_resource_units=4, max_runtime_seconds=3600, max_cost_micros=5_000_000
)


# --- the trusted-composition doubles the facade requires -------------------------
#
# Deliberately the same shapes the harness's own facade suite uses. The approval source
# and ledger are adjudication authorities this package does not own (see
# `harness_jobs.facade.ApprovalSource`), so a local implementation of the *decision*
# would be a second authority; these stand in for a configured store that has an
# approval on file, which is the precondition retirement runs under.


class _Approves:
    """A configured approval store holding a current approval for the request.

    The approval id derives from the idempotency key, so two identical retries present
    the same approval — a source minting a fresh approval per call would make the
    retry-collapse assertions meaningless.
    """

    async def approval_for(self, *, principal, request):
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


class _Ledger:
    """Idempotent on `(job_id, attempt_id)`, as the ledger Protocol requires."""

    def __init__(self):
        self.reserved, self.confirmed, self.released, self.retained = {}, [], [], []

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


class _Resolver:
    """Models the authentication layer: resolves to one principal, ignoring arguments."""

    def __init__(self, principal):
        self.principal = principal

    async def resolve(self, *, org_id, workspace_id, permission):
        return self.principal


def _principal(subject="user-1"):
    return ResolvedPrincipal(
        org_id="org-a",
        workspace_id="ws-1",
        subject=subject,
        permissions=frozenset({REQUIRED_PERMISSION}),
    )


# --- the doubled cloud transport -------------------------------------------------


class _Cloud:
    """The provider hook: records deletions, and can lose a reply or fail on demand.

    This is the ONLY substituted component. It records `(operation_kind, target,
    idempotency_key)` per call so the tests can assert on exactly which mutations were
    attempted, in what order, and how many times.

    `lose_at` makes the hook raise for a given `operation_kind`, which the real executor
    maps to `UNKNOWN` + `RETAIN` without terminalizing the intent — the lost-reply case.
    `fail_at` returns a definite `FAILED`, which is the different case of a provider
    that answered and established nothing happened.
    """

    def __init__(self, *, lose_at=None, fail_at=None):
        self.calls: list[tuple[str, str, str]] = []
        self.lose_at, self.fail_at = lose_at, fail_at

    async def __call__(self, call):
        self.calls.append((call.operation_kind, call.target, call.idempotency_key))
        if self.lose_at is not None and call.operation_kind == self.lose_at:
            # A transport that never returned. Not a provider answer.
            raise ConnectionError("the reply was lost")
        if self.fail_at is not None and call.operation_kind == self.fail_at:
            return CallOutcome.FAILED, "provider rejected the request", None
        return CallOutcome.SUCCEEDED, "deleted", f"ref-{len(self.calls)}"

    @property
    def kinds(self) -> list[str]:
        return [kind for kind, _, _ in self.calls]

    def count(self, kind: str) -> int:
        return self.kinds.count(kind)


# --- the operation under test ----------------------------------------------------


def _plan():
    """A composed retirement plan with something to delete at every boundary."""
    from superplane_bootstrap.inventory import OwnedPrerequisite

    from workspace_provisioning.retirement_inventory import OwnedGrant

    from .test_retirement_plan import component

    return compose_retirement_plan(
        inventory(
            components=(component("controller"),),
            grants=(
                OwnedGrant(
                    {
                        "kind": "kubernetes",
                        "body": {
                            "kind": "RoleBinding",
                            "metadata": {"name": "scoped", "namespace": "ws"},
                        },
                    },
                    {"uid": "rb-uid"},
                ),
            ),
            prerequisites=(
                OwnedPrerequisite(
                    kind="SecurityGroupRule/cluster-endpoint",
                    identifier="sgr-ours",
                    workspace_id="18563dce-15e9-4c58-8824-ff78744085e4",
                    ownership="adp-created",
                    reason="created for this workspace",
                ),
            ),
        )
    )


async def _open(harness, plan, *, ledger=None, key="retire-ws-1"):
    """Admit the retirement through the REAL facade, plan included in the request.

    The encoded plan travels as the `execution_steps` parameter, so the approval's
    `plan_digest` covers this exact ordered list: a different plan is a different
    operation rather than a mutation of an approved one.
    """
    service = OperationFacadeService(
        connect=harness.connect,
        resolver=_Resolver(_principal()),
        approvals=_Approves(),
        ledger=ledger or _Ledger(),
    )
    return await service.open_operation(
        action="teardown",
        workspace_id="ws-1",
        org_id="org-a",
        permission=REQUIRED_PERMISSION,
        parameters={"execution_steps": plan.encode(), "idempotency_key": key},
    )


class _Worker:
    """Drives steps the way a worker must: only through the RPC server's dispatch.

    The worker never touches a connection, an executor or the provider hook — it holds a
    scoped token and a step id, which is the whole point of the RPC boundary. Building
    the server per call models a restarted trusted process reading the stored plan.
    """

    def __init__(self, harness, cloud, lease, *, holder=None):
        self.harness, self.cloud, self.lease = harness, cloud, lease
        self.holder = holder or lease.holder

    def _server(self):
        async def authenticate(token):
            if token != "scoped":
                raise ProviderCallRefused("invalid token")
            return ExecutionGrant(_principal(self.holder), self.lease)

        return ExecutionRPCServer(
            connect=self.harness.connect,
            provider_call=self.cloud,
            authenticate=authenticate,
        )

    async def step(self, step_id):
        return await self._server().dispatch(
            {
                "token": "scoped",
                "method": "execute_step",
                "arguments": {"step_id": step_id},
            }
        )


# =================================================================================
# AC-02: the composed plan governs a real end-to-end execution
# =================================================================================


def test_the_composed_plan_is_admitted_and_executed_in_its_approved_order(harness):
    """Every step runs, in plan order, each exactly once, and the operation succeeds."""

    async def scenario():
        plan = _plan()
        progress = await _open(harness, plan)
        cloud = _Cloud()
        lease = await harness.lease(progress.operation_id, holder="worker")
        worker = _Worker(harness, cloud, lease)

        for step in plan.steps:
            await worker.step(step.step_id)

        # Order is the plan's order, not the order the worker happened to ask in.
        assert cloud.kinds == [step.operation_kind for step in plan.steps]
        # Exactly once each: a duplicated deletion is the failure teardown must not have.
        assert len(cloud.calls) == len({key for _, _, key in cloud.calls})

        async with harness.connect() as connection:
            assert (
                await confirmed_plan_progress(connection, progress.operation_id)
                is PlanProgress.COMPLETE
            )
            assert (
                await connection.fetchval(
                    "SELECT state FROM harness_operations WHERE operation_id=$1",
                    progress.operation_id,
                )
                == "succeeded"
            )
            # The keys the provider saw are the plan's derived keys, not worker-chosen.
            record = await OperationStore().get(
                connection, _principal(), progress.operation_id
            )
            assert [key for _, _, key in cloud.calls] == [
                step_key(record, step) for step in admitted_steps(record)
            ]

    harness.run(scenario())


def test_a_step_outside_the_approved_plan_is_refused_at_dispatch(harness):
    """Execution cannot be extended beyond what approval covered.

    The property that makes the composed plan a control rather than a document. The
    descriptor asked for here is a well-formed deletion of a real-looking namespace; it
    is refused solely because approval did not cover it.
    """

    async def scenario():
        plan = _plan()
        progress = await _open(harness, plan)
        cloud = _Cloud()
        lease = await harness.lease(progress.operation_id, holder="worker")
        worker = _Worker(harness, cloud, lease)

        with pytest.raises(ProviderCallRefused, match="not in the admitted plan"):
            await worker.step("delete-namespace-kube-system")
        assert cloud.calls == []

    harness.run(scenario())


def test_deletion_cannot_begin_before_the_unregister_step_is_confirmed(harness):
    """The composer's ordering is enforced by execution, not merely documented.

    `test_retirement_plan.py` asserts the plan *lists* the gate, drain and withdrawal
    first. That is a claim about a list. This asserts a worker cannot skip them: the
    namespace deletion is refused until its predecessors are confirmed succeeded, so
    ordering holds even against a worker asking for steps out of order.
    """

    async def scenario():
        plan = _plan()
        progress = await _open(harness, plan)
        cloud = _Cloud()
        lease = await harness.lease(progress.operation_id, holder="worker")
        worker = _Worker(harness, cloud, lease)

        component_step = next(
            step for step in plan.steps if step.operation_kind == DELETE_COMPONENT
        )
        with pytest.raises(ProviderCallRefused, match="Preceding"):
            await worker.step(component_step.step_id)
        # Nothing was deleted, and specifically the registry was not withdrawn either.
        assert cloud.calls == []

        # Running the prefix first makes the same request succeed, so the refusal above
        # was about ordering rather than about the descriptor being unusable.
        for step in plan.steps[: plan.steps.index(component_step)]:
            await worker.step(step.step_id)
        await worker.step(component_step.step_id)
        assert cloud.kinds[-1] == DELETE_COMPONENT
        assert cloud.kinds.index(UNREGISTER) < cloud.kinds.index(DELETE_COMPONENT)
        assert DELETE_NAMESPACE not in cloud.kinds

    harness.run(scenario())


def test_a_changed_plan_under_the_same_key_is_refused_rather_than_substituted(harness):
    """Approval bound the exact ordered list; a different one is a different request.

    This is what stops a retirement from having its deletion set widened after approval:
    re-presenting the same idempotency key with an extra or altered step is refused,
    while re-presenting the identical plan collapses onto the same operation.
    """

    async def scenario():
        plan = _plan()
        first = await _open(harness, plan)
        # Identical plan, same key: the same operation, not a second one.
        assert (await _open(harness, plan)).operation_id == first.operation_id

        # One descriptor's target altered — the same steps, one different resource.
        widened = replace(
            plan,
            steps=(
                *plan.steps[:-1],
                replace(plan.steps[-1], target=json.dumps({"kind": "other"})),
            ),
        )
        with pytest.raises(OperationRefused):
            await _open(harness, widened)

        async with harness.connect() as connection:
            assert (
                await connection.fetchval("SELECT count(*) FROM harness_operations")
                == 1
            )

    harness.run(scenario())


# =================================================================================
# AC-01: faults at every lifecycle boundary
# =================================================================================


# Derived from the composed plan, never hardcoded. A literal range here silently stops
# covering the boundaries a composer change adds: this was `range(7)` against an
# eight-step plan, so the final boundary -- `verify-resource-inventory`, the step whose
# whole purpose is to decide whether teardown may be called complete -- had no
# lost-reply case at all, and the suite read as exhaustive.
@pytest.mark.parametrize("index", range(len(_plan().steps)))
def test_a_lost_reply_at_any_boundary_stays_unresolved_and_is_never_reissued(
    harness, index
):
    """Injected at EVERY step in turn: the uncertain deletion is never repeated.

    The parametrization is the point — a lost reply is dangerous at every boundary, not
    just at the first, and a suite that injected only at step 0 would miss an
    implementation that special-cases the head of the plan.

    Three things are asserted, and the third is the one that costs money if wrong:

    1. the intent is recorded `UNRESOLVED`, not failed and not succeeded;
    2. budget is RETAINED, because a resource may exist;
    3. a resumed worker does not call the provider again for that step under a new key.
    """

    async def scenario():
        plan = _plan()
        # A distinct key per parametrized case: these are separate operations, and
        # sharing one would have each case collapse onto the first case's record.
        progress = await _open(harness, plan, key=f"retire-lost-{index}")
        boundary = plan.steps[index]
        cloud = _Cloud(lose_at=boundary.operation_kind)
        lease = await harness.lease(progress.operation_id, holder="worker")
        worker = _Worker(harness, cloud, lease)

        for step in plan.steps[:index]:
            await worker.step(step.step_id)
        result = await worker.step(boundary.step_id)

        # The executor reports RETAIN and does not terminalize. Read off the wire form
        # the worker actually receives, not off an internal object.
        assert result[1] == "retain"

        async with harness.connect() as connection:
            record = await OperationStore().get(
                connection, _principal(), progress.operation_id
            )
            row = await connection.fetchrow(
                "SELECT stage, outcome FROM harness_provider_call_intent "
                "WHERE idempotency_key=$1",
                step_key(record, boundary),
            )
            # Uncertain, and specifically NOT read as done.
            #
            # `INTENDED`, deliberately, and not `UNRESOLVED`: `_execute_provider`
            # persists any provider_ref it got but does NOT call `observe(UNKNOWN)`,
            # because observing terminalizes the row and the worker observed nothing.
            # Leaving it `INTENDED` is what keeps it reconcilable -- `recovery` only
            # re-asks the provider about calls in that stage. An earlier revision of
            # this test asserted `UNRESOLVED` and was wrong about production, not the
            # other way round: `UNRESOLVED` is where a *reconciliation* that still
            # could not decide puts the row, which is a decision to involve a human.
            assert row["stage"] == CallStage.INTENDED.value
            assert stored_outcome(row["outcome"]) != CallOutcome.SUCCEEDED.value
            # The plan is not complete and the operation is not terminal.
            assert (
                await confirmed_plan_progress(connection, progress.operation_id)
                is not PlanProgress.COMPLETE
            )
            assert await connection.fetchval(
                "SELECT state FROM harness_operations WHERE operation_id=$1",
                progress.operation_id,
            ) not in ("succeeded", "failed")

        # The uncertain step is never reissued: a successor asking for it again is
        # refused and must reconcile instead. Reissuing is how a deletion gets
        # attempted twice against a name that may now belong to something else.
        attempted = len(cloud.calls)
        with pytest.raises(ProviderCallRefused, match="recovery"):
            await worker.step(boundary.step_id)
        assert len(cloud.calls) == attempted

        # Nor can a LATER step proceed on top of an unconfirmed predecessor.
        if index + 1 < len(plan.steps):
            with pytest.raises(ProviderCallRefused, match="Preceding"):
                await worker.step(plan.steps[index + 1].step_id)
            assert len(cloud.calls) == attempted

    harness.run(scenario())


def test_a_crash_resumes_from_the_confirmed_prefix_without_repeating_a_deletion(
    harness,
):
    """A new holder and a new attempt re-derive the same keys and skip confirmed work.

    This is what `step_key` being attempt-independent buys, and it is asserted by
    counting provider calls rather than by inspecting keys: the successor completes the
    plan, and the steps the predecessor confirmed are not called a second time.
    """

    async def scenario():
        plan = _plan()
        progress = await _open(harness, plan)
        cloud = _Cloud()
        lease = await harness.lease(progress.operation_id, holder="worker")
        worker = _Worker(harness, cloud, lease)

        confirmed = plan.steps[:3]
        for step in confirmed:
            await worker.step(step.step_id)
        before = list(cloud.calls)

        async with harness.connect() as connection:
            assert (
                await confirmed_plan_progress(connection, progress.operation_id)
                is PlanProgress.PREFIX
            )
            # The worker is gone: its lease expires and recovery hands the operation on.
            await connection.execute(
                "UPDATE harness_operation_leases SET expires_at="
                "clock_timestamp()-interval '1 second' WHERE operation_id=$1",
                progress.operation_id,
            )
            report = await sweep_expired_leases(connection)
            assert report.results[0].action == "retried"

        successor = await harness.lease(
            progress.operation_id, holder="successor", attempt="attempt-2"
        )
        resumed = _Worker(harness, cloud, successor)
        for step in plan.steps:
            await resumed.step(step.step_id)

        # The confirmed prefix was NOT re-called: same keys, replayed from durable
        # records. Anything else would be a second deletion of an already-deleted
        # resource, by a name that may now name something else.
        assert cloud.calls[: len(before)] == before
        assert cloud.count(UNREGISTER) == 1
        assert cloud.count(DELETE_COMPONENT) == 1
        assert cloud.count(DELETE_NAMESPACE) == 0
        assert len(cloud.calls) == len(plan.steps)

        async with harness.connect() as connection:
            assert (
                await confirmed_plan_progress(connection, progress.operation_id)
                is PlanProgress.COMPLETE
            )

    harness.run(scenario())


def test_cancellation_stops_further_deletions_without_claiming_a_rollback(harness):
    """Cancel mid-teardown: no further deletion, and no assertion of undoing.

    Both halves matter. Stopping is the easy half. The half a hurried implementation
    gets wrong is the second: the already-deleted resources are NOT restored, nothing
    records that they were, and the operation does not report a clean unwound state.
    Cleanup is flagged as required instead, which is the honest answer — a partially
    retired workspace needs finishing, not a claim that nothing happened.
    """

    async def scenario():
        plan = _plan()
        progress = await _open(harness, plan)
        cloud = _Cloud()
        lease = await harness.lease(progress.operation_id, holder="worker")
        worker = _Worker(harness, cloud, lease)

        for step in plan.steps[:3]:
            await worker.step(step.step_id)
        deleted_before_cancel = list(cloud.calls)

        async with harness.connect() as connection:
            assert await request_cancellation(
                connection,
                operation_id=progress.operation_id,
                principal=_principal(),
                reason="operator stopped the retirement",
            )

        # No further deletion is dispatched. The refusal is a cancellation, not a
        # generic failure, so a caller can tell "stopped" from "broken".
        with pytest.raises(ProviderCallRefused):
            await worker.step(plan.steps[3].step_id)
        assert cloud.calls == deleted_before_cancel

        async with harness.connect() as connection:
            # Nothing claims the confirmed deletions were undone: their rows still say
            # SUCCEEDED, and the operation is not recorded as a no-op.
            record = await OperationStore().get(
                connection, _principal(), progress.operation_id
            )
            for step in plan.steps[:3]:
                row = await connection.fetchrow(
                    "SELECT stage, outcome FROM harness_provider_call_intent "
                    "WHERE idempotency_key=$1",
                    step_key(record, step),
                )
                # Decoded: the column carries the provider's free text after the
                # enum (`"succeeded: deleted"`), so a raw equality test here would
                # assert about the double's wording rather than about the outcome.
                assert stored_outcome(row["outcome"]) == CallOutcome.SUCCEEDED.value
            assert (
                await confirmed_plan_progress(connection, progress.operation_id)
                is PlanProgress.PREFIX
            )
            row = await connection.fetchrow(
                "SELECT state, cancel_requested_at, cleanup_required "
                "FROM harness_operations WHERE operation_id=$1",
                progress.operation_id,
            )
            assert row["cancel_requested_at"] is not None
            assert row["state"] != "succeeded"

    harness.run(scenario())


def test_a_definite_provider_failure_is_not_recorded_as_an_uncertain_one(harness):
    """`FAILED` and `UNKNOWN` are different facts and must not be merged.

    A provider that answered "I rejected this, nothing happened" permits a release;
    a lost reply does not. Collapsing the two either leaks a reservation for a resource
    that was never created, or releases budget for one that may exist. Asserted as the
    pair, because either direction alone would pass a test of the other.
    """

    async def scenario():
        plan = _plan()
        boundary = plan.steps[2]
        progress = await _open(harness, plan, key="retire-failed")
        cloud = _Cloud(fail_at=boundary.operation_kind)
        lease = await harness.lease(progress.operation_id, holder="worker")
        worker = _Worker(harness, cloud, lease)

        for step in plan.steps[:2]:
            await worker.step(step.step_id)
        result = await worker.step(boundary.step_id)

        # Established absence permits a release; an unresolved one would RETAIN.
        assert result[1] == "release"
        async with harness.connect() as connection:
            record = await OperationStore().get(
                connection, _principal(), progress.operation_id
            )
            row = await connection.fetchrow(
                "SELECT stage, outcome FROM harness_provider_call_intent "
                "WHERE idempotency_key=$1",
                step_key(record, boundary),
            )
            assert row["stage"] == CallStage.OBSERVED.value
            assert stored_outcome(row["outcome"]) == CallOutcome.FAILED.value

        # A definite failure still does not authorize continuing past it.
        with pytest.raises(ProviderCallRefused, match="Preceding"):
            await worker.step(plan.steps[3].step_id)

    harness.run(scenario())


# --- fixtures --------------------------------------------------------------------


@pytest.fixture
def harness(tmp_path_factory, request):
    """A real PostgreSQL carrying the harness schema, on a fresh database per test."""
    with Harness.started(tmp_path_factory, request.node.name) as started:
        yield started
