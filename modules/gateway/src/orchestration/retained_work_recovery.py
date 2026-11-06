"""An owner's bounded authority to review retained work after failed development.

The protected failures remain failures. This receipt authorizes one review of an
operator-bound PR on the same execution and a newer generation of its same claim.
It neither manufactures a developer handoff nor admits another developer attempt.
"""

import asyncio
import json
import os
from dataclasses import replace
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select

from src.agentauth.bootstrap import BootstrapRefusedError
from src.agentauth.grants import AUTHORITY_GATE_DECISION, AgentAction

from .continuation import digest
from .developer_personas import DEVELOPER_PERSONAS
from .dispatch_pass import attempt_run_id
from .execution_policy import Action
from .execution_runner import RunnerContext
from .execution_state import BlockCode, ExecutionPhase, ExecutionStatus, OutcomeKind, PhaseAdvance
from .execution_store import advance_execution, load_execution
from .genesis import resolve_engine_genesis
from .handoff import handoff_receipt_ref, identity_for_attempt
from .models import (
    OrchestrationAcceptedPlan,
    OrchestrationAction,
    OrchestrationDecision,
    OrchestrationFlow,
    OrchestrationNode,
    OrchestrationWorkClaim,
)
from .pr_bindings import active_binding_for_node, binding_scope_matches
from .protected_developer_recovery import current_policy, reservation_id
from .protected_review_recovery import require
from .review_cycle import CycleBlockedError
from .stage_attempts import stage_attempts
from .state import ActorKind, NodeState, transition

KIND = "retained_work_review_recovery"
CONTRACT = "protected-retained-work-review/v1"
WINDOW_SECONDS = 900
RENEWAL_KIND = "retained_work_review_renewal"
RENEWAL_CONTRACT = "protected-retained-work-review-renewal/v1"


def require_unexpired(data, *, reconciliation_required=False):
    reason = "retained_recovery_expired_reconciliation_required" if reconciliation_required else "retained_recovery_expired_renew_unstarted"
    require(datetime.fromisoformat(data["expires_at"]) > datetime.now(UTC), reason)


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
    require(len(rows) <= 100, "retained_renewal_history_limit")
    for renewal in rows:
        saved = json.loads(renewal.reason)
        if saved.get("recovery_decision_id") != row.id:
            continue
        require(
            renewal.flow_id == row.flow_id
            and renewal.actor_kind == "human"
            and renewal.actor_id == row.actor_id
            and renewal.actor_role in {"owner", "org_admin", "platform_admin"}
            and saved.get("contract") == RENEWAL_CONTRACT
            and saved.get("recovery_receipt_digest") == digest(data),
            "retained_renewal_authority_changed",
        )
        return dict(data, expires_at=saved["expires_at"], renewal_decision_id=renewal.id)
    return data


async def sources(session, *, services, node, binding, source_run_id):
    """Verify both dispatched assignments as provenance, not live worker authority."""
    require(os.environ.get("AGENT_AUTHORITY_ENABLED", "false").lower() == "true", "protected_authority_required")
    flow = await session.get(OrchestrationFlow, node.flow_id, populate_existing=True)
    require(
        flow is not None
        and flow.org_id == node.org_id
        and flow.state in {"pending", "ready", "running"}
        and not flow.execution_paused
        and flow.archived_at is None,
        "retained_source_authority_changed",
    )
    current_run_id = attempt_run_id(node.id, node.attempts)
    records, grants, dispatches = {}, {}, {}
    for run_id in dict.fromkeys((current_run_id, source_run_id)):
        raw = await services.protected(node.org_id, run_id)
        try:
            attempt = int(raw["orchestration_node_attempt"]["N"])
            require(
                1 <= attempt <= node.attempts
                and run_id == attempt_run_id(node.id, attempt)
                and raw.get("invocation_id") == {"S": run_id}
                and raw.get("tenant_id") == {"S": node.org_id}
                and raw.get("flow_id") == {"S": node.flow_id}
                and raw.get("orchestration_node_id") == {"S": node.id}
                and raw.get("persona", {}).get("S") in DEVELOPER_PERSONAS
                and raw.get("status") == {"S": "completed"}
                and raw.get("terminal_outcome", {}).get("S") in {"failed", "budget_stopped"}
                and raw.get("workload_binding", {}).get("S")
                and not raw.get("orchestration_continuation_receipt")
                and raw.get("repo") == {"S": binding.repo}
                and raw.get("installation_id") == {"N": str(binding.installation_id)}
                and raw.get("provider_repository_id") == {"N": str(binding.provider_repository_id)}
                and raw.get("issue_number") == {"N": str(int(str(node.issue_ref).lstrip("#")))},
                "retained_source_unverified",
            )
            grant = await asyncio.to_thread(
                services.writer.store.live_grant,
                invocation_id=run_id,
                tenant_id=node.org_id,
                attempt=int(raw["current_attempt"]["N"]),
                now=datetime.now(UTC),
            )
            genesis = await resolve_engine_genesis(session, org_id=node.org_id, decision_id=grant.authority.reference_id)
        except (BootstrapRefusedError, KeyError, TypeError, ValueError):
            raise CycleBlockedError("retained_source_unverified") from None
        from .plan_lineage import approval_authorizes_node

        require(
            grant.flow_id == node.flow_id
            and grant.authority.kind == AUTHORITY_GATE_DECISION
            and binding.repo in grant.repo_scope
            and AgentAction.MONITOR in grant.delegable_actions
            and genesis.flow_id == node.flow_id
            and genesis.root_human_id == grant.authority.human_id
            and await approval_authorizes_node(session, org_id=node.org_id, flow_id=node.flow_id, node_id=node.id, decision_id=genesis.decision_id),
            "retained_source_authority_changed",
        )
        candidates = await session.scalars(
            select(OrchestrationDecision).where(
                OrchestrationDecision.org_id == node.org_id,
                OrchestrationDecision.flow_id == node.flow_id,
                OrchestrationDecision.node_id == node.id,
                OrchestrationDecision.kind == "node_dispatched",
            )
        )
        matched = []
        for row in candidates:
            data = json.loads(row.reason or "{}")
            if data.get("run_id") == run_id and data.get("attempt") == attempt:
                require(
                    row.actor_kind == "service"
                    and row.actor_id == "system:orchestration-dispatch"
                    and data.get("repo") == binding.repo
                    and data.get("issue") == int(str(node.issue_ref).lstrip("#")),
                    "retained_source_dispatch_changed",
                )
                matched.append(row)
        require(len(matched) == 1, "retained_source_dispatch_changed")
        records[run_id], grants[run_id], dispatches[run_id] = raw, grant, matched[0].id
    current, source = records[current_run_id], records[source_run_id]
    chain = current.get("flow_id", {}).get("S")
    require(
        chain and source.get("flow_id") == {"S": chain} and grants[current_run_id].authority.human_id == grants[source_run_id].authority.human_id,
        "retained_source_lineage_changed",
    )
    return (
        current,
        grants[current_run_id],
        {
            "source_run_id": source_run_id,
            "source_dispatch_id": dispatches[source_run_id],
            "source_terminal_digest": digest(source),
            "prior_run_id": current_run_id,
            "current_dispatch_id": dispatches[current_run_id],
            "terminal_digest": digest(current),
            "correlation_id": chain,
        },
    )


async def require_budget(session, *, node, raw, policy, meter):
    """Read the existing chain accumulator; never seed, clear or enlarge it."""
    from src.budget.enforcement_service import BudgetEnforcementService
    from src.budget.reservations import ReservationTarget
    from src.shared.schemas.budget import EntityType

    from .flow_budget import get_flow_reservations

    service = BudgetEnforcementService()
    run_cap = await service._resolve_scope_cap(session, node.org_id, EntityType.RUN, approved_platform_cap=policy._shared_run_spend_usd)
    chain_cap = await service._resolve_scope_cap(session, node.org_id, EntityType.CHAIN, approved_platform_cap=policy._shared_chain_spend_usd)
    chain = raw.get("flow_id", {}).get("S")
    require(chain and meter is not None, "retained_budget_unavailable")
    target = ReservationTarget(node.org_id, "chain", chain, "run", "lifetime", chain_cap)
    observed = await get_flow_reservations().snapshot(target, existing_scope=True)
    require(observed is not None and observed.total_usd.is_finite(), "retained_budget_unavailable")
    if observed.total_usd + run_cap > chain_cap or meter.total_usd + run_cap > policy.limits.max_spend_usd:
        raise CycleBlockedError("retained_budget_exhausted", BlockCode.BUDGET_EXHAUSTED)
    return {"chain_cap_usd": str(chain_cap), "run_cap_usd": str(run_cap), "flow_cap_usd": str(policy.limits.max_spend_usd)}


async def current(session, *, org_id, node_id, request, actor_id, services, lock=False):
    query = select(OrchestrationNode).where(OrchestrationNode.org_id == org_id, OrchestrationNode.id == node_id)
    node = await session.scalar(query.execution_options(populate_existing=True).with_for_update() if lock else query)
    require(node is not None and node.kind == "story" and node.state == "failed", "retained_story_not_failed")
    require(
        node.attempts == request.expected_attempt and request.expected_run_id == attempt_run_id(node.id, node.attempts), "report_assignment_changed"
    )
    identity = await identity_for_attempt(session, org_id=org_id, node_id=node.id, attempt=node.attempts)
    require(identity is not None, "current_execution_changed")
    loaded = await load_execution(session, identity=identity, for_update=lock, released_failure_run_id=request.expected_run_id)
    require(loaded is not None and loaded.kind is OutcomeKind.APPLIED and loaded.record is not None, "current_execution_changed")
    execution = loaded.record
    # A developer can commit its handoff before reporting failure. Keep that
    # execution's own receipt as history; the failed exit and new review authority
    # are still independently verified below.
    require(
        execution.phase == "concluded"
        and execution.status == "concluded"
        and execution.pending_action_key is None
        and execution.handoff_receipt_ref in {None, handoff_receipt_ref(identity, execution.id)},
        "retained_execution_not_settled",
    )
    actions = list(
        await session.scalars(
            select(OrchestrationAction)
            .where(
                OrchestrationAction.org_id == org_id,
                OrchestrationAction.execution_id == execution.id,
            )
            .limit(101)
        )
    )
    require(
        len(actions) <= 100 and all(a.status in {"succeeded", "failed"} and a.kind != "review_cycle_dispatch" for a in actions),
        "pending_effect_requires_reconciliation",
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
        "released_developer_claim_changed",
    )
    binding = await active_binding_for_node(session, org_id=org_id, node_id=node.id, attempt=node.attempts)
    require(
        binding is not None
        and binding_scope_matches(binding, node)
        and binding.run_id == request.expected_run_id
        and binding.registered_by_kind == "human"
        and binding.recovery_reason,
        "operator_bound_delivery_required",
    )
    require(
        claim.provider_repository_id == binding.provider_repository_id and claim.issue_number == int(str(node.issue_ref).lstrip("#")),
        "claim_binding_changed",
    )
    raw, grant, lineage = await sources(session, services=services, node=node, binding=binding, source_run_id=request.source_run_id)
    context = RunnerContext(identity, execution, datetime.now(UTC))
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
    )
    inputs, meter = values[2], values[4]
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
        handoff_receipt_ref=execution.handoff_receipt_ref,
        claim_id=identity.claim_id,
        prior_claim_generation=identity.claim_generation,
        claim_generation=identity.claim_generation + 1,
        released_at=claim.released_at.isoformat(),
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

    require(request.mode == "review_existing_work" and request.source_run_id and request.reviewer_model is None, "retained_review_request_invalid")
    require(actor_id and actor_role in {"owner", "org_admin", "platform_admin"}, "human_plan_approver_required")
    services = services or ReviewCycleServices(None)
    if request.renew_decision_id:
        from .retained_work_renewal import request_renewal

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
            data = json.loads(old.reason)
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
                        source_run_id=request.source_run_id,
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
    preview = dict(**data, snapshot=snapshot, attempts_preserved=True, receipts_preserved=True, next_action="review_existing_work")
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
    move = transition(NodeState.FAILED, NodeState.RUNNING, actor_kind=ActorKind.HUMAN, reason=request.reason)
    require(move.allowed, "retained_recovery_transition_refused")
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
            from_state="failed",
            to_state="running",
            reason=json.dumps(dict(**data, snapshot=snapshot, reason=request.reason)),
        )
    )
    node.state = "running"
    updated = await advance_execution(
        session,
        identity=replace(context.identity, claim_generation=receipt.generation),
        advance=PhaseAdvance(
            phase=ExecutionPhase.AWAITING_REVIEW,
            status=ExecutionStatus.RUNNABLE,
            expected_revision=context.execution.revision,
            next_check_at=datetime.now(UTC),
            progress_note="Operator authorized review of retained work; prior developer failures remain recorded.",
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
    data = await effective_receipt(session, row, json.loads(row.reason))
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
        and binding.run_id == attempt_run_id(node.id, node.attempts)
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
        saved = json.loads(committed.reason) if committed else {}
        require(
            committed is not None
            and committed.org_id == node.org_id
            and committed.flow_id == node.flow_id
            and committed.node_id == node.id
            and committed.kind == "agent_dispatched"
            and committed.actor_id == ACTOR
            and committed.actor_kind == "service"
            and saved.get("developer_recovery_decision_id") == row.id
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
    raw, grant, lineage = await sources(session, services=services, node=node, binding=binding, source_run_id=data["source_run_id"])
    require(all(data.get(k) == v for k, v in lineage.items()), "recovery_receipt_changed")
    values = await current_policy(
        session, services=services, context=context, node=node, binding=binding, run_id=owner, raw=raw, grant=grant, actor_id=row.actor_id
    )
    inputs, meter = values[2], values[4]
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
    if dispatches:
        return None
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
    row = next((r for r in rows if json.loads(r.reason).get("claim_generation") == context.identity.claim_generation), None)
    if row is None:
        return None
    head = await services.head(binding)
    data, values = await verified_decision(session, decision_id=row.id, context=context, node=node, binding=binding, services=services, head_sha=head)
    inputs, meter = values[2], values[4]
    # author_run_id is the current human-recovered binding's assignment. The
    # original producer remains explicit provenance; neither claims completion.
    return dict(
        active_run_id=reservation_id(row.id),
        author_run_id=binding.run_id,
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
        retained_work_contract=CONTRACT,
    )


async def verify_transfer(session, *, decision_id, identity, execution, action, dispatch_decision, expected_run_id, completed_execution, services):
    """Repeat the finite receipt and live source fences under the claim lock."""
    row = await session.get(OrchestrationDecision, decision_id)
    data = json.loads(row.reason) if row else {}
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
        and action.detail.get("retained_work_contract") == CONTRACT
        and action.detail.get("developer_recovery_decision_id") == decision_id
        and json.loads(dispatch_decision.reason).get("developer_recovery_decision_id") == decision_id
        and all(action.detail.get(k) == data.get(k) for k in ("binding_id", "binding_revision", "accepted_scope", "head_sha", "source_run_id"))
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
