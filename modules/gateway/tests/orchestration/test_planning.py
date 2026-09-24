"""The server-side intent-to-plan derivation (#5331, blocker 4).

These tests are organised around the three things that could go wrong here in a way
that matters, rather than around the module's functions:

* A repository name reaching a document without having been checked against the
  tenant's real installations — a grant the server never verified.
* A derived document that the server's own validator would then reject, which turns
  a generator bug into the user's authoring error.
* An invented graph: edges, waves or a policy asserting knowledge the prose does not
  contain, which the preview would then render as confident staging.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.orchestration.compile import plan_hash
from src.orchestration.planning import (
    MAX_OUTCOMES,
    PLANNER_SPEC_REVISION,
    PlanningError,
    PlanningInputs,
    plan_from_draft,
    resolve_issue_ref,
    resolve_repository,
    slugify,
)
from src.orchestration.proposal import validate_proposal
from src.orchestration.registration import transform_for_registration


def _inputs(**overrides) -> PlanningInputs:
    """A plan's inputs as the route builds them, with the tenant already resolved."""
    base = {
        "flow_slug": "checkout-latency",
        "title": "Cut checkout latency",
        "outcomes": ("p95 under 400ms", "No regression in conversion"),
        "issue_ref": "5331",
        "intent": "Checkout is slow and it costs us orders.",
        "org_id": "org-alpha",
    }
    base.update(overrides)
    return PlanningInputs(**base)


class TestARepositoryIsCheckedAgainstRealInstallations:
    """A repository name is a dispatch target, so it is verified or refused."""

    def test_a_repository_no_installation_carries_is_refused_before_a_plan_exists(self):
        """The refusal has to land here, not at dispatch.

        A name passed through would produce a document that registers, previews and
        gets approved by a human, and only then fails with `repository_not_permitted`
        — a refusal arriving after the authority was granted, naming a cause the
        approver cannot connect to their decision.
        """
        with pytest.raises(PlanningError) as caught:
            resolve_repository("acme/not-connected", [(11, ["acme/web"], True)])

        assert caught.value.code == "repository_not_connected"
        # The message has to name the repository and the operator action, because the
        # caller cannot fix this by editing their request.
        assert "acme/not-connected" in str(caught.value)
        assert "Connections" in str(caught.value)

    def test_a_repository_is_matched_case_insensitively_but_reported_as_github_spells_it(self):
        """GitHub folds case; `policy_admission` matches verbatim. Both must hold.

        Refusing `Acme/Web` against a stored `acme/web` would reject a repository the
        tenant genuinely has. Returning it as the caller TYPED it would hand a human a
        string to authorize that differs from the one dispatch compares.
        """
        resolved = resolve_repository("ACME/Web", [(11, ["acme/web"], True)])

        assert resolved is not None
        assert resolved.full_name == "acme/web"
        assert resolved.installation_id == 11

    def test_an_empty_installation_list_refuses_rather_than_accepts_unverified(self):
        """The route passes `[]` when it could not read connections at all.

        Failing open here — treating "I could not check" as "it is fine" — would make
        the whole resolution decorative, and it would do so silently, on exactly the
        degraded path nobody exercises in review.
        """
        with pytest.raises(PlanningError) as caught:
            resolve_repository("acme/web", [])

        assert caught.value.code == "repository_not_connected"

    def test_a_bare_name_is_refused_rather_than_given_an_owner(self):
        """Guessing the owner would be inventing a grant."""
        with pytest.raises(PlanningError) as caught:
            resolve_repository("web", [(11, ["acme/web"], True)])

        assert caught.value.code == "malformed_repository"

    def test_a_match_against_a_stored_snapshot_is_reported_as_not_live(self):
        """Weaker evidence, surfaced rather than hidden.

        `list_connections` can return repositories from a stored snapshot when the
        live GitHub read fails. That is still a usable answer, but an operator
        deciding whether to authorize a dispatch target is entitled to know which one
        they got — so `verified_live` tracks the source instead of being hardcoded.
        """
        resolved = resolve_repository("acme/web", [(11, ["acme/web"], False)])

        assert resolved is not None
        assert resolved.verified_live is False

    def test_the_first_installation_carrying_the_repository_is_the_one_reported(self):
        """Which installation grants this is the question credential scoping asks."""
        resolved = resolve_repository("acme/web", [(11, ["acme/other"], True), (22, ["acme/web"], True)])

        assert resolved is not None
        assert resolved.installation_id == 22

    def test_requesting_no_repository_is_a_legitimate_plan_not_a_default(self):
        """Picking "the only repository they have" would be a grant nobody asked for.

        It would also pick differently the day the tenant connects a second one, which
        makes it a silent behaviour change rather than a stable convenience.
        """
        assert resolve_repository(None, [(11, ["acme/web"], True)]) is None
        assert resolve_repository("   ", [(11, ["acme/web"], True)]) is None


class TestAnIssueReferenceIsNormalizedOrRefused:
    """Parsed by the rule dispatch uses, so a plan cannot store an unroutable ref."""

    @pytest.mark.parametrize(("given", "expected"), [(5331, "5331"), ("5331", "5331"), ("#5331", "5331"), (" #5331 ", "5331")])
    def test_every_accepted_form_is_stored_as_one_bare_decimal_string(self, given, expected):
        """One form in the database is what makes two nodes comparable.

        `unordered_same_issue` and the dispatch lookup both key on this value; `#5331`
        and `5331` both parse but are different strings, so storing whichever the
        caller typed would make two nodes on one issue look like two issues.
        """
        assert resolve_issue_ref(given) == expected

    @pytest.mark.parametrize("given", ["not-a-number", "12ab", "#", "5331.5"])
    def test_a_reference_the_dispatch_parse_rejects_is_refused_here(self, given):
        """Otherwise the story nodes are born `malformed_issue_ref`.

        That failure surfaces only as a `dispatch_blocked_cause` on a plan already
        written and approved, which is far too late to be actionable.
        """
        with pytest.raises(PlanningError) as caught:
            resolve_issue_ref(given)
        assert caught.value.code == "malformed_issue_ref"

    @pytest.mark.parametrize("given", [0, -1, "0"])
    def test_zero_and_negatives_are_refused_though_they_parse(self, given):
        """`int("0")` succeeds happily while pointing at no issue at all."""
        with pytest.raises(PlanningError) as caught:
            resolve_issue_ref(given)
        assert caught.value.code == "malformed_issue_ref"

    def test_no_reference_stays_absent(self):
        assert resolve_issue_ref(None) is None
        assert resolve_issue_ref("") is None


class TestTheDerivedDocumentPassesTheServersOwnRules:
    """A generated plan the server then rejects is a bug here, not an authoring error."""

    def test_a_derived_plan_has_zero_violations(self):
        violations = validate_proposal(plan_from_draft(_inputs()))

        assert violations == []

    def test_a_derived_plan_still_validates_after_the_registration_transform(self):
        """The transform inserts the acceptance gate; rule 4 counts evals per wave.

        Validating only the pre-transform document would miss a generator that emits a
        shape the gate insertion then breaks — and the transformed document is the one
        that actually registers.
        """
        transformed, _ = transform_for_registration(plan_from_draft(_inputs()))

        assert validate_proposal(transformed) == []

    def test_outcomes_that_slugify_identically_do_not_collide_into_one_address(self):
        """Rule 1 rejects duplicate addresses, so a collision here becomes their error."""
        proposal = plan_from_draft(_inputs(outcomes=("Faster!", "faster", "FASTER")))

        addresses = [node.address for node in proposal.nodes]
        assert len(addresses) == len(set(addresses))
        assert validate_proposal(proposal) == []

    def test_an_outcome_of_pure_punctuation_still_yields_a_valid_address(self):
        """Non-Latin or symbol-only prose is legitimate input.

        Slugifying to an empty segment would emit an address rule 1 rejects, so the
        failure mode has to be "an unhelpful name", never "a plan that cannot
        register".
        """
        proposal = plan_from_draft(_inputs(outcomes=("!!!", "上市")))

        assert validate_proposal(proposal) == []
        assert all(node.address.count("/") == 3 for node in proposal.nodes)

    def test_a_very_long_outcome_is_truncated_rather_than_refusing_the_plan(self):
        """An outcome is prose the user wrote; refusing over sentence length is pedantry."""
        proposal = plan_from_draft(_inputs(outcomes=("x" * 900,)))

        story = next(node for node in proposal.nodes if node.kind == "story")
        assert len(story.title) == 512
        assert validate_proposal(proposal) == []

    def test_the_declared_tenant_and_contract_revision_are_what_rule_five_requires(self):
        """Rule 5 refuses a blank `org_id` or `spec_revision`."""
        proposal = plan_from_draft(_inputs())

        assert proposal.org_id == "org-alpha"
        assert proposal.spec_revision == PLANNER_SPEC_REVISION


class TestNothingIsInventedThatThePoseDoesNotContain:
    """The derivation's omissions are the load-bearing part."""

    def test_with_no_issue_there_are_no_edges_between_stories(self):
        """An edge claims one piece of work blocks another. Prose does not say that.

        Emitting them in listed order would serialize work that may be independent and
        make the preview's staging report a sequence the user never described. With no
        shared issue there is nothing forcing an order, so none is asserted.
        """
        proposal = plan_from_draft(_inputs(issue_ref=None, outcomes=("A", "B", "C")))

        stories = {node.address for node in proposal.nodes if node.kind == "story"}
        story_to_story = [edge for edge in proposal.edges if edge.from_address in stories and edge.to_address in stories]
        assert story_to_story == []
        assert validate_proposal(proposal) == []

    def test_stories_sharing_one_issue_are_chained_because_rule_six_forces_it(self):
        """Not an inferred dependency — a consequence of one issue, one delivery.

        Two nodes on one issue with no order between them is `unordered_same_issue`:
        the plan says "work this issue twice, concurrently", which one GitHub issue
        cannot support and which transactional work claims would refuse at run time on
        a plan a human had already accepted. The listed order is the only order
        available, and it is the user's own.
        """
        proposal = plan_from_draft(_inputs(issue_ref="5331", outcomes=("A", "B", "C")))

        stories = [node.address for node in proposal.nodes if node.kind == "story"]
        chain = {(edge.from_address, edge.to_address) for edge in proposal.edges}
        assert (stories[0], stories[1]) in chain
        assert (stories[1], stories[2]) in chain
        # Chained, not fully connected: a transitive edge would be redundant noise.
        assert (stories[0], stories[2]) not in chain

    def test_every_story_precedes_the_single_eval(self):
        """The one dependency the input always supports: you cannot assess unfinished work.

        Asserted for every story even under the chained single-issue shape, so that
        removing the chain could not silently orphan a story from its own assessment.
        """
        for issue_ref in ("5331", None):
            proposal = plan_from_draft(_inputs(issue_ref=issue_ref, outcomes=("A", "B", "C")))

            evals = [node for node in proposal.nodes if node.kind == "eval"]
            assert len(evals) == 1
            stories = {node.address for node in proposal.nodes if node.kind == "story"}
            eval_edges = {edge.from_address for edge in proposal.edges if edge.to_address == evals[0].address}
            assert eval_edges == stories

    def test_all_work_lands_in_one_wave(self):
        """Several waves would assert a dependency structure this cannot know."""
        proposal = plan_from_draft(_inputs(outcomes=("A", "B", "C")))

        waves = {node.address.split("/")[2] for node in proposal.nodes}
        assert len(waves) == 1

    def test_the_document_declares_no_policy_in_either_field(self):
        """Attaching bounds would be choosing how much authority to request for them.

        The inert-policy machinery exists so bounds are proposed by an author and
        granted by a human; a generator that filled them in would be doing both.
        """
        proposal = plan_from_draft(_inputs())

        assert proposal.execution_policy is None
        assert proposal.proposed_execution_policy is None

    def test_the_document_declares_no_gate_of_its_own(self):
        """`transform_for_registration` inserts the acceptance gate; two would be wrong."""
        proposal = plan_from_draft(_inputs())

        assert [node.kind for node in proposal.nodes if node.kind == "gate"] == []

    def test_the_stories_are_the_users_outcomes_in_the_order_they_stated_them(self):
        proposal = plan_from_draft(_inputs(outcomes=("First thing", "Second thing")))

        titles = [node.title for node in proposal.nodes if node.kind == "story"]
        assert titles == ["First thing", "Second thing"]

    def test_the_eval_carries_the_same_issue_as_the_stories(self):
        """An eval with no issue ref is reported by the graph view as a configuration problem.

        Emitting a node already known to be unrunnable would be shipping a defect as a
        feature.
        """
        proposal = plan_from_draft(_inputs(issue_ref="5331"))

        assert {node.issue_ref for node in proposal.nodes} == {"5331"}


class TestARetryCannotDuplicateAFlow:
    """Determinism is what makes the registration path's idempotency reachable."""

    def test_the_same_draft_derives_the_same_plan_hash(self):
        """A caller whose response was lost re-derives and re-registers the same plan.

        If the hash moved, that retry would be `already_registered`'s opposite: a
        second flow for one intent, with the first left orphaned and invisible.
        """
        first = plan_hash(plan_from_draft(_inputs()))
        second = plan_hash(plan_from_draft(_inputs()))

        assert first == second

    def test_a_changed_outcome_changes_the_hash(self):
        """Determinism must not be achieved by ignoring the input.

        Without this, a constant hash would satisfy the test above while making every
        distinct plan collide with the first one registered.
        """
        original = plan_hash(plan_from_draft(_inputs()))
        amended = plan_hash(plan_from_draft(_inputs(outcomes=("p95 under 200ms", "No regression in conversion"))))

        assert original != amended


class TestADraftThatCannotYetBePlannedIsRefusedNotGuessed:
    def test_a_draft_with_no_outcomes_is_refused_with_what_to_do_next(self):
        """An outcome is what a story delivers, so there is genuinely nothing to plan."""
        with pytest.raises(PlanningError) as caught:
            plan_from_draft(_inputs(outcomes=()))

        assert caught.value.code == "draft_not_ready"
        assert "outcome" in str(caught.value).lower()

    def test_too_many_outcomes_are_refused_rather_than_silently_dropped(self):
        """Truncating would produce a plan that LOOKS complete and omits their work.

        That is the worst failure available here, because nothing downstream — not the
        preview, not the human accepting it — can see what went missing.
        """
        with pytest.raises(PlanningError) as caught:
            plan_from_draft(_inputs(outcomes=tuple(f"outcome {index}" for index in range(MAX_OUTCOMES + 1))))

        assert caught.value.code == "too_many_outcomes"

    def test_exactly_the_maximum_is_accepted(self):
        """The bound is a limit, not an off-by-one refusal of a legitimate plan."""
        proposal = plan_from_draft(_inputs(outcomes=tuple(f"outcome {index}" for index in range(MAX_OUTCOMES))))

        assert len([node for node in proposal.nodes if node.kind == "story"]) == MAX_OUTCOMES
        assert validate_proposal(proposal) == []


class TestSlugsStayWithinTheAddressGrammar:
    @pytest.mark.parametrize(
        ("given", "expected"),
        [
            ("Cut checkout latency", "cut-checkout-latency"),
            ("p95 < 400ms!", "p95-400ms"),
            ("  Trailing  ", "trailing"),
            ("Mixed__Case--Here", "mixed-case-here"),
        ],
    )
    def test_prose_becomes_a_hyphenated_lowercase_segment(self, given, expected):
        assert slugify(given, fallback="x") == expected

    def test_text_that_slugifies_to_nothing_uses_the_fallback(self):
        assert slugify("!!!", fallback="outcome") == "outcome"

    def test_a_long_segment_is_bounded(self):
        """One long sentence must not overrun the column the address is stored in."""
        assert len(slugify("word " * 100, fallback="x")) <= 48

    def test_the_bounded_segment_does_not_end_in_a_hyphen(self):
        """Truncation can land mid-separator, and a trailing hyphen is an ugly address.

        It also risks colliding with the same text truncated one character earlier.
        """
        assert not slugify("a" * 47 + " bbbb", fallback="x").endswith("-")


def test_repository_changes_change_the_reviewed_policy_hash():
    repository = resolve_repository("acme/web", [(11, ["acme/web", "acme/api"], True)])
    inputs = _inputs(repository=repository, last_activity_epoch=1800000000)
    proposal = plan_from_draft(inputs)
    policy = proposal.proposed_execution_policy
    assert proposal.execution_policy is None
    assert policy.repository_ids == ["acme/web"]
    assert policy.principal_id is None
    assert policy.limits.max_spend_usd == 5
    assert policy.limits.max_concurrent_actions == 1
    assert "deploy" not in policy.allowed_actions
    assert "merge" not in policy.allowed_actions
    assert plan_hash(proposal) == plan_hash(plan_from_draft(inputs))
    other = plan_from_draft(_inputs(repository=resolve_repository("acme/api", [(11, ["acme/api"], True)]), last_activity_epoch=1800000000))
    assert plan_hash(proposal) != plan_hash(other)
    assert validate_proposal(transform_for_registration(proposal)[0]) == []


class TestAResumedConversationDoesNotYieldASpentGrant:
    """The grant's lifetime must not be consumed by the user's own thinking time.

    #5331 requires both that a user may disconnect mid-planning and reconnect later
    with the saved session identifier, and that approving a plan grants the bounds
    they read. Anchoring the proposed policy's expiry to session *creation* put those
    two in direct conflict: the 24 hours were spent waiting for the user, and spent
    entirely by the resume the feature exists for. A conversation resumed on Thursday
    and approved yielded a grant that expired on Tuesday, and nothing refused it —
    the gate moved, the graph armed, and every dispatch was then denied
    `policy_expired` on a plan the human had just deliberately authorized.

    The anchor is now last activity, which is a real signal rather than a convenient
    one: the ingest and response Lambdas bump `updated_at` on every turn, and the
    conversation row's own storage lifetime is already `updated_at + 86400`, so
    "last activity plus 24 hours" is the horizon the conversation itself is kept for.

    These tests assert against a clock, not against a fixed timestamp, because the
    defect was invisible to every fixed-epoch fixture in this file: `1800000000` is
    2027, so it is comfortably in the future and the bound looked healthy.
    """

    def test_a_conversation_resumed_days_later_is_not_granted_dead_bounds(self):
        """The reproduction. Three days idle, then continued, then planned."""
        recent = int((datetime.now(tz=UTC) - timedelta(minutes=5)).timestamp())
        proposal = plan_from_draft(
            _inputs(
                repository=resolve_repository("acme/web", [(11, ["acme/web"], True)]),
                last_activity_epoch=recent,
            )
        )
        remaining = proposal.proposed_execution_policy.expires_at - datetime.now(tz=UTC)
        # Not merely "in the future": a grant has to have usable life left in it,
        # since the human still has to read the preview and answer the gate.
        assert remaining > timedelta(hours=23)

    def test_a_long_idle_conversation_is_refused_rather_than_given_a_spent_grant(self):
        """Refused, not silently re-clocked.

        Reattaching to a long-idle conversation and asking for a plan without saying
        anything first is the one case the anchor cannot fix, because there is no
        recent activity to anchor to. Emitting bounds that are dead on arrival would
        hand a human something to approve that cannot authorize anything, so the
        derivation refuses with the action that resolves it.

        Re-clocking to `now` instead is the tempting fix and the wrong one: it would
        make the document non-deterministic, so the retry of a lost response would
        register a SECOND flow for one intent rather than matching the first.
        """
        stale = int((datetime.now(tz=UTC) - timedelta(days=3)).timestamp())
        with pytest.raises(PlanningError) as caught:
            plan_from_draft(
                _inputs(
                    repository=resolve_repository("acme/web", [(11, ["acme/web"], True)]),
                    last_activity_epoch=stale,
                )
            )
        assert caught.value.code == "planning_session_idle"
        # The message has to name the act that fixes it. "Expired" alone would send a
        # user looking for a setting rather than back to their conversation.
        assert "continue the conversation" in caught.value.message.lower()

    def test_a_policyless_plan_is_not_refused_for_idleness(self):
        """Without a repository there is no policy, so there is no grant to be spent.

        Refusing here would break planning for the case the module documents as
        legitimate — a document with no repository binding, whose missing binding the
        preview reports as a real prerequisite.
        """
        stale = int((datetime.now(tz=UTC) - timedelta(days=3)).timestamp())
        proposal = plan_from_draft(_inputs(repository=None, last_activity_epoch=stale))
        assert proposal.proposed_execution_policy is None

    def test_deriving_twice_still_yields_one_identical_document(self):
        """The property the anchor choice exists to protect.

        #5331 requires that retrying a request whose response was lost does not create
        a second flow. That holds only because the derivation reads stored state rather
        than a clock, so two attempts agree byte for byte and the second registration
        is the server's own `already_registered` case. Asserted for a RESUMED
        conversation specifically, since that is the input the fix changed.
        """
        recent = int((datetime.now(tz=UTC) - timedelta(hours=6)).timestamp())
        inputs = _inputs(repository=resolve_repository("acme/web", [(11, ["acme/web"], True)]), last_activity_epoch=recent)
        assert plan_hash(plan_from_draft(inputs)) == plan_hash(plan_from_draft(inputs))


@pytest.mark.parametrize("name", ["acme/..", "../web", "acme/.", "acme/web\n"])
def test_repository_paths_cannot_traverse_or_extend_the_provider_path(name):
    if name.endswith("\n"):
        # Leading/trailing whitespace is deliberately normalized.
        assert resolve_repository(name, [(11, ["acme/web"], True)]).full_name == "acme/web"
    else:
        with pytest.raises(PlanningError, match="OWNER/NAME"):
            resolve_repository(name, [(11, [name], True)])
