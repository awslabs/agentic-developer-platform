"""Observe original controller requests without inventing resource handles."""

from harness_jobs.identity import OperationRefused


async def observe_request(provider, operation, plan, *, operation_kind, request_id):
    """Resolve a journalled request to the same resource identity as execution.

    SkyPilot's request ID names an asynchronous transport request, not a billable
    resource. Returning it as a resource handle makes a successful recovery
    permanently fail allocation completeness. A launch must rediscover its actual
    instance; removal returns no new resource and still needs full inventory before
    settlement can establish absence.
    """
    from .node_command_plan import KINDS

    if operation_kind in KINDS and plan.node_bootstrap is not None:
        from .node_command_inventory import recover

        return await recover(provider, operation, plan, operation_kind, request_id)
    if operation_kind not in {"launch", "delete_cluster"} or not request_id:
        raise OperationRefused("journalled controller request unavailable")
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
        return "succeeded", None
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
