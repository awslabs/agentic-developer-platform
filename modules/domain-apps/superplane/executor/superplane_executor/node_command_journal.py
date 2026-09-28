"""Write-once command intent and late-handle evidence; never infer permission."""

import hashlib
from contextlib import asynccontextmanager

from harness_jobs.identity import OperationRefused
from harness_jobs.inventory import AllocationResource, InventoryAuthority
from harness_jobs.leases import lock_lease

from .node_command_plan import canonical, digest, reference


class Journal:
    def __init__(self, provider, operation, call, authorize):
        self.provider, self.operation, self.call, self.authorize = (
            provider,
            operation,
            call,
            authorize,
        )

    @asynccontextmanager
    async def locked(self, contract):
        lease = self.operation.grant.lease
        ref = reference(
            lease.operation_id,
            lease.org_id,
            lease.workspace_id,
            contract["allocation_id"],
            contract["instance_id"],
            contract["purpose"],
        )
        key = int.from_bytes(
            hashlib.sha256(ref.encode()).digest()[:8], "big", signed=True
        )
        async with self.provider.domain_pool.acquire() as c:
            if not await c.fetchval("SELECT pg_try_advisory_lock($1::bigint)", key):
                raise OperationRefused("original node command already in progress")
            try:
                await self.authorize()
                row = await c.fetchrow(
                    "SELECT * FROM controller_node_commands WHERE reference=$1", ref
                )
                if row is None:
                    row = await c.fetchrow(
                        """INSERT INTO controller_node_commands
 (reference,operation_id,org_id,workspace_id,allocation_id,plan_digest,step_key,
 purpose,instance_id,region,account_id,contract,contract_sha256)
 VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13) RETURNING *""",
                        ref,
                        lease.operation_id,
                        lease.org_id,
                        lease.workspace_id,
                        contract["allocation_id"],
                        self.operation.plan_digest,
                        self.call.idempotency_key,
                        contract["purpose"],
                        contract["instance_id"],
                        contract["region"],
                        contract["account_id"],
                        canonical(contract),
                        digest(contract),
                    )
                if (
                    row["contract"] != canonical(contract)
                    or row["plan_digest"] != self.operation.plan_digest
                    or row["step_key"] != self.call.idempotency_key
                ):
                    raise OperationRefused("original node command binding changed")
                # Domain intent precedes shared membership; shared locks/epoch gate
                # precedes dispatch. A crash in between cannot send a command.
                if row["state"] == "prepared":
                    inventory = InventoryAuthority(
                        connect=self.provider.execution_pool.acquire,
                        authenticate=self.provider.registry.authenticate,
                    )
                    async with self.provider.execution_pool.acquire() as shared:
                        await inventory.enumerate_resources(
                            shared,
                            lease,
                            resources=(
                                AllocationResource(
                                    ref,
                                    "aws",
                                    ref,
                                    "node_command",
                                    frozenset({self.call.idempotency_key}),
                                ),
                            ),
                        )
                await self.authorize()
                yield c, row
            finally:
                await c.execute("SELECT pg_advisory_unlock($1::bigint)", key)

    async def dispatching(self, c, row, seconds):
        await self.authorize()
        deadline = self.operation.grant.lease.runtime_deadline
        return await c.fetchrow(
            """UPDATE controller_node_commands
 SET state='dispatching',dispatched_at=clock_timestamp(),
 observation_deadline=LEAST($2,clock_timestamp()+$3::int*interval '1 second')
 WHERE reference=$1 AND state='prepared' AND command_id IS NULL RETURNING *""",
            row["reference"],
            deadline,
            seconds,
        )

    @staticmethod
    async def remember_handle(c, row, command_id):
        # Historical evidence only: deliberately possible after authority expiry.
        # Cannot create an intent, change binding/fence, or certify success.
        saved = await c.fetchrow(
            """UPDATE controller_node_commands
 SET command_id=$3,state=CASE WHEN state='dispatching' THEN 'accepted' ELSE state END
 WHERE reference=$1 AND contract_sha256=$2 AND dispatched_at IS NOT NULL
 AND (command_id IS NULL OR command_id=$3) RETURNING *""",
            row["reference"],
            row["contract_sha256"],
            command_id,
        )
        if saved is None:
            raise OperationRefused(
                "original command handle conflicts with retained evidence"
            )
        return saved

    async def result(self, c, row, result):
        async with self.provider.execution_pool.acquire() as shared:
            async with shared.transaction():
                if not await lock_lease(shared, self.operation.grant.lease):
                    raise OperationRefused("node result lease is no longer live")
                async with c.transaction():
                    await self.authorize()
                    saved = await c.fetchval(
                        """UPDATE controller_node_commands SET result=$3,state='succeeded'
 WHERE reference=$1 AND contract_sha256=$2 AND command_id IS NOT NULL
 AND state IN ('accepted','running','succeeded')
 AND (result IS NULL OR result=$3) RETURNING reference""",
                        row["reference"],
                        row["contract_sha256"],
                        canonical(result),
                    )
                    if saved is None:
                        raise OperationRefused("node command receipt conflicts")
                    await self.authorize()
