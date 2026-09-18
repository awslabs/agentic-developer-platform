"""Tests for the loop-proposal schema and its validation rules.

Issue #4199. Each of the five rules from the story gets its own test, and the
tests assert on `Violation.rule` rather than on message prose so a reworded
message does not break the suite while a *missing rule* still does.

The rule these tests exist to protect is not any single check — it is that the
checks live in one place. `validate_proposal` is imported here from exactly where
`compile_proposal` and the advisory CLI import it. A test that reimplemented a
rule to compare against would defeat the purpose.
"""

import pytest
from pydantic import ValidationError

from src.orchestration.models import NodeKind
from src.orchestration.proposal import (
    ADDRESS_PATTERN,
    INITIAL_NODE_STATE,
    LoopProposal,
    ProposedEdge,
    ProposedNode,
    Violation,
    split_address,
    validate_proposal,
)
from src.orchestration.state import NodeState

FLOW = "demo-flow"
SPEC_REVISION = "issue-4120-r1"


def address(node_ref: str, *, epic: str = "epic-1", wave: str = "wave-1") -> str:
    return f"{FLOW}/{epic}/{wave}/{node_ref}"


def make_proposal(*, nodes=None, edges=None, **overrides) -> LoopProposal:
    """A minimal well-formed proposal, with targeted fields overridden.

    Defaults are deliberately the *smallest* valid plan — one story plus the eval
    rule 4 demands — so that a test overriding one field is testing that field and
    not incidentally tripping another rule.
    """
    payload = {
        "flow_slug": FLOW,
        "title": "Demo flow",
        "org_id": "org-alpha",
        "spec_revision": SPEC_REVISION,
        "nodes": nodes
        if nodes is not None
        else [
            ProposedNode(address=address("story-a"), kind="story", title="Story A"),
            ProposedNode(address=address("eval"), kind="eval", title="Wave 1 eval"),
        ],
        "edges": edges if edges is not None else [],
    }
    payload.update(overrides)
    return LoopProposal(**payload)


def rules(violations: list[Violation]) -> set[str]:
    return {violation.rule for violation in violations}


class TestWellFormed:
    def test_minimal_proposal_has_no_violations(self):
        """The baseline every other test perturbs — it must start clean."""
        assert validate_proposal(make_proposal()) == []

    def test_realistic_multi_wave_proposal_has_no_violations(self):
        """Multiple waves, each with its own eval, chained by edges."""
        nodes = [
            ProposedNode(address=address("story-a", wave="wave-1"), kind="story", title="A"),
            ProposedNode(address=address("story-b", wave="wave-1"), kind="story", title="B"),
            ProposedNode(address=address("eval", wave="wave-1"), kind="eval", title="Eval 1"),
            ProposedNode(address=address("story-c", wave="wave-2"), kind="story", title="C"),
            ProposedNode(address=address("eval", wave="wave-2"), kind="eval", title="Eval 2"),
            ProposedNode(address=address("gate", wave="wave-2"), kind="gate", title="Gate"),
        ]
        edges = [
            ProposedEdge(from_address=address("story-a", wave="wave-1"), to_address=address("eval", wave="wave-1")),
            ProposedEdge(from_address=address("story-b", wave="wave-1"), to_address=address("eval", wave="wave-1")),
            ProposedEdge(from_address=address("eval", wave="wave-1"), to_address=address("story-c", wave="wave-2")),
            ProposedEdge(from_address=address("story-c", wave="wave-2"), to_address=address("eval", wave="wave-2")),
            ProposedEdge(from_address=address("eval", wave="wave-2"), to_address=address("gate", wave="wave-2")),
        ]
        assert validate_proposal(make_proposal(nodes=nodes, edges=edges)) == []

    def test_gate_only_wave_needs_no_eval(self):
        """A decision point between waves of work is a legitimate wave with
        nothing to evaluate; demanding an eval would force a no-op node."""
        nodes = [
            ProposedNode(address=address("story-a", wave="wave-1"), kind="story", title="A"),
            ProposedNode(address=address("eval", wave="wave-1"), kind="eval", title="Eval 1"),
            ProposedNode(address=address("gate", wave="wave-2"), kind="gate", title="Gate only"),
        ]
        assert validate_proposal(make_proposal(nodes=nodes)) == []


class TestRule1Addresses:
    """Rule 1: `flow/epic/wave/node`, unique."""

    def test_duplicate_address_is_a_violation(self):
        """Cost rollup and the graph view both key on address, so two nodes
        answering to one address makes both silently mis-attribute."""
        nodes = [
            ProposedNode(address=address("story-a"), kind="story", title="A"),
            ProposedNode(address=address("story-a"), kind="story", title="A again"),
            ProposedNode(address=address("eval"), kind="eval", title="Eval"),
        ]
        violations = validate_proposal(make_proposal(nodes=nodes))
        assert "duplicate_address" in rules(violations)
        assert any(v.where == address("story-a") for v in violations if v.rule == "duplicate_address")

    @pytest.mark.parametrize(
        "bad",
        [
            "flow/epic/wave",  # three segments
            "flow/epic/wave/node/extra",  # five segments
            "flow//wave/node",  # empty segment
            "/epic/wave/node",  # leading empty segment
            "flow/epic/wave/",  # trailing empty segment
            "just-a-name",  # no separators at all
            "demo-flow/epic 1/wave-1/node",  # space in a segment
        ],
    )
    def test_malformed_address_is_a_violation(self, bad):
        nodes = [ProposedNode(address=bad, kind="story", title="A")]
        assert "address_form" in rules(validate_proposal(make_proposal(nodes=nodes)))

    def test_address_pattern_accepts_dots_and_hyphens(self):
        """Slugs and issue refs use both; rejecting them would force authors into
        an artificial naming scheme."""
        assert ADDRESS_PATTERN.match("my-flow/epic-4191/wave-1/story.a-1")

    def test_flow_segment_must_match_the_declared_flow(self):
        """An address for another flow would be filed under this one while
        claiming to belong elsewhere."""
        nodes = [
            ProposedNode(address="other-flow/epic-1/wave-1/story-a", kind="story", title="A"),
            ProposedNode(address="other-flow/epic-1/wave-1/eval", kind="eval", title="Eval"),
        ]
        assert "flow_segment_mismatch" in rules(validate_proposal(make_proposal(nodes=nodes)))

    def test_split_address_rejects_a_malformed_address(self):
        """A caller that skipped validation must fail here rather than write a
        malformed address to the store."""
        with pytest.raises(ValueError, match="flow/epic/wave/node"):
            split_address("flow/epic/wave")


class TestRule2Kinds:
    """Rule 2: story / eval / gate only. Containers are derived, never nodes."""

    @pytest.mark.parametrize("container", ["wave", "epic", "flow"])
    def test_container_kind_is_a_violation(self, container):
        """The load-bearing case: a container smuggled in as a node would be a
        second source of truth for a value its children already imply."""
        nodes = [
            ProposedNode(address=address("story-a"), kind="story", title="A"),
            ProposedNode(address=address("eval"), kind="eval", title="Eval"),
            ProposedNode(address=address("sneaky"), kind=container, title="A container"),
        ]
        violations = validate_proposal(make_proposal(nodes=nodes))
        assert "container_as_node" in rules(violations)

    def test_container_violation_explains_that_containers_are_derived(self):
        """ "wave is not a valid kind" would send an author hunting for the right
        spelling; the answer is that waves are not declared at all."""
        nodes = [ProposedNode(address=address("sneaky"), kind="wave", title="W")]
        violation = next(v for v in validate_proposal(make_proposal(nodes=nodes)) if v.rule == "container_as_node")
        assert "derived" in violation.message

    def test_unknown_kind_is_a_violation_distinct_from_a_container(self):
        nodes = [ProposedNode(address=address("weird"), kind="deployment", title="D")]
        assert "unknown_kind" in rules(validate_proposal(make_proposal(nodes=nodes)))

    def test_all_three_executable_kinds_are_accepted(self):
        """Guards against the enum and the validator drifting apart."""
        nodes = [ProposedNode(address=address(kind.value), kind=kind.value, title=kind.value) for kind in NodeKind]
        assert rules(validate_proposal(make_proposal(nodes=nodes))) == set()

    def test_container_kinds_are_absent_from_the_node_kind_enum(self):
        """The vocabulary itself must not offer a container kind — if `NodeKind`
        ever gained `WAVE`, rule 2 would start accepting it."""
        assert {kind.value for kind in NodeKind}.isdisjoint({"wave", "epic", "flow"})


class TestRule3Edges:
    """Rule 3: endpoints resolve, and the edge set is acyclic."""

    def test_edge_to_undeclared_node_is_a_violation(self):
        edges = [ProposedEdge(from_address=address("story-a"), to_address=address("ghost"))]
        assert "dangling_edge" in rules(validate_proposal(make_proposal(edges=edges)))

    def test_edge_from_undeclared_node_is_a_violation(self):
        edges = [ProposedEdge(from_address=address("ghost"), to_address=address("eval"))]
        assert "dangling_edge" in rules(validate_proposal(make_proposal(edges=edges)))

    def test_two_node_cycle_is_a_violation(self):
        """In a cycle no member's predecessors are ever satisfied, so the whole
        cycle sits pending forever with no error to explain why."""
        edges = [
            ProposedEdge(from_address=address("story-a"), to_address=address("eval")),
            ProposedEdge(from_address=address("eval"), to_address=address("story-a")),
        ]
        assert "cycle" in rules(validate_proposal(make_proposal(edges=edges)))

    def test_longer_cycle_is_a_violation(self):
        nodes = [
            ProposedNode(address=address("a"), kind="story", title="A"),
            ProposedNode(address=address("b"), kind="story", title="B"),
            ProposedNode(address=address("c"), kind="story", title="C"),
            ProposedNode(address=address("eval"), kind="eval", title="Eval"),
        ]
        edges = [
            ProposedEdge(from_address=address("a"), to_address=address("b")),
            ProposedEdge(from_address=address("b"), to_address=address("c")),
            ProposedEdge(from_address=address("c"), to_address=address("a")),
        ]
        violations = validate_proposal(make_proposal(nodes=nodes, edges=edges))
        assert "cycle" in rules(violations)

    def test_cycle_violation_reports_a_closed_route(self):
        """An author needs a concrete path to fix, not "there is a cycle"."""
        edges = [
            ProposedEdge(from_address=address("story-a"), to_address=address("eval")),
            ProposedEdge(from_address=address("eval"), to_address=address("story-a")),
        ]
        violation = next(v for v in validate_proposal(make_proposal(edges=edges)) if v.rule == "cycle")
        route = violation.where.split(" -> ")
        assert route[0] == route[-1], f"route should close into a loop: {violation.where}"

    def test_self_edge_is_a_violation(self):
        edges = [ProposedEdge(from_address=address("story-a"), to_address=address("story-a"))]
        assert "self_edge" in rules(validate_proposal(make_proposal(edges=edges)))

    def test_diamond_is_not_a_cycle(self):
        """Shared ancestry revisits a node without cycling; a naive visited-set
        check would call this a cycle and reject legitimate parallel branches."""
        nodes = [
            ProposedNode(address=address("root"), kind="story", title="Root"),
            ProposedNode(address=address("left"), kind="story", title="Left"),
            ProposedNode(address=address("right"), kind="story", title="Right"),
            ProposedNode(address=address("join"), kind="story", title="Join"),
            ProposedNode(address=address("eval"), kind="eval", title="Eval"),
        ]
        edges = [
            ProposedEdge(from_address=address("root"), to_address=address("left")),
            ProposedEdge(from_address=address("root"), to_address=address("right")),
            ProposedEdge(from_address=address("left"), to_address=address("join")),
            ProposedEdge(from_address=address("right"), to_address=address("join")),
        ]
        assert "cycle" not in rules(validate_proposal(make_proposal(nodes=nodes, edges=edges)))

    def test_dangling_edge_does_not_also_report_a_phantom_cycle(self):
        """Cycle detection runs over resolvable edges only, so an author gets the
        dangling-endpoint violation without a confusing cycle on top."""
        edges = [ProposedEdge(from_address=address("ghost"), to_address=address("phantom"))]
        found = rules(validate_proposal(make_proposal(edges=edges)))
        assert "dangling_edge" in found
        assert "cycle" not in found

    def test_deep_chain_does_not_hit_the_recursion_limit(self):
        """Proposals are author-supplied; a deep chain must produce a verdict, not
        a RecursionError. 2000 exceeds CPython's default limit of 1000."""
        depth = 2000
        nodes = [ProposedNode(address=address(f"n{i}"), kind="story", title=f"N{i}") for i in range(depth)]
        nodes.append(ProposedNode(address=address("eval"), kind="eval", title="Eval"))
        edges = [ProposedEdge(from_address=address(f"n{i}"), to_address=address(f"n{i + 1}")) for i in range(depth - 1)]
        assert "cycle" not in rules(validate_proposal(make_proposal(nodes=nodes, edges=edges)))

    def test_deep_cycle_is_still_detected(self):
        """The iterative traversal must not lose detection power at depth."""
        depth = 1500
        nodes = [ProposedNode(address=address(f"n{i}"), kind="story", title=f"N{i}") for i in range(depth)]
        nodes.append(ProposedNode(address=address("eval"), kind="eval", title="Eval"))
        edges = [ProposedEdge(from_address=address(f"n{i}"), to_address=address(f"n{i + 1}")) for i in range(depth - 1)]
        edges.append(ProposedEdge(from_address=address(f"n{depth - 1}"), to_address=address("n0")))
        assert "cycle" in rules(validate_proposal(make_proposal(nodes=nodes, edges=edges)))


class TestRule4WaveEvals:
    """Rule 4: a wave with stories has exactly one eval."""

    def test_wave_with_stories_and_no_eval_is_a_violation(self):
        """A wave of work with nothing to conclude it would deliver and never
        assess."""
        nodes = [ProposedNode(address=address("story-a"), kind="story", title="A")]
        assert "wave_eval_cardinality" in rules(validate_proposal(make_proposal(nodes=nodes)))

    def test_wave_with_two_evals_is_a_violation(self):
        """Two evals makes the wave's outcome depend on which one the rollup
        happens to read."""
        nodes = [
            ProposedNode(address=address("story-a"), kind="story", title="A"),
            ProposedNode(address=address("eval-1"), kind="eval", title="Eval 1"),
            ProposedNode(address=address("eval-2"), kind="eval", title="Eval 2"),
        ]
        assert "wave_eval_cardinality" in rules(validate_proposal(make_proposal(nodes=nodes)))

    def test_only_the_offending_wave_is_reported(self):
        """A second wave missing its eval must not mask a first wave that is fine."""
        nodes = [
            ProposedNode(address=address("story-a", wave="wave-1"), kind="story", title="A"),
            ProposedNode(address=address("eval", wave="wave-1"), kind="eval", title="Eval 1"),
            ProposedNode(address=address("story-b", wave="wave-2"), kind="story", title="B"),
        ]
        violations = [v for v in validate_proposal(make_proposal(nodes=nodes)) if v.rule == "wave_eval_cardinality"]
        assert len(violations) == 1
        assert violations[0].where.endswith("wave-2")

    def test_same_wave_name_under_two_epics_are_different_waves(self):
        """`wave-1` under epic-1 and under epic-2 are distinct; keying on the wave
        segment alone would let one epic's eval satisfy the other's."""
        nodes = [
            ProposedNode(address=address("story-a", epic="epic-1"), kind="story", title="A"),
            ProposedNode(address=address("eval", epic="epic-1"), kind="eval", title="Eval 1"),
            ProposedNode(address=address("story-b", epic="epic-2"), kind="story", title="B"),
        ]
        violations = [v for v in validate_proposal(make_proposal(nodes=nodes)) if v.rule == "wave_eval_cardinality"]
        assert len(violations) == 1
        assert "epic-2" in violations[0].where


class TestRule6SameIssueOrdering:
    """Rule 6: two nodes on one issue must be ordered relative to each other.

    Issue #5335. Reusing an issue is legitimate (deliver, then repair what the
    evaluation found); reusing it with *no order between the two nodes* is a plan
    to do one issue twice at once. Transactional work claims remain the
    cross-plan/concurrency backstop — this rule stops an unschedulable plan being
    *accepted* in the first place.
    """

    def _wave(self, first_issue, second_issue, edges):
        nodes = [
            ProposedNode(address=address("story-a"), kind="story", title="A", issue_ref=first_issue),
            ProposedNode(address=address("story-b"), kind="story", title="B", issue_ref=second_issue),
            ProposedNode(address=address("eval"), kind="eval", title="Eval", issue_ref="9999"),
        ]
        return make_proposal(nodes=nodes, edges=edges)

    def _to_eval(self):
        return [
            ProposedEdge(from_address=address("story-a"), to_address=address("eval")),
            ProposedEdge(from_address=address("story-b"), to_address=address("eval")),
        ]

    def test_unordered_duplicate_issue_is_a_violation(self):
        """The reproduction from #5335: both stories claim one issue, both point
        only at the eval, so nothing decides which of them runs first."""
        violations = validate_proposal(self._wave("5127", "5127", self._to_eval()))
        assert "unordered_same_issue" in rules(violations)

    def test_the_violation_names_both_unordered_nodes_and_the_issue(self):
        """An author fixing this needs to know which two nodes to sequence."""
        violations = [v for v in validate_proposal(self._wave("5127", "5127", self._to_eval())) if v.rule == "unordered_same_issue"]
        assert len(violations) == 1
        assert address("story-a") in violations[0].where
        assert address("story-b") in violations[0].where
        assert "5127" in violations[0].message

    @pytest.mark.parametrize(
        ("first", "second"),
        [
            ("5127", "#5127"),
            ("#5127", "5127"),
            (" 5127 ", "5127"),
            ("#5127 ", "5127"),
            ("##5127", "#5127"),
        ],
    )
    def test_equivalent_issue_spellings_are_one_identity(self, first, second):
        """`"5127"` and `"#5127"` both occur in real proposals and the runtime
        claim `lstrip("#")`s them to one number; comparing raw strings would let a
        spelling difference hide a genuine duplicate."""
        assert "unordered_same_issue" in rules(validate_proposal(self._wave(first, second, self._to_eval())))

    def test_explicit_ordering_permits_intentional_reuse(self):
        """The whole point of the rule: sequenced reuse is legal, and this is the
        developer-then-repair shape that must keep compiling."""
        edges = [
            ProposedEdge(from_address=address("story-a"), to_address=address("story-b")),
            ProposedEdge(from_address=address("story-b"), to_address=address("eval")),
        ]
        assert validate_proposal(self._wave("5127", "5127", edges)) == []

    def test_transitive_ordering_is_ordering(self):
        """`a -> eval -> b` sequences a before b as firmly as a direct edge; an
        author who expressed the order through an intermediate still expressed
        it."""
        edges = [
            ProposedEdge(from_address=address("story-a"), to_address=address("eval")),
            ProposedEdge(from_address=address("eval"), to_address=address("story-b")),
        ]
        assert validate_proposal(self._wave("5127", "5127", edges)) == []

    def test_reverse_ordering_is_ordering(self):
        """Order, not direction: b before a is as ordered as a before b."""
        edges = [
            ProposedEdge(from_address=address("story-b"), to_address=address("story-a")),
            ProposedEdge(from_address=address("story-a"), to_address=address("eval")),
        ]
        assert validate_proposal(self._wave("5127", "5127", edges)) == []

    def test_independent_issues_stay_parallel(self):
        """Two stories on different issues are exactly what a wave is for; the
        rule must not force an ordering on unrelated work."""
        assert validate_proposal(self._wave("5127", "5128", self._to_eval())) == []

    def test_nodes_without_an_issue_are_not_a_shared_identity(self):
        """Eval and gate nodes frequently carry no issue; "no issue" is not a
        claim on one."""
        assert validate_proposal(self._wave(None, None, self._to_eval())) == []

    def test_gates_on_one_issue_are_exempt(self):
        """A gate is a human decision the tick presents — it consumes no worker
        and performs no work on its issue, so two gates cannot be competing
        deliveries. Mirrors rule 4 exempting gate-only waves."""
        nodes = [
            ProposedNode(address=address("story-a"), kind="story", title="A", issue_ref="1"),
            ProposedNode(address=address("eval"), kind="eval", title="Eval", issue_ref="2"),
            ProposedNode(address=address("gate-1"), kind="gate", title="G1", issue_ref="7"),
            ProposedNode(address=address("gate-2"), kind="gate", title="G2", issue_ref="7"),
        ]
        edges = [ProposedEdge(from_address=address("story-a"), to_address=address("eval"))]
        assert validate_proposal(make_proposal(nodes=nodes, edges=edges)) == []

    def test_a_story_and_its_eval_on_one_issue_must_still_be_ordered(self):
        """An eval dispatches to its issue and consumes a worker just as a story
        does, so an unordered story+eval pair on one issue is the same
        collision."""
        nodes = [
            ProposedNode(address=address("story-a"), kind="story", title="A", issue_ref="5127"),
            ProposedNode(address=address("eval"), kind="eval", title="Eval", issue_ref="5127"),
        ]
        assert "unordered_same_issue" in rules(validate_proposal(make_proposal(nodes=nodes, edges=[])))

    def test_three_nodes_on_one_issue_report_each_unordered_pair(self):
        """A partially ordered trio still needs the pair that is not ordered
        named, rather than one violation for the issue as a whole."""
        nodes = [
            ProposedNode(address=address("story-a"), kind="story", title="A", issue_ref="5127"),
            ProposedNode(address=address("story-b"), kind="story", title="B", issue_ref="5127"),
            ProposedNode(address=address("story-c"), kind="story", title="C", issue_ref="5127"),
            ProposedNode(address=address("eval"), kind="eval", title="Eval", issue_ref="9999"),
        ]
        # a -> b orders that one pair; c is ordered against neither.
        edges = [
            ProposedEdge(from_address=address("story-a"), to_address=address("story-b")),
            ProposedEdge(from_address=address("story-b"), to_address=address("eval")),
            ProposedEdge(from_address=address("story-c"), to_address=address("eval")),
        ]
        violations = [v for v in validate_proposal(make_proposal(nodes=nodes, edges=edges)) if v.rule == "unordered_same_issue"]
        pairs = {frozenset(v.where.split(" | ")) for v in violations}
        assert pairs == {
            frozenset({address("story-a"), address("story-c")}),
            frozenset({address("story-b"), address("story-c")}),
        }

    def test_identity_matches_the_runtime_parse_exactly(self):
        """The rule's identity key is the runtime's own parse, deliberately.

        `int(str(ref).lstrip("#"))` — the parse in `issue_number_for_dispatch`,
        `policy_admission` and `diagnose` — rejects `" #5127 "`, because the
        whitespace precedes the `#` that `lstrip` removes. A node spelled that way
        cannot be routed to an issue at dispatch either, so it is not a competing
        delivery and must not be reported as one. Validation and the transactional
        claim agreeing on identity matters more than accepting one more spelling;
        if that parse is ever widened, this rule follows it.
        """
        from src.orchestration.dispatch_pass import issue_number_for_dispatch
        from src.orchestration.proposal import _work_identity

        for spelling in ("5127", "#5127", " 5127 ", "#5127 ", "##5127", " #5127 ", "not-an-issue", "0", "-3", None):
            runtime = issue_number_for_dispatch(spelling)
            expected = str(runtime) if runtime is not None else None
            assert _work_identity(spelling) == expected, spelling

    def test_a_malformed_issue_ref_is_not_reported_here(self):
        """An unroutable reference is not a shared work identity, and rule 6 is
        not the place to report a malformed one."""
        assert validate_proposal(self._wave("not-an-issue", "not-an-issue", self._to_eval())) == []

    def test_a_dangling_edge_cannot_order_two_nodes(self):
        """Ordering must be established over edges the engine can actually
        follow, matching rule 3's resolvable-edge subgraph."""
        nodes = [
            ProposedNode(address=address("story-a"), kind="story", title="A", issue_ref="5127"),
            ProposedNode(address=address("story-b"), kind="story", title="B", issue_ref="5127"),
            ProposedNode(address=address("eval"), kind="eval", title="Eval", issue_ref="9999"),
        ]
        edges = [ProposedEdge(from_address=address("story-a"), to_address=address("ghost"))]
        found = rules(validate_proposal(make_proposal(nodes=nodes, edges=edges)))
        assert "dangling_edge" in found
        assert "unordered_same_issue" in found

    def test_a_cyclic_document_still_terminates(self):
        """Validation collects every violation, so this rule runs on a document
        rule 3 already flagged as cyclic and must not hang or recurse."""
        nodes = [
            ProposedNode(address=address("story-a"), kind="story", title="A", issue_ref="5127"),
            ProposedNode(address=address("story-b"), kind="story", title="B", issue_ref="5127"),
            ProposedNode(address=address("eval"), kind="eval", title="Eval", issue_ref="9999"),
        ]
        edges = [
            ProposedEdge(from_address=address("story-a"), to_address=address("story-b")),
            ProposedEdge(from_address=address("story-b"), to_address=address("story-a")),
            ProposedEdge(from_address=address("story-a"), to_address=address("eval")),
        ]
        found = rules(validate_proposal(make_proposal(nodes=nodes, edges=edges)))
        assert "cycle" in found
        # The cycle orders them in both directions, so ordering is not the fault.
        assert "unordered_same_issue" not in found

    def test_a_deep_chain_does_not_recurse(self):
        """Author-supplied depth must raise a violation or nothing — never a
        RecursionError. Mirrors rule 3's deep-chain guarantee."""
        nodes = [
            ProposedNode(
                address=address(f"n{index}"),
                kind="story",
                title=f"N{index}",
                issue_ref="5127" if index in (0, 1999) else None,
            )
            for index in range(2000)
        ]
        nodes.append(ProposedNode(address=address("eval"), kind="eval", title="Eval", issue_ref="9999"))
        edges = [ProposedEdge(from_address=address(f"n{index}"), to_address=address(f"n{index + 1}")) for index in range(1999)]
        edges.append(ProposedEdge(from_address=address("n1999"), to_address=address("eval")))
        # The chain orders the two issue-bearing ends, so the plan is valid.
        assert "unordered_same_issue" not in rules(validate_proposal(make_proposal(nodes=nodes, edges=edges)))


class TestRule5Declarations:
    """Rule 5: org_id and spec_revision are declared."""

    def test_missing_org_id_is_rejected_at_parse_time(self):
        with pytest.raises(ValidationError):
            make_proposal(org_id=None)

    def test_blank_org_id_is_a_violation(self):
        """Whitespace satisfies `min_length=1` while carrying no information."""
        assert "missing_org_id" in rules(validate_proposal(make_proposal(org_id="   ")))

    def test_missing_spec_revision_is_rejected_at_parse_time(self):
        with pytest.raises(ValidationError):
            LoopProposal(flow_slug=FLOW, title="T", org_id="org-alpha", nodes=[])

    def test_blank_spec_revision_is_a_violation(self):
        assert "missing_spec_revision" in rules(validate_proposal(make_proposal(spec_revision=" ")))

    def test_empty_plan_is_a_violation(self):
        """An accepted plan that accepted nothing proves nothing."""
        assert "empty_plan" in rules(validate_proposal(make_proposal(nodes=[])))


class TestSchemaShape:
    def test_unknown_field_is_rejected(self):
        """`extra="forbid"` — a typo'd field name must not be silently dropped,
        which would compile a plan missing whatever the author meant to say."""
        with pytest.raises(ValidationError):
            LoopProposal(
                flow_slug=FLOW,
                title="T",
                org_id="org-alpha",
                spec_revision=SPEC_REVISION,
                nodes=[],
                noeds=[],  # typo for `nodes`
            )

    def test_unknown_node_field_is_rejected(self):
        with pytest.raises(ValidationError):
            ProposedNode(address=address("a"), kind="story", title="A", state="passed")

    def test_node_kind_is_a_plain_string_so_containers_reach_the_validator(self):
        """Typing `kind` as the enum would make pydantic reject `wave` with a
        generic parse error and the whole document would fail to load — the
        author would lose every other violation in the same pass."""
        node = ProposedNode(address=address("a"), kind="wave", title="A")
        assert node.kind == "wave"

    def test_round_trips_through_json(self):
        """The document is stored verbatim in `plan_document`, so it must survive
        a serialise/parse cycle unchanged or the stored plan differs from the
        approved one."""
        original = make_proposal()
        assert LoopProposal.model_validate(original.model_dump(mode="json")) == original


class TestViolationRendering:
    def test_str_includes_rule_message_and_location(self):
        rendered = str(Violation(rule="cycle", message="there is a cycle", where="a -> b -> a"))
        assert "cycle" in rendered and "there is a cycle" in rendered and "a -> b -> a" in rendered

    def test_str_omits_the_bracket_when_there_is_no_location(self):
        assert str(Violation(rule="empty_plan", message="no nodes")) == "empty_plan: no nodes"


class TestValidationCollectsEverything:
    def test_all_violations_are_returned_not_just_the_first(self):
        """The advisory CLI's whole value is one pass; stopping at the first
        failure would make an author fix one rule per run."""
        nodes = [
            ProposedNode(address="malformed", kind="wave", title="Bad"),
            ProposedNode(address=address("story-a"), kind="story", title="A"),
            ProposedNode(address=address("story-a"), kind="story", title="A again"),
        ]
        edges = [ProposedEdge(from_address=address("story-a"), to_address=address("ghost"))]
        found = rules(validate_proposal(make_proposal(nodes=nodes, edges=edges, spec_revision=" ")))
        assert {
            "missing_spec_revision",
            "address_form",
            "duplicate_address",
            "container_as_node",
            "dangling_edge",
            "wave_eval_cardinality",
        } <= found


class TestVocabularyReuse:
    def test_initial_node_state_is_the_vocabulary_pending(self):
        """R-N2a: imported from `state.py`, never re-spelled as a literal."""
        assert INITIAL_NODE_STATE is NodeState.PENDING
