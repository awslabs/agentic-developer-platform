"""Derive the reviewable shape of a plan from the document that would register.

Issue #5331. `POST /flows/drafts/preview` already reported *which* nodes and edges
would be created. That is enough to confirm a document parsed; it is not enough to
take responsibility for what the engine will then do unattended. This module derives
the three facts a person needs before accepting that responsibility, and nothing
else:

* **What runs at the same time as what.** Execution order comes from the dependency
  edges, never from the order the author listed nodes in — the issue states outright
  that visual order alone is not an execution dependency. Waves with no dependency
  between them run in parallel, and a reviewer who cannot see that cannot reason
  about concurrency or blast radius.
* **What must be true before a node counts as done, and who is allowed to say so.**
  Three different mechanisms decide this in this codebase (the state machine for
  gates, the policy for evaluations, merged-PR evidence for stories), and none of
  them is visible in a list of node titles.
* **Whether the plan is bounded at all.** A document with no execution policy is
  *unbounded*, not restricted. That reads as the innocent default and is the single
  most consequential thing on a preview, so it is stated as its own field rather than
  left to be inferred from a `null`.

Every function here is pure over a `LoopProposal`. The preview route takes no
database session by design, and keeping this module free of I/O is what lets that
guarantee survive future edits to the derivation.

The derivation deliberately reuses `proposal._resolvable_adjacency` and
`registration._wave_of` rather than re-deriving either. A second opinion about what
the graph is, or about which wave a node belongs to, would let the preview disagree
with the validator that accepted the document and with the transform that inserted
its gates — and the preview's whole purpose is to show what those two will do.
"""

from __future__ import annotations

from collections import deque
from enum import StrEnum

from src.orchestration.execution_policy import AcceptanceMode, ExecutionPolicy
from src.orchestration.models import NodeKind
from src.orchestration.proposal import LoopProposal, _resolvable_adjacency
from src.orchestration.registration import _wave_of

__all__ = [
    "ConclusionAuthority",
    "DerivedNode",
    "DerivedWave",
    "conclusion_authority",
    "derive_nodes",
    "derive_waves",
    "wave_label",
]


class ConclusionAuthority(StrEnum):
    """Who or what may move a node to `PASSED`.

    Three values because this codebase has exactly three mechanisms, and collapsing
    them would hide the distinction that matters most to a reviewer: whether a step
    comes back to them or completes without them.

    Spelled as a closed enum rather than free text so a client can branch on it, and
    so a node kind added later without a decision here surfaces as
    `UNDETERMINED` instead of being silently labelled human-supervised — the
    direction of that default matters, because "a person checks this" is the
    reassuring answer and must never be the one we guess.
    """

    # A human decision point. Out of `AWAITING_GATE` the state machine admits only
    # `PASSED` and `REJECTED_AT_GATE`, both restricted to `ActorKind.HUMAN`, so this
    # is structural rather than policy-dependent: no policy can grant it away.
    HUMAN_DECISION = "human_decision"
    # An evaluation the policy marked for machine acceptance. Requires an explicit
    # `evaluation_acceptance[address] = machine`; absence means a human concludes it.
    MACHINE_EVALUATION = "machine_evaluation"
    # A story, which passes on evidence of a merged pull request in scope — not on an
    # agent's own report that it finished.
    MERGED_PULL_REQUEST = "merged_pull_request"
    # A kind this module has no rule for. Reported rather than assumed.
    UNDETERMINED = "undetermined"


class DerivedNode:
    """One node with the facts the document does not state on its face.

    A plain object rather than a pydantic model: this is an internal derivation
    result, and the route owns the wire shape. Keeping the two separate means the
    response model can carry field docstrings aimed at an API reader while this stays
    aimed at the person maintaining the derivation.
    """

    __slots__ = ("address", "concluded_by", "depends_on", "epic_ref", "wave_ref")

    def __init__(
        self,
        *,
        address: str,
        epic_ref: str,
        wave_ref: str,
        concluded_by: ConclusionAuthority,
        depends_on: list[str],
    ) -> None:
        self.address = address
        self.epic_ref = epic_ref
        self.wave_ref = wave_ref
        self.concluded_by = concluded_by
        self.depends_on = depends_on


class DerivedWave:
    """A wave, its members, and where it sits in the real execution order."""

    __slots__ = ("epic_ref", "node_addresses", "depends_on", "stage", "wave_ref", "title", "description")

    def __init__(
        self,
        *,
        epic_ref: str,
        wave_ref: str,
        stage: int | None,
        node_addresses: list[str],
        depends_on: list[str],
        title: str | None = None,
        description: str | None = None,
    ) -> None:
        self.epic_ref = epic_ref
        self.wave_ref = wave_ref
        self.stage = stage
        self.title = title
        self.description = description
        self.node_addresses = node_addresses
        self.depends_on = depends_on


def wave_label(epic_ref: str, wave_ref: str) -> str:
    """The `epic/wave` label a reviewer sees for a wave.

    Two segments, not the full four-segment node address: a wave is not a node (the
    schema deliberately has no wave rows), so there is no node address to show. The
    epic is included because wave refs are only unique within their epic — two epics
    may each have a `wave-1`, and a bare `wave-1` in a preview would merge them.
    """
    return f"{epic_ref}/{wave_ref}"


def conclusion_authority(kind: str, address: str, policy: ExecutionPolicy | None) -> ConclusionAuthority:
    """Who may conclude this node, given the policy the plan proposes.

    Reads the *proposed* policy on purpose. The reviewer is deciding whether to grant
    that policy, so the question they are answering is "if I accept this, what stops
    coming back to me?" — which is a fact about the policy in front of them, not about
    whatever is in force now (nothing is, for a draft).

    A gate is resolved before the policy is consulted at all, because a gate's human
    restriction lives in the state machine and cannot be delegated by any policy. An
    evaluation defaults to a human when the policy is silent, absent, or names a mode
    that is not `MACHINE`, matching `authorize_action`'s refusal for anything other
    than an explicit machine grant.
    """
    if kind == NodeKind.GATE.value:
        return ConclusionAuthority.HUMAN_DECISION

    if kind == NodeKind.STORY.value:
        return ConclusionAuthority.MERGED_PULL_REQUEST

    if kind == NodeKind.EVAL.value:
        if policy is None:
            # No policy means no machine grant for anything. An evaluation under a
            # policyless plan is concluded by a person, even though the plan's
            # *execution* is otherwise unbounded — those are separate facts and this
            # is the one this function answers.
            return ConclusionAuthority.HUMAN_DECISION
        mode = policy.evaluation_acceptance.get(address)
        return ConclusionAuthority.MACHINE_EVALUATION if mode is AcceptanceMode.MACHINE else ConclusionAuthority.HUMAN_DECISION

    return ConclusionAuthority.UNDETERMINED


def derive_nodes(proposal: LoopProposal) -> dict[str, DerivedNode]:
    """Derive per-node facts, keyed by address.

    Returns a mapping rather than a list so the route can zip it against the node
    order it already ships without a quadratic lookup, and so a node the derivation
    could not place is a missing key the caller must handle rather than a silently
    shortened list.

    `depends_on` lists a node's *direct* predecessors only, sorted. Direct rather than
    transitive because a reviewer reading one node wants to know what immediately
    holds it up; the transitive picture is what the wave staging conveys.
    """
    policy = _reviewable_policy(proposal)
    adjacency = _resolvable_adjacency(proposal)

    predecessors: dict[str, list[str]] = {address: [] for address in adjacency}
    for source, successors in adjacency.items():
        for successor in successors:
            predecessors[successor].append(source)

    derived: dict[str, DerivedNode] = {}
    for node in proposal.nodes:
        wave = _wave_of(node.address)
        # A malformed address is rule 1's violation, already reported by
        # `validate_proposal` on the same request. Empty refs here keep the preview
        # renderable for the author who has to fix it, rather than raising and
        # replacing their list of violations with a 500.
        epic_ref, wave_ref = wave if wave is not None else ("", "")
        derived[node.address] = DerivedNode(
            address=node.address,
            epic_ref=epic_ref,
            wave_ref=wave_ref,
            concluded_by=conclusion_authority(node.kind, node.address, policy),
            depends_on=sorted(predecessors.get(node.address, ())),
        )
    return derived


def derive_waves(proposal: LoopProposal) -> list[DerivedWave]:
    """Group nodes into waves and stage the waves by their real dependencies.

    `stage` is the answer to "what runs at the same time as what": every wave at the
    same stage has no dependency path to any other wave at that stage, so the engine
    may run them concurrently. It is a longest-path depth over the *wave* graph, not a
    position in some arbitrary linearization — a breadth-first rank would put a wave
    one step after its earliest predecessor even when a later predecessor still holds
    it, which would show work as parallel that cannot be.

    The wave graph is induced from node edges that cross a wave boundary. Note it can
    contain a cycle even when the node graph cannot: `w1/a -> w2/b` together with
    `w2/c -> w1/d` is perfectly acyclic over nodes while making the two waves
    mutually dependent. Those waves get `stage = None` rather than an invented
    number, because their relative order genuinely is not determined by the document
    and printing a number would assert an order the engine will not honour.

    Ordering of the returned list is `(stage, first appearance)`, with unstaged waves
    last, so the output is deterministic and reads top-to-bottom as execution
    proceeds. First appearance rather than a sort on the wave ref, matching
    `registration._waves_in_order` — `wave-10` sorts before `wave-2` as text, and a
    preview that reordered an author's waves alphabetically would misreport the very
    thing this function exists to report.
    """
    members: dict[str, list[str]] = {}
    labels_in_order: list[str] = []
    label_parts: dict[str, tuple[str, str]] = {}

    for node in proposal.nodes:
        wave = _wave_of(node.address)
        if wave is None:
            continue  # Malformed address: rule 1 reports it; it has no wave to group by.
        label = wave_label(*wave)
        if label not in members:
            members[label] = []
            labels_in_order.append(label)
            label_parts[label] = wave
        members[label].append(node.address)

    wave_edges = _wave_dependency_edges(proposal, label_parts)
    stages = _longest_path_stages(labels_in_order, wave_edges)
    metadata = {wave_label(item.epic_ref, item.wave_ref): item for item in proposal.wave_metadata}

    waves = [
        DerivedWave(
            epic_ref=label_parts[label][0],
            wave_ref=label_parts[label][1],
            title=metadata[label].title if label in metadata else None,
            description=metadata[label].description if label in metadata else None,
            stage=stages.get(label),
            node_addresses=members[label],
            depends_on=sorted(predecessor for predecessor, successor in wave_edges if successor == label),
        )
        for label in labels_in_order
    ]

    # `stage is None` sorts last: `float("inf")` rather than a sentinel int so a very
    # deep plan cannot collide with it.
    return sorted(
        waves,
        key=lambda wave: (
            float("inf") if wave.stage is None else wave.stage,
            labels_in_order.index(wave_label(wave.epic_ref, wave.wave_ref)),
        ),
    )


def _reviewable_policy(proposal: LoopProposal) -> ExecutionPolicy | None:
    """The policy whose bounds this document proposes, from whichever field holds it.

    Both fields are consulted because `transform_for_registration` moves the value
    between them: a submitted document declares `execution_policy` and the transform
    demotes it to `proposed_execution_policy` so a draft can carry it inertly.
    `LoopProposal` refuses a document with both set, so there is no ambiguity to
    resolve. Mirrors `draft_routes._policy_summary_of`'s reasoning for the same
    reason — a derivation that read only one field would report every pre-transform
    document as policyless, i.e. as unbounded, which is the one error here that could
    get a grant approved unreviewed.
    """
    return proposal.proposed_execution_policy or proposal.execution_policy


def _wave_dependency_edges(proposal: LoopProposal, label_parts: dict[str, tuple[str, str]]) -> set[tuple[str, str]]:
    """Wave-level dependencies induced by node edges that cross a wave boundary.

    A set, because many node edges typically induce the same wave edge and a reviewer
    needs the wave relationship once, not once per story. Built from
    `_resolvable_adjacency` so an edge with a dangling endpoint — already rule 3's
    violation — cannot invent a dependency on a wave that does not exist.
    """
    adjacency = _resolvable_adjacency(proposal)
    edges: set[tuple[str, str]] = set()

    for source, successors in adjacency.items():
        source_wave = _wave_of(source)
        if source_wave is None:
            continue
        source_label = wave_label(*source_wave)
        for successor in successors:
            successor_wave = _wave_of(successor)
            if successor_wave is None:
                continue
            successor_label = wave_label(*successor_wave)
            if source_label != successor_label and source_label in label_parts and successor_label in label_parts:
                edges.add((source_label, successor_label))

    return edges


def _longest_path_stages(labels: list[str], edges: set[tuple[str, str]]) -> dict[str, int]:
    """Longest-path depth per wave; waves in a cycle are absent from the result.

    Kahn's algorithm carrying a running maximum instead of a simple counter, so a
    wave is staged only once every predecessor has been staged and therefore sits
    strictly after the *latest* thing that holds it. A wave left unprocessed when the
    queue drains is in a cycle; it is omitted rather than defaulted, and the caller
    reports the absence as an undetermined order.
    """
    successors: dict[str, list[str]] = {label: [] for label in labels}
    indegree = dict.fromkeys(labels, 0)
    for source, successor in edges:
        successors[source].append(successor)
        indegree[successor] += 1

    stages = {label: 0 for label in labels if indegree[label] == 0}
    ready = deque(label for label in labels if indegree[label] == 0)

    while ready:
        label = ready.popleft()
        for successor in successors[label]:
            stages[successor] = max(stages.get(successor, 0), stages[label] + 1)
            indegree[successor] -= 1
            if indegree[successor] == 0:
                ready.append(successor)

    # Only fully-resolved waves are returned. A wave still carrying indegree is in a
    # cycle, and any stage accumulated for it above is a partial maximum that would
    # understate its position.
    return {label: stage for label, stage in stages.items() if indegree[label] == 0}
