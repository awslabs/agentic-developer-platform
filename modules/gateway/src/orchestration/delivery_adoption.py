"""Attributed adoption of delivery that never had an engine worker.

Uses the binding and decision stores, not a synthetic dispatch. Adoption reserves
no worker, consumes no attempt, and cannot satisfy pending predecessor gates.
"""

from __future__ import annotations

import json
from dataclasses import asdict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.models.base import utcnow

from .dispatch_pass import DispatchPassConfig, issue_number_for_dispatch, resolve_installation_id
from .models import DecisionKind, NodeKind, OrchestrationDecision, OrchestrationFlow, OrchestrationNode, OrchestrationWorkClaim
from .pr_bindings import (
    BindingError,
    BindingRefusal,
    MergeEvidence,
    PullRequestIdentity,
    RegistrationTarget,
    _accepted_scope,
    active_binding_for_node,
    binding_scope_matches,
    binding_snapshot,
    evidence_for_binding,
    hold_explanation,
    register_binding,
)
from .state import ActorKind, NodeState, transition
from .tick import _predecessor_states, _unsatisfied


def historical_binding(binding) -> bool:
    return bool(
        binding is not None
        and binding.attempt == 0
        and binding.run_id is None
        and binding.registered_by_kind == ActorKind.HUMAN.value
        and binding.recovery_reason
    )


async def _lock_node(session, *, org_id, node_id):
    return (
        await session.execute(
            select(OrchestrationNode)
            .where(OrchestrationNode.org_id == org_id, OrchestrationNode.id == node_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def _assert_no_dispatch(session, node):
    dispatch = await session.scalar(
        select(OrchestrationDecision.id)
        .where(
            OrchestrationDecision.org_id == node.org_id,
            OrchestrationDecision.node_id == node.id,
            OrchestrationDecision.kind == DecisionKind.NODE_DISPATCHED.value,
        )
        .limit(1)
    )
    if node.attempts != 0 or dispatch is not None:
        raise BindingError(BindingRefusal.STALE_RUN, "Historical adoption requires a never-dispatched story; recover its current run instead.")


async def adopt_delivery(
    session: AsyncSession,
    *,
    org_id: str,
    node_id: str,
    pr: PullRequestIdentity,
    installation_id: int,
    actor_id: str,
    reason: str,
    evidence: MergeEvidence | None,
    expected_scope: str,
):
    """Persist provider-verified delivery, attributed to an authenticated human.

    The caller snapshots accepted scope before provider I/O. The node lock then
    fences dispatch, amendment, and concurrent adoption. A held ownership claim
    is refused even if its worker lease appears expired.
    """
    node = await _lock_node(session, org_id=org_id, node_id=node_id)
    if node is None or node.kind != NodeKind.STORY.value:
        raise BindingError(BindingRefusal.NOT_A_STORY, "No story in this tenant can adopt this delivery.")
    await _assert_no_dispatch(session, node)
    scope = await _accepted_scope(session, node)
    if scope != expected_scope:
        raise BindingError(BindingRefusal.SCOPE_CHANGED, "The accepted story scope changed during delivery verification.")
    repo = DispatchPassConfig.from_env().repo
    if not repo or pr.repo.lower() != repo.lower():
        raise BindingError(BindingRefusal.REPOSITORY_MISMATCH, "Historical delivery must use the configured engine repository.")
    current_installation = await resolve_installation_id(session, org_id=org_id)
    if installation_id != current_installation:
        raise BindingError(BindingRefusal.REPOSITORY_MISMATCH, "The tenant installation changed during delivery verification.")
    claim = await session.scalar(
        select(OrchestrationWorkClaim.id)
        .where(
            OrchestrationWorkClaim.org_id == org_id,
            OrchestrationWorkClaim.provider_repository_id == pr.provider_repository_id,
            OrchestrationWorkClaim.issue_number == issue_number_for_dispatch(node.issue_ref),
            OrchestrationWorkClaim.state == "held",
        )
        .with_for_update()
    )
    if claim is not None:
        raise BindingError(BindingRefusal.STALE_RUN, "This issue has an active work owner; reconcile that owner before historical adoption.")
    previous = await active_binding_for_node(session, org_id=org_id, node_id=node_id, attempt=0)
    if previous is not None and not historical_binding(previous):
        raise BindingError(BindingRefusal.STALE_RUN, "An existing worker binding cannot be relabeled as historical delivery.")
    if node.state not in {NodeState.PENDING.value, NodeState.READY.value} and not (
        historical_binding(previous) and node.state in {NodeState.AWAITING_MERGE.value, NodeState.PASSED.value}
    ):
        raise BindingError(BindingRefusal.STALE_RUN, "Historical adoption cannot override an active run, halt, rejection, or human gate.")
    # Validate against a candidate before writing. All provider facts (including
    # immutable identity) must satisfy the ordinary completion predicate.
    from types import SimpleNamespace

    candidate = SimpleNamespace(
        state="active",
        role="implementation",
        head_sha=pr.head_sha,
        provider_repository_id=pr.provider_repository_id,
        provider_pr_node_id=pr.provider_pr_node_id,
        repo=pr.repo,
        pr_number=pr.pr_number,
    )
    _, refusal = evidence_for_binding(candidate, evidence)
    if refusal:
        raise BindingError(refusal, hold_explanation(refusal))
    binding, created = await register_binding(
        session,
        target=RegistrationTarget(
            org_id, node.flow_id, node.id, 0, None, repo, issue_number_for_dispatch(node.issue_ref) or 0, installation_id, scope
        ),
        pr=pr,
        actor_id=actor_id,
        actor_kind=ActorKind.HUMAN,
        recovery_reason=reason,
    )
    if not created:
        return binding
    result = transition(node.state, NodeState.AWAITING_MERGE, actor_kind=ActorKind.HUMAN, reason=reason)
    if not result.allowed:
        raise BindingError(BindingRefusal.STALE_RUN, result.rejection_reason)
    before = node.state
    node.state = result.new_state.value
    node.updated_at = utcnow()
    session.add(
        OrchestrationDecision(
            org_id=org_id,
            flow_id=node.flow_id,
            node_id=node.id,
            kind=DecisionKind.RESULT_OBSERVED.value,
            actor_id=actor_id,
            actor_role="human",
            actor_kind=ActorKind.HUMAN.value,
            from_state=before,
            to_state=node.state,
            reason=json.dumps(
                {
                    "historical_adoption": True,
                    "attempt": 0,
                    "run_id": None,
                    "reason": reason,
                    "binding": binding_snapshot(binding),
                    "merge_receipt": asdict(evidence),
                    "evidence": "Historical delivery verified; waiting for predecessor gates and final reconciliation.",
                }
            ),
        )
    )
    await session.flush()
    return binding


async def observe_historical_delivery(session: AsyncSession, *, node, source) -> tuple[bool, str] | None:
    """One branch of result observation; never reads or fabricates a worker row."""
    if node.attempts != 0 or node.kind != NodeKind.STORY.value:
        return None
    binding = await active_binding_for_node(session, org_id=node.org_id, node_id=node.id, attempt=0)
    if not historical_binding(binding):
        return None
    from .results import _story_evidence

    observed_revision = binding.revision
    observed_scope = binding.accepted_scope
    installation = await resolve_installation_id(session, org_id=node.org_id)
    observation = {}
    url, hold = await _story_evidence(
        session,
        node=node,
        dispatch={"pr_binding_required": True},
        source=source,
        installation_id=installation,
        observation=observation,
    )
    locked = await _lock_node(session, org_id=node.org_id, node_id=node.id)
    current = await active_binding_for_node(session, org_id=node.org_id, node_id=node.id, attempt=0)
    if (
        locked.state != NodeState.AWAITING_MERGE.value
        or locked.attempts != 0
        or not historical_binding(current)
        or current.revision != observed_revision
        or current.accepted_scope != observed_scope
        or not binding_scope_matches(current, locked)
    ):
        return False, "Historical delivery scope or binding changed during verification."
    await _assert_no_dispatch(session, locked)
    # Graph changes lock the same node. An adoption may reserve delivered code
    # before its gates pass, but it never passes those gates on their behalf.
    predecessors = await _predecessor_states(session, org_id=node.org_id, node_id=node.id)
    blocking = _unsatisfied(predecessors)
    if blocking:
        url, hold = None, "Historical delivery is waiting for predecessor nodes: " + ", ".join(blocking)
        observation["historical_hold"] = {
            "code": "predecessor_pending",
            "node_ids": blocking,
            "actor": "operator" if any(state in {"awaiting_gate", "rejected_at_gate", "halted", "failed"} for _, state in predecessors) else "engine",
        }
    flow = await session.get(OrchestrationFlow, node.flow_id)
    if flow is None or flow.state in {"halted", "failed", "superseded", "rejected_at_gate"}:
        url, hold = None, "Historical delivery cannot progress while its flow is held."
        observation["historical_hold"] = {"code": "flow_held"}
    from .policy_admission import load_in_force_policy

    policy = await load_in_force_policy(session, org_id=node.org_id, flow_id=node.flow_id)
    if policy.policy is not None or policy.refusal is not None:
        url, hold = None, "Historical delivery requires reconciliation of this flow's execution policy before completion."
        observation["historical_hold"] = {"code": "execution_policy_reconciliation"}
    detail = f"Historical story completed by verified merged pull request: {url}" if url else hold
    target = NodeState.PASSED if url else NodeState.AWAITING_MERGE
    payload = {"historical_adoption": True, "attempt": 0, "run_id": None, "evidence": detail, **observation}
    if not url:
        payload.pop("merge_receipt", None)
    previous = await session.scalar(
        select(OrchestrationDecision)
        .where(
            OrchestrationDecision.org_id == node.org_id,
            OrchestrationDecision.node_id == node.id,
            OrchestrationDecision.kind == DecisionKind.RESULT_OBSERVED.value,
        )
        .order_by(OrchestrationDecision.created_at.desc(), OrchestrationDecision.id.desc())
        .limit(1)
    )
    if previous is None or json.loads(previous.reason or "{}") != payload:
        if url:
            result = transition(locked.state, target, actor_kind=ActorKind.SERVICE, reason=detail)
            if not result.allowed:
                raise ValueError(result.rejection_reason)
        before = locked.state
        locked.state = target.value
        locked.updated_at = utcnow()
        session.add(
            OrchestrationDecision(
                org_id=node.org_id,
                flow_id=node.flow_id,
                node_id=node.id,
                kind=DecisionKind.RESULT_OBSERVED.value,
                actor_id="system:orchestration-results",
                actor_role="engine",
                actor_kind=ActorKind.SERVICE.value,
                from_state=before,
                to_state=target.value,
                reason=json.dumps(payload),
            )
        )
        await session.flush()
    return bool(url), detail
