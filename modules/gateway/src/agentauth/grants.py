"""Delegated authority: what a verified execution identity may actually do (#5028).

## Why a credential is not enough

:mod:`src.agentauth.run_credential` establishes *which run* is calling. It says
nothing about permissions. Keeping the two apart is deliberate — the alternative
is a credential carrying its own permission claims, which makes every credential
renewal a chance to widen scope and makes revocation impossible without
revoking identity.

## Why not `is_human_rooted`

The chain already records `root_human_id` and `is_human_rooted` on the
webhook-events row, and it is tempting to treat "human-rooted" as authority. It
is not. Those fields record *provenance* — that some human started this chain —
and provenance is not a grant to control every run that shares it. A coordinator
and an unrelated developer run can share a root human and a tenant while having
no legitimate control relationship at all. Worse, the worker IAM role can write
that row (`scaledjob-iam.tf` `DynamoDBWebhookEventsUpdate`), so a boolean stored
there is attacker-writable.

So authority here derives from an :class:`AuthorityReference` pointing at a real
recorded human decision — for AI-DLC, the existing verified gate-decision /
genesis model — and the grant itself lives in a store with **no worker write
path**.

## Relationship authority is resolved, never asserted

A grant may name explicit ``target_run_ids``, or it may name a
:class:`TargetRelationship` the *service* resolves (e.g. "runs this flow node
dispatched"). What a grant may never do is let the caller describe its own
relationship to the target. "I am this run's parent" supplied by the caller is
the forged-lineage attack with extra steps.

Sibling, ancestor, cross-flow and cross-tenant targets are refused unless the
grant explicitly permits that specific relationship or names the run. Shared
tenancy alone confers nothing (AC3).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum


class AgentAction(StrEnum):
    """Actions the shared authorization policy arbitrates.

    Spawn and the control verbs live in one vocabulary because they go through
    one policy. A separate enum per adapter is how two authorization checks that
    agree on the day they are written start disagreeing.
    """

    # Dispatch another persona onto an issue (today's `adp-trigger` behaviour).
    DISPATCH = "dispatch"
    # Read a run's status / control state.
    MONITOR = "monitor"
    PAUSE = "pause"
    RESUME = "resume"
    STEER = "steer"
    ABORT = "abort"


# Actions that mutate a running agent. Split out because these require an
# explicit per-action grant entry — a grant conveying MONITOR must never imply
# any of these, which is what a single "control" permission would have done.
LIVE_CONTROL_ACTIONS: frozenset[AgentAction] = frozenset({AgentAction.PAUSE, AgentAction.RESUME, AgentAction.STEER, AgentAction.ABORT})


class TargetRelationship(StrEnum):
    """Server-resolvable relationships between caller and target run."""

    # The caller's own run. Always permitted for MONITOR; this is the only
    # relationship that needs no explicit grant entry, because a run reading its
    # own status cannot escalate anything.
    SELF = "self"
    # A run the caller dispatched, directly or transitively within its flow.
    DESCENDANT = "descendant"
    # A run assigned to a flow node the caller coordinates.
    FLOW_NODE = "flow_node"


# MONITOR on SELF is authorized without a grant entry. Anything else needs one.
_IMPLICIT_SELF_ACTIONS: frozenset[AgentAction] = frozenset({AgentAction.MONITOR})


class GrantRefusedError(Exception):
    """A grant could not authorize the request.

    Deliberately carries no detail about *why* beyond a coarse reason, and the
    HTTP layer collapses these to a single status. A refusal that distinguished
    "no such grant" from "grant does not cover this target" would let a caller
    map another flow's structure by probing.
    """


@dataclass(frozen=True)
class AuthorityReference:
    """A pointer to the real human authorization event a grant derives from.

    Not a boolean, not a name, not comment text — a reference to a row that a
    human's action created and that workers cannot write. ``kind`` records which
    verified model produced it so a future non-AI-DLC initiation path adds a
    kind rather than loosening this one.
    """

    # Which verified authorization model produced this. "gate_decision" is the
    # AI-DLC path (orchestration_decisions row, actor_kind=HUMAN).
    kind: str
    # Opaque ID of that row.
    reference_id: str
    # The human whose act it was, as recorded ON THE ROW. Carried for audit
    # attribution only: the acting principal stays an agent/service. This
    # naming a human does not make the caller a human (AC7).
    human_id: str
    org_id: str

    def __post_init__(self) -> None:
        if not self.kind or not self.reference_id or not self.human_id or not self.org_id:
            raise GrantRefusedError("incomplete authority reference")


@dataclass(frozen=True)
class DelegatedGrant:
    """One caller's delegated authority within one flow.

    Every field is resolved from the authority store. Nothing here is ever built
    from request data — a caller-constructible grant is not a grant.
    """

    grant_id: str
    tenant_id: str
    # The execution identity this grant is issued to: "<invocation_id>#<attempt>".
    # Attempt-scoped so a grant does not survive into a different attempt of the
    # same invocation, which is a different pod with a different compromise story.
    principal: str
    authority: AuthorityReference
    allowed_actions: frozenset[AgentAction]
    # Explicitly enumerated target runs. Empty is normal — most grants work
    # through relationships instead.
    target_run_ids: frozenset[str] = frozenset()
    # Relationships the service may resolve to authorize a target.
    target_relationships: frozenset[TargetRelationship] = frozenset()
    flow_id: str | None = None
    repo_scope: frozenset[str] = frozenset()
    expires_at: datetime | None = None
    # Bumped by the authority store on revocation and on any narrowing edit.
    # Envelopes carry the epoch they were issued under so a queued action can be
    # revalidated against the current epoch before it executes (AC6).
    revocation_epoch: int = 1
    revoked: bool = False
    # Concurrency/budget ceilings applied by the policy, not by the caller.
    max_dispatch_concurrency: int = 0
    max_chain_depth: int = 0
    # Actions a child dispatched under this grant may inherit. Must be a subset
    # of allowed_actions — enforced in __post_init__, because a delegable set
    # wider than the grant itself IS privilege expansion (AC7).
    delegable_actions: frozenset[AgentAction] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if not self.delegable_actions <= self.allowed_actions:
            raise GrantRefusedError("delegable actions exceed the grant")

    def is_live(self, now: datetime) -> bool:
        if self.revoked:
            return False
        if self.expires_at is not None and now >= self.expires_at:
            return False
        return True

    def permits_action(self, action: AgentAction) -> bool:
        return action in self.allowed_actions

    def child_grant_actions(self, requested: frozenset[AgentAction]) -> frozenset[AgentAction]:
        """Narrow a requested child action set to what may actually be inherited.

        Intersection, never union. A child asking for more than the parent holds
        gets the parent's subset rather than an error, so a legitimate
        over-broad request still dispatches with correct (narrower) authority
        instead of failing the flow.
        """
        return frozenset(requested) & self.delegable_actions


@dataclass(frozen=True)
class TargetFacts:
    """What the service independently resolved about the target run.

    Every field is read from authoritative state by the policy's resolver. The
    caller supplies only the target run ID; if any field here could come from
    the request, the relationship checks below would be self-certified.
    """

    run_id: str
    tenant_id: str
    flow_id: str | None = None
    # Relationships the resolver established between caller and target. A set,
    # because a target can legitimately be both a descendant and a flow node.
    relationships: frozenset[TargetRelationship] = frozenset()
    # Current run generation, for envelope binding.
    generation: int = 0
    is_terminal: bool = False
    repo: str | None = None


@dataclass(frozen=True)
class AuthorizationDecision:
    """The recorded outcome of one authorization question.

    Produced for allowed *and* refused requests alike (AC7). Carries the caller,
    the authority it derived from, the target, the action and the outcome — and
    deliberately no credential material, envelope signature, or instruction text.
    An audit record that quoted a steer instruction would be an audit record
    that leaks the thing the instruction was.
    """

    allowed: bool
    principal: str
    action: AgentAction
    target_run_id: str
    tenant_id: str
    reason: str
    grant_id: str | None = None
    authority_kind: str | None = None
    authority_reference_id: str | None = None
    human_authority_id: str | None = None
    revocation_epoch: int | None = None

    def to_log_fields(self) -> dict[str, object]:
        """Flatten for structured logging. Never includes secrets."""
        return {
            "allowed": self.allowed,
            "principal": self.principal,
            "action": self.action.value,
            "target_run_id": self.target_run_id,
            "tenant_id": self.tenant_id,
            "reason": self.reason,
            "grant_id": self.grant_id,
            "authority_kind": self.authority_kind,
            "authority_reference_id": self.authority_reference_id,
            "human_authority_id": self.human_authority_id,
            "revocation_epoch": self.revocation_epoch,
        }


def evaluate_grant(
    *,
    grant: DelegatedGrant | None,
    action: AgentAction,
    target: TargetFacts,
    caller_tenant_id: str,
    caller_principal: str,
    now: datetime | None = None,
) -> AuthorizationDecision:
    """Decide one request. Pure function of resolved facts — no I/O, no request data.

    Being pure is what makes the adversarial cases (AC2, AC3) unit-testable
    without a cluster: every refusal below is reachable from a constructed
    ``TargetFacts`` plus grant.

    Check order:

    1. **Tenant.** Cross-tenant is refused before anything else is consulted, so
       no other check can accidentally admit it.
    2. **Implicit self-monitor.** A run reading its own status needs no grant.
       This keeps the common case working without provisioning grants for it.
    3. **Grant presence and liveness** (expiry, revocation).
    4. **Action** — membership in ``allowed_actions``. Live control verbs get no
       implicit path whatsoever.
    5. **Target** — explicit ID, or a relationship the *resolver* found and the
       grant permits.
    """
    current = now or datetime.now(UTC)

    # The AUTHENTICATED caller, passed in by the policy from the verified
    # credential — not read off the grant. Those differ in exactly the case AC7
    # cares about: a grantless self-read has no grant to name a principal, and
    # deriving it from the grant recorded the allowed decision as "unknown" while
    # the caller was in fact fully authenticated. An audit trail that cannot name
    # the caller of an allowed request does not satisfy "auditable caller".
    #
    # A grant whose principal disagrees with the credential is a provisioning bug
    # or a store compromise, and is refused below rather than silently preferred
    # in either direction.
    principal = caller_principal

    def refuse(reason: str) -> AuthorizationDecision:
        return AuthorizationDecision(
            allowed=False,
            principal=principal,
            action=action,
            target_run_id=target.run_id,
            tenant_id=caller_tenant_id,
            reason=reason,
            grant_id=grant.grant_id if grant is not None else None,
            authority_kind=grant.authority.kind if grant is not None else None,
            authority_reference_id=grant.authority.reference_id if grant is not None else None,
            human_authority_id=grant.authority.human_id if grant is not None else None,
            revocation_epoch=grant.revocation_epoch if grant is not None else None,
        )

    # 1. Tenant isolation. Checked against the caller's *credential* tenant and
    # the resolver's target tenant — neither is request-supplied.
    if not caller_tenant_id or target.tenant_id != caller_tenant_id:
        return refuse("cross_tenant")

    # 2. A run monitoring itself. No grant needed, no escalation possible.
    if TargetRelationship.SELF in target.relationships and action in _IMPLICIT_SELF_ACTIONS:
        return AuthorizationDecision(
            allowed=True,
            principal=principal,
            action=action,
            target_run_id=target.run_id,
            tenant_id=caller_tenant_id,
            reason="self_monitor",
            grant_id=grant.grant_id if grant is not None else None,
            authority_kind=grant.authority.kind if grant is not None else None,
            authority_reference_id=grant.authority.reference_id if grant is not None else None,
            human_authority_id=grant.authority.human_id if grant is not None else None,
            revocation_epoch=grant.revocation_epoch if grant is not None else None,
        )

    if grant is None:
        return refuse("no_grant")
    if grant.tenant_id != caller_tenant_id:
        return refuse("cross_tenant")
    # The grant must belong to the authenticated caller. The store is asked for
    # the caller's principal so this should always hold; it is checked anyway
    # because "the lookup key matched" and "this grant names this principal" are
    # different statements, and only the second is the authority claim. A store
    # bug or a tampered record that returned another principal's grant would
    # otherwise be honoured.
    if grant.principal != caller_principal:
        return refuse("grant_principal_mismatch")
    if not grant.is_live(current):
        return refuse("grant_revoked" if grant.revoked else "grant_expired")
    if grant.repo_scope and target.repo not in grant.repo_scope:
        return refuse("repository_not_permitted")

    # 4. The action itself. Note there is no "control implies monitor" or
    # "abort implies pause" shortcut: each verb is granted or it is not.
    if not grant.permits_action(action):
        return refuse("action_not_granted")

    # 5. The target. Explicit enumeration first, then resolved relationships
    # intersected with what the grant permits. A relationship the resolver
    # found but the grant does not permit is still a refusal — the resolver
    # reports facts, the grant confers authority.
    if target.run_id in grant.target_run_ids:
        matched = "explicit_target"
    else:
        permitted = target.relationships & grant.target_relationships
        if not permitted:
            return refuse("target_not_permitted")
        # A flow-scoped grant must not reach outside its flow even when a
        # relationship matches, which is what blocks cross-flow control.
        if grant.flow_id is not None and target.flow_id != grant.flow_id:
            return refuse("cross_flow")
        matched = sorted(r.value for r in permitted)[0]

    return AuthorizationDecision(
        allowed=True,
        principal=principal,
        action=action,
        target_run_id=target.run_id,
        tenant_id=caller_tenant_id,
        reason=matched,
        grant_id=grant.grant_id,
        authority_kind=grant.authority.kind,
        authority_reference_id=grant.authority.reference_id,
        human_authority_id=grant.authority.human_id,
        revocation_epoch=grant.revocation_epoch,
    )
