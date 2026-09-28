"""Execute one original approved removal stage; observe completed stages only."""

import asyncio
from urllib.parse import quote

from harness_jobs.execution import CallOutcome
from harness_jobs.identity import OperationRefused
from harness_jobs.inventory import AllocationResource

from . import node_cleanup, node_inventory
from .cleanup_binding import source_for, validate
from .cleanup_snapshot import select
from .provider_inventory import ProviderInventory
from .recovery_workload import selected_call


async def context(provider, operation, target, plan, authorize):
    await authorize()
    lease = operation.grant.lease
    async with provider.execution_pool.acquire() as c:
        source = await source_for(
            c,
            org_id=lease.org_id,
            workspace_id=lease.workspace_id,
            deployment_id=operation.request.parameters["controller_deployment_id"],
            source_id=operation.request.parameters["controller_source_operation_id"],
        )
        await validate(c, source, operation.request, require_binding=True)
        _, snapshot = await select(c, source, plan.cleanup_graph)
    if (
        snapshot["cluster_id"] != target["cluster_id"]
        or snapshot["cluster_name"] != plan.cluster_name
    ):
        raise OperationRefused("staged cleanup original cluster differs")
    resources = {
        row["reference"]: AllocationResource(
            row["resource_id"],
            row["provider"],
            row["reference"],
            row["kind"],
            frozenset(row["operation_keys"]),
        )
        for row in snapshot["resources"]
    }
    await authorize()
    return snapshot, resources


async def guard(provider, operation, target, plan, snapshot, authorize):
    async def resolve(_):
        await authorize()
        return operation, target, plan

    observed = await ProviderInventory(provider=provider, context=resolve).snapshot(
        operation.grant.lease,
        snapshot["allocation_id"],
        "staged-cleanup-identity-check",
    )
    expected = {
        (
            r["resource_id"],
            r["reference"],
            r["provider"],
            r["kind"],
            tuple(r["operation_keys"]),
        )
        for r in snapshot["resources"]
    }
    if (
        not observed["complete"]
        or {
            (
                r["resource_id"],
                r["provider_reference"],
                r["provider"],
                r["kind"],
                tuple(r["operation_keys"]),
            )
            for r in observed["resources"]
        }
        != expected
        or any(r["presence"] == "unknown" for r in observed["resources"])
    ):
        raise OperationRefused(
            "cleanup discovery is incomplete or outside the approved snapshot"
        )
    await authorize()


async def terminated(provider, operation, plan, snapshot, authorize):
    session, _ = await provider.session_for(operation, plan)
    if not snapshot["compute"]:
        return False
    for ref in snapshot["compute"]:
        if ref.startswith("arn:"):
            parts = ref.split(":", 5)
            region, instance_id = parts[3], parts[5].removeprefix("instance/")
            if ref != plan.resource_reference("instance", instance_id, region):
                raise OperationRefused("original cleanup compute scope differs")
        else:
            if plan.data["version"] == 4:
                raise OperationRefused("original cleanup compute region unavailable")
            region, instance_id = plan.data["region"], ref
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
    return True


async def root(provider, operation, target, ref, authorize):
    prefix, kind, namespace, name, uid = ref.split(":")
    if prefix != "kubernetes" or namespace != target["namespace"]:
        raise OperationRefused("original cleanup root scope differs")
    path = provider.workspace.path(target, kind, name)
    response = await node_cleanup.checked_request(
        provider, operation, target, authorize, "GET", path
    )
    if response.status_code == 404:
        return path, uid, None
    if (
        response.status_code != 200
        or response.json().get("metadata", {}).get("uid") != uid
    ):
        raise OperationRefused("original cleanup root replaced or unavailable")
    return path, uid, response.json()


async def pods(provider, operation, target, plan, snapshot, resources, authorize):
    nodes = {ref: resources[ref] for ref in snapshot["nodes"]}
    return await node_cleanup.node_pods(
        provider,
        operation,
        target,
        plan,
        nodes,
        node_cleanup.roots(snapshot["roots"], target["namespace"]),
        authorize,
    )


async def completed(
    provider,
    operation,
    target,
    plan,
    stage,
    snapshot,
    resources,
    authorize,
    request_id=None,
):
    if stage.startswith("cordon:"):
        ref = snapshot["nodes"][int(stage.split(":")[1])]
        _, node = await node_inventory.read(
            provider, operation, target, plan, resources[ref]
        )
        await authorize()
        return (
            await terminated(provider, operation, plan, snapshot, authorize)
            if node is None
            else node.get("spec", {}).get("unschedulable") is True
        )
    if stage.startswith("root:"):
        _, _, obj = await root(
            provider,
            operation,
            target,
            snapshot["roots"][int(stage.split(":")[1])],
            authorize,
        )
        return obj is None
    if stage == "drain":
        for ref in snapshot["roots"]:
            _, _, obj = await root(provider, operation, target, ref, authorize)
            if obj is not None:
                return False
        return not await pods(
            provider, operation, target, plan, snapshot, resources, authorize
        )
    if stage == "down":
        if not request_id or await provider.sky.status(request_id) != "SUCCEEDED":
            return False
        return await terminated(provider, operation, plan, snapshot, authorize)
    if stage.startswith("node:"):
        ref = snapshot["nodes"][int(stage.split(":")[1])]
        _, node = await node_inventory.read(
            provider, operation, target, plan, resources[ref]
        )
        await authorize()
        return node is None and await terminated(
            provider, operation, plan, snapshot, authorize
        )
    if stage.startswith("network:"):
        from .cleanup_network import observe

        return await observe(
            provider,
            operation,
            target,
            plan,
            snapshot["network"][int(stage.split(":")[1])],
            authorize,
        )
    if stage == "inventory":
        from .recovery_observation import removal_complete

        return await removal_complete(
            provider,
            operation,
            target,
            plan,
            {"idempotency_key": "staged-final-inventory"},
            authorize,
        )
    raise OperationRefused("unsupported admitted cleanup stage")


async def execute(provider, operation, target, plan, call, stage, authorize):
    snapshot, resources = await context(provider, operation, target, plan, authorize)
    await guard(provider, operation, target, plan, snapshot, authorize)
    request_id = None
    if stage.startswith("cordon:"):
        await pods(provider, operation, target, plan, snapshot, resources, authorize)
        ref = snapshot["nodes"][int(stage.split(":")[1])]
        identity, node = await node_inventory.read(
            provider, operation, target, plan, resources[ref]
        )
        if node is not None:
            version = node.get("metadata", {}).get("resourceVersion")
            if not isinstance(version, str) or not version:
                raise OperationRefused("original Node resource version unavailable")
            response = await node_cleanup.checked_request(
                provider,
                operation,
                target,
                authorize,
                "PATCH",
                "/api/v1/nodes/" + quote(identity["name"], safe=""),
                headers={"Content-Type": "application/json-patch+json"},
                body=[
                    {"op": "test", "path": "/metadata/uid", "value": identity["uid"]},
                    {
                        "op": "test",
                        "path": "/metadata/resourceVersion",
                        "value": version,
                    },
                    {"op": "add", "path": "/spec/unschedulable", "value": True},
                ],
            )
            if response.status_code != 200:
                raise OperationRefused("original Node cordon unresolved")
    elif stage.startswith("root:"):
        await pods(provider, operation, target, plan, snapshot, resources, authorize)
        path, uid, obj = await root(
            provider,
            operation,
            target,
            snapshot["roots"][int(stage.split(":")[1])],
            authorize,
        )
        if obj is not None:
            response = await node_cleanup.checked_request(
                provider,
                operation,
                target,
                authorize,
                "DELETE",
                path,
                body={
                    "apiVersion": "v1",
                    "kind": "DeleteOptions",
                    "propagationPolicy": "Foreground",
                    "preconditions": {"uid": uid},
                },
            )
            if response.status_code not in {200, 202, 404}:
                raise OperationRefused("original root deletion unresolved")
    elif stage == "down":
        if not await completed(
            provider, operation, target, plan, "drain", snapshot, resources, authorize
        ):
            raise OperationRefused("original workload drain is incomplete")
        async with provider.domain_pool.acquire() as c:
            found = await c.fetchval(
                "UPDATE controller_capacity SET state='retiring' WHERE org_id=$1 AND workspace_id=$2 AND cluster_name=$3 RETURNING cluster_name",
                call.org_id,
                call.workspace_id,
                plan.cluster_name,
            )
        if found is None:
            raise OperationRefused("original capacity ownership unavailable")
        await provider.remember(call, plan)
        await authorize()
        request_id = await provider.sky.submit(
            "/down",
            {
                "cluster_name": plan.cluster_name,
                "purge": False,
                "env_vars": plan.request_environment,
            },
        )
        await provider.remember(call, plan, request_id)
    elif stage.startswith("node:"):
        if not await terminated(provider, operation, plan, snapshot, authorize):
            raise OperationRefused("original compute termination is incomplete")
        ref = snapshot["nodes"][int(stage.split(":")[1])]
        identity, node = await node_inventory.read(
            provider, operation, target, plan, resources[ref]
        )
        if node is not None:
            response = await node_cleanup.checked_request(
                provider,
                operation,
                target,
                authorize,
                "DELETE",
                "/api/v1/nodes/" + quote(identity["name"], safe=""),
                body={
                    "apiVersion": "v1",
                    "kind": "DeleteOptions",
                    "preconditions": {"uid": identity["uid"]},
                },
            )
            if response.status_code not in {200, 202, 404}:
                raise OperationRefused("original Node deletion unresolved")
    elif stage.startswith("network:"):
        from .cleanup_network import execute as release

        if not await terminated(provider, operation, plan, snapshot, authorize):
            raise OperationRefused("original compute still retains networking")
        for ref in snapshot["nodes"]:
            _, node = await node_inventory.read(
                provider, operation, target, plan, resources[ref]
            )
            if node is not None:
                raise OperationRefused("original Node still retains networking")
        await release(
            provider,
            operation,
            target,
            plan,
            snapshot["network"][int(stage.split(":")[1])],
            authorize,
        )
    elif stage not in {"drain", "inventory"}:
        raise OperationRefused("unsupported admitted cleanup stage")
    async with asyncio.timeout(node_cleanup.remaining(operation, 120)):
        while not await completed(
            provider,
            operation,
            target,
            plan,
            stage,
            snapshot,
            resources,
            authorize,
            request_id,
        ):
            await asyncio.sleep(2)
    await authorize()
    return CallOutcome.SUCCEEDED, "Original cleanup stage confirmed", None


async def observe(provider, operation, target, plan, call, authorize, request_id):
    stage = selected_call(operation, plan, call).step_id
    snapshot, resources = await context(provider, operation, target, plan, authorize)
    await guard(provider, operation, target, plan, snapshot, authorize)
    result = await completed(
        provider,
        operation,
        target,
        plan,
        stage,
        snapshot,
        resources,
        authorize,
        request_id,
    )
    await authorize()
    return ("succeeded" if result else "unknown"), None
