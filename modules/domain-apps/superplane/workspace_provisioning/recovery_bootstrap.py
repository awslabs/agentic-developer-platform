"""Read completed canonical bootstrap journals under current recovery authority."""

import json

from harness_jobs.execution import CallOutcome
from harness_jobs.identity import OperationRefused
from harness_jobs.recovery_grant import RecoveryGrant
from superplane_executor.recovery_authority import (
    same_recovery_claim,
    same_recovery_operation,
)

from .artifacts import digest, proposal
from .bootstrap_result import read_bootstrap_anchor
from .recovery_proposals import original_result
from .runtime import validate_phase


async def completed_bootstrap(operation, context, key):
    """Original ready output plus unchanged canonical registration/revocation proof.

    This establishes completion of the original bootstrap, not a new live health
    observation. It grants no provider access and cannot adopt a partial journal.
    """
    row, step, _ = await original_result(operation, context, key)
    _, request, _, source, _ = await validate_phase(
        operation, context, require_fresh=False
    )
    metadata = json.loads(row["artifact_metadata_json"])
    anchor = metadata.get("bootstrap_anchor")
    if (
        step.step_id != "bootstrap-workspace"
        or source is None
        or metadata.get("next_phase") != "complete"
        or metadata.get("source_artifact_id") != source["artifact_id"]
        or row["target_json"] != source["target_json"]
        or row["account_id"] != source["account_id"]
        or not isinstance(anchor, dict)
        or not isinstance(anchor.get("registration"), dict)
        or not isinstance(anchor.get("authority"), list)
    ):
        raise OperationRefused(
            "bootstrap recovery has no original canonical completion"
        )
    registration = anchor["registration"]
    if (
        registration.get("account_id") != row["account_id"]
        or registration.get("region") != request.region
    ):
        raise OperationRefused("canonical bootstrap registration target changed")
    current = [
        entry
        for entry in anchor["authority"]
        if isinstance(entry, dict)
        and entry.get("generation") == anchor.get("current_generation")
        and entry.get("operation_id") == operation.grant.lease.operation_id
    ]
    if len(current) != 1 or not isinstance(current[0].get("claim"), str):
        raise OperationRefused(
            "canonical bootstrap original authority generation changed"
        )
    observed = await read_bootstrap_anchor(
        context,
        operation_id=operation.grant.lease.operation_id,
        org_id=operation.grant.lease.org_id,
        workspace_id=operation.grant.lease.workspace_id,
        registration=registration,
        claim=current[0]["claim"],
    )
    if observed != anchor:
        raise OperationRefused("canonical bootstrap completion journal changed")
    # Authenticate the same immutable output and current recovery subject after
    # the domain journal read, which uses a separate database transaction.
    latest, _, _ = await original_result(operation, context, key)
    if latest != row:
        raise OperationRefused("canonical bootstrap immutable result changed")
    return row, {
        "bootstrap_anchor_sha256": digest(anchor),
        "source_artifact_id": source["artifact_id"],
    }


class BootstrapRecovery:
    def __init__(self, context):
        self.context = context

    async def verified(self, lease, key):
        operation = await self.context.authority.resolve_recovery(lease)
        if not isinstance(operation.grant, RecoveryGrant) or not same_recovery_claim(
            operation.grant.lease, lease
        ):
            raise OperationRefused("canonical bootstrap recovery claim changed")
        row, expected = await completed_bootstrap(operation, self.context, key)
        facts = await self.context.authority.bootstrap(
            lease,
            key,
            artifact_id=row["artifact_id"],
            plan_digest=operation.plan_digest,
        )
        latest = await self.context.authority.resolve_recovery(lease)
        if not same_recovery_operation(latest, operation):
            raise OperationRefused("canonical bootstrap recovery authority changed")
        checked, current = await completed_bootstrap(latest, self.context, key)
        if facts != expected or checked != row or current != expected:
            raise OperationRefused("canonical bootstrap journal observation changed")
        return latest, row

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
            raise OperationRefused("canonical bootstrap recovery descriptor changed")
        return (
            CallOutcome.SUCCEEDED,
            "original canonical bootstrap completion recovered",
            self.reference(row),
        )

    @staticmethod
    def reference(row):
        return json.loads(row["artifact_metadata_json"])["bootstrap_anchor"][
            "registration"
        ]["cluster_arn"]

    async def accounting(self, lease, calls):
        if len(calls) != 1 or calls[0].outcome is not CallOutcome.SUCCEEDED:
            raise OperationRefused("canonical bootstrap call is not confirmed")
        operation, row = await self.verified(lease, calls[0].idempotency_key)
        if calls[0].provider_ref != self.reference(row):
            raise OperationRefused(
                "canonical bootstrap original cluster reference changed"
            )
        return {
            "allocation_id": operation.request.parameters["allocation_id"],
            "inventory_complete": False,
            "release_permitted": False,
            "may_mark_released": False,
            "resource_dispositions": {},
            "unresolved_resources": [],
            "exposure": "unresolved",
            "reason": "original bootstrap complete; allocation remains reserved",
            "lifecycle_proposal": proposal(row),
        }
