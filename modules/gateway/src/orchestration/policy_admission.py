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
from dataclasses import dataclass
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
from .models import ClaimState, NodeKind, OrchestrationAcceptedPlan, OrchestrationFlow, OrchestrationNode, OrchestrationWorkClaim
from .state import NodeState

logger = logging.getLogger(__name__)

__all__ = [
    "AdmissionInputs",
    "action_for_node_kind",
    "authorize_node_dispatch",
    "load_in_force_policy",
    "resolve_authorization_context",
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
    if plan is None:
        return AdmissionInputs(policy=None, plan_version=0)

    raw = (plan.plan_document or {}).get("execution_policy")
    if raw is None:
        return AdmissionInputs(policy=None, plan_version=plan.version)

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
        if node.kind == NodeKind.GATE.value:
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
    """
    member_org_id, member_team_ids = await _member_facts(session, org_id=node.org_id, user_id=principal_user_id)
    from .work_admission import enabled as work_claims_enabled

    work_owned = True
    if work_claims_enabled():
        # A node belonging to a flow does not prove that flow owns the issue.
        # Check the same immutable binding and invocation the producer reserved.
        try:
            issue = int(str(node.issue_ref).lstrip("#"))
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

    action = action_for_node_kind(node.kind)
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

    # One ledger read, used for the rule's total and for the releases below.
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
    )

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
