"""Recheck accepted policy at a protected worker's credential boundary.

The caller supplies an authenticated execution and live grant, never an envelope
or request-selected flow. Status and cleanup remain available independently of
this action gate. Unsupported credential scoping is a refusal.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from sqlalchemy import func, select

from src.agentauth.grants import DelegatedGrant
from src.shared.identity.resolver import UnresolvableUserEntityError, resolve_root_user_entity_id
from src.shared.models.base import utcnow

from .dispatch import graph_address
from .execution_policy import Action, CredentialScope, Decision, DenyReason, ResourceRef, authorize_action
from .models import DecisionKind, NodeKind, OrchestrationAcceptedPlan, OrchestrationDecision, OrchestrationFlow, OrchestrationNode
from .policy_admission import _observed_spend, load_in_force_policy, resolve_authorization_context
from .state import NodeState


async def flow_started_at(session, *, org_id: str, flow_id: str) -> datetime | None:
    """The first committed dispatch survives retries, children and amendments."""
    started = await session.scalar(
        select(func.min(OrchestrationDecision.created_at)).where(
            OrchestrationDecision.org_id == org_id,
            OrchestrationDecision.flow_id == flow_id,
            OrchestrationDecision.kind.in_([DecisionKind.NODE_DISPATCHED.value, DecisionKind.AGENT_DISPATCHED.value]),
        )
    )
    return started.replace(tzinfo=UTC) if started is not None and started.tzinfo is None else started


def runtime_action(execution: dict, node: OrchestrationNode) -> Action | None:
    persona = execution.get("persona", {}).get("S")
    if node.kind == NodeKind.EVAL.value and persona == "operations":
        return Action.EVALUATE
    if node.kind == NodeKind.STORY.value:
        if persona == "reviewer":
            return Action.REVIEW
        if persona == "developer":
            return Action.REPAIR if node.attempts > 1 else Action.DEVELOP
    return None


async def authorize_worker_credential(session, *, execution: dict, grant: DelegatedGrant, broker_path: str) -> Decision:
    """Authorize the existing assignment; this never admits another attempt.

    AWS and raw-secret brokers currently cannot bind a token to an accepted
    environment connection and action. Refuse those capabilities for policy flows.
    The GitHub broker still must mint only for the authenticated repository.
    """
    if grant.authority.kind != "gate_decision":
        return Decision.permit("no accepted engine policy binding")
    inputs = await load_in_force_policy(session, org_id=grant.tenant_id, flow_id=grant.flow_id)
    if inputs.refusal is not None:
        return inputs.refusal
    if inputs.policy is None:
        return Decision.permit("no execution policy in force; legacy semantics apply")
    policy = inputs.policy
    node = await session.scalar(
        select(OrchestrationNode).where(
            OrchestrationNode.id == execution.get("orchestration_node_id", {}).get("S"),
            OrchestrationNode.org_id == grant.tenant_id,
            OrchestrationNode.flow_id == grant.flow_id,
        )
    )
    flow = await session.scalar(select(OrchestrationFlow).where(OrchestrationFlow.id == grant.flow_id, OrchestrationFlow.org_id == grant.tenant_id))
    if (
        node is None
        or flow is None
        or node.state != NodeState.RUNNING.value
        or str(node.attempts) != execution.get("orchestration_node_attempt", {}).get("N")
        or execution.get("tenant_id") != {"S": grant.tenant_id}
        or execution.get("flow_id") != {"S": grant.flow_id}
    ):
        return Decision.block(DenyReason.WORK_NOT_OWNED, "protected execution is not the current flow assignment")

    # Bind the current policy to the actual human authority event. A newer plan
    # cannot silently authorize a worker issued under an earlier decision.
    approval = await session.scalar(
        select(OrchestrationDecision).where(
            OrchestrationDecision.id == grant.authority.reference_id,
            OrchestrationDecision.org_id == grant.tenant_id,
            OrchestrationDecision.flow_id == grant.flow_id,
        )
    )
    accepted_version = None
    if approval is not None:
        accepted_version = await session.scalar(
            select(OrchestrationAcceptedPlan.version)
            .where(
                OrchestrationAcceptedPlan.org_id == grant.tenant_id,
                OrchestrationAcceptedPlan.flow_id == grant.flow_id,
                OrchestrationAcceptedPlan.created_at <= approval.created_at,
            )
            .order_by(OrchestrationAcceptedPlan.version.desc())
            .limit(1)
        )
    if accepted_version != inputs.plan_version:
        return Decision.block(DenyReason.STALE_POLICY_VERSION, "worker authority does not bind the policy version in force")
    started = await flow_started_at(session, org_id=grant.tenant_id, flow_id=grant.flow_id)
    now = utcnow()
    if started is None or (now - started).total_seconds() >= policy.limits.max_wall_clock_seconds:
        return Decision.block(DenyReason.WALL_CLOCK_LIMIT_EXCEEDED, "flow execution deadline is exhausted or unavailable")
    action = runtime_action(execution, node)
    if action is None:
        return Decision.block(DenyReason.ACTION_NOT_PERMITTED, "assignment has no supported policy action")
    try:
        principal = await resolve_root_user_entity_id(session, grant.tenant_id, grant.authority.human_id)
        accepted_principal = await resolve_root_user_entity_id(session, grant.tenant_id, policy.principal_id or "")
    except UnresolvableUserEntityError:
        return Decision.block(DenyReason.MEMBERSHIP_REVOKED, "policy principal no longer resolves in this tenant")
    if principal != accepted_principal:
        return Decision.block(DenyReason.MEMBERSHIP_REVOKED, "worker authority does not belong to the policy principal")
    nodes = list(
        (
            await session.scalars(
                select(OrchestrationNode).where(OrchestrationNode.org_id == grant.tenant_id, OrchestrationNode.flow_id == grant.flow_id)
            )
        ).all()
    )
    spend = await _observed_spend(session, org_id=grant.tenant_id, flow_slug=flow.slug, nodes=nodes)
    repo = execution.get("repo", {}).get("S")
    repository_id = execution.get("provider_repository_id", {}).get("N", "")
    scope = CredentialScope.UNSCOPABLE
    if broker_path == "/internal/v1/github-installation-token" and repo in policy.repository_ids and repo in grant.repo_scope:
        scope = CredentialScope.SCOPED
    context = await resolve_authorization_context(
        session,
        policy=policy,
        plan_version=inputs.plan_version,
        node=node,
        principal_user_id=principal,
        credential_scope=scope,
        spend=spend,
        provider_repository_id=int(repository_id) if repository_id.isdigit() else None,
        expected_invocation_id=execution.get("invocation_id", {}).get("S"),
    )
    context = replace(
        context,
        accepted_plan_version=accepted_version,
        now=now,
        grant_revoked=not grant.is_live(now),
        # Revalidating an admitted action does not consume another slot/attempt.
        observed_attempts=max(0, node.attempts - 1),
        observed_concurrency=max(0, context.observed_concurrency - 1),
    )
    return authorize_action(
        context,
        action,
        ResourceRef(repository_id=repo, org_id=grant.tenant_id, node_address=graph_address(node, flow_slug=flow.slug)),
        accepted_version,
    )
