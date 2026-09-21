"""`POST /orchestration/flows/drafts/preview` — the effective plan, written nowhere.

Issue #5331 (EPIC #4191). An operator cannot meaningfully approve a plan they have
not seen, and the graph that executes is not the document an author wrote. This
route answers "what would registering this produce" without producing it.

Three properties carry the route, and they fail differently, so they are tested
separately:

* :class:`TestEffectiveGraph` — the answer is the *transformed* graph. The
  server-inserted acceptance gate is present and flagged as the server's, because a
  preview that omitted it would show the operator a plan that cannot execute, and one
  that showed it unflagged would attribute the server's gate to the author.
* :class:`TestWritesNothing` — the safety property. No rows, no authority, no
  session. Asserted against real tables via `assert_graph_is_empty`, and
  structurally against the route's own signature.
* :class:`TestPlanHashBinding` — which previewed hash may be passed back as
  `expected_plan_hash` and which may not. The asymmetry is a real trap (acceptance
  re-stamps a policy with the acceptor's principal *before* hashing), so it is
  pinned rather than described.

Reuses `test_registration.py`'s harness for the same reason `test_draft_binding.py`
does: an assertion about this route made against a *different* app fixture is an
assertion about the fixture.
"""

from __future__ import annotations

import inspect as py_inspect

import pytest

from src.orchestration.compile import plan_hash
from src.orchestration.execution_policy import stamp_policy
from src.orchestration.registration import transform_for_registration
from tests.orchestration import test_registration as _reg
from tests.orchestration.test_execution_policy_acceptance import a_policy

# Rebound as module attributes rather than imported by name: pytest collects
# fixtures from a test module's namespace either way, while `from ... import session`
# shadows the name and trips ruff's F811.
session = _reg.session
app_with_router = _reg.app_with_router
autonomy_default_unset = _reg.autonomy_default_unset

ORG_A = _reg.ORG_A
gateless_proposal = _reg.gateless_proposal
author_gated_proposal = _reg.author_gated_proposal
assert_graph_is_empty = _reg.assert_graph_is_empty
client_for = _reg.client_for

ROUTE = "/orchestration/flows/drafts/preview"


def preview(app, proposal, *, permitted: bool = True):
    """POST a proposal to the preview route and return the raw response."""
    with client_for(app, permitted=permitted) as client:
        return client.post(ROUTE, json=proposal.model_dump(mode="json"))


class TestEffectiveGraph:
    """The preview reports what would execute, not what was authored."""

    def test_the_server_inserted_acceptance_gate_is_shown_and_attributed(self, app_with_router, autonomy_default_unset):
        """The gate the author did not write is present, and marked as not theirs.

        This is the entire reason the route exists. `gateless_proposal` has no gate
        anywhere; `transform_for_registration` inserts one dominating every root. An
        operator reviewing the authored document would never see it.
        """
        response = preview(app_with_router, gateless_proposal())
        assert response.status_code == 200
        body = response.json()

        gate_address = body["acceptance_gate_address"]
        gates = [node for node in body["nodes"] if node["kind"] == "gate"]
        assert [gate["address"] for gate in gates] == [gate_address], "the inserted acceptance gate should be the only gate"
        assert gates[0]["inserted_by_server"] is True

        # The author's own nodes must NOT be flagged, or the flag says nothing.
        authored = [node for node in body["nodes"] if node["address"] != gate_address]
        assert authored, "fixture should contribute nodes"
        assert all(node["inserted_by_server"] is False for node in authored)

    def test_an_authors_own_gate_is_not_attributed_to_the_server(self, app_with_router, autonomy_default_unset):
        """A gate the author wrote is reported as theirs.

        The complement of the test above: if `inserted_by_server` were derived from
        `kind == "gate"` rather than from what the author actually submitted, this
        would fail. Without it, both tests pass under that wrong implementation.
        """
        response = preview(app_with_router, author_gated_proposal())
        body = response.json()

        by_address = {node["address"]: node for node in body["nodes"]}
        authors_gate = next(address for address in by_address if address.endswith("/my-gate"))
        assert by_address[authors_gate]["inserted_by_server"] is False

    def test_edges_the_server_adds_are_flagged_too(self, app_with_router, autonomy_default_unset):
        """The inserted gate is wired in, and its wiring is the server's as well.

        A node flagged as inserted whose edges were not would leave an operator
        unable to see *where* in their graph the server placed the gate.
        """
        response = preview(app_with_router, gateless_proposal())
        body = response.json()
        gate_address = body["acceptance_gate_address"]

        inserted = [edge for edge in body["edges"] if edge["inserted_by_server"]]
        assert inserted, "wiring the gate in requires at least one new edge"
        assert all(gate_address in (edge["from_address"], edge["to_address"]) for edge in inserted), (
            "every server-added edge should touch the server-added gate"
        )

    def test_an_invalid_document_reports_its_violations_instead_of_a_422(self, app_with_router, autonomy_default_unset):
        """A rejected document's violations ARE the useful answer.

        A 422 could not carry the effective graph alongside them, and an author
        fixing problems one status code at a time is the workflow this replaces.
        """
        # An edge to a node that does not exist: a graph-level violation, so it
        # survives `LoopProposal`'s own field validation and reaches `validate_proposal`.
        broken = gateless_proposal()
        broken = broken.model_copy(
            update={"edges": [edge.model_copy(update={"to_address": f"{_reg.FLOW}/epic-1/wave-1/nonexistent"}) for edge in broken.edges[:1]]}
        )

        response = preview(app_with_router, broken)
        assert response.status_code == 200
        body = response.json()
        assert body["would_register"] is False
        assert body["violations"], "a rejected document must say why"
        assert any("nonexistent" in violation for violation in body["violations"])

    def test_violations_do_not_render_a_literal_none_for_document_level_problems(self, app_with_router, autonomy_default_unset):
        """`Violation.where` is optional; formatting it by hand prints "None: ".

        Cheap to assert and easy to regress, because the natural way to write this
        rendering is an f-string over the three fields.
        """
        broken = gateless_proposal()
        broken = broken.model_copy(
            update={"edges": [edge.model_copy(update={"to_address": f"{_reg.FLOW}/epic-1/wave-1/nonexistent"}) for edge in broken.edges[:1]]}
        )
        body = preview(app_with_router, broken).json()
        assert not any(violation.startswith("None") or ": None" in violation for violation in body["violations"])

    def test_a_valid_document_would_register(self, app_with_router, autonomy_default_unset):
        body = preview(app_with_router, gateless_proposal()).json()
        assert body["would_register"] is True
        assert body["violations"] == []

    def test_a_caller_without_plan_draft_is_refused(self, app_with_router, autonomy_default_unset):
        """Gated identically to `register_draft`: this shows what registering would do."""
        response = preview(app_with_router, gateless_proposal(), permitted=False)
        assert response.status_code == 403


class TestWritesNothing:
    """The safety property. A preview grants nothing and stores nothing."""

    @pytest.mark.asyncio
    async def test_previewing_writes_no_rows_at_all(self, app_with_router, session, autonomy_default_unset):
        """Zero rows in every table a registration would have written.

        Includes `OrchestrationAcceptedPlan` and `OrchestrationDecision`, which is
        the point: a stored plan row is what `load_in_force_policy` reads as
        authority, and a decision row is what roots a dispatch.
        """
        assert preview(app_with_router, gateless_proposal()).status_code == 200
        await assert_graph_is_empty(session)

    @pytest.mark.asyncio
    async def test_previewing_a_policy_bearing_document_grants_no_authority(self, app_with_router, session, autonomy_default_unset):
        """The safety-critical case: a policy can be DISPLAYED without being ACCEPTED.

        Registration refuses this document (`accept_execution_policy` will not let a
        SERVICE actor accept a policy), so the preview is the only way an operator
        sees the bounds before consenting to them. It must show them while creating
        no grant — no accepted-plan row for `load_in_force_policy` to read, and no
        stamped policy.
        """
        response = preview(app_with_router, gateless_proposal(execution_policy=a_policy()))
        assert response.status_code == 200
        body = response.json()

        # The bounds are visible...
        assert body["proposed_execution_policy"] is not None
        assert body["proposed_execution_policy"]["repository_ids"], "an operator must be able to see what the plan may touch"
        # ...and inert. `wrote_nothing` is on the wire so the fact survives into a
        # client's own output rather than living only in this test.
        assert body["wrote_nothing"] is True
        await assert_graph_is_empty(session)

    @pytest.mark.asyncio
    async def test_a_policyless_document_reports_no_policy_rather_than_an_empty_one(self, app_with_router, session, autonomy_default_unset):
        """`None` means legacy unbounded semantics — a real state, not "authorizes nothing".

        An empty summary here would read as "bounded to nothing", which is the
        opposite of what a policyless plan means.
        """
        body = preview(app_with_router, gateless_proposal()).json()
        assert body["proposed_execution_policy"] is None

    def test_the_route_takes_no_database_session(self):
        """Structural, not promissory: there is no `get_db` dependency to misuse.

        A docstring saying "writes nothing" is only as good as every future edit
        under it. A route with no session cannot create a flow, stamp a policy or
        supersede a plan even if the body gets it wrong — so the guarantee is pinned
        to the signature rather than to reviewer discipline.
        """
        from src.orchestration.draft_routes import preview_draft
        from src.shared.database import get_db

        parameters = py_inspect.signature(preview_draft).parameters
        dependencies = [str(parameter.annotation) for parameter in parameters.values()]
        assert not any("get_db" in annotation for annotation in dependencies), f"preview must take no DB session; found {dependencies}"
        # Belt and braces: the module may import `get_db` for `register_draft`, so
        # assert on what THIS route asks for, and that the name resolves at all (a
        # typo'd string above would otherwise pass vacuously).
        assert get_db is not None


class TestPlanHashBinding:
    """Which previewed hash is bindable, and which is a trap."""

    def test_a_policyless_previews_hash_is_what_registration_puts_in_force(self, app_with_router, autonomy_default_unset):
        """So an acceptance bound to it is not refused as stale.

        Computed over the TRANSFORMED document, matching
        `register_draft_proposal`. Hashing the authored document instead would hand
        the operator a revision the server refuses.
        """
        proposal = gateless_proposal()
        body = preview(app_with_router, proposal).json()

        transformed, _gate = transform_for_registration(proposal)
        assert body["plan_hash"] == plan_hash(transformed)
        assert body["plan_hash"] != plan_hash(proposal), "hashing the authored document would be the bug"
        assert body["plan_hash_is_bindable"] is True

    def test_a_policy_bearing_previews_hash_is_now_bindable_too(self, app_with_router, autonomy_default_unset):
        """Because the draft path stamps nothing — it demotes the policy (#5331).

        This reverses the pre-#5331 property, and the reversal is the point of that
        change rather than a relaxation of it. Previously `compile_proposal` stamped a
        submitted policy at Gate 2a *before* hashing, so the in-force hash was a
        function of WHO accepted and could not be known beforehand by anyone — a
        policy-bearing plan was therefore the one kind of plan that could NOT be bound
        to an exact reviewed revision. The plans carrying the most delegated authority
        had the weakest approval guarantee.

        `transform_for_registration` now demotes a submitted policy into
        `proposed_execution_policy`, which nothing stamps and which
        `policy_admission.load_in_force_policy` contains no code to read. So the
        registered document is exactly what this preview hashed, the hash is bindable,
        and `--expect-plan-hash` covers the case it previously could not.
        """
        proposal = gateless_proposal(execution_policy=a_policy())
        body = preview(app_with_router, proposal).json()

        transformed, _gate = transform_for_registration(proposal)
        assert body["plan_hash"] == plan_hash(transformed)
        assert body["plan_hash_is_bindable"] is True

        # The demotion is what makes it bindable, stated directly: the transformed
        # document a registration receives declares no `execution_policy` at all, so
        # there is nothing for Gate 2a to stamp and nothing to rehash.
        assert transformed.execution_policy is None
        assert transformed.proposed_execution_policy is not None

        # The policy is still fully reported for review. Demoting it must not become
        # "silently dropped" — a graph running unbounded while its author believes it
        # constrained is the worse of the two failures.
        assert body["proposed_execution_policy"] is not None

    def test_the_acceptor_dependent_hash_still_exists_but_moves_to_activation(self, app_with_router, autonomy_default_unset):
        """The asymmetry did not disappear; it moved past the binding point.

        Stamping still folds the accepting principal into the policy hash, so the
        document in force *after* a grant still depends on who granted it. What
        changed is that this now happens when the human answers the acceptance gate,
        strictly after the revision they bound to was fixed — so it can no longer make
        their binding unsatisfiable.

        Pinned here because it is the reason the retry of a successful bound approval
        must be recognised as a replay before staleness is compared
        (`_matching_bound_answer`): the hash in force after a grant is deliberately not
        the hash that was approved.
        """
        proposal = gateless_proposal(execution_policy=a_policy())
        transformed, _gate = transform_for_registration(proposal)
        registered_hash = plan_hash(transformed)

        def granted_by(principal: str):
            promoted = transformed.model_copy(
                update={
                    "execution_policy": stamp_policy(transformed.proposed_execution_policy, principal_id=principal, org_id=ORG_A),
                    "proposed_execution_policy": None,
                }
            )
            return plan_hash(promoted)

        assert granted_by("human-acceptor") != registered_hash, "a grant must change the plan of record; it adds authority"
        assert granted_by("human-acceptor") != granted_by("another-acceptor"), "the stamp carries the principal, so the grant depends on who granted"


class TestTheEffectiveOrderIsDerivedFromDependencies:
    """The preview answers "what runs at the same time as what" (#5331 blocker 5b).

    A list of node addresses, kinds and titles is enough to confirm a document
    parsed. It is not enough to take responsibility for what the engine will then do
    unattended, because it says nothing about concurrency: a reviewer cannot see
    which work proceeds in parallel, and therefore cannot reason about blast radius.

    Every assertion here is written to fail if the staging were taken from the order
    the author listed nodes in, because the issue is explicit that visual order alone
    is not an execution dependency.
    """

    def test_waves_are_staged_by_the_dependency_graph(self, app_with_router, autonomy_default_unset):
        """`gateless_proposal` chains wave-1 into wave-2, so they cannot share a stage."""
        body = preview(app_with_router, gateless_proposal()).json()

        by_wave = {f"{wave['epic_ref']}/{wave['wave_ref']}": wave for wave in body["waves"]}
        assert set(by_wave) == {"epic-1/wave-1", "epic-1/wave-2"}
        assert by_wave["epic-1/wave-1"]["stage"] < by_wave["epic-1/wave-2"]["stage"], "a wave that feeds another must be staged before it"
        assert by_wave["epic-1/wave-2"]["depends_on"] == ["epic-1/wave-1"]
        assert by_wave["epic-1/wave-1"]["depends_on"] == []

    def test_independent_waves_share_a_stage_so_parallelism_is_visible(self, app_with_router, autonomy_default_unset):
        """Two waves with no path between them run concurrently, and must say so.

        The discriminating case for "stage" meaning anything at all: if stages were
        simply a wave's position in author order, wave-2 and wave-3 would differ.

        Both branch waves are placed *downstream* of wave-1 on purpose. The server
        inserts the acceptance gate into the first wave and wires it to every root, so
        two waves that each contained a root would be genuinely serialized by that
        gate — wave-1 would precede wave-2, and a test asserting otherwise would be
        asserting against the effective graph rather than about parallelism. Branching
        after the gate isolates the property under test.
        """
        parallel = gateless_proposal(
            nodes=[
                _reg.ProposedNode(address=_reg.address("story-a"), kind="story", title="A", issue_ref="1"),
                _reg.ProposedNode(address=_reg.address("eval-w1"), kind="eval", title="W1 eval"),
                _reg.ProposedNode(address=_reg.address("story-b", wave="wave-2"), kind="story", title="B", issue_ref="2"),
                _reg.ProposedNode(address=_reg.address("eval-w2", wave="wave-2"), kind="eval", title="W2 eval"),
                _reg.ProposedNode(address=_reg.address("story-c", wave="wave-3"), kind="story", title="C", issue_ref="3"),
                _reg.ProposedNode(address=_reg.address("eval-w3", wave="wave-3"), kind="eval", title="W3 eval"),
            ],
            edges=[
                _reg.ProposedEdge(from_address=_reg.address("story-a"), to_address=_reg.address("eval-w1")),
                # wave-1 fans out into two independent branches.
                _reg.ProposedEdge(from_address=_reg.address("eval-w1"), to_address=_reg.address("story-b", wave="wave-2")),
                _reg.ProposedEdge(from_address=_reg.address("eval-w1"), to_address=_reg.address("story-c", wave="wave-3")),
                _reg.ProposedEdge(
                    from_address=_reg.address("story-b", wave="wave-2"),
                    to_address=_reg.address("eval-w2", wave="wave-2"),
                ),
                _reg.ProposedEdge(
                    from_address=_reg.address("story-c", wave="wave-3"),
                    to_address=_reg.address("eval-w3", wave="wave-3"),
                ),
            ],
        )
        body = preview(app_with_router, parallel).json()

        stages = {f"{wave['epic_ref']}/{wave['wave_ref']}": wave["stage"] for wave in body["waves"]}
        assert stages["epic-1/wave-2"] == stages["epic-1/wave-3"], f"independent sibling waves must share a stage, got {stages}"
        assert stages["epic-1/wave-1"] < stages["epic-1/wave-2"], "the branches both depend on wave-1"

    def test_a_wave_waits_for_its_latest_predecessor_not_its_earliest(self, app_with_router, autonomy_default_unset):
        """Staging is a longest path, not a breadth-first rank.

        With `w1 -> w2 -> w3` AND `w1 -> w3`, a breadth-first rank would place w3 one
        step after w1 (stage 1) — the same stage as w2, implying the two may run
        together when w3 in fact waits for w2. Only a longest-path depth reports w3
        strictly after w2.
        """
        diamond = gateless_proposal(
            nodes=[
                _reg.ProposedNode(address=_reg.address("story-a"), kind="story", title="A", issue_ref="1"),
                _reg.ProposedNode(address=_reg.address("eval-w1"), kind="eval", title="W1 eval"),
                _reg.ProposedNode(address=_reg.address("story-b", wave="wave-2"), kind="story", title="B", issue_ref="2"),
                _reg.ProposedNode(address=_reg.address("eval-w2", wave="wave-2"), kind="eval", title="W2 eval"),
                _reg.ProposedNode(address=_reg.address("story-c", wave="wave-3"), kind="story", title="C", issue_ref="3"),
                _reg.ProposedNode(address=_reg.address("eval-w3", wave="wave-3"), kind="eval", title="W3 eval"),
            ],
            edges=[
                _reg.ProposedEdge(from_address=_reg.address("story-a"), to_address=_reg.address("eval-w1")),
                _reg.ProposedEdge(from_address=_reg.address("eval-w1"), to_address=_reg.address("story-b", wave="wave-2")),
                _reg.ProposedEdge(
                    from_address=_reg.address("story-b", wave="wave-2"),
                    to_address=_reg.address("eval-w2", wave="wave-2"),
                ),
                _reg.ProposedEdge(
                    from_address=_reg.address("eval-w2", wave="wave-2"),
                    to_address=_reg.address("story-c", wave="wave-3"),
                ),
                _reg.ProposedEdge(
                    from_address=_reg.address("story-c", wave="wave-3"),
                    to_address=_reg.address("eval-w3", wave="wave-3"),
                ),
                # The shortcut edge that makes breadth-first and longest-path disagree.
                _reg.ProposedEdge(from_address=_reg.address("eval-w1"), to_address=_reg.address("story-c", wave="wave-3")),
            ],
        )
        body = preview(app_with_router, diamond).json()
        stages = {f"{wave['epic_ref']}/{wave['wave_ref']}": wave["stage"] for wave in body["waves"]}

        assert stages["epic-1/wave-3"] > stages["epic-1/wave-2"], f"wave-3 waits for wave-2, so it cannot share or precede its stage; got {stages}"

    def test_waves_are_listed_in_execution_order(self, app_with_router, autonomy_default_unset):
        """The list reads top-to-bottom as execution proceeds."""
        body = preview(app_with_router, gateless_proposal()).json()
        stages = [wave["stage"] for wave in body["waves"]]
        assert stages == sorted(stages), f"waves should be ordered by stage, got {stages}"

    def test_a_node_reports_its_direct_predecessors(self, app_with_router, autonomy_default_unset):
        """ "What immediately holds this up" is a per-node question."""
        body = preview(app_with_router, gateless_proposal()).json()
        by_address = {node["address"]: node for node in body["nodes"]}

        eval_w1 = by_address[_reg.address("eval-w1")]
        assert sorted(eval_w1["depends_on"]) == sorted([_reg.address("story-a"), _reg.address("story-b")])

    def test_a_node_carries_its_issue_binding_and_wave(self, app_with_router, autonomy_default_unset):
        """A reviewer needs to see where each node's work is tracked."""
        body = preview(app_with_router, gateless_proposal()).json()
        by_address = {node["address"]: node for node in body["nodes"]}

        story_a = by_address[_reg.address("story-a")]
        assert story_a["issue_ref"] == "4527"
        assert (story_a["epic_ref"], story_a["wave_ref"]) == ("epic-1", "wave-1")

        # A node that declares no issue reports an empty binding rather than omitting
        # the field: for a story that gap is itself something to see before approving.
        assert by_address[_reg.address("eval-w1")]["issue_ref"] == ""

    def test_waves_whose_order_is_undetermined_report_no_stage(self, app_with_router, autonomy_default_unset):
        """A wave-level cycle is possible even when the NODE graph is acyclic.

        `w1/a -> w2/b` together with `w2/c -> w1/d` is perfectly valid over nodes —
        no node depends on itself — while making the two *waves* mutually dependent.
        There is no fact of the matter about which runs first, so the preview reports
        `None` rather than invent a number an operator would read as an ordering the
        engine intends to honour.
        """
        tangled = gateless_proposal(
            nodes=[
                _reg.ProposedNode(address=_reg.address("story-a"), kind="story", title="A", issue_ref="1"),
                _reg.ProposedNode(address=_reg.address("eval-w1"), kind="eval", title="W1 eval"),
                _reg.ProposedNode(address=_reg.address("story-b", wave="wave-2"), kind="story", title="B", issue_ref="2"),
                _reg.ProposedNode(address=_reg.address("eval-w2", wave="wave-2"), kind="eval", title="W2 eval"),
            ],
            edges=[
                # wave-1 -> wave-2 ...
                _reg.ProposedEdge(from_address=_reg.address("story-a"), to_address=_reg.address("story-b", wave="wave-2")),
                _reg.ProposedEdge(
                    from_address=_reg.address("story-b", wave="wave-2"),
                    to_address=_reg.address("eval-w2", wave="wave-2"),
                ),
                # ... and wave-2 -> wave-1, via different nodes, so no node cycle exists.
                _reg.ProposedEdge(from_address=_reg.address("eval-w2", wave="wave-2"), to_address=_reg.address("eval-w1")),
            ],
        )
        body = preview(app_with_router, tangled).json()
        stages = {f"{wave['epic_ref']}/{wave['wave_ref']}": wave["stage"] for wave in body["waves"]}

        assert stages["epic-1/wave-1"] is None, f"a wave in a wave-level cycle has no determined stage, got {stages}"
        assert stages["epic-1/wave-2"] is None, f"a wave in a wave-level cycle has no determined stage, got {stages}"


class TestWhoMayConcludeEachNode:
    """Three different mechanisms decide this, and a reviewer must be able to tell.

    Whether a step comes back to a person or completes without them is the question
    an approver is actually answering. The default direction matters: "a human checks
    this" is the reassuring answer, so it must never be the one the code guesses.
    """

    def test_a_gate_is_concluded_by_a_human(self, app_with_router, autonomy_default_unset):
        """Structural, not policy-dependent: the state machine admits only humans."""
        body = preview(app_with_router, gateless_proposal()).json()
        gate = next(node for node in body["nodes"] if node["kind"] == "gate")
        assert gate["concluded_by"] == "human_decision"

    def test_a_story_is_concluded_by_merged_pull_request_evidence(self, app_with_router, autonomy_default_unset):
        """Not by an agent's own report that it finished."""
        body = preview(app_with_router, gateless_proposal()).json()
        stories = [node for node in body["nodes"] if node["kind"] == "story"]
        assert stories
        assert all(node["concluded_by"] == "merged_pull_request" for node in stories)

    def test_an_evaluation_is_human_unless_the_policy_grants_machine_acceptance(self, app_with_router, autonomy_default_unset):
        """A policyless plan's evaluations all come back to a person.

        The safe default, and the one that must not be inverted: absence of a grant
        is not a grant.
        """
        body = preview(app_with_router, gateless_proposal()).json()
        evals = [node for node in body["nodes"] if node["kind"] == "eval"]
        assert evals
        assert all(node["concluded_by"] == "human_decision" for node in evals), "no policy means no machine acceptance"

    def test_an_evaluation_the_proposed_policy_marks_machine_accepted_says_so(self, app_with_router, autonomy_default_unset):
        """The discriminating case: the SAME document differs only by its policy.

        Read from the *proposed* policy, because the reviewer is deciding whether to
        grant it — "if I accept this, what stops coming back to me?"
        """
        from src.orchestration.execution_policy import AcceptanceMode

        machine_accepted = _reg.address("eval-w1")
        with_policy = gateless_proposal(
            execution_policy=a_policy(
                org_id=ORG_A,
                evaluation_acceptance={machine_accepted: AcceptanceMode.MACHINE},
            )
        )
        body = preview(app_with_router, with_policy).json()
        by_address = {node["address"]: node for node in body["nodes"]}

        assert by_address[machine_accepted]["concluded_by"] == "machine_evaluation"
        # The other evaluation is NOT named in the map, so it stays human. Without
        # this half, a bug that marked every eval machine-accepted would still pass.
        assert by_address[_reg.address("eval-w2", wave="wave-2")]["concluded_by"] == "human_decision"


class TestAPolicylessPlanIsReportedAsUnbounded:
    """The most consequential fact on a preview, and the one that looks harmless.

    A document with no execution policy is *unbounded* — no repository restriction,
    no action allowlist, no spend ceiling, no expiry. The natural reading of a `null`
    policy is the opposite ("authorizes nothing"), so the fact is stated positively
    as its own field rather than left to be inferred.
    """

    def test_a_document_with_no_policy_says_execution_is_unbounded(self, app_with_router, autonomy_default_unset):
        body = preview(app_with_router, gateless_proposal()).json()
        assert body["proposed_execution_policy"] is None
        assert body["execution_is_unbounded"] is True

    def test_a_policy_bearing_document_is_not_unbounded(self, app_with_router, autonomy_default_unset):
        """The complement. Without it, a field hardcoded to True would pass above."""
        body = preview(app_with_router, gateless_proposal(execution_policy=a_policy(org_id=ORG_A))).json()
        assert body["proposed_execution_policy"] is not None
        assert body["execution_is_unbounded"] is False

    def test_the_bounds_a_reviewer_must_see_are_all_present(self, app_with_router, autonomy_default_unset):
        """Repositories, targets, autonomous actions, retained gates, expiry, limits.

        These are the bounds being granted; a preview missing any of them asks for
        approval of something unseen.
        """
        body = preview(app_with_router, gateless_proposal(execution_policy=a_policy(org_id=ORG_A))).json()
        summary = body["proposed_execution_policy"]

        assert summary["repository_ids"], "which repositories the authority applies to"
        assert summary["environment_connection_ids"], "which deployment targets"
        assert summary["autonomous_actions"], "what may happen without asking again"
        assert "expires_at" in summary, "when the authority lapses"
        assert summary["limits"]["max_spend_usd"], "the spend ceiling"
        # `human_decisions` may legitimately be empty, so assert the key's presence
        # rather than a truthy value: what matters is that retained gates are stated.
        assert "human_decisions" in summary
