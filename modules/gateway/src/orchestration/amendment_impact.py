"""What accepting this amendment would actually do to the graph.

Issue #5329 (EPIC #4191), on top of #4529's pending-amendment store.

--------------------------------------------------------------------------------
The hole this closes
--------------------------------------------------------------------------------

#4529 shows a reviewer two things before they accept a draft: the gate diff, and the
recorded base version. Both matter, and neither answers the question a person is
actually being asked. "Accept amendment `abc123`?" is a request to authorize unattended
execution of a graph the reviewer has not seen, whose difference from the graph in force
is expressed as two JSON documents, and whose consequences depend on execution state
that appears in neither document.

This module produces the report that makes the question answerable:

* **What changes structurally** — nodes and edges added and removed, and, for nodes
  that persist, whether their prerequisites move.
* **What it costs** — which existing nodes are affected and what state each is in *right
  now*, because "this supersedes a node" reads completely differently when that node is
  `pending` versus `running` versus `passed`.
* **What the engine will then do** — the proposed execution order and what runs
  concurrently, derived from the edges rather than from document order.
* **Whether it can be accepted at all** — every blocking diagnostic the acceptance path
  would raise, found before the reviewer approves rather than as a 4xx afterwards.

--------------------------------------------------------------------------------
Why blockers are predicted by importing the rules, not by restating them
--------------------------------------------------------------------------------

A preview that reports "no problems" and is then refused at acceptance is worse than no
preview: it teaches the reviewer that the diagnostics are noise. So every blocker here
comes from the acceptance path's own code —

* document validity: `proposal.validate_proposal`, the authoritative validator
  `amend_plan` itself runs;
* started-node prerequisites: `amend.prerequisites_change_refused`;
* definition changes on a reused address: `amend.definition_change_refusal`, which
  returns the refusal message `amend_plan` raises;
* superseding an existing node: `state.transition`, which is what
  `_supersede_absent_nodes` calls, including its human-only `passed -> superseded` edge;
* in-flight work a new plan version would strand: `shared_amendment.quiescence_failures`;
* effective limits (receipts applied) that acceptance would lower, and new-work issue
  publication (`amendment_issue_publisher.plan_publications`).

`accept_amendment` runs this same assessment under the flow lock and refuses on any
blocker, so these are enforced, not merely predicted.

That last one is the case a diff cannot show. A `SERVICE` actor's amendment that drops a
`passed` node is refused by the state machine, not by any rule about plans, and the
reviewer would otherwise see only "1 node removed".

The rules are imported, so this module cannot drift from them. Where the acceptance path
raises on the *first* problem, this reports *all* of them: a reviewer fixing one blocker
at a time through a route that refuses at the first is doing the engine's bisection by
hand.

--------------------------------------------------------------------------------
Why the report reads state and the placement resolver does not
--------------------------------------------------------------------------------

`amendment_placement` is pure over two documents and deliberately knows nothing about
execution. This module needs the graph rows: "your anchor is already running" is not a
fact about any document. Keeping the split means placement stays unit-testable without a
database and this stays the single place where a preview consults live state.

--------------------------------------------------------------------------------
What this module does not do
--------------------------------------------------------------------------------

It writes nothing — no rows, no decisions, not even a read lock. It is safe to call
repeatedly and on every keystroke of an edit loop, which is what makes "preview before
approve" cheap enough to be mandatory.

It also does not decide. `blocking` being empty means the acceptance path has no
*predictable* refusal; it does not mean acceptance will succeed, because the base plan
can move between the preview and the accept. That comparison is `accept_amendment`'s
exact-hash check and it stays there — a preview that promised acceptance would be
promising something it cannot hold.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .amend import binding_reservations, definition_change_refusal, prerequisites_change_refused, projection_mismatches
from .amendment_issue_publisher import PlannedPublication, apply_publications, permitted_repositories, plan_publications, verify_adopted_story_issues
from .compile import address_of, is_legacy_acceptance
from .execution_policy import ExecutionPolicy, PolicySummary, summarize_policy
from .models import ClaimState, NodeKind, OrchestrationNode, OrchestrationWorkClaim
from .preview import ConclusionAuthority, derive_nodes, derive_waves, wave_label
from .proposal import LoopProposal, ProposedNode, Violation, validate_proposal
from .repository import OrchestrationRepository
from .shared_amendment import ACTIVE_NODE_STATES, SharedAppendError, history, quiescence_failures
from .state import ActorKind, NodeState, transition
from .work_claims import OwnerKind

__all__ = [
    "AmendmentImpact",
    "EdgeChange",
    "EffectiveLimits",
    "ExecutionStage",
    "NodeDisposition",
    "NodeImpact",
    "WaveMembershipChange",
    "assess_amendment",
    "new_work_issue_violations",
]


@dataclass(frozen=True)
class NodeDisposition:
    """What happens to one node, and what state it is in now.

    `state` is `None` exactly for an added node: it has no row, so it has no state, and
    reporting `pending` would claim a row exists. The distinction matters because
    `pending` is also a real state an existing node can be in.
    """

    address: str
    #: `added`, `removed`, `modified` or `unchanged`.
    change: str
    kind: str
    title: str
    #: The node's current `NodeState`, or None when the node does not exist yet.
    state: str | None
    #: Attempts already made. Non-zero on a removed node means work was spent.
    attempts: int = 0
    #: Who may conclude this node under the proposed plan. Absent for a removed node —
    #: it will not be concluded at all.
    concluded_by: str | None = None
    issue_ref: str | None = None
    proposal_identity: str | None = None
    repository_identity: str | None = None
    provider_repository_id: int | None = None
    execution_owner: str | None = None
    execution_adapter: str | None = None
    criteria: list[str] = field(default_factory=list)
    required_inputs: list[dict] = field(default_factory=list)
    evidence_expectations: list[str] = field(default_factory=list)
    before: dict | None = None
    after: dict | None = None


@dataclass(frozen=True)
class EdgeChange:
    """One dependency edge added or removed, in graph addresses.

    Addresses rather than node ids: an added edge's endpoints may not have ids yet, and
    a reviewer cannot read a uuid anyway.
    """

    from_address: str
    to_address: str
    change: str


@dataclass(frozen=True)
class WaveMembershipChange:
    """A wave whose membership the amendment changes.

    Reported separately from the node list because a wave is derived, not stored, so
    "Wave 2 now contains the cleanup work" appears nowhere in the node diff — it is a
    consequence of the addresses chosen. A reviewer who tracks waves rather than
    addresses would otherwise not see it at all.

    `wave` is the `epic/wave` label, matching `preview.wave_label`: a bare wave ref is
    not unique across epics.
    """

    wave: str
    added_addresses: list[str] = field(default_factory=list)
    removed_addresses: list[str] = field(default_factory=list)
    #: True when no node in force belonged to this wave — the amendment creates it.
    is_new: bool = False
    #: True when the amendment leaves this wave with no members at all.
    is_emptied: bool = False


@dataclass(frozen=True)
class ExecutionStage:
    """One rung of the proposed execution order: the waves that may run concurrently.

    `stage` is None for waves whose relative order the document does not determine
    (`preview.derive_waves` returns that for mutually dependent waves). Carried through
    rather than smoothed over, because printing a number there would assert an order the
    engine will not honour.
    """

    stage: int | None
    waves: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class NodeImpact:
    """Downstream consequence for an existing node that keeps its address.

    Only nodes whose *prerequisites* move appear here. A node whose prerequisites are
    unchanged is unaffected by the amendment no matter how much of the graph around it
    moved — the engine releases it on exactly the same condition as before.
    """

    address: str
    state: str
    added_prerequisites: list[str] = field(default_factory=list)
    removed_prerequisites: list[str] = field(default_factory=list)

    @property
    def newly_blocked(self) -> bool:
        """Whether this node gains a prerequisite, i.e. may now wait longer.

        The direction a reviewer needs flagged: removing a prerequisite releases work
        *earlier*, which is visible as a schedule change, while adding one can park a
        node indefinitely behind work nobody has scheduled.
        """
        return bool(self.added_prerequisites)


@dataclass(frozen=True)
class EffectiveLimits:
    """The limits dispatch would actually enforce, not the ones a document states.

    Before acceptance these are the in-force policy with every verified increase
    receipt applied (`policy_admission.load_in_force_policy`). After acceptance,
    compatible receipt lineage preserves those bounds; incompatible authority
    changes use the new plan's own limits and report each dropped receipt.
    """

    max_spend_usd: str
    max_attempts_per_node: int
    max_concurrent_actions: int
    max_wall_clock_seconds: int
    expires_at: str


@dataclass(frozen=True)
class AmendmentImpact:
    """The whole before/after picture for one proposed amendment.

    One object rather than several returns so a route serializes a single shape and a
    reviewer's reported state cannot be a mixture of two assessments.
    """

    flow_id: str
    #: The plan version this assessment compared against, or None if none is in force.
    base_plan_version: int | None
    #: `plan_hash` of the plan in force, for the caller to bind an approval to.
    base_plan_hash: str | None
    product_before: str | None = None
    product_after: str | None = None
    nodes: list[NodeDisposition] = field(default_factory=list)
    edges: list[EdgeChange] = field(default_factory=list)
    before_nodes: list[dict] = field(default_factory=list)
    after_nodes: list[dict] = field(default_factory=list)
    before_edges: list[dict] = field(default_factory=list)
    after_edges: list[dict] = field(default_factory=list)
    wave_changes: list[WaveMembershipChange] = field(default_factory=list)
    execution_order: list[ExecutionStage] = field(default_factory=list)
    downstream: list[NodeImpact] = field(default_factory=list)
    #: Gate addresses the amendment adds / removes / leaves alone. Uses #4529's
    #: `gate_diff` shape rather than a second one, because the acceptance comment
    #: already reports gating that way and two shapes would read as two facts.
    gates_added: list[str] = field(default_factory=list)
    gates_removed: list[str] = field(default_factory=list)
    policy_before: PolicySummary | None = None
    policy_after: PolicySummary | None = None
    policy_changed: bool = False
    #: Effective limits now and after acceptance. A decrease is a blocker.
    effective_limits_before: EffectiveLimits | None = None
    effective_limits_after: EffectiveLimits | None = None
    receipts_preserved: list[dict] = field(default_factory=list)
    receipts_dropped: list[dict] = field(default_factory=list)
    #: Unbound new work that acceptance will publish (or has published) as issues, with
    #: the target repository and whether a NEW issue will be created. Shown before
    #: approval because creating public issues is itself an effect of accepting.
    issue_publications: list[PlannedPublication] = field(default_factory=list)
    #: Diagnostics that would make acceptance fail. Non-empty means do not accept.
    blocking: list[Violation] = field(default_factory=list)

    @property
    def acceptable(self) -> bool:
        """Whether any *predictable* refusal stands in the way.

        Deliberately not named `will_succeed`. The base plan can move between this
        assessment and the acceptance, and that race is caught by
        `accept_amendment`'s exact-hash comparison, not here.
        """
        return not self.blocking

    @property
    def is_noop(self) -> bool:
        """Whether the amendment changes no node, edge or wave membership.

        Worth stating outright: an amendment that changes only a title or a description
        is a legitimate thing to propose, and a reviewer should be told that is all it
        is rather than left to infer it from three empty lists.
        """
        return (
            self.product_before == self.product_after
            and not any(node.change != "unchanged" for node in self.nodes)
            and not self.edges
            and not self.downstream
            and not self.policy_changed
        )


async def assess_amendment(
    session: AsyncSession,
    *,
    org_id: str,
    flow_id: str,
    proposal: LoopProposal,
    actor_kind: ActorKind = ActorKind.HUMAN,
    actor_id: str | None = None,
    actor_username: str = "",
    require_published: bool = False,
) -> AmendmentImpact:
    """Assess what accepting `proposal` would do to `flow_id`'s graph. Writes nothing.

    Args:
        session: The caller's session. Read-only use: no savepoint, no lock, no flush.
            Deliberately unlocked — a preview is advisory, and taking the flow lock
            `amend_plan` takes would let repeated previewing stall real amendments.
        org_id: Server-resolved tenant. Applied as a filter before any read, so a
            cross-tenant `flow_id` is indistinguishable from a missing one.
        flow_id: The flow the amendment targets.
        proposal: The full replacement document, exactly as it would be accepted.
        actor_kind: Who would accept. Load-bearing, not cosmetic: the
            `passed -> superseded` edge is human-only, so the same document is
            acceptable for a human and refused for a service actor, and a preview that
            assumed `HUMAN` would under-report for the agent path.
        require_published: True inside transactional acceptance, after the route has
            published unbound new work. Any new node still lacking a recorded issue is
            then a blocker; in a preview it is reported as a planned publication.

    Returns:
        An `AmendmentImpact`. A flow with no accepted plan yet yields
        `base_plan_version=None` and every proposed node reported as `added` — the
        honest reading of amending a flow that has never been planned.

    Raises:
        FlowNotFoundError: No such flow in this tenant. Re-raised from the repository
            lookup rather than returned as a blocker, because a missing flow is not a
            problem with the proposal.
    """
    from .amend import FlowNotFoundError

    repo = OrchestrationRepository(session)
    flow = await repo.get_flow(org_id=org_id, flow_id=flow_id)
    if flow is None:
        raise FlowNotFoundError(f"no orchestration flow {flow_id!r} in this tenant")

    in_force = await repo.get_accepted_plan(org_id=org_id, flow_id=flow_id)
    existing_nodes = await repo.list_nodes(org_id=org_id, flow_id=flow_id)
    existing_edges = await repo.list_edges(org_id=org_id, flow_id=flow_id)

    by_address = {address_of(flow.slug, node): node for node in existing_nodes}
    node_by_id = {node.id: node for node in existing_nodes}
    # Edges in graph-address terms. An edge whose endpoint row has vanished is skipped
    # rather than rendered with a raw id: `ondelete="CASCADE"` makes that unreachable,
    # and a half-rendered edge in a review would be worse than an omitted one.
    before_edges = {
        (address_of(flow.slug, node_by_id[edge.from_node_id]), address_of(flow.slug, node_by_id[edge.to_node_id]))
        for edge in existing_edges
        if edge.from_node_id in node_by_id and edge.to_node_id in node_by_id
    }
    after_edges = {(edge.from_address, edge.to_address) for edge in proposal.edges}

    derived = derive_nodes(proposal)
    proposed_by_address = {node.address: node for node in proposal.nodes}
    base_nodes = {
        node.get("address"): node
        for node in ((in_force.plan_document or {}).get("nodes", []) if in_force is not None else [])
        if isinstance(node, dict) and isinstance(node.get("address"), str)
    }
    policy_before = _policy_summary((in_force.plan_document if in_force is not None else {}).get("execution_policy"))
    policy_after = _policy_summary(proposal.execution_policy)

    publications, publication_blockers = await plan_publications(
        session,
        org_id=org_id,
        proposal=proposal,
        existing_addresses=set(by_address),
        permitted=permitted_repositories(in_force.plan_document if in_force is not None else None, proposal),
        lock=require_published,
    )
    if require_published:
        publication_blockers.extend(
            Violation(
                rule="missing_issue_binding",
                message=(
                    f"New work publication in {plan.repository!r} is not synchronized with this revision "
                    f"(action: {plan.action}). Acceptance requires completed publication of the exact reviewed "
                    "content; retry the acceptance after publication completes."
                ),
                where=plan.address,
            )
            for plan in publications
            if plan.action != "reuse"
        )

    started, product_started = await binding_reservations(
        session, flow=flow, nodes=existing_nodes, document=in_force.plan_document if in_force is not None else {}
    )
    blocking = _blocking(
        proposal=proposal,
        flow_slug=flow.slug,
        existing=by_address,
        base_nodes=base_nodes,
        before=before_edges,
        after=after_edges,
        actor_kind=actor_kind,
        publication_blockers=publication_blockers,
        reserved_ids=started,
    )
    blocking.extend(
        await verify_adopted_story_issues(
            session,
            org_id=org_id,
            proposal=proposal,
            base_document=in_force.plan_document if in_force is not None else None,
        )
    )
    if in_force is not None:
        mismatches = projection_mismatches(
            flow,
            in_force.plan_document or {},
            existing_nodes,
            allow_missing_legacy_repositories=await is_legacy_acceptance(session, in_force),
        )
        if mismatches:
            blocking.append(
                Violation(
                    rule="accepted_identity_projection_mismatch",
                    message=f"accepted identity projection mismatch: {mismatches}; refresh before amending",
                    where=flow_id,
                )
            )
    if proposal.product_id != flow.product_id and product_started:
        blocking.append(
            Violation(
                rule="product_retarget_started",
                message="cannot retarget Product after execution reservation; use a new flow",
                where=flow_id,
            )
        )
    if proposal.product_id is None and not await is_legacy_acceptance(session, in_force):
        blocking.append(
            Violation(
                rule="product_required",
                message="new execution acceptance requires an accessible Product",
                where=flow_id,
            )
        )
    if proposal.product_id is not None and actor_id is not None:
        from .compile import ApprovalContext, ProposalRejectedError, require_accessible_product

        try:
            await require_accessible_product(
                session,
                product_id=proposal.product_id,
                decision=ApprovalContext(org_id=org_id, actor_id=actor_id, actor_role="org_admin", actor_kind=actor_kind, username=actor_username),
            )
        except ProposalRejectedError as error:
            blocking.append(Violation(rule="product_inaccessible", message=str(error), where=flow_id))
    from .amend import AmendmentContext, _receipt_dispositions
    from .compile import DecisionKind, ExpiredExecutionPolicyError, prepare_execution_policy, require_unexpired_execution_policy

    preserved, dropped = [], []
    effective_policy = proposal.execution_policy
    if actor_id and actor_kind == ActorKind.HUMAN:
        candidate = prepare_execution_policy(
            proposal,
            decision=AmendmentContext(org_id=org_id, actor_id=actor_id, actor_role="org_admin").to_approval(),
            decision_kind=DecisionKind.PLAN_AMENDED,
        )
        preserved, dropped, effective_policy = await _receipt_dispositions(session, in_force, candidate.model_dump(mode="json"))
    effective_proposal = proposal.model_copy(update={"execution_policy": effective_policy})
    limits_before, limits_after = await _effective_limits(session, org_id=org_id, flow_id=flow_id, proposal=effective_proposal)
    try:
        require_unexpired_execution_policy(effective_proposal)
    except ExpiredExecutionPolicyError as error:
        blocking.append(Violation(rule="expired_execution_policy", message=str(error), where="execution_policy.expires_at"))
    if not dropped:
        blocking.extend(_limit_decreases(limits_before, limits_after))
    blocking.extend(await _in_flight_blockers(session, flow=flow))
    blocking.extend(
        await assignment_conflict_violations(
            session,
            org_id=org_id,
            flow_id=flow_id,
            # Recorded issues are bound first, so a reused issue held elsewhere is caught.
            proposal=apply_publications(proposal, publications),
            existing_addresses=set(by_address),
        )
    )

    dispositions = _dispositions(flow_slug=flow.slug, existing=by_address, base=base_nodes, proposed=proposed_by_address, derived=derived)
    return AmendmentImpact(
        flow_id=flow_id,
        base_plan_version=in_force.version if in_force is not None else None,
        base_plan_hash=in_force.plan_hash if in_force is not None else None,
        product_before=flow.product_id,
        product_after=proposal.product_id,
        nodes=dispositions,
        edges=_edge_changes(before=before_edges, after=after_edges),
        before_nodes=[dict(item.before) for item in sorted(dispositions, key=lambda item: item.address) if item.before is not None],
        after_nodes=[node.model_dump(mode="json") for node in sorted(proposal.nodes, key=lambda item: item.address)],
        before_edges=[{"from_address": source, "to_address": target} for source, target in sorted(before_edges)],
        after_edges=[{"from_address": source, "to_address": target} for source, target in sorted(after_edges)],
        wave_changes=_wave_changes(before=set(by_address), after=set(proposed_by_address)),
        execution_order=_execution_order(proposal),
        downstream=_downstream(existing=by_address, before=before_edges, after=after_edges),
        gates_added=sorted(_gates(proposed_by_address.values()) - _gate_addresses_of_rows(flow.slug, existing_nodes)),
        gates_removed=sorted(_gate_addresses_of_rows(flow.slug, existing_nodes) - _gates(proposed_by_address.values())),
        policy_before=policy_before,
        policy_after=policy_after,
        policy_changed=_policy_dump(policy_before) != _policy_dump(policy_after),
        effective_limits_before=limits_before,
        effective_limits_after=limits_after,
        receipts_preserved=preserved,
        receipts_dropped=dropped,
        issue_publications=publications,
        blocking=blocking,
    )


async def assignment_conflict_violations(
    session: AsyncSession,
    *,
    org_id: str,
    flow_id: str,
    proposal: LoopProposal,
    existing_addresses: set[str],
) -> list[Violation]:
    candidates = []
    for node in proposal.nodes:
        issue = str(node.issue_ref or "").lstrip("#")
        repository_id = node.provider_repository_id
        if repository_id is None and str(node.repository_identity or "").isdigit():
            repository_id = int(node.repository_identity)
        if node.address in existing_addresses or not issue.isdigit() or repository_id is None:
            continue
        candidates.append((node, repository_id, int(issue)))
    if not candidates:
        return []

    claims = (
        await session.execute(
            select(OrchestrationWorkClaim).where(
                OrchestrationWorkClaim.org_id == org_id,
                OrchestrationWorkClaim.state == ClaimState.HELD.value,
            )
        )
    ).scalars()
    by_binding = {(claim.provider_repository_id, claim.issue_number): claim for claim in claims}
    violations = []
    for node, repository_id, issue_number in candidates:
        claim = by_binding.get((repository_id, issue_number))
        if claim is None or (claim.owner_kind == OwnerKind.ENGINE_FLOW.value and claim.owner_ref == flow_id):
            continue
        violations.append(
            Violation(
                rule="active_assignment_conflict",
                message=(
                    f"Issue {issue_number} in repository {repository_id} is already held by "
                    f"{claim.owner_kind} {claim.owner_ref!r}; release or explicitly hand over that claim before acceptance."
                ),
                where=node.address,
            )
        )
    return violations


def _limits_of(policy: ExecutionPolicy | None) -> EffectiveLimits | None:
    if policy is None:
        return None
    expires = policy.expires_at if policy.expires_at.tzinfo is not None else policy.expires_at.replace(tzinfo=UTC)
    return EffectiveLimits(
        max_spend_usd=str(policy.limits.max_spend_usd),
        max_attempts_per_node=policy.limits.max_attempts_per_node,
        max_concurrent_actions=policy.limits.max_concurrent_actions,
        max_wall_clock_seconds=policy.limits.max_wall_clock_seconds,
        expires_at=expires.astimezone(UTC).isoformat(),
    )


async def _effective_limits(
    session: AsyncSession, *, org_id: str, flow_id: str, proposal: LoopProposal
) -> tuple[EffectiveLimits | None, EffectiveLimits | None]:
    """Effective limits before (receipts applied) and initial after (submitted policy).

    `before` is None when no policy is in force, or when the in-force policy cannot be
    verified (admission refuses that flow anyway, so there is nothing to preserve).
    """
    from .policy_admission import load_in_force_policy

    admission = await load_in_force_policy(session, org_id=org_id, flow_id=flow_id)
    return _limits_of(admission.policy), _limits_of(proposal.execution_policy)


#: (field, human label, parse) for every limit an amendment must not silently lower.
_LIMIT_FIELDS = (
    ("max_spend_usd", "flow spend limit (USD)", Decimal),
    ("max_attempts_per_node", "attempts per node", int),
    ("max_concurrent_actions", "concurrent actions", int),
    ("max_wall_clock_seconds", "wall-clock seconds", int),
    ("expires_at", "policy expiry", datetime.fromisoformat),
)


def _limit_decreases(before: EffectiveLimits | None, after: EffectiveLimits | None) -> list[Violation]:
    """Every effective limit acceptance would lower.

    Only compared when both sides carry a policy. Removing the policy entirely is not a
    silent decrease: admission then refuses all dispatch (`STALE_POLICY_VERSION`), and
    `policy_after` shows the removal.
    """
    if before is None or after is None:
        return []
    violations = []
    for name, label, parse in _LIMIT_FIELDS:
        was, now = getattr(before, name), getattr(after, name)
        if parse(now) < parse(was):
            violations.append(
                Violation(
                    rule="effective_limit_decrease",
                    message=(
                        f"Accepting would lower the effective {label} from {was} to {now}. State the limits explicitly "
                        f"in the proposal's execution_policy (at least {was}) to keep them, or lower them deliberately in a "
                        "separate reviewed change."
                    ),
                    where=f"execution_policy.{'limits.' if name != 'expires_at' else ''}{name}",
                )
            )
    return violations


_IN_FLIGHT_MESSAGES = {
    "active_nodes_cross_plan_boundary": "Node(s) {nodes} are running or awaiting merge.",
    "unfinished_execution": "An execution on this flow has not concluded, or has an action pending.",
    "unsettled_action": "An admitted action on this flow has not settled.",
    "unfinished_or_unverified_worker_report": "A worker run on this flow has no verified terminal report.",
    "unsettled_work_claim": "A work claim on this flow is held by a run that has not settled.",
    "amendment_history_limit": "This flow's execution history exceeds the bounded check, so quiescence cannot be verified.",
}


async def _in_flight_blockers(session: AsyncSession, *, flow) -> list[Violation]:
    """In-flight work an ordinary plan change would strand.

    A new plan version invalidates execution authority bound to the current one: the
    execution runner refuses it (`AUTHORITY_UNVERIFIABLE`) and the merge controller
    refuses its delivery contract. The predicates are `shared_amendment`'s quiescence
    checks, imported rather than restated. Queued or dispatched amendment authoring is
    not in-flight execution and is excluded: accepting supersedes it.
    """
    try:
        found = await history(session, flow=flow)
        failures = [code for code in quiescence_failures(found) if code != "unfinished_amendment_authoring"]
    except SharedAppendError as error:
        found, failures = None, [error.code]
    active = sorted(address_of(flow.slug, node) for node in (found or {}).get("nodes", []) if node.state in ACTIVE_NODE_STATES)
    return [
        Violation(
            rule=code,
            message=(
                _IN_FLIGHT_MESSAGES.get(code, f"In-flight work is unsettled ({code}).").format(nodes=", ".join(active))
                + " Accepting would invalidate its execution authority and strand it. Wait for it to settle, or stop it, then accept."
            ),
            where=active[0] if code == "active_nodes_cross_plan_boundary" and len(active) == 1 else None,
        )
        for code in failures
    ]


def _policy_summary(value: object) -> PolicySummary | None:
    if value is None:
        return None
    policy = value if isinstance(value, ExecutionPolicy) else ExecutionPolicy.model_validate(value)
    return summarize_policy(policy)


def _policy_dump(value: PolicySummary | None) -> dict | None:
    return value.model_dump(mode="json") if value is not None else None


def _gates(nodes) -> set[str]:
    """Gate addresses among proposed nodes."""
    return {node.address for node in nodes if node.kind == NodeKind.GATE.value}


def _gate_addresses_of_rows(flow_slug: str, nodes: list[OrchestrationNode]) -> set[str]:
    """Gate addresses among nodes on the graph, excluding already-superseded ones.

    Superseded nodes are excluded because they are not in force: counting one as a
    "removed gate" would report the loss of a human decision point that was already
    gone, and a false gate-removal warning is the kind that gets warnings ignored.
    """
    return {address_of(flow_slug, node) for node in nodes if node.kind == NodeKind.GATE.value and NodeState(node.state) is not NodeState.SUPERSEDED}


def _before_definition(address: str, current: OrchestrationNode, stored: dict | None) -> dict:
    raw = dict(stored or {})
    raw.update(address=address, kind=current.kind, title=current.title, issue_ref=current.issue_ref)
    try:
        return ProposedNode.model_validate(raw).model_dump(mode="json")
    except ValueError:
        return raw


def _dispositions(
    *,
    flow_slug: str,
    existing: dict[str, OrchestrationNode],
    base: dict[str, dict],
    proposed: dict,
    derived: dict,
) -> list[NodeDisposition]:
    """Per-node change, current state and conclusion authority, sorted by address.

    Sorted so two assessments of the same pair of inputs read identically; a dict
    iteration order would make a rendered review unstable between refreshes and
    invite a reviewer to diff the diff.

    An already-`SUPERSEDED` existing node absent from the proposal is **not** reported
    as removed. It was already not in force, `_supersede_absent_nodes` skips it, and
    listing it would pad the review with retired work.
    """
    dispositions: list[NodeDisposition] = []

    for address, node in proposed.items():
        current = existing.get(address)
        derived_node = derived.get(address)
        after = node.model_dump(mode="json")
        before = _before_definition(address, current, base.get(address)) if current is not None else None
        change = "added" if current is None else ("unchanged" if before == after else "modified")
        dispositions.append(
            NodeDisposition(
                address=address,
                change=change,
                kind=node.kind,
                title=node.title,
                state=current.state if current is not None else None,
                attempts=current.attempts if current is not None else 0,
                concluded_by=(derived_node.concluded_by.value if derived_node is not None else ConclusionAuthority.UNDETERMINED.value),
                issue_ref=node.issue_ref,
                proposal_identity=node.proposal_identity,
                repository_identity=node.repository_identity,
                provider_repository_id=node.provider_repository_id,
                execution_owner=node.execution_owner,
                execution_adapter=node.execution_adapter,
                criteria=list(node.criteria),
                required_inputs=[entry.model_dump(mode="json") for entry in node.required_inputs],
                evidence_expectations=list(node.evidence_expectations),
                before=dict(before) if before is not None else None,
                after=after,
            )
        )

    for address, node in existing.items():
        if address in proposed or NodeState(node.state) is NodeState.SUPERSEDED:
            continue
        dispositions.append(
            NodeDisposition(
                address=address,
                change="removed",
                kind=node.kind,
                title=node.title,
                state=node.state,
                attempts=node.attempts,
                before=_before_definition(address, node, base.get(address)),
            )
        )

    return sorted(dispositions, key=lambda item: (item.change, item.address))


def _edge_changes(*, before: set[tuple[str, str]], after: set[tuple[str, str]]) -> list[EdgeChange]:
    """Added and removed edges, sorted. Unchanged edges are omitted.

    Omitted rather than listed as `unchanged`: an amendment restates the whole graph, so
    almost every edge is unchanged, and burying two real changes in forty restatements
    is how a review becomes a formality.
    """
    return sorted(
        [EdgeChange(from_address=source, to_address=target, change="added") for source, target in after - before]
        + [EdgeChange(from_address=source, to_address=target, change="removed") for source, target in before - after],
        key=lambda edge: (edge.change, edge.from_address, edge.to_address),
    )


def _wave_of_address(address: str) -> str | None:
    """The `epic/wave` label of a four-segment address, or None if malformed.

    Malformed addresses are dropped rather than raised on: `validate_proposal` reports
    them precisely as blockers on this same assessment, and raising here would replace
    an author's list of actionable violations with a 500.
    """
    parts = address.split("/")
    return wave_label(parts[1], parts[2]) if len(parts) == 4 and all(parts) else None


def _wave_changes(*, before: set[str], after: set[str]) -> list[WaveMembershipChange]:
    """Which waves gain or lose members, including waves created or emptied.

    Wave membership is derived from the address and stored nowhere, which is exactly why
    it needs reporting: the node diff shows `superplane/epic-1/wave-3/cleanup` being
    added, and nothing in it says "Wave 3 did not exist until now".
    """
    members_before: dict[str, set[str]] = {}
    members_after: dict[str, set[str]] = {}

    for addresses, target in ((before, members_before), (after, members_after)):
        for address in addresses:
            wave = _wave_of_address(address)
            if wave is not None:
                target.setdefault(wave, set()).add(address)

    changes: list[WaveMembershipChange] = []
    for wave in sorted(set(members_before) | set(members_after)):
        was = members_before.get(wave, set())
        now = members_after.get(wave, set())
        if was == now:
            continue
        changes.append(
            WaveMembershipChange(
                wave=wave,
                added_addresses=sorted(now - was),
                removed_addresses=sorted(was - now),
                is_new=not was,
                is_emptied=not now,
            )
        )
    return changes


def _execution_order(proposal: LoopProposal) -> list[ExecutionStage]:
    """The proposed order as stages of concurrently-runnable waves.

    Grouped by stage rather than returned as a flat wave list because the question is
    "what runs at the same time as what", and a flat list answers "in what order were
    these printed". Staging itself is `preview.derive_waves`', imported rather than
    re-derived: a second opinion about execution order would let the amendment preview
    disagree with the draft preview about the same graph.
    """
    stages: dict[int | None, list[str]] = {}
    for wave in derive_waves(proposal):
        stages.setdefault(wave.stage, []).append(wave_label(wave.epic_ref, wave.wave_ref))

    # `None` last: its order is undetermined, so it cannot be placed among the numbered
    # stages without asserting something the document does not say.
    ordered = sorted(stage for stage in stages if stage is not None)
    return [ExecutionStage(stage=stage, waves=stages[stage]) for stage in ordered] + (
        [ExecutionStage(stage=None, waves=stages[None])] if None in stages else []
    )


def _prerequisites(edges: set[tuple[str, str]], address: str) -> set[str]:
    """The addresses `address` directly waits for, in one edge set."""
    return {source for source, target in edges if target == address}


def _downstream(
    *,
    existing: dict[str, OrchestrationNode],
    before: set[tuple[str, str]],
    after: set[tuple[str, str]],
) -> list[NodeImpact]:
    """Existing nodes whose prerequisites move, with the state each is in now.

    Restricted to nodes that already exist: an added node has no "before" to compare
    against, and its prerequisites are reported as added edges instead. Restricted to
    *prerequisite* changes because that is the only change that alters when the engine
    releases a node — gaining a successor delays other work, not this node.
    """
    impacts: list[NodeImpact] = []
    for address, node in sorted(existing.items()):
        was = _prerequisites(before, address)
        now = _prerequisites(after, address)
        if was == now:
            continue
        impacts.append(
            NodeImpact(
                address=address,
                state=node.state,
                added_prerequisites=sorted(now - was),
                removed_prerequisites=sorted(was - now),
            )
        )
    return impacts


def _blocking(
    *,
    proposal: LoopProposal,
    flow_slug: str,
    existing: dict[str, OrchestrationNode],
    base_nodes: dict[str, dict],
    before: set[tuple[str, str]],
    after: set[tuple[str, str]],
    actor_kind: ActorKind,
    publication_blockers: list[Violation],
    reserved_ids: set[str] = frozenset(),
) -> list[Violation]:
    """Every diagnostic that would make `amend_plan` refuse this document.

    Ordered deliberately: document validity first, then per-node refusals. A document
    that fails `validate_proposal` has, by definition, something wrong with its own
    structure, and reporting "node X cannot change prerequisites" about a graph with a
    cycle in it points the author at a symptom.

    The per-node checks still run on an invalid document rather than being gated behind
    the validator. `amend_plan` stops at the first problem and the author fixes one
    thing per round trip; the whole reason to preview is to be handed the list.
    """
    # Concluded evaluations keep the verifier digest they ran under; the acceptance
    # path skips that pin for them, and the preview must promise the same answer.
    from .amend import SETTLED_EVALUATION_STATES

    settled = frozenset(address for address, node in existing.items() if node.kind == "eval" and node.state in SETTLED_EVALUATION_STATES)
    blocking = [
        Violation(rule=violation.rule, message=violation.message, where=violation.where)
        for violation in validate_proposal(proposal, settled_evaluations=settled)
    ]
    # Unbound new work is no longer a blocker by itself: acceptance publishes it. What
    # blocks is work that cannot be published (no identity/repository), a repository
    # outside the policy, or more new issues than one request may create.
    blocking.extend(publication_blockers)
    proposed_by_address = {node.address: node for node in proposal.nodes}

    for address, node in sorted(existing.items()):
        proposed = proposed_by_address.get(address)

        if proposed is None:
            blocking.extend(_supersede_blockers(address=address, node=node, actor_kind=actor_kind))
            continue

        refusal = definition_change_refusal(
            address=address,
            state=node.state,
            attempts=node.attempts,
            current_kind=node.kind,
            current_issue_ref=node.issue_ref,
            proposed_kind=proposed.kind,
            proposed_issue_ref=proposed.issue_ref,
            current_document=base_nodes.get(address),
            proposed_document=proposed.model_dump(mode="json"),
            reserved=node.id in reserved_ids,
        )
        if refusal is not None:
            blocking.append(Violation(rule="amendment_definition_change", message=refusal, where=address))

        if prerequisites_change_refused(state=node.state, before=_prerequisites(before, address), after=_prerequisites(after, address)):
            blocking.append(
                Violation(
                    rule="amendment_started_node_prerequisites",
                    message=(
                        f"cannot change prerequisites of started node {address!r} (state {node.state}); use a new node address. "
                        f"Now waits for: {sorted(_prerequisites(before, address)) or 'nothing'}; "
                        f"would wait for: {sorted(_prerequisites(after, address)) or 'nothing'}"
                    ),
                    where=address,
                )
            )

    return blocking


def new_work_issue_violations(*, existing_addresses: set[str], proposal: LoopProposal) -> list[Violation]:
    return [
        Violation(
            rule="missing_issue_binding",
            message=(
                "New development and validation work must adopt a published issue before acceptance; "
                "otherwise the normal scheduler has no durable dispatch identity."
            ),
            where=node.address,
        )
        for node in proposal.nodes
        if node.address not in existing_addresses and node.kind in {NodeKind.STORY.value, NodeKind.EVAL.value} and not node.issue_ref
    ]


def _supersede_blockers(*, address: str, node: OrchestrationNode, actor_kind: ActorKind) -> list[Violation]:
    """Whether superseding this absent node is legal, asked of the state machine itself.

    `transition` is the authority, and it is consulted rather than approximated because
    the answer is not derivable from the diff: `passed -> superseded` is **human-only**,
    so an amendment that discards accepted work is refused for a service actor and
    permitted for a human. A preview hard-coding either answer would be wrong for the
    other path.

    An already-superseded node is skipped for the same reason `_supersede_absent_nodes`
    skips it: `SUPERSEDED` has no outgoing transition, so asking would manufacture a
    refusal for a node the amendment does not actually touch.
    """
    current = NodeState(node.state)
    if current is NodeState.SUPERSEDED:
        return []

    result = transition(current, NodeState.SUPERSEDED, actor_kind=actor_kind, reason="amendment preview")
    if result.allowed:
        return []
    return [
        Violation(
            rule="amendment_cannot_supersede",
            message=f"amendment cannot supersede node {address!r}: {result.rejection_reason}",
            where=address,
        )
    ]
