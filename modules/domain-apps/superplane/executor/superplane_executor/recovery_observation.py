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
    if operation_kind not in {"launch", "delete_cluster"} or not request_id:
        raise OperationRefused("journalled controller request unavailable")
    if await provider.sky.status(request_id) != "SUCCEEDED":
        return "unknown", None
    if operation_kind == "delete_cluster":
        return "succeeded", None
    instances = await provider.instances(operation, plan)
    if len(instances) != plan.data["node_count"]:
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
