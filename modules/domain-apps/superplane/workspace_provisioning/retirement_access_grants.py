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

    async def call(method, *arguments):
        await effects.authority()
        await verify_target()
        result = await asyncio.to_thread(method, *arguments)
        await effects.authority()
        return result

    observed = {}
    for spec in plan.grants:
        key = spec["key"]
        adapter = eks if spec["kind"] in {"eks-entry", "eks-policy"} else kubernetes
        # The durable intent always precedes the provider call. Existing intended
        # evidence raises here; provider observation in recovery has a distinct
        # authority path and must not be confused with permission to replay.
        previous = await effects.intend(key, recipe[key])
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
    return recorded
