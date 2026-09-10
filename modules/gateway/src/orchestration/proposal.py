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

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Imported, never redefined — see module docstring (R-N2a).
from .models import NodeKind
from .state import NodeState

__all__ = [
    "ADDRESS_PATTERN",
    "LoopProposal",
    "ProposedEdge",
    "ProposedNode",
    "Violation",
    "validate_proposal",
]


# A graph address is `flow/epic/wave/node` — exactly four non-empty segments
# (D-R13). Segments allow word characters, dots and hyphens: enough for slugs and
# issue refs, and deliberately not `/`, which would let one segment forge two and
# make a three-segment address parse as four.
_SEGMENT = r"[A-Za-z0-9][A-Za-z0-9._-]*"
ADDRESS_PATTERN = re.compile(rf"^{_SEGMENT}/{_SEGMENT}/{_SEGMENT}/{_SEGMENT}$")

# The executable node kinds, derived from the store's enum rather than listed.
# Hand-listing them here is exactly how the vocabulary would drift.
_EXECUTABLE_KINDS = frozenset(kind.value for kind in NodeKind)

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


def split_address(address: str) -> tuple[str, str, str, str]:
    """Split a validated graph address into its four segments.

    Raises:
        ValueError: If `address` is not of the form `flow/epic/wave/node`. Callers
            that have already run `validate_proposal` cannot hit this; the raise
            exists so a caller that skipped validation fails loudly here rather
            than writing a malformed address to the store.
    """
    if not ADDRESS_PATTERN.match(address):
        raise ValueError(f"not a graph address of the form 'flow/epic/wave/node': {address!r}")
    flow, epic, wave, node = address.split("/")
    return flow, epic, wave, node


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

    adjacency: dict[str, list[str]] = {address: [] for address in declared}

    for edge in proposal.edges:
        label = f"{edge.from_address} -> {edge.to_address}"
        resolvable = True

        for endpoint, side in ((edge.from_address, "from"), (edge.to_address, "to")):
            if endpoint not in declared:
                resolvable = False
                violations.append(
                    Violation(
                        rule="dangling_edge",
                        message=f"edge {side} endpoint {endpoint!r} does not resolve to a declared node",
                        where=label,
                    )
                )

        if edge.from_address == edge.to_address:
            resolvable = False
            violations.append(
                Violation(
                    rule="self_edge",
                    message="an edge from a node to itself is a one-node cycle; the node's predecessors can never be satisfied",
                    where=label,
                )
            )

        if resolvable:
            adjacency[edge.from_address].append(edge.to_address)

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
        *_check_addresses(proposal),
        *_check_kinds(proposal),
        *_check_edges(proposal),
        *_check_wave_evals(proposal),
    ]


# The state every compiled node starts in. Imported from the vocabulary rather
# than spelled as a literal so a vocabulary change cannot leave this behind.
INITIAL_NODE_STATE = NodeState.PENDING
