"""Explicit owner recovery of a failed protected reviewer; never merge evidence."""

import json
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select

from src.admin.persona_models.service import PreferenceRejectedError
from src.agentauth.launch_configuration import resolve_launch_configuration

from .continuation import digest
from .execution_policy import Action
from .execution_runner import RunnerContext
from .execution_state import OutcomeKind, PhaseAdvance
from .execution_store import advance_execution, load_execution
from .handoff import identity_for_attempt
from .models import OrchestrationAction, OrchestrationDecision, OrchestrationNode
from .pr_bindings import active_binding_for_node, binding_scope_matches
from .review_cycle import DISPATCH_KIND, PHASES, CycleBlockedError
from .shared_window import policy_owner_matches
from .stage_attempts import stage_attempts

KIND = "protected_review_recovery"
CONTRACT = "protected-review-recovery/v1"


def require(condition, reason):
    if not condition:
        raise CycleBlockedError(reason)


async def reviewer_configuration(session, *, org_id, user_id, model):
    """Validate one launch through the existing principal/catalogue boundary."""
    try:
        configuration = await resolve_launch_configuration(
            session, org_id=org_id, user_id=user_id, persona="agent-codex-reviewer", direct_model=model
        )
    except PreferenceRejectedError as error:
        raise CycleBlockedError("reviewer_model_" + error.reason) from None
    selection = configuration.get("model_selection") or {}
    require(
        configuration.get("model_requested") == model
        and configuration.get("model_resolved") == selection.get("model")
        and bool(selection.get("model"))
        and selection.get("source") == "explicit-direct"
        and selection.get("persona") == "agent-codex-reviewer"
        and selection.get("compatibility_class") == "codex-sdk"
        and bool(selection.get("principal_id"))
        and bool(selection.get("harness_contract_revision")),
        "reviewer_model_selection_unverifiable",
    )
    return configuration


async def verify_reviewer_configuration(session, data, *, org_id, user_id):
    """Re-read restrictions; never substitute a newly resolved choice on replay."""
    model = data.get("reviewer_model")
    if model is None:
        require("reviewer_model_configuration" not in data, "reviewer_model_selection_unverifiable")
        return None
    configuration = await reviewer_configuration(session, org_id=org_id, user_id=user_id, model=model)
    require(configuration == data.get("reviewer_model_configuration"), "reviewer_model_selection_changed")
    return configuration


async def current(session, *, org_id, node_id, request, services, actor_id=None, lock=False):
    from .review_cycle_dispatch import continuation_run_id, failed_review

    query = select(OrchestrationNode).where(OrchestrationNode.org_id == org_id, OrchestrationNode.id == node_id)
    node = await session.scalar(query.with_for_update() if lock else query)
    require(node is not None and node.kind == "story" and node.state in {"running", "awaiting_merge"}, "review_recovery_story_not_current")
    identity = await identity_for_attempt(session, org_id=org_id, node_id=node.id, attempt=node.attempts)
    require(identity is not None and node.attempts == request.expected_attempt, "report_assignment_changed")
    loaded = await load_execution(session, identity=identity, for_update=lock)
    require(loaded is not None and loaded.kind is OutcomeKind.APPLIED and loaded.record is not None, "current_execution_changed")
    execution = loaded.record
    require(execution.phase in PHASES and execution.status not in {"concluded", "superseded"}, "review_recovery_phase_changed")
    require(execution.pending_action_key is None, "pending_effect_requires_reconciliation")
    binding = await active_binding_for_node(session, org_id=org_id, node_id=node.id, attempt=node.attempts)
    require(binding is not None and binding_scope_matches(binding, node), "bound_delivery_missing")
    actions = list(
        await session.scalars(
            select(OrchestrationAction)
            .where(
                OrchestrationAction.org_id == org_id,
                OrchestrationAction.execution_id == execution.id,
                OrchestrationAction.kind == DISPATCH_KIND,
            )
            .order_by(OrchestrationAction.created_at, OrchestrationAction.id)
            .limit(101)
        )
    )
    from .unstarted_review import retired_unstarted_dispatch

    require(0 < len(actions) <= 100, "recovery_assignment_changed")
    actions = [action for action in actions if not retired_unstarted_dispatch(action)]
    require(bool(actions) and continuation_run_id(actions[-1].operation_key) == request.expected_run_id, "recovery_assignment_changed")
    context = RunnerContext(identity, execution, datetime.now(UTC))
    raw, _, inputs, principal, _ = await services.authorize(session, context, node, binding, request.expected_run_id, Action.REVIEW)
    require(failed_review(raw), "reviewer_exit_unverified")
    require(inputs.plan_version == request.expected_plan_version, "accepted_policy_changed_or_expired")
    if actor_id is not None:
        require(await policy_owner_matches(session, actor_id, inputs.policy), "recovery_policy_owner_required")
    remaining = inputs.policy.limits.max_attempts_per_node - await stage_attempts(session, org_id=org_id, node_id=node.id, action=Action.REVIEW)
    require(remaining > 0, "continuation_attempts_exhausted")
    require(await services.head(binding) == request.expected_head_sha, "current_pr_changed")
    data = dict(
        contract=CONTRACT,
        org_id=org_id,
        flow_id=node.flow_id,
        node_id=node.id,
        attempt=node.attempts,
        plan_version=inputs.plan_version,
        policy_id=inputs.policy.policy_id,
        policy_hash=inputs.policy.policy_hash,
        execution_id=execution.id,
        claim_id=identity.claim_id,
        claim_generation=identity.claim_generation,
        prior_run_id=request.expected_run_id,
        binding_id=binding.id,
        binding_revision=binding.revision,
        head_sha=request.expected_head_sha,
        accepted_scope=binding.accepted_scope,
        execution_attempts=execution.attempts,
        terminal_digest=digest(
            {key: raw.get(key) for key in ("status", "terminal_outcome", "current_attempt", "orchestration_continuation_receipt", "workload_binding")}
        ),
    )
    if request.reviewer_model is not None:
        data.update(
            reviewer_model=request.reviewer_model,
            reviewer_model_configuration=await reviewer_configuration(session, org_id=org_id, user_id=principal, model=request.reviewer_model),
        )
    return data, node, context, binding, actions


async def request_recovery(session, *, org_id, node_id, actor_id, actor_role, request, accept=False, services=None):
    from .dispatch_pass import attempt_run_id
    from .review_cycle_dispatch import ReviewCycleServices

    if request.mode == "review_released_reviewer":
        from .released_reviewer_recovery import request_recovery as released_recovery

        return await released_recovery(
            session, org_id=org_id, node_id=node_id, actor_id=actor_id, actor_role=actor_role, request=request, accept=accept, services=services
        )

    if request.mode == "review_existing_work":
        from .retained_work_recovery import request_recovery as retained_recovery

        return await retained_recovery(
            session, org_id=org_id, node_id=node_id, actor_id=actor_id, actor_role=actor_role, request=request, accept=accept, services=services
        )
    require(request.source_run_id is None and request.renew_decision_id is None, "retained_review_request_invalid")
    require(request.mode == "review" or request.reviewer_model is None, "reviewer_model_override_unsupported")
    if request.expected_run_id == attempt_run_id(node_id, request.expected_attempt):
        require(request.reviewer_model is None, "reviewer_model_override_unsupported")
        from .protected_developer_recovery import request_recovery as recover_developer

        return await recover_developer(
            session,
            org_id=org_id,
            node_id=node_id,
            actor_id=actor_id,
            actor_role=actor_role,
            request=request,
            accept=accept,
            services=services,
        )

    require(actor_id and actor_role in {"owner", "org_admin", "platform_admin"}, "human_plan_approver_required")
    services = services or ReviewCycleServices(None)
    decision_id = str(
        uuid5(NAMESPACE_URL, CONTRACT + ":" + digest(dict(org=org_id, node=node_id, actor=actor_id, snapshot=request.expected_snapshot)))
    )
    if accept:
        require(request.expected_snapshot, "preview_snapshot_required")
        old = await session.get(OrchestrationDecision, decision_id)
        if old is not None:
            require(
                old.org_id == org_id and old.node_id == node_id and old.actor_id == actor_id and old.actor_kind == "human" and old.kind == KIND,
                "recovery_receipt_conflict",
            )
            require(json.loads(old.reason).get("reviewer_model") == request.reviewer_model, "recovery_receipt_conflict")
            return dict(accepted=True, created=False, decision_id=old.id)
    data, node, context, _, _ = await current(
        session, org_id=org_id, node_id=node_id, request=request, services=services, actor_id=actor_id, lock=accept
    )
    snapshot = digest(data)
    preview = dict(snapshot=snapshot, **data, attempts_preserved=True, receipts_preserved=True, next_action="fresh_codex_review")
    if not accept:
        return preview
    require(snapshot == request.expected_snapshot, "recovery_preview_changed")
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
        identity=context.identity,
        advance=PhaseAdvance(
            phase=context.execution.phase,
            status=context.execution.status,
            expected_revision=context.execution.revision,
            next_check_at=datetime.now(UTC),
        ),
    )
    require(updated.kind is OutcomeKind.APPLIED, "recovery_assignment_changed")
    await session.flush()
    return dict(accepted=True, created=True, decision_id=decision_id, **preview)


async def verified_decision(session, *, decision_id, context, node, binding, services, prior_run_id, head_sha):
    from .plan_lineage import execution_plan_matches
    from .policy_admission import load_in_force_policy

    row = await session.get(OrchestrationDecision, decision_id, populate_existing=True)
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
        execution_id=context.execution.id,
        claim_id=context.identity.claim_id,
        claim_generation=context.identity.claim_generation,
        prior_run_id=prior_run_id,
        binding_id=binding.id,
        binding_revision=binding.revision,
        head_sha=head_sha,
        accepted_scope=binding.accepted_scope,
    )
    require(all(data.get(k) == v for k, v in expected.items()), "recovery_authority_changed")
    inputs = await load_in_force_policy(session, org_id=node.org_id, flow_id=node.flow_id)
    # Recovery records the current policy; a preserving amendment retains the
    # execution's older assignment identity. Require both authorities, without
    # rewriting the claim or accepting an unrelated plan-version mismatch.
    require(
        inputs.policy is not None
        and inputs.refusal is None
        and inputs.plan_version == data["plan_version"]
        and inputs.policy.policy_id == data["policy_id"]
        and inputs.policy.policy_hash == data["policy_hash"]
        and await execution_plan_matches(
            session,
            org_id=node.org_id,
            flow_id=node.flow_id,
            node_id=node.id,
            version=context.identity.accepted_plan_version,
            current_version=inputs.plan_version,
        )
        and await policy_owner_matches(session, row.actor_id, inputs.policy),
        "recovery_authority_changed",
    )
    raw = await services.protected(node.org_id, prior_run_id)
    from .review_cycle_dispatch import failed_review

    require(
        failed_review(raw)
        and digest(
            {key: raw.get(key) for key in ("status", "terminal_outcome", "current_attempt", "orchestration_continuation_receipt", "workload_binding")}
        )
        == data["terminal_digest"],
        "recovery_receipt_changed",
    )
    await verify_reviewer_configuration(session, data, org_id=node.org_id, user_id=inputs.policy.principal_id)
    return data


async def recovery_snapshot(session, context, node, binding, services, dispatches):
    if not dispatches:
        return None
    from .dispatch_pass import attempt_run_id
    from .review_cycle_dispatch import continuation_run_id, current_author_run

    active = continuation_run_id(dispatches[-1].operation_key)
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
    selected = next((row for row in rows if json.loads(row.reason).get("prior_run_id") == active), None)
    if selected is None:
        return None
    head_sha = await services.head(binding)
    await verified_decision(
        session,
        decision_id=selected.id,
        context=context,
        node=node,
        binding=binding,
        services=services,
        prior_run_id=active,
        head_sha=head_sha,
    )
    facts = await services.facts(session, context, node, binding, dispatches, budget_recovery_verified=True)
    require(facts["head_sha"] == head_sha, "current_pr_changed")
    return dict(
        **facts,
        binding_id=binding.id,
        binding_revision=binding.revision,
        repo=binding.repo,
        pr_number=binding.pr_number,
        provider_repository_id=binding.provider_repository_id,
        provider_pr_node_id=binding.provider_pr_node_id,
        accepted_scope=binding.accepted_scope,
        sequence=len(dispatches) + 1,
        next_action=Action.REVIEW.value,
        author_run_id=await current_author_run(session, node=node, default=attempt_run_id(node.id, node.attempts)),
        protected_recovery_decision_id=selected.id,
    )
