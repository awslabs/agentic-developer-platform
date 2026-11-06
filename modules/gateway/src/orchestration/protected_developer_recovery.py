"""Owner recovery of a delivered developer without a handoff receipt.

The owner reserves a fresh claim generation for the engine, not for the exited
developer. The missing handoff remains missing. A separate recovery receipt
permits exactly the first review of the retained delivery (#8096).
"""

import asyncio
import json
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import and_, select

from src.agentauth.bootstrap import BootstrapRefusedError
from src.agentauth.engine import validate_engine_authority

from .continuation import digest
from .developer_personas import DEVELOPER_PERSONAS
from .dispatch import graph_address
from .dispatch_pass import attempt_run_id
from .execution_policy import Action, ResourceRef, authorize_action
from .execution_runner import RunnerContext
from .execution_state import ExecutionStatus, OutcomeKind, PhaseAdvance
from .execution_store import advance_execution, load_execution
from .handoff import identity_for_attempt
from .models import OrchestrationAction, OrchestrationDecision, OrchestrationFlow, OrchestrationNode, OrchestrationWorkClaim
from .pr_bindings import active_binding_for_node, binding_scope_matches
from .protected_review_recovery import require
from .review_cycle import CycleBlockedError
from .shared_window import policy_owner_matches
from .stage_attempts import stage_attempts

KIND = "developer_review_recovery"
CONTRACT = "protected-developer-review-recovery/v1"


def reservation_id(decision_id):
    return "recovery:" + decision_id


def terminal_digest(raw):
    return digest(raw)


async def parent_authority(session, *, services, node, binding, run_id):
    """Verify historical delivery and still-live plan authority, never ownership."""
    require(os.environ.get("AGENT_AUTHORITY_ENABLED", "false").lower() == "true", "protected_authority_required")
    raw = await services.protected(node.org_id, run_id)
    require(
        run_id == attempt_run_id(node.id, node.attempts)
        and raw
        and raw.get("invocation_id") == {"S": run_id}
        and raw.get("tenant_id") == {"S": node.org_id}
        and raw.get("orchestration_node_id") == {"S": node.id}
        and raw.get("orchestration_node_attempt") == {"N": str(node.attempts)}
        and raw.get("persona", {}).get("S") in DEVELOPER_PERSONAS
        and not raw.get("orchestration_continuation_receipt")
        and raw.get("status") == {"S": "completed"}
        and raw.get("terminal_outcome") == {"S": "complete"}
        and raw.get("workload_binding", {}).get("S")
        and raw.get("repo") == {"S": binding.repo}
        and raw.get("installation_id") == {"N": str(binding.installation_id)}
        and raw.get("provider_repository_id") == {"N": str(binding.provider_repository_id)},
        "developer_exit_unverified",
    )
    try:
        grant = await asyncio.to_thread(
            services.writer.store.live_grant,
            invocation_id=run_id,
            tenant_id=node.org_id,
            attempt=int(raw["current_attempt"]["N"]),
            now=datetime.now(UTC),
        )
        await validate_engine_authority(session=session, execution=raw, grant=grant, store=services.writer.store)
    except (BootstrapRefusedError, KeyError, ValueError):
        raise CycleBlockedError("grant_revoked_or_unverifiable") from None
    return raw, grant


async def current_policy(session, *, services, context, node, binding, run_id, raw, grant, actor_id, preview=False, read_only=False):
    options = {"read_only": True} if read_only else {}
    values = await services.policy_context(session, context, node, binding, run_id, Action.REVIEW, raw, grant, **options)
    _, _, inputs, _, _, auth = values
    require(await policy_owner_matches(session, actor_id, inputs.policy), "recovery_policy_owner_required")
    require(
        await stage_attempts(
            session, org_id=node.org_id, node_id=node.id, action=Action.REVIEW, exclude_operation_key=context.execution.pending_action_key
        )
        < inputs.policy.limits.max_attempts_per_node,
        "continuation_attempts_exhausted",
    )
    flow = await session.get(OrchestrationFlow, node.flow_id)
    require(flow is not None and not flow.execution_paused and flow.archived_at is None, "flow_paused_or_archived")
    # Preview has already proved the exact completed developer can be replaced by
    # this owner. Only that proposed ownership differs from the live read. Every
    # other policy check, including membership, expiry, spend and scope, remains.
    if preview:
        auth = replace(auth, work_owned_by_policy_flow=True)
    decision = authorize_action(
        auth,
        Action.REVIEW,
        ResourceRef(repository_id=binding.repo, node_address=graph_address(node, flow_slug=flow.slug), org_id=node.org_id),
        inputs.plan_version,
    )
    if not decision.permitted:
        raise CycleBlockedError(decision.reason.value)
    return values


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
        execution.phase == "admitted"
        and execution.status not in {"concluded", "superseded"}
        and execution.pending_action_key is None
        and execution.handoff_receipt_ref is None,
        "developer_recovery_phase_changed",
    )
    require(
        not await session.scalar(
            select(OrchestrationAction.id)
            .where(
                OrchestrationAction.org_id == org_id,
                OrchestrationAction.execution_id == execution.id,
                # The runner may already have delivered the missing-handoff
                # notification. Preserve that receipt without treating it as
                # unfinished delivery work. Every other action still fences
                # recovery, including an unacknowledged notification.
                ~and_(
                    OrchestrationAction.kind == "notify_execution_block",
                    OrchestrationAction.status == "succeeded",
                    OrchestrationAction.receipt_ref.is_not(None),
                    OrchestrationAction.receipt_ref != "",
                    OrchestrationAction.observed_at.is_not(None),
                ),
            )
            .limit(1)
        ),
        "pending_effect_requires_reconciliation",
    )
    claim = await session.get(OrchestrationWorkClaim, identity.claim_id, populate_existing=True)
    released = (
        claim is not None
        and claim.state == "released"
        and claim.release_reason == "completed"
        and claim.released_at is not None
        and claim.active_run_id is None
    )
    held = (
        claim is not None
        and claim.state == "held"
        and claim.active_run_id == request.expected_run_id
        and claim.release_reason is None
        and claim.released_at is None
    )
    require(
        (released or held)
        and claim.generation == identity.claim_generation
        and claim.claim_event_id == request.expected_run_id
        and claim.owner_kind == "engine_flow"
        and claim.owner_ref == node.flow_id,
        "released_developer_claim_changed",
    )
    binding = await active_binding_for_node(session, org_id=org_id, node_id=node.id, attempt=node.attempts)
    require(binding is not None and binding_scope_matches(binding, node) and binding.run_id == request.expected_run_id, "bound_delivery_missing")
    require(
        claim.provider_repository_id == binding.provider_repository_id and claim.issue_number == int(str(node.issue_ref).lstrip("#")),
        "claim_binding_changed",
    )
    raw, grant = await parent_authority(session, services=services, node=node, binding=binding, run_id=request.expected_run_id)
    context = RunnerContext(identity, execution, datetime.now(UTC))
    _, _, inputs, _, _, _ = await current_policy(
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
    require(inputs.plan_version == request.expected_plan_version, "accepted_policy_changed_or_expired")
    require(await services.head(binding) == request.expected_head_sha, "current_pr_changed")
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
        prior_claim_state=claim.state,
        claim_generation=identity.claim_generation + 1,
        released_at=claim.released_at.isoformat() if claim.released_at is not None else None,
        release_reason=claim.release_reason,
        prior_run_id=request.expected_run_id,
        binding_id=binding.id,
        binding_revision=binding.revision,
        accepted_scope=binding.accepted_scope,
        head_sha=request.expected_head_sha,
        terminal_digest=terminal_digest(raw),
    )
    return data, node, context, binding


async def request_recovery(session, *, org_id, node_id, actor_id, actor_role, request, accept=False, services=None):
    from .review_cycle_dispatch import ReviewCycleServices
    from .work_claims import DEFAULT_LEASE_SECONDS, ClaimBinding, ClaimOwner, Disposition, OwnerKind, bind_run, claim_work

    require(actor_id and actor_role in {"owner", "org_admin", "platform_admin"}, "human_plan_approver_required")
    services = services or ReviewCycleServices(None)
    decision_id = str(
        uuid5(NAMESPACE_URL, CONTRACT + ":" + digest(dict(org=org_id, node=node_id, actor=actor_id, snapshot=request.expected_snapshot)))
    )
    if accept:
        require(request.expected_snapshot, "preview_snapshot_required")
        # Serialize identical acceptances before checking the released state.
        await session.scalar(select(OrchestrationNode).where(OrchestrationNode.org_id == org_id, OrchestrationNode.id == node_id).with_for_update())
        old = await session.get(OrchestrationDecision, decision_id, populate_existing=True)
        if old is not None:
            data = json.loads(old.reason)
            require(
                old.org_id == org_id and old.node_id == node_id and old.actor_id == actor_id and old.actor_kind == "human" and old.kind == KIND,
                "recovery_receipt_conflict",
            )
            require(
                all(
                    data.get(k) == v
                    for k, v in dict(
                        attempt=request.expected_attempt,
                        plan_version=request.expected_plan_version,
                        prior_run_id=request.expected_run_id,
                        head_sha=request.expected_head_sha,
                        snapshot=request.expected_snapshot,
                    ).items()
                ),
                "recovery_receipt_conflict",
            )
            return dict(accepted=True, created=False, decision_id=old.id)
    data, node, context, binding = await current(
        session, org_id=org_id, node_id=node_id, request=request, actor_id=actor_id, services=services, lock=accept
    )
    snapshot = digest(data)
    preview = dict(**data, snapshot=snapshot, attempts_preserved=True, receipts_preserved=True, next_action="fresh_codex_review")
    if not accept:
        return preview
    require(snapshot == request.expected_snapshot, "recovery_preview_changed")
    event = reservation_id(decision_id)
    if data["prior_claim_state"] == "held":
        # current(lock=True) holds the claim and verifies the exact completed
        # protected worker, absent handoff, settled effects, policy and PR head.
        # Reserve its next generation in the same transaction as the recovery
        # receipt. Never release the lane or pretend a handoff was received.
        claim = await session.get(OrchestrationWorkClaim, data["claim_id"])
        claim.generation = data["claim_generation"]
        claim.claim_event_id = event
        claim.active_run_id = None
        claim.claimed_at = claim.heartbeat_at = datetime.now(UTC)
        claim.lease_expires_at = claim.claimed_at + timedelta(seconds=DEFAULT_LEASE_SECONDS)
        await session.flush()
    else:
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
    bound = await bind_run(session, org_id=org_id, claim_id=data["claim_id"], generation=data["claim_generation"], run_id=event)
    require(bound.disposition is Disposition.ADMITTED, "recovery_claim_changed")
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
        identity=replace(context.identity, claim_generation=data["claim_generation"]),
        advance=PhaseAdvance(
            phase=context.execution.phase,
            status=ExecutionStatus.RUNNABLE,
            expected_revision=context.execution.revision,
            next_check_at=datetime.now(UTC),
        ),
    )
    require(updated.kind is OutcomeKind.APPLIED, "recovery_assignment_changed")
    # An accepted recovery begins a fresh bounded wait for its first reviewer.
    # The exited developer's old node clock must not time out that reservation.
    node.updated_at = datetime.now(UTC)
    await session.flush()
    return dict(accepted=True, created=True, decision_id=decision_id, **preview)


async def verified_decision(session, *, decision_id, context, node, binding, services, head_sha, successor_action=None):
    row = await session.get(OrchestrationDecision, decision_id)
    require(
        row is not None
        and row.org_id == node.org_id
        and row.flow_id == node.flow_id
        and row.node_id == node.id
        and row.kind == KIND
        and row.actor_kind == "human"
        and row.actor_role in {"owner", "org_admin", "platform_admin"},
        "recovery_authority_changed",
    )
    data = json.loads(row.reason)
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
        accepted_scope=binding.accepted_scope,
        head_sha=head_sha,
    )
    require(
        all(data.get(k) == v for k, v in expected.items()) and node.state == "running" and binding_scope_matches(binding, node),
        "recovery_authority_changed",
    )
    claim = await session.get(OrchestrationWorkClaim, context.identity.claim_id, populate_existing=True)
    owner = reservation_id(row.id)
    if successor_action is not None:
        from .review_cycle_dispatch import ACTOR, continuation_run_id, receipt_id

        committed = await session.get(OrchestrationDecision, receipt_id(successor_action.operation_key))
        saved = json.loads(committed.reason) if committed else {}
        require(
            committed is not None
            and committed.org_id == node.org_id
            and committed.node_id == node.id
            and committed.flow_id == node.flow_id
            and committed.actor_id == ACTOR
            and committed.actor_kind == "service"
            and committed.kind == "agent_dispatched"
            and saved.get("developer_recovery_decision_id") == row.id
            and successor_action.detail.get("developer_recovery_decision_id") == row.id
            and saved.get("run_id") == continuation_run_id(successor_action.operation_key),
            "recovery_dispatch_changed",
        )
        owner = saved["run_id"]
    require(
        claim is not None
        and claim.org_id == node.org_id
        and claim.owner_kind == "engine_flow"
        and claim.owner_ref == node.flow_id
        and claim.state == "held"
        and claim.generation == data["claim_generation"]
        and claim.active_run_id == owner
        and claim.claim_event_id == owner,
        "recovery_claim_changed",
    )
    raw, grant = await parent_authority(session, services=services, node=node, binding=binding, run_id=data["prior_run_id"])
    require(terminal_digest(raw) == data["terminal_digest"], "recovery_receipt_changed")
    values = await current_policy(
        session, services=services, context=context, node=node, binding=binding, run_id=owner, raw=raw, grant=grant, actor_id=row.actor_id
    )
    inputs = values[2]
    require(
        inputs.plan_version == data["plan_version"]
        and inputs.policy.policy_id == data["policy_id"]
        and inputs.policy.policy_hash == data["policy_hash"],
        "recovery_authority_changed",
    )
    return data, values


async def recovery_snapshot(session, context, node, binding, services, dispatches):
    if dispatches:
        return None
    rows = await session.scalars(
        select(OrchestrationDecision)
        .where(OrchestrationDecision.org_id == node.org_id, OrchestrationDecision.node_id == node.id, OrchestrationDecision.kind == KIND)
        .order_by(OrchestrationDecision.created_at.desc())
        .limit(100)
    )
    row = next((r for r in rows if json.loads(r.reason).get("claim_generation") == context.identity.claim_generation), None)
    if row is None:
        return None
    head = await services.head(binding)
    data, values = await verified_decision(session, decision_id=row.id, context=context, node=node, binding=binding, services=services, head_sha=head)
    inputs, meter = values[2], values[4]
    return dict(
        active_run_id=reservation_id(row.id),
        author_run_id=data["prior_run_id"],
        head_sha=head,
        worker_complete=True,
        remaining_spend_usd=str(inputs.policy.limits.max_spend_usd - meter.total_usd)
        if inputs.policy._budget_enforcement_enabled and meter
        else None,
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
    )


async def verify_transfer(session, *, decision_id, identity, execution, action, dispatch_decision, expected_run_id, completed_execution):
    """Bind the committed first-review action to its exact owner reservation.

    Provider/grant/policy checks run in preparation; this repeats the SQL and
    terminal evidence fences under the claim lock immediately before transfer.
    """
    row = await session.get(OrchestrationDecision, decision_id)
    data = json.loads(row.reason) if row else {}
    expected = dict(
        contract=CONTRACT,
        org_id=identity.org_id,
        flow_id=execution.flow_id,
        node_id=identity.node_id,
        attempt=identity.cycle,
        accepted_plan_version=identity.accepted_plan_version,
        execution_id=execution.id,
        claim_id=identity.claim_id,
        claim_generation=identity.claim_generation,
        binding_id=action.detail.get("binding_id"),
        binding_revision=action.detail.get("binding_revision"),
        accepted_scope=action.detail.get("accepted_scope"),
        head_sha=action.detail.get("head_sha"),
    )
    require(
        row is not None
        and row.org_id == identity.org_id
        and row.node_id == identity.node_id
        and row.flow_id == execution.flow_id
        and row.kind == KIND
        and row.actor_kind == "human"
        and row.actor_role in {"owner", "org_admin", "platform_admin"}
        and all(data.get(k) == v for k, v in expected.items())
        and expected_run_id == reservation_id(decision_id)
        and action.detail.get("developer_recovery_decision_id") == decision_id
        and action.detail.get("action") == "review"
        and action.detail.get("author_run_id") == data.get("prior_run_id")
        and json.loads(dispatch_decision.reason).get("developer_recovery_decision_id") == decision_id
        and terminal_digest(completed_execution) == data.get("terminal_digest"),
        "recovery_authority_changed",
    )
    from .policy_admission import load_in_force_policy

    inputs = await load_in_force_policy(session, org_id=identity.org_id, flow_id=execution.flow_id)
    flow = await session.get(OrchestrationFlow, execution.flow_id, populate_existing=True)
    require(
        inputs.refusal is None
        and inputs.policy is not None
        and inputs.plan_version == data["plan_version"]
        and inputs.policy.policy_id == data["policy_id"]
        and inputs.policy.policy_hash == data["policy_hash"]
        and await policy_owner_matches(session, row.actor_id, inputs.policy)
        and flow is not None
        and not flow.execution_paused
        and flow.archived_at is None,
        "recovery_authority_changed",
    )
