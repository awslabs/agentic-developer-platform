"""Close the exact native dispatch proved by the operations runtime verifier."""

from src.shared.models.base import utcnow

from .environment_leases import LeaseHolder, ReleaseReason, release_lease
from .models import OrchestrationAction


async def settle_native_release(session, *, execution, run_id, verified):
    """Participate in the caller's node/execution/claim completion transaction.

    A dispatch may still be prepared after Actions finishes. Its independently
    verified receipt resolves that effect, but no unrelated pending action is
    eligible. Compare the complete verified snapshot under lock so a changed
    dispatch cannot release a successor's deployment lease.
    """
    action = await session.get(OrchestrationAction, verified["action_id"], with_for_update=True, populate_existing=True)
    if (
        action is None
        or action.org_id != execution.org_id
        or action.execution_id != execution.id
        or action.kind != "operations_native_release_dispatch"
        or action.status not in {"prepared", "unknown", "succeeded"}
        or action.operation_key != verified["operation_key"]
        or action.detail != verified["detail"]
        or action.detail.get("correlation") != run_id
    ):
        raise ValueError("verified native release changed before settlement")
    release = action.detail.get("shared_release") or {}
    if release.get("generation") != execution.claim_generation:
        raise ValueError("verified native release generation changed before settlement")
    handed = await release_lease(
        session,
        canonical_target_key=release["target_key"],
        holder=LeaseHolder(execution.org_id, release["owner"], execution.claim_generation),
        reason=ReleaseReason.COMPLETED,
        terminal_evidence=verified["runtime_evidence_id"],
    )
    if not handed.applied:
        raise ValueError("verified native release lease settlement refused")
    action.status = "succeeded"
    action.observed_at = utcnow()
    action.receipt_ref = verified["runtime_evidence_id"]
