"""Resolve live facts and admit (or refuse) an action under an accepted policy (#5128).

`execution_policy.authorize_action` is the *rule*: a pure function over facts. This
module is the *fact resolver* — the half that has to talk to the database and the
identity services, and the half where getting a default wrong silently converts a
denial into a permit. Keeping them apart is what makes the rule testable against
adversarial literals; see `execution_policy`'s module docstring.

**Every resolution here fails closed, and the shapes of "closed" differ:**

- A fact the context can represent as absent (`member_org_id`, `observed_spend_usd`)
  is passed as `None`, and the rule denies on it. This module never substitutes a
  convenient default for a fact it could not read — that substitution *is* the
  vulnerability, because `0` spend mints the full allowance again and a defaulted
  org id makes a revoked member look current.
- A fact the context cannot represent as absent means we do not call the rule at
  all and refuse at the call site.

**Why "missing usage is unknown, not zero" needs care rather than a literal reading.**
A node that has never run has no ledger row *by construction*, so on a fresh flow
every node's cost is `UNKNOWN`. Treating any `UNKNOWN` as unreconciled spend would
block the first dispatch of every flow forever — a total engine outage dressed up as
a safety property, and one that would look correct in a unit test that only ever
examined a single node.

The distinction that actually matters is between *expected* absence and
*unreconciled* absence:

- A node still `pending`/`ready` has not executed. Its absent cost is expected, and
  contributes nothing.
- A node that HAS executed (`running` onward) but has no ledger row is genuinely
  unknown spend — the reconciliation gap the issue names. That denies.
- A `gate` node never bills a model call, so its absence is expected in any state.

So spend is `None` (deny) only when something that ran cannot be accounted for, and
a number otherwise. The lower bound is never treated as a total.

**What this module deliberately does NOT do.** It does not mint credentials, does not
terminate running work (`authorize_action` admits; it does not revoke in flight), and
does not write the refusal anywhere — the caller owns its own audit trail and
transaction. It also does not reimplement membership, cost or plan reads; each is
delegated to the service that owns it, so this module cannot drift from them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.models.base import utcnow

from .cost import CostStatus, get_cost_by_address
from .dispatch import graph_address
from .execution_policy import (
    Action,
    AuthorizationContext,
    CredentialScope,
    Decision,
    DenyReason,
    ExecutionPolicy,
    ResourceRef,
    authorize_action,
)
from .flow_budget import release_flow_admission, reserve_flow_admission
from .models import (
    ClaimState,
    NodeKind,
    OrchestrationAcceptedPlan,
    OrchestrationAction,
    OrchestrationExecution,
    OrchestrationFlow,
    OrchestrationNode,
    OrchestrationWorkClaim,
)
from .state import NodeState

logger = logging.getLogger(__name__)

__all__ = [
    "AdmissionInputs",
    "action_for_node_kind",
    "authorize_coordinator_child_request",
    "authorize_node_dispatch",
    "load_in_force_policy",
    "resolve_authorization_context",
    "resolve_node_action",
]


# Node kinds map to the policy's action vocabulary. A `GATE` is deliberately absent:
# a gate is a human decision, and no autonomous action is admitted for one at all —
# mapping it to an `Action` would be the first step toward a policy that could
# "permit" clearing a human gate, which #5128 forbids outright.
_NODE_KIND_ACTIONS: dict[str, Action] = {
    NodeKind.STORY.value: Action.DEVELOP,
    NodeKind.EVAL.value: Action.EVALUATE,
}

# States in which a node has begun executing and could therefore have incurred
# model spend. A node outside this set has no ledger row *by construction*, which is
# an expected absence rather than an unreconciled one (see the module docstring).
_EXECUTED_STATES = frozenset(
    {
        NodeState.RUNNING.value,
        NodeState.AWAITING_MERGE.value,
        NodeState.AWAITING_GATE.value,
        NodeState.PASSED.value,
        NodeState.REJECTED_AT_GATE.value,
        NodeState.FAILED.value,
        NodeState.HALTED.value,
        NodeState.SUPERSEDED.value,
    }
)


def action_for_node_kind(kind: str) -> Action | None:
    """The policy action a node of this kind would perform, or `None`.

    `None` for a gate — and for any kind added later without a deliberate decision
    here. Returning `None` rather than raising lets the caller refuse a node it
    cannot classify instead of crashing a whole dispatch pass on one row, while
    still never guessing an action.
    """
    return _NODE_KIND_ACTIONS.get(kind)


def resolve_node_action(node: OrchestrationNode, *, action_override: Action | None = None, continuing_node: bool = False) -> Action | None:
    """The action dispatching THIS node would perform, repair promotion included.

    Extracted from :func:`authorize_node_dispatch` because #5224's coordinator path
    needs the same value to pass as `child_action` to `authorize_child_request`. Two
    sites deriving it independently is precisely the drift that produced the
    `schema_version == 2` downgrade this story already had to fix: a coordinator
    could be told a `develop` child was permitted while the child's own admission
    then evaluated `repair`, so the owner's accepted `allowed_child_actions` would
    not describe what actually ran.

    `None` for a kind with no autonomous action (a gate), so the caller refuses
    rather than guesses. `continuing_node` reproduces the existing off-by-one
    exactly: a continuation is re-authorizing the attempt already counted, so it
    compares against `attempts > 1` rather than `attempts > 0`.
    """
    if action_for_node_kind(node.kind) is None:
        return None
    if action_override is not None:
        return action_override
    if node.kind == NodeKind.STORY.value and node.attempts > int(continuing_node):
        return Action.REPAIR
    return action_for_node_kind(node.kind)


@dataclass(frozen=True)
class AdmissionInputs:
    """The policy in force for a flow, plus the plan version it was accepted on.

    A separate type so "there is no policy" (`policy is None`) stays distinguishable
    from "there is a policy and it happens to permit everything". The first
    preserves legacy semantics; the second is an authorization decision. Collapsing
    them would make an un-policied flow indistinguishable from a fully-permissive
    one in the logs.
    """

    policy: ExecutionPolicy | None
    plan_version: int
    refusal: Decision | None = None


async def load_in_force_policy(session: AsyncSession, *, org_id: str, flow_id: str) -> AdmissionInputs:
    """Read the accepted policy from the plan currently in force for this flow.

    Both `org_id` and `flow_id` are filtered in SQL, so a flow id belonging to
    another tenant resolves to nothing rather than to that tenant's policy.

    A malformed stored policy refuses this flow without crashing other flows.
    Only actual absence preserves legacy semantics; unreadable authority cannot
    remove the restrictions that a human accepted.
    """
    stmt = select(OrchestrationAcceptedPlan).where(
        OrchestrationAcceptedPlan.org_id == org_id,
        OrchestrationAcceptedPlan.flow_id == flow_id,
        OrchestrationAcceptedPlan.superseded_at.is_(None),
    )
    plan = (await session.execute(stmt)).scalar_one_or_none()
    raw = (plan.plan_document or {}).get("execution_policy") if plan is not None else None
    if raw is None:
        # Removing an accepted policy withdraws authority. It must not turn old
        # workers or the next dispatch into an unrestricted legacy flow.
        documents = await session.scalars(
            select(OrchestrationAcceptedPlan.plan_document).where(
                OrchestrationAcceptedPlan.org_id == org_id,
                OrchestrationAcceptedPlan.flow_id == flow_id,
            )
        )
        version = plan.version if plan is not None else 0
        if any((document or {}).get("execution_policy") is not None for document in documents):
            return AdmissionInputs(
                policy=None,
                plan_version=version,
                refusal=Decision.block(DenyReason.STALE_POLICY_VERSION, "accepted policy was removed; authority cannot revert to legacy access"),
            )
        return AdmissionInputs(policy=None, plan_version=version)

    try:
        return AdmissionInputs(policy=ExecutionPolicy.model_validate(raw), plan_version=plan.version)
    except ValueError:
        logger.exception(
            "orchestration admission: flow %s (org %s) plan v%s has an unparseable execution_policy — refusing admission",
            flow_id,
            org_id,
            plan.version,
        )
        return AdmissionInputs(
            policy=None,
            plan_version=plan.version,
            refusal=Decision.block(DenyReason.SCHEMA_UNSUPPORTED, "accepted execution policy cannot be validated"),
        )


async def _member_facts(session: AsyncSession, *, org_id: str, user_id: str) -> tuple[str | None, frozenset[str]]:
    """The principal's *current* org and team membership, or `(None, empty)`.

    `None` is what a revoked member looks like. Revocation in this codebase is a hard
    delete of the org-local `users` row, with both membership tables cascading off
    it, so "no row" is the whole signal — there is no status column to consult.

    Deliberately **no `is_active` filter** on `tenant_memberships`: that column marks
    which workspace the user currently has selected, not whether the membership is
    real (see `admin/memberships.py`). Filtering on it would make a member who is
    browsing another workspace read as revoked, which would halt their flows.

    Both lookups are delegated to the modules that own them, so a change to how
    membership is stored cannot leave a stale copy of the query here.
    """
    from src.admin import team_memberships
    from src.admin.exceptions import ResourceNotFoundError
    from src.shared.models.onboarding import TenantMembership

    membership = (
        await session.execute(
            select(TenantMembership.tenant_id).where(
                TenantMembership.user_id == user_id,
                TenantMembership.tenant_id == org_id,
            )
        )
    ).scalar_one_or_none()

    if membership is None:
        # A legacy native user can have no `tenant_memberships` row while still being
        # a real member via `users.org_id` — the same fallback `workspaces.py` makes
        # deliberately. Treating that as revoked would deny a legitimate member, so
        # the users row is consulted before concluding absence.
        from src.shared.models.organization import User

        native = (await session.execute(select(User.org_id).where(User.id == user_id, User.org_id == org_id))).scalar_one_or_none()
        if native is None:
            return None, frozenset()
        membership = native

    try:
        rows = await team_memberships.list_memberships(session, user_id=user_id, org_id=org_id)
        teams = frozenset(row.team_id for row in rows)
    except ResourceNotFoundError:
        # The user is not resolvable in this org after all. Fail closed on identity
        # rather than proceeding with an empty team set, which an org-level policy
        # (empty `team_ids`) would otherwise accept.
        return None, frozenset()

    return membership, teams


@dataclass(frozen=True)
class SpendObservation:
    """What the settled ledger says about a flow, and whose holds it makes stale.

    `total_usd` is `None` when something that ran cannot be accounted for, which
    denies. `settled_node_ids` names the nodes whose spend is now fully in the
    settled ledger and are no longer running, so their pessimistic in-flight holds
    are double-counting and can be released.

    Returned together because they come from one pass over one query result and are
    two readings of the same fact. Splitting them into two functions would mean two
    `get_cost_by_address` calls that could observe different ledger states — and a
    release decided against a *different* read than the total it is reconciling
    against is how a hold gets released for spend the total does not yet include.
    """

    total_usd: Decimal | None
    settled_node_ids: frozenset[str] = frozenset()


def _engine_evaluation():
    """A current K1 evaluation context proves this node is an observer, not a worker.

    Legacy eval workers still count. A running E2 observer neither bills model
    usage nor occupies a worker slot while its correction runs beneath it.
    """
    return (
        select(OrchestrationExecution.id)
        .join(OrchestrationAction, OrchestrationAction.execution_id == OrchestrationExecution.id)
        .where(
            OrchestrationExecution.org_id == OrchestrationNode.org_id,
            OrchestrationExecution.flow_id == OrchestrationNode.flow_id,
            OrchestrationExecution.node_id == OrchestrationNode.id,
            OrchestrationExecution.cycle == OrchestrationNode.attempts,
            OrchestrationAction.org_id == OrchestrationNode.org_id,
            OrchestrationAction.kind == "evaluation_context",
            OrchestrationAction.status == "succeeded",
        )
        .exists()
    )


async def _observed_spend(session: AsyncSession, *, org_id: str, flow_slug: str, nodes: list[OrchestrationNode]) -> SpendObservation:
    """Settled spend for a flow, and which nodes' holds it supersedes.

    A `None` total denies. See the module docstring for why this is not simply "any
    UNKNOWN denies": on a fresh flow every node is UNKNOWN by construction, and
    denying on that would block every flow's first dispatch permanently.

    Reserved-but-unsettled spend is **not** visible here — the ledger this reads is
    the settled one. That is a known lower bound, recorded rather than papered over,
    and it is why the flow binding carries the reservation side (`flow_budget.py`).
    A lower bound is the correct conservative input for a cap comparison only because
    the cap denies when the bound is already exceeded; it cannot admit spend the
    ledger has not yet seen, which is exactly what the reservation plane adds.

    **Why a `running` node is never releasable**, even with a usage row: a run bills
    incrementally, so a mid-run figure is a partial total and the run may spend up to
    the per-run cap more. Releasing its hold would leave that remainder bounded by
    nothing. Only a node that has stopped has a figure that will not grow.
    """
    measured = {cost.address: cost for cost in await get_cost_by_address(session, org_id=org_id, address_prefix=flow_slug)}

    eval_ids = [node.id for node in nodes if node.kind == NodeKind.EVAL.value]
    observers = (
        set(
            await session.scalars(
                select(OrchestrationNode.id).where(OrchestrationNode.org_id == org_id, OrchestrationNode.id.in_(eval_ids), _engine_evaluation())
            )
        )
        if eval_ids
        else set()
    )

    total = Decimal(0)
    settled: set[str] = set()
    for node in nodes:
        # `graph_address` rather than a local f-string: the ledger is grouped by
        # exactly that spelling, and a second one here would silently match nothing
        # and read every node as unmeasured.
        address = graph_address(node, flow_slug=flow_slug)
        cost = measured.get(address)
        if cost is not None and cost.status is not CostStatus.UNKNOWN:
            total += cost.amount_usd or Decimal(0)
            if node.state != NodeState.RUNNING.value:
                # Stopped and accounted for: this node's cost is in `total` now, so
                # its in-flight hold is the same dollars a second time.
                settled.add(node.id)
            continue
        # No usable figure. Whether that is expected depends on whether this node
        # ever ran, and on whether it is the kind of node that bills at all.
        if node.kind == NodeKind.GATE.value or node.id in observers:
            continue
        if node.state in _EXECUTED_STATES:
            logger.warning(
                "orchestration admission: flow %s (org %s) node %s is %s with no usage row — spend is UNKNOWN, refusing new spend until reconciled",
                flow_slug,
                org_id,
                address,
                node.state,
            )
            return SpendObservation(total_usd=None)
    return SpendObservation(total_usd=total, settled_node_ids=frozenset(settled))


async def _running_count(session: AsyncSession, *, org_id: str, flow_id: str) -> int:
    """Actions currently admitted under this flow — the concurrency observation.

    Counted from `running` nodes, which is engine-owned state written by
    `dispatch_node`, never a worker-reported number. A worker-writable count would
    let a run inflate its own headroom by under-reporting.
    """
    return (
        await session.execute(
            select(func.count())
            .select_from(OrchestrationNode)
            .where(
                OrchestrationNode.org_id == org_id,
                OrchestrationNode.flow_id == flow_id,
                OrchestrationNode.state == NodeState.RUNNING.value,
                ~((OrchestrationNode.kind == NodeKind.EVAL.value) & _engine_evaluation()),
            )
        )
    ).scalar_one()


async def resolve_authorization_context(
    session: AsyncSession,
    *,
    policy: ExecutionPolicy,
    plan_version: int,
    node: OrchestrationNode,
    principal_user_id: str,
    credential_scope: CredentialScope,
    spend: SpendObservation,
    provider_repository_id: int | None = None,
    expected_invocation_id: str | None = None,
    work_claim_issue: int | None = None,
) -> AuthorizationContext:
    """Build the fact set for one admission decision from live state.

    `principal_user_id` must be a canonical `users.id` — the namespace
    `tenant_memberships` and `team_memberships` are keyed on. Dispatch already
    resolves it from the attributed approver via `resolve_root_user_entity_id`, so
    nothing here accepts a Cognito sub and silently finds no membership for it
    (which would deny every dispatch and look like a policy bug).

    `credential_scope` is passed in rather than resolved here because only the caller
    knows what it was able to establish about the credential it will actually use.
    A resolver that guessed would be free to guess `SCOPED`.

    `spend` is passed in for the same reason, plus one specific to it: the caller also
    releases superseded holds from that same observation, and re-reading the ledger
    here would let the total the rule denies on and the releases performed against it
    come from two different reads of a moving ledger.

    `work_claim_issue` overrides which issue the claim is looked up by. It exists for
    #5224's coordinator: a wave coordinator holds the claim on its **launch** issue but
    is anchored, for policy purposes, to its wave's *evaluation* node — whose
    `issue_ref` is a different issue (or unset until the wave is materialized). Keying
    the lookup on the node would therefore find no held claim and deny every
    coordinator request once work claims are enabled. The default is unchanged, so no
    existing caller's lookup moves, and this narrows rather than widens: an explicit
    issue must still match a claim held by this same flow and invocation.
    """
    member_org_id, member_team_ids = await _member_facts(session, org_id=node.org_id, user_id=principal_user_id)
    from src.admin.access_control import AccessControl
    from src.admin.config import Permission, membership_role_to_admin_role
    from src.shared.identity.workspaces import memberships_for_login

    # Use the existing membership resolver and permission map, requiring an
    # actual current role. The legacy RBAC rollback flag must not turn a missing
    # membership into approval authority for an accepted bounded policy.
    principal_can_authorize = False
    if member_org_id is not None:
        _, memberships = await memberships_for_login(session, principal_user_id)
        pair = memberships.get(node.org_id)
        if pair is not None and pair[1] is not None:
            role = membership_role_to_admin_role(pair[1].role)
            principal_can_authorize = Permission.PLAN_APPROVE in AccessControl(session).get_role_permissions(role)
    from .work_admission import enabled as work_claims_enabled

    work_owned = True
    if work_claims_enabled():
        # A node belonging to a flow does not prove that flow owns the issue.
        # Check the same immutable binding and invocation the producer reserved.
        try:
            issue = work_claim_issue if work_claim_issue is not None else int(str(node.issue_ref).lstrip("#"))
        except (ValueError, TypeError):
            issue = 0
        work_owned = bool(
            provider_repository_id
            and expected_invocation_id
            and issue > 0
            and await session.scalar(
                select(OrchestrationWorkClaim.id).where(
                    OrchestrationWorkClaim.org_id == node.org_id,
                    OrchestrationWorkClaim.provider_repository_id == provider_repository_id,
                    OrchestrationWorkClaim.issue_number == issue,
                    OrchestrationWorkClaim.owner_kind == "engine_flow",
                    OrchestrationWorkClaim.owner_ref == node.flow_id,
                    OrchestrationWorkClaim.active_run_id == expected_invocation_id,
                    OrchestrationWorkClaim.state == ClaimState.HELD.value,
                )
            )
        )

    return AuthorizationContext(
        policy=policy,
        # Equal by construction at this call site: the policy was read *from* the
        # plan in force, so there is no version skew to detect here. The pair is not
        # redundant in general — #5122's execution rows pin the version an action was
        # admitted under, and comparing a pinned version against the current one is
        # what catches an action still in flight across an amendment. Passing the
        # same value twice is honest about what this caller can observe rather than
        # inventing a second reading of the same row.
        accepted_plan_version=plan_version,
        in_force_plan_version=plan_version,
        principal_id=principal_user_id,
        member_org_id=member_org_id,
        member_team_ids=member_team_ids,
        principal_can_authorize=principal_can_authorize,
        # Grant revocation lives in the agentauth plane, which is keyed by run rather
        # than by flow and has no row until a run exists. There is nothing to consult
        # before the first dispatch, so this is False here and the membership and
        # expiry checks carry the revocation weight at admission. Left explicit
        # rather than omitted so the gap is visible to a reader.
        grant_revoked=False,
        now=utcnow(),
        credential_scope=credential_scope,
        observed_spend_usd=spend.total_usd,
        observed_attempts=node.attempts,
        observed_concurrency=await _running_count(session, org_id=node.org_id, flow_id=node.flow_id),
        work_owned_by_policy_flow=work_owned,
    )


async def authorize_node_dispatch(
    session: AsyncSession,
    *,
    node: OrchestrationNode,
    principal_user_id: str,
    target_repository: str,
    installation_resolved: bool,
    provider_repository_id: int | None = None,
    expected_invocation_id: str | None = None,
    action_override: Action | None = None,
    continuing_node: bool = False,
    work_claim_issue: int | None = None,
) -> Decision:
    """Admit or refuse dispatching one node under its flow's accepted policy.

    The flow's slug and sibling nodes are resolved here from the node's own
    `flow_id` rather than accepted as parameters. That is deliberate: a caller
    passing another flow's node list would silently compute spend against the wrong
    flow, and the resulting permit would look entirely normal. Deriving them from the
    node makes that mismatch unrepresentable.

    Returns a permit with an explanatory detail when the flow has **no** policy:
    absence preserves legacy semantics exactly (#5128), and this function is the
    only place that decision is made, so a future caller cannot accidentally invert
    it by treating a missing policy as a refusal.

    `target_repository` is matched against the policy's `repository_ids` verbatim, so
    a policy must name repositories in the same form dispatch is configured with
    (`owner/name`). Normalising the two forms here would mean guessing which owner a
    bare name referred to, and a wrong guess admits work against the wrong
    repository. A mismatch denies with `repository_not_permitted`, which is a
    legible operator error rather than a silent widening.
    """
    inputs = await load_in_force_policy(session, org_id=node.org_id, flow_id=node.flow_id)
    if inputs.refusal is not None:
        return inputs.refusal
    if inputs.policy is None:
        return Decision.permit("no execution policy in force; legacy semantics apply")

    from .shared_policy import authorize_shared_dispatch, is_shared_continuation

    if await is_shared_continuation(session, org_id=node.org_id, flow_id=node.flow_id):
        if continuing_node:
            return Decision.block(DenyReason.ACTION_NOT_PERMITTED, "Shared continuation actions use their authenticated execution controller.")
        if not installation_resolved:
            return Decision.block(DenyReason.CREDENTIAL_SCOPE_UNAVAILABLE, "The repository installation is unresolved.")
        return await authorize_shared_dispatch(
            session,
            node=node,
            principal_user_id=principal_user_id,
            target_repository=target_repository,
            provider_repository_id=provider_repository_id,
            expected_invocation_id=expected_invocation_id,
            action_override=action_override,
        )

    from .runtime_policy import flow_started_at, policy_github_permissions

    started = await flow_started_at(session, org_id=node.org_id, flow_id=node.flow_id)
    if started is not None and (utcnow() - started).total_seconds() >= inputs.policy.limits.max_wall_clock_seconds:
        return Decision.block(DenyReason.WALL_CLOCK_LIMIT_EXCEEDED, "flow execution deadline is exhausted")

    flow_slug = (
        await session.execute(
            select(OrchestrationFlow.slug).where(
                OrchestrationFlow.org_id == node.org_id,
                OrchestrationFlow.id == node.flow_id,
            )
        )
    ).scalar_one_or_none()
    if flow_slug is None:
        # A node whose flow is missing has no address, so its spend cannot be
        # attributed and its limits cannot be evaluated. `dispatch_node` refuses this
        # case too; refusing here as well means the policy path never evaluates
        # limits against an address it could not build.
        return Decision.block(
            DenyReason.WORK_NOT_OWNED,
            f"node references missing flow {node.flow_id!r}",
        )

    flow_nodes = list(
        (
            await session.execute(
                select(OrchestrationNode).where(
                    OrchestrationNode.org_id == node.org_id,
                    OrchestrationNode.flow_id == node.flow_id,
                )
            )
        )
        .scalars()
        .all()
    )

    action = resolve_node_action(node, action_override=action_override, continuing_node=continuing_node)
    if action is None:
        # A gate is never dispatched, and an unclassifiable kind must not be guessed.
        return Decision.block(
            DenyReason.ACTION_NOT_PERMITTED,
            f"node kind {node.kind!r} has no autonomous action under an execution policy",
        )

    # Only claim a scoped credential when the two things this caller can actually
    # verify both hold: the installation resolved to exactly one owner, and the
    # policy names the repository the run will act on. When they do, the token the
    # run receives is minted for that single repository with the least-privilege
    # agent permission set (`internal/routes.py` mints it per repo), so the scope is
    # real rather than assumed. When they do not, `UNKNOWN` is passed and the rule
    # blocks — which is the point: there is no branch here that reaches for a
    # broader platform credential when the narrow one cannot be established.
    scope = CredentialScope.UNKNOWN
    if installation_resolved and target_repository in inputs.policy.repository_ids:
        scope = CredentialScope.SCOPED if policy_github_permissions(inputs.policy, action) is not None else CredentialScope.UNSCOPABLE

    # A continuation uses its initialized provider accumulator even before the
    # first asynchronous usage row arrives. Fresh admissions also reconcile the
    # settled ledger's completed-node holds.
    if continuing_node:
        from .flow_meter import read_flow_meter

        if node.state != NodeState.RUNNING.value:
            return Decision.block(DenyReason.WORK_NOT_OWNED, "continuation no longer belongs to a running node")
        meter = await read_flow_meter(org_id=node.org_id, flow_id=node.flow_id, policy=inputs.policy)
        spend = SpendObservation(total_usd=meter.total_usd if meter is not None else None)
    else:
        spend = await _observed_spend(session, org_id=node.org_id, flow_slug=flow_slug, nodes=flow_nodes)

    context = await resolve_authorization_context(
        session,
        policy=inputs.policy,
        plan_version=inputs.plan_version,
        node=node,
        principal_user_id=principal_user_id,
        credential_scope=scope,
        spend=spend,
        provider_repository_id=provider_repository_id,
        expected_invocation_id=expected_invocation_id,
        work_claim_issue=work_claim_issue,
    )
    if continuing_node:
        context = replace(context, observed_attempts=max(0, node.attempts - 1), observed_concurrency=max(0, context.observed_concurrency - 1))

    resource = ResourceRef(
        repository_id=target_repository,
        node_address=graph_address(node, flow_slug=flow_slug),
        org_id=node.org_id,
    )

    decision = authorize_action(context, action, resource, inputs.plan_version)
    if not decision.permitted:
        return decision

    # --- The flow's shared allowance, held transactionally (#5128 step 4). ---
    #
    # Deliberately AFTER the rule, and only on a permit. Reserving first would hold
    # headroom for an action that a membership, scope or expiry check then refused,
    # and that hold is released by nothing — the caller never dispatches, so no
    # results pass ever reconciles it. A flow could then be starved of its own
    # allowance by repeatedly attempting work its policy forbids.
    #
    # `observed_spend_usd` is not None here: the rule blocks on `SPEND_UNKNOWN`
    # before reaching this line, which is what makes "prevent new spend until
    # reconciled" hold ahead of the reservation rather than inside it.
    #
    # Release superseded holds FIRST, so the headroom a finished sibling gave back is
    # available to this admission rather than only to the next one. Done here, at
    # admission, rather than when the results pass observes completion: a run's cost
    # reaches `usage_logs` minutes after it stops, so releasing at observation would
    # open a window where the spend counts in neither term. Sweeping at admission is
    # self-healing instead of ordering-dependent — it needs no successful callback,
    # so a crashed pod or a missed results pass cannot leak a hold for the full TTL.
    for settled_node_id in spend.settled_node_ids:
        await release_flow_admission(
            org_id=node.org_id,
            flow_id=node.flow_id,
            policy=inputs.policy,
            settled_usd=spend.total_usd or Decimal(0),
            node_id=settled_node_id,
        )

    reservation = await reserve_flow_admission(
        org_id=node.org_id,
        flow_id=node.flow_id,
        policy=inputs.policy,
        settled_usd=context.observed_spend_usd or Decimal(0),
        node_id=node.id,
    )
    if not reservation.admitted:
        if reservation.degraded:
            return Decision.block(DenyReason.BUDGET_UNAVAILABLE, "flow reservations are unavailable; no new policy-governed work can be admitted")
        # The same typed reason the settled-ledger check uses. A caller cannot act
        # differently on "over cap by the ledger" versus "over cap once concurrent
        # admissions are counted" — both mean this delivery has spent what it was
        # granted — and a second reason would imply a distinction #5122 would then
        # have to render.
        return Decision.block(
            DenyReason.SPEND_LIMIT_EXCEEDED,
            f"flow allowance of ${inputs.policy.limits.max_spend_usd} is exhausted once in-flight admissions are counted",
        )

    from .flow_meter import prepare_flow_meter

    if not await prepare_flow_meter(org_id=node.org_id, flow_id=node.flow_id, policy=inputs.policy, nodes=flow_nodes):
        await release_flow_admission(
            org_id=node.org_id, flow_id=node.flow_id, policy=inputs.policy, settled_usd=context.observed_spend_usd or Decimal(0), node_id=node.id
        )
        return Decision.block(DenyReason.BUDGET_UNAVAILABLE, "shared model allowance is unavailable; existing usage must be reconciled")

    return decision


async def authorize_coordinator_child_request(
    session: AsyncSession,
    *,
    coordinator_node: OrchestrationNode,
    child_node: OrchestrationNode,
    child_persona: str,
    principal_user_id: str,
    inputs: AdmissionInputs,
    child_action_override: Action | None = None,
    continuing_child: bool = False,
    expected_invocation_id: str | None = None,
    provider_repository_id: int | None = None,
    work_claim_issue: int | None = None,
) -> Decision:
    """May this coordinator REQUEST this child? **Not the child's own admission** (#5224).

    The coordinator half of a child dispatch. The caller must still run the child's
    own :func:`authorize_node_dispatch` — this establishes only that the coordinator
    was allowed to ask, and `graph_dispatch` calls both. Collapsing them would let
    coordinator authority substitute for the child's accepted action, live claim and
    bounded grant, which is the substitution the issue forbids outright.

    **Nothing here reserves budget.** Asking is not spending: the child's own
    admission holds the headroom against the same shared flow allowance, so a
    coordinator cannot charge the meter twice by fanning out, and a refused child
    leaves no hold behind. The coordinator's request is still *bounded* by that
    allowance, because the `authorize_action` inside `authorize_child_request`
    observes the same spend, attempt and concurrency facts — a coordinator whose flow
    is exhausted cannot keep asking.

    Facts are resolved against the COORDINATOR's own node, immediately before the
    request. `resolve_authorization_context` therefore re-reads ownership, policy
    version, current membership, role and the shared limits on every call, so an
    amendment that narrows the assigned node set, a revoked membership or an
    exhausted allowance takes effect at the next child request rather than at the
    next acceptance.

    `expected_invocation_id`, `provider_repository_id` and `work_claim_issue` describe
    the COORDINATOR's claim, not the child's. The child's claim is checked by its own
    admission; passing the child's identity here would ask whether the child owned work
    it has not been dispatched for yet.
    """
    if inputs.policy is None:  # pragma: no cover - callers check absence first
        return Decision.permit("no execution policy in force; legacy semantics apply")

    child_action = resolve_node_action(child_node, action_override=child_action_override, continuing_node=continuing_child)
    if child_action is None:
        # A gate, or a kind this build cannot classify. Refused rather than guessed:
        # a coordinator must not be able to request work whose action nobody can name.
        return Decision.block(
            DenyReason.CHILD_ACTION_NOT_PERMITTED,
            f"child node kind {child_node.kind!r} has no autonomous action a coordinator could request",
        )

    flow_slug = await session.scalar(
        select(OrchestrationFlow.slug).where(
            OrchestrationFlow.org_id == coordinator_node.org_id,
            OrchestrationFlow.id == coordinator_node.flow_id,
        )
    )
    if flow_slug is None:
        # Without a slug there is no address, so the accepted node set cannot be
        # compared against anything. The same refusal `authorize_node_dispatch` makes.
        return Decision.block(DenyReason.WORK_NOT_OWNED, f"coordinator node references missing flow {coordinator_node.flow_id!r}")

    # The coordinator's own spend observation, over its flow's nodes. Read through
    # the meter rather than the settled ledger: the coordinator is mid-run, so its
    # own initialized accumulator is the current reading, exactly as the
    # `continuing_node` branch of `authorize_node_dispatch` does.
    from .flow_meter import read_flow_meter

    meter = await read_flow_meter(org_id=coordinator_node.org_id, flow_id=coordinator_node.flow_id, policy=inputs.policy)
    spend = SpendObservation(total_usd=meter.total_usd if meter is not None else None)

    context = await resolve_authorization_context(
        session,
        policy=inputs.policy,
        plan_version=inputs.plan_version,
        node=coordinator_node,
        principal_user_id=principal_user_id,
        # A coordinator needs no provider credential to ask: `COORDINATE` is not in
        # `_REPOSITORY_ACTIONS`, and the request goes to the platform's own
        # authenticated dispatch service. The child's admission establishes the
        # child's credential scope, which is where a provider token is actually
        # minted.
        #
        # `SCOPED` is nonetheless the correct value and not a convenience: the
        # credential test in `authorize_child_request` blocks anything that is neither
        # `SCOPED` nor an explicit v2 user grant, and it makes no exception for
        # non-repository actions. Passing `UNSCOPABLE` to mean "no provider credential
        # is involved" would therefore refuse every coordinator request with
        # `credential_scope_unavailable` — a deny reason directing an operator to go
        # implement a provider capability that this path never needed. The narrow
        # reading is the accurate one: a request carrying no provider credential cannot
        # exceed this policy's repositories, which is exactly what `SCOPED` asserts.
        credential_scope=CredentialScope.SCOPED,
        spend=spend,
        provider_repository_id=provider_repository_id,
        expected_invocation_id=expected_invocation_id,
        work_claim_issue=work_claim_issue,
    )
    # The coordinator's own admitted attempt and concurrency slot are already counted
    # — it is running right now. Requesting a child must not be charged against them a
    # second time, the same correction `authorize_worker_credential` applies when it
    # revalidates an already-admitted action.
    context = replace(
        context,
        observed_attempts=max(0, coordinator_node.attempts - 1),
        observed_concurrency=max(0, context.observed_concurrency - 1),
    )

    from .execution_policy import authorize_child_request

    return authorize_child_request(
        context,
        child_persona,
        child_action,
        ResourceRef(
            org_id=coordinator_node.org_id,
            node_address=graph_address(coordinator_node, flow_slug=flow_slug),
        ),
        inputs.plan_version,
    )
