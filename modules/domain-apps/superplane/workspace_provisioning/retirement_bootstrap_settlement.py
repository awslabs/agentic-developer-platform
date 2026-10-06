"""Settle original bootstrap ownership under separately approved retirement."""

import asyncio
import json
from dataclasses import asdict

from harness_jobs.inventory import (
    AllocationResource,
    InventoryAuthority,
    ResourceObservation,
    ResourcePresence,
)
from harness_jobs.store import OperationStore, stored_outcome

from .artifacts import digest, read_artifact
from .bootstrap_runtime import original_managed_allocation
from .runtime_config import LifecycleRefused


async def bootstrap_source(operation, context, inventory):
    """Resolve exact immutable source and its paid parent, never a caller's ID."""
    lease = operation.grant.lease
    parameters = operation.request.parameters
    async with context.connect() as connection:
        source = await OperationStore().get(
            connection,
            operation.grant.principal,
            parameters["retirement_source_operation_id"],
        )
        approved = await connection.fetchval(
            "SELECT EXISTS(SELECT 1 FROM harness_approval_consumption "
            "WHERE operation_id=$1 AND org_id=$2 AND workspace_id=$3 AND plan_digest=$4 "
            "AND reservation_state IN ('confirmed','retained','released'))",
            parameters["retirement_source_operation_id"],
            lease.org_id,
            lease.workspace_id,
            parameters["retirement_source_payload_digest"],
        )
    if source is None:
        raise LifecycleRefused("bootstrap allocation has no original operation")
    request = source.admitted_request()
    allocation = request.parameters.get("allocation_id")
    if (
        source.action != "provision"
        or source.state != "succeeded"
        or not approved
        or request.parameters.get("lifecycle_phase") != "bootstrap-workspace"
        or (source.org_id, source.workspace_id) != (lease.org_id, lease.workspace_id)
        or source.plan_digest != parameters["retirement_source_payload_digest"]
        or source.job_id != parameters["retirement_source_job_id"]
        or source.attempt_id != parameters["retirement_source_attempt_id"]
        or not allocation
        or allocation
        in {parameters["allocation_id"], parameters["control_allocation_id"]}
        or allocation not in json.loads(parameters.get("cleanup_allocation_ids", "[]"))
        or digest(asdict(inventory)) != parameters["retirement_inventory_sha256"]
        or inventory.preserve_cluster
        or not inventory.components_complete
    ):
        raise LifecycleRefused(
            "bootstrap allocation differs from approved original ownership"
        )
    row = await read_artifact(
        context.domain_connect,
        artifact_id=request.parameters["lifecycle_artifact_id"],
        org_id=lease.org_id,
        workspace_id=lease.workspace_id,
        require_fresh=False,
    )
    if (
        request.parameters.get("lifecycle_source_operation_id")
        != row["source_operation_id"]
        or await original_managed_allocation(operation, context, row)
        != parameters["original_allocation_id"]
    ):
        raise LifecycleRefused(
            "bootstrap allocation lost its original paid infrastructure"
        )
    async with context.connect() as connection:
        calls = await connection.fetch(
            "SELECT * FROM harness_provider_call_intent WHERE org_id=$1 AND workspace_id=$2 AND allocation_id=$3 LIMIT 2",
            lease.org_id,
            lease.workspace_id,
            allocation,
        )
    if len(calls) != 1 or any(
        call["operation_id"] != source.operation_id
        or call["provider"] != "superplane-lifecycle"
        or call["operation_kind"] != "bootstrap-workspace"
        or call["provider_ref"] != inventory.cluster_arn
        or stored_outcome(call["outcome"]) != "succeeded"
        or call["stage"] not in {"observed", "reconciled"}
        for call in calls
    ):
        raise LifecycleRefused(
            "bootstrap allocation contains unverified provider creation"
        )
    return source, allocation, calls[0]["idempotency_key"]


async def settle_bootstrap_allocation(
    operation,
    context,
    inventory,
    *,
    observations,
    cluster_absent,
    authorize,
    authenticate,
    token,
):
    """Freshly enumerate and query original bootstrap resources through shared authority."""
    await authorize()
    source, allocation, creation_key = original = await bootstrap_source(
        operation, context, inventory
    )
    lease = operation.grant.lease
    # Production bootstrap records the actual target cluster as its wrapper receipt.
    # Preserve that handle separately from the paid infrastructure's own inventory.
    cluster = AllocationResource(
        inventory.cluster_arn,
        "superplane-lifecycle",
        inventory.cluster_arn,
        "bootstrap-cluster-reference",
        frozenset({creation_key}),
    )

    async def current():
        await authorize()
        if await bootstrap_source(operation, context, inventory) != original:
            raise LifecycleRefused(
                "bootstrap allocation source changed during settlement"
            )

    async def catalog():
        await current()
        resources = await asyncio.to_thread(
            observations.catalog,
            inventory,
            None,
            operation.request.parameters,
            {},
            frozenset({creation_key}),
            include_infrastructure=False,
        )
        resources[cluster.provider_reference] = cluster
        await current()
        return resources

    async def observe(resource):
        await current()
        if resource == cluster:
            absent = await cluster_absent()
            result = ResourceObservation(
                ResourcePresence.ABSENT if absent else ResourcePresence.PRESENT,
                cluster.provider_reference,
            )
        else:
            result = await asyncio.to_thread(observations.observe, inventory, resource)
        await current()
        return result

    resources = await catalog()

    async def query(query_lease, members, _query_id):
        if query_lease != lease or {
            member.resource_id: member for member in members
        } != {member.resource_id: member for member in resources.values()}:
            raise LifecycleRefused(
                "bootstrap allocation query changed original membership"
            )
        return {member.resource_id: await observe(member) for member in members}

    authority = InventoryAuthority(
        connect=context.connect,
        authenticate=authenticate,
        query_provider=query,
        related_allocation_id=allocation,
    )
    async with context.connect() as connection:
        sealed = await connection.fetchval(
            "SELECT sealed_revision FROM harness_allocation_seal WHERE org_id=$1 AND workspace_id=$2 AND allocation_id=$3",
            lease.org_id,
            lease.workspace_id,
            allocation,
        )
        if not sealed:
            await authority.enumerate_resources(
                connection, lease, resources=tuple(resources.values())
            )
        attempts = {
            provider: await authority.begin_provider_enumeration(
                connection, lease, provider=provider
            )
            for provider in sorted({member.provider for member in resources.values()})
        }
    listing_returned = False
    try:
        if await catalog() != resources:
            raise LifecycleRefused(
                "bootstrap membership changed during fresh enumeration"
            )
        present = set()
        for reference, member in resources.items():
            observation = await observe(member)
            if observation.presence is ResourcePresence.UNKNOWN:
                raise LifecycleRefused("bootstrap provider listing is unavailable")
            if observation.presence is ResourcePresence.PRESENT:
                present.add(reference)
        listing_returned = True
        async with context.connect() as connection:
            for provider, attempt in attempts.items():
                await authority.record_provider_enumeration(
                    connection,
                    lease,
                    provider=provider,
                    attempt=attempt,
                    provider_references=frozenset(
                        ref for ref in present if resources[ref].provider == provider
                    ),
                )
            await authority.seal_allocation(connection, lease)
            report = await authority.observe_report(connection, lease)
            await authority.publish_report(connection, lease, observations=report)
        assessment = await authority.assess_cleanup(
            executor_id=lease.holder,
            workspace_id=lease.workspace_id,
            allocation_id=allocation,
            operation_authority=token,
            observations=report,
        )
        await current()
        if (
            not assessment.may_mark_released
            or not assessment.inventory
            or not assessment.inventory.complete
        ):
            raise LifecycleRefused("bootstrap allocation retains resource exposure")
    except Exception:
        if not listing_returned:
            async with context.connect() as connection:
                for attempt in attempts.values():
                    await authority.fail_provider_enumeration(
                        connection, lease, attempt=attempt
                    )
        raise
    return {
        "allocation_id": allocation,
        "source_operation_id": source.operation_id,
        "inventory_complete": assessment.inventory.complete,
        "may_mark_released": assessment.may_mark_released,
        "exposure": assessment.exposure.value,
        "resource_dispositions": {
            key: value.value for key, value in assessment.dispositions
        },
    }
