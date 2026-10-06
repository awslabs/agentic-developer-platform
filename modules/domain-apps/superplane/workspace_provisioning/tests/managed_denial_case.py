"""SQL-bound revocation denials using synthetic ownership and doubled transport."""

from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from harness_jobs.execution import (
    BudgetDisposition,
    CancellationPending,
    ProviderCallRefused,
)
from harness_jobs.execution_plan import (
    PlanProgress,
    confirmed_plan_progress,
    step_key,
)
from harness_jobs.execution_rpc import ExecutionRPCServer
from harness_jobs.identity import OperationRefused
from harness_jobs.store import OperationStore

from workspace_provisioning import retirement_managed_access


async def exercise_managed_denial(
    harness,
    facade,
    operation,
    provider,
    plan,
    artifact,
    deletion,
    authenticate,
    cloud,
    monkeypatch,
    case,
):
    lease = operation.grant.lease
    boundary = next(
        step for step in deletion.steps if step.step_id == "revoke-control-entry"
    )
    async with harness.connect() as connection:
        record = await OperationStore().get(
            connection, operation.grant.principal, lease.operation_id
        )
    key = step_key(record, boundary)
    released = tuple(facade.ledger.released)
    before = cloud.mutations
    principal = plan.grants[0]["principal_arn"]
    if case == "deny-replaced-entry":
        cloud.entries[principal]["accessEntryArn"] += "-replacement"
    elif case == "deny-broadened-entry":
        cloud.entries[principal]["kubernetesGroups"].append("unapproved-group")
    elif case == "deny-unapproved-policy":
        monkeypatch.setattr(
            cloud,
            "list_associated_access_policies",
            lambda **_parameters: {
                "associatedAccessPolicies": [
                    {
                        "policyArn": "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy",
                        "accessScope": {"type": "cluster"},
                    }
                ]
            },
        )
    elif case == "deny-interlock":
        provider.lifecycle.managed_fence = AsyncMock(return_value=False)
    entry = deepcopy(cloud.entries[principal])
    verifications = 0

    async def verify(*_arguments):
        nonlocal verifications
        verifications += 1
        if verifications != 2:
            return
        assert cloud.mutations == before
        async with harness.connect() as connection:
            if case == "deny-released-before-delete":
                await connection.execute(
                    "UPDATE harness_approval_consumption SET reservation_state='released' "
                    "WHERE operation_id=$1",
                    artifact["source_operation_id"],
                )
            elif case == "deny-expired-lease":
                await connection.execute(
                    "UPDATE harness_operation_leases SET expires_at="
                    "clock_timestamp()-interval '1 second' WHERE operation_id=$1",
                    lease.operation_id,
                )

    provider.control_verify = verify
    source_fault_injected = False
    if case.endswith("-during-source"):
        require_source = retirement_managed_access.require_managed_control_source

        async def change_authority_during_source(*arguments, **options):
            nonlocal source_fault_injected
            result = await require_source(*arguments, **options)
            if verifications == 2 and not source_fault_injected:
                assert cloud.mutations == before
                source_fault_injected = True
                if case == "deny-cancelled-during-source":
                    assert await facade.cancel_operation(
                        lease.operation_id,
                        reason="cancel during final source verification",
                    )
                else:
                    async with harness.connect() as connection:
                        if case == "deny-expired-during-source":
                            await connection.execute(
                                "UPDATE harness_operation_leases SET expires_at="
                                "clock_timestamp()-interval '1 second' WHERE operation_id=$1",
                                lease.operation_id,
                            )
                        elif case == "deny-fenced-during-source":
                            await connection.execute(
                                "UPDATE harness_operation_leases SET fence_token=fence_token+1 "
                                "WHERE operation_id=$1",
                                lease.operation_id,
                            )
            return result

        monkeypatch.setattr(
            retirement_managed_access,
            "require_managed_control_source",
            change_authority_during_source,
        )
    errors = []

    async def deliver(call):
        if case == "deny-unapproved-target":
            call = replace(call, target="unapproved-target")
        elif case == "deny-foreign-workspace":
            call = replace(call, workspace_id="foreign-workspace")
        elif case == "deny-stale-fence":
            call = replace(call, fence_token=call.fence_token + 1)
        try:
            return await provider(call)
        except Exception as error:
            errors.append(str(error))
            raise

    server = ExecutionRPCServer(
        connect=harness.connect, provider_call=deliver, authenticate=authenticate
    )
    request = {
        "token": "scoped-worker",
        "method": "execute_step",
        "arguments": {"step_id": boundary.step_id},
    }
    if case == "deny-no-intent":
        call = SimpleNamespace(
            operation_id=lease.operation_id,
            org_id=lease.org_id,
            workspace_id=lease.workspace_id,
            job_id=operation.job_id,
            attempt_id=lease.attempt_id,
            fence_token=lease.fence_token,
            idempotency_key=key,
            provider=boundary.provider,
            operation_kind=boundary.operation_kind,
            target=boundary.target,
        )
        with pytest.raises(OperationRefused, match="no original outer intent"):
            await deliver(call)
    elif case in {
        "deny-expired-lease",
        "deny-expired-during-source",
        "deny-fenced-during-source",
    }:
        with pytest.raises(ProviderCallRefused, match="lease|Lease"):
            await server.dispatch(request)
    elif case == "deny-cancelled-during-source":
        with pytest.raises(CancellationPending) as pending:
            await server.dispatch(request)
        assert pending.value.disposition is BudgetDisposition.RETAIN
        assert pending.value.call.may_have_happened
    else:
        result = await server.dispatch(request)
        assert result[0]["outcome"] is None
        assert result[0]["stage"] == "intended"
        assert result[1] == "retain"
    expected = {
        "deny-no-intent": "no original outer intent",
        "deny-unapproved-target": "not an admitted step",
        "deny-foreign-workspace": "differs from its admitted attempt",
        "deny-stale-fence": "differs from its admitted attempt",
        "deny-expired-lease": "lease expired",
        "deny-expired-during-source": "lease expired",
        "deny-cancelled-during-source": "cancellation requested",
        "deny-fenced-during-source": "lease expired",
        "deny-released-before-delete": "approval is no longer retained",
        "deny-replaced-entry": "changed before revocation",
        "deny-broadened-entry": "changed before revocation",
        "deny-unapproved-policy": "residual policy associations",
        "deny-interlock": "admission interlock changed",
    }
    assert len(errors) == 1 and expected[case] in errors[0], errors
    assert cloud.mutations == before
    assert cloud.entries[principal] == entry
    assert source_fault_injected == case.endswith("-during-source")
    if case != "deny-no-intent":
        with pytest.raises(ProviderCallRefused):
            await server.dispatch(request)
    async with harness.connect() as connection:
        call = await connection.fetchrow(
            "SELECT stage,outcome,operation_id,org_id,workspace_id,job_id,attempt_id,fence_token "
            "FROM harness_provider_call_intent WHERE idempotency_key=$1",
            key,
        )
        if case == "deny-no-intent":
            assert call is None
        else:
            assert call["stage"] == "intended"
            assert call["outcome"] is None
            assert (
                call["operation_id"],
                call["org_id"],
                call["workspace_id"],
                call["job_id"],
                call["attempt_id"],
                call["fence_token"],
            ) == (
                lease.operation_id,
                lease.org_id,
                lease.workspace_id,
                operation.job_id,
                lease.attempt_id,
                lease.fence_token,
            )
        assert await confirmed_plan_progress(connection, lease.operation_id) is (
            PlanProgress.PREFIX if case == "deny-no-intent" else PlanProgress.UNKNOWN
        )
        assert (
            await connection.fetchval(
                "SELECT state FROM harness_operations WHERE operation_id=$1",
                lease.operation_id,
            )
            != "succeeded"
        )
        if case == "deny-cancelled-during-source":
            assert await connection.fetchval(
                "SELECT cleanup_required AND cancel_requested_at IS NOT NULL "
                "FROM harness_operations WHERE operation_id=$1",
                lease.operation_id,
            )
        assert await connection.fetchval(
            "SELECT reservation_state FROM harness_approval_consumption WHERE operation_id=$1",
            lease.operation_id,
        ) in {"confirmed", "retained"}
        seal = await connection.fetchrow(
            "SELECT operation_id,sealed_revision FROM harness_allocation_seal "
            "WHERE allocation_id=$1 AND org_id=$2 AND workspace_id=$3",
            plan.original_allocation_id,
            lease.org_id,
            lease.workspace_id,
        )
        assert seal["sealed_revision"] == "reviewed-paid-apply"
        assert (
            await connection.fetchval(
                "SELECT reservation_state FROM harness_approval_consumption WHERE operation_id=$1",
                seal["operation_id"],
            )
            == "confirmed"
        )
        assert await connection.fetchval(
            "SELECT reservation_state FROM harness_approval_consumption WHERE operation_id=$1",
            artifact["source_operation_id"],
        ) == ("released" if case == "deny-released-before-delete" else "confirmed")
        assert await connection.fetchval(
            "SELECT EXISTS(SELECT 1 FROM workspace_lifecycle_artifacts WHERE artifact_id=$1)",
            artifact["artifact_id"],
        )
    assert cloud.mutations == before
    assert tuple(facade.ledger.released) == released
