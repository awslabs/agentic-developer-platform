"""Domain resource evidence; shared Harness remains the execution authority.

A session advisory lock serializes one native network resource across allocations.
No transaction stays open during provider I/O. An uncertain create cannot be
reissued: only an exact provider readback can resolve its committed intent.
"""

import hashlib
import json
from contextlib import asynccontextmanager

from harness_jobs.identity import OperationRefused

from .network_plan import canonical


class NetworkPending(OperationRefused):
    """An original journalled effect needs another provider observation."""


class NetworkJournal:
    def __init__(self, pool, operation, plan, authorize):
        self.pool, self.operation, self.plan, self.authorize = (
            pool,
            operation,
            plan,
            authorize,
        )
        self.lease = operation.grant.lease
        self.allocation = operation.request.parameters["allocation_id"]
        self.cluster = plan.network["cluster"]

    @asynccontextmanager
    async def locked(self, key):
        lock = int.from_bytes(
            hashlib.sha256(key.encode()).digest()[:8], "big", signed=True
        )
        async with self.pool.acquire() as connection:
            if not await connection.fetchval(
                "SELECT pg_try_advisory_lock($1::bigint)", lock
            ):
                raise OperationRefused(
                    "network resource is busy; retry under original authority"
                )
            try:
                await self.authorize()
                yield connection
            finally:
                await connection.execute("SELECT pg_advisory_unlock($1::bigint)", lock)

    async def member(self, c, key):
        row = await c.fetchrow(
            "SELECT * FROM controller_network_members WHERE resource_key=$1 AND allocation_id=$2",
            key,
            self.allocation,
        )
        if row is not None:
            if (
                (
                    row["org_id"],
                    row["workspace_id"],
                    row["cluster_id"],
                    row["membership_generation"],
                )
                != (
                    self.lease.org_id,
                    self.lease.workspace_id,
                    self.cluster["cluster_id"],
                    self.cluster["membership_generation"],
                )
                or row["released_at"] is not None
                or row["source_operation_id"] != self.lease.operation_id
                or row["source_plan_digest"] != self.operation.plan_digest
            ):
                raise OperationRefused(
                    "network allocation membership changed or was released"
                )
            return
        await c.execute(
            """INSERT INTO controller_network_members
          (resource_key,allocation_id,org_id,workspace_id,cluster_id,membership_generation,source_operation_id,source_plan_digest)
          VALUES($1,$2,$3,$4,$5,$6,$7,$8)""",
            key,
            self.allocation,
            self.lease.org_id,
            self.lease.workspace_id,
            self.cluster["cluster_id"],
            self.cluster["membership_generation"],
            self.lease.operation_id,
            self.operation.plan_digest,
        )

    async def effect(self, c, row, action, descriptor, mutate, observe):
        """Record one finite mutation before dispatch; repeated uncertain calls only observe."""
        key, generation, encoded = (
            row["resource_key"],
            row["generation"],
            canonical(descriptor),
        )
        prior = await c.fetchrow(
            "SELECT * FROM controller_network_effects WHERE resource_key=$1 AND generation=$2 AND action=$3",
            key,
            generation,
            action,
        )
        if prior and prior["descriptor"] != encoded:
            raise OperationRefused("network effect descriptor changed")
        if prior is None:
            await self.authorize()
            await c.execute(
                """INSERT INTO controller_network_effects
              (resource_key,generation,action,operation_id,attempt_id,fence_token,descriptor)
              VALUES($1,$2,$3,$4,$5,$6,$7)""",
                key,
                generation,
                action,
                self.lease.operation_id,
                self.lease.attempt_id,
                self.lease.fence_token,
                encoded,
            )
            await self.authorize()
            await mutate()
        result = await observe()
        await self.authorize()
        if result is None:
            raise NetworkPending(
                "network effect remains uncertain; preserve its original intent"
            )
        await c.execute(
            "UPDATE controller_network_effects SET result=$4,confirmed_at=clock_timestamp() WHERE resource_key=$1 AND generation=$2 AND action=$3",
            key,
            generation,
            action,
            canonical(result),
        )
        return result

    async def ensure(self, key, descriptor, *, adopted, observe, create):
        encoded = canonical(descriptor)
        async with self.locked(key) as c:
            row = await c.fetchrow(
                "SELECT * FROM controller_network_resources WHERE resource_key=$1", key
            )
            if row and (
                row["org_id"] != self.lease.org_id
                or (row["state"] != "absent" and row["descriptor"] != encoded)
            ):
                raise OperationRefused(
                    "network resource belongs to another scope or plan"
                )
            if row and row["state"] == "delete_intended":
                raise OperationRefused(
                    "network resource is retiring; reconcile before admitting a new dependency"
                )
            if row and row["state"] == "absent":
                if await c.fetchval(
                    "SELECT count(*) FROM controller_network_members WHERE resource_key=$1 AND released_at IS NULL",
                    key,
                ):
                    raise OperationRefused(
                        "retired network resource still has unresolved members"
                    )
                await c.execute(
                    "UPDATE controller_network_resources SET generation=generation+1,state='intended',provider_reference=NULL,created_by_operation=$2,descriptor=$3 WHERE resource_key=$1",
                    key,
                    self.lease.operation_id,
                    encoded,
                )
                row = await c.fetchrow(
                    "SELECT * FROM controller_network_resources WHERE resource_key=$1",
                    key,
                )
            if row is None:
                # Provider readback must distinguish an explicitly adopted resource
                # from a new owned one; it must never silently adopt a name match.
                existing = await observe(None)
                if existing is not None and adopted is False:
                    raise OperationRefused(
                        "unrecorded network resource cannot become allocation-owned"
                    )
                if adopted is True and existing is None:
                    raise OperationRefused(
                        "approved adopted network resource is absent"
                    )
                await c.execute(
                    """INSERT INTO controller_network_resources
                  (resource_key,org_id,descriptor,state,owned,provider_reference,created_by_operation)
                  VALUES($1,$2,$3,$4,$5,$6,$7)""",
                    key,
                    self.lease.org_id,
                    encoded,
                    "present" if existing else "intended",
                    existing is None,
                    canonical(existing) if existing else None,
                    self.lease.operation_id,
                )
                row = await c.fetchrow(
                    "SELECT * FROM controller_network_resources WHERE resource_key=$1",
                    key,
                )
            await self.member(c, key)
            if row["state"] == "intended":
                token = hashlib.sha256(
                    f"{key}/{row['generation']}".encode()
                ).hexdigest()
                result = await self.effect(
                    c,
                    row,
                    "create",
                    descriptor,
                    lambda: create(token),
                    lambda: observe(token),
                )
                await c.execute(
                    "UPDATE controller_network_resources SET state='present',provider_reference=$2 WHERE resource_key=$1",
                    key,
                    canonical(result),
                )
            else:
                result = await observe(json.loads(row["provider_reference"]))
                if result is None or canonical(result) != row["provider_reference"]:
                    raise OperationRefused(
                        "network resource identity changed after confirmation"
                    )
            return result

    async def release(self, key, *, observe, delete, expected=None):
        async with self.locked(key) as c:
            row = await c.fetchrow(
                "SELECT * FROM controller_network_resources WHERE resource_key=$1", key
            )
            member = await c.fetchrow(
                "SELECT * FROM controller_network_members WHERE resource_key=$1 AND allocation_id=$2",
                key,
                self.allocation,
            )
            if row is None or member is None:
                if expected is not None:
                    raise OperationRefused(
                        "original staged network membership unavailable"
                    )
                return
            if row["org_id"] != self.lease.org_id or (
                member["org_id"],
                member["workspace_id"],
                member["cluster_id"],
                member["membership_generation"],
            ) != (
                self.lease.org_id,
                self.lease.workspace_id,
                self.cluster["cluster_id"],
                self.cluster["membership_generation"],
            ):
                raise OperationRefused("network cleanup membership mismatch")
            source = self.operation.request.parameters.get(
                "controller_source_operation_id", self.lease.operation_id
            )
            if member["source_operation_id"] != source:
                raise OperationRefused(
                    "network cleanup is not bound to the original operation"
                )
            if expected is not None and member["released_at"] is None:
                if (
                    row["generation"] != expected["generation"]
                    or member["membership_generation"]
                    != expected["membership_generation"]
                    or row["descriptor"] != canonical(expected["descriptor"])
                    or row["provider_reference"] != canonical(expected["reference"])
                    or row["owned"] != expected["owned"]
                ):
                    raise OperationRefused("original staged network identity changed")
            if row["state"] == "absent" or member["released_at"] is not None:
                return
            others = await c.fetchval(
                "SELECT count(*) FROM controller_network_members WHERE resource_key=$1 AND allocation_id<>$2 AND released_at IS NULL",
                key,
                self.allocation,
            )
            if others or not row["owned"]:
                await c.execute(
                    "UPDATE controller_network_members SET released_at=clock_timestamp() WHERE resource_key=$1 AND allocation_id=$2",
                    key,
                    self.allocation,
                )
                return
            if row["state"] == "intended":
                raise OperationRefused(
                    "uncertain network creation must be observed before cleanup"
                )
            reference = json.loads(row["provider_reference"])
            actual = await observe(reference)
            if actual is not None and canonical(actual) != row["provider_reference"]:
                raise OperationRefused("network cleanup native identity changed")
            await c.execute(
                "UPDATE controller_network_resources SET state='delete_intended' WHERE resource_key=$1",
                key,
            )
            if actual is not None:

                async def absent():
                    return (
                        {"absent": True} if await observe(reference) is None else None
                    )

                await self.effect(
                    c,
                    row,
                    "delete",
                    {"reference": reference},
                    lambda: delete(reference),
                    absent,
                )
            await self.authorize()
            # Native absence may predate this operation, in which case no delete
            # effect is invented. Publish the two dependent bookkeeping facts
            # together: no crash may expose absent + unreleased without a journal.
            # Provider reads/mutations and current authorization already completed.
            async with c.transaction():
                await c.execute(
                    "UPDATE controller_network_resources SET state='absent' WHERE resource_key=$1",
                    key,
                )
                await c.execute(
                    "UPDATE controller_network_members SET released_at=clock_timestamp() WHERE resource_key=$1 AND allocation_id=$2",
                    key,
                    self.allocation,
                )

    async def change(self, key, action, descriptor, mutate, observe):
        async with self.locked(key) as c:
            row = await c.fetchrow(
                "SELECT * FROM controller_network_resources WHERE resource_key=$1", key
            )
            if (
                row is None
                or row["org_id"] != self.lease.org_id
                or row["state"] != "present"
                or not row["owned"]
            ):
                raise OperationRefused(
                    "only a confirmed owned network resource can be changed"
                )
            await self.member(c, key)
            return await self.effect(c, row, action, descriptor, mutate, observe)
