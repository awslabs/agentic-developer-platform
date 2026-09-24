"""The loop proposal: schema-as-code for a plan an agent wants to become state.

Issue #4199 (EPIC #4191, intent #4120). The engine's tables are unreachable from
agent pods by design — no DB credential exists there. So an authoring agent
cannot write the plan it authored; it submits a **document** and the engine
decides whether that document becomes state.

This module is that document's definition, and the definition is the contract.
Two callers import from here:

  - `.github/scripts/validate_loop_proposal.py` — **advisory**. Runs wherever the
    author runs. An author can skip it, and nothing structural stops them.
  - `compile.compile_proposal` — **authoritative**. Runs inside the transaction
    that creates nodes. It is the only code path that creates nodes, so it cannot
    be skipped.

The double validation is the whole point, and it only works because both callers
import the *same* `validate_proposal` from the *same* module. That is what makes
"advisory and authoritative agree" structural rather than aspirational — there is
one definition, not two that are supposed to match. A second copy of these rules
in the CLI would be free to drift, and the drift would be invisible until a
document the CLI blessed was rejected at approval (or worse, the reverse).

Violations are **returned, not raised**. The CLI's job is to show an author
everything wrong with their document in one pass, so a model that aborted on the
first bad address would make the tool useless — the author would fix one rule per
run. `compile_proposal` turns a non-empty list into a raise; that translation is
the compiler's decision, not this module's.

Vocabulary reuse (R-N2a): `NodeKind` and `NodeState` are imported from the store
and NOT redefined here. A second copy of either is a requirement violation, not a
style preference — the existing run-status sets drifted three ways inside a set
whose own comment claimed drift was impossible.

Container levels are the load-bearing exclusion. `NodeKind` contains story / eval
/ gate and deliberately not wave / epic / flow: containers are **derived** by
rolling up their member nodes, never rows. A proposal declaring `kind="wave"` is
trying to smuggle a container in as a node, which would create a second source of
truth for a value already implied by its children. Rule 2 rejects it.

Pydantic v2 (`pydantic==2.12.5`, pyproject.toml:12) with `ConfigDict` — precedent
`admin/identity/schemas.py:80`. Deliberately NOT the v2-deprecated `class Config`
still present at `shared/schemas/budget.py:51`; this story does not migrate those,
but it does not copy them either.
"""

from dataclasses import dataclass
from datetime import datetime
from heapq import heapify, heappop, heappush
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Imported, never redefined — see module docstring (R-N2a). The address grammar
# moved to `address.py` (#5128) so `execution_policy.py` can constrain its
# evaluation-address keys to the same pattern without the two modules importing
# each other; it is re-exported below so existing importers are unaffected.
from .address import ADDRESS_PATTERN, split_address
from .execution_policy import ExecutionPolicy
from .models import NodeKind
from .state import NodeState

__all__ = [
    "ADDRESS_PATTERN",
    "LoopProposal",
    "ProposedEdge",
    "ProposedNode",
    "Violation",
    "split_address",
    "validate_proposal",
]

# The executable node kinds, derived from the store's enum rather than listed.
# Hand-listing them here is exactly how the vocabulary would drift.
_EXECUTABLE_KINDS = frozenset(kind.value for kind in NodeKind)

# The kinds that dispatch to their issue and consume a worker. A `gate` is a
# human decision the tick presents; it performs no work on an issue, so it is
# absent. Derived from the enum for the same reason `_EXECUTABLE_KINDS` is.
_ISSUE_CONSUMING_KINDS = frozenset({NodeKind.STORY.value, NodeKind.EVAL.value})

# Container levels are named explicitly ONLY so the violation message can say
# "containers are derived" instead of the unhelpful "not a valid kind". They are
# not a vocabulary — they are the rejection's explanation.
_CONTAINER_KINDS = frozenset({"wave", "epic", "flow"})

# DFS colours for cycle detection. Module-level constants rather than locals so
# the traversal reads as the standard three-colour algorithm without tripping
# ruff's N806 (non-lowercase local).
_WHITE = 0  # Unvisited
_GREY = 1  # On the current DFS path — a back-edge into grey is a cycle
_BLACK = 2  # Fully explored; cannot be part of a cycle reachable from here


@dataclass(frozen=True)
class Violation:
    """One thing wrong with a proposal.

    Carries a machine-readable `rule` alongside the human `message` so CI can
    assert on a specific rule without string-matching prose, and `where` so an
    author is pointed at the offending node or edge rather than told the document
    is bad.
    """

    rule: str
    message: str
    where: str | None = None

    def __str__(self) -> str:
        location = f" [{self.where}]" if self.where else ""
        return f"{self.rule}: {self.message}{location}"


class ProposedNode(BaseModel):
    """One executable node in a proposed plan.

    `kind` is a plain `str`, not `NodeKind`. That is deliberate: typing it as the
    enum would make pydantic reject `kind="wave"` during parsing with a generic
    validation error, and the whole document would fail to load. We want the
    *proposal* to parse so `validate_proposal` can report a precise, actionable
    violation for that node while still checking every other rule in the same
    pass. Rule 2 is where `kind` is enforced.
    """

    model_config = ConfigDict(extra="forbid")

    # The graph address, `flow/epic/wave/node`. Form is checked by rule 1 rather
    # than by a pattern constraint here, for the same reason `kind` is a str.
    address: str
    kind: str
    title: str = Field(min_length=1, max_length=512)
    # The GitHub issue this node materialises as. Optional: eval and gate nodes
    # frequently have no issue of their own.
    issue_ref: str | None = None
    # Stored in the accepted plan document; absence retains human mode.
    evaluation: dict | None = None


class ProposedEdge(BaseModel):
    """A dependency edge between two proposed nodes, by address.

    Addresses, not ids: the proposal is authored before any row exists, so there
    are no ids to reference yet. `compile_proposal` resolves these to node ids
    after insert.
    """

    model_config = ConfigDict(extra="forbid")

    from_address: str
    to_address: str


# --- The design loop's story (#4885) ---------------------------------------
# The canonical five AIDLC inception stages, in order, from
# `modules/agent-factory/rules/personas/aidlc.md`. Spelled here as the single
# server-side list a document is validated against: a typo that parsed would
# become a permanently unrenderable chip on the card, and nothing downstream
# could tell it from a real stage. Ordered because the strip renders in this
# order, and a card whose gates read out of sequence misrepresents the process.
DESIGN_STAGES: tuple[str, ...] = (
    "intent-capture",
    "reverse-engineering",
    "requirements-analysis",
    "delivery-planning",
    "loop-proposal",
)

# `skipped` and `not_reached` are DIFFERENT and must not be merged.
#
#   approved     — the gate ran and a human approved it.
#   open         — the gate is posted and a human is being waited on right now.
#   skipped      — scope decided this stage does not run at all (`poc` skips
#                  reverse-engineering). It is NOT pending work.
#   not_reached  — the stage will run, but the loop has not got there yet.
#
# Collapsing `skipped` into `not_reached` makes a stage that is never coming read
# as unfinished work, which is the exact confusion the strip exists to remove;
# collapsing it the other way claims a gate was answered when nobody looked at it.
DESIGN_STAGE_STATES: tuple[str, ...] = ("approved", "open", "skipped", "not_reached")

# Hard cap on the use-case description, enforced at write time. This string rides
# EVERY row of the flows list response, so an unbounded body would inflate the
# whole page for one flow's sake. Over-length is REJECTED, never truncated:
# silently cutting a sentence mid-word ships a description whose author cannot
# tell it was altered, and the fix (write a shorter one) belongs with them.
DESCRIPTION_MAX_LEN = 500


class DesignStage(BaseModel):
    """One AIDLC gate's outcome within a flow's design history.

    `name` and `state` are `Literal`s rather than plain `str`s — the opposite
    choice to `ProposedNode.kind` above, and deliberately so. `kind` stays a str
    because `validate_proposal` can report a precise violation for one bad node
    while still checking the rest of the document. There is no equivalent
    reporting pass for design history, and no execution depends on it, so the
    type IS the validation and an unknown stage name is a 422 at the boundary.
    """

    model_config = ConfigDict(extra="forbid")

    name: Literal[DESIGN_STAGES]  # type: ignore[valid-type]
    state: Literal[DESIGN_STAGE_STATES]  # type: ignore[valid-type]
    # Present ONLY for `approved`. An `approved_at` on an open or skipped stage
    # would be a timestamp for an approval that did not happen, and the card
    # renders it as one — so it is rejected rather than ignored.
    approved_at: datetime | None = None

    @model_validator(mode="after")
    def _approved_at_only_when_approved(self) -> "DesignStage":
        if self.state == "approved":
            if self.approved_at is None:
                raise ValueError(f"stage {self.name!r} is 'approved' but carries no approved_at")
        elif self.approved_at is not None:
            raise ValueError(f"stage {self.name!r} is {self.state!r}, which cannot carry an approved_at")
        return self


class DesignHistory(BaseModel):
    """The inception record for a flow: its scope and its five gates' outcomes.

    A whole-object model rather than a free-form dict, so an invalid history is
    refused at the API boundary instead of persisting as an unrenderable blob. A
    JSON column will accept anything; this is what makes it not.
    """

    model_config = ConfigDict(extra="forbid")

    # `auto` | `poc` | `workshop` (aidlc.md "Scope Modes"). Scope decides WHICH
    # stages run — never whether they gate — which is why `skipped` is a stage
    # state and not a property of the scope.
    scope: Literal["auto", "poc", "workshop"]
    stages: list[DesignStage] = Field(min_length=1, max_length=len(DESIGN_STAGES))

    @model_validator(mode="after")
    def _no_duplicate_stages(self) -> "DesignHistory":
        """One entry per stage. A duplicate makes "N of 5 approved" ambiguous.

        Two rows for `delivery-planning`, one approved and one open, describe
        contradictory states for the same gate and the strip would render
        whichever came last — an arbitrary answer to a question with a real one.
        """
        names = [stage.name for stage in self.stages]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"design history names the same stage more than once: {', '.join(duplicates)}")
        return self


class EpicDisplay(BaseModel):
    """Model-authored explanation of the capability, motivation and scope."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=3000)


class EpicMetadata(EpicDisplay):
    epic_ref: str = Field(min_length=1, max_length=128)


class WaveDisplay(BaseModel):
    """Model-authored display text, with no execution or identity fields."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, min_length=1, max_length=DESCRIPTION_MAX_LEN)


class WaveMetadata(WaveDisplay):
    """Display text for a derived wave, keyed by its stable epic and wave refs."""

    epic_ref: str = Field(min_length=1, max_length=128)
    wave_ref: str = Field(min_length=1, max_length=128)


class LoopProposal(BaseModel):
    """A complete plan an authoring agent proposes for approval.

    `org_id` is declared here but is NOT trusted as the tenant the plan lands in.
    `compile_proposal` takes the org from the server-resolved approval context and
    *compares*; a mismatch is rejected rather than silently re-homed. The field
    exists so the mismatch is detectable at all — a document with no declared org
    could be compiled into any tenant without contradiction.
    """

    model_config = ConfigDict(extra="forbid")

    # The flow segment of every node's address, and the flow this plan is for.
    flow_slug: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=512)
    # The tenant the author believes this plan belongs to. Checked, not trusted.
    org_id: str = Field(min_length=1)
    # The spec revision this proposal was authored against (rule 5). Without it,
    # a plan authored against an older contract is indistinguishable from one
    # authored against the current spec.
    spec_revision: str = Field(min_length=1)
    # The originating intent issue (e.g. "4120"). Optional: hand-run flows have none.
    intent_ref: str | None = None
    nodes: list[ProposedNode] = Field(default_factory=list)
    edges: list[ProposedEdge] = Field(default_factory=list)
    # Presentation only: refs, nodes and edges remain the execution identities.
    # Omitted metadata preserves legacy labels and execution hashes.
    wave_metadata: list[WaveMetadata] = Field(default_factory=list)
    epic_metadata: list[EpicMetadata] = Field(default_factory=list)

    # --- The design loop's story (#4885), both optional -------------------
    # Provenance about how this plan came to be, NOT part of the plan's
    # executable content — which is why `compile.plan_hash` excludes both. Two
    # documents differing only in description are the same plan, and hashing
    # them differently would refuse a fail-soft retry that spanned the deploy as
    # a plan-of-record rewrite.
    #
    # Optional, and an omission is honoured as `NULL` rather than defaulted:
    # a hand-authored proposal has no design loop behind it, and an author who
    # cannot state a stage's outcome must leave it out rather than guess. That is
    # what keeps "we do not know" reachable by construction.
    description: str | None = Field(default=None, max_length=DESCRIPTION_MAX_LEN)
    design_history: DesignHistory | None = None

    # --- The authority the owner delegates (#5128), optional --------------
    # What autonomous actions this plan's agents may take, where, and within what
    # bounds. **Unlike the two fields above, this IS part of the plan's executable
    # content** and so is deliberately covered by `compile.plan_hash`: two
    # documents differing only in what they authorize are not the same plan, and
    # hashing them alike would let an amendment that widened authority be mistaken
    # for a retry of the narrower one.
    #
    # Optional, and **an omission preserves legacy semantics exactly**: a plan with
    # no policy dispatches as it did before this field existed. That is what keeps
    # the change opt-in, and it is why the default is `None` rather than a
    # permissive policy — a default that authorized anything would silently widen
    # every existing flow on deploy, and a default that authorized nothing would
    # silently halt them all.
    #
    # `policy_id`, `policy_hash` and `principal_id` inside this block are
    # server-stamped: a submitted document setting any of them is rejected by
    # `execution_policy.stamp_policy`, not overwritten. An author cannot name their
    # own policy identity or principal, for the same reason `org_id` above is
    # compared rather than trusted.
    execution_policy: ExecutionPolicy | None = None

    # --- The authority a plan PROPOSES but has not been granted (#5331) -----
    # The same document as `execution_policy` above, in the one place nothing reads
    # it as a grant. This is where a policy lives between *submitted* and
    # *accepted*, and the separation is the whole mechanism.
    #
    # Why a second field rather than a flag on the first. `policy_admission.
    # load_in_force_policy` resolves authority by reading
    # `plan_document["execution_policy"]`, and its permit path formats
    # `policy.policy_id or '(unstamped)'` — so an unaccepted policy sitting in that
    # field would be *enforced as authority*, unstamped, with no principal, by code
    # that has no way to tell a proposal from a grant. Inertness therefore cannot be
    # a property anyone remembers to check; it has to be a field the admission path
    # contains no code to read. This is that field, and grepping for its name across
    # `policy_admission.py`, `runtime_policy.py` and `dispatch_pass.py` returns
    # nothing by design.
    #
    # Set by `registration.demote_proposed_policy`, which moves a submitted
    # `execution_policy` here as part of the registration transform — so a *draft*
    # carries its policy's full text for human review while granting nothing at all.
    # Promoted back by `registration.promote_proposed_policy` at the moment a human
    # answers the acceptance gate bound to this exact revision, through the
    # unchanged `compile.accept_execution_policy` / `stamp_policy` path with that
    # human as principal. The direct acceptance route (`POST /flows`) never uses
    # either: its caller is already human and already holds `PLAN_APPROVE`, so there
    # is no interval to protect.
    #
    # **Covered by `compile.plan_hash`, for the same reason `execution_policy` is.**
    # Two drafts differing only in the authority they propose are not the same draft
    # — one asks a human to grant more than the other — and hashing them alike would
    # make the wider proposal indistinguishable from a retry of the narrower one, so
    # a human's bound acceptance of the narrow revision would arm the wide one. The
    # key is omitted from the canonical JSON when absent (`plan_hash` drops it), so
    # every plan authored before this field existed keeps its exact bytes and hash.
    #
    # Dropping a submitted policy instead of demoting it would be the worse failure:
    # the graph would run with legacy *unbounded* semantics while its author
    # believed it constrained. Both halves of that are why this field exists.
    proposed_execution_policy: ExecutionPolicy | None = None

    @model_validator(mode="after")
    def _one_policy_field_at_most(self) -> "LoopProposal":
        """A document may declare a policy, or carry a demoted one — never both.

        Both set at once has no coherent reading: the two fields would name
        different authority for one plan, and whichever the reader consulted would
        be an arbitrary answer to a question with a real one. In particular
        `promote_proposed_policy` moves the value between the fields, so a document
        holding both is either a hand-edited accepted plan or a transform applied
        twice, and neither should compile.

        Refused at the boundary rather than resolved by precedence, because a
        precedence rule is exactly what would let a submitter park a wide policy in
        the field the reader ignores and a narrow one in the field it reads.
        """
        if self.execution_policy is not None and self.proposed_execution_policy is not None:
            raise ValueError(
                "a plan may declare 'execution_policy' or carry a demoted 'proposed_execution_policy', not both; "
                "two different grants for one plan have no defined reading"
            )
        return self


def _check_addresses(proposal: LoopProposal) -> list[Violation]:
    """Rule 1: every address is `flow/epic/wave/node`, and addresses are unique.

    Uniqueness is not cosmetic. Cost rollup and the graph view both key on
    address, so two nodes answering to one address makes both silently
    mis-attribute — the rollup double-counts and the view renders one node over
    the other. The store's `uq_orchestration_nodes_address` index would also
    reject it at insert, but by then the transaction is already open and the
    author gets a database error instead of an explanation.
    """
    violations: list[Violation] = []
    seen: set[str] = set()

    for node in proposal.nodes:
        if not ADDRESS_PATTERN.match(node.address):
            violations.append(
                Violation(
                    rule="address_form",
                    message="graph address must be of the form 'flow/epic/wave/node' with four non-empty segments",
                    where=node.address,
                )
            )
            continue

        if node.address in seen:
            violations.append(
                Violation(
                    rule="duplicate_address",
                    message=(
                        "graph address is declared more than once; addresses must be unique because cost rollup and the graph view both key on them"
                    ),
                    where=node.address,
                )
            )
        seen.add(node.address)

        # An address whose flow segment disagrees with the proposal's flow is an
        # address for a different flow. Compiling it would file the node under
        # this flow while it claims to belong to another.
        flow_segment = node.address.split("/", 1)[0]
        if flow_segment != proposal.flow_slug:
            violations.append(
                Violation(
                    rule="flow_segment_mismatch",
                    message=f"address flow segment {flow_segment!r} does not match the proposal's flow_slug {proposal.flow_slug!r}",
                    where=node.address,
                )
            )

    return violations


def _check_kinds(proposal: LoopProposal) -> list[Violation]:
    """Rule 2: every kind is story / eval / gate. Containers are not nodes.

    Wave, EPIC and flow are derived by rolling up member nodes. A container as a
    node would be a second source of truth for a value its children already
    imply, and the two would drift. The distinct message for container kinds is
    load-bearing for the author: "wave is not a valid kind" invites them to hunt
    for the right spelling, when the actual answer is that waves are not declared
    at all.
    """
    violations: list[Violation] = []
    allowed = ", ".join(sorted(_EXECUTABLE_KINDS))

    for node in proposal.nodes:
        if node.kind in _EXECUTABLE_KINDS:
            continue

        if node.kind in _CONTAINER_KINDS:
            violations.append(
                Violation(
                    rule="container_as_node",
                    message=(
                        f"{node.kind!r} is a container level, not an executable node; containers "
                        f"(wave/epic/flow) are derived by rolling up their member nodes and must not be declared"
                    ),
                    where=node.address,
                )
            )
        else:
            violations.append(
                Violation(
                    rule="unknown_kind",
                    message=f"kind {node.kind!r} is not an executable node kind (expected one of: {allowed})",
                    where=node.address,
                )
            )

    return violations


def _check_edges(proposal: LoopProposal) -> list[Violation]:
    """Rule 3: edge endpoints resolve to declared nodes, and the edge set is acyclic.

    A dangling endpoint cannot be compiled — there is no node id to point at. A
    cycle cannot be executed: the tick advances `pending -> ready` when
    predecessors are satisfied, and in a cycle no member's predecessors are ever
    satisfied, so the whole cycle sits pending forever with no error to explain
    why.

    Cycle detection runs over the subgraph of resolvable edges only. Including a
    dangling edge would either crash the traversal or invent a phantom node, and
    either way the author would get a confusing "cycle" on top of the dangling-
    endpoint violation they already have.
    """
    violations: list[Violation] = []
    declared = {node.address for node in proposal.nodes}

    adjacency = _resolvable_adjacency(proposal)

    for edge in proposal.edges:
        label = f"{edge.from_address} -> {edge.to_address}"

        for endpoint, side in ((edge.from_address, "from"), (edge.to_address, "to")):
            if endpoint not in declared:
                violations.append(
                    Violation(
                        rule="dangling_edge",
                        message=f"edge {side} endpoint {endpoint!r} does not resolve to a declared node",
                        where=label,
                    )
                )

        if edge.from_address == edge.to_address:
            violations.append(
                Violation(
                    rule="self_edge",
                    message="an edge from a node to itself is a one-node cycle; the node's predecessors can never be satisfied",
                    where=label,
                )
            )

    cycle = _find_cycle(adjacency)
    if cycle is not None:
        violations.append(
            Violation(
                rule="cycle",
                message="the edge set contains a cycle; nodes in a cycle can never have their predecessors satisfied and would sit pending forever",
                where=" -> ".join(cycle),
            )
        )

    return violations


def _work_identity(issue_ref: str | None) -> str | None:
    """The normalized work identity an `issue_ref` denotes, or None if it has one.

    Two nodes claim the same work when they resolve to the same issue *number*,
    not the same string. `"5127"`, `"#5127"` and `" 5127 "` are one issue spelled
    three ways, and comparing raw strings would let a whitespace or `#` difference
    hide a genuine duplicate.

    Deliberately the same parse the runtime performs — `int(str(ref).lstrip("#"))`
    in `dispatch_pass.issue_number_for_dispatch`, `policy_admission` and
    `diagnose` — so plan validation and the transactional work claim cannot
    disagree about which nodes are on one issue. Reimplemented rather than
    imported for the import-weight reason given on rule 6.

    Returns None for an absent, unparseable or non-positive reference. Such a
    reference cannot be a shared work identity: the engine could not route it to
    an issue either, and rule 6 is not the place to report a malformed one.

    Repository identity is not part of the key because the proposal schema has no
    per-node repository: every node of a flow dispatches into the one repository
    resolved at dispatch time, so within a document a shared issue number is a
    shared issue. If per-node repositories are ever added, this key is where they
    join.
    """
    if not issue_ref:
        return None
    try:
        issue = int(str(issue_ref).lstrip("#"))
    except ValueError:
        return None
    return str(issue) if issue > 0 else None


def _resolvable_adjacency(proposal: LoopProposal) -> dict[str, list[str]]:
    """The dependency graph over edges that can actually be followed.

    Declared once and shared by rule 3 (cycles) and rule 6 (same-issue ordering)
    so the two rules cannot disagree about what the graph is. Only *resolvable*
    edges are included — an edge with a dangling endpoint or a self-edge is
    reported by rule 3 and then excluded, because traversing it would either
    invent a phantom node or manufacture a cycle on top of the violation the
    author already has.

    Every declared address is a key, including isolated nodes, so a caller can
    traverse from any node without a membership test first.
    """
    declared = {node.address for node in proposal.nodes}
    adjacency: dict[str, list[str]] = {address: [] for address in declared}

    for edge in proposal.edges:
        if edge.from_address == edge.to_address:
            continue  # A self-edge is rule 3's one-node cycle, not an ordering.
        if edge.from_address in declared and edge.to_address in declared:
            adjacency[edge.from_address].append(edge.to_address)

    return adjacency


def _reaches(adjacency: dict[str, list[str]], source: str, target: str) -> bool:
    """Whether `target` is reachable from `source` by following dependency edges.

    This is what "ordered before" means in a proposal: `from -> to` says `from`
    must reach `PASSED` before `to` becomes ready (see `tick`), so an
    order exists between two nodes exactly when one can be reached from the
    other. Reachability, not a direct-edge test: `a -> x -> b` orders `a` before
    `b` just as firmly as `a -> b`, and an author who expressed the order through
    an intermediate node has still expressed it.

    Iterative breadth-first with a visited set so a deep author-supplied chain
    cannot raise `RecursionError`. Rule 6 only calls this after obtaining a
    topological order; cyclic documents are rejected by rule 3 first because
    "before" is not well-defined until their cycle is repaired.

    A single search with an early exit rather than a precomputed transitive
    closure: closure over a large plan is quadratic in memory for a question
    asked about only the few nodes that share an issue.
    """
    if source == target:
        return True

    seen = {source}
    frontier = [source]

    while frontier:
        node = frontier.pop()
        for successor in adjacency.get(node, ()):
            if successor == target:
                return True
            if successor not in seen:
                seen.add(successor)
                frontier.append(successor)

    return False


def _topological_ranks(adjacency: dict[str, list[str]]) -> dict[str, int] | None:
    """Return each node's position in a topological order, or None for a cycle."""
    indegree = dict.fromkeys(adjacency, 0)
    for successors in adjacency.values():
        for successor in successors:
            indegree[successor] += 1

    ready = [node for node, degree in indegree.items() if degree == 0]
    heapify(ready)
    order: list[str] = []
    while ready:
        node = heappop(ready)
        order.append(node)
        for successor in adjacency[node]:
            indegree[successor] -= 1
            if indegree[successor] == 0:
                heappush(ready, successor)

    if len(order) != len(adjacency):
        return None
    return {node: index for index, node in enumerate(order)}


def _find_cycle(adjacency: dict[str, list[str]]) -> list[str] | None:
    """Return one cycle as a path, or None if the graph is acyclic.

    Iterative DFS with an explicit stack rather than recursion: a proposal is
    author-supplied, and a deep chain would blow Python's recursion limit with a
    `RecursionError` instead of a violation. Returning *a* cycle (not all of
    them) is deliberate — one concrete path is what an author needs to fix it,
    and enumerating every cycle in a dense graph is exponential.
    """
    colour = dict.fromkeys(adjacency, _WHITE)

    for root in adjacency:
        if colour[root] != _WHITE:
            continue

        # Each frame is (node, iterator over its successors). `path` mirrors the
        # frames so a detected back-edge can be reported as a readable route.
        stack: list[tuple[str, object]] = [(root, iter(adjacency[root]))]
        path: list[str] = [root]
        colour[root] = _GREY

        while stack:
            node, successors = stack[-1]
            advanced = False

            for successor in successors:  # type: ignore[union-attr]
                if colour[successor] == _GREY:
                    # Back-edge into the current path: everything from that node
                    # onward is the cycle. Close it so the route reads as a loop.
                    start = path.index(successor)
                    return path[start:] + [successor]
                if colour[successor] == _WHITE:
                    colour[successor] = _GREY
                    stack.append((successor, iter(adjacency[successor])))
                    path.append(successor)
                    advanced = True
                    break

            if not advanced:
                colour[node] = _BLACK
                stack.pop()
                path.pop()

    return None


def _check_wave_evals(proposal: LoopProposal) -> list[Violation]:
    """Rule 4: every wave containing stories contains exactly one `eval` node.

    A wave of stories with no evaluation has nothing that can conclude it — the
    loop would deliver work and never assess it. Two evals is equally broken: the
    wave's outcome would depend on which one the rollup happened to read.

    Waves with no stories are exempt. A gate-only wave is legitimate (a decision
    point between waves of work), and demanding an eval for nothing to evaluate
    would force authors to declare a no-op node.

    A wave is keyed by `(epic, wave)`, not by wave segment alone: two EPICs may
    each have a `wave-1`, and they are different waves.
    """
    violations: list[Violation] = []
    stories: dict[tuple[str, str], int] = {}
    evals: dict[tuple[str, str], int] = {}

    for node in proposal.nodes:
        if not ADDRESS_PATTERN.match(node.address):
            continue  # Already reported by rule 1; its wave cannot be determined.
        _, epic, wave, _ = split_address(node.address)
        key = (epic, wave)
        stories.setdefault(key, 0)
        evals.setdefault(key, 0)
        if node.kind == NodeKind.STORY.value:
            stories[key] += 1
        elif node.kind == NodeKind.EVAL.value:
            evals[key] += 1

    for key, story_count in sorted(stories.items()):
        if story_count == 0:
            continue
        eval_count = evals[key]
        if eval_count != 1:
            epic, wave = key
            violations.append(
                Violation(
                    rule="wave_eval_cardinality",
                    message=(
                        f"wave contains {story_count} story node(s) and {eval_count} eval node(s); "
                        f"a wave with stories must contain exactly one eval node"
                    ),
                    where=f"{proposal.flow_slug}/{epic}/{wave}",
                )
            )

    return violations


def _check_same_issue_ordering(proposal: LoopProposal) -> list[Violation]:
    """Rule 6: two nodes on one issue must be ordered relative to each other.

    Reusing an issue across nodes is legitimate and intentional — a story
    delivers it, a later story repairs what the evaluation found. What is not
    legitimate is two nodes on the same issue with *no order between them*: the
    plan then says "do this issue twice, independently, at the same time", which
    is duplicate work on one issue rather than a sequence.

    The runtime is not where this belongs. Transactional work claims do serialise
    the collision (one node admits, the other is refused), and they stay the
    cross-plan and concurrency backstop. But that turns an unschedulable plan
    into a run-time refusal on a plan that was already *accepted* — the author is
    told at execution what should have been rejected at authoring, and the
    accepted plan on record is one the engine cannot actually execute as written.

    Order means reachable, not adjacent: `a -> eval -> b` sequences `a` before
    `b` perfectly well. So this only fires when neither node can reach the other,
    which is precisely the case where nothing decides which runs first.

    Only `story` and `eval` nodes are considered. A `gate` is a human decision
    presented by the tick; it consumes no worker and performs no work on its
    issue, so two gates on one issue are not competing deliveries. That mirrors
    rule 4 exempting gate-only waves. The set is derived here rather than by
    importing `dispatch_pass.node_requires_issue_routing`, whose module pulls in
    boto3 and SQS — the advisory CLI validates from a bare checkout with only
    pydantic, and this rule must not drag the dispatch stack into that path.

    Nodes with no issue reference are skipped: eval and gate nodes frequently
    have none, and "no issue" is not a shared identity.
    """
    violations: list[Violation] = []
    by_issue: dict[str, list[str]] = {}

    for node in proposal.nodes:
        if node.kind not in _ISSUE_CONSUMING_KINDS:
            continue
        identity = _work_identity(node.issue_ref)
        if identity is None:
            continue
        by_issue.setdefault(identity, []).append(node.address)

    adjacency = _resolvable_adjacency(proposal)
    topological_ranks = _topological_ranks(adjacency)

    for identity, addresses in sorted(by_issue.items()):
        if len(addresses) < 2:
            continue
        # A cyclic document is already rejected by rule 3, and has no valid
        # topological order against which this rule can define "before". Avoid
        # adding expensive secondary diagnostics to a graph that must first have
        # its cycle repaired.
        if topological_ranks is None:
            continue

        # The heap-backed topological rank makes the witness deterministic even
        # when several nodes are ready at once. If each consecutive same-issue
        # pair is reachable, transitivity orders every remaining pair too. The
        # first failed reachability check is therefore one concrete unordered
        # pair the author can safely connect in rank order. Report only that one
        # actionable witness per issue: enumerating every unordered pair creates
        # quadratic output and repeats graph traversals without improving the
        # rejection decision.
        ordered_addresses = sorted(addresses)
        chain = sorted(ordered_addresses, key=topological_ranks.__getitem__)
        for first, second in zip(chain, chain[1:], strict=False):
            if _reaches(adjacency, first, second):
                continue
            violations.append(
                Violation(
                    rule="unordered_same_issue",
                    message=(
                        f"nodes {first!r} and {second!r} both deliver issue {identity!r} but neither "
                        f"is ordered before the other; add a dependency edge between them to declare "
                        f"the intended sequence, or point one of them at a different issue"
                    ),
                    where=f"{first} | {second}",
                )
            )
            break

    return violations


def _check_declarations(proposal: LoopProposal) -> list[Violation]:
    """Rule 5: `org_id` and `spec_revision` are present and meaningful.

    Pydantic's `min_length=1` already rejects empty strings, so this catches the
    whitespace-only case that satisfies the length constraint while carrying no
    information. A proposal must also declare at least one node — an empty plan
    compiles to nothing and would record an accepted plan that accepted nothing.
    """
    violations: list[Violation] = []

    if not proposal.org_id.strip():
        violations.append(Violation(rule="missing_org_id", message="org_id must be declared and non-blank"))

    if not proposal.spec_revision.strip():
        violations.append(
            Violation(
                rule="missing_spec_revision",
                message=(
                    "spec_revision must be declared and non-blank; without it a plan authored "
                    "against an older contract is indistinguishable from a current one"
                ),
            )
        )

    if not proposal.nodes:
        violations.append(Violation(rule="empty_plan", message="a proposal must declare at least one node"))

    return violations


def _check_evaluation_specs(proposal: LoopProposal) -> list[Violation]:
    from .evaluation_contract import specification
    from .execution_policy import AcceptanceMode, Action

    violations = []
    for node in proposal.nodes:
        if node.evaluation is None:
            policy = proposal.execution_policy
            if (
                node.kind == NodeKind.EVAL.value
                and policy is not None
                and Action.EVALUATE in policy.allowed_actions
                and policy.evaluation_acceptance.get(node.address) is AcceptanceMode.MACHINE
            ):
                violations.append(
                    Violation(
                        "evaluation_specification_missing", "Active machine evaluation requires an explicit evidence specification", node.address
                    )
                )
            continue
        if node.kind != NodeKind.EVAL.value:
            violations.append(Violation("evaluation_node_kind", "Only eval nodes may carry an evaluation specification", node.address))
            continue
        try:
            spec = specification(node.evaluation)
        except ValueError:
            violations.append(Violation("evaluation_specification_invalid", "Evaluation specification is invalid or unavailable", node.address))
            continue
        active = proposal.execution_policy
        if (
            active is not None
            and Action.EVALUATE in active.allowed_actions
            and active.evaluation_acceptance.get(node.address) is AcceptanceMode.MACHINE
            and spec.acceptance_mode != "machine"
        ):
            violations.append(Violation("evaluation_policy_mode_mismatch", "Machine policy cannot use human evidence acceptance", node.address))
        if spec.evidence_schema == "repository-evaluation/v1":
            from .repository_evaluation_contract import harness_digest

            if spec.runner.harness_sha256 != harness_digest():
                violations.append(
                    Violation("evaluation_harness_mismatch", "Repository harness digest does not match the installed verifier", node.address)
                )
            kinds = {item.address: item.kind for item in proposal.nodes}
            parents = {edge.from_address for edge in proposal.edges if edge.to_address == node.address and kinds.get(edge.from_address) == "story"}
            if parents != {item.address for item in spec.predecessors}:
                violations.append(Violation("evaluation_predecessor_mismatch", "Evidence must declare every direct story predecessor", node.address))
        if spec.acceptance_mode == "machine":
            # Validation checks the requested authority, including an inert draft.
            # Admission still reads only the accepted execution_policy field.
            policy = proposal.execution_policy or proposal.proposed_execution_policy
            if policy is None or policy.evaluation_acceptance.get(node.address) is not AcceptanceMode.MACHINE:
                violations.append(
                    Violation("evaluation_policy_mode_mismatch", "Machine evidence requires accepted policy machine mode", node.address)
                )
            elif spec.runner.repository not in policy.repository_ids:
                violations.append(
                    Violation("evaluation_repository_not_permitted", "Evaluation harness requires a permitted repository", node.address)
                )
            elif spec.evidence_schema != "repository-evaluation/v1" and spec.environment_connection_id not in policy.environment_connection_ids:
                violations.append(
                    Violation("evaluation_connection_not_permitted", "Evaluation target requires a permitted environment connection", node.address)
                )
    return violations


def _check_display_metadata(proposal: LoopProposal) -> list[Violation]:
    waves = set()
    for node in proposal.nodes:
        try:
            _, epic, wave, _ = split_address(node.address)
            waves.add((epic, wave))
        except ValueError:
            pass  # Address validation reports the malformed node separately.
    epics = {epic for epic, _ in waves}
    seen_epics = set()
    violations = []
    for metadata in proposal.epic_metadata:
        if metadata.epic_ref in seen_epics:
            violations.append(Violation("duplicate_epic_metadata", "Declare display metadata once per epic.", metadata.epic_ref))
        if metadata.epic_ref not in epics:
            violations.append(Violation("unknown_epic_metadata", "Display metadata must reference an epic present in the nodes.", metadata.epic_ref))
        seen_epics.add(metadata.epic_ref)
    seen = set()
    for metadata in proposal.wave_metadata:
        key = (metadata.epic_ref, metadata.wave_ref)
        where = "/".join(key)
        if key in seen:
            violations.append(Violation("duplicate_wave_metadata", "Declare display metadata once per wave.", where))
        if key not in waves:
            violations.append(Violation("unknown_wave_metadata", "Display metadata must reference a wave present in the nodes.", where))
        seen.add(key)
    return violations


def validate_proposal(proposal: LoopProposal) -> list[Violation]:
    """Check a proposal against every rule and return **all** violations.

    Returns rather than raises, and runs every rule rather than stopping at the
    first failure, because the advisory CLI's whole value is showing an author
    everything wrong in one pass. `compile_proposal` is what turns a non-empty
    result into a rejection.

    An empty list means the document is well-formed. It does **not** mean the
    document may be compiled — tenant ownership is checked by `compile_proposal`
    against server-resolved context this function cannot see.

    Args:
        proposal: A parsed `LoopProposal`.

    Returns:
        Every violation found, in rule order. Empty when well-formed.
    """
    return [
        *_check_declarations(proposal),
        *_check_evaluation_specs(proposal),
        *_check_addresses(proposal),
        *_check_display_metadata(proposal),
        *_check_kinds(proposal),
        *_check_edges(proposal),
        *_check_wave_evals(proposal),
        *_check_same_issue_ordering(proposal),
    ]


# The state every compiled node starts in. Imported from the vocabulary rather
# than spelled as a literal so a vocabulary change cannot leave this behind.
INITIAL_NODE_STATE = NodeState.PENDING
