"""Current delivery diagnosis, separate from completion and merge authorization."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select

from .execution_state import TERMINAL_EXECUTION_STATUSES
from .models import OrchestrationExecution


class DeliveryProgress(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stage: str
    actor: str
    detail: str
    blocker: str | None = None
    blockers: list[str] = Field(default_factory=list)
    next_action: str | None = None
    scheduled_action: str | None = None
    next_check_at: str | None = None
    observed_at: str | None = None
    automation: str = "not_configured"
    head_sha: str | None = None
    checks_state: str | None = None
    review_state: str | None = None


def provider_progress(binding, evidence) -> dict:
    """Bounded provider facts; never grants approval or claims a job is scheduled."""
    if evidence is None:
        return DeliveryProgress(
            stage="provider_unavailable",
            actor="engine",
            blocker="provider_unavailable",
            blockers=["provider_unavailable"],
            detail="GitHub evidence is unavailable; merge and review status could not be verified.",
            next_action="Recheck GitHub evidence.",
            head_sha=binding.head_sha,
        ).model_dump()
    checks = evidence.checks_state or ("SUCCESS" if evidence.checks_successful else "UNKNOWN")
    review = evidence.review_state or ("approved" if evidence.review_approved else "missing")
    common = dict(head_sha=evidence.head_sha, checks_state=checks, review_state=review)
    blockers = []
    if evidence.provider_repository_id != binding.provider_repository_id or evidence.provider_pr_node_id != binding.provider_pr_node_id:
        blockers.append("pr_identity_changed")
    if not evidence.head_sha or evidence.head_sha != binding.head_sha:
        blockers.append("head_changed")
    if checks in {"FAILURE", "ERROR"}:
        blockers.append("ci_failed")
    elif not evidence.checks_successful:
        blockers.append("ci_pending" if checks in {"PENDING", "EXPECTED"} else "ci_missing")
    if not evidence.review_approved:
        blockers.append(
            {"changes_requested": "changes_requested", "stale": "review_stale", "unverified": "review_unverified"}.get(review, "review_required")
        )
    if evidence.merged and (not evidence.merge_commit_sha or not evidence.merged_at):
        blockers.append("merge_evidence_incomplete")
    if not evidence.merged:
        if evidence.draft:
            blockers.append("draft")
        if evidence.mergeable == "CONFLICTING":
            blockers.append("merge_conflict")
        elif evidence.merge_state in {"BLOCKED", "BEHIND", "DIRTY"}:
            blockers.append("github_merge_blocked")
    descriptions = {
        "pr_identity_changed": (
            "binding",
            "operator",
            "The observed PR identity differs from the registered implementation.",
            "Verify and recover the correct PR binding.",
        ),
        "head_changed": (
            "binding",
            "developer",
            "The PR head differs from the engine binding; evidence for the registered revision cannot complete this story.",
            "Register the current PR revision and obtain fresh review.",
        ),
        "ci_failed": ("repair", "developer", "CI has failed on the current PR revision.", "Repair the failing checks, then request a fresh review."),
        "ci_pending": ("checks", "github", "CI is still running on the current PR revision.", "Wait for checks to finish."),
        "ci_missing": (
            "checks",
            "developer",
            "Successful checks for the current PR revision have not been verified.",
            "Run or restore the required checks.",
        ),
        "changes_requested": ("repair", "developer", "The reviewer requested changes.", "Address the findings and obtain a fresh review."),
        "review_stale": ("review", "reviewer", "The latest approval describes a different commit.", "Review the current PR revision."),
        "review_unverified": (
            "review",
            "reviewer",
            "The review evidence is incomplete or could not be verified.",
            "Publish a complete approval or requested-changes verdict for this revision.",
        ),
        "review_required": (
            "review",
            "reviewer",
            "The current PR revision still needs verified approval.",
            "Review and publish the verdict for this revision.",
        ),
        "draft": ("merge", "developer", "The PR is still a draft.", "Mark the PR ready when review requirements are satisfied."),
        "merge_conflict": ("repair", "developer", "The PR has merge conflicts.", "Resolve conflicts and rerun checks and review."),
        "github_merge_blocked": ("merge", "operator", "GitHub reports an unmet merge requirement.", "Resolve the repository's merge requirements."),
        "merge_evidence_incomplete": (
            "verification",
            "engine",
            "GitHub has not supplied complete merge evidence.",
            "Recheck the merge commit and merge time.",
        ),
    }
    if blockers:
        stage, actor, detail, action = descriptions[blockers[0]]
        return DeliveryProgress(
            stage=stage, actor=actor, detail=detail, next_action=action, blocker=blockers[0], blockers=blockers, **common
        ).model_dump()
    if evidence.merged:
        return DeliveryProgress(
            stage="reconciliation",
            actor="engine",
            detail="The bound PR is merged with verified checks and approval.",
            next_action="Reconcile delivery and remaining gates.",
            **common,
        ).model_dump()
    if evidence.mergeable != "MERGEABLE" or evidence.merge_state not in {"CLEAN", "UNSTABLE", "HAS_HOOKS"}:
        return DeliveryProgress(
            stage="merge",
            actor="engine",
            detail="Checks and approval are verified; GitHub merge eligibility still needs confirmation.",
            next_action="Recheck GitHub merge eligibility.",
            blocker="mergeability_unverified",
            blockers=["mergeability_unverified"],
            **common,
        ).model_dump()
    return DeliveryProgress(
        stage="merge",
        actor="operator",
        detail="Checks and approval are verified and GitHub reports the PR mergeable.",
        next_action="Merge through the repository's normal rules.",
        **common,
    ).model_dump()


async def current_executions(session, *, org_id: str, flow_id: str) -> dict:
    """Newest cycle per node, tenant-filtered before projection."""
    rows = await session.scalars(
        select(OrchestrationExecution)
        .where(
            OrchestrationExecution.org_id == org_id,
            OrchestrationExecution.flow_id == flow_id,
        )
        .order_by(OrchestrationExecution.node_id, OrchestrationExecution.cycle.desc())
    )
    result = {}
    for row in rows:
        result.setdefault(row.node_id, row)
    return result


def node_progress(
    *,
    node,
    binding,
    dispatch,
    result,
    execution=None,
    policy_enabled=False,
    plan_version=None,
    observed_at=None,
) -> DeliveryProgress | None:
    if node.kind != "story":
        return None
    if node.state == "passed":
        return DeliveryProgress(
            stage="complete", actor="none", detail="Delivery evidence was accepted.", automation="not_applicable", observed_at=observed_at
        )
    if node.state in {"failed", "halted", "rejected_at_gate", "awaiting_gate", "superseded"}:
        return DeliveryProgress(
            stage=node.state,
            actor="none" if node.state == "superseded" else "operator",
            blocker=node.state,
            blockers=[node.state],
            detail="This story was superseded." if node.state == "superseded" else "This story requires a human decision before it can continue.",
            next_action=None if node.state == "superseded" else "Inspect the recorded hold and use the appropriate approval or resume control.",
            automation="paused",
        )
    if execution is not None:
        current_execution = (
            policy_enabled
            and execution.accepted_plan_version > 0
            and execution.node_id == node.id
            and execution.org_id == node.org_id
            and execution.flow_id == node.flow_id
            and execution.cycle == node.attempts
            and (plan_version is None or execution.accepted_plan_version == plan_version)
        )
        if not current_execution:
            return DeliveryProgress(
                stage="continuation",
                actor="operator",
                blocker="execution_stale",
                blockers=["execution_stale"],
                detail="The recorded execution does not match the current attempt and accepted execution policy.",
                next_action="Continue this story under its current attempt and accepted plan.",
                automation="paused",
            )
        terminal = execution.status in TERMINAL_EXECUTION_STATUSES
        due = execution.next_check_at.isoformat() if execution.next_check_at and not terminal else None
        actor = execution.block_owner or {"delivering": "developer", "awaiting_review": "reviewer", "repairing": "developer"}.get(
            execution.phase, "engine"
        )
        return DeliveryProgress(
            stage=execution.phase,
            actor=actor,
            blocker=execution.block_code,
            blockers=[execution.block_code] if execution.block_code else [],
            detail=execution.block_detail or execution.progress_note or "The engine is tracking this delivery stage.",
            next_action=execution.block_required_input or ("Reconcile the recorded delivery stage." if not terminal else None),
            scheduled_action="Reconcile this execution" if due else None,
            next_check_at=due,
            automation="engine" if not terminal else "not_applicable",
            observed_at=execution.progressed_at.isoformat() if execution.progressed_at else None,
        )
    raw = result.get("delivery_progress")
    observed_binding = result.get("binding") or {}
    current = binding is not None and getattr(binding, "attempt", None) == node.attempts
    matching_observation = (
        current
        and result.get("attempt") == node.attempts
        and isinstance(observed_binding, dict)
        and observed_binding.get("id") == binding.id
        and observed_binding.get("revision") == binding.revision
    )
    progress = None
    if matching_observation and isinstance(raw, dict):
        try:
            progress = DeliveryProgress.model_validate(raw)
            progress.observed_at = observed_at
        except ValidationError:
            # An old/malformed diagnostic snapshot must not blank the whole graph.
            progress = None
    from .delivery_adoption import historical_binding

    if current and historical_binding(binding):
        hold = result.get("historical_hold") if matching_observation else None
        if not progress or (progress.stage == "reconciliation" and hold):
            blocker = hold.get("code") if isinstance(hold, dict) else "historical_verification_pending"
            progress = DeliveryProgress(
                stage="historical_delivery",
                actor="operator"
                if blocker == "execution_policy_reconciliation"
                else hold.get("actor", "engine")
                if isinstance(hold, dict)
                else "engine",
                blocker=blocker,
                blockers=[blocker],
                detail=result.get("evidence") if matching_observation else "Historical delivery is registered; verification is pending.",
                next_action="Reconcile policy obligations."
                if blocker == "execution_policy_reconciliation"
                else "Recheck delivery and predecessor requirements.",
                head_sha=binding.head_sha,
                observed_at=observed_at if matching_observation else None,
            )
        if isinstance(hold, dict) and hold.get("code") and hold["code"] not in progress.blockers:
            progress.blockers.append(hold["code"])
        progress.automation = "reconciliation_only"
        progress.scheduled_action = None
        progress.next_check_at = None
        return progress
    if progress is None:
        if node.state == "running":
            progress = DeliveryProgress(
                stage="development",
                actor="developer",
                detail="The current attempt was dispatched; check worker activity for its live status.",
                next_action="Finish the work and acknowledge its PR handoff.",
            )
        elif node.state == "awaiting_merge" and not current:
            progress = DeliveryProgress(
                stage="handoff",
                actor="developer",
                blocker="pr_binding_missing",
                blockers=["pr_binding_missing"],
                detail="No PR is registered for this attempt.",
                next_action="Complete or recover the PR handoff.",
            )
        elif node.state == "awaiting_merge":
            progress = DeliveryProgress(
                stage="verification",
                actor="engine",
                blocker="evidence_not_observed",
                blockers=["evidence_not_observed"],
                detail="The current PR binding is waiting for fresh provider evidence.",
                next_action="Recheck the bound PR.",
            )
        else:
            progress = DeliveryProgress(
                stage="pending",
                actor="engine",
                detail="Waiting for this story's dependencies and admission requirements.",
                next_action="Reassess readiness when prerequisites complete.",
            )
    # Legacy completion checks are not scheduled reviewer, repair or merge jobs.
    # Provider snapshots cannot supply controller scheduling fields.
    progress.scheduled_action = None
    progress.next_check_at = None
    if node.state == "awaiting_merge":
        code = "execution_not_initialized" if policy_enabled else "automation_not_configured"
        if code not in progress.blockers:
            progress.blockers.append(code)
        if progress.blocker is None:
            progress.blocker = code
        progress.automation = "not_configured"
    return progress
