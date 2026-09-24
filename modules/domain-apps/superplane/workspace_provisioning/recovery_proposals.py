"""Recover immutable proposal output, without claiming current provider state."""

import asyncio
import json

from harness_jobs.execution import CallOutcome, read_call
from harness_jobs.execution_plan import step_key
from harness_jobs.identity import OperationRefused
from harness_jobs.recovery_grant import RecoveryGrant, lock_recovery_grant
from harness_jobs.store import OperationStore
from superplane_executor.recovery_authority import (
    same_recovery_claim,
    same_recovery_operation,
)

from .artifacts import proposal, read_artifact
from .runtime import validate_phase
from .terraform import verify_prepared_artifact

PROPOSAL_PHASES = frozenset({"prepare-infrastructure", "prepare-adoption"})


async def original_result(operation, context, key):
    """Bind an immutable result to its actual original call and producer audit."""
    if not isinstance(operation.grant, RecoveryGrant):
        raise OperationRefused("actual recovery grant is required")
    lease = operation.grant.lease
    _, request, _, _, step = await validate_phase(
        operation, context, require_fresh=False
    )
    async with context.connect() as connection, connection.transaction():
        if not await lock_recovery_grant(connection, operation.grant):
            raise OperationRefused("proposal recovery principal or claim expired")
        record = await OperationStore().get(
            connection, operation.grant.principal, lease.operation_id
        )
        call = await read_call(connection, idempotency_key=key)
        if (
            record is None
            or (record.job_id, record.plan_digest, record.request_payload)
            != (operation.job_id, operation.plan_digest, operation.request_payload)
            or key != step_key(record, step)
            or call is None
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
            raise OperationRefused("proposal does not name the original admitted call")
    async with context.domain_connect() as connection:
        rows = await connection.fetch(
            "SELECT artifact_id FROM workspace_lifecycle_artifacts "
            "WHERE org_id=$1 AND workspace_id=$2 AND source_operation_id=$3 LIMIT 2",
            lease.org_id,
            lease.workspace_id,
            lease.operation_id,
        )
    if len(rows) != 1:
        raise OperationRefused("completed proposal has no unique immutable result")
    row = await read_artifact(
        context.domain_connect,
        artifact_id=rows[0]["artifact_id"],
        org_id=lease.org_id,
        workspace_id=lease.workspace_id,
        require_fresh=False,
    )
    if (
        row["source_operation_id"],
        row["source_job_id"],
        row["source_attempt_id"],
        row["source_payload_digest"],
        row["source_request_payload"],
        row["producer_attempt_id"],
        row["producer_fence_token"],
    ) != (
        lease.operation_id,
        record.job_id,
        record.attempt_id,
        record.plan_digest,
        record.request_payload,
        call.attempt_id,
        call.fence_token,
    ):
        raise OperationRefused("proposal producer differs from original call authority")
    async with context.connect() as connection:
        producer = await connection.fetchval(
            "SELECT EXISTS(SELECT 1 FROM harness_execution_audit WHERE operation_id=$1 "
            "AND org_id=$2 AND workspace_id=$3 AND attempt_id=$4 AND fence_token=$5 "
            "AND event='record_intent' AND allowed AND actor=$6)",
            lease.operation_id,
            lease.org_id,
            lease.workspace_id,
            call.attempt_id,
            call.fence_token,
            row["producer_holder"],
        )
    if not producer:
        raise OperationRefused("proposal producer has no original execution audit")
    return row, step, request


class ProposalRecovery:
    def __init__(self, context):
        self.context = context

    async def verified(self, lease, key):
        operation = await self.context.authority.resolve_recovery(lease)
        if not isinstance(operation.grant, RecoveryGrant) or not same_recovery_claim(
            operation.grant.lease, lease
        ):
            raise OperationRefused("proposal recovery claim changed")
        row, step, request = await original_result(operation, self.context, key)
        if step.step_id not in PROPOSAL_PHASES:
            raise OperationRefused("this lifecycle phase requires provider recovery")
        if step.step_id == "prepare-adoption":
            from .adoption import verify_adoption_artifact

            verify_adoption_artifact(row, request)
        else:
            metadata, target = (
                json.loads(row["artifact_metadata_json"]),
                json.loads(row["target_json"]),
            )
            account = request.target_account_id
            if (
                row["account_id"] != account
                or any(
                    target.get(name) != value
                    for name, value in {
                        "account_id": account,
                        "aws_region": request.region,
                        "org_id": lease.org_id,
                        "workspace_id": lease.workspace_id,
                        "workspace_name": operation.request.parameters[
                            "workspace_name"
                        ],
                    }.items()
                )
                or metadata.get("next_phase") != "apply-infrastructure"
            ):
                raise OperationRefused("prepared proposal target or next phase changed")
            await asyncio.to_thread(verify_prepared_artifact, row, self.context)
        current = await self.context.authority.resolve_recovery(lease)
        if not same_recovery_operation(current, operation):
            raise OperationRefused(
                "proposal recovery authority changed during verification"
            )
        async with self.context.connect() as connection, connection.transaction():
            if not await lock_recovery_grant(connection, current.grant):
                raise OperationRefused("proposal recovery expired during verification")
        return operation, row

    async def observe(self, lease, key, provider, kind, target):
        operation, row = await self.verified(lease, key)
        _, _, _, _, step = await validate_phase(
            operation, self.context, require_fresh=False
        )
        if (provider, kind, target) != (
            step.provider,
            step.operation_kind,
            step.target,
        ):
            raise OperationRefused("proposal observer descriptor changed")
        return (
            CallOutcome.SUCCEEDED,
            "original reviewed proposal recovered",
            row["artifact_id"],
        )

    async def accounting(self, lease, calls):
        if len(calls) != 1 or calls[0].outcome is not CallOutcome.SUCCEEDED:
            raise OperationRefused("original proposal call is not confirmed")
        operation, row = await self.verified(lease, calls[0].idempotency_key)
        if calls[0].provider_ref != row["artifact_id"]:
            raise OperationRefused("confirmed proposal reference changed")
        return {
            "allocation_id": operation.request.parameters["allocation_id"],
            "inventory_complete": False,
            "release_permitted": False,
            "may_mark_released": False,
            "resource_dispositions": {},
            "unresolved_resources": [],
            "exposure": "unresolved",
            "reason": "immutable proposal recovered; provider allocation not assessed",
            "lifecycle_proposal": proposal(row),
        }
