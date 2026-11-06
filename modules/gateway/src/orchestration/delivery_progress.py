"""Current delivery diagnosis, separate from completion and merge authorization."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select

from .execution_state import TERMINAL_EXECUTION_STATUSES
from .models import OrchestrationExecution
from .story_acceptance import StoryAcceptance
from .validation_diagnostics import ValidationCheck, check_result, safe_code


class DeliveryProgress(BaseModel):
    model_config = ConfigDict(extra="forbid")

    validations: list[ValidationCheck] = Field(default_factory=list)
    acceptance: StoryAcceptance | None = None
    stage: str
    actor: str
    detail: str
    blocker: str | None = None
    blockers: list[str] = Field(default_factory=list)
    next_action: str | None = None
    required_input: str | None = None
    scheduled_action: str | None = None
    next_check_at: str | None = None
    observed_at: str | None = None
    # Who drives the next step. "engine" = a controller execution owns it;
    # "not_started" = an accepted policy will dispatch it once ready; "paused" /
    # "blocked" = a human or admission hold; "reconciliation_only" = historical
    # delivery; "not_initialized" = policy accepted but no execution recorded
    # for this attempt; "not_configured" = no accepted autonomous policy.
    # The default is only correct for provider snapshots of legacy flows and is
    # overridden for every node path below that knows the policy state.
    automation: str = "not_configured"
    head_sha: str | None = None
    checks_state: str | None = None
    review_state: str | None = None


EVALUATION_WAIT_DETAILS = {
    "verified_deployment_required": (
        "flow-owner",
        "The required deployment receipt is missing or stale.",
        "Verify deployment of the current predecessor revision.",
    ),
    "evaluation_predecessor_evidence_missing": ("engine", "A passed predecessor has no acceptance evidence.", "Reconcile the predecessor decision."),
    "evaluation_merge_evidence_missing_or_ambiguous": (
        "engine",
        "Current merge and check evidence cannot be verified.",
        "Reconcile the predecessor merge and checks.",
    ),
    "evaluation_predecessor_binding_changed": (
        "developer",
        "The predecessor pull request binding changed.",
        "Register and verify the current predecessor revision.",
    ),
    "evaluation_merge_plan_changed": (
        "flow-owner",
        "The predecessor merge no longer matches the accepted plan.",
        "Review the changed plan and obtain current evidence.",
    ),
    "evaluation_specification_missing": (
        "flow-owner",
        "The machine evaluation has no accepted specification.",
        "Accept a machine specification or choose human acceptance.",
    ),
    "evaluation_flow_paused": ("flow-owner", "The flow is paused.", "Resume the flow before presenting the evaluation gate."),
    "evaluation_flow_not_active": ("flow-owner", "The flow is not active.", "Resume the flow before presenting the evaluation gate."),
    "evaluation_evidence_unverifiable": (
        "platform-operator",
        "Accepted evaluation evidence cannot be verified.",
        "Reconcile the accepted plan and predecessor evidence before retrying the evaluation.",
    ),
    "deployment_authority_unverifiable": (
        "platform-operator",
        "Required deployment evidence cannot be verified.",
        "Restore deployment evidence access.",
    ),
}


def _admission_progress(node, plan_version, policy_hash, refusal):
    if node.state != "ready" or not isinstance(refusal, dict):
        return None
    from .admission_diagnostics import CONTRACT, description, recognized_code

    if (
        refusal.get("contract") != CONTRACT
        or type(refusal.get("attempt")) is not int
        or refusal["attempt"] != node.attempts
        or type(refusal.get("accepted_plan_version")) is not int
        or refusal["accepted_plan_version"] != plan_version
        or refusal.get("policy_hash") != policy_hash
        or not isinstance(refusal.get("block_code"), str)
        or safe_code(refusal["block_code"]) != refusal["block_code"]
    ):
        return None
    code = refusal["block_code"]
    owner, detail, action = description(code)
    checks = []
    if not recognized_code(code):
        unknown = check_result(code, observed_at=refusal.get("observed_at"))
        checks.append(unknown)
        detail, action = unknown.detail, unknown.next_action
    if code == "operations_release_unverified" and safe_code(refusal.get("detail")) != "validation_unavailable":
        underlying = check_result(refusal["detail"], observed_at=refusal.get("observed_at"))
        checks.append(underlying)
        detail, action = underlying.detail, underlying.next_action
    return DeliveryProgress(
        validations=checks,
        stage="admission",
        actor=owner,
        blocker=code,
        blockers=[code],
        detail=detail,
        next_action=action,
        automation="blocked",
        observed_at=refusal.get("observed_at"),
    )


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


def with_runtime_budget_wait(progress, activity):
    """Overlay fresh current-worker diagnostics without changing engine authority."""
    wait = activity.runtime_budget_wait if activity else None
    if progress is None or wait is None or progress.blocker or progress.automation != "engine":
        return progress
    return progress.model_copy(
        update={
            **wait.model_dump(),
            "blockers": [wait.blocker],
            "automation": "blocked",
            "actor": "reviewer" if "reviewer" in activity.persona else "developer",
        }
    )


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


def is_preserved_execution(execution, preserved_pairs):
    """Expose only a derived display flag, never an approval record to the graph."""
    return execution is not None and (execution.node_id, execution.accepted_plan_version) in preserved_pairs


def node_progress(
    *,
    node,
    binding,
    dispatch,
    result,
    execution=None,
    policy_enabled=False,
    plan_version=None,
    preserved_execution=False,
    policy_hash=None,
    admission_refusal=None,
    evaluation_wait=None,
    repository_refusal=None,
    developer_retry=None,
    observed_at=None,
) -> DeliveryProgress | None:
    if node.kind == "eval":
        if node.state == "passed":
            return DeliveryProgress(stage="complete", actor="none", detail="Evaluation was accepted.", automation="not_applicable")
        if node.state == "waived":
            return DeliveryProgress(stage="waived", actor="none", detail="Owner waived independent evaluation.", automation="not_applicable")
        if node.state == "superseded":
            return DeliveryProgress(stage="superseded", actor="none", detail="Evaluation was superseded.", automation="not_applicable")
        if node.state in {"running", "awaiting_merge"}:
            return DeliveryProgress(stage=node.state, actor="engine", detail="Evaluation is in progress.", automation="engine")
        if node.state == "awaiting_gate":
            return DeliveryProgress(
                stage="awaiting_gate",
                actor="flow-owner",
                detail="Human evaluation is ready for review.",
                next_action="Review the presented evidence and decide whether to approve the gate.",
                automation="paused",
            )
        if node.state in {"pending", "ready"}:
            admission = _admission_progress(node, plan_version, policy_hash, admission_refusal)
            if admission is not None:
                return admission
            if repository_refusal is not None:
                from .repository_evaluation_diagnostics import DETAILS

                code = repository_refusal.get("code")
                if code in DETAILS:
                    owner, detail, required_input, action = DETAILS[code]
                    return DeliveryProgress(
                        stage="evidence_wait",
                        actor=owner,
                        blocker=code,
                        blockers=[code],
                        detail=detail,
                        required_input=required_input,
                        next_action=action,
                        automation="blocked",
                        observed_at=repository_refusal.get("observed_at"),
                    )
            if (
                isinstance(evaluation_wait, dict)
                and type(evaluation_wait.get("attempt")) is int
                and evaluation_wait["attempt"] == node.attempts
                and type(evaluation_wait.get("plan_version")) is int
                and evaluation_wait["plan_version"] == plan_version
                and evaluation_wait.get("block_code") in EVALUATION_WAIT_DETAILS
            ):
                code = evaluation_wait["block_code"]
                owner, detail, action = EVALUATION_WAIT_DETAILS[code]
                return DeliveryProgress(
                    stage="evidence_wait",
                    actor=owner,
                    blocker=code,
                    blockers=[code],
                    detail=detail,
                    next_action=action,
                    automation="blocked",
                    observed_at=evaluation_wait.get("observed_at"),
                )
            return DeliveryProgress(
                stage=node.state,
                actor="engine",
                detail=("Evaluation is ready for the next engine check." if node.state == "ready" else "Evaluation is waiting for its predecessors."),
                automation="not_started",
            )
        return DeliveryProgress(
            stage=node.state,
            actor="operator",
            blocker=node.state,
            blockers=[node.state],
            detail="Evaluation requires a decision before it can continue.",
            automation="paused",
        )
    if node.kind != "story":
        return None
    from .deployment_authority import DELIVERY_PHASES

    if node.state == "passed" and (execution is None or execution.status in TERMINAL_EXECUTION_STATUSES or execution.phase not in DELIVERY_PHASES):
        return DeliveryProgress(
            stage="complete",
            actor="none",
            detail=result["evidence"] if result.get("external_completion") else "Delivery evidence was accepted.",
            automation="not_applicable",
            observed_at=observed_at,
        )
    admission = _admission_progress(node, plan_version, policy_hash, admission_refusal)
    if admission is not None:
        return admission
    if node.state in {"failed", "ready"} and isinstance(developer_retry, dict) and developer_retry.get("attempt") == node.attempts:
        outcome = developer_retry.get("result")
        if outcome == "automatic_recovery_disabled":
            return DeliveryProgress(
                stage="retry",
                actor="flow-owner",
                automation="paused",
                detail="Automatic retry recovery is off for this flow. Saved progress and attempts are retained.",
                next_action="Turn automatic retry recovery on in the flow controls to allow the engine to continue within approved limits.",
            )
        if outcome == "checkpoint_requires_reconciliation":
            return DeliveryProgress(
                stage="retry",
                actor="platform-operator",
                automation="blocked",
                detail="The saved checkpoint could not be verified against this story's assignment. The existing branch and progress are retained.",
                next_action="Reconcile the checkpoint identity and revision before the engine starts another paid attempt.",
                blocker=outcome,
                blockers=[outcome],
            )
        if outcome in {"retry_backoff", "scheduled"}:
            return DeliveryProgress(
                stage="retry",
                actor="engine",
                detail=(
                    "The worker never acknowledged startup. Its assignment was fenced; the engine will retry within the accepted allowance."
                    if developer_retry.get("reason") == "stall_retry"
                    else "The worker attempt ended. The engine will continue using the prior failure evidence and saved progress."
                ),
                next_action="Recheck recovery eligibility and dispatch the next permitted attempt.",
                scheduled_action="Continue after backoff" if outcome == "retry_backoff" else "Dispatch the recovery attempt",
                automation="engine",
            )
        if outcome == "recovery_evidence_unavailable":
            return DeliveryProgress(
                stage="retry",
                actor="engine",
                detail="Recovery evidence could not be verified. The engine will check again before starting another worker.",
                blocker=outcome,
                blockers=[outcome],
                next_action="Recheck recovery evidence.",
                scheduled_action="Recheck recovery eligibility",
                automation="reconciliation_only",
            )
        descriptions = {
            "attempts_exhausted": "The approved development attempts are exhausted.",
            "policy_expired": "The accepted execution policy has expired.",
            "flow_deadline_exhausted": "The flow's approved execution window is exhausted.",
            "flow_not_active": "The flow is paused or no longer active.",
            "repair_requires_approval": "The accepted policy does not allow autonomous repair.",
            "failure_requires_operator": "The recorded failure requires operator intervention.",
            "operations_input_required": (
                "The operations agent identified missing protected input or human evidence. Automatic retries cannot supply it."
            ),
            "deterministic_failure_unchanged": "The operations agent repeated the same completed diagnosis without a verified repair.",
        }
        if outcome in descriptions:
            return DeliveryProgress(
                stage="failed",
                actor="operator",
                detail=(
                    "Worker vanished twice; an operator must inspect this story before another attempt."
                    if outcome == "failure_requires_operator" and developer_retry.get("detail") == "worker vanished twice"
                    else descriptions[outcome]
                ),
                blocker=outcome,
                blockers=[outcome],
                next_action=(
                    "Inspect the latest completed diagnosis in the operations task board, "
                    "supply the required input or evidence, then resume this story."
                    if outcome == "operations_input_required"
                    else "Inspect the failure and resolve the recorded recovery blocker."
                ),
                automation="paused",
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
            and (plan_version is None or execution.accepted_plan_version == plan_version or preserved_execution)
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
        operations_continuation = (getattr(execution, "handoff_receipt_ref", None) or "").startswith("operations-continuation:")
        return DeliveryProgress(
            stage="operations_continuation" if operations_continuation else execution.phase,
            actor="engine" if operations_continuation and not execution.block_owner else actor,
            blocker=execution.block_code,
            blockers=[execution.block_code] if execution.block_code else [],
            detail=execution.block_detail or execution.progress_note or "The engine is tracking this delivery stage.",
            next_action=execution.block_required_input
            or (
                "Automatically continue repair, deployment and verification."
                if operations_continuation and not terminal
                else "Reconcile the recorded delivery stage."
                if not terminal
                else None
            ),
            scheduled_action=("Recheck operations continuation" if operations_continuation else "Reconcile this execution") if due else None,
            next_check_at=due,
            automation="blocked" if execution.block_code else "engine" if not terminal else "not_applicable",
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
                automation="engine" if policy_enabled else "not_configured",
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
            # Not dispatched yet. Under an accepted policy the engine starts it when
            # its predecessors pass; only a flow without that policy is unconfigured.
            progress = DeliveryProgress(
                stage="pending",
                actor="engine",
                detail="Waiting for this story's dependencies and admission requirements.",
                next_action="Reassess readiness when prerequisites complete.",
                automation="not_started" if policy_enabled else "not_configured",
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
        progress.automation = "not_initialized" if policy_enabled else "not_configured"
    return progress
