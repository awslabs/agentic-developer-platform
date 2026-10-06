"""Create only the approved finite cleanup grants and retain uncertain effects.

The trusted runtime supplies real EksGrants/KubeGrants and a fresh target guard.
No recovery path retries a create with unresolved intent. A failed run retains
its original journal and allocation so grant cleanup cannot disappear from the
accounting record.
"""

import asyncio

from .artifacts import canonical
from .runtime_config import LifecycleRefused


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
