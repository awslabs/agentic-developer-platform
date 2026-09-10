"""Compile an approved loop proposal into orchestration-graph rows.

Issue #4199 (EPIC #4191, intent #4120). This module is the **only** code path that
creates orchestration nodes, and that exclusivity is the security property the
story is built on.

Agents never write these tables. They cannot: no DB credential exists in an agent
pod. An authoring agent submits a `LoopProposal` document, and this function
decides whether that document becomes state. Two checks stand between a document
and the graph, and neither is skippable from the outside:

1. **Re-validation.** `validate_proposal` runs again here, against the same rules
   the advisory CLI runs. The CLI is a convenience for the author — it runs in the
   author's environment and an author can simply not run it. This call is the
   control. AC-29 is the test that proves it: it calls `compile_proposal` directly
   with a document that fails validation, bypassing the CLI entirely, and asserts
   both that it raises and that zero rows land.

2. **Tenant ownership.** The org the plan lands in comes from the
   **server-resolved** approval context, never from the document. The document's
   declared `org_id` is *compared* against it, and a mismatch is **rejected**, not
   silently re-homed. Re-homing would be the worse failure: a plan authored for
   tenant A quietly becoming tenant B's state, with tenant B's operator's name on
   the decision record.

3. **Only an approval rewrites the plan of record.** A compile whose
   `decision_kind` is not in `genesis.APPROVAL_DECISION_KINDS` — #4528's
   `PLAN_DRAFTED`, and any non-approval kind added later — is refused if a plan is
   already in force for the flow. Enforced here rather than in the caller that
   needs it because a guard in a caller protects only that caller: review PR #4558
   reproduced both a plan-of-record rewrite *and* a dispatch of unapproved work
   through a direct call with a non-approval kind, since flow-scoped genesis
   (`_latest_approval_decision_id`) lets a pre-existing human approval root nodes
   appended afterwards.

**Atomicity.** All inserts happen inside a single `begin_nested()` savepoint, so a
failure part-way leaves nothing behind. Without it, a failure between the node
insert and the accepted-plan insert would leave nodes on the graph for a plan that
was never accepted — the graph would show work nobody approved, which is precisely
the trust property this EPIC exists to establish. A savepoint rather than a
`commit()` because **the caller owns the transaction**: gate approval records its
own decision alongside this compile, and the two must land together or not at all.
This module never commits. Same convention as `repository.py`.

**Idempotency (R-NF2).** Compiling the same proposal twice does not double-insert.
Nodes are matched by graph address and edges by endpoint pair, so a retried
approval — a dropped connection, a duplicate delivery — converges instead of
either duplicating the graph or dying on a unique-index violation.

**No migration.** Every table written here was created by the store story's
migration (`029_orchestration_graph.py`). `spec_revision` needs no column of its
own: `plan_document` stores the accepted document verbatim, and the document
carries its own spec revision. Adding a column for a field already inside the
stored document would create two copies of one value.
"""

import hashlib
import json
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from .genesis import APPROVAL_DECISION_KINDS
from .models import DecisionKind
from .proposal import LoopProposal, Violation, split_address, validate_proposal
from .repository import OrchestrationRepository
from .state import ActorKind, NodeState

__all__ = [
    "ApprovalContext",
    "CompileResult",
    "NonApprovalSupersedeError",
    "ProposalRejectedError",
    "TenantMismatchError",
    "address_of",
    "compile_proposal",
    "plan_hash",
    "upsert_edges",
    "upsert_nodes",
]


class ProposalRejectedError(RuntimeError):
    """Raised when a proposal is refused. Nothing has been written.

    Raises rather than returns, unlike `transition()` in `state.py`. The
    difference is deliberate and not an inconsistency: a rejected *transition* is
    evidence to persist (R-N2b, the primary detector for off-plan agent activity),
    so it must survive as a value. A rejected *proposal* has produced no state to
    reason about, and the caller's transaction must not proceed as though it had.
    An exception is the only outcome that cannot be accidentally ignored.
    """

    def __init__(self, message: str, violations: list[Violation] | None = None) -> None:
        super().__init__(message)
        self.violations = violations or []


class TenantMismatchError(ProposalRejectedError):
    """Raised when a document's declared `org_id` is not the approver's org.

    A subclass so a caller that only cares "was this refused?" catches
    `ProposalRejectedError`, while the tenant case stays separately catchable —
    it is a possible attack, not a typo, and a caller may want to alert on it
    rather than just report it back to the author.
    """


class NonApprovalSupersedeError(ProposalRejectedError):
    """Raised when a non-approval compile would supersede an in-force accepted plan.

    The plan of record is what answers "what did a human approve?", and only an
    approval may rewrite it. A compile whose `decision_kind` is not in
    `APPROVAL_DECISION_KINDS` — issue #4528's `PLAN_DRAFTED`, and any future
    non-approval kind — is refused rather than allowed to supersede.

    A subclass of `ProposalRejectedError` so a caller that only asks "was this
    refused?" catches it with everything else, while staying separately catchable:
    like `TenantMismatchError` this is a possible privilege escalation rather than
    an author's mistake, and deserves its own status code and alerting.
    """


@dataclass(frozen=True)
class ApprovalContext:
    """Who approved this plan, and for which tenant. **Server-resolved.**

    Every field here must come from the request's authenticated context, never
    from the proposal document. `org_id` in particular is the tenant the plan
    lands in — that is why the document's own `org_id` is only ever compared
    against this one, never used in its place.
    """

    org_id: str
    actor_id: str
    actor_role: str
    # Approving a plan is a human act. Defaulted rather than required because the
    # overwhelming majority of callers are the gate-approval route; a service
    # actor must say so explicitly.
    actor_kind: ActorKind = ActorKind.HUMAN
    # Free-text justification, carried verbatim onto the decision record.
    reason: str | None = None


@dataclass(frozen=True)
class CompileResult:
    """What the compile produced. Returned only on success.

    `already_compiled` is True when this call was a no-op because the identical
    document was already in force. Callers that report "N nodes created" need to
    distinguish that from a first compile, or a retried approval will claim to
    have created a graph it merely found.
    """

    flow_id: str
    plan_version: int
    decision_id: str
    plan_hash: str
    # Graph address -> node id, for callers that need to address what they made.
    node_ids: dict[str, str] = field(default_factory=dict)
    nodes_created: int = 0
    edges_created: int = 0
    already_compiled: bool = False


# Fields of `LoopProposal` that describe how the plan came to be rather than what
# it executes, and are therefore NOT part of its identity. See `plan_hash`.
HASH_EXCLUDED_FIELDS = frozenset({"description", "design_history"})


def plan_hash(proposal: LoopProposal) -> str:
    """Stable SHA-256 of a proposal document.

    Canonicalised with sorted keys and no incidental whitespace so that two
    semantically identical documents hash identically regardless of field order —
    otherwise idempotency would depend on JSON key ordering, and a re-serialised
    resubmission of the same plan would look like a different plan.

    Note this hashes the document as *authored*, including its declared `org_id`.
    That is intended: the hash answers "is this the same document?", and a
    document differing only in declared tenant is not the same document.

    **`description` and `design_history` are excluded (#4885), and that exclusion
    is load-bearing twice over.**

    Semantically: they are provenance *about* how the plan came to be, not the
    plan's executable content. Two documents differing only in their use-case
    sentence describe the same graph, the same waves and the same dependencies —
    they are the same plan, and the hash answers exactly that question.

    Operationally: `plan_hash` is what idempotency compares. Including the new
    fields would change the hash of every document that carries them, so a
    fail-soft retry spanning the #4885 deploy — the worker retries by
    construction — would no longer match its own in-force plan. It would fall
    through the idempotency return and be refused 409 as a plan-of-record
    rewrite, turning a dropped connection into a permanent failure. `EXCLUDED`
    is a frozenset rather than an inline literal so a field added to it here
    cannot be forgotten by a reader who only greps for the field name.
    """
    canonical = json.dumps(proposal.model_dump(mode="json", exclude=HASH_EXCLUDED_FIELDS), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def compile_proposal(
    session: AsyncSession,
    proposal: LoopProposal,
    decision: ApprovalContext,
    *,
    decision_kind: DecisionKind = DecisionKind.PLAN_ACCEPTED,
    initial_states: dict[str, NodeState] | None = None,
) -> CompileResult:
    """Validate a proposal authoritatively and compile it to rows, atomically.

    This is the engine's single ingress for plan state. See the module docstring
    for why each guarantee below is structural rather than conventional.

    Args:
        session: The caller's session. Its transaction is **not** committed here —
            gate approval commits this compile together with its own writes.
        proposal: The document to compile.
        decision: Server-resolved approval context. `decision.org_id` is the
            tenant the plan lands in; the document's declared `org_id` is only
            compared against it.
        decision_kind: The kind of decision row this compile records. Defaults to
            `PLAN_ACCEPTED`, which is what every acceptance path wants. Issue
            #4528's draft registration passes `PLAN_DRAFTED` — a kind absent from
            `genesis.APPROVAL_DECISION_KINDS`, so the compiled graph exists and is
            readable while being unable to root a dispatch. Parameterised here
            rather than forked into a second compiler because a second
            "insert the proposal's nodes" is free to accept a document this one
            would refuse, which is exactly what AC-29 forbids.
        initial_states: Graph address -> the state that node is *created* in, for
            addresses this compile creates. Anything omitted uses the column
            default (`pending`). This is a creation-time value, not a transition:
            `transition()` is the authority for *changing* a node's state and is
            untouched by this. #4528's acceptance gate is born in `awaiting_gate`
            because there is no legal edge into that state from `pending`, so the
            alternative would be a second, unguarded writer of node state.

    Returns:
        A `CompileResult`. `already_compiled` is True when the identical document
        was already in force and nothing was written.

    Raises:
        ProposalRejectedError: The document failed validation. No rows written.
        TenantMismatchError: The document declares a different tenant than the
            approver's resolved org. No rows written.
        NonApprovalSupersedeError: `decision_kind` is not an approval kind and a
            plan is already in force for the flow. No rows written.
    """
    # --- Gate 1: authoritative re-validation -------------------------------
    # The advisory CLI may or may not have run. This is the control, and it runs
    # before anything opens a savepoint so a refused document touches no state.
    violations = validate_proposal(proposal)
    if violations:
        detail = "; ".join(str(violation) for violation in violations)
        raise ProposalRejectedError(
            f"loop proposal failed authoritative validation with {len(violations)} violation(s): {detail}",
            violations=violations,
        )

    # --- Gate 2: tenant ownership -----------------------------------------
    # Compared, never substituted. A mismatch is rejected rather than re-homed.
    if proposal.org_id != decision.org_id:
        raise TenantMismatchError(
            f"proposal declares org_id {proposal.org_id!r} but the approver's resolved org is {decision.org_id!r}; "
            "a proposal is never re-homed to the approver's tenant"
        )

    repo = OrchestrationRepository(session)
    document = proposal.model_dump(mode="json")
    document_hash = plan_hash(proposal)

    # Everything below is one savepoint: nodes, edges, the decision and the
    # accepted-plan row land together or not at all. A partial compile would put
    # nodes on the graph for a plan nobody accepted.
    async with session.begin_nested():
        flow = await _resolve_flow(repo, proposal=proposal, org_id=decision.org_id)

        # --- Idempotency (R-NF2) -------------------------------------------
        # An identical document already in force means this is a retry. Return
        # what exists rather than writing a second identical plan version.
        in_force = await repo.get_accepted_plan(org_id=decision.org_id, flow_id=flow.id)
        if in_force is not None and in_force.plan_hash == document_hash:
            existing = await repo.list_nodes(org_id=decision.org_id, flow_id=flow.id)
            return CompileResult(
                flow_id=flow.id,
                plan_version=in_force.version,
                decision_id=in_force.accepted_by_decision_id or "",
                plan_hash=document_hash,
                node_ids={address_of(flow.slug, node): node.id for node in existing},
                already_compiled=True,
            )

        # --- Gate 3: only an approval may rewrite the plan of record ----------
        # Checked *after* the idempotency return above, deliberately: an identical
        # document writes nothing at all, so a retry must stay a no-op rather than
        # become a 409 the moment a plan is in force. That ordering is the whole
        # reason a fail-soft caller (issue #4528's worker) can retry safely.
        #
        # This lives here, in the primitive, rather than in the calling module that
        # needs it. `registration.py` has its own stricter refusal, but a guard in a
        # caller only protects that caller: reached directly with a non-approval
        # `decision_kind`, this function would supersede the in-force plan (a
        # plan-of-record rewrite) and — because `_latest_approval_decision_id` is
        # flow-scoped — leave the new nodes rooted by the *human's* pre-existing
        # approval, dispatching work nobody accepted under that human's identity.
        # Both escalations from review PR #4558 reproduce through that route, so the
        # invariant belongs where no caller can bypass it. `APPROVAL_DECISION_KINDS`
        # is imported from `genesis.py` rather than restated so this and the rule
        # that roots dispatch cannot disagree about what an approval is.
        if in_force is not None and decision_kind.value not in APPROVAL_DECISION_KINDS:
            raise NonApprovalSupersedeError(
                f"a {decision_kind.value!r} compile cannot supersede plan version {in_force.version}, which is in force "
                f"for flow {flow.slug!r}: only an approval decision may rewrite the plan of record. Use the amendment "
                "path (PLAN_APPROVE), or target a flow with no plan in force."
            )

        node_ids, nodes_created = await upsert_nodes(
            repo,
            proposal=proposal,
            org_id=decision.org_id,
            flow_id=flow.id,
            initial_states=initial_states,
        )
        edges_created = await upsert_edges(repo, proposal=proposal, org_id=decision.org_id, flow_id=flow.id, node_ids=node_ids)

        # The decision is appended before the plan row so the plan can point at
        # it: `accepted_by_decision_id` is nullable only because one of the two
        # has to be inserted first, not because it is optional information.
        record = await repo.append_decision(
            org_id=decision.org_id,
            flow_id=flow.id,
            kind=decision_kind.value,
            actor_id=decision.actor_id,
            actor_role=decision.actor_role,
            actor_kind=ActorKind(decision.actor_kind).value,
            reason=decision.reason,
        )

        plan = await repo.record_accepted_plan(
            org_id=decision.org_id,
            flow_id=flow.id,
            plan_document=document,
            plan_hash=document_hash,
            accepted_by_decision_id=record.id,
        )

        return CompileResult(
            flow_id=flow.id,
            plan_version=plan.version,
            decision_id=record.id,
            plan_hash=document_hash,
            node_ids=node_ids,
            nodes_created=nodes_created,
            edges_created=edges_created,
        )


# --- Shared with amend.py ---------------------------------------------------
# The three helpers below are module-public (no leading underscore) because
# `amend.py` calls them. Issue #4200 amends a plan by reusing this compile path
# rather than reimplementing it: a second copy of "insert the proposal's nodes"
# could accept a document this one would refuse, and validation parity between
# the original and amended paths is exactly what AC-29 forbids breaking.


def address_of(flow_slug: str, node) -> str:
    """Reassemble a stored node's graph address from its components.

    The store holds the four segments denormalised (there is no container table to
    join out of), so the address is composed on read rather than selected.
    """
    return f"{flow_slug}/{node.epic_ref}/{node.wave_ref}/{node.node_ref}"


async def _resolve_flow(repo: OrchestrationRepository, *, proposal: LoopProposal, org_id: str):
    """Find the flow this proposal is for within the tenant, or create it.

    Matched by slug, scoped to `org_id`: the slug is the flow segment of every
    node's address, so it is what the document actually identifies. Two tenants
    may each have a `delivery-loop` flow, and they are different flows — which is
    why the lookup is over the tenant's flows and never global.

    The design-loop capture fields (#4885) are written **on creation only**. An
    existing flow keeps whatever it already has, which is the same rule
    `title` and `intent_ref` have always followed here: this function resolves a
    flow, it does not reconcile one. Updating them on every compile would let a
    later document silently rewrite the recorded design history of a flow whose
    gates a human already answered — and an amendment (`amend.py` reaches this
    same path) would overwrite the inception record of the plan it amends.
    """
    for candidate in await repo.list_flows(org_id=org_id):
        if candidate.slug == proposal.flow_slug:
            return candidate

    return await repo.create_flow(
        org_id=org_id,
        slug=proposal.flow_slug,
        title=proposal.title,
        intent_ref=proposal.intent_ref,
        description=proposal.description,
        # Dumped to plain JSON, not handed over as the Pydantic model: this value
        # goes into a JSON column, and `mode="json"` is what turns the validated
        # `approved_at` datetimes back into the ISO strings the column stores and
        # the API re-serialises. `None` stays `None` — the absent case must not
        # become `{}`, which would render as a design history with no stages.
        design_history=(proposal.design_history.model_dump(mode="json") if proposal.design_history else None),
    )


async def upsert_nodes(
    repo: OrchestrationRepository,
    *,
    proposal: LoopProposal,
    org_id: str,
    flow_id: str,
    initial_states: dict[str, NodeState] | None = None,
) -> tuple[dict[str, str], int]:
    """Insert the proposal's nodes, reusing any that already exist by address.

    Reuse rather than insert-and-hope is what makes idempotency hold for a
    *partial* overlap, not just the identical-document case. The store's
    `uq_orchestration_nodes_address` index would reject a duplicate anyway, so the
    alternative is not "two nodes" but "an IntegrityError mid-transaction" — a
    retry after a partial failure would fail forever instead of converging.

    Every newly created node starts in `NodeState.PENDING`, which is the column
    default in `models.py`. Not passed explicitly: a literal here would be a
    second place the initial state is decided, and the two could disagree.

    `initial_states` overrides that default per address, and applies **only to
    nodes this call creates** — a node already on the graph keeps whatever state
    it reached, so a retried registration cannot reset a gate a human already
    answered back to `awaiting_gate`.
    """
    existing = {address_of(proposal.flow_slug, node): node for node in await repo.list_nodes(org_id=org_id, flow_id=flow_id)}

    node_ids: dict[str, str] = {}
    created = 0

    for proposed in proposal.nodes:
        present = existing.get(proposed.address)
        if present is not None:
            node_ids[proposed.address] = present.id
            continue

        _, epic_ref, wave_ref, node_ref = split_address(proposed.address)
        override = (initial_states or {}).get(proposed.address)
        node = await repo.add_node(
            org_id=org_id,
            flow_id=flow_id,
            epic_ref=epic_ref,
            wave_ref=wave_ref,
            node_ref=node_ref,
            kind=proposed.kind,
            title=proposed.title,
            issue_ref=proposed.issue_ref,
            state=override.value if override is not None else None,
        )
        node_ids[proposed.address] = node.id
        created += 1

    return node_ids, created


async def upsert_edges(
    repo: OrchestrationRepository,
    *,
    proposal: LoopProposal,
    org_id: str,
    flow_id: str,
    node_ids: dict[str, str],
) -> int:
    """Insert the proposal's edges, skipping any already present.

    Endpoints are resolved through `node_ids`, which validation has already
    guaranteed covers every edge endpoint (rule 3 rejects a dangling endpoint), so
    a `KeyError` here would mean validation and compile disagree — impossible
    while both call the same `validate_proposal`.
    """
    present = {(edge.from_node_id, edge.to_node_id) for edge in await repo.list_edges(org_id=org_id, flow_id=flow_id)}

    created = 0
    for proposed in proposal.edges:
        pair = (node_ids[proposed.from_address], node_ids[proposed.to_address])
        if pair in present:
            continue
        await repo.add_edge(org_id=org_id, flow_id=flow_id, from_node_id=pair[0], to_node_id=pair[1])
        present.add(pair)
        created += 1

    return created


# Re-exported so a caller asserting "compiled nodes start pending" imports the
# value from the same place the compiler does, rather than spelling the literal.
INITIAL_NODE_STATE = NodeState.PENDING
