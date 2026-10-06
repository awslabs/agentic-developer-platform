"""Actual recovery claim observes original UID ownership, never reruns a delete."""

import json
from dataclasses import replace

import pytest
from harness_jobs.execution import CallOutcome, CallStage
from harness_jobs.execution_plan import step_key
from harness_jobs.identity import OperationRefused, ResolvedPrincipal
from harness_jobs.recovery import sweep_scoped_expired_leases
from harness_jobs.store import OperationStore

from workspace_provisioning.retirement_plan import (
    DELETE_COMPONENT,
    compose_retirement_plan,
)
from workspace_provisioning.retirement_runtime import RetirementRecoveryObserver

from .postgres_bridge import Harness, requires_harness_postgres
from .test_retirement_adapters import owned as ownership_fixture
from .test_retirement_execution_postgres import _Cloud, _open, _principal, _Worker
from .test_retirement_terraform import reviewed

pytestmark = requires_harness_postgres


@pytest.fixture
def harness(tmp_path_factory, request):
    with Harness.started(tmp_path_factory, request.node.name) as instance:
        yield instance


@pytest.mark.parametrize("absent", [True, False])
def test_successor_observes_exact_original_component_without_replaying(
    harness, ownership_fixture, absent
):
    remover, api, original = ownership_fixture
    inventory = replace(original, org_id="org-a", workspace_id="ws-1")
    plan = compose_retirement_plan(inventory)
    boundary = next(
        step for step in plan.steps if step.operation_kind == DELETE_COMPONENT
    )
    principal = ResolvedPrincipal(
        org_id="org-a",
        workspace_id="ws-1",
        subject="user-1",
        permissions=frozenset({"workspace:recover"}),
    )

    async def scenario():
        progress = await _open(
            harness,
            plan,
            key=f"retire-recovered-{absent}",
            retirement_inventory=inventory,
        )
        cloud = _Cloud(lose_at=DELETE_COMPONENT)
        lease = await harness.lease(progress.operation_id, holder="predecessor")
        worker = _Worker(harness, cloud, lease)
        for step in plan.steps:
            await worker.step(step.step_id)
            if step == boundary:
                break
        before = list(cloud.calls)
        if absent:
            api.body = None

        async def resolve(grant, admitted):
            assert grant.principal == principal
            assert grant.lease.operation_id == progress.operation_id
            assert grant.lease.fence_token > lease.fence_token
            assert admitted.operation_id == progress.operation_id
            return inventory, remover, None

        observer = RetirementRecoveryObserver(
            connect=harness.connect, principal=principal, resolve=resolve
        )
        async with harness.connect() as connection:
            record = await OperationStore().get(
                connection, _principal(), progress.operation_id
            )
        with pytest.raises(PermissionError, match="claim changed"):
            await observer(
                lease,
                step_key(record, boundary),
                boundary.provider,
                boundary.operation_kind,
                boundary.target,
            )
        errors = []

        async def observed(claim, *arguments):
            try:
                return await observer(claim, *arguments)
            except Exception as exc:
                errors.append(exc)
                raise

        async with harness.connect() as connection:
            await connection.execute(
                "UPDATE harness_operation_leases SET expires_at="
                "clock_timestamp()-interval '1 second' WHERE operation_id=$1",
                progress.operation_id,
            )
            report = await sweep_scoped_expired_leases(
                connection,
                principal=principal,
                candidates=frozenset({progress.operation_id}),
                observe_claim=observed,
                max_reconcile_attempts=1,
            )
            record = await OperationStore().get(
                connection, _principal(), progress.operation_id
            )
            row = await connection.fetchrow(
                "SELECT stage, outcome FROM harness_provider_call_intent "
                "WHERE idempotency_key=$1",
                step_key(record, boundary),
            )
            assert not errors, repr(errors)
            assert row["stage"] == (
                CallStage.RECONCILED.value if absent else CallStage.UNRESOLVED.value
            )
            assert row["outcome"].split(":", 1)[0] == (
                CallOutcome.SUCCEEDED.value if absent else CallOutcome.UNKNOWN.value
            )
            assert report.results[0].action == ("retried" if absent else "unknown")
        assert cloud.calls == before
        if absent:
            successor = await harness.lease(
                progress.operation_id, holder="successor", attempt="attempt-2"
            )
            resumed = _Worker(harness, cloud, successor)
            for step in plan.steps:
                await resumed.step(step.step_id)
            assert cloud.count(DELETE_COMPONENT) == 1
        assert api.deletes == []

    harness.run(scenario())


@pytest.mark.parametrize("changed", ["intact", "missing", "replaced", "allocation"])
def test_successor_requires_original_reviewed_destroy_before_observing(
    harness, ownership_fixture, reviewed, tmp_path, changed
):
    remover, api, original = ownership_fixture
    inventory = replace(original, org_id="org-a", workspace_id="ws-1")
    source, _, parameters = reviewed
    target = {
        **source.target,
        "org_id": inventory.org_id,
        "workspace_id": inventory.workspace_id,
        "account_id": inventory.cluster_arn.split(":")[4],
        "aws_region": inventory.cluster_arn.split(":")[3],
    }
    document = json.loads(source.authorization.read_text())
    document.update(target)
    authorization = tmp_path / "retirement-authorization.json"
    authorization.write_text(json.dumps(document))
    artifact = replace(
        source,
        original_allocation_id="original-workspace-allocation",
        authorization=authorization,
        target=target,
    )
    plan = compose_retirement_plan(inventory, managed_destroy=artifact)
    boundary = next(
        step for step in plan.steps if step.operation_kind == DELETE_COMPONENT
    )
    principal = ResolvedPrincipal(
        org_id="org-a",
        workspace_id="ws-1",
        subject="user-1",
        permissions=frozenset({"workspace:recover"}),
    )

    async def scenario():
        progress = await _open(
            harness,
            plan,
            key=f"retire-managed-recovered-{changed}",
            retirement_inventory=inventory,
            approved_parameters={
                **parameters,
                "allocation_id": "original-workspace-allocation",
                "original_allocation_id": "original-workspace-allocation",
            },
        )
        cloud = _Cloud(lose_at=DELETE_COMPONENT)
        lease = await harness.lease(progress.operation_id, holder="predecessor")
        worker = _Worker(harness, cloud, lease)
        for step in plan.steps:
            await worker.step(step.step_id)
            if step == boundary:
                break
        before = list(cloud.calls)
        api.body = None
        if changed == "replaced":
            artifact.plan_file.write_bytes(b"replacement")

        async def resolve(grant, admitted):
            assert grant.lease.fence_token > lease.fence_token
            assert admitted.operation_id == progress.operation_id
            if changed == "missing":
                return inventory, remover, None
            if changed == "allocation":
                return inventory, remover, replace(
                    artifact, original_allocation_id="another-allocation"
                )
            return inventory, remover, artifact

        observer = RetirementRecoveryObserver(
            connect=harness.connect, principal=principal, resolve=resolve
        )
        errors = []

        async def observed(*args):
            try:
                return await observer(*args)
            except OperationRefused as exc:
                errors.append(str(exc))
                raise

        async with harness.connect() as connection:
            await connection.execute(
                "UPDATE harness_operation_leases SET expires_at="
                "clock_timestamp()-interval '1 second' WHERE operation_id=$1",
                progress.operation_id,
            )
            report = await sweep_scoped_expired_leases(
                connection,
                principal=principal,
                candidates=frozenset({progress.operation_id}),
                observe_claim=observed,
                max_reconcile_attempts=1,
            )
            record = await OperationStore().get(
                connection, _principal(), progress.operation_id
            )
            row = await connection.fetchrow(
                "SELECT stage FROM harness_provider_call_intent WHERE idempotency_key=$1",
                step_key(record, boundary),
            )
        if changed == "intact":
            assert not errors
            assert row["stage"] == CallStage.RECONCILED.value
            assert report.results[0].action == "retried"
        else:
            assert errors
            assert row["stage"] != CallStage.RECONCILED.value
        assert cloud.calls == before
        assert api.deletes == []

    harness.run(scenario())
