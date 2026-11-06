"""Placement resolution: a placement choice becomes explicit edges (#5329).

`src/orchestration/amendment_placement.py` is the translation #4529 left open. Its store
accepts only a finished `LoopProposal`, so "add a cleanup checker after U1, feeding Wave
1's acceptance" had to be hand-written as a whole graph — including every edge the author
did not mean to touch.

What is asserted here, and why each matters:

  - **Edges, not wave labels.** Every placement kind is asserted on its exact resulting
    edge set. This is the whole point of the module: `tick` releases work from edges and
    nothing reads wave membership, so a placement that produced a node with no edges
    would produce a node that runs immediately.
  - **Strictly additive.** No placement removes an in-force edge, and the in-force node
    set is carried through unchanged. Asserted for every kind, because dropping an
    anchor's `-> successor` edge during an `insert_after` is precisely how inserted work
    releases downstream work early.
  - **Ambiguity is refused, not guessed.** A parallel branch with no rejoin, a missing
    anchor, an anchor that is not on the plan, a collision with an existing address: each
    returns a named blocker and **no document**. A partially resolved document is what an
    unreviewed approval is made of.
  - **The resolved document passes the authoritative validator.** Asserted by calling the
    real `validate_proposal`, not by re-checking the rules here — a resolver whose output
    the acceptance path refuses is worse than one that refuses up front.

The Superplane-shaped fixture (`superplane_plan`) is the validation table's scenario: two
passed code stories, a wave eval, a deploy gate in a later wave, and follow-up issues that
exist in GitHub but are not on the graph. It is used for the adoption cases so those are
exercised against the shape they were specified against rather than a minimal toy.
"""

from src.orchestration.amendment_placement import (
    PlacementKind,
    PlacementRequest,
    ProposedPlacement,
    ProposedWork,
    resolve_placement,
)
from src.orchestration.proposal import LoopProposal, NodeLineage, ProposedEdge, ProposedNode, validate_proposal

FLOW = "superplane"
SPEC = "issue-5329-r1"


def node(ref: str, kind: str, *, epic: str = "epic-1", wave: str = "wave-1", issue: str | None = None) -> ProposedNode:
    return ProposedNode(address=f"{FLOW}/{epic}/{wave}/{ref}", kind=kind, title=f"{kind} {ref}", issue_ref=issue)


def at(ref: str, *, epic: str = "epic-1", wave: str = "wave-1") -> str:
    return f"{FLOW}/{epic}/{wave}/{ref}"


def superplane_plan() -> LoopProposal:
    """The in-force plan: Wave 1 delivers two stories and evaluates them; Wave 2 gates deploy.

    Shaped after the validation table's fixture rather than minimally, so the adoption
    cases below exercise a real wave-with-eval and a later gate that insertion must be
    shown to affect.
    """
    return LoopProposal(
        flow_slug=FLOW,
        title="Superplane delivery",
        org_id="org-alpha",
        spec_revision=SPEC,
        intent_ref="4191",
        description="Deliver the Superplane control surface.",
        nodes=[
            node("u1", "story", issue="5301"),
            node("u2", "story", issue="5302"),
            node("wave-1-acceptance", "eval"),
            node("deploy-gate", "gate", wave="wave-2"),
        ],
        edges=[
            ProposedEdge(from_address=at("u1"), to_address=at("wave-1-acceptance")),
            ProposedEdge(from_address=at("u2"), to_address=at("wave-1-acceptance")),
            ProposedEdge(from_address=at("wave-1-acceptance"), to_address=at("deploy-gate", wave="wave-2")),
        ],
    )


def pairs(resolution) -> set[tuple[str, str]]:
    """The added edges as bare pairs, for asserting the exact resulting set."""
    return {(edge.from_address, edge.to_address) for edge in resolution.added_edges}


def document_pairs(resolution) -> set[tuple[str, str]]:
    """Every edge on the resolved document, added or inherited."""
    return {(edge.from_address, edge.to_address) for edge in resolution.document.edges}


def request_for(kind: PlacementKind, **placement) -> PlacementRequest:
    """The issue's own example: implement a cleanup checker, then validate with it."""
    return PlacementRequest(
        work=[
            ProposedWork(ref="cleanup-checker", kind="story", title="Implement cleanup checker", issue_ref="5410"),
            ProposedWork(ref="cleanup-validation", kind="story", title="Execute cleanup validation", issue_ref="5411"),
        ],
        placement=ProposedPlacement(kind=kind, **placement),
    )


class TestAdditive:
    """No placement kind removes anything. The direction that matters for safety."""

    def test_new_work_preserves_lineage_and_labels(self):
        base = superplane_plan()
        request = request_for(PlacementKind.INSERT_AFTER, anchor=at("u1"))
        work = request.work[0].model_copy(update={"lineage": NodeLineage(story_id="EI-S02"), "labels": {"source": "issue"}})
        request = request.model_copy(update={"work": [work, *request.work[1:]]})
        resolution = resolve_placement(base=base, request=request)
        added = next(node for node in resolution.document.nodes if node.address.endswith("/cleanup-checker"))
        assert added.lineage.story_id == "EI-S02"
        assert added.labels == {"source": "issue"}

    def test_every_in_force_node_and_edge_survives_every_placement_kind(self):
        """Carried through unchanged, so nothing is superseded by adding work.

        `amend_plan` supersedes a node by its *absence* from the document. A resolver that
        dropped a node while inserting work would therefore supersede it silently, and a
        reviewer reading "2 nodes added" would not be told that one was removed.
        """
        base = superplane_plan()
        before_nodes = {item.address for item in base.nodes}
        before_edges = {(edge.from_address, edge.to_address) for edge in base.edges}

        for request in (
            request_for(PlacementKind.INSERT_BEFORE, anchor=at("wave-1-acceptance")),
            request_for(PlacementKind.INSERT_AFTER, anchor=at("u1")),
            request_for(PlacementKind.PARALLEL_BRANCH, anchor=at("u1"), rejoin_at=[at("wave-1-acceptance")]),
            request_for(PlacementKind.EXISTING_WAVE, epic_ref="epic-1", wave_ref="wave-1"),
            request_for(PlacementKind.NEW_WAVE, epic_ref="epic-1", wave_ref="wave-3"),
        ):
            resolution = resolve_placement(base=base, request=request)
            assert resolution.resolved, (request.placement.kind, resolution.blockers)
            assert before_nodes <= {item.address for item in resolution.document.nodes}
            assert before_edges <= document_pairs(resolution), f"{request.placement.kind} dropped an in-force edge"

    def test_insert_after_does_not_reparent_the_anchors_successors(self):
        """The anchor's own edge to its successor stays.

        Re-parenting is the early-release bug: if `u1 -> wave-1-acceptance` were deleted
        and replaced with `cleanup-validation -> wave-1-acceptance`, the eval would no
        longer wait for `u1` at all. Additive insertion means it waits for both.
        """
        resolution = resolve_placement(base=superplane_plan(), request=request_for(PlacementKind.INSERT_AFTER, anchor=at("u1")))

        assert (at("u1"), at("wave-1-acceptance")) in document_pairs(resolution)
        assert (at("cleanup-validation"), at("wave-1-acceptance")) in pairs(resolution)

    def test_insert_before_leaves_the_anchors_existing_prerequisites_in_place(self):
        """The anchor gains a prerequisite; it does not trade its old ones for the new work."""
        resolution = resolve_placement(base=superplane_plan(), request=request_for(PlacementKind.INSERT_BEFORE, anchor=at("wave-1-acceptance")))

        incoming = {source for source, target in document_pairs(resolution) if target == at("wave-1-acceptance")}
        assert incoming == {at("u1"), at("u2"), at("cleanup-validation")}


class TestPlacementKinds:
    """Each kind's exact resulting edge set."""

    def test_insert_before_runs_between_the_anchors_predecessors_and_the_anchor(self):
        resolution = resolve_placement(base=superplane_plan(), request=request_for(PlacementKind.INSERT_BEFORE, anchor=at("wave-1-acceptance")))

        assert pairs(resolution) == {
            (at("u1"), at("cleanup-checker")),
            (at("u2"), at("cleanup-checker")),
            (at("cleanup-checker"), at("cleanup-validation")),
            (at("cleanup-validation"), at("wave-1-acceptance")),
        }

    def test_insert_after_makes_the_anchors_successors_wait_for_the_chain(self):
        resolution = resolve_placement(base=superplane_plan(), request=request_for(PlacementKind.INSERT_AFTER, anchor=at("u1")))

        assert pairs(resolution) == {
            (at("u1"), at("cleanup-checker")),
            (at("cleanup-checker"), at("cleanup-validation")),
            (at("cleanup-validation"), at("wave-1-acceptance")),
        }

    def test_parallel_branch_shares_the_anchors_predecessors_and_never_orders_against_it(self):
        """Concurrency is the *absence* of an edge to the anchor, which is worth asserting.

        A branch that gained `u1 -> cleanup-checker` would be an `insert_after` wearing a
        different name, and the reviewer would be told two things run at once when they do
        not.
        """
        base = superplane_plan()
        resolution = resolve_placement(
            base=base,
            request=request_for(PlacementKind.PARALLEL_BRANCH, anchor=at("u2"), rejoin_at=[at("wave-1-acceptance")]),
        )

        assert pairs(resolution) == {
            (at("cleanup-checker"), at("cleanup-validation")),
            (at("cleanup-validation"), at("wave-1-acceptance")),
        }
        assert (at("u2"), at("cleanup-checker")) not in document_pairs(resolution)

    def test_parallel_branch_inherits_predecessors_when_the_anchor_has_them(self):
        resolution = resolve_placement(
            base=superplane_plan(),
            request=request_for(PlacementKind.PARALLEL_BRANCH, anchor=at("wave-1-acceptance"), rejoin_at=[at("deploy-gate", wave="wave-2")]),
        )

        assert pairs(resolution) == {
            (at("u1"), at("cleanup-checker")),
            (at("u2"), at("cleanup-checker")),
            (at("cleanup-checker"), at("cleanup-validation")),
            (at("cleanup-validation"), at("deploy-gate", wave="wave-2")),
        }

    def test_existing_wave_concludes_at_that_waves_eval(self):
        """ "Counts towards this wave's acceptance" in edge terms.

        Note it derives no *prerequisite*: a wave name states no order. The chain is
        eligible immediately unless an item declares `requires`, and the impact report
        says so rather than this module inventing a predecessor.
        """
        resolution = resolve_placement(base=superplane_plan(), request=request_for(PlacementKind.EXISTING_WAVE, epic_ref="epic-1", wave_ref="wave-1"))

        assert pairs(resolution) == {
            (at("cleanup-checker"), at("cleanup-validation")),
            (at("cleanup-validation"), at("wave-1-acceptance")),
        }

    def test_new_wave_derives_nothing_and_takes_both_ends_explicitly(self):
        """A new wave has no anchor to read an order from, so guessing one would be invention."""
        request = PlacementRequest(
            work=[
                ProposedWork(ref="cleanup-checker", kind="story", title="Implement cleanup checker", requires=[at("wave-1-acceptance")]),
                ProposedWork(ref="cleanup-eval", kind="eval", title="Cleanup wave eval", precedes=[at("deploy-gate", wave="wave-2")]),
            ],
            placement=ProposedPlacement(kind=PlacementKind.NEW_WAVE, epic_ref="epic-1", wave_ref="wave-3"),
        )
        resolution = resolve_placement(base=superplane_plan(), request=request)

        assert pairs(resolution) == {
            (at("wave-1-acceptance"), at("cleanup-checker", wave="wave-3")),
            (at("cleanup-checker", wave="wave-3"), at("cleanup-eval", wave="wave-3")),
            (at("cleanup-eval", wave="wave-3"), at("deploy-gate", wave="wave-2")),
        }


class TestAddressDerivation:
    def test_anchored_placements_take_epic_and_wave_from_the_anchor(self):
        """So a placement naming Wave 1 cannot produce an address in Wave 4."""
        resolution = resolve_placement(
            base=superplane_plan(),
            request=request_for(PlacementKind.INSERT_AFTER, anchor=at("deploy-gate", wave="wave-2")),
        )

        assert resolution.added_nodes == [at("cleanup-checker", wave="wave-2"), at("cleanup-validation", wave="wave-2")]

    def test_naming_a_wave_on_an_anchored_placement_is_refused_rather_than_ignored(self):
        """An ignored argument is a caller believing something untrue about the result."""
        request = request_for(PlacementKind.INSERT_AFTER, anchor=at("u1"), epic_ref="epic-9", wave_ref="wave-9")
        resolution = resolve_placement(base=superplane_plan(), request=request)

        assert resolution.document is None
        assert [item.rule for item in resolution.blockers] == ["placement_wave_not_applicable"]


class TestAmbiguityIsRefused:
    """Every one of these returns no document. A guessed placement is an unreviewed plan."""

    def test_parallel_branch_without_a_rejoin_is_refused(self):
        """The issue's named ambiguity case.

        Without a rejoin nothing downstream waits for the branch, so neither its result
        nor its failure can affect the plan — a materially different proposal from the one
        the author believes they made.
        """
        resolution = resolve_placement(base=superplane_plan(), request=request_for(PlacementKind.PARALLEL_BRANCH, anchor=at("u1")))

        assert resolution.document is None
        assert [item.rule for item in resolution.blockers] == ["placement_rejoin_required"]

    def test_missing_anchor_is_refused(self):
        resolution = resolve_placement(base=superplane_plan(), request=request_for(PlacementKind.INSERT_AFTER))

        assert resolution.document is None
        assert [item.rule for item in resolution.blockers] == ["placement_anchor_required"]

    def test_an_anchor_absent_from_the_plan_in_force_is_refused(self):
        """A stale address from an older plan version and a typo need different answers,
        so the blocker states the fact rather than suggesting a correction."""
        resolution = resolve_placement(base=superplane_plan(), request=request_for(PlacementKind.INSERT_AFTER, anchor=at("nonexistent")))

        assert resolution.document is None
        assert [item.rule for item in resolution.blockers] == ["placement_anchor_unknown"]

    def test_colliding_with_an_existing_address_is_refused_rather_than_redefining_it(self):
        """`amend_plan` keeps a same-address node's row, state and history.

        So a collision would *redefine* live work rather than add any — a legitimate
        operation with different reviewable consequences, and not something a placement
        may do by accident.
        """
        request = PlacementRequest(
            work=[ProposedWork(ref="u1", kind="story", title="Implement cleanup checker")],
            placement=ProposedPlacement(kind=PlacementKind.EXISTING_WAVE, epic_ref="epic-1", wave_ref="wave-1"),
        )
        resolution = resolve_placement(base=superplane_plan(), request=request)

        assert resolution.document is None
        assert [item.rule for item in resolution.blockers] == ["placement_address_taken"]

    def test_new_wave_onto_an_occupied_wave_is_refused(self):
        resolution = resolve_placement(base=superplane_plan(), request=request_for(PlacementKind.NEW_WAVE, epic_ref="epic-1", wave_ref="wave-1"))

        assert resolution.document is None
        assert [item.rule for item in resolution.blockers] == ["placement_wave_exists"]

    def test_an_unresolvable_declared_prerequisite_is_refused(self):
        request = PlacementRequest(
            work=[ProposedWork(ref="cleanup-validation", kind="story", title="Validate", requires=[at("never-existed")])],
            placement=ProposedPlacement(kind=PlacementKind.EXISTING_WAVE, epic_ref="epic-1", wave_ref="wave-1"),
        )
        resolution = resolve_placement(base=superplane_plan(), request=request)

        assert resolution.document is None
        assert [item.rule for item in resolution.blockers] == ["placement_prerequisite_unknown"]

    def test_an_unknown_rejoin_target_is_refused(self):
        resolution = resolve_placement(
            base=superplane_plan(),
            request=request_for(PlacementKind.PARALLEL_BRANCH, anchor=at("u1"), rejoin_at=[at("never-existed")]),
        )

        assert resolution.document is None
        assert [item.rule for item in resolution.blockers] == ["placement_rejoin_unknown"]

    def test_all_placement_problems_are_reported_together(self):
        """One round trip per fix would make a three-problem request three reviews."""
        request = PlacementRequest(
            work=[
                ProposedWork(ref="u1", kind="story", title="Collides with an existing node"),
                ProposedWork(ref="validation", kind="story", title="Validate", requires=[at("nope")], precedes=[at("also-nope")]),
            ],
            placement=ProposedPlacement(kind=PlacementKind.EXISTING_WAVE, epic_ref="epic-1", wave_ref="wave-1"),
        )
        resolution = resolve_placement(base=superplane_plan(), request=request)

        assert resolution.document is None
        assert {item.rule for item in resolution.blockers} == {
            "placement_address_taken",
            "placement_prerequisite_unknown",
            "placement_successor_unknown",
        }

    def test_an_unresolved_anchor_defers_the_prerequisite_check_rather_than_crashing(self):
        """With no resolvable anchor there are no addresses yet, so a prerequisite cannot be
        checked against them. The anchor blocker is the answer the caller can act on; the
        prerequisites are re-checked on the next attempt once the addresses exist.

        Asserted because the alternative — pairing work items against an empty address list
        — raises, and the caller would get a 500 instead of the placement problem.
        """
        request = PlacementRequest(
            work=[ProposedWork(ref="x", kind="story", title="X", requires=[at("nope")])],
            placement=ProposedPlacement(kind=PlacementKind.PARALLEL_BRANCH, anchor=at("also-nope")),
        )
        resolution = resolve_placement(base=superplane_plan(), request=request)

        assert resolution.document is None
        assert {item.rule for item in resolution.blockers} == {"placement_anchor_unknown", "placement_rejoin_required"}


class TestExplicitDependencies:
    def test_a_sibling_in_the_same_chain_resolves_as_a_prerequisite(self):
        """ "Validation waits for the implementation proposed beside it" needs no second placement."""
        request = PlacementRequest(
            work=[
                ProposedWork(ref="cleanup-checker", kind="story", title="Implement"),
                ProposedWork(ref="cleanup-validation", kind="story", title="Validate", requires=[at("cleanup-checker")]),
            ],
            placement=ProposedPlacement(kind=PlacementKind.EXISTING_WAVE, epic_ref="epic-1", wave_ref="wave-1"),
        )
        resolution = resolve_placement(base=superplane_plan(), request=request)

        assert resolution.resolved
        assert (at("cleanup-checker"), at("cleanup-validation")) in pairs(resolution)

    def test_a_declared_prerequisite_and_a_derived_one_are_reported_once(self):
        """`upsert_edges` would accept the duplicate, but the preview would show one
        dependency twice — reading as two separate reasons a node is blocked."""
        request = PlacementRequest(
            work=[
                ProposedWork(ref="cleanup-checker", kind="story", title="Implement"),
                # Exactly the edge the declaration-order chain already derives.
                ProposedWork(ref="cleanup-validation", kind="story", title="Validate", requires=[at("cleanup-checker")]),
            ],
            placement=ProposedPlacement(kind=PlacementKind.EXISTING_WAVE, epic_ref="epic-1", wave_ref="wave-1"),
        )
        resolution = resolve_placement(base=superplane_plan(), request=request)

        chain_edge = [
            edge for edge in resolution.added_edges if (edge.from_address, edge.to_address) == (at("cleanup-checker"), at("cleanup-validation"))
        ]
        assert len(chain_edge) == 1

    def test_live_input_prerequisites_are_carried_onto_the_validation_node(self):
        """The validation table's case: validation waits on the checker *and* on authorized
        live inputs, so the checker can proceed while validation shows its precise block."""
        base = superplane_plan().model_copy(
            update={
                "nodes": [*superplane_plan().nodes, node("live-inputs", "gate", wave="wave-2")],
                "edges": list(superplane_plan().edges),
            }
        )
        request = PlacementRequest(
            work=[
                ProposedWork(ref="cleanup-checker", kind="story", title="Implement cleanup checker"),
                ProposedWork(
                    ref="cleanup-validation",
                    kind="story",
                    title="Execute cleanup validation",
                    requires=[at("live-inputs", wave="wave-2")],
                ),
            ],
            placement=ProposedPlacement(kind=PlacementKind.EXISTING_WAVE, epic_ref="epic-1", wave_ref="wave-1"),
        )
        resolution = resolve_placement(base=base, request=request)

        assert resolution.resolved
        incoming = {source for source, target in document_pairs(resolution) if target == at("cleanup-validation")}
        assert incoming == {at("cleanup-checker"), at("live-inputs", wave="wave-2")}
        # And the checker itself is not behind the withheld input.
        checker_incoming = {source for source, target in document_pairs(resolution) if target == at("cleanup-checker")}
        assert at("live-inputs", wave="wave-2") not in checker_incoming


class TestResolvedDocumentIsAcceptable:
    def test_the_resolved_document_passes_the_authoritative_validator(self):
        """Called for real, not re-checked here.

        A resolver whose output `amend_plan` refuses is worse than one that refuses up
        front: the reviewer would see a preview, approve it, and get a rejection.
        """
        for request in (
            request_for(PlacementKind.INSERT_BEFORE, anchor=at("wave-1-acceptance")),
            request_for(PlacementKind.INSERT_AFTER, anchor=at("u1")),
            request_for(PlacementKind.PARALLEL_BRANCH, anchor=at("u1"), rejoin_at=[at("wave-1-acceptance")]),
            request_for(PlacementKind.EXISTING_WAVE, epic_ref="epic-1", wave_ref="wave-1"),
        ):
            resolution = resolve_placement(base=superplane_plan(), request=request)
            assert resolution.resolved, resolution.blockers
            assert validate_proposal(resolution.document) == [], f"{request.placement.kind} produced an invalid document"

    def test_tenant_flow_and_spec_revision_are_inherited_from_the_plan_in_force(self):
        """Not re-declared by the request. `amend_plan` compares the document's `org_id`
        and `flow_slug` against the flow being amended, so a request that could set them
        would be a request that could aim a proposal at another tenant's flow."""
        base = superplane_plan()
        resolution = resolve_placement(base=base, request=request_for(PlacementKind.INSERT_AFTER, anchor=at("u1")))

        assert resolution.document.org_id == base.org_id
        assert resolution.document.flow_slug == base.flow_slug
        assert resolution.document.spec_revision == base.spec_revision

    def test_the_stated_purpose_lands_on_the_document_description(self):
        request = PlacementRequest(
            work=[ProposedWork(ref="cleanup-checker", kind="story", title="Implement")],
            placement=ProposedPlacement(kind=PlacementKind.EXISTING_WAVE, epic_ref="epic-1", wave_ref="wave-1"),
            purpose="Close the cleanup gap #5320 found.",
        )
        resolution = resolve_placement(base=superplane_plan(), request=request)

        assert resolution.document.description == "Close the cleanup gap #5320 found."

    def test_omitting_a_purpose_keeps_the_in_force_description(self):
        resolution = resolve_placement(base=superplane_plan(), request=request_for(PlacementKind.INSERT_AFTER, anchor=at("u1")))

        assert resolution.document.description == "Deliver the Superplane control surface."


class TestEvalCardinalityIsNotBypassed:
    def test_adding_a_second_eval_to_a_wave_that_has_one_resolves_but_fails_validation(self):
        """Resolution succeeds; the authoritative validator refuses it. That split is deliberate.

        This module does not own rule 4, so it must not pre-empt it — a resolver with its
        own opinion on eval cardinality could accept a document `amend_plan` refuses, or
        refuse one it would accept. The preview's job is to surface this violation, and
        `test_amendment_impact.py` asserts that it does.
        """
        request = PlacementRequest(
            work=[ProposedWork(ref="extra-eval", kind="eval", title="A second eval")],
            placement=ProposedPlacement(kind=PlacementKind.EXISTING_WAVE, epic_ref="epic-1", wave_ref="wave-1"),
        )
        resolution = resolve_placement(base=superplane_plan(), request=request)

        assert resolution.resolved
        assert [item.rule for item in validate_proposal(resolution.document)] == ["wave_eval_cardinality"]

    def test_a_read_only_validation_step_is_a_story_and_needs_no_second_eval(self):
        """Following #5301, a code story completes on a verified merged PR — so a read-only
        validation task must not be *represented* as one needing a fabricated PR, and must
        not be smuggled in as an extra eval either. It is a story whose completion comes
        from observed evidence, and the wave's single existing eval still concludes it."""
        request = PlacementRequest(
            work=[ProposedWork(ref="cleanup-validation", kind="story", title="Execute cleanup validation", issue_ref="5411")],
            placement=ProposedPlacement(kind=PlacementKind.EXISTING_WAVE, epic_ref="epic-1", wave_ref="wave-1"),
        )
        resolution = resolve_placement(base=superplane_plan(), request=request)

        assert resolution.resolved
        assert validate_proposal(resolution.document) == []
        assert pairs(resolution) == {(at("cleanup-validation"), at("wave-1-acceptance"))}


class TestIssueAdoption:
    def test_an_existing_follow_up_issue_is_adopted_by_reference(self):
        """Nothing is created by resolving a placement. Adoption is naming the issue on
        the node, which is what makes retries unable to produce duplicate issues: the
        reference is an input, not a side effect."""
        resolution = resolve_placement(
            base=superplane_plan(),
            request=PlacementRequest(
                work=[ProposedWork(ref="cleanup-checker", kind="story", title="Implement cleanup checker", issue_ref="5410")],
                placement=ProposedPlacement(kind=PlacementKind.EXISTING_WAVE, epic_ref="epic-1", wave_ref="wave-1"),
            ),
        )

        added = [item for item in resolution.document.nodes if item.address == at("cleanup-checker")]
        assert [item.issue_ref for item in added] == ["5410"]

    def test_reusing_an_in_force_issue_without_an_order_fails_the_authoritative_validator(self):
        """Rule 6: two nodes on one issue with no order between them says "do this issue
        twice, independently, at once". Reported by the validator, surfaced by the preview."""
        resolution = resolve_placement(
            base=superplane_plan(),
            request=PlacementRequest(
                # `u1` already carries issue 5301, and an existing-wave placement derives
                # no edge between them.
                work=[ProposedWork(ref="u1-repair", kind="story", title="Repair what the eval found", issue_ref="5301")],
                placement=ProposedPlacement(kind=PlacementKind.EXISTING_WAVE, epic_ref="epic-1", wave_ref="wave-1"),
            ),
        )

        assert resolution.resolved
        assert [item.rule for item in validate_proposal(resolution.document)] == ["unordered_same_issue"]


class TestDeterminism:
    def test_resolution_is_stable_across_runs(self):
        """A resolver that varied its edge order would change `plan_hash`, and the hash is
        what binds an approval to the revision the reviewer read."""
        request = request_for(PlacementKind.INSERT_BEFORE, anchor=at("wave-1-acceptance"))
        first = resolve_placement(base=superplane_plan(), request=request)
        second = resolve_placement(base=superplane_plan(), request=request)

        assert first.document.model_dump(mode="json") == second.document.model_dump(mode="json")
        assert [(e.from_address, e.to_address, e.reason) for e in first.added_edges] == [
            (e.from_address, e.to_address, e.reason) for e in second.added_edges
        ]


class TestEdgeRationale:
    def test_every_added_edge_carries_a_reason(self):
        """The preview explains *why* Wave 2's gate now waits, which the graph cannot show."""
        resolution = resolve_placement(base=superplane_plan(), request=request_for(PlacementKind.INSERT_AFTER, anchor=at("u1")))

        assert all(edge.reason for edge in resolution.added_edges)

    def test_an_inherited_prerequisite_names_the_anchor_it_came_from(self):
        """Otherwise a reviewer sees an edge from a node the proposal never mentioned."""
        resolution = resolve_placement(base=superplane_plan(), request=request_for(PlacementKind.INSERT_BEFORE, anchor=at("wave-1-acceptance")))

        inherited = [edge for edge in resolution.added_edges if edge.from_address == at("u1")]
        assert len(inherited) == 1
        assert at("wave-1-acceptance") in inherited[0].reason
