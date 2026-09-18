"""Recheck accepted policy at a protected worker's credential boundary.

The caller supplies an authenticated execution and live grant, never an envelope
or request-selected flow. Status and cleanup remain available independently of
this action gate. Unsupported credential scoping is a refusal.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_, select

from src.agentauth.github_operations import MEDIATED_GITHUB_OPERATION_PATH
from src.agentauth.grants import DelegatedGrant
from src.shared.identity.resolver import UnresolvableUserEntityError, resolve_root_user_entity_id
from src.shared.models.base import utcnow

from .dispatch import graph_address
from .execution_policy import Action, CredentialScope, Decision, DenyReason, ExecutionPolicy, ResourceRef, authorize_action
from .models import DecisionKind, NodeKind, OrchestrationAcceptedPlan, OrchestrationDecision, OrchestrationFlow, OrchestrationNode
from .policy_admission import AdmissionInputs, SpendObservation, load_in_force_policy, resolve_authorization_context
from .state import NodeState


@dataclass(frozen=True)
class WorkerCredentialDecision(Decision):
    permissions: dict[str, str] | None = None
    not_after: datetime | None = None
    provider_permissions: bool = False
    credential_id: str | None = None
    credential_secret_arn: str | None = None
    credential_ids: tuple[str, ...] = ()
    aws_role_arns: tuple[str, ...] = ()
    policy_id: str | None = None
    plan_version: int | None = None
    # The action this assignment was admitted for, and the immutable provider
    # repository it was admitted against. Both are reported so the mediated
    # operation service can build its assignment from what THIS function proved,
    # rather than re-deriving authority from a request body.
    action: Action | None = None
    provider_repository_id: int | None = None
    # The accepted policy document itself, so a consumer checks content and
    # gates against what a human actually accepted instead of rebuilding a
    # document from these fields and evaluating against its own reconstruction.
    policy: ExecutionPolicy | None = None


def policy_github_permissions(policy: ExecutionPolicy, action: Action) -> dict[str, str] | None:
    """GitHub contents-write also authorizes merge; never disguise that scope."""
    permissions = {"contents": "read", "pull_requests": "read", "issues": "read", "checks": "read", "metadata": "read"}
    if action is Action.EVALUATE:
        return permissions
    if action is Action.COORDINATE:
        # Read-only, deliberately. A coordinator's job is to read progress and
        # evidence and to request children through the authenticated dispatch
        # service — a platform-internal call that needs no provider write. Any
        # write it appeared to need (a comment, a label) is a GitHub *mutation*,
        # which #5223's separately authorized mediated capability owns; granting
        # `issues: write` here would pre-empt that authorization decision.
        return permissions
    if action is Action.REVIEW:
        return {**permissions, "pull_requests": "write", "issues": "write"}
    if action in {Action.DEVELOP, Action.REPAIR} and policy.permits(Action.MERGE):
        return {**permissions, "contents": "write", "pull_requests": "write", "issues": "write"}
    # A provider token that can write contents cannot enforce a human-only merge
    # gate. That needs mediated writes/scoped branch capabilities, not a broad
    # installation-token fallback.
    return None


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
    """The policy action this protected assignment is performing.

    Resolved from the execution record's **engine-written** fields, never from
    anything a worker body can choose. `persona` is a request-supplied string on the
    dispatch body, so it alone cannot decide authority; `wave_coordinator` and
    `coordinator_flow_id` are written only by `agentauth/dispatch.py` under a
    `gate_decision` authority and only alongside a committed dispatch receipt, which
    is why the coordinator test keys on them and treats the persona as a corroborating
    condition rather than the deciding one (#5224 design point 3).

    **A coordinator resolves to `COORDINATE`, not `EVALUATE`.** A wave coordinator is
    assigned to its wave's evaluation node, so the persona/kind test below would
    otherwise classify it as an evaluation — handing a coordinator the machine
    acceptance authority to *conclude* that evaluation, which is precisely the
    conflation #5224 forbids. The coordinator branch therefore comes first.

    This is not a live behavior change for existing flows: under an accepted policy a
    coordinator could not be dispatched at all before this story
    (`graph_dispatch._authorize_dispatch_policy` refused every `coordinates` request),
    and a flow with no accepted policy never reaches this function
    (`authorize_worker_credential` returns early). So no already-running coordinator
    was relying on the `EVALUATE` reading. A v1/v2 policy now resolves `COORDINATE`
    and denies with `action_not_permitted`, which is the correct refusal: absence of
    an accepted coordination scope grants nothing.
    """
    persona = execution.get("persona", {}).get("S")
    if execution.get("wave_coordinator") == {"BOOL": True} and execution.get("coordinator_flow_id", {}).get("S"):
        # Engine-written metadata, corroborated by the coordinator personas the
        # dispatch path admits. An execution carrying coordinator metadata but a
        # persona the engine never assigns as a coordinator is a mismatch, and
        # returning `None` refuses it rather than guessing which field to trust.
        return Action.COORDINATE if persona in {"operations", "aidlc"} else None
    if node.kind == NodeKind.EVAL.value and persona == "operations":
        return Action.EVALUATE
    if node.kind == NodeKind.STORY.value:
        if persona == "reviewer":
            return Action.REVIEW
        if persona == "developer":
            return Action.REPAIR if node.attempts > 1 else Action.DEVELOP
    return None


async def authorize_worker_credential(
    session,
    *,
    execution: dict,
    grant: DelegatedGrant,
    broker_path: str,
    inputs: AdmissionInputs | None = None,
    credential_request: dict | None = None,
) -> Decision:
    """Authorize the existing assignment; this never admits another attempt.

    Version 1 requires constrained provider capabilities. Version 2 may explicitly
    accept user credentials with their configured provider permissions/lifetime.
    The GitHub broker still must mint only for the authenticated repository.
    """
    if grant.authority.kind != "gate_decision":
        return Decision.permit("no accepted engine policy binding")
    inputs = inputs or await load_in_force_policy(session, org_id=grant.tenant_id, flow_id=grant.flow_id)
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
    if approval is not None and approval.actor_kind == "human" and approval.actor_id == grant.authority.human_id:
        accepted_version = await session.scalar(
            select(OrchestrationAcceptedPlan.version)
            .where(
                OrchestrationAcceptedPlan.org_id == grant.tenant_id,
                OrchestrationAcceptedPlan.flow_id == grant.flow_id,
                or_(
                    OrchestrationAcceptedPlan.accepted_by_decision_id == approval.id,
                    OrchestrationAcceptedPlan.created_at <= approval.created_at,
                ),
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
    from .flow_meter import read_flow_meter

    meter = await read_flow_meter(org_id=grant.tenant_id, flow_id=grant.flow_id, policy=policy)
    spend = SpendObservation(total_usd=meter.total_usd if meter is not None else None)
    repo = execution.get("repo", {}).get("S")
    repository_id = execution.get("provider_repository_id", {}).get("N", "")
    scope = CredentialScope.UNSCOPABLE
    permissions = policy_github_permissions(policy, action)
    not_after = min(policy.expires_at, started + timedelta(seconds=policy.limits.max_wall_clock_seconds))
    if grant.expires_at is not None:
        not_after = min(not_after, grant.expires_at)
    if broker_path == "/internal/v1/github-installation-token" and repo in policy.repository_ids and repo in grant.repo_scope:
        # GitHub installation tokens last one hour. We cannot issue one whose
        # lifetime would exceed this grant, even when issuance itself is allowed.
        if permissions is not None and not_after > now + timedelta(hours=1, seconds=30):
            scope = CredentialScope.SCOPED
    elif broker_path == MEDIATED_GITHUB_OPERATION_PATH and repo in policy.repository_ids and repo in grant.repo_scope:
        # Mediation is scopable where a token is not. The gateway holds the
        # installation credential and performs the single typed operation itself,
        # so there is no provider lifetime to reconcile against this grant and no
        # `contents: write` capability handed to the worker. That is why this
        # branch does not consult `policy_github_permissions`: its None result
        # means "no TOKEN can express this policy", which is the reason to
        # mediate, not a reason to refuse mediation.
        #
        # This grants no action the assignment lacks. `authorize_action` below
        # still gates the assignment's own action, and merge is a separate typed
        # operation the caller must be separately authorized for.
        scope = CredentialScope.SCOPED
    elif broker_path == "model" and repo in policy.repository_ids and repo in grant.repo_scope:
        # Model execution remains inside the policy-checking gateway boundary.
        scope = CredentialScope.SCOPED
    credential_id = None
    credential_secret_arn = None
    credential_ids = ()
    aws_role_arns = ()
    authority = policy.user_credentials
    user_paths = {
        "/internal/v1/credential-assume-role",
        "/internal/v1/credential-raw-read",
        "/internal/v1/proxy-request",
        "/internal/v1/credential-materialize",
        "/internal/v1/user-credentials",
        "/internal/v1/worker-task-credentials",
    }
    if broker_path in user_paths and authority is not None and action in authority.actions:
        from src.shared.services.credential_resolver import CredentialNotFoundError

        from .user_credentials import resolve_user_credential

        try:
            if broker_path == "/internal/v1/worker-task-credentials":
                aws_role_arns = tuple(authority.aws_role_arns)
                if not aws_role_arns:
                    raise CredentialNotFoundError("no approved direct roles")
            elif broker_path == "/internal/v1/user-credentials":
                accessible = []
                for selected_id in authority.vault_credential_ids:
                    try:
                        await resolve_user_credential(
                            session, org_id=grant.tenant_id, user_id=(credential_request or {}).get("user_id", ""), credential_id=selected_id
                        )
                        await resolve_user_credential(session, org_id=grant.tenant_id, user_id=principal, credential_id=selected_id)
                    except CredentialNotFoundError:
                        continue
                    accessible.append(selected_id)
                if not accessible:
                    raise CredentialNotFoundError("no approved credentials available")
                credential_ids = tuple(accessible)
                credential_id = accessible[0]
            else:
                request = credential_request or {}
                selected = await resolve_user_credential(
                    session, org_id=grant.tenant_id, user_id=request.get("user_id", ""), service=request.get("service"), label=request.get("label")
                )
                if selected.id not in authority.vault_credential_ids:
                    raise CredentialNotFoundError("credential is not approved")
                # The current plan owner must still have vault access too. This
                # prevents a worker body from borrowing another user's selection.
                await resolve_user_credential(session, org_id=grant.tenant_id, user_id=principal, credential_id=selected.id)
                credential_id, credential_secret_arn = selected.id, selected.secret_arn
            scope = CredentialScope.USER_GRANTED
        except CredentialNotFoundError:
            return Decision.block(DenyReason.CREDENTIAL_SCOPE_UNAVAILABLE, "approved user credential unavailable")

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
    decision = authorize_action(
        context,
        action,
        ResourceRef(
            repository_id=repo,
            org_id=grant.tenant_id,
            node_address=graph_address(node, flow_slug=flow.slug),
            user_credential_id=credential_id,
            aws_role_arn=aws_role_arns[0] if aws_role_arns else None,
        ),
        accepted_version,
    )
    if decision.permitted and scope is CredentialScope.USER_GRANTED:
        return WorkerCredentialDecision(
            permitted=True,
            detail=decision.detail,
            not_after=not_after,
            provider_permissions=True,
            credential_id=credential_id,
            credential_secret_arn=credential_secret_arn,
            credential_ids=credential_ids,
            aws_role_arns=aws_role_arns,
            policy_id=policy.policy_id,
            plan_version=inputs.plan_version,
        )
    if decision.permitted and broker_path == "/internal/v1/github-installation-token":
        return WorkerCredentialDecision(permitted=True, detail=decision.detail, permissions=permissions, not_after=not_after)
    if decision.permitted and broker_path == MEDIATED_GITHUB_OPERATION_PATH:
        # `scope` is still UNSCOPABLE unless the branch above matched, so a repo
        # outside the accepted policy or the grant is already refused by
        # `authorize_action`. Report the proven action and immutable repository ID
        # so the operation service never has to re-derive either one.
        return WorkerCredentialDecision(
            permitted=True,
            detail=decision.detail,
            not_after=not_after,
            action=action,
            provider_repository_id=int(repository_id) if repository_id.isdigit() else None,
            policy=policy,
            policy_id=policy.policy_id,
            plan_version=inputs.plan_version,
        )
    return decision
