"""Observe original controller requests without inventing resource handles."""

import asyncio

from harness_jobs.identity import OperationRefused


async def removal_complete(provider, operation, target, plan, call, authorize):
    """Observe every aggregate removal obligation; never resume mutations here."""
    from superplane_executor.provider_inventory import ProviderInventory

    if target is None or call is None or not callable(authorize):
        return False

    async def context(lease):
        await authorize()
        if lease != operation.grant.lease:
            raise OperationRefused("original cleanup recovery claim changed")
        return operation, target, plan

    inventory = ProviderInventory(provider=provider, context=context)
    snapshot = await inventory.snapshot(
        operation.grant.lease,
        operation.request.parameters["allocation_id"],
        call["idempotency_key"],
    )
    resources = snapshot["resources"]
    if (
        not snapshot["complete"]
        or not resources
        or any(resource["presence"] != "absent" for resource in resources)
    ):
        return False
    # Generic inventory permits EC2 NotFound as absence. Node removal requires
    # positive termination of original compute, so repeat exact ID reads here.
    originals = [r for r in resources if r["kind"] == "instance"]
    if not originals:
        return False
    session, _ = await provider.session_for(operation, plan)
    for resource in originals:
        reference = resource["provider_reference"]
        if reference.startswith("arn:"):
            parts = reference.split(":", 5)
            region = parts[3]
            instance_id = parts[5].removeprefix("instance/")
            if reference != plan.resource_reference("instance", instance_id, region):
                return False
        else:
            if plan.data["version"] == 4:
                return False
            region, instance_id = plan.data["region"], reference
        await authorize()
        response = await asyncio.to_thread(
            session.client("ec2", region_name=region).describe_instances,
            InstanceIds=[instance_id],
        )
        await authorize()
        instances = [i for r in response["Reservations"] for i in r["Instances"]]
        if (
            len(instances) != 1
            or instances[0].get("InstanceId") != instance_id
            or instances[0].get("State", {}).get("Name") != "terminated"
        ):
            return False
    await authorize()
    return True


async def observe_request(
    provider,
    operation,
    plan,
    *,
    operation_kind,
    request_id,
    target=None,
    call=None,
    authorize=None,
):
    """Resolve a journalled request to the same resource identity as execution.

    SkyPilot's request ID names an asynchronous transport request, not a billable
    resource. Returning it as a resource handle makes a successful recovery
    permanently fail allocation completeness. A launch must rediscover its actual
    instance; removal returns no new resource and still needs full inventory before
    settlement can establish absence.
    """
    from .node_command_plan import KINDS

    if operation_kind == "delete_cluster" and plan.cleanup_graph is not None:
        from .staged_cleanup import observe

        if target is None or call is None or not callable(authorize):
            return "unknown", None
        try:
            return await observe(
                provider, operation, target, plan, call, authorize, request_id
            )
        except Exception:  # noqa: BLE001 - uncertainty cannot authorize stage replay
            return "unknown", None
    if operation_kind in {"deploy", "status"}:
        from .recovery_workload import observe

        if target is None or call is None or not callable(authorize):
            raise OperationRefused("original workload recovery context unavailable")
        return await observe(provider, operation, target, plan, call, authorize)
    if operation_kind in KINDS and plan.node_bootstrap is not None:
        from .node_command_inventory import recover

        return await recover(provider, operation, plan, operation_kind, request_id)
    if operation_kind not in {"launch", "delete_cluster"}:
        raise OperationRefused("journalled controller request unavailable")
    if not request_id:
        # Captured capacity remains inventory exposure, never proof of request
        # terminality after a lost launch/down handle. Do not replay the POST.
        return "unknown", None
    if await provider.sky.status(request_id) != "SUCCEEDED":
        return "unknown", None
    if plan.network is not None:
        from harness_jobs.inventory import ResourcePresence

        from .network_inventory import observe_native, rows

        dependencies = await rows(provider, operation)
        if operation_kind == "delete_cluster":
            if any(row["released_at"] is None for row in dependencies):
                return "unknown", None
        else:
            # A successful SkyPilot request does not prove networking completed.
            # Unfinished subeffects remain under the original operation for recovery.
            if any(
                row["state"] != "present" or row["released_at"] is not None
                for row in dependencies
            ):
                return "unknown", None
            async with provider.domain_pool.acquire() as connection:
                finished = await connection.fetchval(
                    "SELECT compute_region FROM controller_network_completion WHERE operation_id=$1 AND allocation_id=$2 AND plan_digest=$3",
                    operation.grant.lease.operation_id,
                    operation.request.parameters["allocation_id"],
                    operation.plan_digest,
                )
            if not finished or (finished != plan.cluster_region and not dependencies):
                return "unknown", None
            session, _ = await provider.session_for(operation, plan)
            for dependency in dependencies:
                if (
                    await observe_native(
                        session,
                        plan.data["provider_account_id"],
                        {plan.cluster_region, *plan.network["regions"]},
                        dependency,
                        require_ready=True,
                    )
                    != ResourcePresence.PRESENT
                ):
                    return "unknown", None
    if operation_kind == "delete_cluster":
        try:
            complete = await removal_complete(
                provider, operation, target, plan, call, authorize
            )
        except Exception:  # noqa: BLE001 - incomplete reads retain the original intent
            complete = False
        return ("succeeded" if complete else "unknown"), None
    instances = await provider.instances(operation, plan)
    if len(instances) != plan.data["node_count"]:
        return "unknown", None
    if plan.network is not None and any(
        instance.get("SuperplaneRegion") != finished for instance in instances
    ):
        return "unknown", None
    references = [instance.get("InstanceId") for instance in instances]
    if any(not isinstance(ref, str) or not ref for ref in references) or len(
        set(references)
    ) != len(references):
        raise OperationRefused("recovered launch resource identity unavailable")
    references = [
        plan.resource_reference("instance", reference, instance.get("SuperplaneRegion"))
        for reference, instance in zip(references, instances, strict=True)
    ]
    return "succeeded", references[0]
