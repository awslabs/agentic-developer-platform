"""Cancellation keeps original paid/control ownership despite partial cleanup."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from harness_jobs.execution import (
    BudgetDisposition,
    CallOutcome,
    CancellationPending,
    ProviderCallRefused,
)
from harness_jobs.execution_plan import (
    PlanProgress,
    confirmed_plan_progress,
    step_key,
)
from harness_jobs.execution_rpc import ExecutionRPCServer
from harness_jobs.identity import ResolvedPrincipal
from harness_jobs.recovery import sweep_scoped_expired_leases
from harness_jobs.store import OperationStore

from workspace_provisioning.retirement_runtime import RetirementRecoveryObserver


async def exercise_managed_cancellation(
    harness,
    facade,
    operation,
    inventory,
    provider,
    plan,
    artifact,
    deletion,
    authenticate,
    deployment,
    cloud,
    monkeypatch,
    case,
):
    lease = operation.grant.lease
    released = tuple(facade.ledger.released)

    async def cancel():
        assert await facade.cancel_operation(
            lease.operation_id, reason="stop the reviewed retirement"
        )

    if case == "cancel-before-intent":
        await cancel()
    elif case == "cancel-before-delete":

        async def cancel_at_verification(*_arguments):
            await cancel()

        provider.control_verify = cancel_at_verification
    elif case == "cancel-after-delete":
        original_event = cloud.event

        def cancel_after_effect(event, target):
            original_event(event, target)
            if event == "delete-entry" and target == plan.grants[0]["principal_arn"]:
                harness.run(cancel())

        monkeypatch.setattr(cloud, "event", cancel_after_effect)
    elif case == "incomplete-delete":
        cloud.residual = plan.grants[0]["principal_arn"]
    elif case == "incomplete-inventory":
        provider.verify_inventory = AsyncMock(
            return_value=(
                CallOutcome.UNKNOWN,
                "owned resources remain unverified",
                None,
            )
        )

    server = ExecutionRPCServer(
        connect=harness.connect, provider_call=provider, authenticate=authenticate
    )

    async def step(identifier):
        return await server.dispatch(
            {
                "token": "scoped-worker",
                "method": "execute_step",
                "arguments": {"step_id": identifier},
            }
        )

    before = cloud.mutations
    boundary = "revoke-control-entry"
    if case == "cancel-before-intent":
        with pytest.raises(ProviderCallRefused, match="Cancelled"):
            await step(boundary)
    elif case.startswith("cancel-"):
        with pytest.raises(CancellationPending) as pending:
            await step(boundary)
        assert pending.value.disposition is BudgetDisposition.RETAIN
        assert pending.value.call.may_have_happened
    else:
        result = await step(boundary)
        if case == "incomplete-inventory":
            assert result[0]["outcome"] == "succeeded"
            async with harness.connect() as connection:
                assert (
                    await confirmed_plan_progress(connection, lease.operation_id)
                    is PlanProgress.PREFIX
                )
            boundary = deletion.steps[-1].step_id
            result = await step(boundary)
            provider.verify_inventory.assert_awaited_once()
        assert result[0]["outcome"] is None
        assert result[0]["stage"] == "intended"
        assert result[1] == BudgetDisposition.RETAIN.value
    mutations = before + (case not in {"cancel-before-intent", "cancel-before-delete"})
    assert cloud.mutations == mutations
    assert (provider.removals.eks.observe(plan.grants[0]) is not None) == (
        case in {"cancel-before-intent", "cancel-before-delete", "incomplete-delete"}
    )
    with pytest.raises(ProviderCallRefused):
        await step(boundary)
    assert cloud.mutations == mutations

    async with harness.connect() as connection:
        record = await OperationStore().get(
            connection, operation.grant.principal, lease.operation_id
        )
        call = await connection.fetchrow(
            "SELECT stage,outcome FROM harness_provider_call_intent WHERE idempotency_key=$1",
            step_key(
                record,
                next(item for item in deletion.steps if item.step_id == boundary),
            ),
        )
        if case == "cancel-before-intent":
            assert call is None
        else:
            assert call["stage"] == "intended"
            assert call["outcome"] is None
        assert (
            await confirmed_plan_progress(connection, lease.operation_id)
            is not PlanProgress.COMPLETE
        )
        await connection.execute(
            "UPDATE harness_operation_leases SET expires_at="
            "clock_timestamp()-interval '1 second' WHERE operation_id=$1",
            lease.operation_id,
        )

    principal = ResolvedPrincipal(
        org_id=lease.org_id,
        workspace_id=lease.workspace_id,
        subject="cleanup-recovery-owner",
        permissions=frozenset({"workspace:recover"}),
    )
    observer = RetirementRecoveryObserver(
        connect=harness.connect,
        principal=principal,
        resolve=AsyncMock(return_value=(inventory, provider.removals, None)),
        control_context=SimpleNamespace(
            connect=harness.connect,
            domain_connect=harness.connect,
            policy=deployment,
            policy_fixture=True,
        ),
    )
    async with harness.connect() as connection:
        report = await sweep_scoped_expired_leases(
            connection,
            principal=principal,
            candidates=frozenset({lease.operation_id}),
            observe_claim=observer,
            max_reconcile_attempts=1,
        )
        assert report.results[0].action in {"cancelled", "unknown"}
        operation_row = await connection.fetchrow(
            "SELECT state,cancel_requested_at,cleanup_required FROM harness_operations "
            "WHERE operation_id=$1",
            lease.operation_id,
        )
        assert operation_row["state"] != "succeeded"
        assert (operation_row["cancel_requested_at"] is not None) == case.startswith(
            "cancel-"
        )
        if case in {"cancel-before-delete", "cancel-after-delete"}:
            assert operation_row["cleanup_required"]
            assert operation_row["state"] == "unknown"
        assert (
            await connection.fetchval(
                "SELECT sealed_revision FROM harness_allocation_seal WHERE allocation_id=$1",
                plan.original_allocation_id,
            )
            == "reviewed-paid-apply"
        )
        assert (
            await connection.fetchval(
                "SELECT reservation_state FROM harness_approval_consumption WHERE operation_id=$1",
                artifact["source_operation_id"],
            )
            == "confirmed"
        )
        assert (
            await connection.fetchval(
                "SELECT approval.reservation_state FROM harness_approval_consumption approval "
                "JOIN harness_allocation_seal seal ON seal.operation_id=approval.operation_id "
                "AND seal.org_id=approval.org_id AND seal.workspace_id=approval.workspace_id "
                "WHERE seal.allocation_id=$1 AND seal.org_id=$2 AND seal.workspace_id=$3",
                plan.original_allocation_id,
                lease.org_id,
                lease.workspace_id,
            )
            == "confirmed"
        )
        assert await connection.fetchval(
            "SELECT EXISTS(SELECT 1 FROM workspace_lifecycle_artifacts WHERE artifact_id=$1)",
            artifact["artifact_id"],
        )
    assert cloud.mutations == mutations
    assert tuple(facade.ledger.released) == released
    assert not deletion.completes_teardown
