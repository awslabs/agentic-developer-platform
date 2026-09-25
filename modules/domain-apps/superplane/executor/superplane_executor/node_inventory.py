"""Exact dedicated Node identities in the existing fenced allocation inventory."""

import json
import re
from urllib.parse import quote

from harness_jobs.execution_plan import admitted_steps, step_key
from harness_jobs.identity import OperationRefused
from harness_jobs.inventory import (
    AllocationResource,
    ResourceObservation,
    ResourcePresence,
)
from harness_jobs.store import _record

PREFIX = "node:v1:"
KIND = "kubernetes_node"


def enabled(operation):
    return "controller_deployment_id" in getattr(
        getattr(operation, "request", None), "parameters", {}
    )


def reference(cluster_id, name, uid, provider_id, region):
    parts = [cluster_id, name, uid, provider_id, region]
    if any(not isinstance(v, str) or not v for v in parts):
        raise OperationRefused("original Node identity incomplete")
    value = PREFIX + json.dumps(parts, separators=(",", ":"), ensure_ascii=True)
    if len(value) > 255:
        raise OperationRefused("original Node identity exceeds supported bound")
    decode(value)
    return value


def decode(value):
    try:
        if (
            not isinstance(value, str)
            or not value.startswith(PREFIX)
            or len(value) > 255
        ):
            raise ValueError()
        parts = json.loads(value[len(PREFIX) :])
        if not isinstance(parts, list) or len(parts) != 5:
            raise ValueError()
        cluster, name, uid, provider_id, region = parts
        if any(not isinstance(v, str) for v in parts) or (
            not re.fullmatch(r"[a-f0-9-]{36}", cluster)
            or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,252}", name)
            or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", uid)
            or not re.fullmatch(r"[a-z]{2}-[a-z]+-[0-9]", region)
            or not re.fullmatch(
                r"aws:///"
                + re.escape(region)
                + r"(?:[a-z]|-[a-z0-9-]+)/i-[a-f0-9]{17}",
                provider_id,
            )
            or value
            != PREFIX + json.dumps(parts, separators=(",", ":"), ensure_ascii=True)
        ):
            raise ValueError()
        return dict(
            cluster_id=cluster,
            name=name,
            uid=uid,
            provider_id=provider_id,
            region=region,
        )
    except (ValueError, TypeError):
        raise OperationRefused("original Node reference invalid") from None


async def creating_keys(provider, operation, plan):
    lease = operation.grant.lease
    source = operation.request.parameters.get(
        "controller_source_operation_id", lease.operation_id
    )
    async with provider.execution_pool.acquire() as c:
        row = await c.fetchrow(
            "SELECT * FROM harness_operations WHERE operation_id=$1 AND org_id=$2 AND workspace_id=$3",
            source,
            lease.org_id,
            lease.workspace_id,
        )
        calls = await c.fetch(
            "SELECT idempotency_key,provider,operation_kind,target FROM harness_provider_call_intent WHERE operation_id=$1 AND org_id=$2 AND workspace_id=$3",
            source,
            lease.org_id,
            lease.workspace_id,
        )
    if row is None:
        raise OperationRefused("original Node creating operation unavailable")
    record = _record(row)
    request = record.admitted_request()
    # Plan.read expands the admitted CA and regional bindings. Compare the
    # immutable request inputs, not raw source JSON against that expanded plan.
    bindings = (
        "allocation_id",
        "controller_plan",
        "controller_certificate_authority",
        "controller_regions",
        "controller_node_bootstrap",
        "controller_network_cluster",
        "controller_network_regions",
    )
    if request.action != "provision" or any(
        request.parameters.get(key) != operation.request.parameters.get(key)
        for key in bindings
    ):
        raise OperationRefused("original Node creating plan differs")
    approved = {step_key(record, step): step for step in admitted_steps(record)}
    keys = set()
    for call in calls:
        step = approved.get(call["idempotency_key"])
        if (
            step is not None
            and (call["provider"], call["operation_kind"], call["target"])
            == (step.provider, step.operation_kind, step.target)
            and (
                step.provider == "aws"
                and step.operation_kind in {"launch", "run-node-bootstrap"}
                and step.target == plan.cluster_name
            )
        ):
            keys.add(call["idempotency_key"])
    return frozenset(keys)


def node_identity(node, target, plan, workspace_id, expected):
    try:
        metadata, spec = node["metadata"], node["spec"]
        provider_id = spec["providerID"]
        region, zone = expected[provider_id]
        labels = metadata["labels"]
        if any(
            labels.get(k) != v
            for k, v in {
                "superplane.ai/capacity": plan.cluster_name,
                "superplane.ai/workspace": workspace_id,
                "topology.kubernetes.io/region": region,
                "topology.kubernetes.io/zone": zone,
            }.items()
        ):
            raise ValueError()
        return reference(
            target["cluster_id"], metadata["name"], metadata["uid"], provider_id, region
        )
    except (KeyError, TypeError, ValueError):
        raise OperationRefused(
            "Node differs from original dedicated allocation"
        ) from None


async def discover(provider, operation, target, plan, resources, instances):
    if not enabled(operation):
        return {}
    provider.workspace.require_dedicated_node_authority(target)
    keys = await creating_keys(provider, operation, plan)
    known = {r.provider_reference: r for r in resources.values() if r.kind == KIND}
    expected = {}
    for instance in instances:
        region, zone = (
            instance["SuperplaneRegion"],
            instance["Placement"]["AvailabilityZone"],
        )
        native = plan.resource_reference("instance", instance["InstanceId"], region)
        resource = resources.get(native)
        if (
            resource is None
            or resource.kind != "instance"
            or not resource.operation_keys & keys
        ):
            raise OperationRefused("Node lacks original creating instance membership")
        expected[f"aws:///{zone}/{instance['InstanceId']}"] = (region, zone)
    originals = {}
    for ref, resource in known.items():
        identity = decode(ref)
        if (
            identity["cluster_id"] != target["cluster_id"]
            or not resource.operation_keys & keys
        ):
            raise OperationRefused("retained Node source provenance differs")
        provider_id = identity["provider_id"]
        if provider_id in originals and originals[provider_id] != ref:
            raise OperationRefused("original instance has conflicting Node identities")
        originals[provider_id] = ref
        expected[provider_id] = (identity["region"], provider_id.split("/")[3])
    response = await provider.workspace.request(
        operation, target, "GET", "/api/v1/nodes?limit=257"
    )
    if response.status_code != 200:
        raise OperationRefused("complete dedicated Node listing unavailable")
    body = response.json()
    if (
        not isinstance(body, dict)
        or body.get("metadata", {}).get("continue")
        or (not isinstance(body.get("items"), list) or len(body["items"]) > 256)
    ):
        raise OperationRefused("complete bounded Node listing required")
    found, seen = dict(known), set()
    for node in body["items"]:
        if not isinstance(node, dict) or not isinstance(node.get("spec"), dict):
            raise OperationRefused("malformed Node listing")
        provider_id = node["spec"].get("providerID")
        if provider_id not in expected:
            continue
        ref = node_identity(
            node, target, plan, operation.grant.lease.workspace_id, expected
        )
        if provider_id in seen or (
            provider_id in originals and originals[provider_id] != ref
        ):
            raise OperationRefused("original Node was replaced or duplicated")
        seen.add(provider_id)
        found[ref] = AllocationResource(
            ref,
            "aws",
            ref,
            KIND,
            keys | (known[ref].operation_keys if ref in known else frozenset()),
        )
    return found


async def read(provider, operation, target, plan, resource):
    provider.workspace.require_dedicated_node_authority(target)
    identity = decode(resource.provider_reference)
    if identity["cluster_id"] != target["cluster_id"]:
        raise OperationRefused("original Node cluster differs")
    response = await provider.workspace.request(
        operation, target, "GET", "/api/v1/nodes/" + quote(identity["name"], safe="")
    )
    if response.status_code == 404:
        return identity, None
    if response.status_code != 200:
        raise OperationRefused("original Node observation refused")
    node = response.json()
    expected = {
        identity["provider_id"]: (
            identity["region"],
            identity["provider_id"].split("/")[3],
        )
    }
    if (
        node_identity(node, target, plan, operation.grant.lease.workspace_id, expected)
        != resource.provider_reference
    ):
        raise OperationRefused("original Node UID changed")
    return identity, node


async def observe(provider, operation, target, plan, resource):
    try:
        _, node = await read(provider, operation, target, plan, resource)
        return ResourceObservation(
            ResourcePresence.ABSENT if node is None else ResourcePresence.PRESENT,
            resource.provider_reference,
            None
            if node is None
            else (
                "deleting"
                if node["metadata"].get("deletionTimestamp")
                else "registered"
            ),
        )
    except Exception:
        return ResourceObservation(
            ResourcePresence.UNKNOWN,
            resource.provider_reference,
            detail="original Node not verified",
        )
