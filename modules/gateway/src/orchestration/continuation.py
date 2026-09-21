"""Explicit, snapshot-bound adoption of existing code delivery by the K2 runner.

This operation changes execution authority, not the accepted graph. Completed
nodes and human gates remain untouched. An active legacy worker blocks adoption;
an operator never wins a race by starting a second developer beside it.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select

from .compile import ApprovalContext
from .dispatch_pass import attempt_run_id
from .execution_policy import Action, ExecutionPolicy, policy_hash, stamp_policy
from .execution_state import ExecutionIdentity, ExecutionPhase, OutcomeKind
from .execution_store import create_execution
from .models import (
    OrchestrationAcceptedPlan,
    OrchestrationDecision,
    OrchestrationEdge,
    OrchestrationExecution,
    OrchestrationFlow,
    OrchestrationNode,
    OrchestrationPullRequestBinding,
    OrchestrationWorkClaim,
)
from .pr_bindings import binding_scope_matches, binding_snapshot
from .state import ActorKind
from .work_claims import ClaimBinding, ClaimOwner, OwnerKind, bind_run, claim_work

MODE = "shared_worker_role"
CODE_ACTIONS = frozenset({Action.DEVELOP, Action.REVIEW, Action.REPAIR, Action.MERGE})
PRESERVED = frozenset({"passed", "superseded", "awaiting_gate", "rejected_at_gate", "failed", "halted"})


class ContinuationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    execution_policy: ExecutionPolicy
    # Explicit transport adoption of an accepted, never-started policy. Its
    # bounds and gates must remain byte-equivalent after schema normalization.
    preserve_accepted_policy: bool = False
    # Selected by the approver; only merged story settlement changes. Evaluation
    # nodes keep their existing evidence and acceptance requirements.
    delivery_mode: Literal["code_only"] | None = None
    # A reconciliation is an explicit owner assertion, never zero inferred from
    # missing delayed usage. The next admission also checks live observed usage.
    reconciled_spend_usd: Decimal = Field(ge=0, le=100000)
    reconciliation_evidence: str = Field(min_length=8, max_length=4000)
    effects_and_credentials_reconciled: bool
    worker_role_arn: str = Field(pattern=r"^arn:aws(?:-[a-z-]+)?:iam::[0-9]{12}:role/[a-zA-Z0-9+=,.@_/-]+$")
    expected_snapshot: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def code_delivery_only(self):
        policy = self.execution_policy
        if (
            set(policy.allowed_actions) - CODE_ACTIONS
            or policy.environment_connection_ids
            or (policy.evaluation_acceptance and not self.preserve_accepted_policy)
        ):
            raise ValueError("continuation authorizes code delivery only; deployment and evaluation retain their existing gates")
        if not {Action.REVIEW, Action.REPAIR}.issubset(policy.allowed_actions):
            raise ValueError("continuation requires explicit review and repair authority")
        if policy.user_credentials is None or self.worker_role_arn not in policy.user_credentials.aws_role_arns:
            raise ValueError("the existing worker IAM role must be explicitly accepted in user_credentials")
        if set(policy.allowed_actions) - set(policy.user_credentials.actions):
            raise ValueError("the accepted worker role must cover every requested code-delivery action")
        if self.reconciled_spend_usd >= policy.limits.max_spend_usd:
            raise ValueError("the total spend ceiling must exceed reconciled historical spend")
        return self


class ContinuationRefusedError(ValueError):
    def __init__(self, code: str, detail: str):
        self.code, self.detail = code, detail
        super().__init__(detail)


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def request_digest(request: ContinuationRequest, actor: ApprovalContext) -> str:
    excluded = {"expected_snapshot"}
    # Preserve lost-response replay for requests accepted before this field existed.
    if not request.preserve_accepted_policy:
        excluded.add("preserve_accepted_policy")
    if request.delivery_mode is None:
        excluded.add("delivery_mode")
    return digest(
        {
            "request": request.model_dump(mode="json", exclude=excluded),
            "actor": actor.actor_id,
            "org": actor.org_id,
            "budget_scope": "authenticated_gateway_calls",
        }
    )


async def initialize_meter(*, org_id, flow_id, policy, marker):
    from .shared_policy import initialize_shared_meter

    return await initialize_shared_meter(org_id=org_id, flow_id=flow_id, policy=policy, marker=marker)


async def _snapshot(session, org_id: str, flow_id: str, *, lock=False, require_pristine=False):
    query = (
        select(OrchestrationFlow).where(OrchestrationFlow.org_id == org_id, OrchestrationFlow.id == flow_id).execution_options(populate_existing=True)
    )
    flow = await session.scalar(query.with_for_update() if lock else query)
    if flow is None:
        raise ContinuationRefusedError("flow_not_found", "No flow in this tenant.")
    plan = await session.scalar(
        select(OrchestrationAcceptedPlan)
        .where(
            OrchestrationAcceptedPlan.org_id == org_id,
            OrchestrationAcceptedPlan.flow_id == flow_id,
            OrchestrationAcceptedPlan.superseded_at.is_(None),
        )
        .execution_options(populate_existing=True)
    )
    if plan is None:
        raise ContinuationRefusedError("accepted_plan_missing", "The flow has no accepted plan to continue.")
    nodes = list(
        (
            await session.scalars(
                select(OrchestrationNode)
                .where(
                    OrchestrationNode.org_id == org_id,
                    OrchestrationNode.flow_id == flow_id,
                )
                .order_by(OrchestrationNode.id)
                .execution_options(populate_existing=True)
            )
        ).all()
    )
    bindings = list(
        (
            await session.scalars(
                select(OrchestrationPullRequestBinding)
                .where(
                    OrchestrationPullRequestBinding.org_id == org_id,
                    OrchestrationPullRequestBinding.flow_id == flow_id,
                    OrchestrationPullRequestBinding.state == "active",
                )
                .order_by(OrchestrationPullRequestBinding.id)
                .execution_options(populate_existing=True)
            )
        ).all()
    )
    executions = list(
        (
            await session.scalars(
                select(OrchestrationExecution)
                .where(
                    OrchestrationExecution.org_id == org_id,
                    OrchestrationExecution.flow_id == flow_id,
                )
                .order_by(OrchestrationExecution.id)
            )
        ).all()
    )
    edges = list(
        (
            await session.scalars(
                select(OrchestrationEdge)
                .where(
                    OrchestrationEdge.org_id == org_id,
                    OrchestrationEdge.flow_id == flow_id,
                )
                .order_by(OrchestrationEdge.id)
            )
        ).all()
    )
    claims = list(
        (
            await session.scalars(
                select(OrchestrationWorkClaim)
                .where(
                    OrchestrationWorkClaim.org_id == org_id,
                    OrchestrationWorkClaim.owner_ref == flow_id,
                    OrchestrationWorkClaim.state == "held",
                )
                .order_by(OrchestrationWorkClaim.id)
            )
        ).all()
    )
    snapshot = {
        "flow": {"id": flow.id, "state": flow.state},
        "plan": {"id": plan.id, "version": plan.version, "hash": plan.plan_hash},
        "nodes": [
            {
                "id": n.id,
                "kind": n.kind,
                "state": n.state,
                "attempts": n.attempts,
                "title": n.title,
                "issue_ref": n.issue_ref,
                "address": [n.epic_ref, n.wave_ref, n.node_ref],
            }
            for n in nodes
        ],
        "bindings": [binding_snapshot(b) for b in bindings],
        "executions": [{"id": e.id, "revision": e.revision, "phase": e.phase, "status": e.status} for e in executions],
        "edges": [[e.from_node_id, e.to_node_id] for e in edges],
        "claims": [{"id": c.id, "generation": c.generation, "active_run_id": c.active_run_id} for c in claims],
    }
    if require_pristine:
        from .run_reports import OrchestrationRunReport

        evidence = {}
        for name, model, conditions in (
            ("reports", OrchestrationRunReport, [OrchestrationRunReport.flow_id == flow_id]),
            ("bindings", OrchestrationPullRequestBinding, [OrchestrationPullRequestBinding.flow_id == flow_id]),
            ("claims", OrchestrationWorkClaim, [OrchestrationWorkClaim.owner_ref == flow_id]),
            (
                "dispatches",
                OrchestrationDecision,
                [OrchestrationDecision.flow_id == flow_id, OrchestrationDecision.kind == "node_dispatched"],
            ),
        ):
            key = model.run_id if name == "reports" else model.id
            evidence[name] = list(await session.scalars(select(key).where(model.org_id == org_id, *conditions).order_by(key)))
        decision = (
            await session.get(OrchestrationDecision, plan.accepted_by_decision_id, populate_existing=True) if plan.accepted_by_decision_id else None
        )
        snapshot["accepted_policy"] = {
            "document_hash": digest(plan.plan_document),
            "created_at": plan.created_at.isoformat(),
            "decision": {
                "id": decision.id,
                "org_id": decision.org_id,
                "flow_id": decision.flow_id,
                "kind": decision.kind,
                "actor_id": decision.actor_id,
                "actor_kind": decision.actor_kind,
                "created_at": decision.created_at.isoformat(),
            }
            if decision
            else None,
            "start_evidence": evidence,
        }
    return flow, plan, nodes, bindings, snapshot


def _preserved_authority(plan, snapshot, policy, now):
    """An explicit credential transport change cannot reset accepted bounds."""
    raw = (plan.plan_document or {}).get("execution_policy")
    try:
        previous = ExecutionPolicy.model_validate(raw)
    except ValueError:
        raise ContinuationRefusedError("accepted_policy_unverifiable", "A valid accepted policy is required for preserved continuation.") from None
    acceptance = snapshot["accepted_policy"]
    decision = acceptance["decision"]
    if (
        not decision
        or decision["org_id"] != plan.org_id
        or decision["flow_id"] != plan.flow_id
        or decision["kind"] != "plan_accepted"
        or decision["actor_kind"] != "human"
        or decision["actor_id"] != previous.principal_id
        or previous.policy_hash != policy_hash(previous)
        or previous.policy_id != "pol_" + policy_hash(previous)[:32]
    ):
        raise ContinuationRefusedError("accepted_policy_unverifiable", "The original policy and human acceptance must be verifiable.")
    excluded = {"schema_version", "user_credentials", "principal_id", "policy_id", "policy_hash"}
    if previous.model_dump(mode="json", exclude=excluded) != policy.model_dump(mode="json", exclude=excluded):
        raise ContinuationRefusedError(
            "accepted_policy_changed", "Preserved continuation must retain every accepted limit, expiry, action, scope and gate."
        )
    if (
        snapshot["executions"]
        or any(acceptance["start_evidence"].values())
        or any(n["attempts"] != 0 or (n["kind"] != "gate" and n["state"] not in {"pending", "ready"}) for n in snapshot["nodes"])
    ):
        raise ContinuationRefusedError("accepted_flow_already_started", "Preserved continuation requires a flow with no worker or delivery history.")
    started = min(datetime.fromisoformat(acceptance["created_at"]), datetime.fromisoformat(decision["created_at"]))
    if started > now or now >= started + timedelta(seconds=previous.limits.max_wall_clock_seconds):
        raise ContinuationRefusedError("wall_clock_limit_exceeded", "The original accepted wall-clock allowance is exhausted or unverifiable.")
    return {
        "accepted_at": started.isoformat(),
        "continued_at": now.isoformat(),
        "preserved_plan_version": plan.version,
        "preserved_policy_id": previous.policy_id,
        "preserved_policy_hash": previous.policy_hash,
        "preserved_acceptance_decision_id": decision["id"],
    }


async def preview_continuation(session, *, flow_id, actor, request, resolver, resolve_pr, now=None):
    now = now or datetime.now(UTC)
    if actor.actor_kind is not ActorKind.HUMAN:
        raise ContinuationRefusedError("human_acceptance_required", "Only a plan approver can accept continuation authority.")
    policy = stamp_policy(request.execution_policy, principal_id=actor.actor_id, org_id=actor.org_id)
    flow, plan, nodes, bindings, snapshot = await _snapshot(session, actor.org_id, flow_id, require_pristine=request.preserve_accepted_policy)
    blockers = []
    if os.environ.get("AGENT_WORKER_ROLE_ARN") != request.worker_role_arn:
        blockers.append({"code": "worker_role_mismatch", "detail": "The accepted role must match the configured worker IAM role."})
    marker = (plan.plan_document or {}).get("execution_continuation")
    preserved_authority = {}
    if request.preserve_accepted_policy and not marker:
        try:
            preserved_authority = _preserved_authority(plan, snapshot, policy, now)
        except ContinuationRefusedError as error:
            blockers.append({"code": error.code, "detail": error.detail})
    elif marker or (plan.plan_document or {}).get("execution_policy") is not None or snapshot["executions"]:
        blockers.append({"code": "already_governed", "detail": "Use the existing execution and amendment controls for a governed flow."})
    if flow.state not in {"pending", "running"}:
        blockers.append({"code": "flow_not_running", "detail": "The flow's current human control must be resolved first."})
    if not request.effects_and_credentials_reconciled:
        blockers.append({"code": "reconciliation_required", "detail": "An approver must reconcile existing effects and credentials."})
    if policy.expires_at <= now:
        blockers.append({"code": "policy_expired", "detail": "Continuation requires an unexpired policy."})
    stages, provider_facts, initial_runs = [], {}, {}
    for node in nodes:
        stage = {"node_id": node.id, "state": node.state, "attempt": node.attempts, "action": "preserve"}
        stages.append(stage)
        if node.kind != "story" or node.state in PRESERVED:
            continue
        if node.attempts == 0 and node.state in {"pending", "ready"}:
            stage["action"] = "develop_when_dependencies_pass"
            continue
        if node.state not in {"running", "awaiting_merge"}:
            blockers.append(
                {"node_id": node.id, "code": "recovery_required", "detail": "Preserve this node and use its existing recovery control first."}
            )
            continue
        matching = [b for b in bindings if b.node_id == node.id and b.attempt == node.attempts]
        if len(matching) != 1 or not binding_scope_matches(matching[0], node):
            blockers.append(
                {
                    "node_id": node.id,
                    "code": "current_binding_required",
                    "detail": "Register or recover the current implementation PR before continuation.",
                }
            )
            continue
        binding = matching[0]
        if binding.repo not in policy.repository_ids:
            blockers.append(
                {"node_id": node.id, "code": "repository_not_permitted", "detail": "The accepted policy must include the bound repository."}
            )
            continue
        if node.attempts >= policy.limits.max_attempts_per_node:
            blockers.append(
                {
                    "node_id": node.id,
                    "code": "attempt_limit_exceeded",
                    "detail": "The total attempt ceiling must include previously consumed attempts.",
                }
            )
            continue
        run_id = attempt_run_id(node.id, node.attempts)
        try:
            run = await resolver.resolve(run_id)
            from .work_claims import compute_liveness

            exited = (
                run
                and run.get("tenant_id") == actor.org_id
                and compute_liveness(
                    run.get("status"),
                    str(run.get("arrived_at") or ""),
                    now,
                    run.get("status_updated_at"),
                )
                == "exited"
            )
        except Exception:
            exited = False
        if not exited:
            blockers.append(
                {
                    "node_id": node.id,
                    "code": "prior_worker_active_or_unverified",
                    "detail": "Wait for the existing worker to exit; continuation will not replace it.",
                }
            )
            continue
        try:
            pr = await resolve_pr(org_id=actor.org_id, installation_id=binding.installation_id, repo=binding.repo, pr_number=binding.pr_number)
            if pr.provider_repository_id != binding.provider_repository_id or pr.provider_pr_node_id != binding.provider_pr_node_id:
                raise ValueError("identity changed")
        except Exception:
            blockers.append(
                {
                    "node_id": node.id,
                    "code": "pr_identity_unverifiable",
                    "detail": "The provider must confirm the current bound PR identity and head.",
                }
            )
            continue
        provider_facts[node.id] = {"binding_id": binding.id, "head_sha": pr.head_sha, "run_status": run["status"]}
        initial_runs[node.id] = {
            "run_id": run_id,
            "attempt": node.attempts,
            "repo": binding.repo,
            "installation_id": binding.installation_id,
            "provider_repository_id": binding.provider_repository_id,
            "head_sha": pr.head_sha,
            "evidence_origin": "owner_reconciled_legacy_delivery",
            "observed_status": run["status"],
        }
        stage.update(action="review_current_revision", phase="awaiting_review", pr_number=binding.pr_number, head_sha=pr.head_sha)
    return {
        "flow_id": flow_id,
        "base_plan_version": plan.version,
        "snapshot": digest({"database": snapshot, "provider": provider_facts, "request": request_digest(request, actor)}),
        "database_snapshot": digest(snapshot),
        "stages": stages,
        "blockers": blockers,
        "ready": not blockers,
        "initial_runs": initial_runs,
        "preserved_authority": preserved_authority,
        "delivery_mode": request.delivery_mode,
        "budget_scope": "authenticated_gateway_calls",
        "budget_limitation": "The shared IAM role retains its configured permissions; direct provider calls are outside this gateway budget.",
    }


async def _accept_continuation(session, *, flow_id, actor, request, resolver, resolve_pr, now=None):
    now = now or datetime.now(UTC)
    if not request.expected_snapshot:
        raise ContinuationRefusedError("preview_required", "Accept the exact snapshot returned by continuation preview.")
    # Lost-response replay is recognized before the precondition that the flow is
    # legacy. The accepted immutable request hash binds the same human and bounds.
    _, current, _, _, _ = await _snapshot(session, actor.org_id, flow_id, require_pristine=request.preserve_accepted_policy)
    marker = (current.plan_document or {}).get("execution_continuation")
    if marker and marker.get("request_hash") == request_digest(request, actor) and marker.get("snapshot_hash") == request.expected_snapshot:
        return {"flow_id": flow_id, "plan_version": current.version, "decision_id": current.accepted_by_decision_id, "already_accepted": True}
    preview = await preview_continuation(session, flow_id=flow_id, actor=actor, request=request, resolver=resolver, resolve_pr=resolve_pr, now=now)
    if preview["snapshot"] != request.expected_snapshot:
        raise ContinuationRefusedError("snapshot_changed", "The flow, PR head, worker state, or requested authority changed; preview again.")
    if preview["blockers"]:
        raise ContinuationRefusedError(preview["blockers"][0]["code"], preview["blockers"][0]["detail"])
    # Serialize with graph dispatch. All provider I/O above completed before this
    # lock; every controller rechecks the actual head before issuing a new effect.
    flow, plan, nodes, bindings, snapshot = await _snapshot(
        session, actor.org_id, flow_id, lock=True, require_pristine=request.preserve_accepted_policy
    )
    raced_marker = (plan.plan_document or {}).get("execution_continuation")
    if (
        raced_marker
        and raced_marker.get("request_hash") == request_digest(request, actor)
        and raced_marker.get("snapshot_hash") == request.expected_snapshot
    ):
        return {"flow_id": flow_id, "plan_version": plan.version, "decision_id": plan.accepted_by_decision_id, "already_accepted": True}
    if digest(snapshot) != preview["database_snapshot"]:
        raise ContinuationRefusedError("snapshot_changed", "The flow changed during acceptance; preview again.")
    policy = stamp_policy(request.execution_policy, principal_id=actor.actor_id, org_id=actor.org_id)
    document = copy.deepcopy(plan.plan_document)
    marker = {
        "contract_version": 1,
        "mode": MODE,
        "accepted_at": now.isoformat(),
        "prior_spend_usd": str(request.reconciled_spend_usd),
        "prior_attempts": {n.id: n.attempts for n in nodes},
        "initial_runs": preview["initial_runs"],
        "snapshot_hash": request.expected_snapshot,
        "request_hash": request_digest(request, actor),
        "reconciliation_evidence": request.reconciliation_evidence,
        "worker_role_arn": request.worker_role_arn,
        "budget_scope": "authenticated_gateway_calls",
        **preview["preserved_authority"],
        **({"delivery_mode": request.delivery_mode} if request.delivery_mode else {}),
    }
    if not await initialize_meter(org_id=actor.org_id, flow_id=flow_id, policy=policy, marker=marker):
        raise ContinuationRefusedError(
            "budget_initialization_unavailable", "The shared budget must acknowledge the reconciled baseline before acceptance."
        )
    document.update(execution_policy=policy.model_dump(mode="json"), execution_continuation=marker)
    decision = OrchestrationDecision(
        org_id=actor.org_id,
        flow_id=flow_id,
        kind="plan_accepted",
        actor_id=actor.actor_id,
        actor_role=actor.actor_role,
        actor_kind="human",
        reason=json.dumps({"action": "continue_existing_delivery", **marker}),
    )
    session.add(decision)
    await session.flush()
    new_plan = OrchestrationAcceptedPlan(
        org_id=actor.org_id,
        flow_id=flow_id,
        version=plan.version + 1,
        plan_document=document,
        plan_hash=digest(document),
        accepted_by_decision_id=decision.id,
    )
    plan.superseded_at = now
    session.add(new_plan)
    await session.flush()
    for node in nodes:
        initial = preview["initial_runs"].get(node.id)
        if initial is None:
            continue
        binding = next(b for b in bindings if b.node_id == node.id and b.attempt == node.attempts)
        claim = await claim_work(
            session,
            binding=ClaimBinding(actor.org_id, binding.provider_repository_id, int(str(node.issue_ref).lstrip("#"))),
            owner=ClaimOwner(OwnerKind.ENGINE_FLOW, flow_id),
            event_id=initial["run_id"],
        )
        if not claim.admitted:
            raise ContinuationRefusedError("lane_already_owned", "An existing work claim prevents continuation; reconcile it first.")
        bound = await bind_run(session, org_id=actor.org_id, claim_id=claim.claim_id, generation=claim.generation, run_id=initial["run_id"])
        if not bound.admitted:
            raise ContinuationRefusedError("lane_already_owned", "The continued work claim could not bind its original run.")
        identity = ExecutionIdentity(
            org_id=actor.org_id,
            node_id=node.id,
            cycle=node.attempts,
            accepted_plan_version=new_plan.version,
            claim_id=claim.claim_id,
            claim_generation=claim.generation,
        )
        result = await create_execution(
            session,
            identity=identity,
            flow_id=flow_id,
            phase=ExecutionPhase.AWAITING_REVIEW,
            deadline_at=min(policy.expires_at, now + timedelta(seconds=policy.limits.max_wall_clock_seconds)),
        )
        if result.kind is not OutcomeKind.APPLIED:
            raise ContinuationRefusedError("execution_conflict", "The continuation execution conflicts with existing authority.")
    return {"flow_id": flow_id, "plan_version": new_plan.version, "decision_id": decision.id, "already_accepted": False}


async def accept_continuation(session, **kwargs):
    """All acceptance writes commit together, including a claim conflict refusal."""
    async with session.begin_nested():
        return await _accept_continuation(session, **kwargs)
