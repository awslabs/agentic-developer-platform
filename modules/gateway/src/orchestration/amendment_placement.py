"""Resolve a placement choice into explicit nodes and dependency edges.

Issue #5329 (EPIC #4191), building on #4529's pending-amendment store.

--------------------------------------------------------------------------------
The hole this closes
--------------------------------------------------------------------------------

#4529 gave discovered work somewhere to wait: a `PENDING` row holding a complete
`LoopProposal`, inert until a named human accepts it. What it did not give anyone is a
way to *author* that document from an intent like "add a cleanup checker and a cleanup
validation after U1, feeding Wave 1's acceptance". The only input the store takes is a
finished replacement plan, so a human or agent proposing an addition had to hand-write
the whole graph — including every edge they did not mean to change.

This module is the missing translation. It takes the plan in force plus a description of
what to add and where, and returns the complete proposal document with every incoming and
outgoing edge spelled out, plus the reason each edge exists.

--------------------------------------------------------------------------------
Why placement has to become edges, and cannot stay a wave name
--------------------------------------------------------------------------------

The engine releases work from edges. `tick` promotes `pending -> ready` when a node's
predecessors are satisfied, and a node with no predecessors is eligible immediately.
Wave membership is *derived* from the address (`preview.derive_waves` groups by segment);
no scheduler reads it. So "put it in Wave 1" is not a placement — it is a label, and a
node carrying that label with no edges is a node that runs right now.

That is the failure this module exists to prevent, and it is why every placement kind
below resolves to a concrete edge set rather than to a wave assignment. A placement whose
edges cannot be determined is returned as a *blocker*, never as a document with the
question quietly answered one way.

--------------------------------------------------------------------------------
Why resolution is strictly additive
--------------------------------------------------------------------------------

No placement kind here removes an edge from the plan in force. `insert_before` does not
re-parent the anchor's predecessors onto the new work and drop the originals; it keeps
them and adds. `insert_after` does not move the anchor's successors behind the new work
by deleting `anchor -> successor`; it keeps that edge and adds `new -> successor`.

Two reasons, and the second is the load-bearing one:

* Dropping an edge is how inserted work *releases* downstream work early. An
  `insert_after` that re-parented a successor would, between the delete and the add,
  describe a graph where the successor waits for the new node only — and if the new node
  is already satisfiable, the successor runs before the anchor it was always waiting for.
* `amend_plan` refuses a changed *incoming* edge set on any node past `pending`/`ready`.
  Additive resolution means the only nodes whose incoming set changes are the ones the
  proposal deliberately makes wait, so that refusal fires on exactly the cases a human
  needs to be told about, rather than on every insertion.

Superseding a node is a different operation with a different authority (`amend_plan` does
it by absence from the document) and it is not reachable from here: `resolve_placement`
carries every in-force node through to the resolved document unchanged.

--------------------------------------------------------------------------------
What this module does NOT do
--------------------------------------------------------------------------------

It does not validate the result. `proposal.validate_proposal` is authoritative and owns
cycle detection, eval cardinality, kind checks and same-issue ordering; running a second
opinion here would be free to accept a document the acceptance path refuses. It does not
look at execution state either — "this anchor is already running" is
`amendment_impact`'s question, because it needs the graph rows and this needs only the
two documents. And it writes nothing: the caller decides whether the resolved document
becomes a draft.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .address import ADDRESS_PATTERN, split_address
from .proposal import (
    DESCRIPTION_MAX_LEN,
    LoopProposal,
    NamedInputPrerequisite,
    NodeLineage,
    ProposedEdge,
    ProposedNode,
    Violation,
    parse_node_lineage,
)

__all__ = [
    "AddedEdge",
    "PlacementKind",
    "PlacementRequest",
    "PlacementResolution",
    "ProposedPlacement",
    "ProposedWork",
    "resolve_placement",
]

#: Segment grammar for a proposed node's own address segment. The same shape
#: `address.ADDRESS_PATTERN` allows per segment, applied to one segment, so a caller
#: cannot smuggle a `/` and turn one segment into two — which would let a request place
#: work at an address in a different wave than the one the placement names.
_SEGMENT = r"^[A-Za-z0-9][A-Za-z0-9._-]*$"


class PlacementKind(StrEnum):
    """Where the proposed work goes, relative to what is already on the graph.

    Five members because these are the five relationships that resolve to a *determined*
    edge set given the plan in force. Anything else a caller wants is expressible as
    explicit prerequisites and successors on the work items themselves, which is why
    there is no "custom" member: a member whose resolution rule is "whatever the caller
    said" would be a second, unreviewed path to the edge list.
    """

    #: The chain runs between the anchor's existing predecessors and the anchor. The
    #: anchor keeps every prerequisite it had and gains one.
    INSERT_BEFORE = "insert_before"
    #: The chain runs after the anchor. The anchor's existing successors keep waiting for
    #: the anchor and additionally wait for the chain's last node.
    INSERT_AFTER = "insert_after"
    #: The chain runs concurrently with the anchor, from the anchor's own predecessors,
    #: and rejoins at explicitly named nodes. `rejoin_at` is required — see
    #: `_resolve_parallel_branch`.
    PARALLEL_BRANCH = "parallel_branch"
    #: The chain joins an existing wave. Its prerequisites are explicit; its default
    #: successor is that wave's single eval node, which is what "counts towards this
    #: wave's acceptance" means in edge terms.
    EXISTING_WAVE = "existing_wave"
    #: The chain forms a new wave. Both ends are explicit: a new wave has no anchor to
    #: derive an order from.
    NEW_WAVE = "new_wave"


class ProposedWork(BaseModel):
    """One node the proposal adds, before its address has been resolved.

    Carries a bare `ref` rather than a full address because the address's first three
    segments are a *consequence* of the placement — deriving them here is what stops a
    caller from naming Wave 1 in the placement and Wave 4 in the address.

    `kind` mirrors `ProposedNode.kind` in being a plain string: an unknown kind must
    reach `validate_proposal` and be reported against its node, not fail this model and
    take the whole request's diagnostics with it.
    """

    model_config = ConfigDict(extra="forbid")

    #: The final address segment, unique within the resolved wave.
    ref: str = Field(min_length=1, max_length=128, pattern=_SEGMENT)
    kind: str = Field(min_length=1, max_length=32)
    title: str = Field(min_length=1, max_length=512)
    #: The GitHub issue this node materialises as. An existing follow-up issue is
    #: *adopted* by naming it here; nothing is created by resolving a placement. Two
    #: nodes sharing an issue are legitimate and `validate_proposal`'s rule 6 requires
    #: them to be ordered relative to each other.
    issue_ref: str | None = Field(default=None, max_length=128)
    lineage: NodeLineage | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    _parse_lineage = field_validator("lineage", mode="before")(parse_node_lineage)
    #: The evaluation contract for an eval node, passed through verbatim. Shaped and
    #: validated by `evaluation_contract`, never interpreted here.
    evaluation: dict | None = None
    proposal_identity: str | None = Field(default=None, min_length=1, max_length=255)
    repository_identity: str | None = Field(default=None, min_length=1, max_length=255)
    provider_repository_id: int | None = Field(default=None, gt=0)
    execution_owner: str | None = Field(default=None, min_length=1, max_length=255)
    execution_adapter: str | None = Field(default=None, min_length=1, max_length=128)
    criteria: list[str] = Field(default_factory=list, max_length=64)
    required_inputs: list[NamedInputPrerequisite] = Field(default_factory=list, max_length=32)
    evidence_expectations: list[str] = Field(default_factory=list, max_length=64)
    #: Additional in-force or sibling addresses that must precede this item, beyond
    #: whatever the placement derives. This is how a validation step declares that it
    #: waits on authorized live inputs as well as on the implementation it validates.
    requires: list[str] = Field(default_factory=list, max_length=32)
    #: Additional addresses this item must precede, beyond whatever the placement
    #: derives. Named so a proposal can state "and Wave 2's gate must now wait for
    #: this" without needing a second placement.
    precedes: list[str] = Field(default_factory=list, max_length=32)


class ProposedPlacement(BaseModel):
    """Where a chain of proposed work attaches to the plan in force."""

    model_config = ConfigDict(extra="forbid")

    kind: PlacementKind
    #: The existing node the placement is relative to. Required by the three anchored
    #: kinds, refused by the two wave kinds — an anchor they would ignore is a caller
    #: believing something about the result that is not true.
    anchor: str | None = Field(default=None, max_length=512)
    #: Where a parallel branch rejoins the main line. Required by `PARALLEL_BRANCH`.
    rejoin_at: list[str] = Field(default_factory=list, max_length=32)
    #: The epic and wave segments for the two wave kinds. Ignored by the anchored kinds,
    #: which take both from the anchor.
    epic_ref: str | None = Field(default=None, max_length=128, pattern=_SEGMENT)
    wave_ref: str | None = Field(default=None, max_length=128, pattern=_SEGMENT)


class PlacementRequest(BaseModel):
    """A complete proposal: what to add, and where it goes.

    One placement per request, deliberately. Work items are chained in declaration order
    within the placement, which covers "implement the checker, then validate with it"
    without a second placement; two genuinely unrelated additions are two proposals, and
    keeping them separate is what lets a reviewer reject one of them.
    """

    model_config = ConfigDict(extra="forbid")

    work: list[ProposedWork] = Field(min_length=1, max_length=32)
    placement: ProposedPlacement
    #: Carried onto the resolved document's `description`, so the reviewer sees the
    #: stated purpose of the addition next to the graph it produces.
    purpose: str | None = Field(default=None, max_length=DESCRIPTION_MAX_LEN)


@dataclass(frozen=True)
class AddedEdge:
    """One dependency edge the resolution added, and why it exists.

    The reason is the point. A reviewer looking at a preview can see *that* Wave 2's gate
    now waits for a new validation node; what they cannot see from the graph is whether
    that was the proposal's intent or a side effect of the placement rule. Carrying the
    rationale from the resolver — which is the only code that knows which rule produced
    the edge — is how the preview explains itself without guessing.
    """

    from_address: str
    to_address: str
    reason: str


@dataclass(frozen=True)
class PlacementResolution:
    """The resolved document, what it added, and why it cannot be resolved.

    `document` is None exactly when `blockers` is non-empty. A partially resolved
    document is deliberately not offered: the caller's next step is either to preview a
    real proposal or to show the reviewer what is unresolved, and a document with an
    ambiguous placement guessed one way is the input to an approval nobody reviewed.
    """

    document: LoopProposal | None
    added_nodes: list[str] = field(default_factory=list)
    added_edges: list[AddedEdge] = field(default_factory=list)
    blockers: list[Violation] = field(default_factory=list)

    @property
    def resolved(self) -> bool:
        return self.document is not None and not self.blockers


def _blocker(rule: str, message: str, where: str | None = None) -> Violation:
    """A placement problem, in `validate_proposal`'s shape.

    Reusing `Violation` rather than introducing a parallel type so one renderer serves
    both: to the reviewer, "this placement is ambiguous" and "this document has a cycle"
    are the same class of answer — a named reason acceptance is unavailable — and a
    second shape would mean the API and CLI each decide which list to show first.
    """
    return Violation(rule=rule, message=message, where=where)


def _predecessors_of(base: LoopProposal, address: str) -> list[str]:
    """The in-force document's direct predecessors of `address`, sorted."""
    return sorted({edge.from_address for edge in base.edges if edge.to_address == address})


def _successors_of(base: LoopProposal, address: str) -> list[str]:
    """The in-force document's direct successors of `address`, sorted."""
    return sorted({edge.to_address for edge in base.edges if edge.from_address == address})


def _wave_eval_of(base: LoopProposal, epic_ref: str, wave_ref: str) -> str | None:
    """The single eval node of one wave, or None if it has none.

    None is also returned when a wave somehow has two, because the caller's use for this
    is "what concludes this wave" and that question has no answer in that case. Rule 4
    (`_check_wave_evals`) reports the cardinality itself; guessing one of the two here
    would attach the proposal's work to whichever happened to be declared first.
    """
    found = [
        node.address
        for node in base.nodes
        if node.kind == "eval" and ADDRESS_PATTERN.match(node.address) and split_address(node.address)[1:3] == (epic_ref, wave_ref)
    ]
    return found[0] if len(found) == 1 else None


def resolve_placement(*, base: LoopProposal, request: PlacementRequest) -> PlacementResolution:
    """Turn a placement choice into a complete proposal document.

    Args:
        base: The plan currently in force, parsed. Every one of its nodes and edges is
            carried through unchanged — this function only ever adds.
        request: What to add and where.

    Returns:
        A `PlacementResolution`. When `blockers` is non-empty, `document` is None and the
        blockers name what a human must decide before a proposal exists at all.
    """
    blockers: list[Violation] = []
    placement = request.placement

    declared = {node.address for node in base.nodes}

    # --- Shape the placement's own arguments before using any of them -----------
    # Checked first and as a group, so a request with two problems reports both rather
    # than reporting the first and then failing on the second's absence.
    anchored = placement.kind in {PlacementKind.INSERT_BEFORE, PlacementKind.INSERT_AFTER, PlacementKind.PARALLEL_BRANCH}
    if anchored:
        if not placement.anchor:
            blockers.append(
                _blocker(
                    "placement_anchor_required",
                    f"{placement.kind.value} is relative to an existing node, so it requires an anchor address",
                )
            )
        elif placement.anchor not in declared:
            # Deliberately not "did you mean": an anchor that does not exist in the plan
            # in force is as likely to be a stale address from an older version as a
            # typo, and those need different answers from the human.
            blockers.append(
                _blocker(
                    "placement_anchor_unknown",
                    f"anchor {placement.anchor!r} is not a node in the plan currently in force",
                    placement.anchor,
                )
            )
        if placement.epic_ref or placement.wave_ref:
            blockers.append(
                _blocker(
                    "placement_wave_not_applicable",
                    f"{placement.kind.value} takes its epic and wave from the anchor; naming them separately would let the two disagree",
                )
            )
    else:
        if placement.anchor:
            blockers.append(
                _blocker(
                    "placement_anchor_not_applicable",
                    f"{placement.kind.value} places work by wave, not relative to a node; an anchor here would be ignored",
                    placement.anchor,
                )
            )
        if not placement.epic_ref or not placement.wave_ref:
            blockers.append(
                _blocker(
                    "placement_wave_required",
                    f"{placement.kind.value} requires both an epic and a wave to address the work into",
                )
            )

    if placement.kind is PlacementKind.PARALLEL_BRANCH and not placement.rejoin_at:
        # The ambiguity the issue calls out by name. A branch with no rejoin is not a
        # slightly-underspecified placement, it is a different plan: nothing downstream
        # waits for it, so its result cannot gate anything and its failure stops nothing.
        blockers.append(
            _blocker(
                "placement_rejoin_required",
                "a parallel branch must say where it rejoins: without a rejoin edge nothing downstream waits for it, "
                "so neither its result nor its failure can affect the plan",
            )
        )
    if placement.rejoin_at and placement.kind is not PlacementKind.PARALLEL_BRANCH:
        blockers.append(
            _blocker(
                "placement_rejoin_not_applicable",
                f"{placement.kind.value} derives its own successors; use each work item's `precedes` to add more",
            )
        )
    for target in placement.rejoin_at:
        if target not in declared:
            blockers.append(_blocker("placement_rejoin_unknown", f"rejoin target {target!r} is not a node in the plan currently in force", target))

    # --- Derive each new node's address ----------------------------------------
    if anchored and placement.anchor and placement.anchor in declared:
        _, epic_ref, wave_ref, _ = split_address(placement.anchor)
    else:
        epic_ref, wave_ref = placement.epic_ref or "", placement.wave_ref or ""

    chain: list[str] = []
    if epic_ref and wave_ref:
        for item in request.work:
            address = f"{base.flow_slug}/{epic_ref}/{wave_ref}/{item.ref}"
            if address in declared:
                # Reusing an existing address would silently *redefine* that node rather
                # than add one, and `amend_plan` keeps its row, state and history. That
                # is a legitimate operation with different reviewable consequences, so it
                # is not something a placement is allowed to do by collision.
                blockers.append(
                    _blocker(
                        "placement_address_taken",
                        f"{address!r} already exists in the plan in force; adding work cannot redefine an existing node",
                        address,
                    )
                )
            elif address in chain:
                blockers.append(_blocker("placement_duplicate_work", f"two proposed work items resolve to the same address {address!r}", address))
            chain.append(address)

    if placement.kind is PlacementKind.NEW_WAVE and epic_ref and wave_ref:
        occupied = [
            node.address for node in base.nodes if ADDRESS_PATTERN.match(node.address) and split_address(node.address)[1:3] == (epic_ref, wave_ref)
        ]
        if occupied:
            blockers.append(
                _blocker(
                    "placement_wave_exists",
                    f"{epic_ref}/{wave_ref} already has {len(occupied)} node(s); use existing_wave to add to it",
                    f"{epic_ref}/{wave_ref}",
                )
            )

    # --- Check the explicit edges the work items name ---------------------------
    # Only when the chain was actually derived. An unresolved epic/wave — a missing or
    # unknown anchor — leaves `chain` empty, and pairing it against the work items would
    # raise instead of reporting the anchor blocker that is the real answer. The caller
    # gets the placement problem it can act on; prerequisites are re-checked on the next
    # attempt once the addresses exist.
    #
    # Resolvable against the in-force plan or against a sibling in this same chain: a
    # validation item legitimately requires the implementation item proposed beside it.
    resolvable = declared | set(chain)
    for item, address in zip(request.work, chain, strict=False):
        for required in item.requires:
            if required not in resolvable:
                blockers.append(
                    _blocker("placement_prerequisite_unknown", f"{address!r} requires {required!r}, which is not a node in this plan", required)
                )
        for preceded in item.precedes:
            if preceded not in resolvable:
                blockers.append(
                    _blocker("placement_successor_unknown", f"{address!r} precedes {preceded!r}, which is not a node in this plan", preceded)
                )

    if blockers:
        return PlacementResolution(document=None, blockers=blockers)

    # --- Resolve the placement to edges ----------------------------------------
    added = _resolve_edges(base=base, placement=placement, chain=chain, epic_ref=epic_ref, wave_ref=wave_ref)
    for item, address in zip(request.work, chain, strict=True):
        added.extend(
            AddedEdge(from_address=required, to_address=address, reason="declared prerequisite of the proposed work") for required in item.requires
        )
        added.extend(
            AddedEdge(from_address=address, to_address=preceded, reason="declared successor of the proposed work") for preceded in item.precedes
        )

    # Deduplicate on the pair, keeping the first reason. A placement rule and an explicit
    # `requires` can legitimately derive the same edge, and `upsert_edges` would accept
    # the duplicate — but the preview would then report one dependency twice, which reads
    # as two separate reasons a node is blocked.
    seen: set[tuple[str, str]] = set()
    unique: list[AddedEdge] = []
    for edge in added:
        key = (edge.from_address, edge.to_address)
        if key in seen:
            continue
        seen.add(key)
        unique.append(edge)

    document = base.model_copy(
        update={
            "nodes": [
                *base.nodes,
                *[
                    ProposedNode(
                        address=address,
                        kind=item.kind,
                        title=item.title,
                        issue_ref=item.issue_ref,
                        lineage=item.lineage,
                        labels=item.labels,
                        evaluation=item.evaluation,
                        proposal_identity=item.proposal_identity or f"{base.flow_slug}:{address}",
                        repository_identity=item.repository_identity,
                        provider_repository_id=item.provider_repository_id,
                        execution_owner=item.execution_owner,
                        execution_adapter=item.execution_adapter,
                        criteria=item.criteria,
                        required_inputs=[entry.model_dump(mode="json") for entry in item.required_inputs],
                        evidence_expectations=item.evidence_expectations,
                    )
                    for item, address in zip(request.work, chain, strict=True)
                ],
            ],
            "edges": [*base.edges, *[ProposedEdge(from_address=edge.from_address, to_address=edge.to_address) for edge in unique]],
            # The stated purpose replaces the in-force document's description, which
            # described the *original* plan. `description` is excluded from `plan_hash`
            # (see `LoopProposal`), so this cannot make two otherwise-identical proposals
            # look like different plans.
            "description": request.purpose if request.purpose is not None else base.description,
        }
    )

    return PlacementResolution(document=document, added_nodes=chain, added_edges=unique, blockers=[])


def _resolve_edges(
    *,
    base: LoopProposal,
    placement: ProposedPlacement,
    chain: list[str],
    epic_ref: str,
    wave_ref: str,
) -> list[AddedEdge]:
    """The edges one placement kind implies. Additive by construction — see the module docstring."""
    edges: list[AddedEdge] = []

    # Every placement chains its own work in declaration order. This is what makes
    # "implement the checker, then validate with it" one placement: the order the caller
    # listed the items in is the order they run, and nothing else in the document has to
    # say so.
    for earlier, later in zip(chain, chain[1:], strict=False):
        edges.append(AddedEdge(from_address=earlier, to_address=later, reason="proposed work runs in the order it was declared"))

    head, tail = chain[0], chain[-1]

    if placement.kind is PlacementKind.INSERT_BEFORE:
        anchor = placement.anchor or ""
        # The chain takes the anchor's predecessors so it sits *between* them and the
        # anchor rather than starting immediately. The anchor keeps those predecessors:
        # see the module docstring on why nothing is re-parented.
        edges.extend(
            AddedEdge(from_address=predecessor, to_address=head, reason=f"inherited from {anchor}, which the proposed work now precedes")
            for predecessor in _predecessors_of(base, anchor)
        )
        edges.append(AddedEdge(from_address=tail, to_address=anchor, reason=f"{anchor} must now wait for the proposed work"))

    elif placement.kind is PlacementKind.INSERT_AFTER:
        anchor = placement.anchor or ""
        edges.append(AddedEdge(from_address=anchor, to_address=head, reason=f"the proposed work runs after {anchor}"))
        # The anchor's successors additionally wait for the chain. Their existing
        # `anchor -> successor` edges stay, so this narrows what can run and never widens
        # it — the direction a reviewer needs guaranteed.
        edges.extend(
            AddedEdge(from_address=tail, to_address=successor, reason=f"already waited for {anchor}, so it must now also wait for the proposed work")
            for successor in _successors_of(base, anchor)
        )

    elif placement.kind is PlacementKind.PARALLEL_BRANCH:
        anchor = placement.anchor or ""
        # Same predecessors as the anchor, and no edge to the anchor: that is precisely
        # what makes the two concurrent rather than ordered.
        edges.extend(
            AddedEdge(from_address=predecessor, to_address=head, reason=f"the branch starts where {anchor} starts, so the two run concurrently")
            for predecessor in _predecessors_of(base, anchor)
        )
        edges.extend(
            AddedEdge(from_address=tail, to_address=target, reason="the parallel branch rejoins here") for target in sorted(set(placement.rejoin_at))
        )

    elif placement.kind is PlacementKind.EXISTING_WAVE:
        # No derived prerequisites: a wave name says nothing about order, and inventing
        # one would be the "wave membership is a placement" mistake. Prerequisites come
        # from each item's `requires`, and a chain with none is eligible immediately —
        # which the impact report states plainly rather than this module hiding.
        wave_eval = _wave_eval_of(base, epic_ref, wave_ref)
        if wave_eval is not None and wave_eval not in chain:
            edges.append(
                AddedEdge(
                    from_address=tail,
                    to_address=wave_eval,
                    reason=f"{epic_ref}/{wave_ref} concludes at {wave_eval}, which must now cover the proposed work",
                )
            )

    # NEW_WAVE derives nothing at all. A wave with no existing members has no anchor to
    # read an order from and no eval to conclude at, so both ends are the caller's to
    # state through `requires`/`precedes`. Deriving "after the last wave" would be
    # guessing from declaration order, which is not a dependency.

    return edges
