"""Owner- or policy-authorized review of retained work after a positively exited run.

Recovery records the missing/failed receipt honestly. It never fabricates a
worker completion or review, resets attempts, or repeats development.
"""

import asyncio
import json
import logging
import time
import traceback
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from .continuation import digest
from .execution_policy import Action
from .execution_runner import RunnerContext
from .models import DecisionKind, OrchestrationDecision, OrchestrationNode, OrchestrationWorkClaim
from .pr_bindings import active_binding_for_node, binding_scope_matches
from .review_cycle import CycleBlockedError
from .run_reports import OrchestrationRunReport
from .shared_cycle import shared_marker, validate_current_report_assignment
from .shared_policy import authorize_shared_action, shared_inputs

KIND = "review_recovery_requested"
CONTRACT = "shared-review-recovery/v1"
RECOVERY_ACTOR = "system:stalled-story-review"


class ReviewRecoveryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_attempt: int = Field(strict=True, ge=1)
    expected_plan_version: int = Field(strict=True, ge=1)
    expected_run_id: str = Field(min_length=1, max_length=255)
    expected_head_sha: str = Field(pattern=r"^[a-f0-9]{40}$")
    reason: str = Field(min_length=10, max_length=2000)
    expected_snapshot: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


async def exited_run(run_id, org_id, resolver=None):
    from .controls import get_run_binding_resolver
    from .work_claims import compute_liveness

    resolver = resolver or await get_run_binding_resolver()
    # Lifecycle is mutable; the budget resolver's day-long identity cache cannot
    # establish a current exit. Read the registry consistently at acceptance.
    run = await resolver.read_current(run_id)
    if (
        not run
        or run.get("tenant_id") != org_id
        or compute_liveness(run.get("status"), str(run.get("arrived_at") or ""), datetime.now(UTC), run.get("status_updated_at")) != "exited"
    ):
        raise CycleBlockedError("prior_worker_active_or_unverified")
    return {key: str(run[key]) if run.get(key) is not None else None for key in ("status", "arrived_at", "status_updated_at")}


def recorded_worker_exited(data):
    """Validate the server-observed exit retained in the attributed decision.

    The scheduler consumes this durable evidence with the unchanged report,
    claim, plan and PR fences. It does not need a second registry-read grant.
    A new execution requires a new run identity; this authorizes only replacement
    of the exact exited identity named by the decision.
    """
    from .work_claims import compute_liveness

    observed = data.get("worker_exit")
    return (
        isinstance(observed, dict)
        and compute_liveness(observed.get("status"), str(observed.get("arrived_at") or ""), datetime.now(UTC), observed.get("status_updated_at"))
        == "exited"
    )


async def prepare_recovery(session, *, org_id, node_id, actor_id, actor_role, request, resolver=None, autonomous=False):
    from .pr_identity import resolve_pr_identity
    from .review_cycle import PHASES

    if autonomous:
        if actor_id != RECOVERY_ACTOR or actor_role != "engine":
            raise CycleBlockedError("recovery_actor_invalid")
    elif not actor_id or actor_role not in {"owner", "org_admin", "platform_admin"}:
        raise CycleBlockedError("human_plan_approver_required")
    node = await session.scalar(select(OrchestrationNode).where(OrchestrationNode.id == node_id, OrchestrationNode.org_id == org_id))
    if node is None or node.kind != "story" or node.state not in {"running", "awaiting_merge", "failed"}:
        raise CycleBlockedError("review_recovery_story_not_current")
    plan, _ = await shared_marker(session, org_id=org_id, flow_id=node.flow_id)
    inputs, _ = await shared_inputs(session, org_id=org_id, flow_id=node.flow_id)
    if plan.version != request.expected_plan_version or node.attempts != request.expected_attempt or inputs.policy.expires_at <= datetime.now(UTC):
        raise CycleBlockedError("accepted_policy_changed_or_expired")
    report = await session.get(OrchestrationRunReport, request.expected_run_id)
    if report is None or report.org_id != org_id or report.node_id != node_id or report.flow_id != node.flow_id or report.attempt != node.attempts:
        raise CycleBlockedError("report_assignment_changed")
    execution, identity = await validate_current_report_assignment(session, report)
    if execution.phase not in PHASES or execution.status in {"concluded", "superseded"}:
        raise CycleBlockedError("review_recovery_phase_changed")
    if execution.pending_action_key:
        raise CycleBlockedError("pending_effect_requires_reconciliation")
    if autonomous:
        await require_autonomous_recovery(session, node, report, execution, inputs)
    binding = await active_binding_for_node(session, org_id=org_id, node_id=node_id, attempt=node.attempts)
    if binding is None or not binding_scope_matches(binding, node):
        raise CycleBlockedError("bound_delivery_missing")
    # Evaluate the proposed restoration in preview without writing graph state.
    proposed = SimpleNamespace(**{k: v for k, v in node.__dict__.items() if not k.startswith("_")})
    proposed.state = "running"
    context = RunnerContext(identity, execution, datetime.now(UTC))
    await authorize_shared_action(session, context, proposed, binding, report.run_id, Action.REVIEW, observation=True)
    exit_evidence = await exited_run(report.run_id, org_id, resolver or (report_exit_resolver(report) if autonomous else None))
    remote = await resolve_pr_identity(org_id=org_id, installation_id=binding.installation_id, repo=binding.repo, pr_number=binding.pr_number)
    if (
        remote.head_sha != request.expected_head_sha
        or remote.provider_repository_id != binding.provider_repository_id
        or remote.provider_pr_node_id != binding.provider_pr_node_id
    ):
        raise CycleBlockedError("current_pr_changed")
    checkpoint_key = str(uuid5(NAMESPACE_URL, f"stalled-pr:{org_id}:{node.id}:{report.run_id}"))
    checkpoint_intent = await session.get(OrchestrationDecision, checkpoint_key)
    data = {
        "recovery_source": "checkpoint"
        if (
            checkpoint_intent is not None
            and checkpoint_intent.org_id == org_id
            and checkpoint_intent.node_id == node.id
            and checkpoint_intent.kind == "recovery_pr_prepared"
            and checkpoint_intent.actor_id == RECOVERY_ACTOR
            and checkpoint_intent.actor_kind == "service"
            and json.loads(checkpoint_intent.reason).get("checkpoint", {}).get("head_sha") == remote.head_sha
        )
        else "existing_pr",
        "contract": CONTRACT,
        "org_id": org_id,
        "flow_id": node.flow_id,
        "node_id": node_id,
        "attempt": node.attempts,
        "plan_version": plan.version,
        "plan_hash": plan.plan_hash,
        "execution_id": execution.id,
        "claim_id": identity.claim_id,
        "claim_generation": identity.claim_generation,
        "prior_run_id": report.run_id,
        "binding_id": binding.id,
        "binding_revision": binding.revision,
        "head_sha": remote.head_sha,
        "node_state": node.state,
        "execution_attempts": execution.attempts,
        "autonomous": autonomous,
        "worker_receipt_digest": digest(report.worker_receipt) if report.worker_receipt else None,
        "worker_exit": exit_evidence,
        "terminal_receipt_digest": digest(report.terminal_receipt) if report.terminal_receipt else None,
        "review_receipt_digest": digest(report.review_receipt) if report.review_receipt else None,
    }
    return data, node, report, execution


async def request_review_recovery(session, *, org_id, node_id, actor_id, actor_role, request, accept=False, resolver=None, autonomous=False):
    if autonomous:
        if actor_id != RECOVERY_ACTOR or actor_role != "engine":
            raise CycleBlockedError("recovery_actor_invalid")
    elif not actor_id or actor_role not in {"owner", "org_admin", "platform_admin"}:
        raise CycleBlockedError("human_plan_approver_required")
    actor_kind = "service" if autonomous else "human"
    decision_id = str(
        uuid5(NAMESPACE_URL, CONTRACT + ":" + digest({"org": org_id, "node": node_id, "actor": actor_id, "snapshot": request.expected_snapshot}))
    )
    if accept:
        if not request.expected_snapshot:
            raise CycleBlockedError("preview_snapshot_required")
        old = await session.get(OrchestrationDecision, decision_id)
        if old is not None:
            if old.org_id != org_id or old.node_id != node_id or old.actor_id != actor_id or old.actor_kind != actor_kind or old.kind != KIND:
                raise CycleBlockedError("recovery_receipt_conflict")
            return {"accepted": True, "created": False, "decision_id": old.id}
    data, node, report, execution = await prepare_recovery(
        session, org_id=org_id, node_id=node_id, actor_id=actor_id, actor_role=actor_role, request=request, resolver=resolver, autonomous=autonomous
    )
    snapshot = digest(data)
    preview = {"snapshot": snapshot, **data, "attempts_preserved": True, "receipts_preserved": True, "next_action": "fresh_codex_review"}
    if not accept:
        return preview
    if snapshot != request.expected_snapshot:
        raise CycleBlockedError("recovery_preview_changed")
    # Serialize with dispatch, handoff and report ingestion after provider reads.
    from .execution_state import OutcomeKind, PhaseAdvance
    from .execution_store import advance_execution, load_execution
    from .review_cycle import PHASES

    if autonomous:
        from .flow_execution import flow_is_paused

        if await flow_is_paused(session, org_id=org_id, flow_id=node.flow_id, lock=True):
            raise CycleBlockedError("flow_paused")
    await session.scalar(select(OrchestrationNode).where(OrchestrationNode.id == node_id).with_for_update().execution_options(populate_existing=True))
    raced = await session.get(OrchestrationDecision, decision_id, populate_existing=True)
    if raced is not None:
        return {"accepted": True, "created": False, "decision_id": raced.id}
    _, identity = await validate_current_report_assignment(session, report)
    current = await load_execution(session, identity=identity, for_update=True)
    claim = await session.scalar(
        select(OrchestrationWorkClaim)
        .where(OrchestrationWorkClaim.id == identity.claim_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    await session.refresh(node)
    await session.refresh(report)
    if (
        current is None
        or current.kind is not OutcomeKind.APPLIED
        or current.record.pending_action_key
        or current.record.attempts != data["execution_attempts"]
        or current.record.phase not in PHASES
        or claim is None
        or claim.active_run_id != report.run_id
        or node.state != data["node_state"]
    ):
        raise CycleBlockedError("recovery_assignment_changed")
    if (digest(report.terminal_receipt) if report.terminal_receipt else None) != data["terminal_receipt_digest"] or (
        digest(report.review_receipt) if report.review_receipt else None
    ) != data["review_receipt_digest"]:
        raise CycleBlockedError("recovery_receipt_changed")
    from .models import OrchestrationPullRequestBinding

    binding = await session.get(OrchestrationPullRequestBinding, data["binding_id"], populate_existing=True, with_for_update=True)
    if binding is None or binding.state != "active" or binding.revision != data["binding_revision"] or not binding_scope_matches(binding, node):
        raise CycleBlockedError("recovery_binding_changed")
    if (digest(report.worker_receipt) if report.worker_receipt else None) != data["worker_receipt_digest"]:
        raise CycleBlockedError("recovery_receipt_changed")
    if autonomous:
        inputs, _ = await shared_inputs(session, org_id=org_id, flow_id=node.flow_id)
        await require_autonomous_recovery(session, node, report, current.record, inputs)
    # A separate recovery decision authorizes another review, never approval or a
    # successful worker terminal. Old credentials are fenced when the successor
    # acquires the current claim through the ordinary dispatch transaction.
    session.add(
        OrchestrationDecision(
            id=decision_id,
            org_id=org_id,
            flow_id=node.flow_id,
            node_id=node.id,
            kind=KIND,
            actor_id=actor_id,
            actor_role=actor_role,
            actor_kind=actor_kind,
            reason=json.dumps({**data, "snapshot": snapshot, "reason": request.reason}),
        )
    )
    if node.state == "failed":
        from .state import ActorKind, NodeState, transition

        moved = transition(
            node.state, NodeState.RUNNING, actor_kind=ActorKind(actor_kind), reason=request.reason, stalled_review_authorized=autonomous
        )
        if not moved.allowed:
            raise CycleBlockedError("recovery_transition_refused")
        node.state = moved.new_state.value
        session.add(
            OrchestrationDecision(
                org_id=org_id,
                flow_id=node.flow_id,
                node_id=node.id,
                kind=DecisionKind.NODE_RESUMED.value,
                actor_kind=actor_kind,
                actor_id=actor_id,
                actor_role=actor_role,
                from_state="failed",
                to_state="running",
                reason=json.dumps({"review_recovery_decision_id": decision_id, "reason": request.reason}),
            )
        )
    updated = await advance_execution(
        session,
        identity=identity,
        advance=PhaseAdvance(
            phase=current.record.phase, status=current.record.status, expected_revision=current.record.revision, next_check_at=datetime.now(UTC)
        ),
    )
    if updated.kind is not OutcomeKind.APPLIED:
        raise CycleBlockedError("recovery_assignment_changed")
    await session.flush()
    return {"accepted": True, "created": True, "decision_id": decision_id, **preview}


async def verify_recovery_decision(session, *, decision_id, context, node, binding, prior_run_id, head_sha):
    row = await session.get(OrchestrationDecision, decision_id)
    data = json.loads(row.reason) if row else {}
    if binding is None or node is None:
        raise CycleBlockedError("recovery_authority_changed")
    plan, _ = await shared_marker(session, org_id=node.org_id, flow_id=node.flow_id)
    from .plan_lineage import ancestor_plan, receipt_plan

    plan = await receipt_plan(session, plan, data, node_id=node.id)
    if plan is None or await ancestor_plan(session, plan, context.identity.accepted_plan_version, node_id=node.id) is None:
        raise CycleBlockedError("recovery_authority_changed")
    expected = {
        "contract": CONTRACT,
        "org_id": node.org_id,
        "flow_id": node.flow_id,
        "node_id": node.id,
        "attempt": node.attempts,
        "plan_version": plan.version,
        "plan_hash": plan.plan_hash,
        "execution_id": context.execution.id,
        "claim_id": context.identity.claim_id,
        "claim_generation": context.identity.claim_generation,
        "prior_run_id": prior_run_id,
        "binding_id": binding.id,
        "binding_revision": binding.revision,
        "head_sha": head_sha,
    }
    if (
        row is None
        or row.org_id != node.org_id
        or row.node_id != node.id
        or row.flow_id != node.flow_id
        or row.kind != KIND
        or not recovery_actor_matches(row, data)
        or any(data.get(k) != v for k, v in expected.items())
        or not recorded_worker_exited(data)
    ):
        raise CycleBlockedError("recovery_authority_changed")
    report = await session.get(OrchestrationRunReport, prior_run_id)
    if report is None or any(
        (digest(getattr(report, key)) if getattr(report, key) else None) != data.get(key + "_digest")
        for key in ("worker_receipt", "terminal_receipt", "review_receipt")
    ):
        raise CycleBlockedError("recovery_receipt_changed")
    return data


async def recovery_snapshot(session, context, node, binding, services, dispatches):
    from .dispatch_pass import attempt_run_id
    from .review_cycle_dispatch import continuation_run_id, current_author_run

    active = continuation_run_id(dispatches[-1].operation_key) if dispatches else attempt_run_id(node.id, node.attempts)
    rows = list(
        (
            await session.scalars(
                select(OrchestrationDecision)
                .where(OrchestrationDecision.org_id == node.org_id, OrchestrationDecision.node_id == node.id, OrchestrationDecision.kind == KIND)
                .order_by(OrchestrationDecision.created_at.desc())
                .limit(100)
            )
        ).all()
    )
    selected = next((r for r in rows if json.loads(r.reason).get("prior_run_id") == active), None)
    if selected is None:
        return None
    head = await services.head(binding)
    recovery = await verify_recovery_decision(
        session, decision_id=selected.id, context=context, node=node, binding=binding, prior_run_id=active, head_sha=head
    )
    _, _, inputs, _, meter = await services.authorize(session, context, node, binding, active, Action.REVIEW)
    return {
        "active_run_id": active,
        "head_sha": head,
        "remaining_attempts": inputs.policy.limits.max_attempts_per_node - node.attempts - context.execution.attempts,
        "remaining_spend_usd": str(inputs.policy.limits.max_spend_usd - meter.total_usd)
        if inputs.policy._budget_enforcement_enabled and meter
        else None,
        "binding_id": binding.id,
        "binding_revision": binding.revision,
        "repo": binding.repo,
        "pr_number": binding.pr_number,
        "provider_repository_id": binding.provider_repository_id,
        "provider_pr_node_id": binding.provider_pr_node_id,
        "accepted_scope": binding.accepted_scope,
        "sequence": len(dispatches) + 1,
        "next_action": Action.REVIEW.value,
        "author_run_id": await current_author_run(session, node=node, default=attempt_run_id(node.id, node.attempts)),
        "recovery_decision_id": selected.id,
        "recovery": {"source": recovery.get("recovery_source", "existing_pr"), "prior_run_id": active, "checkpoint_sha": head},
    }


async def pending_recovery_for_report(session, report):
    """Local authority to wait for a fresh review, never completion evidence."""
    rows = list(
        (
            await session.scalars(
                select(OrchestrationDecision)
                .where(
                    OrchestrationDecision.org_id == report.org_id, OrchestrationDecision.node_id == report.node_id, OrchestrationDecision.kind == KIND
                )
                .order_by(OrchestrationDecision.created_at.desc())
                .limit(100)
            )
        ).all()
    )
    selected = next((row for row in rows if json.loads(row.reason).get("prior_run_id") == report.run_id), None)
    if selected is None:
        return None
    execution, identity = await validate_current_report_assignment(session, report)
    node = await session.get(OrchestrationNode, report.node_id)
    binding = await active_binding_for_node(session, org_id=report.org_id, node_id=report.node_id, attempt=report.attempt)
    await verify_recovery_decision(
        session,
        decision_id=selected.id,
        context=RunnerContext(identity, execution, datetime.now(UTC)),
        node=node,
        binding=binding,
        prior_run_id=report.run_id,
        head_sha=json.loads(selected.reason)["head_sha"],
    )
    return selected


async def recovered_worker_exit(session, report, claim):
    """A committed, attributed recovery proves the old worker no longer uses a slot.

    Moving a claim alone is insufficient: the exit observation, unchanged old
    report and actual successor dispatch must all be retained in the ledger.
    """
    from src.activity.liveness import compute_liveness

    from .models import OrchestrationAction
    from .review_cycle_dispatch import ACTOR, DISPATCH_KIND, continuation_run_id, receipt_id

    if claim is None or claim.active_run_id == report.run_id:
        return False
    rows = list(
        (
            await session.scalars(
                select(OrchestrationDecision)
                .where(
                    OrchestrationDecision.org_id == report.org_id,
                    OrchestrationDecision.node_id == report.node_id,
                    OrchestrationDecision.kind == KIND,
                )
                .limit(100)
            )
        ).all()
    )
    for row in rows:
        data = json.loads(row.reason)
        if not recovery_actor_matches(row, data):
            continue
        expected = {
            "contract": CONTRACT,
            "org_id": report.org_id,
            "flow_id": report.flow_id,
            "node_id": report.node_id,
            "attempt": report.attempt,
            "prior_run_id": report.run_id,
            "claim_id": claim.id,
            "claim_generation": claim.generation,
        }
        if any(data.get(k) != v for k, v in expected.items()):
            continue
        exit_evidence = data.get("worker_exit") or {}
        if (
            compute_liveness(
                exit_evidence.get("status"), exit_evidence.get("arrived_at") or "", datetime.now(UTC), exit_evidence.get("status_updated_at")
            )
            != "exited"
        ):
            continue
        if any(
            (digest(getattr(report, key)) if getattr(report, key) else None) != data.get(key + "_digest")
            for key in ("worker_receipt", "terminal_receipt", "review_receipt")
        ):
            continue
        actions = list(
            (
                await session.scalars(
                    select(OrchestrationAction)
                    .where(
                        OrchestrationAction.org_id == report.org_id,
                        OrchestrationAction.execution_id == data["execution_id"],
                        OrchestrationAction.kind == DISPATCH_KIND,
                    )
                    .limit(100)
                )
            ).all()
        )
        for action in actions:
            if (
                action.detail.get("recovery_decision_id") != row.id
                or action.detail.get("active_run_id") != report.run_id
                or action.detail.get("action") != "review"
            ):
                continue
            receipt = await session.get(OrchestrationDecision, receipt_id(action.operation_key))
            saved = json.loads(receipt.reason) if receipt else {}
            if (
                receipt
                and receipt.org_id == report.org_id
                and receipt.node_id == report.node_id
                and receipt.actor_id == ACTOR
                and receipt.actor_kind == "service"
                and receipt.kind == "agent_dispatched"
                and saved.get("authority_mode") == "shared_worker_role"
                and saved.get("run_id") == continuation_run_id(action.operation_key)
            ):
                return True
    return False


def recovery_actor_matches(row, data):
    if data.get("autonomous") is True:
        return row.actor_kind == "service" and row.actor_id == RECOVERY_ACTOR and row.actor_role == "engine"
    return row.actor_kind == "human" and bool(row.actor_id) and row.actor_role in {"owner", "org_admin", "platform_admin"}


async def require_autonomous_recovery(session, node, report, execution, inputs):
    """Only timed-out shared stories with policy headroom enter autonomous review."""
    from .flow_execution import flow_is_paused

    if node.state != "failed" or node.kind != "story":
        raise CycleBlockedError("not_a_stalled_story")
    if await flow_is_paused(session, org_id=node.org_id, flow_id=node.flow_id):
        raise CycleBlockedError("flow_paused")
    last = await session.scalar(
        select(OrchestrationDecision)
        .where(
            OrchestrationDecision.org_id == node.org_id,
            OrchestrationDecision.node_id == node.id,
            OrchestrationDecision.to_state.is_not(None),
            OrchestrationDecision.rejection_reason.is_(None),
        )
        .order_by(OrchestrationDecision.created_at.desc(), OrchestrationDecision.id.desc())
        .limit(1)
    )
    if last is None or last.kind != DecisionKind.NODE_STALLED.value or last.to_state != "failed":
        raise CycleBlockedError("not_an_engine_stall")
    if execution.pending_action_key:
        raise CycleBlockedError("pending_effect_requires_reconciliation")
    if node.attempts + execution.attempts >= inputs.policy.limits.max_attempts_per_node:
        raise CycleBlockedError("continuation_attempts_exhausted")
    if not inputs.policy.permits(Action.REVIEW):
        raise CycleBlockedError("review_not_authorized")
    if inputs.policy.expires_at <= datetime.now(UTC):
        raise CycleBlockedError("policy_expired")
    # An explicit reviewer blocker must not be turned into another paid review.
    if report.review_receipt:
        raise CycleBlockedError("reviewer_result_requires_reconciliation")


async def recover_stalled_stories(factory, *, resolver=None):
    """Bounded tick pass; record recovery, then let the existing runner dispatch."""
    from .execution_runner import RunnerConfig
    from .flow_execution import flow_is_paused
    from .models import OrchestrationFlow
    from .pr_identity import resolve_pr_identity

    if not RunnerConfig.from_env().enabled:
        return 0
    async with factory() as session:
        node_ids = list(
            (
                await session.scalars(
                    select(OrchestrationNode.id)
                    .join(OrchestrationFlow, OrchestrationFlow.id == OrchestrationNode.flow_id)
                    .where(
                        OrchestrationFlow.execution_paused.is_(False),
                        OrchestrationNode.kind == "story",
                        OrchestrationNode.state == "failed",
                    )
                    # Sample so permanently blocked stories cannot monopolize the pass.
                    .order_by(func.random())
                    .limit(100)
                )
            ).all()
        )
    recovered = 0
    deadline = time.monotonic() + 30
    for node_id in node_ids:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            # Cancellation rolls back local work; durable PR intents reconcile
            # any provider mutation whose response was lost to this deadline.
            async with asyncio.timeout(remaining), factory() as session:
                node = await session.get(OrchestrationNode, node_id)
                if node is None or await flow_is_paused(session, org_id=node.org_id, flow_id=node.flow_id):
                    continue
                plan, _ = await shared_marker(session, org_id=node.org_id, flow_id=node.flow_id)
                claim = await session.scalar(
                    select(OrchestrationWorkClaim).where(
                        OrchestrationWorkClaim.org_id == node.org_id,
                        OrchestrationWorkClaim.owner_ref == node.flow_id,
                        OrchestrationWorkClaim.owner_kind == "engine_flow",
                        OrchestrationWorkClaim.state == "held",
                        OrchestrationWorkClaim.issue_number == int(str(node.issue_ref).lstrip("#")),
                    )
                )
                if claim is None:
                    continue
                report = await session.get(OrchestrationRunReport, claim.active_run_id)
                if report is None or report.node_id != node.id or report.attempt != node.attempts:
                    continue
                execution, identity = await validate_current_report_assignment(session, report)
                inputs, _ = await shared_inputs(session, org_id=node.org_id, flow_id=node.flow_id)
                await require_autonomous_recovery(session, node, report, execution, inputs)
                binding = await active_binding_for_node(session, org_id=node.org_id, node_id=node.id, attempt=node.attempts)
                if binding is None:
                    from .recovery_checkpoint import ensure_checkpoint_pr

                    binding = await ensure_checkpoint_pr(session, node=node, report=report, execution=execution, identity=identity)
                remote = await resolve_pr_identity(
                    org_id=node.org_id, installation_id=binding.installation_id, repo=binding.repo, pr_number=binding.pr_number
                )
                request = ReviewRecoveryRequest(
                    expected_attempt=node.attempts,
                    expected_plan_version=plan.version,
                    expected_run_id=report.run_id,
                    expected_head_sha=remote.head_sha,
                    reason="Policy-authorized review of retained work after an engine-recorded stall.",
                )
                arguments = dict(
                    org_id=node.org_id, node_id=node.id, actor_id=RECOVERY_ACTOR, actor_role="engine", resolver=resolver, autonomous=True
                )
                preview = await request_review_recovery(session, request=request, **arguments)
                request.expected_snapshot = preview["snapshot"]
                await request_review_recovery(session, request=request, accept=True, **arguments)
                await session.commit()
                recovered += 1
        except Exception as error:
            # A provider or policy failure never disables the other flows' tick.
            # Store only a bounded code, never provider exception text/credentials.
            from httpx import HTTPStatusError

            code = error.reason if isinstance(error, CycleBlockedError) else "recovery_evidence_unavailable"
            if isinstance(error, HTTPStatusError):
                code = f"recovery_provider_http_{error.response.status_code}"
            frames = traceback.extract_tb(error.__traceback__)
            location = f"{frames[-1].name}:{frames[-1].lineno}" if frames else "unknown"
            # Lambda installs a WARNING root handler. Keep the failure visible
            # without printing provider bodies, request headers, or exception text.
            logging.getLogger(__name__).warning(
                "stalled review recovery blocked node=%s code=%s error_type=%s location=%s",
                node_id,
                code,
                type(error).__name__,
                location,
            )
            async with factory() as session:
                node = await session.get(OrchestrationNode, node_id)
                if node is not None:
                    key = str(uuid5(NAMESPACE_URL, f"recovery-block:{node.org_id}:{node.id}:{node.attempts}:{code}"))
                    if await session.get(OrchestrationDecision, key) is None:
                        session.add(
                            OrchestrationDecision(
                                id=key,
                                org_id=node.org_id,
                                flow_id=node.flow_id,
                                node_id=node.id,
                                kind="stalled_review_blocked",
                                actor_kind="service",
                                actor_id=RECOVERY_ACTOR,
                                actor_role="engine",
                                reason=code,
                            )
                        )
                        await session.commit()
    return recovered


def report_exit_resolver(report):
    """Read the exact dispatched registry key, without a scan or identity cache."""
    from .run_store import EngineRunStore

    async def read_current(run_id):
        arrived = report.dispatch_metadata.get("arrived_at")
        if run_id != report.run_id or not arrived:
            raise CycleBlockedError("recovery_registry_key_missing")
        response = await asyncio.to_thread(
            EngineRunStore.from_env().table.get_item, Key={"event_id": run_id, "arrived_at": arrived}, ConsistentRead=True
        )
        row = response.get("Item")
        if (
            not row
            or row.get("tenant_id") != report.org_id
            or row.get("event_id") != run_id
            or row.get("engine_node_id") != report.node_id
            or row.get("engine_attempt") != report.attempt
        ):
            raise CycleBlockedError("recovery_registry_binding_changed")
        return row

    return SimpleNamespace(read_current=read_current)
