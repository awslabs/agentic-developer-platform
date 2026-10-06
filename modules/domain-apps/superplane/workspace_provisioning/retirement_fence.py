"""Activate the original bootstrap fence and record its bounded live inventory.

The access approval authorizes closing admission and observation. The resulting
UID inventory is input to a separate teardown approval, never deletion authority
by itself. Adopted or shared clusters cannot enter this path.
"""

import asyncio
import re

from superplane_bootstrap.kube_grants import KubeGrants, _digest
from superplane_bootstrap.retirement_fence import WORKLOADS, documents

from .artifacts import digest
from .runtime_config import LifecycleRefused

KEY = "activate-retirement-fence"


def _original(inventory):
    if inventory.cluster_ownership != "adp-created" or not inventory.remove_namespace:
        raise LifecycleRefused("retirement fence requires exclusive managed ownership")
    rows = {g.spec.get("key"): g for g in inventory.grants}
    try:
        policy = rows["retirement-fence-policy"]
        binding = rows["retirement-fence-binding"]
        name = policy.spec["body"]["metadata"]["name"]
        generation = policy.spec["generation"]
        expected = documents(name, generation)
        if not re.fullmatch(r"sp-bootstrap-[a-f0-9]{24}-retirement", name):
            raise ValueError()
        for row, body in zip((policy, binding), expected, strict=True):
            if (
                row.spec["body"] != body
                or row.spec["generation"] != generation
                or row.spec["cluster_arn"] != inventory.cluster_arn
                or not row.identity.get("uid")
                or row.identity.get("generation") != generation
                or row.identity.get("digest") != _digest(body)
            ):
                raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise LifecycleRefused(
            "original bootstrap retirement fence is unavailable"
        ) from None
    return policy, binding


def review_recipe(inventory, runtime):
    policy, binding = _original(inventory)
    name = policy.spec["body"]["metadata"]["name"]
    return {
        KEY: {
            "service": "kubernetes",
            "method": "patch_validating_admission_policy",
            "account_id": inventory.cluster_arn.split(":")[4],
            "arguments": {
                "cluster_arn": inventory.cluster_arn,
                "name": name,
                "policy_uid": policy.identity["uid"],
                "binding_uid": binding.identity["uid"],
                "generation": policy.spec["generation"],
                "active_spec_sha256": digest(
                    documents(name, policy.spec["generation"], active=True)[0]["spec"]
                ),
            },
        }
    }


def _checked_body(grants, original, *, active):
    desired = documents(
        original.spec["body"]["metadata"]["name"],
        original.spec["generation"],
        active=active,
    )[0 if original.spec["body"]["kind"] == "ValidatingAdmissionPolicy" else 1]
    actual = grants._get(original.spec)
    if (
        actual is None
        or actual.get("metadata", {}).get("uid") != original.identity["uid"]
        or actual.get("metadata", {}).get("deletionTimestamp")
        or actual.get("metadata", {})
        .get("annotations", {})
        .get("superplane.aws-e/authority-generation")
        != original.spec["generation"]
        or actual.get("spec") != desired["spec"]
    ):
        raise LifecycleRefused(
            "retirement fence immutable identity or specification changed"
        )
    return actual


def verify(grants, inventory, metadata):
    metadata = validate_fence_metadata(metadata)
    if not isinstance(grants, KubeGrants):
        raise LifecycleRefused("retirement fence needs a pinned Kubernetes transport")
    policy, binding = _original(inventory)
    expected = review_recipe(inventory, {})[KEY]["arguments"]
    if metadata["identity"] != expected:
        raise LifecycleRefused("retirement fence belongs to a different bootstrap")
    body = _checked_body(grants, policy, active=True)
    _checked_body(grants, binding, active=True)
    status = body.get("status", {})
    if (
        not body["metadata"].get("generation")
        or status.get("observedGeneration") != body["metadata"]["generation"]
        or "typeChecking" not in status
        or status["typeChecking"].get("expressionWarnings")
    ):
        raise LifecycleRefused(
            "retirement admission fence is not observed and type checked"
        )
    return True


def snapshot(grants, inventory):
    """Original producer UIDs plus proven controller descendants, never adoption."""
    from superplane_bootstrap.workload_inventory import ownership_closure, read

    baseline = inventory.system_workload_baseline
    if (
        not isinstance(baseline, dict)
        or baseline.get("cluster_arn") != inventory.cluster_arn
    ):
        raise LifecycleRefused("original system workload ownership is unavailable")
    roots = [item["identity"] for item in baseline["objects"]]
    roots += [
        (
            item.desired["kind"],
            item.desired["metadata"].get("namespace", ""),
            item.desired["metadata"]["name"],
            item.identity["uid"],
        )
        for item in inventory.components
        if item.owned
    ]
    return ownership_closure(read(grants), roots)


def validate_fence_metadata(value):
    fields = {
        "version",
        "identity",
        "managed_workload_inventory",
        "managed_workload_inventory_sha256",
    }
    if not isinstance(value, dict) or set(value) != fields or value.get("version") != 1:
        raise LifecycleRefused("retirement fence evidence is malformed")
    identity = value["identity"]
    if (
        not isinstance(identity, dict)
        or set(identity)
        != {
            "cluster_arn",
            "name",
            "policy_uid",
            "binding_uid",
            "generation",
            "active_spec_sha256",
        }
        or not all(isinstance(v, str) and v for v in identity.values())
    ):
        raise LifecycleRefused("retirement fence identity is malformed")
    for field in ("generation", "active_spec_sha256"):
        if not re.fullmatch(r"[a-f0-9]{64}", identity[field]):
            raise LifecycleRefused("retirement fence digest is malformed")
    rows = value["managed_workload_inventory"]
    if (
        not isinstance(rows, (list, tuple))
        or len(rows) > 10000
        or any(
            not isinstance(row, (list, tuple))
            or len(row) != 4
            or not all(isinstance(v, str) for v in row)
            or not row[2]
            or not row[3]
            or row[0] not in {kind for _version, kind, _resource in WORKLOADS}
            for row in rows
        )
    ):
        raise LifecycleRefused("retirement workload inventory is malformed")
    normalized = [tuple(row) for row in rows]
    if (
        normalized != sorted(set(normalized))
        or digest(normalized) != value["managed_workload_inventory_sha256"]
    ):
        raise LifecycleRefused("retirement workload inventory digest changed")
    return value


async def prepare(facts, effects, clients):
    """Close admission under the paid access intent, then record approval input."""
    from .retirement_clients import cleanup_client

    if facts.operation.request.parameters.get("retirement_prepare_destroy") != "v1":
        raise LifecycleRefused("retirement fence activation was not approved")
    recipe = review_recipe(facts.inventory, facts.config)
    if effects.recipe.get(KEY) != recipe[KEY]:
        raise LifecycleRefused(
            "retirement fence differs from the approved access recipe"
        )
    previous = await effects.intend(KEY, recipe[KEY])
    async with cleanup_client(facts, effects, clients) as grants:
        policy, binding = _original(facts.inventory)
        if previous is not None:
            await asyncio.to_thread(verify, grants, facts.inventory, previous)
            return validate_fence_metadata(previous)
        await effects.authority()
        body = await asyncio.to_thread(_checked_body, grants, policy, active=False)
        await asyncio.to_thread(_checked_body, grants, binding, active=False)
        version = body["metadata"].get("resourceVersion")
        if not version:
            raise LifecycleRefused("retirement fence has no patch precondition")
        patch = [
            {"op": "test", "path": "/metadata/uid", "value": policy.identity["uid"]},
            {"op": "test", "path": "/metadata/resourceVersion", "value": version},
            {"op": "test", "path": "/spec", "value": policy.spec["body"]["spec"]},
            {
                "op": "replace",
                "path": "/spec/validations/0/expression",
                "value": "false",
            },
        ]
        await effects.authority()
        await asyncio.to_thread(
            grants._resource(policy.spec).patch,
            name=body["metadata"]["name"],
            body=patch,
            content_type="application/json-patch+json",
        )
        metadata = {
            "version": 1,
            "identity": recipe[KEY]["arguments"],
            "managed_workload_inventory": [],
            "managed_workload_inventory_sha256": digest([]),
        }
        deadline = asyncio.get_running_loop().time() + 30
        while True:
            await effects.authority()
            try:
                await asyncio.to_thread(verify, grants, facts.inventory, metadata)
                break
            except LifecycleRefused:
                if asyncio.get_running_loop().time() >= deadline:
                    raise
                await asyncio.sleep(1)
        rows = await asyncio.to_thread(snapshot, grants, facts.inventory)
        metadata.update(
            managed_workload_inventory=rows,
            managed_workload_inventory_sha256=digest(rows),
        )
        await effects.authority()
        await asyncio.to_thread(verify, grants, facts.inventory, metadata)
        await effects.confirm(KEY, recipe[KEY], metadata)
        return validate_fence_metadata(metadata)
