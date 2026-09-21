"""Register a compiled loop proposal as an inert draft.

Issue #4528 (EPIC #4191, intent #4120), story 2/3 of the engine bridge. The
authoring agent finishes a loop proposal and the plan lands here, where it becomes
a graph a human can *see* — and nothing else. Making it live is a human act
performed later, through story 1/3's `@agent-engine accept` comment or the
dashboard's accept button, both of which already work on what this module
produces and neither of which needed changing.

--------------------------------------------------------------------------------
Why a draft is inert, twice over
--------------------------------------------------------------------------------

"Registration must not auto-start execution" is the issue's first bug class, and
one guard is not enough for it: a single check is a check somebody can delete. So
there are two independent reasons a registered draft cannot execute, and removing
either one leaves the other standing.

**1. No approval decision exists.** Registration records `PLAN_DRAFTED`, which is
deliberately absent from `genesis.APPROVAL_DECISION_KINDS`. The dispatch pass
looks for the latest decision *of an approval kind* to root a chain (D-R12); with
none, it counts `genesis_refused` and leaves the node alone. This is why
registration cannot simply call the acceptance path: a plain `compile_proposal`
writes `PLAN_ACCEPTED`, which is exactly the row that arms dispatch. The engine
would have started executing a plan whose author was an agent, which inverts the
EPIC's central guarantee rather than bending it.

**2. The graph is topologically behind one human-only edge.** Registration
synthesises an *acceptance gate* that every root of the proposed graph depends on,
and creates it in `awaiting_gate`. `state.py` makes every edge out of
`awaiting_gate` toward progress `_HUMAN_ONLY`, so a service actor cannot walk out
of it; the tick only advances a node whose predecessors are all `passed`, so
nothing behind the gate is even a candidate. A tick may run a thousand times and
move nothing.

The acceptance gate is also what makes the plan *accept-ready* rather than merely
stored. `@agent-engine accept` answers "the flow's single node in `awaiting_gate`"
(#4527, `engine_commands._resolve_gate`), so the draft presents exactly one
answerable gate and the one-command promise in the closing comment is literally
true. Answering it writes `GATE_APPROVED` — an approval kind — which is what
supplies the human root guard 1 was withholding. The two guards release together,
by one human act, which is the design.

**3. Registration creates flows; it never extends one.** The two guards above are
both *node*-level, and the first review of this module (PR #4558) found that both
are bypassed — not broken — by registering into a flow that already carries a
human approval. Neither guard was wrong; they were answering the wrong question.

`_latest_approval_decision_id` (`dispatch_pass.py`) selects the latest
approval-kind decision **anywhere in the flow** — it is flow-scoped, not
node-scoped. So a pre-existing human `PLAN_ACCEPTED`/`GATE_APPROVED` row roots a
node appended later by *anyone*, and guard 1 becomes irrelevant because the
registrant never has to write an approval row at all. Guard 2 fails in the same
breath: `initial_states` applies only to addresses a compile *creates*
(`compile.upsert_nodes`), so if the acceptance gate's address already exists it
keeps whatever state it reached — `passed`, for a flow whose gate a human already
answered. The reproduction dispatched an attacker-chosen issue as a real agent run
attributed to the human approver, with `is_human_rooted: true`.

`_refuse_if_flow_is_live` is therefore the third guard, and it is the one that
matches the scope of the thing it defends: **a flow that has ever been approved is
not a registration target.** Two independent conditions, checked read-only before
anything is created, because they fail independently:

- **Any approval-kind decision on the flow.** This targets exactly the row
  `_latest_approval_decision_id` would select. Phrased as "is there an approval
  the engine could root a dispatch in", not "is there a plan", because the
  decision table is what dispatch actually reads.
- **A differing in-force accepted plan.** This targets what the plan store reads.

This also closes the second finding, plan-of-record rewrite (`record_accepted_plan`
supersedes the in-force row, and `PLAN_DRAFT` is held by every ordinary member).
The fix is structural rather than a permission check: with the refusal in force a
draft can only ever insert **version 1 into a flow with nothing in force**, so the
supersede branch is unreachable from this path. Stated out loud here because
`compile_proposal` calls `record_accepted_plan` unconditionally, and "the caller
guarantees there is nothing to supersede" is the kind of invariant that gets
silently invalidated by a later edit if nobody writes it down.

The single permitted overlap is the **exact idempotent retry**: the same document,
hash for hash, already in force. A fail-soft caller retries by construction (see
the worker's `draft_registration_note`), so refusing a retry would turn a dropped
connection into a permanent failure. That case writes nothing — `compile_proposal`
returns `already_compiled` — so permitting it grants no new authority.

The gate is **born** in `awaiting_gate` rather than created pending and moved. A
move would need a `pending -> awaiting_gate` edge that `state.py` does not have,
and inventing one would open that edge to the *engine* everywhere else in the
graph. Creating a node in a state is not a transition and does not weaken
`transition()`'s monopoly on state *changes*.

--------------------------------------------------------------------------------
The autonomy default: gates at every wave boundary (OFF by default, see #4575)
--------------------------------------------------------------------------------

The *intent* of this transform: while the engine is new, a proposal that declares
**no gate at all** gets one inserted at every wave boundary, so a human sees each
wave land before the next starts. A proposal that declares its own gates is passed
through untouched — an author who thought about gating is not overridden by a
default whose whole purpose is to cover authors who did not.

The transform remains opt-in via `ORCHESTRATION_AUTONOMY_GATE_EVERY_WAVE`
so upgrading does not silently change authors' autonomy preferences. Gate nodes
are presented by the tick once their predecessors pass (#4575). The acceptance
gate is separate and always inserted so drafts require approval before execution.

It is a registration-time transform over the document, not a compile-time special
case, for one concrete reason: the transformed document is what
`validate_proposal` then judges. A transform that produced a duplicate address, a
dangling edge or a cycle is *rejected* with the same violations any bad document
gets, rather than being compiled because it came from trusted code. Trusted code
is exactly the code nobody re-checks.

`insert_wave_gates` re-routes cross-wave edges *through* the wave's gate rather
than adding the gate alongside them. A gate hanging off the side of a boundary the
work flows straight past is decoration; the point is that the next wave's
predecessor is the gate.

--------------------------------------------------------------------------------
Tenant isolation
--------------------------------------------------------------------------------

Nothing here resolves a tenant. `org_id` arrives on the server-resolved
`ApprovalContext` the route built from the authenticated principal, and
`compile_proposal`'s Gate 2 *compares* the document's declared `org_id` against it
and rejects a mismatch instead of re-homing. This module deliberately adds no
tenant logic of its own — a second implementation could disagree with that one.
"""

from __future__ import annotations

import logging
import os

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .compile import ApprovalContext, CompileResult, ProposalRejectedError, address_of, compile_proposal, plan_hash
from .genesis import APPROVAL_DECISION_KINDS
from .models import DecisionKind, NodeKind, OrchestrationFlow
from .proposal import ADDRESS_PATTERN, LoopProposal, ProposedEdge, ProposedNode, split_address
from .repository import OrchestrationRepository
from .state import NodeState

logger = logging.getLogger("bedrockgateway.orchestration.registration")

__all__ = [
    "ACCEPTANCE_GATE_REF",
    "AUTONOMY_FLAG_ENV",
    "WAVE_GATE_REF",
    "DraftFlowConflictError",
    "acceptance_gate_address",
    "demote_proposed_policy",
    "gate_every_wave_enabled",
    "insert_acceptance_gate",
    "insert_wave_gates",
    "promote_proposed_policy",
    "register_draft_proposal",
    "transform_for_registration",
]


class DraftFlowConflictError(ProposalRejectedError):
    """The target flow has already been approved, so it is not a registration target.

    A subclass of `ProposalRejectedError` so a caller that only asks "was this
    refused?" catches it with everything else, while staying separately catchable:
    this is the refusal that means *someone tried to extend an approved plan*, which
    is a different event to an author's malformed document and deserves a different
    status code (409, not 422) and a different level of operator attention.

    Raised **before** anything is created — the flow lookup is read-only — so a
    refusal leaves no flow row, no nodes and no decision behind.
    """


# The per-environment autonomy setting. Read from the environment rather than
# threaded through the request so it is an operator lever, not a caller's choice:
# a client-supplied "skip the gates" parameter would let the authoring agent turn
# its own training wheels off.
AUTONOMY_FLAG_ENV = "ORCHESTRATION_AUTONOMY_GATE_EVERY_WAVE"

# The node segment of the synthesised acceptance gate, and of a synthesised
# wave-boundary gate. Fixed strings because they are addresses: a caller that
# could name them could collide with an authored node deliberately.
ACCEPTANCE_GATE_REF = "accept"
WAVE_GATE_REF = "wave-gate"

_FALSE_SPELLINGS = frozenset({"0", "false", "no", "off"})


def gate_every_wave_enabled() -> bool:
    """Whether a gateless proposal gets a gate at every wave boundary.

    Defaults to False to preserve existing autonomy preferences. Operators may
    opt into an inserted checkpoint for every wave; authored gates always remain
    in place, and the engine presents them once their prerequisites pass. The
    separate acceptance gate still requires approval before a draft starts.
    """
    raw = os.environ.get(AUTONOMY_FLAG_ENV)
    if raw is None:
        return False
    return raw.strip().lower() not in _FALSE_SPELLINGS


def _waves_in_order(proposal: LoopProposal) -> list[tuple[str, str]]:
    """The proposal's `(epic, wave)` keys, in first-appearance order.

    Declaration order, not sorted order: wave refs are author-chosen strings, and
    sorting them lexically puts `wave-10` before `wave-2`. Where the boundaries
    are is a question about the *edges*, which is what `insert_wave_gates` uses
    this ordering only to name things consistently.
    """
    seen: list[tuple[str, str]] = []
    for node in proposal.nodes:
        if not ADDRESS_PATTERN.match(node.address):
            continue
        _, epic, wave, _ = split_address(node.address)
        if (epic, wave) not in seen:
            seen.append((epic, wave))
    return seen


def _wave_of(address: str) -> tuple[str, str] | None:
    """The `(epic, wave)` an address belongs to, or None if it is malformed.

    Malformed addresses are left alone rather than raising: rule 1 will reject the
    document a moment later with a precise violation, and crashing here would turn
    an author's typo into a 500.
    """
    if not ADDRESS_PATTERN.match(address):
        return None
    _, epic, wave, _ = split_address(address)
    return epic, wave


def insert_wave_gates(proposal: LoopProposal) -> LoopProposal:
    """Put a gate on every wave boundary. No-op if the proposal declares any gate.

    A wave's gate is inserted between the wave and everything downstream of it:
    the wave's sinks (nodes with no successor inside the wave) point at the gate,
    and every edge that left the wave now leaves the gate instead. That is what
    makes it a boundary gate rather than an ornament — after the transform there is
    no path out of the wave that does not pass through it.

    The last wave gets no gate: it has no boundary after it, and the acceptance
    gate already covers "may this plan run at all".
    """
    if any(node.kind == NodeKind.GATE.value for node in proposal.nodes):
        # The author gated their own plan. Passing it through untouched is the
        # point of the condition, not an optimisation.
        return proposal

    waves = _waves_in_order(proposal)
    if len(waves) < 2:
        # One wave has no internal boundary. The acceptance gate is the only gate
        # such a plan can meaningfully have.
        return proposal

    outgoing: dict[str, list[ProposedEdge]] = {}
    for edge in proposal.edges:
        outgoing.setdefault(edge.from_address, []).append(edge)

    gate_nodes: list[ProposedNode] = []
    edges: list[ProposedEdge] = []
    # Only waves that something actually leaves need a gate; a wave nothing
    # depends on has no boundary to guard.
    gate_address_for: dict[tuple[str, str], str] = {}

    for epic, wave in waves:
        crosses_out = any(
            _wave_of(edge.from_address) == (epic, wave) and _wave_of(edge.to_address) not in (None, (epic, wave)) for edge in proposal.edges
        )
        if not crosses_out:
            continue
        gate_address_for[(epic, wave)] = f"{proposal.flow_slug}/{epic}/{wave}/{WAVE_GATE_REF}"

    if not gate_address_for:
        return proposal

    for (epic, wave), gate_address in gate_address_for.items():
        gate_nodes.append(
            ProposedNode(
                address=gate_address,
                kind=NodeKind.GATE.value,
                title=f"Human gate: {wave} complete (auto-inserted at the wave boundary)",
            )
        )

    for edge in proposal.edges:
        from_wave = _wave_of(edge.from_address)
        to_wave = _wave_of(edge.to_address)
        gate_address = gate_address_for.get(from_wave) if from_wave is not None else None

        if gate_address is None or to_wave == from_wave:
            # Wholly inside a wave, or out of a wave with no gate: unchanged.
            edges.append(edge)
            continue

        # A cross-wave edge. It now runs source -> gate -> destination, so the
        # downstream wave's predecessor is the gate.
        edges.append(ProposedEdge(from_address=edge.from_address, to_address=gate_address))
        edges.append(ProposedEdge(from_address=gate_address, to_address=edge.to_address))

    # Re-routing the cross-wave edges is not sufficient on its own. A node whose
    # only successors are inside its own wave — or which has no successors at all —
    # contributes no cross-wave edge to re-route, so nothing above puts it behind
    # the gate, and the next wave could start while it was still running. That is
    # weaker than the "a human sees each wave land before the next starts" intent
    # the autonomy default exists to serve (found in review, PR #4558).
    #
    # So every *sink within the wave* gains an explicit edge to its gate. Sink is
    # judged on successors inside the same wave only: a node that already reaches
    # the gate via a re-routed cross-wave edge is not a sink and needs no second
    # path, and `_dedupe_edges` would collapse it anyway.
    # Iterates the ORIGINAL nodes, so the synthesised gates are not themselves
    # candidates — a gate is not a sink of its own wave, and treating it as one
    # would emit a self-edge that rule 3 rejects as a one-node cycle.
    for node in proposal.nodes:
        wave_key = _wave_of(node.address)
        gate_address = gate_address_for.get(wave_key) if wave_key is not None else None
        if gate_address is None:
            continue

        has_successor_in_wave = any(_wave_of(edge.to_address) == wave_key for edge in outgoing.get(node.address, ()))
        if has_successor_in_wave:
            continue

        edges.append(ProposedEdge(from_address=node.address, to_address=gate_address))

    return proposal.model_copy(update={"nodes": [*proposal.nodes, *gate_nodes], "edges": _dedupe_edges(edges)})


def insert_acceptance_gate(proposal: LoopProposal) -> tuple[LoopProposal, str]:
    """Put one gate in front of the whole graph. Returns the document and its address.

    Every node with no predecessor gains the gate as its predecessor, so the gate
    dominates the graph: there is no node whose predecessors can all be `passed`
    while the gate is unanswered. Combined with the gate being created in
    `awaiting_gate` and every progress edge out of `awaiting_gate` being
    human-only, that is what "inert" means structurally rather than by policy.

    Placed in the first wave of the first EPIC. A gate is exempt from rule 4's
    "a wave with stories has exactly one eval" (which counts stories and evals, not
    gates), so adding it cannot invalidate an otherwise-valid document.
    """
    waves = _waves_in_order(proposal)
    epic, wave = waves[0] if waves else ("epic", "wave-1")
    address = f"{proposal.flow_slug}/{epic}/{wave}/{ACCEPTANCE_GATE_REF}"

    has_incoming = {edge.to_address for edge in proposal.edges}
    roots = [node.address for node in proposal.nodes if node.address not in has_incoming]

    gate = ProposedNode(
        address=address,
        kind=NodeKind.GATE.value,
        title="Human gate: accept this plan to start execution",
    )
    edges = [*proposal.edges, *(ProposedEdge(from_address=address, to_address=root) for root in roots)]

    return proposal.model_copy(update={"nodes": [gate, *proposal.nodes], "edges": _dedupe_edges(edges)}), address


def _dedupe_edges(edges: list[ProposedEdge]) -> list[ProposedEdge]:
    """Drop duplicate endpoint pairs, preserving order.

    Re-routing can produce the same `gate -> node` pair from several cross-wave
    edges. The store would collapse them anyway (`upsert_edges` skips pairs it has
    already seen), but a document carrying visible duplicates is a document an
    operator reading the accepted plan has to explain.
    """
    seen: set[tuple[str, str]] = set()
    unique: list[ProposedEdge] = []
    for edge in edges:
        pair = (edge.from_address, edge.to_address)
        if pair in seen:
            continue
        seen.add(pair)
        unique.append(edge)
    return unique


def acceptance_gate_address(proposal: LoopProposal) -> str | None:
    """The address of the gate that dominates this whole document, or None (#5331).

    Identifies the gate whose answer means "this plan may run at all", as distinct
    from an authored gate or an inserted wave-boundary gate. Needed because
    promoting a demoted policy is something only the *acceptance* gate's approval
    may do: a wave gate says "the previous wave's work is good", which is not a
    grant of delegated authority over everything still to come.

    Identified **structurally**, not by name alone, and that is the load-bearing
    part. `insert_acceptance_gate` points the new gate at every prior root, so after
    the transform the acceptance gate is the *only* node in the document with no
    incoming edge — rule 3 rejects cycles, so a valid document cannot have a second
    one. Trusting the `accept` node ref by itself would let an author who declared
    their own gate with that ref in a later wave have its approval activate the
    plan's policy, which is the authority inversion this whole field exists to
    prevent. All three conditions must hold: sole root, gate kind, fixed ref.

    Returns None for any document that does not present exactly that shape,
    including an untransformed authored document and any graph with several roots.
    None means "no promotion from this node", never "promote anyway".
    """
    has_incoming = {edge.to_address for edge in proposal.edges}
    roots = [node for node in proposal.nodes if node.address not in has_incoming]
    if len(roots) != 1:
        return None
    root = roots[0]
    if root.kind != NodeKind.GATE.value or not ADDRESS_PATTERN.match(root.address):
        return None
    _, _, _, node_ref = split_address(root.address)
    return root.address if node_ref == ACCEPTANCE_GATE_REF else None


def demote_proposed_policy(proposal: LoopProposal) -> LoopProposal:
    """Move a submitted `execution_policy` into `proposed_execution_policy` (#5331).

    The registration transform that makes a policy-bearing document *registrable*
    while granting nothing. Before this existed, such a document could not be
    registered as a draft at all: `compile.accept_execution_policy` refuses a
    non-human acceptor, draft registration compiles as `ActorKind.SERVICE` by
    design, and so the plans carrying the most authority were the only ones that
    could not be previewed and then accepted at an exact revision. The author's
    remaining options were to approve in one unattended step without seeing the
    effective graph, or to strip the bounds.

    Demotion is not a softened version of that refusal — the refusal is untouched.
    `accept_execution_policy` still refuses a SERVICE actor that reaches it with
    `execution_policy` set, and a test holds that. What changes is that the draft
    path no longer reaches it with that field set, because the policy now travels in
    a field `policy_admission.load_in_force_policy` contains no code to read. The
    document compiles, the graph is inert behind its acceptance gate as always, and
    the policy's full text is available for a human to review before granting it.

    A no-op when there is no policy, and a no-op when the policy has already been
    demoted — so applying the transform twice (a retry re-running it over a document
    read back from the store) cannot lose or duplicate anything.

    Deliberately NOT applied by the direct acceptance route (`POST /flows`): its
    caller is human and holds `PLAN_APPROVE`, so the policy is stamped in that same
    call and there is no interval between proposal and grant to protect. Demoting
    there would add a second step to a path that has no gap.
    """
    if proposal.execution_policy is None:
        return proposal
    return proposal.model_copy(update={"execution_policy": None, "proposed_execution_policy": proposal.execution_policy})


def promote_proposed_policy(proposal: LoopProposal) -> LoopProposal:
    """Move `proposed_execution_policy` back into `execution_policy` (#5331).

    The inverse of :func:`demote_proposed_policy`, and the ONLY way a demoted policy
    becomes authority. Called from the bound-acceptance path when a human answers a
    plan's acceptance gate with the exact revision they reviewed
    (`adapters/github_comments.apply_gate_answer_for_context`), and from nowhere
    else — not from the tick, not from dispatch, not from any agent-reachable route.

    This function does **not** decide whether promotion is allowed. It moves a field.
    The authority decision stays where it already lives: the caller hands the result
    to `compile.accept_execution_policy`, which refuses any non-human acceptor and
    stamps the policy with that human's principal id via `stamp_policy`. Keeping the
    two separate is what stops this from becoming a second, weaker acceptance check
    that is free to drift from the real one — there is still exactly one place a
    policy becomes a grant.

    A no-op when nothing was demoted, so the promotion step is unconditional at the
    call site and a policyless plan's acceptance is byte-identical to what it was.
    """
    if proposal.proposed_execution_policy is None:
        return proposal
    return proposal.model_copy(update={"execution_policy": proposal.proposed_execution_policy, "proposed_execution_policy": None})


def transform_for_registration(proposal: LoopProposal) -> tuple[LoopProposal, str]:
    """Apply the registration-time transforms. Returns the document and the gate address.

    Order matters: wave gates first, then the acceptance gate. Reversed, the
    acceptance gate would already be a declared gate and `insert_wave_gates` would
    read the document as author-gated and decline to touch it — the autonomy
    default would silently never apply to anything.

    The policy demotion (#5331) runs last, and its position does not matter to the
    two gate transforms — neither reads the policy — but it must be inside this
    function rather than at its two call sites. `register_draft_proposal` hashes what
    this returns and `preview_draft` displays what this returns, and the hash the
    preview shows is the revision an acceptance binds to; a demotion applied at only
    one of the two would make the preview's hash and the registered hash disagree,
    so every bound acceptance of a policy-bearing plan would be refused as stale.
    """
    working = insert_wave_gates(proposal) if gate_every_wave_enabled() else proposal
    transformed, gate_address = insert_acceptance_gate(working)
    return demote_proposed_policy(transformed), gate_address


async def _refuse_if_flow_is_live(
    session: AsyncSession,
    *,
    proposal: LoopProposal,
    org_id: str,
    document_hash: str,
) -> None:
    """Refuse to register into a flow that has ever been approved.

    The third inertness guard, and the only one scoped to the flow rather than to a
    node — see the module docstring for why the node-level guards are not enough on
    their own. Read-only: it resolves the flow without creating it, so a refusal
    writes nothing at all.

    Args:
        session: Caller-owned session. Nothing is written.
        proposal: The **transformed** document — the one `compile_proposal` will
            receive. Only `flow_slug` is read from it, which the transforms do not
            change, but it is passed post-transform so that it and `document_hash`
            cannot describe two different documents.
        org_id: The server-resolved tenant.
        document_hash: `plan_hash` of that same transformed document. Must match
            what `compile_proposal` computes, or the retry permitted below is a
            different retry to the one it treats as a no-op.

    Raises:
        DraftFlowConflictError: The flow carries an approval-kind decision, or an
            in-force plan that is not this same document.
    """
    repo = OrchestrationRepository(session)

    flow = next((candidate for candidate in await repo.list_flows(org_id=org_id) if candidate.slug == proposal.flow_slug), None)
    if flow is None:
        # The overwhelmingly common case: a new flow, nothing to conflict with.
        return

    in_force = await repo.get_accepted_plan(org_id=org_id, flow_id=flow.id)

    # The exact idempotent retry, checked FIRST and deliberately so. `compile_proposal`
    # returns early for an identical in-force document and writes nothing at all — no
    # node, no edge, no decision, no plan row — so permitting it cannot grant any
    # authority, on an approved flow or otherwise. Checking it before the refusals
    # below is what makes the fail-soft retry property unconditional: a worker whose
    # connection dropped gets the same answer whether or not a human has since
    # accepted the plan, instead of a 409 for a request that would change nothing.
    if in_force is not None and in_force.plan_hash == document_hash:
        return

    # Condition 1: the row `_latest_approval_decision_id` would select. Checked on
    # the decision table because that is what dispatch reads to root a chain — a
    # flow with an approval can root any node later appended to it.
    decisions = await repo.list_decisions(org_id=org_id, flow_id=flow.id)
    approvals = [decision for decision in decisions if decision.kind in APPROVAL_DECISION_KINDS]
    if approvals:
        raise DraftFlowConflictError(
            f"flow {proposal.flow_slug!r} already carries {len(approvals)} approval decision(s), so it is not a "
            "registration target: a node registered into it would be rooted by a human approval nobody gave for it. "
            "Register the proposal under a new flow_slug, or amend the existing plan through the approval path."
        )

    # Condition 2: what the plan store reads. Anything reaching here is a *different*
    # document, which would supersede the in-force plan — a rewrite of the plan of
    # record, and not something this permission authorises.
    if in_force is not None:
        raise DraftFlowConflictError(
            f"flow {proposal.flow_slug!r} already has plan version {in_force.version} in force. Registering a "
            "different document would supersede it, which is a rewrite of the plan of record; use the amendment "
            "path (PLAN_APPROVE) for that, or register under a new flow_slug."
        )


async def register_draft_proposal(
    session: AsyncSession,
    proposal: LoopProposal,
    actor: ApprovalContext,
) -> tuple[CompileResult, str]:
    """Register a proposal as a draft. Returns the compile result and gate address.

    Delegates to `compile_proposal` — still the only code that creates flow, node
    and edge rows — with the draft decision kind and the acceptance gate's initial
    state. Everything that makes a compile trustworthy (authoritative
    re-validation, tenant comparison, one savepoint, idempotency by `plan_hash`)
    therefore applies to a draft unchanged, including for the retry a fail-soft
    caller will make.

    Args:
        session: Caller-owned session. Nothing is committed here.
        proposal: The authored document, *before* the registration transforms.
        actor: Server-resolved context. `actor.org_id` is the tenant.

    Registration **creates** flows and never extends one: a flow that has already
    been approved is refused outright (see the module docstring, guard 3). That is
    what keeps a draft unable to inherit a sibling node's human approval, and what
    makes `compile_proposal`'s unconditional `record_accepted_plan` safe to reach
    from here — there is nothing in force to supersede.

    Returns:
        `(result, acceptance_gate_address)`.

    Raises:
        DraftFlowConflictError: The target flow has already been approved. Checked
            first, and read-only, so this refusal writes nothing.
        ProposalRejectedError: The transformed document failed validation.
        TenantMismatchError: The document declares another tenant.
    """
    transformed, gate_address = transform_for_registration(proposal)
    document_hash = plan_hash(transformed)
    repo = OrchestrationRepository(session)
    flow = await session.scalar(
        select(OrchestrationFlow).where(OrchestrationFlow.org_id == actor.org_id, OrchestrationFlow.slug == transformed.flow_slug).with_for_update()
    )
    if flow is not None:
        # A bound acceptance promotes the policy and records a new plan version.
        # Replaying the original registration must return that original draft,
        # without replacing the granted plan or compiling additional nodes.
        for prior in await repo.list_plan_versions(org_id=actor.org_id, flow_id=flow.id):
            if prior.plan_hash == document_hash:
                nodes = await repo.list_nodes(org_id=actor.org_id, flow_id=flow.id)
                return CompileResult(
                    flow_id=flow.id,
                    plan_version=prior.version,
                    decision_id=prior.accepted_by_decision_id,
                    plan_hash=prior.plan_hash,
                    node_ids={address_of(flow.slug, node): node.id for node in nodes},
                    already_compiled=True,
                ), gate_address

    # Hashed on the TRANSFORMED document, because that is what `compile_proposal`
    # receives and therefore what its idempotency compares and what the plan store
    # holds. Hashing the authored document here would make the retry this check
    # permits a different retry to the one that call treats as a no-op, and every
    # fail-soft retry would be refused as a plan-of-record rewrite.
    await _refuse_if_flow_is_live(session, proposal=transformed, org_id=actor.org_id, document_hash=document_hash)

    result = await compile_proposal(
        session,
        transformed,
        actor,
        decision_kind=DecisionKind.PLAN_DRAFTED,
        initial_states={gate_address: NodeState.AWAITING_GATE},
    )

    logger.info(
        "plan_drafted flow=%s org=%s actor=%s v%s nodes=%s edges=%s gate=%s idempotent=%s",
        result.flow_id,
        actor.org_id,
        actor.actor_id,
        result.plan_version,
        result.nodes_created,
        result.edges_created,
        gate_address,
        result.already_compiled,
    )

    return result, gate_address
