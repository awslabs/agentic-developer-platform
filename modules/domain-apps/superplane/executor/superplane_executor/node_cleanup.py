"""Guarded removal of original dedicated Nodes around the existing SkyPilot down."""

import asyncio
from datetime import UTC, datetime
from urllib.parse import quote

from harness_jobs.identity import OperationRefused
from harness_jobs.inventory import InventoryAuthority

from . import node_inventory


def remaining(operation, seconds):
    value = min(
        seconds,
        (operation.grant.lease.runtime_deadline - datetime.now(UTC)).total_seconds(),
    )
    if value <= 0:
        raise OperationRefused("original cleanup deadline exhausted")
    return value


async def checked_request(
    provider, operation, target, authorize, method, path, **kwargs
):
    await authorize()
    response = await provider.workspace.request(
        operation, target, method, path, **kwargs
    )
    await authorize()
    return response


async def prepare(provider, operation, target, plan, authorize):
    """Persist exact Node obligations, then cordon without requiring Ready/GPU."""
    from .inventory import Finalizer

    finalizer = Finalizer(provider, provider.registry)
    resources = await finalizer.known(
        operation.grant.lease, operation.request.parameters["allocation_id"]
    )
    await authorize()
    instances = await provider.instances(operation, plan, include_terminated=True)
    await authorize()
    nodes = await node_inventory.discover(
        provider, operation, target, plan, resources, instances
    )
    await authorize()
    new = tuple(resource for ref, resource in nodes.items() if ref not in resources)
    if new:
        authority = InventoryAuthority(
            connect=provider.execution_pool.acquire,
            authenticate=provider.registry.authenticate,
        )
        async with provider.execution_pool.acquire() as c:
            await authority.enumerate_resources(c, operation.grant.lease, resources=new)
    for resource in nodes.values():
        await authorize()
        identity, node = await node_inventory.read(
            provider, operation, target, plan, resource
        )
        await authorize()
        if node is None:
            continue
        version = node.get("metadata", {}).get("resourceVersion")
        if not isinstance(version, str) or not version:
            raise OperationRefused("Node resource version unavailable for cordon")
        response = await checked_request(
            provider,
            operation,
            target,
            authorize,
            "PATCH",
            "/api/v1/nodes/" + quote(identity["name"], safe=""),
            headers={"Content-Type": "application/json-patch+json"},
            body=[
                {"op": "test", "path": "/metadata/uid", "value": identity["uid"]},
                {"op": "test", "path": "/metadata/resourceVersion", "value": version},
                {"op": "add", "path": "/spec/unschedulable", "value": True},
            ],
        )
        if response.status_code != 200:
            raise OperationRefused("original Node cordon unresolved")
    return nodes, resources


def roots(known_references, namespace):
    result = {}
    for ref in known_references:
        try:
            prefix, kind, ns, name, uid = ref.split(":")
            if prefix != "kubernetes" or ns != namespace or not name or not uid:
                raise ValueError()
            result[(kind, name)] = uid
        except ValueError:
            raise OperationRefused("original workload identity invalid") from None
    return result


async def node_pods(
    provider, operation, target, plan, nodes, original_roots, authorize, known_pods=None
):
    """Return active owned Pods; refuse unrelated placement before compute removal."""
    active = []
    for resource in [None, *nodes.values()]:
        identity = (
            node_inventory.decode(resource.provider_reference) if resource else None
        )
        path = (
            "/api/v1/pods?fieldSelector="
            + quote("spec.nodeName=" + identity["name"], safe="")
            + "&limit=257"
            if identity
            else "/api/v1/namespaces/"
            + quote(target["namespace"], safe="")
            + "/pods?limit=257"
        )
        response = await checked_request(
            provider,
            operation,
            target,
            authorize,
            "GET",
            path,
        )
        body = response.json() if response.status_code == 200 else None
        if (
            not isinstance(body, dict)
            or body.get("metadata", {}).get("continue")
            or (not isinstance(body.get("items"), list) or len(body["items"]) > 256)
        ):
            raise OperationRefused("complete original Node Pod listing unavailable")
        for pod in body["items"]:
            metadata = pod.get("metadata", {})
            if (
                identity and pod.get("spec", {}).get("nodeName") != identity["name"]
            ) or not metadata.get("uid"):
                raise OperationRefused("Node Pod identity differs")
            owners = metadata.get("ownerReferences", [])
            controllers = [o for o in owners if o.get("controller") is True]
            if len(controllers) != 1:
                if (
                    not identity
                    and metadata.get("labels", {}).get("superplane.ai/capacity")
                    != plan.cluster_name
                ):
                    continue
                raise OperationRefused("Node contains unowned or ambiguous Pod")
            owner = controllers[0]
            namespace = metadata.get("namespace")
            fingerprint = (
                namespace,
                metadata.get("name"),
                pod.get("spec", {}).get("nodeName"),
                owner.get("kind"),
                owner.get("name"),
                owner.get("uid"),
                metadata.get("labels", {}).get("superplane.ai/capacity"),
            )
            if known_pods is not None and metadata["uid"] in known_pods:
                if known_pods[metadata["uid"]] != fingerprint:
                    raise OperationRefused("original Pod identity changed during drain")
                active.append(pod)
                continue
            if namespace == "kube-system" and owner.get("kind") == "DaemonSet":
                # Preserve the shared DaemonSet. Verify its actual UID, not merely
                # a caller-supplied owner kind on a foreign Pod.
                response = await checked_request(
                    provider,
                    operation,
                    target,
                    authorize,
                    "GET",
                    "/apis/apps/v1/namespaces/kube-system/daemonsets/"
                    + quote(owner.get("name", ""), safe=""),
                )
                if response.status_code != 200 or response.json().get(
                    "metadata", {}
                ).get("uid") != owner.get("uid"):
                    raise OperationRefused("system DaemonSet identity unavailable")
                continue
            if namespace != target["namespace"]:
                raise OperationRefused("original Node contains unrelated workload")
            if owner.get("kind") == "ReplicaSet":
                response = await checked_request(
                    provider,
                    operation,
                    target,
                    authorize,
                    "GET",
                    "/apis/apps/v1/namespaces/"
                    + quote(namespace, safe="")
                    + "/replicasets/"
                    + quote(owner.get("name", ""), safe=""),
                )
                replica = response.json() if response.status_code == 200 else {}
                if replica.get("metadata", {}).get("uid") != owner.get("uid"):
                    raise OperationRefused("original replica ownership unavailable")
                parents = [
                    o
                    for o in replica["metadata"].get("ownerReferences", [])
                    if o.get("controller") is True
                ]
                if len(parents) != 1:
                    raise OperationRefused("original replica controller unavailable")
                owner = parents[0]
            if original_roots.get((owner.get("kind"), owner.get("name"))) != owner.get(
                "uid"
            ):
                if (
                    not identity
                    and metadata.get("labels", {}).get("superplane.ai/capacity")
                    != plan.cluster_name
                ):
                    continue
                raise OperationRefused("Node Pod is not from original workload UID")
            if known_pods is not None:
                known_pods[metadata["uid"]] = fingerprint
            active.append(pod)
    return active


async def drain(provider, operation, target, plan, nodes, known_references, authorize):
    original_roots = roots(known_references, target["namespace"])
    known_pods = {}
    # Refuse foreign placement before deleting anything. Foreground deletion of
    # owned roots drains their Pods; no arbitrary eviction or forced finalizers.
    await node_pods(
        provider, operation, target, plan, nodes, original_roots, authorize, known_pods
    )
    await provider.workspace.delete(
        operation, target, plan, authorize, known_references=known_references
    )
    async with asyncio.timeout(remaining(operation, 120)):
        while True:
            pending = False
            for (kind, name), uid in original_roots.items():
                response = await checked_request(
                    provider,
                    operation,
                    target,
                    authorize,
                    "GET",
                    provider.workspace.path(target, kind, name),
                )
                if response.status_code == 200:
                    if response.json().get("metadata", {}).get("uid") != uid:
                        raise OperationRefused("workload replaced during drain")
                    pending = True
                elif response.status_code != 404:
                    raise OperationRefused("workload drain observation unavailable")
            pods = await node_pods(
                provider,
                operation,
                target,
                plan,
                nodes,
                original_roots,
                authorize,
                known_pods,
            )
            if not pending and not pods:
                return
            await asyncio.sleep(2)


async def terminated(provider, operation, plan, resources, authorize):
    """Require positive termination of every retained original EC2 identity."""
    session, _ = await provider.session_for(operation, plan)
    originals = [
        resource for resource in resources.values() if resource.kind == "instance"
    ]
    if not originals:
        raise OperationRefused("original compute inventory unavailable")
    async with asyncio.timeout(remaining(operation, 120)):
        while True:
            complete = True
            observed = []
            for resource in originals:
                ref = resource.provider_reference
                if ref.startswith("arn:"):
                    parts = ref.split(":", 5)
                    region = parts[3]
                    instance_id = parts[5].removeprefix("instance/")
                    if ref != plan.resource_reference("instance", instance_id, region):
                        raise OperationRefused("original EC2 reference differs")
                else:
                    region, instance_id = plan.data["region"], ref
                    if plan.data["version"] == 4:
                        raise OperationRefused("regional EC2 identity unavailable")
                await authorize()
                response = await asyncio.to_thread(
                    session.client("ec2", region_name=region).describe_instances,
                    InstanceIds=[instance_id],
                )
                await authorize()
                instances = [
                    i for r in response["Reservations"] for i in r["Instances"]
                ]
                if len(instances) != 1 or instances[0].get("InstanceId") != instance_id:
                    raise OperationRefused(
                        "positive original EC2 termination unavailable"
                    )
                complete = (
                    complete
                    and instances[0].get("State", {}).get("Name") == "terminated"
                )
                observed.append({**instances[0], "SuperplaneRegion": region})
            if complete:
                return observed
            await asyncio.sleep(2)


async def remove(
    provider, operation, target, plan, nodes, resources, instances, authorize
):
    await authorize()
    current = await node_inventory.discover(
        provider, operation, target, plan, {**resources, **nodes}, instances
    )
    await authorize()
    new = tuple(
        resource
        for ref, resource in current.items()
        if ref not in resources and ref not in nodes
    )
    if new:
        authority = InventoryAuthority(
            connect=provider.execution_pool.acquire,
            authenticate=provider.registry.authenticate,
        )
        async with provider.execution_pool.acquire() as c:
            await authority.enumerate_resources(c, operation.grant.lease, resources=new)
    nodes = current
    for resource in nodes.values():
        await authorize()
        identity, node = await node_inventory.read(
            provider, operation, target, plan, resource
        )
        await authorize()
        if node is None:
            continue
        response = await checked_request(
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
            raise OperationRefused("original Node removal unresolved")
    async with asyncio.timeout(remaining(operation, 60)):
        while True:
            pending = False
            for resource in nodes.values():
                await authorize()
                _, node = await node_inventory.read(
                    provider, operation, target, plan, resource
                )
                await authorize()
                pending = pending or node is not None
            if not pending:
                await authorize()
                current = await node_inventory.discover(
                    provider, operation, target, plan, {**resources, **nodes}, instances
                )
                await authorize()
                if set(current) != set(nodes):
                    raise OperationRefused("original Node registered during cleanup")
                return
            await asyncio.sleep(2)
