"""Use the maintained allocation authority for actual retirement observations.

Wire ``verify_step`` into RetirementRuntime and this object as ExecutionRPCServer's
after-step hook. Provider-call success and allocation/accounting release remain
separate decisions owned by the shared finalizer and InventoryAuthority.
"""

import asyncio
import json
from types import SimpleNamespace

from harness_jobs.effects import may_create
from harness_jobs.execution import CallOutcome
from harness_jobs.identity import OperationRefused
from harness_jobs.inventory import InventoryAuthority, ResourcePresence
from superplane_executor.inventory import Finalizer


class RetirementFinalizer(Finalizer):
    def __init__(
        self,
        *,
        execution_pool,
        domain_pool,
        resolve,
        observations,
        authenticate,
        token_for,
    ):
        self.provider = SimpleNamespace(
            execution_pool=execution_pool, domain_pool=domain_pool
        )
        self.resolve, self.observations, self.authority_token = (
            resolve,
            observations,
            token_for,
        )
        self.authority = InventoryAuthority(
            connect=execution_pool.acquire,
            authenticate=authenticate,
            query_provider=self.query,
        )

    async def context(self, operation_id):
        operation, inventory, artifact = await self.resolve(operation_id)
        lease = operation.grant.lease
        parameters = operation.request.parameters
        if (
            lease.operation_id != operation_id
            or operation.request.action != "teardown"
            or not parameters.get("allocation_id")
            or parameters["allocation_id"] != parameters.get("original_allocation_id")
            or (inventory.workspace_id, inventory.org_id)
            != (lease.workspace_id, lease.org_id)
        ):
            raise OperationRefused(
                "retirement inventory lacks original allocation authority"
            )
        return (
            operation,
            {"inventory": inventory, "domain_org_id": lease.org_id},
            artifact,
        )

    async def discover(self, operation, target, artifact, calls):
        lease = operation.grant.lease
        allocation = operation.request.parameters["allocation_id"]
        known = await self.known(lease, allocation)
        # Attribute newly discovered dependencies to the ORIGINAL allocation's
        # creating intents, including a lost original provider reply.
        async with self.provider.execution_pool.acquire() as connection:
            rows = await connection.fetch(
                "SELECT idempotency_key,provider,operation_kind FROM harness_provider_call_intent "
                "WHERE org_id=$1 AND workspace_id=$2 AND allocation_id=$3",
                lease.org_id,
                lease.workspace_id,
                allocation,
            )
        creation_keys = frozenset(
            row["idempotency_key"]
            for row in rows
            if may_create(row["operation_kind"], provider=row["provider"])
        )
        return await asyncio.to_thread(
            self.observations.catalog,
            target["inventory"],
            artifact,
            operation.request.parameters,
            known,
            creation_keys,
        )

    async def observe(self, operation, target, artifact, resource):
        return await asyncio.to_thread(
            self.observations.observe, target["inventory"], resource
        )

    def token_for(self, operation):
        # This must return the actual authority verified by authenticate; a worker
        # cannot select another allocation through a locally invented token.
        return self.authority_token(operation)

    async def verify_step(self, operation, inventory, authorize):
        current, target, artifact = await self.context(
            operation.grant.lease.operation_id
        )
        if current.request != operation.request or target["inventory"] != inventory:
            raise OperationRefused("retirement inventory changed before verification")
        await authorize()
        resources = await self.discover(current, target, artifact, [])
        if not resources:
            raise OperationRefused("retirement has no established provider inventory")
        for resource in resources.values():
            await authorize()
            observation = await self.observe(current, target, artifact, resource)
            if observation.presence is not ResourcePresence.ABSENT:
                return (
                    CallOutcome.UNKNOWN,
                    "owned resources or unverified obligations remain",
                    None,
                )
        await authorize()
        # The shared after-step hook still seals/enumerates/queries again and
        # publishes the exact accounting assessment before any ledger release.
        return CallOutcome.SUCCEEDED, "fresh retirement provider absence observed", None

    async def _persist_observation(self, operation, target, payload, assessment):
        async with self.provider.domain_pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    "INSERT INTO controller_execution_accounting(operation_id,org_id,workspace_id,observation) "
                    "VALUES ($1,$2::text::uuid,$3::text::uuid,$4::json) "
                    "ON CONFLICT(operation_id) DO UPDATE SET observation=EXCLUDED.observation",
                    operation.grant.lease.operation_id,
                    target["domain_org_id"],
                    operation.grant.lease.workspace_id,
                    json.dumps(payload),
                )
                if assessment and assessment.may_mark_released:
                    # Keep the ownership rows and recorded immutable handles. Only
                    # withdraw the active projection after fresh complete absence.
                    await connection.execute(
                        "UPDATE workspaces SET status='Deleted' WHERE id::text=$1 AND org_id::text=$2 "
                        "AND status IN ('Teardown','retired') AND is_default=false",
                        operation.grant.lease.workspace_id,
                        target["domain_org_id"],
                    )
