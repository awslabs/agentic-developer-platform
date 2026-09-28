"""Tenant-scoped recovery: a domain service recovers its own work and nothing else.

Issue #5536 (w6 superplane controller), EPIC #4910, Wave 6.

`sweep_expired_leases` is deliberately unscoped -- an operator reconciling the harness's
own obligations asks about every tenant. A *domain* service is the opposite case: it
holds one tenant's grants, so a sweep it started would act outside them. Rather than let
each domain reimplement recovery to get a tenant filter (and get the budget rules
subtly wrong), `sweep_scoped_expired_leases` is the same engine with a scope.

These are real-database tests because the properties are database properties: the tenant
filter must be applied in the query and before LIMIT, the candidate narrowing must
intersect rather than replace it, and the settlement hook must run inside the same
transaction that settles the operation -- which can only be shown by making it fail and
observing that the settlement rolled back with it.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from harness_jobs import OperationStore
from harness_jobs.execution import CallOutcome, record_intent
from harness_jobs.identity import ContractViolation, OperationRefused
from harness_jobs.leases import acquire
from harness_jobs.recovery import sweep_scoped_expired_leases

from .conftest import admit_paid, requires_postgres
from .test_admission_postgres import principal as admission_principal
from .test_admission_postgres import request


def principal(**kwargs):
    return replace(
        admission_principal(**kwargs),
        permissions=frozenset({"workspace:provision", "workspace:recover"}),
    )


pytestmark = requires_postgres


async def _expired_operation(store, connection, *, key, org, workspace, intent=True):
    """An operation whose executor died: one recorded intent, lease lapsed."""
    admitted = await admit_paid(
        store, connection, principal(org=org, workspace=workspace), request(key)
    )
    record = admitted.record
    lease = await acquire(
        connection,
        operation_id=record.operation_id,
        holder="worker-" + key,
        attempt_id="attempt-" + key,
        duration=timedelta(seconds=60),
    )
    if intent:
        await record_intent(
            connection,
            lease,
            idempotency_key="call-" + key,
            provider="prov",
            operation_kind="create",
            target="target-" + key,
        )
    await connection.execute(
        "UPDATE harness_operation_leases "
        "SET expires_at=clock_timestamp()-interval '1 minute', "
        "    runtime_deadline=clock_timestamp()-interval '1 minute' "
        "WHERE operation_id=$1",
        record.operation_id,
    )
    return record, lease


async def _succeeded(idempotency_key, provider, operation_kind, target):
    return CallOutcome.SUCCEEDED, "observed", "provider-ref"


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------


async def test_scoped_sweep_recovers_only_the_principal_tenant(connection):
    store = OperationStore()
    mine, _ = await _expired_operation(
        store, connection, key="mine", org="org-a", workspace="ws-1"
    )
    theirs, theirs_lease = await _expired_operation(
        store, connection, key="theirs", org="org-b", workspace="ws-2"
    )

    report = await sweep_scoped_expired_leases(
        connection,
        principal=principal(org="org-a", workspace="ws-1"),
        observe_call=_succeeded,
    )

    assert [result.operation_id for result in report.results] == [mine.operation_id]
    # The other tenant's lease is untouched: same fence token, still open.
    row = await connection.fetchrow(
        "SELECT fence_token, closed_at FROM harness_operation_leases "
        "WHERE operation_id=$1",
        theirs.operation_id,
    )
    assert row["fence_token"] == theirs_lease.fence_token
    assert row["closed_at"] is None
    state = await connection.fetchval(
        "SELECT state FROM harness_operations WHERE operation_id=$1",
        theirs.operation_id,
    )
    assert state not in ("succeeded", "failed", "unknown", "cancelled")


async def test_candidates_intersect_the_scope_rather_than_widening_it(connection):
    # Naming another tenant's operation must not select it. `candidates` can only
    # ever reduce the set -- otherwise it would be an authority-granting parameter.
    store = OperationStore()
    await _expired_operation(
        store, connection, key="mine", org="org-a", workspace="ws-1"
    )
    theirs, _ = await _expired_operation(
        store, connection, key="theirs", org="org-b", workspace="ws-2"
    )

    report = await sweep_scoped_expired_leases(
        connection,
        principal=principal(org="org-a", workspace="ws-1"),
        candidates=frozenset({theirs.operation_id}),
        observe_call=_succeeded,
    )
    assert report.results == ()


async def test_empty_candidate_set_selects_nothing(connection):
    store = OperationStore()
    await _expired_operation(
        store, connection, key="mine", org="org-a", workspace="ws-1"
    )
    report = await sweep_scoped_expired_leases(
        connection,
        principal=principal(org="org-a", workspace="ws-1"),
        candidates=frozenset(),
        observe_call=_succeeded,
    )
    # An empty set is "nothing qualified", not "no filter".
    assert report.results == () and report.retained == 0


async def test_scope_cannot_be_supplied_without_an_authenticated_principal(connection):
    for bad in (None, object(), {"org_id": "org-a", "workspace_id": "ws-1"}):
        with pytest.raises(
            OperationRefused, match="authenticated workspace:recover principal"
        ):
            await sweep_scoped_expired_leases(connection, principal=bad)


async def test_scoped_sweep_rejects_an_ambient_transaction(connection):
    async with connection.transaction():
        with pytest.raises(ContractViolation, match="transaction boundaries"):
            await sweep_scoped_expired_leases(
                connection, principal=principal(org="org-a", workspace="ws-1")
            )


async def test_scoped_sweep_bounds_the_pass(connection):
    for bad in (0, 201, -1):
        with pytest.raises(ContractViolation, match="max_operations"):
            await sweep_scoped_expired_leases(
                connection,
                principal=principal(org="org-a", workspace="ws-1"),
                max_operations=bad,
            )


# ---------------------------------------------------------------------------
# The settlement hook
# ---------------------------------------------------------------------------


async def test_settlement_hook_runs_inside_the_fenced_transaction(connection):
    store = OperationStore()
    record, lease = await _expired_operation(
        store, connection, key="hooked", org="org-a", workspace="ws-1"
    )
    seen = []

    async def on_settled(hook_connection, hook_lease, result):
        # Called with the claim still held, so the lease row is lockable here, and
        # before the claim closes.
        assert hook_connection is connection
        assert hook_lease.operation_id == record.operation_id
        assert hook_lease.fence_token > lease.fence_token
        closed = await hook_connection.fetchval(
            "SELECT closed_at FROM harness_operation_leases WHERE operation_id=$1",
            record.operation_id,
        )
        assert closed is None, "the hook must run before the claim is closed"
        seen.append(result)

    report = await sweep_scoped_expired_leases(
        connection,
        principal=principal(org="org-a", workspace="ws-1"),
        observe_call=_succeeded,
        on_settled=on_settled,
    )
    assert len(seen) == 1
    assert seen[0].operation_id == record.operation_id
    assert seen[0].budget_disposition == "settle"
    assert seen[0].call_dispositions
    assert report.results[0].action == seen[0].action


async def test_failing_settlement_hook_rolls_back_the_settlement(connection):
    # The property that makes the hook safe for a domain to depend on: if its own
    # accounting cannot be written, the operation is NOT left settled and closed
    # with no record of the disposition. It stays recoverable.
    store = OperationStore()
    record, _ = await _expired_operation(
        store, connection, key="refused", org="org-a", workspace="ws-1"
    )

    async def refuses(hook_connection, hook_lease, result):
        raise RuntimeError("domain accounting unavailable")

    with pytest.raises(RuntimeError, match="domain accounting unavailable"):
        await sweep_scoped_expired_leases(
            connection,
            principal=principal(org="org-a", workspace="ws-1"),
            observe_call=_succeeded,
            on_settled=refuses,
        )

    row = await connection.fetchrow(
        "SELECT o.state, l.closed_at FROM harness_operations o "
        "LEFT JOIN harness_operation_leases l USING (operation_id) "
        "WHERE o.operation_id=$1",
        record.operation_id,
    )
    assert row["closed_at"] is None, "a refused hook must not leave the claim closed"
    assert row["state"] not in ("succeeded", "failed", "unknown", "cancelled")


async def test_settlement_hook_is_not_called_for_a_deferred_operation(connection):
    # A deferred result is not a settlement. Calling the hook here would have a
    # domain record an outcome that is still in flight.
    store = OperationStore()
    await _expired_operation(
        store, connection, key="deferred", org="org-a", workspace="ws-1"
    )
    calls = []

    async def unreadable(idempotency_key, provider, operation_kind, target):
        raise OSError("provider unavailable")

    async def on_settled(hook_connection, hook_lease, result):
        calls.append(result)

    report = await sweep_scoped_expired_leases(
        connection,
        principal=principal(org="org-a", workspace="ws-1"),
        observe_call=unreadable,
        on_settled=on_settled,
    )
    assert report.results[0].action == "deferred"
    assert calls == []


async def test_settlement_hook_is_not_called_for_a_retried_operation(connection):
    # No recorded calls and attempts remaining: the lease is released for another
    # attempt rather than settled, so there is nothing for a domain to account for.
    store = OperationStore()
    await _expired_operation(
        store, connection, key="retried", org="org-a", workspace="ws-1", intent=False
    )
    calls = []

    async def on_settled(hook_connection, hook_lease, result):
        calls.append(result)

    report = await sweep_scoped_expired_leases(
        connection,
        principal=principal(org="org-a", workspace="ws-1"),
        on_settled=on_settled,
    )
    assert report.results[0].action == "retried"
    assert calls == []


async def test_scoped_sweep_without_an_observer_retains(connection):
    # No credential means nothing is established about the provider, so the budget
    # is retained rather than released. Same rule as the unscoped sweep.
    store = OperationStore()
    await _expired_operation(
        store, connection, key="blind", org="org-a", workspace="ws-1"
    )
    report = await sweep_scoped_expired_leases(
        connection,
        principal=principal(org="org-a", workspace="ws-1"),
        max_reconcile_attempts=1,
    )
    assert report.results[0].budget_disposition == "retain"


async def test_claim_binding_and_preparation_outside_settlement_transaction(connection):
    record, old = await _expired_operation(
        OperationStore(), connection, key="claim-binding", org="org-a", workspace="ws-1"
    )
    actor = principal(
        org="org-a", workspace="ws-1", subject="authenticated-recovery-run"
    )
    seen = []

    async def prepare(lease, calls):
        assert not connection.is_in_transaction()
        binding = await connection.fetchrow(
            "SELECT * FROM harness_recovery_claim_bindings "
            "WHERE operation_id=$1 AND fence_token=$2",
            lease.operation_id,
            lease.fence_token,
        )
        assert binding["subject"] == actor.subject
        assert binding["holder"] == lease.holder != old.holder
        assert binding["attempt_id"] == lease.attempt_id
        seen.append(lease)
        raise RuntimeError("fresh inventory unavailable")

    report = await sweep_scoped_expired_leases(
        connection, principal=actor, observe_call=_succeeded, prepare_settlement=prepare
    )
    assert seen and report.results[0].action == "deferred"
    assert (
        await connection.fetchval(
            "SELECT closed_at FROM harness_operation_leases WHERE operation_id=$1",
            record.operation_id,
        )
        is None
    )


async def test_claim_expiring_during_inventory_cannot_settle(connection):
    record, _ = await _expired_operation(
        OperationStore(),
        connection,
        key="slow-inventory",
        org="org-a",
        workspace="ws-1",
    )
    settled = []

    async def prepare(lease, calls):
        await connection.execute(
            "UPDATE harness_operation_leases "
            "SET expires_at=clock_timestamp()-interval '1 second' "
            "WHERE operation_id=$1",
            lease.operation_id,
        )
        return True

    async def on_settled(*args):
        settled.append(args)

    report = await sweep_scoped_expired_leases(
        connection,
        principal=principal(org="org-a", workspace="ws-1"),
        observe_call=_succeeded,
        prepare_settlement=prepare,
        on_settled=on_settled,
    )
    assert report.results[0].action == "skipped" and not settled
    assert (
        await connection.fetchval(
            "SELECT closed_at FROM harness_operation_leases WHERE operation_id=$1",
            record.operation_id,
        )
        is None
    )
