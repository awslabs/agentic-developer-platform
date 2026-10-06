"""Create only the approved finite cleanup grants and retain uncertain effects.

The trusted runtime supplies real EksGrants/KubeGrants and a fresh target guard.
No recovery path retries a create with unresolved intent. A failed run retains
its original journal and allocation so grant cleanup cannot disappear from the
accounting record.
"""

import asyncio

from .artifacts import canonical, digest
from .runtime_config import LifecycleRefused


def managed_revocation_recipe(plan, access_artifact):
    """Derive one exact deletion from a read_artifact-verified access row.

    The admitted retirement operation must bind this row and its producer.
    """
    from .retirement_access_artifact import validate_access_artifact
    from .retirement_managed_access import ManagedRetirementAccessPlan

    if (
        not isinstance(plan, ManagedRetirementAccessPlan)
        or len(plan.grants) != 1
        or plan.revocation_order != ("cleaner-entry",)
        or plan.grants[0].get("key") != "cleaner-entry"
        or plan.grants[0].get("kind") != "eks-entry"
    ):
        raise LifecycleRefused("managed revocation requires its finite EKS entry")
    identity = validate_access_artifact(access_artifact, plan)["cleaner-entry"]
    spec = plan.grants[0]
    return {
        "revoke-cleaner-entry-"
        + digest([access_artifact["artifact_id"], identity])[:24]: {
            "service": "eks",
            "method": "delete_access_entry",
            "account_id": plan.cluster_arn.split(":")[4],
            "arguments": {
                "clusterName": plan.cluster_arn.rsplit("/", 1)[-1],
                "principalArn": spec["principal_arn"],
            },
        }
    }


async def revoke_managed_access_grant(
    plan, access_artifact, effects, *, eks, verify_cluster
):
    from .retirement_access_artifact import validate_access_artifact

    recipe = managed_revocation_recipe(plan, access_artifact)
    if canonical(effects.recipe) != canonical(recipe):
        raise LifecycleRefused("managed revocation differs from its admitted recipe")
    spec = plan.grants[0]
    identity = validate_access_artifact(access_artifact, plan)["cleaner-entry"]
    key, descriptor = next(iter(recipe.items()))

    async def check():
        await effects.authority()
        await verify_cluster()

    await check()
    previous = await effects.intend(key, descriptor)
    if previous is not None:
        if previous != identity:
            raise LifecycleRefused("managed revocation confirmation changed")
        await check()
        if await asyncio.to_thread(eks.observe, spec) is not None:
            raise LifecycleRefused("confirmed managed revocation is still present")
        return identity
    await check()
    observed = await asyncio.to_thread(eks.observe, spec)
    if observed != identity:
        raise LifecycleRefused("managed access entry differs from approved readback")
    await check()
    await asyncio.to_thread(eks.delete, spec, identity)
    await check()
    if await asyncio.to_thread(eks.observe, spec) is not None:
        raise LifecycleRefused("managed revocation has no provider absence readback")
    await effects.confirm(key, descriptor, identity)
    return identity


async def establish_access_grants(plan, effects, *, eks, kubernetes, verify_target):
    recipe = plan.recipe()
    if canonical(recipe) != canonical(effects.recipe):
        raise LifecycleRefused("cleanup grant journal differs from the approved plan")

    async def call(method, *arguments, **options):
        await effects.authority()
        await verify_target()
        result = await asyncio.to_thread(method, *arguments, **options)
        await effects.authority()
        return result

    from .retirement_inventory import (
        OwnedGrant,
        RetainedCleanupCapability,
        require_cleanup_group_mapping,
        require_dormant_cleanup_group,
    )
    from .retirement_managed_access import ManagedRetirementAccessPlan

    capability = None
    if isinstance(plan, ManagedRetirementAccessPlan):
        if len(plan.grants) != 1 or len(plan.retained_grants) != 6:
            raise LifecycleRefused("managed access has an incomplete finite grant set")
        original = tuple(
            OwnedGrant(item["spec"], item["identity"]) for item in plan.retained_grants
        )
        capability = RetainedCleanupCapability(
            org_id=plan.org_id,
            workspace_id=plan.workspace_id,
            cluster_arn=plan.cluster_arn,
            group=plan.cleanup_group,
            generation=original[0].spec["generation"],
            original_allocation_id=plan.original_allocation_id,
            grants=original,
        )

    observed = {}
    for spec in plan.grants:
        key = spec["key"]
        adapter = eks if spec["kind"] in {"eks-entry", "eks-policy"} else kubernetes
        # The durable intent always precedes the provider call. Existing intended
        # evidence raises here; provider observation in recovery has a distinct
        # authority path and must not be confused with permission to replay.
        previous = await effects.intend(key, recipe[key])
        if capability is not None and previous is None:
            for original in capability.grants:
                current_grant = await call(kubernetes.observe, original.spec)
                if current_grant != original.identity:
                    raise LifecycleRefused(
                        "retained cleanup grant changed before mapping"
                    )
                kubernetes.verify(original.spec, current_grant)
            await call(require_dormant_cleanup_group, capability, eks)
        current = await call(adapter.observe, spec)
        if previous is not None:
            if current != previous:
                raise LifecycleRefused("confirmed cleanup grant changed or disappeared")
            adapter.verify(spec, current)
            observed[key] = current
            continue
        if current is not None:
            raise LifecycleRefused("cleanup grant already exists outside this intent")
        created = await call(adapter.create, spec)
        adapter.verify(spec, created)
        current = await call(adapter.observe, spec)
        if current is None or current != created:
            raise LifecycleRefused("new cleanup grant lacks exact provider readback")
        adapter.verify(spec, current)
        await effects.confirm(key, recipe[key], current)
        observed[key] = current

    recorded = await effects.complete()
    if canonical(recorded) != canonical(observed):
        raise LifecycleRefused("cleanup grant evidence differs from provider readback")
    # Earlier grants may have been replaced while later ones were being created.
    # Recheck the complete set before any immutable ready artifact can be written.
    for spec in plan.grants:
        adapter = eks if spec["kind"] in {"eks-entry", "eks-policy"} else kubernetes
        current = await call(adapter.observe, spec)
        if current != recorded[spec["key"]]:
            raise LifecycleRefused("cleanup grant changed before artifact publication")
        adapter.verify(spec, current)
    if capability is not None:
        for original in capability.grants:
            current_grant = await call(kubernetes.observe, original.spec)
            if current_grant != original.identity:
                raise LifecycleRefused("retained cleanup grant changed after mapping")
            kubernetes.verify(original.spec, current_grant)
        await call(
            require_cleanup_group_mapping,
            capability,
            eks,
            spec=plan.grants[0],
            identity=recorded["cleaner-entry"],
        )
    return recorded
