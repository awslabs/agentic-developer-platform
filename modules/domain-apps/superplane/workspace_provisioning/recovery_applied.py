"""Recover a completed apply reply using saved bytes and fresh API-owned reads."""

import asyncio
import json

from harness_jobs.execution import CallOutcome
from harness_jobs.identity import OperationRefused
from harness_jobs.recovery_grant import RecoveryGrant, lock_recovery_grant
from superplane_executor.recovery_authority import (
    same_recovery_claim,
    same_recovery_operation,
)

from .recovery_proposals import ProposalRecovery, original_result
from .runtime import validate_phase
from .terraform import verify_prepared_artifact

APPLIED_PHASES = frozenset({"apply-infrastructure"})


class AppliedRecovery(ProposalRecovery):
    """No replay or credentials: interrupted apply without a result stays unknown."""

    async def verified(self, lease, key):
        operation = await self.context.authority.resolve_recovery(lease)
        if not isinstance(operation.grant, RecoveryGrant) or not same_recovery_claim(
            operation.grant.lease, lease
        ):
            raise OperationRefused("apply recovery claim changed")
        row, step, _ = await original_result(operation, self.context, key)
        _, _, _, source, _ = await validate_phase(
            operation, self.context, require_fresh=False
        )
        metadata = json.loads(row["artifact_metadata_json"])
        if (
            step.step_id not in APPLIED_PHASES
            or source is None
            or metadata.get("next_phase") != "bootstrap-workspace"
            or metadata.get("source_artifact_id") != source["artifact_id"]
            or metadata.get("allocation_source_operation_id") != lease.operation_id
            or row["target_json"] != source["target_json"]
            or metadata.get("module_sha256")
            != json.loads(source["artifact_metadata_json"]).get("module_sha256")
            or not isinstance(metadata.get("provider_snapshot"), dict)
            or not metadata["provider_snapshot"]
        ):
            raise OperationRefused(
                "completed apply lacks its original reviewed provider result"
            )
        await asyncio.to_thread(verify_prepared_artifact, source, self.context)
        facts = await self.context.authority.lifecycle(
            lease,
            key,
            artifact_id=row["artifact_id"],
            plan_digest=operation.plan_digest,
            phase=step.step_id,
        )
        if facts != {
            "provider_snapshot": metadata["provider_snapshot"],
            "source_artifact_id": source["artifact_id"],
        }:
            raise OperationRefused(
                "fresh applied provider identities differ from original result"
            )
        # Recheck the saved source after the external observation, as well as the
        # live claim. A changed file or revoked run cannot settle the paid task.
        await asyncio.to_thread(verify_prepared_artifact, source, self.context)
        current = await self.context.authority.resolve_recovery(lease)
        if not same_recovery_operation(current, operation):
            raise OperationRefused(
                "apply recovery authority changed during observation"
            )
        async with self.context.connect() as connection, connection.transaction():
            if not await lock_recovery_grant(connection, current.grant):
                raise OperationRefused("apply recovery expired during observation")
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
            raise OperationRefused("apply recovery descriptor changed")
        return (
            CallOutcome.SUCCEEDED,
            "completed apply freshly observed; bootstrap approval remains required",
            row["artifact_id"],
        )

    async def accounting(self, lease, calls):
        result = await super().accounting(lease, calls)
        result["reason"] = (
            "completed apply freshly observed; bootstrap and allocation settlement remain outstanding"
        )
        return result
