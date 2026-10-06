"""Recovery exercises real SQL claims against a synthetic management-mode inventory."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

from harness_jobs.execution import CallStage
from harness_jobs.execution_plan import step_key
from harness_jobs.execution_rpc import ExecutionGrant, ExecutionRPCServer
from harness_jobs.identity import ResolvedPrincipal
from harness_jobs.recovery import sweep_scoped_expired_leases
from superplane_bootstrap.grant_plan import compile_grants
from superplane_bootstrap.kube_grants import _digest as grant_digest

from workspace_provisioning.retirement_managed_access import (
    compile_managed_access_review,
    managed_recipe_inputs,
)
from workspace_provisioning.retirement_runtime import RetirementRecoveryObserver


def management_plan(arguments):
    inventory = arguments["inventory"]
    recipe = managed_recipe_inputs(inventory, arguments["runtime"])
    generation = next(
        grant.spec["generation"]
        for grant in inventory.grants
        if grant.spec["key"].startswith("cleanup-")
    )
    expected = {
        spec["key"]: spec
        for spec in compile_grants(
            SimpleNamespace(
                target=arguments["eks"].target,
                generation=generation,
                original_allocation_id=arguments["original_allocation_id"],
            ),
            recipe["release"],
            recipe["principals"],
            controller_mode=recipe["controller_mode"],
        )["grants"]
    }
    inventory = replace(
        inventory,
        grants=tuple(
            replace(
                grant,
                spec=expected[grant.spec["key"]],
                identity={
                    **grant.identity,
                    "digest": grant_digest(expected[grant.spec["key"]]["body"]),
                },
            )
            if grant.spec["key"].startswith("cleanup-")
            else grant
            for grant in inventory.grants
        ),
    )
    arguments["inventory"] = inventory
    return compile_managed_access_review(
        inventory,
        arguments["runtime"],
        original_allocation_id=arguments["original_allocation_id"],
        bootstrap_artifact_id=arguments["bootstrap_artifact_id"],
        retirement_request_id=arguments["retirement_request_id"],
        **recipe,
    )


async def recover_managed_revoke(
    harness,
    operation,
    owned,
    removals,
    plan,
    artifact,
    deployment,
    cloud,
    monkeypatch,
    case,
):
    lease = operation.grant.lease
    principal = ResolvedPrincipal(
        org_id=lease.org_id,
        workspace_id=lease.workspace_id,
        subject="recovery-owner",
        permissions=frozenset({"workspace:recover"}),
    )
    if case == "recovery-replaced":
        removals.eks.create(plan.grants[0])
    async with harness.connect() as connection:
        if case == "recovery-control-released":
            await connection.execute(
                "UPDATE harness_approval_consumption SET reservation_state='released' "
                "WHERE operation_id=$1",
                artifact["source_operation_id"],
            )
        elif case == "recovery-artifact-missing":
            await connection.execute(
                "DELETE FROM workspace_lifecycle_artifacts WHERE artifact_id=$1",
                artifact["artifact_id"],
            )
        await connection.execute(
            "UPDATE harness_operation_leases SET expires_at="
            "clock_timestamp()-interval '1 second' WHERE operation_id=$1",
            lease.operation_id,
        )
    reads, errors = [], []
    describe = cloud.describe_access_entry

    async def release_control():
        async with harness.connect() as connection:
            await connection.execute(
                "UPDATE harness_approval_consumption SET reservation_state='released' "
                "WHERE operation_id=$1",
                artifact["source_operation_id"],
            )

    def observe(**parameters):
        reads.append(parameters)
        if case == "recovery-unreadable":
            raise TimeoutError("provider absence is unanswered")
        if case == "recovery-released-during-read":
            harness.run(release_control())
        return describe(**parameters)

    monkeypatch.setattr(cloud, "describe_access_entry", observe)

    async def resolve(grant, record):
        assert grant.lease.fence_token > lease.fence_token
        assert grant.principal == principal
        assert record.operation_id == lease.operation_id
        if case == "recovery-expired":
            async with harness.connect() as connection:
                await connection.execute(
                    "UPDATE harness_operation_leases SET expires_at="
                    "clock_timestamp()-interval '1 second' WHERE operation_id=$1",
                    lease.operation_id,
                )
        return owned, removals, None

    observer = RetirementRecoveryObserver(
        connect=harness.connect,
        principal=principal,
        resolve=resolve,
        control_context=SimpleNamespace(
            connect=harness.connect,
            domain_connect=harness.connect,
            policy=deployment,
            policy_fixture=True,
        ),
    )

    async def observed(*arguments):
        try:
            return await observer(*arguments)
        except Exception as error:
            errors.append(error)
            raise

    before = cloud.mutations
    async with harness.connect() as connection:
        report = await sweep_scoped_expired_leases(
            connection,
            principal=principal,
            candidates=frozenset({lease.operation_id}),
            observe_claim=observed,
            max_reconcile_attempts=1,
        )
        from harness_jobs.execution_plan import admitted_steps
        from harness_jobs.store import OperationStore

        record = await OperationStore().get(connection, principal, lease.operation_id)
        boundary = next(
            step
            for step in admitted_steps(record)
            if step.step_id == "revoke-control-entry"
        )
        stage = await connection.fetchval(
            "SELECT stage FROM harness_provider_call_intent WHERE idempotency_key=$1",
            step_key(record, boundary),
        )
        assert (
            await connection.fetchval(
                "SELECT sealed_revision FROM harness_allocation_seal WHERE allocation_id=$1",
                plan.original_allocation_id,
            )
            == "reviewed-paid-apply"
        )
        assert await connection.fetchval(
            "SELECT reservation_state FROM harness_approval_consumption WHERE operation_id=$1",
            artifact["source_operation_id"],
        ) == (
            "released"
            if case in {"recovery-control-released", "recovery-released-during-read"}
            else "confirmed"
        )
    if case == "recovery-absent":
        assert not errors, repr(errors)
        assert stage == CallStage.RECONCILED.value
        assert report.results[0].action == "retried"
        successor = await harness.lease(
            lease.operation_id, holder="successor", attempt="attempt-2"
        )
        provider = AsyncMock(side_effect=AssertionError("recovery replayed deletion"))
        server = ExecutionRPCServer(
            connect=harness.connect,
            provider_call=provider,
            authenticate=AsyncMock(
                return_value=ExecutionGrant(
                    replace(operation.grant.principal, subject=successor.holder),
                    successor,
                )
            ),
        )
        result = await server.dispatch(
            {
                "token": "successor",
                "method": "execute_step",
                "arguments": {"step_id": boundary.step_id},
            }
        )
        assert result[0]["outcome"] == "succeeded"
        provider.assert_not_awaited()
    else:
        assert stage != CallStage.RECONCILED.value
        if case in {"recovery-present", "recovery-replaced"}:
            assert not errors, repr(errors)
            assert reads
        else:
            assert errors
        if case in {
            "recovery-control-released",
            "recovery-artifact-missing",
            "recovery-expired",
        }:
            assert not reads
    assert cloud.mutations == before
