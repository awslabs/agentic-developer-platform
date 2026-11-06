"""Owner-authorized continuation of an unstarted deployment after claim release.

The new generation belongs only to the engine. The original worker, merge and
review evidence remain immutable; a recorded bridge names their exact authority.
"""

import asyncio
import json
from dataclasses import asdict, replace
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

from botocore.exceptions import BotoCoreError, ClientError
from httpx import HTTPError
from sqlalchemy import select

from src.agentauth.github_provider import ProviderConflictError, ProviderUnavailableError

from .continuation import digest
from .execution_policy import Action
from .execution_state import UNRESOLVED_ACTION_STATUSES, ExecutionPhase, ExecutionStatus, OutcomeKind, PhaseAdvance
from .execution_store import advance_execution, load_execution
from .merge_controller import MergeVerificationPendingError
from .merge_evidence import EvidenceUnavailableError
from .models import OrchestrationAction, OrchestrationDecision, OrchestrationFlow, OrchestrationWorkClaim
from .protected_merge_recovery import ReleasedMergeObserver, current, snapshot_verification, terminal
from .protected_review_recovery import require
from .review_cycle import CycleBlockedError
from .review_cycle_dispatch import ReviewCycleServices
from .work_claims import ClaimBinding, ClaimOwner, Disposition, OwnerKind, ReleaseReason, bind_run, claim_work, release_work

KIND = "delivery_continuation_recovered"
CONTRACT = "protected-delivery-recovery/v1"
PREFIX = "delivery-recovery:"


def binding_digest(binding):
    return digest(
        {
            key: getattr(binding, key)
            for key in (
                "id",
                "revision",
                "run_id",
                "repo",
                "installation_id",
                "provider_repository_id",
                "provider_pr_node_id",
                "pr_number",
            )
        }
    )


async def request_recovery(session, *, org_id, node_id, actor_id, actor_role, request, accept=False, services=None):
    from .merge_controller import MergeServices

    require(actor_id and actor_role in {"owner", "org_admin", "platform_admin"}, "human_plan_approver_required")
    require(request.mode == "resume_delivery" and request.reviewer_model is None, "explicit_delivery_recovery_required")
    require(request.source_run_id is None and request.renew_decision_id is None, "unexpected_delivery_recovery_input")
    request_data = request.model_dump(mode="json")
    decision_id = str(
        uuid5(NAMESPACE_URL, CONTRACT + ":" + digest(dict(org=org_id, node=node_id, actor=actor_id, snapshot=request.expected_snapshot)))
    )

    async def replay():
        old = await session.get(OrchestrationDecision, decision_id, populate_existing=True)
        if old is None:
            return None
        require(
            (old.org_id, old.node_id, old.actor_id, old.actor_role, old.actor_kind, old.kind)
            == (org_id, node_id, actor_id, actor_role, "human", KIND)
            and json.loads(old.reason).get("request") == request_data,
            "recovery_receipt_conflict",
        )
        return dict(accepted=True, created=False, decision_id=old.id)

    if accept:
        require(request.expected_snapshot, "preview_snapshot_required")
        previous = await replay()
        if previous:
            return previous
    arguments = dict(org_id=org_id, node_id=node_id, actor_id=actor_id, request=request)
    observer = ReleasedMergeObserver(services or MergeServices(None, authority=ReviewCycleServices(None)), arguments)
    try:
        data, node, context, binding, dispatch = await current(session, **arguments)
        authority = await observer.authority_for(context)
        terminal_data = await terminal(authority, node=node, binding=binding, request=request, dispatch=dispatch)
        require(
            datetime.fromisoformat(terminal_data["terminal_recorded_at"]) <= datetime.fromisoformat(data["claim_released_at"]),
            "reviewer_release_precedes_terminal",
        )
        state = await observer.provider.read(binding)
        require(state.merged and state.head_sha == request.expected_head_sha, "merged_pr_identity_changed")
        receipt, verification = await observer.completed_elsewhere(session, context, node, binding, request.expected_run_id, state)
        require(verification["review_state"] == "approved", "merged_pr_review_not_approved")
        original = data["existing_merge_receipt"]
        require(
            all(
                getattr(receipt, key) == original[key]
                for key in (
                    "merge_sha",
                    "reviewed_head_sha",
                    "repo",
                    "pr_number",
                    "provider_repository_id",
                    "provider_pr_node_id",
                    "review_ref",
                )
            ),
            "existing_merge_changed",
        )
        require(
            terminal_data == await terminal(authority, node=node, binding=binding, request=request, dispatch=dispatch), "reviewer_terminal_changed"
        )
    except MergeVerificationPendingError:
        raise CycleBlockedError("merged_pr_checks_pending") from None
    except (ProviderConflictError, ProviderUnavailableError, EvidenceUnavailableError, HTTPError, BotoCoreError, ClientError):
        raise CycleBlockedError("merge_evidence_unavailable") from None
    verification = {key: value for key, value in verification.items() if key != "observed_at"}
    snapshot = digest(dict(**data, **terminal_data, provider_verification=snapshot_verification(verification)))
    preview = dict(
        **data,
        **terminal_data,
        snapshot=snapshot,
        provider_verification=verification,
        next_claim_generation=context.identity.claim_generation + 1,
        attempts_preserved=True,
        receipts_preserved=True,
        next_action="engine_deployment_only",
    )
    if not accept:
        return preview
    require(snapshot == request.expected_snapshot, "recovery_preview_changed")
    await load_execution(session, identity=context.identity, for_update=True, released_failure_run_id=request.expected_run_id)
    previous = await replay()
    if previous:
        return previous
    locked, _, _, _, _ = await current(session, **arguments, lock=True)
    require(locked == data, "recovery_preview_changed")
    event = PREFIX + decision_id
    claimed = await claim_work(
        session,
        binding=ClaimBinding(org_id, binding.provider_repository_id, int(str(node.issue_ref).lstrip("#"))),
        owner=ClaimOwner(OwnerKind.ENGINE_FLOW, node.flow_id),
        event_id=event,
    )
    require(
        claimed.disposition is Disposition.ADMITTED
        and claimed.claim_id == context.identity.claim_id
        and claimed.generation == preview["next_claim_generation"],
        "recovery_claim_changed",
    )
    bound = await bind_run(session, org_id=org_id, claim_id=claimed.claim_id, generation=claimed.generation, run_id=event)
    require(bound.disposition is Disposition.ADMITTED, "recovery_claim_changed")
    session.add(
        OrchestrationDecision(
            id=decision_id,
            org_id=org_id,
            flow_id=node.flow_id,
            node_id=node_id,
            kind=KIND,
            actor_kind="human",
            actor_id=actor_id,
            actor_role=actor_role,
            reason=json.dumps(preview | dict(request=request_data, contract=CONTRACT, reviewer_dispatch=dispatch)),
        )
    )
    updated = await advance_execution(
        session,
        identity=replace(context.identity, claim_generation=claimed.generation),
        advance=PhaseAdvance(
            phase=ExecutionPhase.DEPLOYMENT_PENDING,
            status=ExecutionStatus.RUNNABLE,
            expected_revision=context.execution.revision,
            next_check_at=datetime.now(UTC),
        ),
    )
    require(updated.kind is OutcomeKind.APPLIED, "recovery_assignment_changed")
    await session.flush()
    return dict(accepted=True, created=True, decision_id=decision_id, **preview)


async def verified_bridge(session, *, identity, node, binding, receipt):
    """Resolve only the exact historical merge licensed to this new generation."""
    claim = await session.get(OrchestrationWorkClaim, identity.claim_id, populate_existing=True)
    require(
        claim is not None
        and claim.state == "held"
        and claim.owner_kind == "engine_flow"
        and claim.owner_ref == node.flow_id
        and claim.org_id == node.org_id
        and claim.generation == identity.claim_generation
        and claim.claim_event_id == claim.active_run_id
        and (claim.active_run_id or "").startswith(PREFIX),
        "delivery_recovery_claim_changed",
    )
    decision = await session.get(OrchestrationDecision, claim.active_run_id.removeprefix(PREFIX), populate_existing=True)
    require(decision is not None, "delivery_recovery_decision_changed")
    data = await verified_merge_history(session, identity=identity, node=node, binding=binding, receipt=receipt, decision=decision)
    flow = await session.get(OrchestrationFlow, node.flow_id, populate_existing=True)
    require(flow is not None and flow.state in {"pending", "running"} and not flow.execution_paused and flow.archived_at is None, "flow_unavailable")
    return data, claim.active_run_id


async def verified_merge_history(session, *, identity, node, binding, receipt, decision=None):
    """Read historical evidence only; this never grants a claim or action authority."""
    if decision is None:
        rows = list(
            await session.scalars(
                select(OrchestrationDecision)
                .where(
                    OrchestrationDecision.org_id == identity.org_id,
                    OrchestrationDecision.flow_id == node.flow_id,
                    OrchestrationDecision.node_id == node.id,
                    OrchestrationDecision.kind == KIND,
                )
                .limit(101)
            )
        )
        require(len(rows) <= 100, "delivery_recovery_history_limit")
        matches = [row for row in rows if json.loads(row.reason).get("next_claim_generation") == identity.claim_generation]
        require(len(matches) == 1, "delivery_recovery_decision_changed")
        decision = matches[0]
    require(
        decision is not None
        and decision.org_id == node.org_id
        and decision.node_id == node.id
        and decision.flow_id == node.flow_id
        and decision.kind == KIND
        and decision.actor_kind == "human"
        and decision.actor_role in {"owner", "org_admin", "platform_admin"},
        "delivery_recovery_decision_changed",
    )
    data = json.loads(decision.reason)
    require(
        data.get("contract") == CONTRACT
        and data.get("claim_id") == identity.claim_id
        and data.get("claim_generation") + 1 == data.get("next_claim_generation") == identity.claim_generation
        and data.get("accepted_plan_version") == identity.accepted_plan_version
        and data.get("attempt") == identity.cycle
        and data.get("node_id") == node.id
        and data.get("flow_id") == node.flow_id
        and data.get("org_id") == identity.org_id
        and data.get("existing_merge_receipt") == receipt.model_dump(mode="json")
        and data.get("binding_digest") == binding_digest(binding)
        and data.get("accepted_scope") == binding.accepted_scope
        and data.get("head_sha") == binding.head_sha
        and data.get("binding_id") == binding.id
        and data.get("binding_revision") == binding.revision
        and node.kind == "story"
        and node.state == "passed"
        and node.attempts == identity.cycle,
        "delivery_recovery_binding_changed",
    )
    require(
        all(getattr(receipt, key) == value for key, value in asdict(identity).items() if key != "claim_generation")
        and receipt.claim_generation == data["claim_generation"],
        "delivery_recovery_merge_changed",
    )
    action = await session.scalar(
        select(OrchestrationAction).where(
            OrchestrationAction.org_id == identity.org_id,
            OrchestrationAction.execution_id == data["execution_id"],
            OrchestrationAction.operation_key == receipt.operation_key,
            OrchestrationAction.kind == "merge_pull_request",
            OrchestrationAction.status == "succeeded",
        )
    )
    require(action is not None and (action.detail or {}).get("merge_receipt") == data["existing_merge_receipt"], "delivery_recovery_merge_changed")
    return data


async def authority_context(services, session, context, node, binding, run_id, action):
    from .deployment_authority import load_delivery_merge
    from .review_recovery import ReviewRecoveryRequest

    require(action in {Action.DEPLOY, Action.EVALUATE}, "delivery_recovery_action_not_authorized")
    _, _, receipt = await load_delivery_merge(session, identity=context.identity, node=node)
    data, expected_run = await verified_bridge(session, identity=context.identity, node=node, binding=binding, receipt=receipt)
    require(run_id == expected_run and context.execution.id == data["execution_id"], "delivery_recovery_assignment_changed")
    request = ReviewRecoveryRequest.model_validate(data["request"])
    observed = await terminal(services, node=node, binding=binding, request=request, dispatch=data["reviewer_dispatch"])
    require(observed["terminal_digest"] == data["terminal_digest"], "reviewer_terminal_changed")
    raw = await services.protected(node.org_id, data["reviewer_run_id"])
    grant = await asyncio.to_thread(
        services.writer.store.live_grant,
        invocation_id=data["reviewer_run_id"],
        tenant_id=node.org_id,
        attempt=int(raw["current_attempt"]["N"]),
        now=datetime.now(UTC),
    )
    require(digest(raw) == data["terminal_digest"], "reviewer_terminal_changed")
    return await services.policy_context(session, context, node, binding, run_id, action, raw, grant)


async def close_recovered_delivery(session, record):
    """The engine owns this synthetic run, so no worker callback will release it."""
    if record.phase != ExecutionPhase.CONCLUDED or record.status != ExecutionStatus.CONCLUDED or record.pending_action_key:
        return
    claim = await session.get(OrchestrationWorkClaim, record.claim_id, populate_existing=True)
    if claim is None or not (claim.active_run_id or "").startswith(PREFIX):
        return
    require(
        (claim.org_id, claim.owner_kind, claim.owner_ref, claim.generation, claim.state)
        == (record.org_id, "engine_flow", record.flow_id, record.claim_generation, "held")
        and claim.claim_event_id == claim.active_run_id,
        "delivery_recovery_claim_changed",
    )
    decision = await session.get(OrchestrationDecision, claim.active_run_id.removeprefix(PREFIX))
    require(
        decision is not None
        and decision.kind == KIND
        and decision.actor_kind == "human"
        and (decision.org_id, decision.flow_id, decision.node_id) == (record.org_id, record.flow_id, record.node_id),
        "delivery_recovery_decision_changed",
    )
    data = json.loads(decision.reason)
    require(
        data.get("contract") == CONTRACT
        and data.get("execution_id") == record.id
        and data.get("claim_id") == claim.id
        and data.get("next_claim_generation") == claim.generation,
        "delivery_recovery_assignment_changed",
    )
    unresolved = await session.scalar(
        select(OrchestrationAction.id)
        .where(
            OrchestrationAction.org_id == record.org_id,
            OrchestrationAction.execution_id == record.id,
            OrchestrationAction.status.in_([status.value for status in UNRESOLVED_ACTION_STATUSES]),
        )
        .limit(1)
    )
    require(unresolved is None, "pending_effect_requires_reconciliation")
    released = await release_work(
        session,
        org_id=record.org_id,
        claim_id=record.claim_id,
        generation=record.claim_generation,
        reason=ReleaseReason.COMPLETED,
        terminal_evidence="engine delivery concluded:" + record.id,
    )
    require(released.disposition is Disposition.ADMITTED, "delivery_recovery_release_changed")
