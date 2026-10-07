"""Reconcile the separately approved temporary EKS cleanup allocation."""

import asyncio

from harness_jobs.inventory import (
    AllocationResource,
    InventoryAuthority,
    ResourceObservation,
    ResourcePresence,
)
from harness_jobs.store import stored_outcome

from .artifacts import read_artifact
from .retirement_access_artifact import validate_access_artifact
from .retirement_control import resolve_managed_control
from .runtime_config import LifecycleRefused


async def settle_control_allocation(
    operation,
    context,
    inventory,
    *,
    eks,
    cluster_absent,
    authorize,
    authenticate,
    token,
):
    lease = operation.grant.lease
    allocation = operation.request.parameters["control_allocation_id"]
    plan, paid = await resolve_managed_control(operation, inventory, context)
    artifact = await read_artifact(
        context.domain_connect,
        artifact_id=operation.request.parameters["retirement_access_artifact_id"],
        org_id=lease.org_id,
        workspace_id=lease.workspace_id,
        require_fresh=False,
    )
    identities = validate_access_artifact(artifact, plan)
    if len(plan.grants) != 1 or set(identities) != {"cleaner-entry"}:
        raise LifecycleRefused("control allocation has no finite original membership")
    async with context.connect() as connection:
        calls = await connection.fetch(
            "SELECT * FROM harness_provider_call_intent WHERE org_id=$1 AND workspace_id=$2 AND allocation_id=$3",
            lease.org_id,
            lease.workspace_id,
            allocation,
        )
    if len(calls) != 1 or any(
        row["operation_id"] != artifact["source_operation_id"]
        or row["provider"] != "superplane-lifecycle"
        or row["operation_kind"] != "prepare-retirement-access"
        or stored_outcome(row["outcome"]) != "succeeded"
        or row["stage"] not in {"observed", "reconciled"}
        or row["provider_ref"] is not None
        for row in calls
    ):
        raise LifecycleRefused(
            "control allocation has unresolved or unexpected creation"
        )
    reference = identities["cleaner-entry"]["arn"]
    member = AllocationResource(
        reference,
        "aws",
        reference,
        "eks-access-entry",
        frozenset({calls[0]["idempotency_key"]}),
    )

    async def current():
        await authorize()
        if await resolve_managed_control(operation, inventory, context) != (plan, paid):
            raise LifecycleRefused("control allocation source changed")
        async with context.connect() as connection:
            fresh = await connection.fetch(
                "SELECT * FROM harness_provider_call_intent WHERE org_id=$1 AND workspace_id=$2 AND allocation_id=$3",
                lease.org_id,
                lease.workspace_id,
                allocation,
            )
        if fresh != calls:
            raise LifecycleRefused("control allocation creation journal changed")

    async def observe():
        await current()
        absent = await cluster_absent()
        if not absent:
            absent = await asyncio.to_thread(eks.observe, plan.grants[0]) is None
        await current()
        return ResourceObservation(
            ResourcePresence.ABSENT if absent else ResourcePresence.PRESENT, reference
        )

    async def query(grant_lease, resources, _query_id):
        if grant_lease != lease or tuple(resources) != (member,):
            raise LifecycleRefused("control allocation query changed membership")
        return {reference: await observe()}

    authority = InventoryAuthority(
        connect=context.connect,
        authenticate=authenticate,
        query_provider=query,
        related_allocation_id=allocation,
    )
    await current()
    async with context.connect() as connection:
        sealed = await connection.fetchval(
            "SELECT sealed_revision FROM harness_allocation_seal WHERE org_id=$1 AND workspace_id=$2 AND allocation_id=$3",
            lease.org_id,
            lease.workspace_id,
            allocation,
        )
        if not sealed:
            await authority.enumerate_resources(connection, lease, resources=(member,))
        attempts = {
            provider: await authority.begin_provider_enumeration(
                connection, lease, provider=provider
            )
            for provider in ("aws", "superplane-lifecycle")
        }
    observed = await observe()
    async with context.connect() as connection:
        for provider, attempt in attempts.items():
            await authority.record_provider_enumeration(
                connection,
                lease,
                provider=provider,
                attempt=attempt,
                provider_references=frozenset({reference})
                if provider == "aws" and observed.presence is ResourcePresence.PRESENT
                else frozenset(),
            )
        await authority.seal_allocation(connection, lease)
        observations = await authority.observe_report(connection, lease)
        await authority.publish_report(connection, lease, observations=observations)
    assessment = await authority.assess_cleanup(
        executor_id=lease.holder,
        workspace_id=lease.workspace_id,
        allocation_id=allocation,
        operation_authority=token,
        observations=observations,
    )
    await current()
    if (
        not assessment.may_mark_released
        or not assessment.inventory
        or not assessment.inventory.complete
    ):
        raise LifecycleRefused("temporary control allocation retains exposure")
    return {
        "allocation_id": allocation,
        "source_operation_id": artifact["source_operation_id"],
        "inventory_complete": assessment.inventory.complete,
        "may_mark_released": assessment.may_mark_released,
        "exposure": assessment.exposure.value,
        "resource_dispositions": {
            key: value.value for key, value in assessment.dispositions
        },
    }
