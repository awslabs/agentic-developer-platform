"""Owner acceptance of an already-merged delivery after premature claim release.

No claim is acquired and no work is replayed. The original review's generation,
execution, attempts and spend remain authoritative. All provider I/O precedes
the final locked SQL comparison and atomic terminal settlement.
"""

import asyncio
import json
import os
import re
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

from botocore.exceptions import BotoCoreError, ClientError
from httpx import HTTPError
from sqlalchemy import select

from src.agentauth.bootstrap import BootstrapRefusedError
from src.agentauth.github_provider import ProviderConflictError, ProviderUnavailableError

from .continuation import digest
from .execution_policy import Action
from .execution_runner import RunnerContext
from .execution_state import OutcomeKind
from .execution_store import load_execution, settle_released_merge
from .handoff import identity_for_attempt
from .merge_controller import MERGE_KIND, MergeServices, MergeVerificationPendingError, code_only_delivery, settle_merge
from .merge_evidence import SEMANTIC_SOURCE_KINDS, EvidenceUnavailableError
from .models import (
    OrchestrationAcceptedPlan,
    OrchestrationAction,
    OrchestrationDecision,
    OrchestrationFlow,
    OrchestrationNode,
    OrchestrationWorkClaim,
)
from .policy_admission import load_in_force_policy
from .pr_bindings import active_binding_for_node, binding_scope_matches
from .protected_review_recovery import require
from .review_cycle import DISPATCH_KIND, CycleBlockedError
from .review_cycle_dispatch import ACTOR, ReviewCycleServices, continuation_run_id, receipt_id
from .shared_window import policy_owner_matches
from .state import ActorKind, NodeState, transition

KIND = "merged_delivery_settled"
CONTRACT = "protected-merged-delivery-settlement/v1"


def snapshot_verification(verification):
    """Compare stable provider evidence, preserving every raw source for audit."""
    sources = []
    semantic_sources = dict.fromkeys(SEMANTIC_SOURCE_KINDS, 0)
    for source in verification["check_sources"]:
        kind = source.get("kind") if isinstance(source, dict) else None
        require(isinstance(kind, str) and kind in SEMANTIC_SOURCE_KINDS, "merge_evidence_unavailable")
        semantic_sources[kind] += 1
        raw = source.get("digest")
        semantic = source.get("semantic_digest")
        require(
            isinstance(raw, str) and re.fullmatch(r"[0-9a-f]{64}", raw) and isinstance(semantic, str) and re.fullmatch(r"[0-9a-f]{64}", semantic),
            "merge_evidence_unavailable",
        )
        sources.append({**source, "digest": semantic})
    require(
        all(
            semantic_sources[kind] == 1
            for kind in ("pull_request", "pull_request_recheck", "repository_settings", "base_branch", "base_branch_recheck", "review_requirements")
        )
        and all(semantic_sources[kind] >= 1 for kind in ("branch_rules", "check_runs", "statuses", "reviews"))
        and semantic_sources["branch_protection"] <= 1
        and semantic_sources["rules_capability_verification"] <= 1
        and semantic_sources["branch_protection"] + semantic_sources["rules_capability_verification"] >= 1,
        "merge_evidence_unavailable",
    )
    return {**verification, "check_sources": sources}


async def legacy_stall_provenance(session, node, execution, claim, *, lock=False):
    """Bind only an unfenced legacy deadline projection of this completed assignment."""
    if node.state != "failed":
        return None
    query = (
        select(OrchestrationDecision)
        .where(
            OrchestrationDecision.org_id == node.org_id,
            OrchestrationDecision.flow_id == node.flow_id,
            OrchestrationDecision.node_id == node.id,
            OrchestrationDecision.to_state.is_not(None),
            OrchestrationDecision.rejection_reason.is_(None),
        )
        .order_by(OrchestrationDecision.created_at.desc(), OrchestrationDecision.id.desc())
        .limit(1)
    )
    event = await session.scalar((query.with_for_update() if lock else query).execution_options(populate_existing=True))
    require(
        event is not None
        and event.kind == "node_stalled"
        and event.actor_kind == "service"
        and event.actor_id == "system:orchestration-stall-detector"
        and event.actor_role == "engine"
        and event.from_state == "running"
        and event.to_state == "failed",
        "merge_settlement_stall_provenance_changed",
    )
    reason = re.fullmatch(r"stalled: ([0-9]+)s in 'running' exceeds the ([0-9]+)s threshold \(agent pod deadline ([0-9]+)s\)", event.reason or "")
    require(reason is not None, "merge_settlement_stall_provenance_changed")
    elapsed, threshold, deadline = map(int, reason.groups())
    require(elapsed > threshold > 0 and deadline > threshold, "merge_settlement_stall_provenance_changed")
    require(
        execution.created_at is not None
        and claim.released_at <= event.created_at <= datetime.now(UTC)
        and execution.created_at <= event.created_at
        and (execution.updated_at is None or execution.updated_at <= event.created_at),
        "merge_settlement_stall_provenance_changed",
    )
    return {
        "decision_id": event.id,
        "decision_digest": digest(
            {
                key: getattr(event, key)
                for key in (
                    "id",
                    "org_id",
                    "flow_id",
                    "node_id",
                    "kind",
                    "actor_kind",
                    "actor_id",
                    "actor_role",
                    "from_state",
                    "to_state",
                    "reason",
                    "rejection_reason",
                )
            }
            | {"created_at": event.created_at.isoformat()}
        ),
        "created_at": event.created_at.isoformat(),
    }


async def current(session, *, org_id, node_id, request, actor_id, lock=False):
    delivery = request.mode == "resume_delivery"
    identity = await identity_for_attempt(session, org_id=org_id, node_id=node_id, attempt=request.expected_attempt)
    require(identity is not None, "current_execution_changed")
    loaded = await load_execution(session, identity=identity, for_update=lock, released_failure_run_id=request.expected_run_id)
    require(loaded is not None and loaded.kind is OutcomeKind.APPLIED and loaded.record is not None, "current_execution_changed")
    execution = loaded.record
    # A successful reviewer may have released ownership before the engine
    # observed its approval. The same terminal-review and merge evidence below
    # is required whether or not that observation advanced to merge_ready.
    require(
        (execution.phase, execution.status)
        in (
            {("deployment_pending", "runnable")}
            if delivery
            else {("merge_ready", "runnable"), ("awaiting_review", "runnable"), ("awaiting_review", "awaiting_external")}
        ),
        "merge_settlement_phase_changed",
    )
    require(execution.pending_action_key is None, "pending_effect_requires_reconciliation")
    query = select(OrchestrationNode).where(OrchestrationNode.org_id == org_id, OrchestrationNode.id == node_id)
    node = await session.scalar((query.with_for_update() if lock else query).execution_options(populate_existing=True))
    require(
        node is not None
        and node.kind == "story"
        and node.state in ({"passed"} if delivery else {"running", "awaiting_merge", "failed"})
        and node.attempts == identity.cycle,
        "merge_settlement_story_not_current",
    )
    flow = await session.get(OrchestrationFlow, node.flow_id, populate_existing=True)
    require(flow is not None and flow.state in {"pending", "running"} and not flow.execution_paused and flow.archived_at is None, "flow_unavailable")
    claim = await session.get(OrchestrationWorkClaim, identity.claim_id, populate_existing=True)
    require(
        claim is not None
        and claim.state == "released"
        and claim.release_reason == "completed"
        and claim.released_at is not None
        and claim.active_run_id is None
        and claim.claim_event_id == request.expected_run_id
        and claim.generation == identity.claim_generation,
        "released_reviewer_assignment_changed",
    )
    legacy_stall = await legacy_stall_provenance(session, node, execution, claim, lock=lock)
    binding = await active_binding_for_node(session, org_id=org_id, node_id=node_id, attempt=node.attempts)
    require(
        binding is not None
        and binding.role == "implementation"
        and binding_scope_matches(binding, node)
        and binding.head_sha == request.expected_head_sha,
        "bound_delivery_changed",
    )
    context = RunnerContext(identity, execution, datetime.now(UTC))
    inputs = await load_in_force_policy(session, org_id=org_id, flow_id=node.flow_id)
    require(
        inputs.refusal is None
        and inputs.policy is not None
        and inputs.plan_version == request.expected_plan_version
        and inputs.policy.expires_at > datetime.now(UTC),
        "accepted_policy_changed_or_expired",
    )
    require(await policy_owner_matches(session, actor_id, inputs.policy), "recovery_policy_owner_required")
    require(
        inputs.policy.org_id == org_id and binding.repo in inputs.policy.repository_ids and inputs.policy.permits(Action.MERGE),
        "code_delivery_policy_not_authorized",
    )
    require(
        await code_only_delivery(session, context, node) is not delivery,
        "full_delivery_recovery_required" if delivery else "code_only_settlement_required",
    )
    if delivery:
        require(inputs.policy.permits(Action.DEPLOY), "deployment_policy_not_authorized")
    plan = await session.scalar(
        select(OrchestrationAcceptedPlan)
        .where(
            OrchestrationAcceptedPlan.org_id == org_id,
            OrchestrationAcceptedPlan.flow_id == node.flow_id,
            OrchestrationAcceptedPlan.superseded_at.is_(None),
        )
        .execution_options(populate_existing=True)
    )
    require(plan is not None and (plan.plan_document or {}).get("execution_continuation") is None, "protected_authority_required")
    query = (
        select(OrchestrationAction)
        .where(
            OrchestrationAction.org_id == org_id,
            OrchestrationAction.execution_id == execution.id,
        )
        .order_by(OrchestrationAction.created_at, OrchestrationAction.id)
        .limit(101)
    )
    actions = list(await session.scalars((query.with_for_update() if lock else query).execution_options(populate_existing=True)))
    require(len(actions) <= 100, "merge_action_requires_reconciliation")
    merge_actions = [row for row in actions if row.kind == MERGE_KIND]
    if delivery:
        from dataclasses import asdict

        from .merge_controller import MergeReceipt

        require(len(merge_actions) == 1, "existing_merge_receipt_required")
        prior = merge_actions[0]
        try:
            merge = MergeReceipt.model_validate((prior.detail or {}).get("merge_receipt"))
        except (ValueError, TypeError):
            raise CycleBlockedError("existing_merge_receipt_required") from None
        require(
            prior.status == "succeeded"
            and merge.operation_key == prior.operation_key
            and all(getattr(merge, key) == value for key, value in asdict(identity).items())
            and (
                merge.execution_id,
                merge.flow_id,
                merge.repo,
                merge.pr_number,
                merge.provider_repository_id,
                merge.provider_pr_node_id,
                merge.reviewed_head_sha,
            )
            == (
                execution.id,
                node.flow_id,
                binding.repo,
                binding.pr_number,
                binding.provider_repository_id,
                binding.provider_pr_node_id,
                binding.head_sha,
            ),
            "existing_merge_binding_changed",
        )
        allowed = {MERGE_KIND, DISPATCH_KIND, "review_evidence", "notify_execution_block", "merge_repair_request"}
        from .story_acceptance import is_acceptance_observation

        require(all(row.kind in allowed or is_acceptance_observation(row) for row in actions), "delivery_effect_requires_reconciliation")
    else:
        require(not merge_actions, "merge_action_requires_reconciliation")
    require(all(row.status in {"succeeded", "failed"} for row in actions), "pending_effect_requires_reconciliation")
    dispatches = [row for row in actions if row.kind == DISPATCH_KIND]
    require(
        dispatches and dispatches[-1].status == "succeeded" and continuation_run_id(dispatches[-1].operation_key) == request.expected_run_id,
        "reviewer_dispatch_changed",
    )
    # The committed dispatch binds the historical reviewer independently of the
    # held-claim admission path, which intentionally refuses released claims.
    dispatch = await session.get(OrchestrationDecision, receipt_id(dispatches[-1].operation_key), populate_existing=True, with_for_update=lock)
    saved = json.loads(dispatch.reason) if dispatch else {}
    require(
        dispatch is not None
        and dispatch.org_id == org_id
        and dispatch.node_id == node_id
        and dispatch.flow_id == node.flow_id
        and dispatch.actor_kind == "service"
        and dispatch.actor_id == ACTOR
        and dispatch.kind == "agent_dispatched"
        and saved.get("run_id") == request.expected_run_id
        and saved.get("operation_key") == dispatches[-1].operation_key
        and saved.get("execution_id") == execution.id
        and saved.get("claim_generation") == identity.claim_generation
        and saved.get("accepted_plan_version") == identity.accepted_plan_version
        and saved.get("action") in {"review", "repair"}
        and (saved.get("envelope", {}).get("review_expect") or {}).get("author_run_id") == binding.run_id,
        "reviewer_dispatch_changed",
    )
    data = dict(
        contract=CONTRACT,
        org_id=org_id,
        flow_id=node.flow_id,
        node_id=node_id,
        attempt=node.attempts,
        node_state=node.state,
        legacy_stall=legacy_stall,
        plan_version=inputs.plan_version,
        plan_hash=plan.plan_hash,
        plan_document_digest=digest(plan.plan_document),
        policy_id=inputs.policy.policy_id,
        policy_hash=inputs.policy.policy_hash,
        policy_digest=digest(inputs.policy.model_dump(mode="json")),
        execution_id=execution.id,
        execution_revision=execution.revision,
        execution_phase=execution.phase,
        execution_status=execution.status,
        accepted_plan_version=identity.accepted_plan_version,
        execution_attempts=execution.attempts,
        handoff_receipt_ref=execution.handoff_receipt_ref,
        claim_id=identity.claim_id,
        claim_generation=identity.claim_generation,
        claim_event_id=claim.claim_event_id,
        claim_released_at=claim.released_at.isoformat(),
        reviewer_run_id=request.expected_run_id,
        binding_id=binding.id,
        binding_revision=binding.revision,
        head_sha=binding.head_sha,
        accepted_scope=binding.accepted_scope,
        binding_digest=digest(
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
        ),
        actions_digest=digest(
            [dict(id=r.id, kind=r.kind, status=r.status, artifact_ref=r.artifact_ref, receipt_ref=r.receipt_ref, detail=r.detail) for r in actions]
        ),
        dispatch_digest=digest(saved),
        code_only=not delivery,
    )
    if delivery:
        data["existing_merge_receipt"] = merge.model_dump(mode="json")
    return data, node, context, binding, saved


async def terminal(services, *, node, binding, request, dispatch):
    require(os.environ.get("AGENT_AUTHORITY_ENABLED", "false").lower() == "true", "protected_authority_required")
    raw = await services.protected(node.org_id, request.expected_run_id)
    require(
        raw
        and raw.get("invocation_id") == {"S": request.expected_run_id}
        and raw.get("tenant_id") == {"S": node.org_id}
        and raw.get("orchestration_node_id") == {"S": node.id}
        and raw.get("orchestration_node_attempt") == {"N": str(node.attempts)}
        and raw.get("persona") == {"S": "agent-codex-reviewer"}
        and raw.get("status") == {"S": "completed"}
        and raw.get("terminal_outcome") == {"S": "complete"}
        and raw.get("workload_binding", {}).get("S")
        and raw.get("repo") == {"S": binding.repo}
        and raw.get("installation_id") == {"N": str(binding.installation_id)}
        and raw.get("provider_repository_id") == {"N": str(binding.provider_repository_id)}
        and raw.get("orchestration_continuation_action") == {"S": dispatch["action"]}
        and raw.get("orchestration_continuation_receipt") == {"S": receipt_id(dispatch["operation_key"])}
        and raw.get("parent_principal") == {"S": dispatch["parent_principal"]}
        and re.fullmatch(r"[a-f0-9]{64}", raw.get("terminal_report_digest", {}).get("S", "")),
        "successful_reviewer_terminal_unverified",
    )
    try:
        recorded = datetime.fromisoformat(raw["terminal_recorded_at"]["S"].replace("Z", "+00:00"))
        require(recorded.tzinfo is not None and recorded <= datetime.now(UTC), "successful_reviewer_terminal_unverified")
        grant = await asyncio.to_thread(
            services.writer.store.live_grant,
            invocation_id=request.expected_run_id,
            tenant_id=node.org_id,
            attempt=int(raw["current_attempt"]["N"]),
            now=datetime.now(UTC),
        )
    except (BootstrapRefusedError, KeyError, TypeError, ValueError):
        raise CycleBlockedError("grant_revoked_or_unverifiable") from None
    require(grant.flow_id == node.flow_id and grant.authority.reference_id == dispatch["authority_reference_id"], "reviewer_authority_changed")
    return {"terminal_digest": digest(raw), "terminal_recorded_at": recorded.isoformat()}


class ReleasedMergeObserver(MergeServices):
    """Only the owner service can construct this read-only historical adapter."""

    def __init__(self, services, arguments):
        super().__init__(services.factory, authority=services.authority, provider=services.provider, storage=services.storage)
        self.arguments = arguments

    async def state(self, session, context):
        _, node, current_context, binding, _ = await current(session, **self.arguments)
        require(
            current_context.identity == context.identity and current_context.execution.revision == context.execution.revision,
            "current_execution_changed",
        )
        return node, binding, self.arguments["request"].expected_run_id


async def request_recovery(session, *, org_id, node_id, actor_id, actor_role, request, accept=False, services=None):
    require(actor_id and actor_role in {"owner", "org_admin", "platform_admin"}, "human_plan_approver_required")
    require(request.mode == "settle_merged", "explicit_merge_settlement_required")
    request_data = request.model_dump(mode="json")
    decision_id = str(
        uuid5(NAMESPACE_URL, CONTRACT + ":" + digest(dict(org=org_id, node=node_id, actor=actor_id, snapshot=request.expected_snapshot)))
    )

    async def replay():
        old = await session.get(OrchestrationDecision, decision_id, populate_existing=True)
        if old is None:
            return None
        require(
            old.org_id == org_id
            and old.node_id == node_id
            and old.actor_id == actor_id
            and old.actor_role == actor_role
            and old.actor_kind == "human"
            and old.kind == KIND
            and json.loads(old.reason).get("request") == request_data,
            "recovery_receipt_conflict",
        )
        return dict(accepted=True, created=False, decision_id=old.id)

    if accept:
        require(request.expected_snapshot, "preview_snapshot_required")
        old = await replay()
        if old:
            return old
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
        # This exceptional path requires current approval; it cannot withdraw a
        # standing negative App review based on an older accepted artifact.
        require(verification["review_state"] == "approved", "merged_pr_review_not_approved")
        require(
            terminal_data == await terminal(authority, node=node, binding=binding, request=request, dispatch=dispatch), "reviewer_terminal_changed"
        )
    except MergeVerificationPendingError:
        raise CycleBlockedError("merged_pr_checks_pending") from None
    except (ProviderConflictError, ProviderUnavailableError, EvidenceUnavailableError, HTTPError, BotoCoreError, ClientError):
        raise CycleBlockedError("merge_evidence_unavailable") from None
    except CycleBlockedError:
        if accept:
            old = await replay()
            if old:
                return old
        raise
    stable_verification = {key: value for key, value in verification.items() if key != "observed_at"}
    snapshot = digest(dict(**data, **terminal_data, provider_verification=snapshot_verification(stable_verification)))
    preview = dict(
        snapshot=snapshot,
        **data,
        **terminal_data,
        provider_verification=stable_verification,
        attempts_preserved=True,
        receipts_preserved=True,
        claim_preserved=True,
        next_action="conclude_verified_merge",
    )
    if not accept:
        return preview
    require(snapshot == request.expected_snapshot, "recovery_preview_changed")
    # Flow -> claim -> accepted plan -> execution, matching the canonical store.
    await load_execution(session, identity=context.identity, for_update=True, released_failure_run_id=request.expected_run_id)
    old = await replay()
    if old:
        return old
    locked, _, _, _, _ = await current(session, **arguments, lock=True)
    require(locked == data, "recovery_preview_changed")
    settled = await settle_released_merge(
        session,
        identity=context.identity,
        expected_revision=context.execution.revision,
        reviewer_run_id=request.expected_run_id,
        released_at=datetime.fromisoformat(data["claim_released_at"]),
        receipt=receipt,
        verification=verification,
    )
    require(settled.kind is OutcomeKind.APPLIED, "merge_settlement_assignment_changed")
    if data["legacy_stall"] is not None:
        # The locked provenance and full fresh merge proof authorize only an
        # atomic projection repair. RUNNING is never committed separately and
        # no dispatch, claim acquisition or attempt admission occurs.
        move = transition(
            NodeState.FAILED,
            NodeState.RUNNING,
            actor_kind=ActorKind.SERVICE,
            reason="Restore legacy stall projection for verified existing merge settlement",
            stalled_review_authorized=True,
        )
        require(move.allowed and node.state == "failed", "merge_settlement_stall_provenance_changed")
        session.add(
            OrchestrationDecision(
                org_id=org_id,
                flow_id=node.flow_id,
                node_id=node.id,
                kind="merged_delivery_stall_recovered",
                actor_kind="service",
                actor_id="system:merged-delivery-settlement",
                actor_role="engine",
                from_state="failed",
                to_state="running",
                reason=json.dumps({"legacy_stall": data["legacy_stall"], "snapshot": snapshot, "owner_actor_id": actor_id}),
            )
        )
        node.state = "running"
        await session.flush()
    await settle_merge(session, context, receipt, data)
    from .story_acceptance import record_acceptance_completion

    if settled.record is not None:
        await record_acceptance_completion(session, settled.record, datetime.now(UTC))
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
            reason=json.dumps(dict(**preview, request=request_data, merge_receipt=receipt.model_dump(mode="json"))),
        )
    )
    await session.flush()
    return dict(accepted=True, created=True, decision_id=decision_id, **preview)
