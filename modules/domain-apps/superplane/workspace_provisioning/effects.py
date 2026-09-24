"""Append-only evidence for finite SDK effects inside one admitted lifecycle phase.

This journal cannot execute code or settle an operation. A phase-specific composer
supplies the exact approved recipe and dispatches its explicit SDK methods. The
shared executor still owns the one outer provider intent, lease and budget.
"""

from contextlib import asynccontextmanager
from copy import deepcopy
import json

from .artifacts import canonical, digest
from .runtime_config import LifecycleRefused


class LifecycleEffects:
    def __init__(self, operation, context, *, phase, recipe):
        self.operation, self.context, self.phase = operation, context, phase
        if not isinstance(recipe, dict) or not recipe or len(recipe) > 100:
            raise LifecycleRefused("finite lifecycle effect recipe is required")
        self.recipe = deepcopy(recipe)
        for key, descriptor in self.recipe.items():
            if (
                not isinstance(key, str)
                or not key
                or len(key) > 200
                or not isinstance(descriptor, dict)
                or set(descriptor) != {"service", "method", "account_id", "arguments"}
                or not isinstance(descriptor["arguments"], dict)
            ):
                raise LifecycleRefused("finite lifecycle effect descriptor is invalid")
        self.recipe_digest = digest(self.recipe)

    async def authority(self):
        from .authority import current_operation, validated_request

        current = await current_operation(self.operation, self.context)
        validated_request(current, self.context)
        return current

    @asynccontextmanager
    async def fenced(self):
        from harness_jobs.execution import CallStage, read_call
        from harness_jobs.execution_plan import admitted_steps, step_key
        from harness_jobs.leases import lock_lease
        from harness_jobs.store import OperationStore

        await self.authority()
        async with self.context.connect() as shared, shared.transaction():
            if not await lock_lease(shared, self.operation.grant.lease):
                raise LifecycleRefused("lifecycle effect lease is no longer live")
            record = await OperationStore().get(
                shared,
                self.operation.grant.principal,
                self.operation.grant.lease.operation_id,
            )
            steps = admitted_steps(record) if record else ()
            if len(steps) != 1 or (
                steps[0].step_id,
                steps[0].provider,
                steps[0].operation_kind,
            ) != (self.phase, "superplane-lifecycle", self.phase):
                raise LifecycleRefused(
                    "finite SDK recipe is outside the admitted phase"
                )
            outer = await read_call(shared, idempotency_key=step_key(record, steps[0]))
            if outer is None or outer.stage is not CallStage.INTENDED:
                raise LifecycleRefused(
                    "SDK effects require the unresolved outer phase intent"
                )
            # Intent is committed in the domain database before this context exits,
            # then authority is rechecked before dispatch. Never hold the shared
            # transaction open during a cloud call or nest OperationExecutor here.
            async with self.context.domain_connect() as domain:
                yield domain

    def descriptor(self, key, descriptor):
        if key not in self.recipe or canonical(descriptor) != canonical(
            self.recipe[key]
        ):
            raise LifecycleRefused("SDK effect differs from the approved finite recipe")
        return digest(descriptor)

    async def rows(self, connection):
        lease = self.operation.grant.lease
        return [
            dict(row)
            for row in await connection.fetch(
                "SELECT * FROM workspace_lifecycle_effects WHERE org_id=$1 AND workspace_id=$2 AND operation_id=$3 AND phase=$4 ORDER BY effect_key,event",
                lease.org_id,
                lease.workspace_id,
                lease.operation_id,
                self.phase,
            )
        ]

    def verify_rows(self, rows):
        lease = self.operation.grant.lease
        grouped = {}
        for row in rows:
            key = row["effect_key"]
            if (
                key not in self.recipe
                or row["recipe_digest"] != self.recipe_digest
                or row["descriptor_digest"] != digest(self.recipe[key])
                or row["descriptor_json"] != canonical(self.recipe[key])
                or row["source_payload_digest"] != self.operation.plan_digest
                or row["source_job_id"] != self.operation.job_id
                or row["org_id"] != lease.org_id
                or row["workspace_id"] != lease.workspace_id
                or row["operation_id"] != lease.operation_id
            ):
                raise LifecycleRefused(
                    "recorded lifecycle effect recipe or admission changed"
                )
            immutable = {
                k: v for k, v in row.items() if k not in {"created_at", "event_digest"}
            }
            if digest(immutable) != row["event_digest"]:
                raise LifecycleRefused("lifecycle effect evidence changed")
            grouped.setdefault(key, []).append(row)
        for events in grouped.values():
            if [row["event"] for row in events] not in [
                ["intended"],
                ["confirmed", "intended"],
            ]:
                raise LifecycleRefused(
                    "lifecycle effect evidence sequence is incomplete"
                )
        return grouped

    async def append(self, connection, key, event, result):
        lease = self.operation.grant.lease
        values = {
            "org_id": lease.org_id,
            "workspace_id": lease.workspace_id,
            "operation_id": lease.operation_id,
            "source_job_id": self.operation.job_id,
            "source_payload_digest": self.operation.plan_digest,
            "phase": self.phase,
            "effect_key": key,
            "event": event,
            "recipe_digest": self.recipe_digest,
            "descriptor_digest": digest(self.recipe[key]),
            "descriptor_json": canonical(self.recipe[key]),
            "holder": lease.holder,
            "attempt_id": lease.attempt_id,
            "fence_token": lease.fence_token,
            "result_json": canonical(result),
        }
        columns = tuple(values) + ("event_digest",)
        await connection.execute(
            "INSERT INTO workspace_lifecycle_effects ("
            + ",".join(columns)
            + ") VALUES ("
            + ",".join("$" + str(i) for i in range(1, len(columns) + 1))
            + ")",
            *values.values(),
            digest(values),
        )

    async def intend(self, key, descriptor):
        """Return confirmed evidence or commit one intent; uncertain rows never replay."""
        self.descriptor(key, descriptor)
        async with self.fenced() as connection:
            grouped = self.verify_rows(await self.rows(connection))
            events = grouped.get(key, [])
            if events:
                if events[0]["event"] == "confirmed":
                    return json.loads(events[0]["result_json"])
                raise LifecycleRefused(
                    "lifecycle effect is ambiguous; recovery must observe it"
                )
            await self.append(connection, key, "intended", None)
        # A lost commit acknowledgement raises above and cannot reach a mutation.
        # A revocation while the durable write awaited also stops dispatch here.
        await self.authority()
        return None

    async def confirm(self, key, descriptor, result):
        """Only phase-specific verified SDK readback is accepted as a result."""
        self.descriptor(key, descriptor)
        if not isinstance(result, dict) or not result:
            raise LifecycleRefused("positive lifecycle provider readback is required")
        async with self.fenced() as connection:
            grouped = self.verify_rows(await self.rows(connection))
            events = grouped.get(key, [])
            if len(events) != 1 or events[0]["event"] != "intended":
                raise LifecycleRefused(
                    "lifecycle effect has no unique unresolved intent"
                )
            await self.append(connection, key, "confirmed", result)
        await self.authority()

    async def complete(self):
        """Read all evidence afresh; no unresolved or unapproved effect can disappear."""
        async with self.fenced() as connection:
            grouped = self.verify_rows(await self.rows(connection))
        if set(grouped) != set(self.recipe):
            raise LifecycleRefused("lifecycle phase is missing required recipe effects")
        if any(events[0]["event"] != "confirmed" for events in grouped.values()):
            raise LifecycleRefused("lifecycle phase has unresolved provider effects")
        return {
            key: json.loads(events[0]["result_json"]) for key, events in grouped.items()
        }
