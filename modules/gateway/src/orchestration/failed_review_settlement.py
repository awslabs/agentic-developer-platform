"""Owner settlement of an exited failed reviewer; no retry or completion grant."""

import json
import os
import re
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

from botocore.exceptions import BotoCoreError, ClientError
from httpx import HTTPError
from sqlalchemy import select

from src.agentauth.bootstrap import envelope_digest
from src.agentauth.github_provider import ProviderUnavailableError
from src.shared.models.base import utcnow

from .continuation import digest
from .dispatch_pass import attempt_run_id
from .execution_state import ExecutionIdentity, ExecutionPhase, ExecutionStatus, OutcomeKind, PhaseAdvance
from .execution_store import advance_execution, load_execution, supersede_replaced_admission
from .handoff import identity_for_attempt
from .merge_provider import MergeProvider
from .models import OrchestrationAction, OrchestrationDecision, OrchestrationExecution, OrchestrationFlow, OrchestrationNode, OrchestrationWorkClaim
from .policy_admission import load_in_force_policy
from .pr_bindings import active_binding_for_node, binding_scope_matches
from .protected_review_recovery import require
from .released_reviewer_evidence import action_snapshot, committed_dispatch
from .review_cycle import DISPATCH_KIND, PHASES, CycleBlockedError
from .review_cycle_dispatch import ReviewCycleServices, continuation_run_id, failed_review
from .shared_window import policy_owner_matches
from .state import ActorKind, NodeState, transition
from .unstarted_review import retired_unstarted_dispatch
from .work_claims import Disposition, ReleaseReason, release_work

CONTRACT = "failed-review-settlement/v1"
KIND = "failed_review_settled"


async def current(session, *, org_id, node_id, actor_id, request, lock=False):
    query = select(OrchestrationNode).where(OrchestrationNode.org_id == org_id, OrchestrationNode.id == node_id)
    node = await session.scalar((query.with_for_update(skip_locked=True) if lock else query).execution_options(populate_existing=True))
    require(node is not None and node.kind == "story" and node.state in {"running", "awaiting_merge"}, "review_settlement_story_not_current")
    require(node.attempts == request.expected_attempt, "report_assignment_changed")
    identity = await identity_for_attempt(session, org_id=org_id, node_id=node_id, attempt=node.attempts)
    require(identity is not None, "current_execution_changed")
    loaded = await load_execution(session, identity=identity, for_update=lock)
    require(loaded is not None and loaded.kind is OutcomeKind.APPLIED and loaded.record is not None, "current_execution_changed")
    execution = loaded.record
    require(execution.phase in PHASES and execution.status not in {"concluded", "superseded"}, "review_settlement_phase_changed")
    require(execution.pending_action_key is None, "pending_effect_requires_reconciliation")
    claim = await session.get(OrchestrationWorkClaim, identity.claim_id, populate_existing=True, with_for_update=lock)
    require(
        claim is not None
        and claim.org_id == org_id
        and claim.owner_kind == "engine_flow"
        and claim.owner_ref == node.flow_id
        and claim.state == "held"
        and claim.generation == identity.claim_generation
        and claim.active_run_id == request.expected_run_id
        and claim.claim_event_id == request.expected_run_id,
        "reviewer_claim_changed",
    )
    binding = await active_binding_for_node(session, org_id=org_id, node_id=node_id, attempt=node.attempts)
    require(binding is not None and binding.role == "implementation" and binding_scope_matches(binding, node), "bound_delivery_changed")
    require(
        claim.provider_repository_id == binding.provider_repository_id and claim.issue_number == int(str(node.issue_ref).lstrip("#")),
        "reviewer_claim_changed",
    )
    # Settlement spends nothing and grants no further execution. In particular,
    # exhausted budgets, retry allowances and expired execution windows cannot
    # prevent recording the terminal failure of work already admitted.
    inputs = await load_in_force_policy(session, org_id=org_id, flow_id=node.flow_id)
    require(inputs.refusal is None and inputs.policy is not None and inputs.plan_version == request.expected_plan_version, "accepted_policy_changed")
    require(await policy_owner_matches(session, actor_id, inputs.policy), "recovery_policy_owner_required")
    query = (
        select(OrchestrationAction)
        .where(OrchestrationAction.org_id == org_id, OrchestrationAction.execution_id == execution.id)
        .order_by(OrchestrationAction.created_at, OrchestrationAction.id)
        .limit(101)
    )
    actions = list(await session.scalars((query.with_for_update() if lock else query).execution_options(populate_existing=True)))
    require(len(actions) <= 100 and all(row.status in {"succeeded", "failed"} for row in actions), "pending_effect_requires_reconciliation")
    # A merge/deployment or any other effect requires its own reconciliation.
    require(
        all(row.kind in {DISPATCH_KIND, "review_evidence", "notify_execution_block", "merge_repair_request"} for row in actions),
        "delivery_effect_requires_reconciliation",
    )
    dispatches = [row for row in actions if row.kind == DISPATCH_KIND and not retired_unstarted_dispatch(row)]
    require(
        dispatches and continuation_run_id(dispatches[-1].operation_key) == request.expected_run_id,
        "reviewer_dispatch_changed",
    )
    _, dispatch = await committed_dispatch(
        session, identity=identity, execution=execution, node=node, binding=binding, action=dispatches[-1], generation=identity.claim_generation
    )
    # An older admission may have been replaced before its result observer ran.
    # Do not ignore it: the same settlement must prove it exited and has no
    # effects or current ownership before retiring its obsolete lifecycle.
    query = (
        select(OrchestrationExecution)
        .where(
            OrchestrationExecution.org_id == org_id,
            OrchestrationExecution.node_id == node_id,
            OrchestrationExecution.id != execution.id,
            OrchestrationExecution.status.not_in(("concluded", "superseded")),
        )
        .order_by(OrchestrationExecution.cycle)
        .limit(26)
    )
    predecessors = list(await session.scalars((query.with_for_update() if lock else query).execution_options(populate_existing=True)))
    require(len(predecessors) <= 25, "replaced_admission_history_limit")
    retired = []
    for old in predecessors:
        require(
            old.flow_id == node.flow_id
            and old.cycle < identity.cycle
            and old.claim_id == identity.claim_id
            and old.claim_generation < identity.claim_generation
            and old.phase == "admitted"
            and old.status in {"runnable", "awaiting_external"}
            and old.attempts == 0
            and old.pending_action_key is None
            and old.handoff_receipt_ref is None
            and old.notification_receipt_ref is None,
            "replaced_admission_requires_reconciliation",
        )
        action = await session.scalar(
            select(OrchestrationAction.id)
            .where(
                OrchestrationAction.org_id == org_id,
                OrchestrationAction.execution_id == old.id,
            )
            .limit(1)
        )
        require(action is None, "replaced_admission_requires_reconciliation")
        retired.append(
            dict(
                execution_id=old.id,
                revision=old.revision,
                cycle=old.cycle,
                accepted_plan_version=old.accepted_plan_version,
                claim_id=old.claim_id,
                claim_generation=old.claim_generation,
                run_id=attempt_run_id(node.id, old.cycle),
            )
        )
    snapshot = dict(
        replaced_admissions=retired,
        contract=CONTRACT,
        org_id=org_id,
        flow_id=node.flow_id,
        node_id=node.id,
        node_state=node.state,
        node_attempt=node.attempts,
        plan_version=inputs.plan_version,
        policy_digest=digest(inputs.policy.model_dump(mode="json")),
        execution_id=execution.id,
        execution_revision=execution.revision,
        execution_phase=execution.phase,
        execution_status=execution.status,
        execution_attempts=execution.attempts,
        handoff_receipt_ref=execution.handoff_receipt_ref,
        claim_id=identity.claim_id,
        claim_generation=identity.claim_generation,
        claim_event_id=claim.claim_event_id,
        reviewer_run_id=request.expected_run_id,
        binding_id=binding.id,
        binding_revision=binding.revision,
        binding_head_sha=binding.head_sha,
        accepted_scope=binding.accepted_scope,
        actions_digest=digest([action_snapshot(row) for row in actions]),
        dispatch_digest=digest(dispatch),
    )
    return snapshot, node, identity, execution, binding, dispatch


def terminal_record(raw, *, expected, outcomes, code):
    try:
        require(
            isinstance(raw, dict)
            and all(raw.get(key) == value for key, value in expected.items())
            and raw.get("status") == {"S": "completed"}
            and raw.get("terminal_outcome", {}).get("S") in outcomes
            and raw.get("workload_binding", {}).get("S")
            and re.fullmatch(r"[a-f0-9]{64}", raw.get("terminal_report_digest", {}).get("S", "")),
            code,
        )
        recorded = datetime.fromisoformat(raw["terminal_recorded_at"]["S"].replace("Z", "+00:00"))
        require(recorded.tzinfo is not None and recorded <= datetime.now(UTC), code)
    except (KeyError, TypeError, ValueError, AttributeError):
        raise CycleBlockedError(code) from None
    return dict(terminal_digest=digest(raw), terminal_recorded_at=recorded.isoformat(), terminal_outcome=raw["terminal_outcome"]["S"])


async def terminal(services, node, binding, request, dispatch, predecessors):
    require(os.environ.get("AGENT_AUTHORITY_ENABLED", "false").lower() == "true", "protected_authority_required")
    raw = await services.protected(node.org_id, request.expected_run_id)
    require(isinstance(raw, dict) and failed_review(raw), "failed_reviewer_terminal_unverified")
    evidence = terminal_record(
        raw,
        expected=dict(
            **dispatch["execution_metadata"],
            invocation_id={"S": request.expected_run_id},
            tenant_id={"S": node.org_id},
            repo={"S": binding.repo},
            flow_id={"S": node.flow_id},
            envelope_digest={"S": envelope_digest(dispatch["envelope"])},
            parent_principal={"S": dispatch["parent_principal"]},
        ),
        outcomes={"failed", "budget_stopped"},
        code="failed_reviewer_terminal_unverified",
    )
    old_evidence = []
    for old in predecessors:
        raw = await services.protected(node.org_id, old["run_id"])
        proof = terminal_record(
            raw,
            expected=dict(
                invocation_id={"S": old["run_id"]},
                tenant_id={"S": node.org_id},
                flow_id={"S": node.flow_id},
                orchestration_node_id={"S": node.id},
                orchestration_node_attempt={"N": str(old["cycle"])},
                repo={"S": binding.repo},
            ),
            outcomes={"complete", "failed", "budget_stopped"},
            code="replaced_admission_exit_unverified",
        )
        old_evidence.append(dict(**old, **proof))
    return dict(**evidence, retired_admission_evidence=old_evidence)


async def request_settlement(session, *, org_id, node_id, actor_id, actor_role, request, accept=False, services=None, provider=None):
    require(actor_id and actor_role in {"owner", "org_admin", "platform_admin"}, "human_plan_approver_required")
    require(request.mode == "settle_failed", "explicit_failure_settlement_required")
    require(request.source_run_id is None and request.renew_decision_id is None and request.reviewer_model is None, "unexpected_settlement_input")
    services, provider = services or ReviewCycleServices(None), provider or MergeProvider()
    request_data = request.model_dump(mode="json")
    decision_id = str(
        uuid5(NAMESPACE_URL, CONTRACT + ":" + digest(dict(org=org_id, node=node_id, actor=actor_id, snapshot=request.expected_snapshot)))
    )

    async def replay():
        prior = await session.get(OrchestrationDecision, decision_id, populate_existing=True)
        if prior is None:
            return None
        require(
            (prior.org_id, prior.node_id, prior.actor_id, prior.actor_role, prior.actor_kind, prior.kind)
            == (org_id, node_id, actor_id, actor_role, "human", KIND)
            and json.loads(prior.reason).get("request") == request_data,
            "recovery_receipt_conflict",
        )
        return dict(accepted=True, created=False, decision_id=prior.id)

    if accept:
        require(request.expected_snapshot, "preview_snapshot_required")
        prior = await replay()
        if prior:
            return prior
    arguments = dict(org_id=org_id, node_id=node_id, actor_id=actor_id, request=request)
    data, node, identity, execution, binding, dispatch = await current(session, **arguments)
    try:
        evidence = await terminal(services, node, binding, request, dispatch, data["replaced_admissions"])
        remote = await provider.read(binding)
        require(remote.open and not remote.merged and remote.queue_id is None and remote.head_sha == request.expected_head_sha, "current_pr_changed")
        require(evidence == await terminal(services, node, binding, request, dispatch, data["replaced_admissions"]), "reviewer_terminal_changed")
    except (BotoCoreError, ClientError, HTTPError, ProviderUnavailableError):
        raise CycleBlockedError("settlement_evidence_unavailable") from None
    snapshot = digest(dict(**data, **evidence, head_sha=remote.head_sha))
    preview = dict(
        **data,
        **evidence,
        head_sha=remote.head_sha,
        snapshot=snapshot,
        attempts_preserved=True,
        receipts_preserved=True,
        next_action="record_failed_node_only",
    )
    if not accept:
        return preview
    require(snapshot == request.expected_snapshot, "recovery_preview_changed")
    # Match the engine's flow-before-execution lock order. Busy state asks the
    # caller to retry; never wait for a node while holding a dispatch flow lock.
    flow = await session.scalar(
        select(OrchestrationFlow)
        .where(
            OrchestrationFlow.org_id == org_id,
            OrchestrationFlow.id == data["flow_id"],
            OrchestrationFlow.archived_at.is_(None),
        )
        .with_for_update(skip_locked=True)
    )
    require(flow is not None, "review_settlement_busy")
    prior = await replay()
    if prior:
        return prior
    locked, node, identity, execution, _, _ = await current(session, **arguments, lock=True)
    require(locked == data, "recovery_preview_changed")
    move = transition(node.state, NodeState.FAILED, actor_kind=ActorKind.HUMAN, reason=request.reason)
    require(move.allowed, "failure_transition_refused")
    concluded = await advance_execution(
        session,
        identity=identity,
        advance=PhaseAdvance(
            phase=ExecutionPhase.CONCLUDED,
            status=ExecutionStatus.CONCLUDED,
            expected_revision=execution.revision,
            progress_note="Owner recorded the current reviewer's verified terminal failure; delivery remains incomplete.",
        ),
    )
    require(concluded.kind is OutcomeKind.APPLIED, "reviewer_execution_changed")
    released = await release_work(
        session,
        org_id=org_id,
        claim_id=identity.claim_id,
        generation=identity.claim_generation,
        reason=ReleaseReason.FAILED,
        terminal_evidence=decision_id + ":" + evidence["terminal_digest"],
    )
    require(released.disposition is Disposition.ADMITTED, "reviewer_claim_changed")
    previous_state = node.state
    node.state, node.updated_at = NodeState.FAILED.value, utcnow()
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
            from_state=previous_state,
            to_state=NodeState.FAILED.value,
            reason=json.dumps(dict(**preview, request=request_data)),
        )
    )
    await session.flush()
    for old in data["replaced_admissions"]:
        changed = await supersede_replaced_admission(
            session,
            identity=ExecutionIdentity(org_id, node_id, old["cycle"], old["accepted_plan_version"], old["claim_id"], old["claim_generation"]),
            expected_revision=old["revision"],
            successor_id=execution.id,
            successor_revision=concluded.record.revision,
            successor_cycle=identity.cycle,
            issue_number=int(str(node.issue_ref).lstrip("#")),
            failed_settlement_decision_id=decision_id,
        )
        require(changed, "replaced_admission_changed")
    return dict(accepted=True, created=True, decision_id=decision_id, **preview)
