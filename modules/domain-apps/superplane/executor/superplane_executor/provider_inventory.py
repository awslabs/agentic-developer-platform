"""Fresh provider observations for the protected domain recovery endpoint.

Only the trusted domain composition supplies ``provider`` and ``context``. The
recovery worker receives resource evidence, never the provider's credentials.
The existing executor finalizer owns AWS/Kubernetes enumeration and observation.
"""

from harness_jobs.identity import OperationRefused
from harness_jobs.leases import lock_lease
from superplane_executor.inventory import Finalizer


def _identity(lease):
    return tuple(
        getattr(lease, field)
        for field in (
            "operation_id",
            "org_id",
            "workspace_id",
            "holder",
            "attempt_id",
            "fence_token",
        )
    )


class ProviderInventory(Finalizer):
    def __init__(self, *, provider, context):
        # No execution assignment or controller credential is manufactured here.
        # context resolves the original admitted operation from the recovery claim.
        self.provider = provider
        self.resolve_context = context

    async def snapshot(self, lease, allocation_id, query_id):
        if not isinstance(query_id, str) or not query_id.strip() or len(query_id) > 256:
            raise OperationRefused("inventory query identity required")
        operation, target, plan = await self.resolve_context(lease)
        if (
            _identity(operation.grant.lease) != _identity(lease)
            or operation.request.parameters.get("allocation_id") != allocation_id
            or not allocation_id
        ):
            raise OperationRefused(
                "inventory does not match the original allocation claim"
            )
        async with self.provider.execution_pool.acquire() as connection:
            async with connection.transaction():
                if not await lock_lease(connection, lease):
                    raise OperationRefused("inventory claim expired")
                calls = await connection.fetch(
                    "SELECT * FROM harness_provider_call_intent WHERE operation_id=$1",
                    lease.operation_id,
                )

        # discover always queries the provider, retaining handles no longer present
        # in listings. Failure propagates; a SQL-only/empty fallback is never complete.
        resources = await self.discover(operation, target, plan, calls)
        result = []
        identifiers = set()
        for resource in resources.values():
            if resource.resource_id in identifiers:
                raise OperationRefused("duplicate provider inventory identity")
            identifiers.add(resource.resource_id)
            observation = await self.observe(operation, target, plan, resource)
            result.append(
                {
                    "resource_id": resource.resource_id,
                    "provider": resource.provider,
                    "provider_reference": resource.provider_reference,
                    "kind": resource.kind,
                    "operation_keys": sorted(resource.operation_keys),
                    "presence": observation.presence.value,
                    "provider_state": observation.provider_state,
                    "detail": observation.detail or "",
                }
            )
        current, current_target, current_plan = await self.resolve_context(lease)
        if (
            _identity(current.grant.lease) != _identity(lease)
            or current.request != operation.request
            or current_target != target
            or current_plan != plan
        ):
            raise OperationRefused("inventory authority changed during provider reads")
        async with self.provider.execution_pool.acquire() as connection:
            async with connection.transaction():
                if not await lock_lease(connection, lease):
                    raise OperationRefused(
                        "inventory claim expired during provider reads"
                    )
        return {"complete": True, "resources": result}
