"""Classify interrupted mutations from maintained journals without replay or cleanup."""

import json

from harness_jobs.execution import CallOutcome, read_call
from harness_jobs.execution_plan import step_key
from harness_jobs.identity import OperationRefused
from harness_jobs.recovery_grant import RecoveryGrant, lock_recovery_grant
from harness_jobs.store import OperationStore
from superplane_executor.recovery_authority import same_recovery_claim

from .artifacts import digest
from .effects import LifecycleEffects
from .runtime import validate_phase

PARTIAL_PHASES = frozenset(
    {"apply-infrastructure", "bootstrap-workspace", "bootstrap-account"}
)


class PartialLifecycleRecovery:
    def __init__(self, context):
        self.context = context

    async def verified(self, lease, key):
        operation = await self.context.authority.resolve_recovery(lease)
        if not isinstance(operation.grant, RecoveryGrant) or not same_recovery_claim(
            operation.grant.lease, lease
        ):
            raise OperationRefused("partial lifecycle recovery claim changed")
        config, request, authorization, source, step = await validate_phase(
            operation, self.context, require_fresh=False
        )
        if step.step_id not in PARTIAL_PHASES or source is None:
            raise OperationRefused(
                "partial recovery is outside an admitted mutation phase"
            )
        async with self.context.connect() as connection, connection.transaction():
            if not await lock_recovery_grant(connection, operation.grant):
                raise OperationRefused("partial lifecycle recovery authority expired")
            record = await OperationStore().get(
                connection, operation.grant.principal, lease.operation_id
            )
            call = await read_call(connection, idempotency_key=key)
            keys = await connection.fetch(
                "SELECT idempotency_key FROM harness_provider_call_intent WHERE operation_id=$1",
                lease.operation_id,
            )
            if (
                record is None
                or call is None
                or [item["idempotency_key"] for item in keys] != [key]
                or (record.job_id, record.plan_digest, record.request_payload)
                != (operation.job_id, operation.plan_digest, operation.request_payload)
                or key != step_key(record, step)
                or call.fence_token >= lease.fence_token
                or (
                    call.operation_id,
                    call.org_id,
                    call.workspace_id,
                    call.job_id,
                    call.provider,
                    call.operation_kind,
                    call.target,
                )
                != (
                    lease.operation_id,
                    lease.org_id,
                    lease.workspace_id,
                    operation.job_id,
                    step.provider,
                    step.operation_kind,
                    step.target,
                )
            ):
                raise OperationRefused("partial recovery original intent changed")
            if not await connection.fetchval(
                "SELECT EXISTS(SELECT 1 FROM harness_execution_audit WHERE operation_id=$1 "
                "AND org_id=$2 AND workspace_id=$3 AND attempt_id=$4 AND fence_token=$5 "
                "AND event='record_intent' AND allowed)",
                lease.operation_id,
                lease.org_id,
                lease.workspace_id,
                call.attempt_id,
                call.fence_token,
            ):
                raise OperationRefused("partial recovery has no original intent audit")
        inventory = await self.inventory(
            operation, config, request, authorization, source, step.step_id
        )
        async with self.context.connect() as connection, connection.transaction():
            if not await lock_recovery_grant(connection, operation.grant):
                raise OperationRefused("partial recovery expired during journal read")
            if await read_call(connection, idempotency_key=key) != call:
                raise OperationRefused(
                    "partial recovery original call changed during read"
                )
        return operation, call, inventory

    async def inventory(self, operation, config, request, authorization, source, phase):
        metadata = json.loads(source["artifact_metadata_json"])
        recipe = None
        if phase == "bootstrap-account":
            from .account_runtime import bootstrap_recipe

            _, _, recipe = bootstrap_recipe(
                config,
                request,
                authorization,
                source["account_id"],
                metadata["creation_source_parent_id"],
            )
        elif phase == "bootstrap-workspace":
            from .network import network_recipe

            outputs = {
                key: value["value"] for key, value in metadata["outputs"].items()
            }
            recipe = network_recipe(operation, config, outputs)
        grouped, authority = {}, []
        lease = operation.grant.lease
        async with self.context.domain_connect() as connection:
            if recipe is not None:
                journal = LifecycleEffects(
                    operation, self.context, phase=phase, recipe=recipe
                )
                grouped = journal.verify_rows(await journal.rows(connection))
            if phase == "bootstrap-workspace":
                rows = await connection.fetch(
                    "SELECT generation,operation_id,org_id,cluster_arn,claim,plan_json,progress_json,revoked "
                    "FROM workspace_bootstrap_authority WHERE workspace_id=$1 AND operation_id=$2 LIMIT 129",
                    lease.workspace_id,
                    lease.operation_id,
                )
                if len(rows) > 128:
                    raise OperationRefused(
                        "partial bootstrap authority inventory exceeds bound"
                    )
                for row in rows:
                    if (
                        row["org_id"] != lease.org_id
                        or row["cluster_arn"] != outputs["cluster_arn"]
                    ):
                        raise OperationRefused(
                            "partial bootstrap journal target changed"
                        )
                    authority.append(
                        {
                            "generation": row["generation"],
                            "claim": row["claim"],
                            "plan_sha256": digest(row["plan_json"]),
                            "progress_sha256": digest(row["progress_json"]),
                            "revoked": row["revoked"],
                        }
                    )
        return {
            "phase": phase,
            "source_artifact_id": source["artifact_id"],
            "confirmed_effect_keys": sorted(
                key
                for key, rows in grouped.items()
                if any(row["event"] == "confirmed" for row in rows)
            ),
            "uncertain_effect_keys": sorted(
                key
                for key, rows in grouped.items()
                if all(row["event"] != "confirmed" for row in rows)
            ),
            "authority_generations": sorted(
                authority, key=lambda row: row["generation"]
            ),
            "workflow_complete": False,
            "provider_absence_verified": False,
            "cleanup_authorized": False,
        }

    async def observe(self, lease, key, provider, kind, target):
        _, call, _ = await self.verified(lease, key)
        if (provider, kind, target) != (
            call.provider,
            call.operation_kind,
            call.target,
        ):
            raise OperationRefused("partial recovery descriptor changed")
        return (
            CallOutcome.UNKNOWN,
            "partial mutation requires separately governed cleanup",
            call.provider_ref,
        )

    async def accounting(self, lease, calls):
        if len(calls) != 1 or calls[0].outcome is CallOutcome.SUCCEEDED:
            raise OperationRefused("partial recovery requires its original call")
        operation, call, inventory = await self.verified(
            lease, calls[0].idempotency_key
        )
        if call != calls[0]:
            raise OperationRefused("partial recovery call changed before accounting")
        return {
            "allocation_id": operation.request.parameters["allocation_id"],
            "inventory_complete": False,
            "release_permitted": False,
            "may_mark_released": False,
            "resource_dispositions": {},
            "unresolved_resources": [],
            "exposure": "unresolved",
            "reason": "partial lifecycle mutation retained; separately approved cleanup required",
            "partial_lifecycle": inventory,
        }
