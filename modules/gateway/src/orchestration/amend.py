"""Plan amendment: supersede an accepted plan with a new version, attributably.

Issue #4200 (EPIC #4191, intent #4120). Plans meet reality — a wave needs
splitting, unplanned work arrives, a story turns out to be two. Without a
first-class amendment operation the accepted plan and reality diverge and the plan
quietly stops being the source of truth. This module makes amendment cheap,
validated the same way the original was, attributed, and preserved as history.

**This is a second write path into promotion state** — the exact state agents must
never reach. That is why it does not simply reuse the compile path's assumptions:

- **Authorization is the same permission as gate approval**, never a weaker one.
  `Permission.PLAN_APPROVE` gates both. A softer door into the same room is the
  same hole, and the EPIC's central guarantee is that promotion state cannot be
  reached without an explicit approval authority. Enforced at the route
  (`routes.py`), which is also where the org is server-resolved.

- **Superseding is a normal event, not an error.** The prior plan version stays
  queryable forever: amendment inserts version N+1 and marks N superseded, and
  nothing ever rewrites a plan row in place. "What was approved at that gate"
  therefore stays answerable after any number of amendments — an accepted plan
  that can be edited proves nothing about what was accepted.

**Why validation is delegated, not reimplemented.** The amendment is a full
`LoopProposal` document, not a patch, and it is re-validated by the same
`validate_proposal` and compiled by the same in-transaction `compile_proposal`.
An amendment therefore *cannot* be less validated than an original (AC-29). A
patch format would have needed its own validator, and two validators drift — the
amended path would eventually accept a plan the original path would have refused,
which is the softer-door failure in a different disguise.

**What amendment adds on top of compile.** `compile_proposal` is deliberately
additive: it upserts the proposal's nodes and never touches a node the proposal
omits, because for a first compile there is nothing to omit. Amendment is where
absence becomes meaningful — a node dropped from the new plan must stop being
worked. This module supersedes dropped nodes, reconciles retained definitions and
replaces the live dependency topology. Prior plan documents retain the history.

**Preservation is the point (semantics 4).** A node whose address *and* definition
are unchanged keeps its state, its attempt count and its run history. Re-planning
a wave must not throw away work already accepted in it, so an already-`passed`
node is never reset by an amendment that does not mention it. `compile_proposal`'s
address-keyed upsert gives this for free — it reuses the existing row rather than
replacing it.

**Atomicity.** Everything happens in one `begin_nested()` savepoint. A half-
amended graph — nodes from two plan versions with no coherent state — is one of
the four blast radii the issue names. This module never commits: the caller owns
the transaction, same convention as `compile.py` and `repository.py`.

**No migration.** `orchestration_accepted_plans` is already versioned with a
`superseded_at` discriminator and `orchestration_decisions` is already
append-only, both from the store story's `029_orchestration_graph.py`.
`DecisionKind.PLAN_AMENDED` already exists in the vocabulary.
"""

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .compile import (
    ApprovalContext,
    ProposalRejectedError,
    TenantMismatchError,
    accept_execution_policy,
    address_of,
    plan_hash,
    require_evaluation_acceptor,
    upsert_edges,
    upsert_nodes,
)
from .models import DecisionKind, OrchestrationFlow, OrchestrationNode
from .proposal import LoopProposal, validate_proposal
from .repository import OrchestrationRepository
from .state import ActorKind, NodeState, transition

__all__ = [
    "AmendResult",
    "AmendmentContext",
    "FlowNotFoundError",
    "amend_plan",
]


class FlowNotFoundError(LookupError):
    """The flow does not exist **within the caller's tenant**.

    Raised identically whether the flow is absent altogether or belongs to
    another org, and the route maps it to 404 — never 403. A 403 would confirm
    that some other tenant owns that `flow_id`, which is existence disclosure: a
    caller could enumerate `flow_id`s and learn which ones are real by reading the
    status code. 404 for both cases leaks nothing.
    """


@dataclass(frozen=True)
class AmendmentContext:
    """Who is amending, and for which tenant. **Server-resolved.**

    Every field must come from the request's authenticated context. `org_id` in
    particular is the tenant whose flow is amended and the tenant the document's
    declared `org_id` is compared against — a document is never re-homed to the
    amender's tenant.

    Separate from `compile.ApprovalContext` despite the identical shape, because
    the two carry different `actor_role` semantics in the decision record and a
    future field on one should not silently appear on the other. `to_approval()`
    converts.
    """

    org_id: str
    actor_id: str
    # Snapshotted by the caller at amendment time. A copy, not a join: roles
    # change, and attribution must reflect the authority actually held when the
    # amendment was made, not whatever the actor holds when the row is later read.
    actor_role: str
    # Amending a plan is a human act. Defaulted rather than required for the same
    # reason as ApprovalContext: a service actor must say so explicitly.
    actor_kind: ActorKind = ActorKind.HUMAN
    # Why the plan is being amended, carried verbatim onto the decision record and
    # onto every supersede transition it causes.
    reason: str | None = None

    def to_approval(self) -> ApprovalContext:
        """The equivalent `ApprovalContext`, for the delegated compile path."""
        return ApprovalContext(
            org_id=self.org_id,
            actor_id=self.actor_id,
            actor_role=self.actor_role,
            actor_kind=self.actor_kind,
            reason=self.reason,
        )


@dataclass(frozen=True)
class AmendResult:
    """What the amendment produced. Returned only on success.

    `superseded_version` and `plan_version` are both reported because the decision
    record stores the pair, and a caller rendering "v1 → v2" needs both without a
    second query.

    `already_amended` is True when the call was a no-op because the identical
    document was already in force (R-NF2). A caller reporting "N nodes superseded"
    must distinguish that from a real amendment, or a retried submission will
    claim to have changed a graph it merely found.
    """

    flow_id: str
    plan_version: int
    superseded_version: int | None
    decision_id: str
    plan_hash: str
    node_ids: dict[str, str] = field(default_factory=dict)
    nodes_created: int = 0
    nodes_superseded: int = 0
    edges_created: int = 0
    already_amended: bool = False


async def amend_plan(
    session: AsyncSession,
    flow_id: str,
    proposal: LoopProposal,
    actor: AmendmentContext,
) -> AmendResult:
    """Supersede a flow's accepted plan with a new version, atomically.

    Args:
        session: The caller's session. **Not committed here** — the caller owns
            the transaction, so an amendment can land together with whatever else
            the request writes.
        flow_id: The flow to amend. Resolved with an `org_id` filter before
            anything is read.
        proposal: The full replacement plan document (not a patch).
        actor: Server-resolved amendment context. `actor.org_id` is authoritative.

    Returns:
        An `AmendResult`. `already_amended` is True when the identical document
        was already in force and nothing was written.

    Raises:
        FlowNotFoundError: No such flow in the caller's tenant. Maps to 404.
        ProposalRejectedError: The document failed authoritative validation. No
            rows written — an amendment cannot smuggle in a plan the original path
            would have refused (AC-29).
        TenantMismatchError: The document declares a different tenant than the
            amender's resolved org. No rows written.
    """
    repo = OrchestrationRepository(session)

    # --- Tenant isolation, before any read of plan state -------------------
    # The org filter is applied here rather than checked after loading, so a
    # cross-org flow_id is indistinguishable from a nonexistent one.
    flow = await repo.get_flow(org_id=actor.org_id, flow_id=flow_id)
    if flow is None:
        raise FlowNotFoundError(f"no orchestration flow {flow_id!r} in this tenant")

    # --- Validation parity (AC-29) -----------------------------------------
    # The same validator the original path runs, run before anything opens a
    # savepoint so a refused document touches no state. Delegating to
    # compile_proposal alone would not be enough: its rejection happens inside its
    # own savepoint, and we need the flow-level checks below to run against a
    # document already known to be well-formed.
    violations = validate_proposal(proposal)
    if violations:
        detail = "; ".join(str(violation) for violation in violations)
        raise ProposalRejectedError(
            f"amendment failed authoritative validation with {len(violations)} violation(s): {detail}",
            violations=violations,
        )

    # --- Tenant ownership of the document ----------------------------------
    # Compared, never substituted — a document authored for another tenant is
    # rejected rather than quietly re-homed onto this amender's flow.
    if proposal.org_id != actor.org_id:
        raise TenantMismatchError(
            f"amendment declares org_id {proposal.org_id!r} but the amender's resolved org is {actor.org_id!r}; "
            "an amendment is never re-homed to the amender's tenant"
        )

    # An amendment addressed at a different flow than the one being amended would
    # file its nodes under this flow while their addresses claim another. Rejected
    # rather than reconciled: the address is the node's identity.
    if proposal.flow_slug != flow.slug:
        raise ProposalRejectedError(
            f"amendment declares flow_slug {proposal.flow_slug!r} but flow {flow_id!r} is {flow.slug!r}; an amendment must target the flow it amends"
        )

    # --- Stamp the amended execution policy (#5128) ------------------------
    # An amendment producing a new accepted version IS how a policy is amended, so
    # a re-submitted policy is re-stamped here and the new version carries it.
    #
    # The same `accept_execution_policy` the original path uses, imported rather
    # than reimplemented, for the same reason this module already shares
    # `validate_proposal` and `upsert_nodes` (AC-29 parity): a second acceptance
    # rule here would be free to accept a policy `compile_proposal` refuses, and
    # the amendment path is the *easier* one to reach. In particular this is what
    # stops an amendment from being the way a SERVICE actor gets a policy accepted.
    #
    # Before the hash, so the stamp is inside what idempotency compares — see
    # `compile.plan_hash` on why the policy is hashed at all.
    proposal = accept_execution_policy(proposal, decision=actor.to_approval(), decision_kind=DecisionKind.PLAN_AMENDED)

    document = proposal.model_dump(mode="json")
    document_hash = plan_hash(proposal)

    # One savepoint for the whole amendment: the new plan row, the superseded
    # prior row, the superseded nodes, the new nodes and the decision land
    # together or not at all.
    async with session.begin_nested():
        # Serialize amendments before reading the current version, including flows
        # with no nodes yet. Dispatch/tick lock nodes in the same ID order below.
        await session.execute(
            select(OrchestrationFlow).where(OrchestrationFlow.org_id == actor.org_id, OrchestrationFlow.id == flow.id).with_for_update()
        )
        in_force = await repo.get_accepted_plan(org_id=actor.org_id, flow_id=flow.id)
        if in_force is not None and (in_force.plan_document or {}).get("execution_continuation"):
            raise ProposalRejectedError(
                "Shared worker continuations require the bounded append preview/accept path; "
                "a full proposal cannot discard their authority marker or invalidate active worker assignments."
            )

        # --- Idempotency (R-NF2) -------------------------------------------
        # An identical document already in force means this is a retry — a dropped
        # connection, a double-submit. Return what exists rather than writing a
        # second version that differs from the first only in its number.
        if in_force is not None and in_force.plan_hash == document_hash:
            existing = await repo.list_nodes(org_id=actor.org_id, flow_id=flow.id)
            return AmendResult(
                flow_id=flow.id,
                plan_version=in_force.version,
                superseded_version=None,
                decision_id=in_force.accepted_by_decision_id or "",
                plan_hash=document_hash,
                node_ids={address_of(flow.slug, node): node.id for node in existing},
                already_amended=True,
            )

        require_evaluation_acceptor(proposal, actor.to_approval(), in_force)
        superseded_version = in_force.version if in_force is not None else None

        # --- Supersede the dropped nodes -----------------------------------
        # Unlike additive compilation, absence in the replacement plan
        # is meaningful. Done BEFORE the upsert so the "already on the graph" set
        # is the pre-amendment one — running it after would see the newly inserted
        # nodes too and, since they are all in the new plan, do nothing wrong but
        # cost a pointless second pass.
        proposed_addresses = {node.address for node in proposal.nodes}
        existing_nodes = list(
            (
                await session.execute(
                    select(OrchestrationNode)
                    .where(OrchestrationNode.org_id == actor.org_id, OrchestrationNode.flow_id == flow.id)
                    .order_by(OrchestrationNode.id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .all()
        )
        _reconcile_definitions(existing_nodes, proposal)
        nodes_superseded = _supersede_absent_nodes(
            existing_nodes,
            proposed_addresses=proposed_addresses,
            flow_slug=flow.slug,
            actor=actor,
        )

        # --- Reuse the compile path for everything additive -----------------
        # Nodes with an unchanged address keep their row, and therefore their
        # state, attempts and run history (semantics 4). New nodes land `pending`
        # from the column default.
        node_ids, nodes_created = await upsert_nodes(
            repo,
            proposal=proposal,
            org_id=actor.org_id,
            flow_id=flow.id,
        )
        # Executable edges describe only the current plan. Keeping removed edges
        # would strand successors behind nodes that are now superseded.
        desired = {(node_ids[e.from_address], node_ids[e.to_address]) for e in proposal.edges}
        existing_edges = await repo.list_edges(org_id=actor.org_id, flow_id=flow.id)
        before = {(e.from_node_id, e.to_node_id) for e in existing_edges}
        for node in existing_nodes:
            if address_of(flow.slug, node) not in proposed_addresses:
                continue
            if NodeState(node.state) not in {NodeState.PENDING, NodeState.READY}:
                old_inputs = {source for source, target in before if target == node.id}
                new_inputs = {source for source, target in desired if target == node.id}
                if old_inputs != new_inputs:
                    raise ProposalRejectedError(
                        f"cannot change prerequisites of started node {address_of(flow.slug, node)!r}; use a new node address"
                    )
        for edge in existing_edges:
            if (edge.from_node_id, edge.to_node_id) not in desired:
                await session.delete(edge)
        await session.flush()
        flow.title = proposal.title
        edges_created = await upsert_edges(
            repo,
            proposal=proposal,
            org_id=actor.org_id,
            flow_id=flow.id,
            node_ids=node_ids,
        )

        # The decision is appended before the plan row so the plan can point at
        # it — same ordering constraint as the compile path. That ordering means
        # the decision's reason must name a version number that does not exist
        # yet, so it is derived from the same maximum `record_accepted_plan`
        # allocates from and cross-checked against the row actually written below.
        versions = [existing.version for existing in await repo.list_plan_versions(org_id=actor.org_id, flow_id=flow.id)]
        expected_version = (max(versions) + 1) if versions else 1

        record = await repo.append_decision(
            org_id=actor.org_id,
            flow_id=flow.id,
            kind=DecisionKind.PLAN_AMENDED.value,
            actor_id=actor.actor_id,
            actor_role=actor.actor_role,
            actor_kind=ActorKind(actor.actor_kind).value,
            reason=_amendment_reason(actor.reason, superseded_version, expected_version, nodes_superseded),
        )

        # `record_accepted_plan` allocates the next version and marks the prior
        # one superseded. Not reimplemented here: the store story owns that
        # sequencing, and its unique index on (flow_id, version) is what makes a
        # concurrent double-amend fail loudly instead of producing two "version 2"
        # plans.
        plan = await repo.record_accepted_plan(
            org_id=actor.org_id,
            flow_id=flow.id,
            plan_document=document,
            plan_hash=document_hash,
            accepted_by_decision_id=record.id,
        )

        # The decision row is append-only, so a version number that disagreed with
        # the plan actually written could never be corrected. Assert instead: this
        # can only fire if the store's allocation stops matching the maximum it
        # allocates from, and inside the savepoint a raise costs nothing.
        if plan.version != expected_version:
            raise ProposalRejectedError(
                f"plan version allocation disagreed with the attributed version (attributed v{expected_version}, wrote v{plan.version}); "
                "the amendment was rolled back rather than recorded with wrong attribution"
            )

        return AmendResult(
            flow_id=flow.id,
            plan_version=plan.version,
            superseded_version=superseded_version,
            decision_id=record.id,
            plan_hash=document_hash,
            node_ids=node_ids,
            nodes_created=nodes_created,
            nodes_superseded=nodes_superseded,
            edges_created=edges_created,
        )


def _reconcile_definitions(existing_nodes: list[OrchestrationNode], proposal: LoopProposal) -> None:
    """Update unstarted routing without changing execution identity or history."""
    proposed_by_address = {node.address: node for node in proposal.nodes}
    for node in existing_nodes:
        address = address_of(proposal.flow_slug, node)
        proposed = proposed_by_address.get(address)
        if proposed is None:
            continue
        if node.state == NodeState.SUPERSEDED.value:
            raise ProposalRejectedError(f"cannot reuse superseded node {address!r}; use a new node address")
        if node.kind != proposed.kind:
            raise ProposalRejectedError(f"cannot change kind of node {address!r}; use a new node address")
        if node.issue_ref != proposed.issue_ref:
            if node.attempts or NodeState(node.state) not in {NodeState.PENDING, NodeState.READY}:
                raise ProposalRejectedError(f"cannot change issue_ref of started node {address!r}; use a new node address")
            node.issue_ref = proposed.issue_ref
        node.title = proposed.title


def _supersede_absent_nodes(
    existing_nodes: list[OrchestrationNode],
    *,
    proposed_addresses: set[str],
    flow_slug: str,
    actor: AmendmentContext,
) -> int:
    """Transition nodes absent from the new plan to `superseded`. Returns the count.

    Every state change goes through `transition()` (R-N2a) — this function never
    assigns `node.state` on its own authority. That matters most for the
    `passed -> superseded` edge, which the vocabulary marks **human-only**: a
    service actor amending a plan cannot discard accepted work, and the guard, not
    this function, is what enforces it.

    A node already in a terminal-and-superseded state is skipped rather than
    re-transitioned: `SUPERSEDED` has no outgoing edges, so attempting it would be
    rejected, and a rejected transition is evidence of off-plan activity (R-N2b) —
    writing that evidence for the engine's own idempotent re-pass would be a false
    positive in the primary deviation detector.

    A rejection on any other node RAISES rather than being recorded. That is the
    opposite of the tick's behaviour and deliberate: mid-amendment we are inside
    the savepoint, and a node that cannot be superseded means the amendment cannot
    be applied coherently. Recording the rejection and continuing would produce
    exactly the "nodes from two plan versions" state the issue names as a blast
    radius. The raise unwinds the savepoint, so nothing lands.
    """
    superseded = 0

    for node in existing_nodes:
        if address_of(flow_slug, node) in proposed_addresses:
            continue

        current = NodeState(node.state)
        if current is NodeState.SUPERSEDED:
            continue

        result = transition(
            current,
            NodeState.SUPERSEDED,
            actor_kind=ActorKind(actor.actor_kind),
            reason=actor.reason or "superseded by plan amendment",
        )

        if not result.allowed:
            raise ProposalRejectedError(f"amendment cannot supersede node {address_of(flow_slug, node)!r}: {result.rejection_reason}")

        node.state = result.new_state.value
        superseded += 1

    return superseded


def _amendment_reason(
    reason: str | None,
    superseded_version: int | None,
    created_version: int,
    nodes_superseded: int,
) -> str:
    """Compose the decision record's reason, preserving the actor's own words.

    The version pair and supersede count are appended to the operator's stated
    reason rather than replacing it: the issue requires the superseded → created
    version numbers on the decision row, and `orchestration_decisions` has no
    column for them. Appending keeps the requirement satisfied without a migration
    this story is explicitly not supposed to need, and the actor's justification
    stays readable at the front of the field.

    `superseded_version` is None when the flow had no plan in force — amending a
    flow whose original compile never happened. Rendered as "none" rather than
    "v0", which would name a version that never existed.
    """
    stated = reason or "plan amended"
    from_version = "none" if superseded_version is None else f"v{superseded_version}"
    return f"{stated} [amendment: {from_version} -> v{created_version}, {nodes_superseded} node(s) superseded]"
