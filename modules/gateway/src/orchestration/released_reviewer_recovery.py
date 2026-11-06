"""Restore a prematurely released retained reviewer on the same execution.

Explicit owner preview/accept preserves attempts, failures and spend (#8142, #8063).
"""

import json
from dataclasses import replace
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select

from .continuation import digest
from .execution_policy import Action
from .execution_runner import RunnerContext
from .execution_state import ExecutionPhase, ExecutionStatus, OutcomeKind, PhaseAdvance
from .execution_store import advance_execution, load_execution
from .handoff import identity_for_attempt
from .models import (
    OrchestrationAcceptedPlan,
    OrchestrationDecision,
    OrchestrationNode,
    OrchestrationWorkClaim,
)
from .pr_bindings import active_binding_for_node, binding_scope_matches
from .protected_developer_recovery import current_policy, reservation_id
from .protected_review_recovery import require
from .released_reviewer_evidence import historical_authority, receipt_data
from .retained_work_recovery import require_budget
from .review_cycle import CycleBlockedError
from .shared_window import policy_owner_matches
from .stage_attempts import stage_attempts

KIND = "released_reviewer_recovery"
CONTRACT = "protected-released-reviewer/v1"
WINDOW_SECONDS = 900
RENEWAL_KIND = "released_reviewer_renewal"
RENEWAL_CONTRACT = "protected-released-reviewer-renewal/v1"


def require_unexpired(data, *, reconciliation_required=False):
    reason = "released_reviewer_expired_reconciliation_required" if reconciliation_required else "released_reviewer_expired_renew_unstarted"
    try:
        require(datetime.fromisoformat(data["expires_at"]) > datetime.now(UTC), reason)
    except (TypeError, ValueError, KeyError):
        raise CycleBlockedError("released_reviewer_receipt_malformed") from None


async def effective_receipt(session, row, data):
    """A separate exact renewal may extend publication authority, never history."""
    rows = await session.scalars(
        select(OrchestrationDecision)
        .where(
            OrchestrationDecision.org_id == row.org_id,
            OrchestrationDecision.node_id == row.node_id,
            OrchestrationDecision.kind == RENEWAL_KIND,
        )
        .order_by(OrchestrationDecision.created_at.desc(), OrchestrationDecision.id.desc())
        .limit(101)
    )
    rows = list(rows)
    require(len(rows) <= 100, "released_reviewer_renewal_history_limit")
    for renewal in rows:
        saved = receipt_data(renewal)
        if saved.get("recovery_decision_id") != row.id:
            continue
        require(
            renewal.flow_id == row.flow_id
            and renewal.actor_kind == "human"
            and renewal.actor_id == row.actor_id
            and renewal.actor_role in {"owner", "org_admin", "platform_admin"}
            and saved.get("contract") == RENEWAL_CONTRACT
            and saved.get("recovery_receipt_digest") == digest(data),
            "released_reviewer_renewal_authority_changed",
        )
        return dict(data, expires_at=saved["expires_at"], renewal_decision_id=renewal.id)
    return data


async def current(session, *, org_id, node_id, request, actor_id, services, lock=False):
    query = select(OrchestrationNode).where(OrchestrationNode.org_id == org_id, OrchestrationNode.id == node_id)
    node = await session.scalar(query.execution_options(populate_existing=True).with_for_update() if lock else query)
    require(node is not None and node.kind == "story" and node.state == "running", "review_recovery_story_not_current")
    require(node.attempts == request.expected_attempt, "report_assignment_changed")
    identity = await identity_for_attempt(session, org_id=org_id, node_id=node.id, attempt=node.attempts)
    require(identity is not None, "current_execution_changed")
    loaded = await load_execution(session, identity=identity, for_update=lock, released_failure_run_id=request.expected_run_id)
    require(loaded is not None and loaded.kind is OutcomeKind.APPLIED and loaded.record is not None, "current_execution_changed")
    execution = loaded.record
    require(
        execution.phase in {"awaiting_review", "repairing"}
        and execution.status not in {"concluded", "superseded"}
        and execution.pending_action_key is None
        and execution.handoff_receipt_ref is None,
        "released_reviewer_execution_changed",
    )
    claim = await session.get(OrchestrationWorkClaim, identity.claim_id, populate_existing=True)
    require(
        claim is not None
        and claim.state == "released"
        and claim.release_reason == "failed"
        and claim.generation == identity.claim_generation
        and claim.active_run_id is None
        and claim.claim_event_id == request.expected_run_id
        and claim.owner_kind == "engine_flow"
        and claim.owner_ref == node.flow_id
        and claim.released_at is not None,
        "released_reviewer_claim_changed",
    )
    binding = await active_binding_for_node(session, org_id=org_id, node_id=node.id, attempt=node.attempts)
    require(
        binding is not None and binding_scope_matches(binding, node) and binding.registered_by_kind == "human" and binding.recovery_reason,
        "operator_bound_delivery_required",
    )
    require(
        claim.provider_repository_id == binding.provider_repository_id and claim.issue_number == int(str(node.issue_ref).lstrip("#")),
        "claim_binding_changed",
    )
    context = RunnerContext(identity, execution, datetime.now(UTC))
    raw, grant, lineage, _ = await historical_authority(
        session, services=services, context=context, node=node, binding=binding, run_id=request.expected_run_id, generation=identity.claim_generation
    )
    values = await current_policy(
        session,
        services=services,
        context=context,
        node=node,
        binding=binding,
        run_id=request.expected_run_id,
        raw=raw,
        grant=grant,
        actor_id=actor_id,
        preview=True,
        read_only=True,
    )
    inputs, meter = values[2], values[4]
    require(await policy_owner_matches(session, lineage["source_actor_id"], inputs.policy), "released_reviewer_provenance_changed")
    require(inputs.plan_version == request.expected_plan_version, "accepted_policy_changed_or_expired")
    plan = await session.scalar(
        select(OrchestrationAcceptedPlan).where(
            OrchestrationAcceptedPlan.org_id == org_id,
            OrchestrationAcceptedPlan.flow_id == node.flow_id,
            OrchestrationAcceptedPlan.superseded_at.is_(None),
        )
    )
    require(plan is not None and (plan.plan_document or {}).get("execution_continuation") is None, "protected_authority_required")
    require(await services.head(binding) == request.expected_head_sha, "current_pr_changed")
    limits = await require_budget(session, node=node, raw=raw, policy=inputs.policy, meter=meter)
    # A server-derived window makes a preview finite without persisting preview
    # state or trusting a caller-supplied expiry. Replays cannot extend it.
    expires = datetime.fromtimestamp((int(datetime.now(UTC).timestamp()) // WINDOW_SECONDS + 1) * WINDOW_SECONDS, UTC)
    expires = min(expires, inputs.policy.expires_at, grant.expires_at)
    data = dict(
        contract=CONTRACT,
        org_id=org_id,
        flow_id=node.flow_id,
        node_id=node.id,
        attempt=node.attempts,
        accepted_plan_version=identity.accepted_plan_version,
        plan_version=inputs.plan_version,
        policy_id=inputs.policy.policy_id,
        policy_hash=inputs.policy.policy_hash,
        execution_id=execution.id,
        execution_revision=execution.revision,
        execution_attempts=execution.attempts,
        claim_id=identity.claim_id,
        prior_claim_generation=identity.claim_generation,
        claim_generation=identity.claim_generation + 1,
        released_at=claim.released_at.isoformat(),
        release_reason=claim.release_reason,
        binding_id=binding.id,
        binding_revision=binding.revision,
        installation_id=binding.installation_id,
        repo=binding.repo,
        pr_number=binding.pr_number,
        provider_repository_id=binding.provider_repository_id,
        provider_pr_node_id=binding.provider_pr_node_id,
        accepted_scope=binding.accepted_scope,
        head_sha=request.expected_head_sha,
        expires_at=expires.isoformat(),
        limits=limits,
        **lineage,
    )
    require_unexpired(data)
    return data, node, context, binding


async def request_recovery(session, *, org_id, node_id, actor_id, actor_role, request, accept=False, services=None):
    from .review_cycle_dispatch import ReviewCycleServices
    from .work_claims import ClaimBinding, ClaimOwner, Disposition, OwnerKind, bind_run, claim_work

    require(
        request.mode == "review_released_reviewer" and request.source_run_id is None and request.reviewer_model is None,
        "released_reviewer_request_invalid",
    )
    require(actor_id and actor_role in {"owner", "org_admin", "platform_admin"}, "human_plan_approver_required")
    services = services or ReviewCycleServices(None)
    if request.renew_decision_id:
        from .released_reviewer_renewal import request_renewal

        return await request_renewal(
            session, org_id=org_id, node_id=node_id, actor_id=actor_id, actor_role=actor_role, request=request, accept=accept, services=services
        )
    decision_id = str(
        uuid5(NAMESPACE_URL, CONTRACT + ":" + digest(dict(org=org_id, node=node_id, actor=actor_id, snapshot=request.expected_snapshot)))
    )
    if accept:
        require(request.expected_snapshot, "preview_snapshot_required")
        await session.scalar(select(OrchestrationNode).where(OrchestrationNode.org_id == org_id, OrchestrationNode.id == node_id).with_for_update())
        old = await session.get(OrchestrationDecision, decision_id, populate_existing=True)
        if old is not None:
            data = receipt_data(old)
            require(
                old.org_id == org_id
                and old.node_id == node_id
                and old.actor_id == actor_id
                and old.kind == KIND
                and old.actor_kind == "human"
                and all(
                    data.get(k) == v
                    for k, v in dict(
                        snapshot=request.expected_snapshot,
                        attempt=request.expected_attempt,
                        plan_version=request.expected_plan_version,
                        prior_run_id=request.expected_run_id,
                        head_sha=request.expected_head_sha,
                    ).items()
                ),
                "recovery_receipt_conflict",
            )
            return dict(accepted=True, created=False, decision_id=old.id)
    data, node, context, binding = await current(
        session, org_id=org_id, node_id=node_id, request=request, actor_id=actor_id, services=services, lock=accept
    )
    snapshot = digest(data)
    preview = dict(**data, snapshot=snapshot, attempts_preserved=True, receipts_preserved=True, next_action="review_released_reviewer")
    if not accept:
        return preview
    require(snapshot == request.expected_snapshot, "recovery_preview_changed")
    event = reservation_id(decision_id)
    receipt = await claim_work(
        session,
        binding=ClaimBinding(org_id, binding.provider_repository_id, int(str(node.issue_ref).lstrip("#"))),
        owner=ClaimOwner(OwnerKind.ENGINE_FLOW, node.flow_id),
        event_id=event,
    )
    require(
        receipt.disposition is Disposition.ADMITTED and receipt.claim_id == data["claim_id"] and receipt.generation == data["claim_generation"],
        "recovery_claim_changed",
    )
    bound = await bind_run(session, org_id=org_id, claim_id=receipt.claim_id, generation=receipt.generation, run_id=event)
    require(bound.disposition is Disposition.ADMITTED, "recovery_claim_changed")
    require_unexpired(data)
    session.add(
        OrchestrationDecision(
            id=decision_id,
            org_id=org_id,
            flow_id=node.flow_id,
            node_id=node.id,
            kind=KIND,
            actor_kind="human",
            actor_id=actor_id,
            actor_role=actor_role,
            reason=json.dumps(dict(**data, snapshot=snapshot, reason=request.reason)),
        )
    )
    updated = await advance_execution(
        session,
        identity=replace(context.identity, claim_generation=receipt.generation),
        advance=PhaseAdvance(
            phase=ExecutionPhase.AWAITING_REVIEW,
            status=ExecutionStatus.RUNNABLE,
            expected_revision=context.execution.revision,
            next_check_at=datetime.now(UTC),
            progress_note="Owner restored the prematurely released reviewer lane; attempts, findings and failures remain recorded.",
        ),
    )
    require(updated.kind is OutcomeKind.APPLIED, "recovery_assignment_changed")
    await session.flush()
    require_unexpired(data)
    return dict(accepted=True, created=True, decision_id=decision_id, **preview)


async def verified_decision(session, *, decision_id, context, node, binding, services, head_sha, successor_action=None, _inspect_expired=False):
    row = await session.get(OrchestrationDecision, decision_id)
    require(
        row is not None and row.kind == KIND and row.actor_kind == "human" and row.actor_role in {"owner", "org_admin", "platform_admin"},
        "recovery_authority_changed",
    )
    recorded = receipt_data(row)
    require(digest({k: v for k, v in recorded.items() if k not in {"snapshot", "reason"}}) == recorded.get("snapshot"), "recovery_receipt_changed")
    data = await effective_receipt(session, row, recorded)
    expected = dict(
        contract=CONTRACT,
        org_id=node.org_id,
        flow_id=node.flow_id,
        node_id=node.id,
        attempt=node.attempts,
        accepted_plan_version=context.identity.accepted_plan_version,
        execution_id=context.execution.id,
        claim_id=context.identity.claim_id,
        claim_generation=context.identity.claim_generation,
        binding_id=binding.id,
        binding_revision=binding.revision,
        installation_id=binding.installation_id,
        accepted_scope=binding.accepted_scope,
        repo=binding.repo,
        pr_number=binding.pr_number,
        provider_repository_id=binding.provider_repository_id,
        provider_pr_node_id=binding.provider_pr_node_id,
        head_sha=head_sha,
    )
    require(
        row.org_id == node.org_id
        and row.flow_id == node.flow_id
        and row.node_id == node.id
        and all(data.get(k) == v for k, v in expected.items())
        and node.state == "running"
        and binding.registered_by_kind == "human"
        and binding.recovery_reason
        and binding_scope_matches(binding, node),
        "recovery_authority_changed",
    )
    if not _inspect_expired:
        require_unexpired(data, reconciliation_required=successor_action is not None)
    owner = reservation_id(row.id)
    if successor_action is not None:
        from .review_cycle_dispatch import ACTOR, continuation_run_id, receipt_id

        committed = await session.get(OrchestrationDecision, receipt_id(successor_action.operation_key))
        saved = receipt_data(committed)
        require(
            committed is not None
            and committed.org_id == node.org_id
            and committed.flow_id == node.flow_id
            and committed.node_id == node.id
            and committed.kind == "agent_dispatched"
            and committed.actor_id == ACTOR
            and committed.actor_kind == "service"
            and saved.get("developer_recovery_decision_id") == row.id
            and saved.get("execution_id") == context.execution.id
            and saved.get("accepted_plan_version") == context.identity.accepted_plan_version
            and saved.get("claim_generation") == context.identity.claim_generation
            and saved.get("operation_key") == successor_action.operation_key
            and saved.get("action") == "review"
            and successor_action.org_id == node.org_id
            and successor_action.execution_id == context.execution.id
            and successor_action.detail.get("developer_recovery_decision_id") == row.id
            and successor_action.detail.get("action") == "review"
            and saved.get("run_id") == continuation_run_id(successor_action.operation_key),
            "recovery_dispatch_changed",
        )
        owner = saved["run_id"]
    claim = await session.get(OrchestrationWorkClaim, context.identity.claim_id, populate_existing=True)
    require(
        claim is not None
        and claim.org_id == node.org_id
        and claim.state == "held"
        and claim.owner_kind == "engine_flow"
        and claim.owner_ref == node.flow_id
        and claim.generation == data["claim_generation"]
        and claim.active_run_id == owner
        and claim.claim_event_id == owner,
        "recovery_claim_changed",
    )
    raw, grant, lineage, _ = await historical_authority(
        session,
        services=services,
        context=context,
        node=node,
        binding=binding,
        run_id=data["prior_run_id"],
        generation=data["prior_claim_generation"],
        recovery_id=row.id,
        history_action_ids=data.get("history_action_ids"),
    )
    require(all(data.get(k) == v for k, v in lineage.items()), "recovery_receipt_changed")
    values = await current_policy(
        session,
        services=services,
        context=context,
        node=node,
        binding=binding,
        run_id=owner,
        raw=raw,
        grant=grant,
        actor_id=row.actor_id,
        read_only=_inspect_expired,
    )
    inputs, meter = values[2], values[4]
    require(await policy_owner_matches(session, lineage["source_actor_id"], inputs.policy), "released_reviewer_provenance_changed")
    require(
        inputs.plan_version == data["plan_version"]
        and inputs.policy.policy_id == data["policy_id"]
        and inputs.policy.policy_hash == data["policy_hash"],
        "recovery_authority_changed",
    )
    limits = await require_budget(session, node=node, raw=raw, policy=inputs.policy, meter=meter)
    require(limits == data["limits"], "recovery_budget_changed")
    if not _inspect_expired:
        require_unexpired(data, reconciliation_required=successor_action is not None)
    return data, values


async def recovery_snapshot(session, context, node, binding, services, dispatches):
    rows = await session.scalars(
        select(OrchestrationDecision)
        .where(
            OrchestrationDecision.org_id == node.org_id,
            OrchestrationDecision.node_id == node.id,
            OrchestrationDecision.kind == KIND,
        )
        .order_by(OrchestrationDecision.created_at.desc())
        .limit(100)
    )
    row = next((r for r in rows if receipt_data(r).get("claim_generation") == context.identity.claim_generation), None)
    if row is None:
        return None
    if any(a.detail.get("developer_recovery_decision_id") == row.id for a in dispatches):
        return None
    head = await services.head(binding)
    data, values = await verified_decision(session, decision_id=row.id, context=context, node=node, binding=binding, services=services, head_sha=head)
    inputs, meter = values[2], values[4]
    # author_run_id is the current human-recovered binding's assignment. The
    # original producer remains explicit provenance; neither claims completion.
    return dict(
        active_run_id=reservation_id(row.id),
        author_run_id=data["author_run_id"],
        released_review_retry_of=data["prior_run_id"],
        findings=data.get("findings", []),
        review_artifact=data.get("review_artifact"),
        source_run_id=data["source_run_id"],
        head_sha=head,
        remaining_spend_usd=str(inputs.policy.limits.max_spend_usd - meter.total_usd),
        remaining_attempts=inputs.policy.limits.max_attempts_per_node
        - await stage_attempts(session, org_id=node.org_id, node_id=node.id, action=Action.REVIEW),
        binding_id=binding.id,
        binding_revision=binding.revision,
        accepted_scope=binding.accepted_scope,
        repo=binding.repo,
        pr_number=binding.pr_number,
        provider_repository_id=binding.provider_repository_id,
        provider_pr_node_id=binding.provider_pr_node_id,
        sequence=1,
        next_action=Action.REVIEW.value,
        developer_recovery_decision_id=row.id,
        released_reviewer_contract=CONTRACT,
    )


async def verify_transfer(session, *, decision_id, identity, execution, action, dispatch_decision, expected_run_id, completed_execution, services):
    """Repeat the finite receipt and live source fences under the claim lock."""
    row = await session.get(OrchestrationDecision, decision_id)
    data = receipt_data(row)
    require(
        row is not None
        and row.kind == KIND
        and row.org_id == identity.org_id
        and row.node_id == identity.node_id
        and row.flow_id == execution.flow_id
        and row.actor_kind == "human"
        and row.actor_role in {"owner", "org_admin", "platform_admin"}
        and data.get("contract") == CONTRACT
        and data.get("execution_id") == execution.id
        and data.get("attempt") == identity.cycle
        and data.get("accepted_plan_version") == identity.accepted_plan_version
        and data.get("claim_id") == identity.claim_id
        and data.get("claim_generation") == identity.claim_generation
        and expected_run_id == reservation_id(decision_id)
        and action.detail.get("action") == "review"
        and action.detail.get("released_reviewer_contract") == CONTRACT
        and action.detail.get("developer_recovery_decision_id") == decision_id
        and action.detail.get("released_review_retry_of") == data.get("prior_run_id")
        and action.detail.get("findings", []) == data.get("findings", [])
        and action.detail.get("review_artifact") == data.get("review_artifact")
        and receipt_data(dispatch_decision).get("developer_recovery_decision_id") == decision_id
        and all(
            action.detail.get(k) == data.get(k)
            for k in ("binding_id", "binding_revision", "accepted_scope", "head_sha", "source_run_id", "author_run_id")
        )
        and digest(completed_execution) == data.get("terminal_digest"),
        "recovery_authority_changed",
    )
    require(services is not None, "recovery_authority_changed")
    node = await session.get(OrchestrationNode, identity.node_id, populate_existing=True)
    binding = await active_binding_for_node(session, org_id=identity.org_id, node_id=identity.node_id, attempt=identity.cycle)
    require(node is not None and binding is not None, "recovery_authority_changed")
    await verified_decision(
        session,
        decision_id=decision_id,
        context=RunnerContext(identity, execution, datetime.now(UTC)),
        node=node,
        binding=binding,
        services=services,
        head_sha=await services.head(binding),
    )
