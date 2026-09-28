"""Accepted code-delivery policy for the explicitly selected shared worker role.

The role retains its configured AWS permissions. Reporting capabilities identify
platform model traffic; they do not claim provider-side IAM isolation. This uses
the existing policy rule and Redis reservation accumulator, never a second budget.
"""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import JSON, and_, func, not_, or_, select

from .dispatch import graph_address
from .execution_policy import Action, CredentialScope, ResourceRef, authorize_action
from .execution_state import BlockCode
from .flow_meter import meter_target, read_flow_meter
from .models import (
    OrchestrationAcceptedPlan,
    OrchestrationDecision,
    OrchestrationFlow,
    OrchestrationNode,
    OrchestrationWorkClaim,
)
from .policy_admission import SpendObservation, load_in_force_policy, resolve_authorization_context
from .stage_attempts import stage_attempts

CODE_ACTIONS = frozenset({Action.DEVELOP, Action.REPAIR, Action.REVIEW, Action.MERGE})


def _refuse(code, block=BlockCode.AUTHORITY_UNVERIFIABLE):
    from .review_cycle import CycleBlockedError

    raise CycleBlockedError(code, block)


def _utc(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


async def shared_inputs(session, *, org_id, flow_id, lock=False, allow_elapsed_window=False):
    """Only an attributed, in-force continuation accepts the configured role."""
    if os.environ.get("ADP_SHARED_WORKER_CONTINUATION_ENABLED", "false").lower() != "true":
        _refuse("shared_worker_continuation_disabled")
    # Admission holds this lock through assignment creation. Read-only model and
    # merge rechecks need no new slot and must not lock across provider callbacks.
    query = select(OrchestrationFlow).where(OrchestrationFlow.id == flow_id, OrchestrationFlow.org_id == org_id)
    flow = await session.scalar(query.with_for_update() if lock else query)
    if flow is None or flow.org_id != org_id or flow.state not in {"pending", "running"}:
        _refuse("continued_flow_not_active")
    inputs = await load_in_force_policy(session, org_id=org_id, flow_id=flow_id)
    plan = await session.scalar(
        select(OrchestrationAcceptedPlan).where(
            OrchestrationAcceptedPlan.org_id == org_id,
            OrchestrationAcceptedPlan.flow_id == flow_id,
            OrchestrationAcceptedPlan.superseded_at.is_(None),
        )
    )
    marker = (plan.plan_document or {}).get("execution_continuation") if plan else None
    if (
        inputs.refusal
        or inputs.policy is None
        or not isinstance(marker, dict)
        or marker.get("mode") != "shared_worker_role"
        or marker.get("contract_version") != 1
        or marker.get("budget_scope") != "authenticated_gateway_calls"
    ):
        _refuse("shared_worker_continuation_not_accepted")
    decision = await session.get(OrchestrationDecision, plan.accepted_by_decision_id) if plan.accepted_by_decision_id else None
    if decision is None or decision.org_id != org_id or decision.flow_id != flow_id or decision.actor_kind != "human":
        _refuse("continuation_acceptance_unverifiable")
    role = marker.get("worker_role_arn")
    authority = inputs.policy.user_credentials
    if not role or role != os.environ.get("AGENT_WORKER_ROLE_ARN") or authority is None or role not in authority.aws_role_arns:
        _refuse("shared_worker_role_changed")
    try:
        started = _utc(marker["accepted_at"])
        baseline = Decimal(marker["prior_spend_usd"])
        if not baseline.is_finite() or baseline < 0 or started > datetime.now(UTC):
            raise ValueError("invalid baseline")
    except (KeyError, ValueError, TypeError, InvalidOperation):
        _refuse("continuation_baseline_unverifiable")
    if not allow_elapsed_window and (datetime.now(UTC) - started).total_seconds() >= inputs.policy.limits.max_wall_clock_seconds:
        _refuse("wall_clock_limit_exceeded", BlockCode.ATTEMPTS_EXHAUSTED)
    return inputs, marker


async def initialize_shared_meter(*, org_id, flow_id, policy, marker) -> bool:
    """Seed before the acceptance transaction commits; never call at admission.

    A committed accepted plan proves initialization and historical settlement were
    acknowledged. A Redis loss after acceptance blocks all future admissions rather
    than reinitializing zero. An aborted acceptance may leave an orphan seed; no
    governed run can use it.
    """
    from src.budget.config import budget_config

    from .flow_budget import get_flow_reservations

    try:
        baseline = Decimal(marker["prior_spend_usd"])
        if not baseline.is_finite() or baseline < 0 or baseline >= policy.limits.max_spend_usd:
            return False
        store = get_flow_reservations()
        if not budget_config.budget_reservation_enabled or not store.enabled:
            return False
        target = meter_target(org_id=org_id, flow_id=flow_id, policy=policy)
        anchor = await store.reserve("__initialized__", Decimal(0), [replace(target, require_initialization=False)])
        if anchor is None or not anchor.admitted:
            return False
        seed = await store.reserve("__historical_spend__", baseline, [target])
        if seed is None or not seed.admitted:
            return False
        # This is acknowledged historical usage, not an in-flight provider call.
        # A strict reservation adds a pending receipt marker with a 61-minute
        # deadline. Settle the baseline now so that marker cannot later make an
        # otherwise valid lifetime meter unreadable. Reconcile swallows transport
        # failures, so require an observed settled snapshot before acceptance.
        await store.reconcile("__historical_spend__", baseline, [target])
        snapshot = await store.snapshot(target)
        return snapshot is not None and not snapshot.has_pending and snapshot.total_usd >= baseline
    except Exception:
        return False


async def _active_count(session, *, org_id, flow_id, exclude_run_id=None, initial_runs=None):
    from .run_reports import OrchestrationRunReport
    from .work_claims import ReleaseReason

    reports = list(
        await session.scalars(
            select(OrchestrationRunReport).where(
                OrchestrationRunReport.org_id == org_id,
                OrchestrationRunReport.flow_id == flow_id,
                or_(OrchestrationRunReport.terminal_receipt.is_(None), OrchestrationRunReport.terminal_receipt == JSON.NULL),
            )
        )
    )
    assignments = {}
    for report in reports:
        metadata = report.dispatch_metadata or {}
        fence = metadata.get("execution_continuation") or metadata.get("handoff_expect")
        if (
            isinstance(fence, dict)
            and isinstance(fence.get("claim_id"), str)
            and type(fence.get("claim_generation")) is int
            and fence["claim_generation"] > 0
        ):
            assignments[report.run_id] = fence
    claims = {
        claim.id: claim
        for claim in await session.scalars(
            select(OrchestrationWorkClaim).where(
                OrchestrationWorkClaim.org_id == org_id,
                OrchestrationWorkClaim.id.in_({fence["claim_id"] for fence in assignments.values()}),
            )
        )
    }
    count = 0
    for report in reports:
        if report.run_id == exclude_run_id:
            continue
        initial = (initial_runs or {}).get(report.node_id, {})
        if (
            initial.get("evidence_origin") == "owner_reconciled_legacy_delivery"
            and initial.get("run_id") == report.run_id
            and initial.get("attempt") == report.attempt
        ):
            continue
        fence = assignments.get(report.run_id)
        claim = claims.get(fence["claim_id"]) if fence else None
        if claim is not None and claim.provider_repository_id == report.provider_repository_id:
            # A generation advances only after an evidenced release or reconciled
            # takeover. A released current generation likewise records completion.
            # Expiry, node state, or a missing/changed claim alone proves neither.
            if claim.generation > fence["claim_generation"] or (
                claim.generation == fence["claim_generation"]
                and claim.state == "released"
                and claim.released_at is not None
                and claim.release_reason in {reason.value for reason in ReleaseReason}
            ):
                continue
            from .review_recovery import recovered_worker_exit

            if await recovered_worker_exit(session, report, claim):
                continue
        count += 1
    historical = or_(
        False,
        *[
            and_(OrchestrationNode.id == node_id, OrchestrationNode.attempts == run["attempt"])
            for node_id, run in (initial_runs or {}).items()
            if run.get("evidence_origin") == "owner_reconciled_legacy_delivery"
        ],
    )
    legacy = await session.scalar(
        select(func.count())
        .select_from(OrchestrationNode)
        .where(
            OrchestrationNode.org_id == org_id,
            OrchestrationNode.flow_id == flow_id,
            OrchestrationNode.state == "running",
            # A terminal current-attempt report is still a report: finishing a
            # worker must not turn its story back into an unreported legacy run.
            ~select(OrchestrationRunReport.run_id)
            .where(
                OrchestrationRunReport.org_id == OrchestrationNode.org_id,
                OrchestrationRunReport.flow_id == OrchestrationNode.flow_id,
                OrchestrationRunReport.node_id == OrchestrationNode.id,
                OrchestrationRunReport.attempt == OrchestrationNode.attempts,
            )
            .exists(),
            not_(historical),
        )
    )
    return count + (legacy or 0)


async def authorize_shared_action(session, context, node, binding, run_id, action, *, reserve=False, observation=False):
    """Recheck membership, scope, claim, revision, clock, spend, and shared limits."""
    inputs, marker = await shared_inputs(session, org_id=node.org_id, flow_id=node.flow_id, lock=reserve)
    if action not in CODE_ACTIONS or node.kind != "story":
        _refuse("shared_continuation_code_delivery_only", BlockCode.HUMAN_GATE_REQUIRED)
    if node.state not in {"running", "awaiting_merge"}:
        _refuse("continued_story_not_active")
    identity = context.identity
    from .plan_lineage import execution_plan_matches

    if (
        identity.org_id != node.org_id
        or identity.node_id != node.id
        or not await execution_plan_matches(
            session,
            org_id=node.org_id,
            flow_id=node.flow_id,
            node_id=node.id,
            version=identity.accepted_plan_version,
            current_version=inputs.plan_version,
        )
    ):
        _refuse("policy_changed")
    claim = await session.scalar(
        select(OrchestrationWorkClaim).where(
            OrchestrationWorkClaim.id == identity.claim_id,
            OrchestrationWorkClaim.org_id == node.org_id,
        )
    )
    if (
        claim is None
        or claim.state != "held"
        or claim.generation != identity.claim_generation
        or claim.owner_kind != "engine_flow"
        or claim.owner_ref != node.flow_id
        or claim.active_run_id != run_id
    ):
        _refuse("active_claim_changed", BlockCode.OWNERSHIP_LOST)
    if (
        binding.org_id != node.org_id
        or binding.node_id != node.id
        or binding.repo not in inputs.policy.repository_ids
        or binding.provider_repository_id != claim.provider_repository_id
    ):
        _refuse("binding_scope_changed")
    from .pr_bindings import binding_scope_matches

    if not binding_scope_matches(binding, node):
        _refuse("binding_scope_changed")
    meter = await read_flow_meter(org_id=node.org_id, flow_id=node.flow_id, policy=inputs.policy)
    if inputs.policy._budget_enforcement_enabled and (meter is None or meter.total_usd < Decimal(marker["prior_spend_usd"])):
        _refuse("budget_unavailable", BlockCode.BUDGET_EXHAUSTED)
    principal = inputs.policy.principal_id
    auth = await resolve_authorization_context(
        session,
        policy=inputs.policy,
        plan_version=inputs.plan_version,
        node=node,
        principal_user_id=principal,
        credential_scope=CredentialScope.USER_GRANTED,
        spend=SpendObservation(total_usd=meter.total_usd if meter else None),
        provider_repository_id=binding.provider_repository_id,
        expected_invocation_id=run_id,
    )
    used = await stage_attempts(session, org_id=node.org_id, node_id=node.id, action=action) - 1
    auth = replace(
        auth,
        observed_attempts=max(0, used),
        # Reading/reconciling an admitted execution does not start another worker.
        # Actual dispatch and replay retain the shared admission check below.
        observed_concurrency=0
        if observation and not reserve
        else await _active_count(session, org_id=node.org_id, flow_id=node.flow_id, exclude_run_id=run_id, initial_runs=marker.get("initial_runs")),
        work_owned_by_policy_flow=True,
    )
    flow = await session.get(OrchestrationFlow, node.flow_id)
    decision = authorize_action(
        auth,
        action,
        ResourceRef(
            repository_id=binding.repo,
            node_address=graph_address(node, flow_slug=flow.slug),
            org_id=node.org_id,
            aws_role_arn=marker["worker_role_arn"],
        ),
        inputs.plan_version,
    )
    if not decision.permitted:
        code = (
            BlockCode.BUDGET_EXHAUSTED if "spend" in decision.reason.value or "budget" in decision.reason.value else BlockCode.AUTHORITY_UNVERIFIABLE
        )
        _refuse(decision.reason.value, code)
    if reserve and inputs.policy._budget_enforcement_enabled:
        from .flow_budget import reserve_flow_admission

        await _release_finished_admissions(session, inputs.policy, node.flow_id, meter.total_usd)
        reservation = await reserve_flow_admission(
            org_id=node.org_id, flow_id=node.flow_id, policy=inputs.policy, settled_usd=meter.total_usd, node_id=node.id
        )
        if reservation is None or not reservation.admitted:
            _refuse("budget_unavailable" if reservation is None or reservation.degraded else "spend_limit_exceeded", BlockCode.BUDGET_EXHAUSTED)
    return inputs, principal, meter, auth


async def _release_finished_admissions(session, policy, flow_id, metered_usd):
    """Release stopped shared stories' redundant admission holds under the flow lock.

    The caller has already read the existing model meter successfully. That meter
    retains actual charges and outstanding provider reservations; it is never
    modified here. Require both engine-owned terminal state and authenticated
    terminal reports for the current attempt. A queued or active successor keeps
    the hold. Initial and continuation admissions share this flow lock, so a new
    worker cannot be admitted between this check and its replacement reservation.
    """
    from .flow_budget import release_flow_admission
    from .run_reports import OrchestrationRunReport

    nodes = list(
        await session.scalars(
            select(OrchestrationNode).where(
                OrchestrationNode.org_id == policy.org_id,
                OrchestrationNode.flow_id == flow_id,
                OrchestrationNode.kind == "story",
                OrchestrationNode.state.in_({"passed", "failed", "halted", "superseded"}),
                OrchestrationNode.attempts > 0,
            )
        )
    )
    if not nodes:
        return
    reports = list(
        await session.scalars(
            select(OrchestrationRunReport).where(
                OrchestrationRunReport.org_id == policy.org_id,
                OrchestrationRunReport.flow_id == flow_id,
                OrchestrationRunReport.node_id.in_([node.id for node in nodes]),
            )
        )
    )
    for stopped in nodes:
        current = [report for report in reports if report.node_id == stopped.id and report.attempt == stopped.attempts]
        if not current or any(
            not isinstance(report.terminal_receipt, dict)
            or report.terminal_receipt.get("contract_version") != 1
            or report.terminal_receipt.get("run_id") != report.run_id
            or report.terminal_receipt.get("attempt") != stopped.attempts
            or report.terminal_receipt.get("outcome") not in {"complete", "failed"}
            for report in current
        ):
            continue
        await release_flow_admission(org_id=policy.org_id, flow_id=flow_id, policy=policy, settled_usd=metered_usd, node_id=stopped.id)


async def authorize_shared_model(session, assignment):
    """Authenticate first, then call this for a live model request's assignment.

    Returns policy, principal, node, and flow for the existing model middleware's
    quote/reservation and usage attribution path. No provider credential is issued.
    """
    if assignment.terminal_receipt is not None:
        _refuse("run_already_finished")
    node = await session.get(OrchestrationNode, assignment.node_id)
    inputs, marker = await shared_inputs(session, org_id=assignment.org_id, flow_id=assignment.flow_id)
    if node is None or node.org_id != assignment.org_id or node.flow_id != assignment.flow_id or node.attempts != assignment.attempt:
        _refuse("report_scope_changed")
    flow = await session.get(OrchestrationFlow, node.flow_id)
    if node.state not in {"running", "awaiting_merge"} or flow is None or flow.state not in {"pending", "running"}:
        _refuse("continued_story_not_active")
    from .shared_cycle import validate_current_report_assignment

    execution, _ = await validate_current_report_assignment(session, assignment)
    if execution.status in {"concluded", "superseded"}:
        _refuse("current_execution_missing")
    action = {
        "developer": Action.DEVELOP,
        "reviewer": Action.REVIEW,
        "codex": Action.REVIEW,
        "codex-reviewer": Action.REVIEW,
        "agent-codex-reviewer": Action.REVIEW,
    }.get(assignment.persona)
    declared = assignment.dispatch_metadata.get("action")
    if declared in {Action.DEVELOP.value, Action.REPAIR.value, Action.REVIEW.value}:
        action = Action(declared)
    if action is None:
        _refuse("model_action_unverifiable")
    # A development request precedes PR binding, so it uses the authoritative
    # assignment's repository and node scope instead of inventing a PR binding.
    meter = await read_flow_meter(org_id=node.org_id, flow_id=node.flow_id, policy=inputs.policy)
    if inputs.policy._budget_enforcement_enabled and (meter is None or meter.total_usd < Decimal(marker["prior_spend_usd"])):
        _refuse("budget_unavailable", BlockCode.BUDGET_EXHAUSTED)
    auth = await resolve_authorization_context(
        session,
        policy=inputs.policy,
        plan_version=inputs.plan_version,
        node=node,
        principal_user_id=inputs.policy.principal_id,
        credential_scope=CredentialScope.USER_GRANTED,
        spend=SpendObservation(total_usd=meter.total_usd if meter else None),
        provider_repository_id=assignment.provider_repository_id,
        expected_invocation_id=assignment.run_id,
    )
    # The request belongs to an already admitted run, so it does not consume an
    # additional concurrency slot or attempt every time it calls a model.
    claim = await session.scalar(
        select(OrchestrationWorkClaim).where(
            OrchestrationWorkClaim.org_id == node.org_id,
            OrchestrationWorkClaim.id == execution.claim_id,
        )
    )
    if (
        claim is None
        or claim.state != "held"
        or claim.generation != execution.claim_generation
        or claim.owner_ref != node.flow_id
        or claim.owner_kind != "engine_flow"
        or claim.active_run_id != assignment.run_id
        or claim.provider_repository_id != assignment.provider_repository_id
    ):
        _refuse("active_claim_changed", BlockCode.OWNERSHIP_LOST)
    auth = replace(
        auth,
        observed_attempts=max(
            0,
            (
                node.attempts
                if action is Action.DEVELOP or (assignment.persona == "developer" and not assignment.dispatch_metadata.get("review_cycle_input"))
                else await stage_attempts(session, org_id=node.org_id, node_id=node.id, action=action)
            )
            - 1,
        ),
        observed_concurrency=await _active_count(
            session, org_id=node.org_id, flow_id=node.flow_id, exclude_run_id=assignment.run_id, initial_runs=marker.get("initial_runs")
        ),
        work_owned_by_policy_flow=True,
    )
    decision = authorize_action(
        auth,
        action,
        ResourceRef(
            repository_id=assignment.repo,
            node_address=graph_address(node, flow_slug=flow.slug),
            org_id=node.org_id,
            aws_role_arn=marker["worker_role_arn"],
        ),
        inputs.plan_version,
    )
    if not decision.permitted:
        _refuse(
            decision.reason.value,
            BlockCode.BUDGET_EXHAUSTED if "budget" in decision.reason.value or "spend" in decision.reason.value else BlockCode.AUTHORITY_UNVERIFIABLE,
        )
    return inputs.policy, inputs.policy.principal_id, node, flow


async def is_shared_continuation(session, *, org_id, flow_id) -> bool:
    plan = await session.scalar(
        select(OrchestrationAcceptedPlan).where(
            OrchestrationAcceptedPlan.org_id == org_id,
            OrchestrationAcceptedPlan.flow_id == flow_id,
            OrchestrationAcceptedPlan.superseded_at.is_(None),
        )
    )
    marker = (plan.plan_document or {}).get("execution_continuation") if plan else None
    return isinstance(marker, dict) and marker.get("mode") == "shared_worker_role"


async def authorize_shared_dispatch(
    session, *, node, principal_user_id, target_repository, provider_repository_id, expected_invocation_id, action_override=None
):
    """Initial shared dispatch uses the same rule, meter and pending SQL work claim."""
    from .execution_policy import Decision, DenyReason
    from .policy_admission import resolve_node_action
    from .review_cycle import CycleBlockedError

    try:
        inputs, marker = await shared_inputs(session, org_id=node.org_id, flow_id=node.flow_id, lock=True)
    except CycleBlockedError as exc:
        reason = next((r for r in DenyReason if r.value == str(exc)), DenyReason.CREDENTIAL_SCOPE_UNAVAILABLE)
        return Decision.block(reason, str(exc))
    action = action_override or resolve_node_action(node)
    if principal_user_id != inputs.policy.principal_id:
        return Decision.block(DenyReason.MEMBERSHIP_REVOKED, "Dispatch principal differs from the accepted policy owner.")
    if action not in CODE_ACTIONS or node.kind != "story" or node.state != "ready":
        return Decision.block(DenyReason.ACTION_NOT_PERMITTED, "Only ready code stories may enter shared-worker delivery.")
    claim = await session.scalar(
        select(OrchestrationWorkClaim).where(
            OrchestrationWorkClaim.org_id == node.org_id,
            OrchestrationWorkClaim.provider_repository_id == provider_repository_id,
            OrchestrationWorkClaim.owner_ref == node.flow_id,
            OrchestrationWorkClaim.owner_kind == "engine_flow",
            OrchestrationWorkClaim.active_run_id == expected_invocation_id,
            OrchestrationWorkClaim.state == "held",
        )
    )
    if claim is None or str(claim.issue_number) != str(node.issue_ref).lstrip("#"):
        return Decision.block(DenyReason.WORK_NOT_OWNED, "Shared dispatch requires its current server-assigned work claim.")
    meter = await read_flow_meter(org_id=node.org_id, flow_id=node.flow_id, policy=inputs.policy)
    if inputs.policy._budget_enforcement_enabled and (meter is None or meter.total_usd < Decimal(marker["prior_spend_usd"])):
        return Decision.block(DenyReason.BUDGET_UNAVAILABLE, "The accepted shared model budget is unavailable; it cannot be reset.")
    auth = await resolve_authorization_context(
        session,
        policy=inputs.policy,
        plan_version=inputs.plan_version,
        node=node,
        principal_user_id=principal_user_id,
        credential_scope=CredentialScope.USER_GRANTED,
        spend=SpendObservation(total_usd=meter.total_usd if meter else None),
        provider_repository_id=provider_repository_id,
        expected_invocation_id=expected_invocation_id,
    )
    auth = replace(
        auth,
        work_owned_by_policy_flow=True,
        observed_concurrency=await _active_count(
            session, org_id=node.org_id, flow_id=node.flow_id, exclude_run_id=expected_invocation_id, initial_runs=marker.get("initial_runs")
        ),
    )
    flow = await session.get(OrchestrationFlow, node.flow_id)
    decision = authorize_action(
        auth,
        action,
        ResourceRef(
            repository_id=target_repository,
            node_address=graph_address(node, flow_slug=flow.slug),
            org_id=node.org_id,
            aws_role_arn=marker["worker_role_arn"],
        ),
        inputs.plan_version,
    )
    if not decision.permitted or not inputs.policy._budget_enforcement_enabled:
        return decision
    from .flow_budget import reserve_flow_admission

    await _release_finished_admissions(session, inputs.policy, node.flow_id, meter.total_usd)
    reservation = await reserve_flow_admission(
        org_id=node.org_id, flow_id=node.flow_id, policy=inputs.policy, settled_usd=meter.total_usd, node_id=node.id
    )
    if reservation is None or not reservation.admitted:
        return Decision.block(
            DenyReason.BUDGET_UNAVAILABLE if reservation is None or reservation.degraded else DenyReason.SPEND_LIMIT_EXCEEDED,
            "The flow allowance cannot admit another worker.",
        )
    return decision
