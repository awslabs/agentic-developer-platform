"""Retire a stale review intent only before its atomic preparation boundary."""

from sqlalchemy import select

from .execution_state import Observation, ObservedOutcome, OutcomeKind
from .execution_store import record_observation
from .models import OrchestrationAction, OrchestrationDecision, OrchestrationWorkClaim
from .review_cycle import DISPATCH_KIND, CycleBlockedError

PREFIX = "unstarted-review-head-change:"


def retired_unstarted_dispatch(action):
    return action.kind == DISPATCH_KIND and action.status == "failed" and action.receipt_ref == PREFIX + action.id


async def settle_unstarted_dispatch(session, context, snapshot):
    """Called under the runner's node/execution locks, with no provider I/O.

    Dispatch preparation takes these same locks before writing its receipt or
    moving the claim. If it won the race, refuse and roll back the entire runner
    advance. If retirement won, preparation's pending-key fence refuses it.
    Provisioned or potentially published assignments are never retired here.
    """
    from .review_cycle_dispatch import receipt_id

    action = await session.get(OrchestrationAction, snapshot["action_id"], populate_existing=True)
    claim = await session.scalar(
        select(OrchestrationWorkClaim)
        .where(
            OrchestrationWorkClaim.id == context.identity.claim_id,
            OrchestrationWorkClaim.org_id == context.identity.org_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    receipt = await session.get(OrchestrationDecision, receipt_id(snapshot["operation_key"]))
    if (
        action is None
        or action.org_id != context.identity.org_id
        or action.execution_id != context.execution.id
        or action.kind != DISPATCH_KIND
        or action.operation_key != snapshot["operation_key"]
        or action.status not in {"prepared", "unknown"}
        or action.detail != snapshot["detail"]
        or receipt is not None
        or claim is None
        or claim.state != "held"
        or claim.generation != context.identity.claim_generation
        or claim.active_run_id != snapshot["detail"]["active_run_id"]
    ):
        raise CycleBlockedError("unstarted_review_preparation_changed")
    outcome = await record_observation(
        session,
        identity=context.identity,
        observation=Observation(
            operation_key=action.operation_key,
            outcome=ObservedOutcome.FAILED,
            receipt_ref=PREFIX + action.id,
            detail="head_changed_before_preparation; observed_head_sha=" + snapshot["current_head"],
        ),
    )
    if outcome.kind is not OutcomeKind.APPLIED:
        raise CycleBlockedError("unstarted_review_retirement_conflict")
