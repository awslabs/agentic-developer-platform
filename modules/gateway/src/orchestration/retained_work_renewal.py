"""Explicit finite renewal only before retained review publication can begin."""

import json
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select

from . import retained_work_recovery as retained
from .continuation import digest
from .dispatch_pass import attempt_run_id
from .execution_runner import RunnerContext
from .execution_state import ExecutionPhase, ExecutionStatus, OutcomeKind, PhaseAdvance
from .execution_store import advance_execution, load_execution
from .handoff import identity_for_attempt
from .models import OrchestrationAction, OrchestrationDecision, OrchestrationNode, OrchestrationWorkClaim
from .pr_bindings import active_binding_for_node
from .protected_developer_recovery import reservation_id
from .protected_review_recovery import require


async def unstarted(session, *, context, decision_id, services):
    """No committed dispatch means provisioning/publication never had authority."""
    from .review_cycle_dispatch import continuation_run_id, receipt_id

    claim = await session.get(OrchestrationWorkClaim, context.identity.claim_id, populate_existing=True)
    require(
        claim is not None
        and claim.state == "held"
        and claim.generation == context.identity.claim_generation
        and claim.active_run_id == reservation_id(decision_id)
        and claim.claim_event_id == reservation_id(decision_id),
        "retained_renewal_requires_reconciliation",
    )
    actions = list(
        await session.scalars(
            select(OrchestrationAction)
            .where(OrchestrationAction.org_id == context.identity.org_id, OrchestrationAction.execution_id == context.execution.id)
            .execution_options(populate_existing=True)
            .limit(101)
        )
    )
    require(len(actions) <= 100, "retained_renewal_requires_reconciliation")
    pending = []
    for action in actions:
        if action.kind != "review_cycle_dispatch":
            require(action.status in {"succeeded", "failed"}, "pending_effect_requires_reconciliation")
            continue
        require(
            action.detail.get("developer_recovery_decision_id") == decision_id
            and action.status in {"prepared", "dispatched", "unknown"}
            and await session.get(OrchestrationDecision, receipt_id(action.operation_key)) is None
            and await services.protected(context.identity.org_id, continuation_run_id(action.operation_key)) is None,
            "retained_renewal_requires_reconciliation",
        )
        pending.append(dict(id=action.id, operation_key=action.operation_key, status=action.status, detail=action.detail))
    require(
        len(pending) <= 1 and context.execution.pending_action_key == (pending[0]["operation_key"] if pending else None),
        "retained_renewal_requires_reconciliation",
    )
    return pending


async def request_renewal(session, *, org_id, node_id, actor_id, actor_role, request, accept, services):
    decision_id = str(
        uuid5(
            NAMESPACE_URL,
            retained.RENEWAL_CONTRACT + ":" + digest(dict(org=org_id, node=node_id, actor=actor_id, snapshot=request.expected_snapshot)),
        )
    )
    query = select(OrchestrationNode).where(OrchestrationNode.org_id == org_id, OrchestrationNode.id == node_id)
    node = await session.scalar(query.execution_options(populate_existing=True).with_for_update() if accept else query)
    expected = dict(
        recovery_decision_id=request.renew_decision_id,
        attempt=request.expected_attempt,
        plan_version=request.expected_plan_version,
        prior_run_id=request.expected_run_id,
        source_run_id=request.source_run_id,
        head_sha=request.expected_head_sha,
    )
    if accept:
        require(request.expected_snapshot, "preview_snapshot_required")
        old = await session.get(OrchestrationDecision, decision_id, populate_existing=True)
        if old is not None:
            saved = json.loads(old.reason)
            require(
                old.org_id == org_id
                and old.node_id == node_id
                and old.actor_id == actor_id
                and old.actor_kind == "human"
                and old.kind == retained.RENEWAL_KIND
                and saved.get("snapshot") == request.expected_snapshot
                and all(saved.get(k) == v for k, v in expected.items()),
                "recovery_receipt_conflict",
            )
            return dict(accepted=True, created=False, decision_id=old.id)
    require(
        node is not None
        and node.kind == "story"
        and node.state == "running"
        and node.attempts == request.expected_attempt
        and request.expected_run_id == attempt_run_id(node.id, node.attempts),
        "retained_renewal_assignment_changed",
    )
    identity = await identity_for_attempt(session, org_id=org_id, node_id=node.id, attempt=node.attempts)
    require(identity is not None, "current_execution_changed")
    loaded = await load_execution(session, identity=identity, for_update=accept)
    require(loaded is not None and loaded.kind is OutcomeKind.APPLIED and loaded.record is not None, "current_execution_changed")
    execution = loaded.record
    require(
        execution.phase == "awaiting_review" and execution.status in {"runnable", "blocked", "awaiting_external"},
        "retained_renewal_requires_reconciliation",
    )
    context = RunnerContext(identity, execution, retained.datetime.now(retained.UTC))
    pending = await unstarted(session, context=context, decision_id=request.renew_decision_id, services=services)
    binding = await active_binding_for_node(session, org_id=org_id, node_id=node_id, attempt=node.attempts)
    row = await session.get(OrchestrationDecision, request.renew_decision_id)
    require(binding is not None and row is not None and row.actor_id == actor_id, "retained_renewal_authority_changed")
    data, values = await retained.verified_decision(
        session,
        decision_id=row.id,
        context=context,
        node=node,
        binding=binding,
        services=services,
        head_sha=await services.head(binding),
        _inspect_expired=True,
    )
    # Recovery may retain the failed developer's original handoff. Renewal must
    # preserve exactly that verified history, including the absence of a receipt.
    require(
        execution.handoff_receipt_ref == data.get("handoff_receipt_ref"),
        "retained_renewal_requires_reconciliation",
    )
    require(all(data.get(k) == v for k, v in expected.items() if k != "recovery_decision_id"), "retained_renewal_assignment_changed")
    now = retained.datetime.now(retained.UTC)
    require(retained.datetime.fromisoformat(data["expires_at"]) <= now, "retained_renewal_not_expired")
    expires = retained.datetime.fromtimestamp((int(now.timestamp()) // retained.WINDOW_SECONDS + 1) * retained.WINDOW_SECONDS, retained.UTC)
    expires = min(expires, values[1].expires_at, values[2].policy.expires_at)
    preview = dict(
        **expected,
        contract=retained.RENEWAL_CONTRACT,
        org_id=org_id,
        flow_id=node.flow_id,
        node_id=node.id,
        recovery_receipt_digest=digest(json.loads(row.reason)),
        prior_renewal_decision_id=data.get("renewal_decision_id"),
        prior_expires_at=data["expires_at"],
        expires_at=expires.isoformat(),
        execution_id=execution.id,
        execution_revision=execution.revision,
        execution_attempts=execution.attempts,
        claim_id=identity.claim_id,
        claim_generation=identity.claim_generation,
        pending_dispatch=pending,
        limits=data["limits"],
    )
    retained.require_unexpired(preview)
    preview["snapshot"] = digest(preview)
    if not accept:
        return preview
    require(preview["snapshot"] == request.expected_snapshot, "recovery_preview_changed")
    session.add(
        OrchestrationDecision(
            id=decision_id,
            org_id=org_id,
            flow_id=node.flow_id,
            node_id=node_id,
            kind=retained.RENEWAL_KIND,
            actor_kind="human",
            actor_id=actor_id,
            actor_role=actor_role,
            reason=json.dumps(dict(preview, reason=request.reason)),
        )
    )
    updated = await advance_execution(
        session,
        identity=identity,
        advance=PhaseAdvance(
            phase=ExecutionPhase.AWAITING_REVIEW,
            status=ExecutionStatus.AWAITING_EXTERNAL if pending else ExecutionStatus.RUNNABLE,
            expected_revision=execution.revision,
            next_check_at=retained.datetime.now(retained.UTC),
            progress_note="Operator renewed the same unstarted retained-work review; attempts and original receipt remain unchanged.",
        ),
    )
    require(updated.kind is OutcomeKind.APPLIED, "recovery_assignment_changed")
    await session.flush()
    retained.require_unexpired(preview)
    return dict(accepted=True, created=True, decision_id=decision_id, **preview)
