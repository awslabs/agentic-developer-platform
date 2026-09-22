"""A submitted execution policy is retained inertly, and granted only by a bound
human acceptance of the plan (#5331, EPIC #4191).

Before this story a policy-bearing document could not be registered at all:
`compile_proposal` stamps a submitted policy at Gate 2a, and
`accept_execution_policy` refuses a non-HUMAN acceptor, so draft registration — a
SERVICE act — was refused outright. That is the right refusal for the wrong reason.
The author's bounds were unreviewable rather than merely unaccepted, so the operator
who would have to consent to them never got to read them.

`transform_for_registration` now **demotes** a submitted policy into
`proposed_execution_policy`, and `_activate_proposed_policy` promotes it back at the
moment a human answers the plan's acceptance gate bound to an exact revision.

The tests are organised by what each one would let through if it were deleted,
because "the policy activates correctly" is the easy half and every interesting
failure is on the other side of it:

* :class:`TestADraftsPolicyGrantsNothing` — the inertness property, asserted through
  `policy_admission` rather than by inspecting the document. This is the one that
  matters: the whole design rests on `load_in_force_policy` having no code that reads
  the demoted field, and an assertion that merely checked which key the JSON used
  would pass even if admission had started honouring it.
* :class:`TestOnlyABoundHumanAcceptanceGrants` — the four ways authority could be
  granted by something other than a person's deliberate, identified act: a service
  actor, an unbound approval, a rejection, and a wave gate's approval.
* :class:`TestAnUnboundApprovalCannotArmInertBounds` — the failure the demotion
  itself creates, and the reason it is refused rather than allowed. An unbound
  approval would pass the acceptance gate and arm every root behind it while the
  policy stayed inert: the graph would run under legacy unbounded semantics while its
  author believes it constrained. That is worse than either refusing or granting.
* :class:`TestABoundAcceptanceGrants` — the positive case, plus the ordering
  consequence it forces. A grant records a new plan version, so the bound hash is no
  longer in force the instant the approval succeeds; the retry of that same request
  must still be recognised as a replay rather than refused as stale.

The harness is `test_registration.py`'s, deliberately: real `AccessControl` over real
`users` and `tenant_memberships` rows, the real `run_tick` and `run_dispatch_pass`,
and the real `apply_gate_answer_for_context` seam both input paths share. An
assertion about inertness made against a stubbed admission path would be an assertion
about the stub.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from src.orchestration.adapters.github_comments import (
    GateAnswerStatus,
    InputPath,
    apply_gate_answer_for_context,
)
from src.orchestration.compile import ApprovalContext, PolicyNotAcceptableError, accept_execution_policy, plan_hash
from src.orchestration.execution_policy import Action, DenyReason, ResourceRef
from src.orchestration.models import DecisionKind, OrchestrationAcceptedPlan, OrchestrationDecision
from src.orchestration.policy_admission import authorize_node_dispatch, load_in_force_policy
from src.orchestration.registration import (
    ACCEPTANCE_GATE_REF,
    AUTONOMY_FLAG_ENV,
    WAVE_GATE_REF,
    register_draft_proposal,
    transform_for_registration,
)
from src.orchestration.repository import OrchestrationRepository
from src.orchestration.state import ActorKind
from tests.orchestration import test_registration as _reg
from tests.orchestration.test_execution_policy_acceptance import a_policy

# Rebound as module attributes rather than imported by name: pytest collects fixtures
# from a test module's namespace either way, while `from ... import session` shadows
# the name and trips ruff's F811. Same reasoning as `test_draft_preview.py`.
session = _reg.session
registrar = _reg.registrar
access = _reg.access
autonomy_default_unset = _reg.autonomy_default_unset
provider_repository_identity = _reg.provider_repository_identity

ORG_A = _reg.ORG_A
FLOW = _reg.FLOW
HUMAN_USER_ID = _reg.HUMAN_USER_ID
AGENT_USER_ID = _reg.AGENT_USER_ID
address = _reg.address
gateless_proposal = _reg.gateless_proposal
author_gated_proposal = _reg.author_gated_proposal
nodes_by_ref = _reg.nodes_by_ref
seed_org = _reg.seed_org
seed_principal = _reg.seed_principal
token_context = _reg.token_context
dispatch_config = _reg.dispatch_config


# The policy fixture names `test_execution_policy_acceptance`'s flow and eval address,
# which are not this module's. Repointed so `org_id` matches the tenant and the
# `evaluation_acceptance` key names a node that actually exists in these proposals —
# a policy keyed to an absent address would be validly *shaped* and would make the
# admission assertions below test nothing about this graph.
def policy_for_these_fixtures(**overrides: object):
    """`a_policy()` retargeted at this module's flow, repository and nodes."""
    base: dict[str, object] = {
        "org_id": ORG_A,
        "repository_ids": ["aws-e/adp"],
        "evaluation_acceptance": {},
    }
    base.update(overrides)
    return a_policy(**base)


async def register_policy_bearing_draft(session, registrar, *, policy=None, proposal=None):
    """Register a draft whose submitted `execution_policy` is demoted on the way in.

    Returns `(result, proposal_as_submitted)`. The org and the human principal are
    seeded first for the reason `seed_org`'s docstring gives: a test asserting
    "nothing was granted" would otherwise pass because the tenant was unwired rather
    than because the policy was inert.
    """
    await seed_org(session)
    await seed_principal(session, org_id=ORG_A, role=_reg.AdminRole.ORG_ADMIN.value)
    submitted = (proposal or gateless_proposal()).model_copy(update={"execution_policy": policy or policy_for_these_fixtures()})
    result, _ = await register_draft_proposal(session, submitted, registrar)
    return result, submitted


async def answer_acceptance_gate(session, access, *, node_id, approve=True, expected_plan_hash=None, user_id=HUMAN_USER_ID):
    """Answer a gate through the seam both real input paths share."""
    return await apply_gate_answer_for_context(
        session,
        context=token_context(ORG_A, user_id=user_id),
        node_id=node_id,
        approve=approve,
        reason="Reviewed the plan and the authority it delegates.",
        access=access,
        input_path=InputPath.DASHBOARD,
        expected_plan_hash=expected_plan_hash,
    )


async def in_force(session):
    return await OrchestrationRepository(session).get_accepted_plan(org_id=ORG_A, flow_id=(await flow_id_of(session)))


async def flow_id_of(session) -> str:
    return (await session.execute(sa.select(_reg.OrchestrationFlow.id).where(_reg.OrchestrationFlow.org_id == ORG_A))).scalar_one()


async def plan_versions(session) -> list[OrchestrationAcceptedPlan]:
    return (await session.execute(sa.select(OrchestrationAcceptedPlan).order_by(OrchestrationAcceptedPlan.version))).scalars().all()


class TestTheDemotionAndThePromotionAreOneMechanism:
    """#5331 blocker 5a, revalidated: exactly one inverse, used at the one grant site.

    The activation tests below prove the BEHAVIOUR end to end. These prove the
    structure that behaviour rests on, which is a separate claim: that the field move
    has one implementation.

    The defect this was written for was live. `promote_proposed_policy` is documented
    as "the inverse of `demote_proposed_policy`" and as "the ONLY way a demoted policy
    becomes authority", and `demote_proposed_policy` points at it as the thing that
    undoes it — while the grant path actually inlined its own `model_copy` of the same
    two fields. Neutering `promote_proposed_policy` entirely left all 26 activation
    tests green, which is the signature of dead code carrying a load-bearing claim.
    Two implementations of one inverse can drift, and the drift would surface as a
    plan armed with bounds that are not the ones the human reviewed.
    """

    def test_promotion_exactly_undoes_demotion(self):
        """Round-tripping a policy-bearing document must be the identity.

        Asserted on the whole document, not just the two policy fields: a promotion
        that also touched a node, an edge or the spec revision would change
        `plan_hash`, and the hash is what a human's acceptance is bound to.
        """
        from src.orchestration.registration import demote_proposed_policy, promote_proposed_policy

        original = gateless_proposal().model_copy(update={"execution_policy": policy_for_these_fixtures()})
        demoted = demote_proposed_policy(original)

        # The demotion is real, so the round trip is not trivially the identity.
        assert demoted.execution_policy is None
        assert demoted.proposed_execution_policy is not None
        assert promote_proposed_policy(demoted) == original

    def test_the_grant_path_uses_that_promotion_rather_than_its_own_copy(self):
        """The structural half, asserted by parsing rather than by string search.

        A comment saying "we use the shared helper" is not evidence. What is evidence
        is that the grant function calls it and does not move the two fields itself.
        """
        import ast
        import inspect

        from src.orchestration.adapters import github_comments

        def calls_in(node) -> set[str]:
            return {
                target.func.id if isinstance(target.func, ast.Name) else getattr(target.func, "attr", "")
                for target in ast.walk(node)
                if isinstance(target, ast.Call)
            }

        tree = ast.parse(inspect.getsource(github_comments))
        # The grant site is identified by what it DOES, not by its name: it is whatever
        # function hands a document to `accept_execution_policy`. Matching on a name
        # would let a rename move the field-moving code somewhere this test no longer
        # looks, which is precisely the drift being guarded against.
        grants = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and "accept_execution_policy" in calls_in(node)
        ]

        # Exactly one, because two acceptance sites would be two answers to "who may
        # grant authority" — the thing the single-resolver design exists to prevent.
        assert len(grants) == 1, f"expected one acceptance site, found {[node.name for node in grants]}"
        called = calls_in(grants[0])

        assert "promote_proposed_policy" in called, "the grant must use the documented inverse, not a second copy of it"
        # And must not hand-roll the field move it delegates.
        assert "model_copy" not in called, "moving the policy fields here would be the second implementation"


class TestADraftsPolicyGrantsNothing:
    """Registered, reviewable, and enforcing nothing until a human accepts it."""

    async def test_the_policy_is_retained_rather_than_dropped(self, session, registrar):
        """Retained is the requirement; dropped would be the worse failure.

        Refusing the document (the pre-#5331 behaviour) left the operator unable to
        read the bounds they were being asked to consent to. But silently dropping
        the policy to get the document registered would be worse than either: the
        graph would run unbounded while its author believed it constrained. So the
        submitted bounds must be present in the stored document, verbatim.
        """
        _result, submitted = await register_policy_bearing_draft(session, registrar)

        stored = (await in_force(session)).plan_document
        assert stored["proposed_execution_policy"] is not None, "the author's bounds were dropped, not retained"
        assert stored["proposed_execution_policy"]["repository_ids"] == submitted.execution_policy.repository_ids
        # And it is not ALSO in the enforced field, which would make it live.
        assert stored.get("execution_policy") is None

    async def test_admission_reports_no_policy_in_force(self, session, registrar):
        """The inertness property at the only layer that decides authority.

        `load_in_force_policy` is what every admission consults, and it reads
        `plan_document["execution_policy"]`. A demoted policy must therefore resolve
        to `policy=None` — legacy semantics — and, critically, must NOT resolve to a
        `STALE_POLICY_VERSION` refusal: that branch fires when a plan's policy was
        *removed* after having been accepted, and a v1 draft has no prior version
        that carried one. Getting that wrong would wedge every policy-bearing flow at
        registration instead of leaving it inert.
        """
        await register_policy_bearing_draft(session, registrar)

        inputs = await load_in_force_policy(session, org_id=ORG_A, flow_id=await flow_id_of(session))

        assert inputs.policy is None, "a proposed policy must not be readable as authority in force"
        assert inputs.refusal is None, f"a v1 draft has no removed policy to refuse over, got {inputs.refusal}"
        assert inputs.plan_version == 1

    async def test_the_proposed_policy_neither_permits_nor_denies_a_dispatch(self, session, registrar):
        """Inert means *no effect*, in both directions — the assertion that
        distinguishes this design from a half-applied one.

        A policy that was partly honoured would show up here as a denial: these
        fixtures' nodes are not in the policy's `evaluation_acceptance`, the dispatch
        repository is matched verbatim, and the spend and attempt limits would all be
        evaluated. So a `permit` whose detail names legacy semantics is the specific
        evidence that admission did not read the demoted field at all, rather than
        reading it and happening to allow this node.
        """
        await register_policy_bearing_draft(session, registrar)
        story = (await nodes_by_ref(session))["story-a"]

        decision = await authorize_node_dispatch(
            session,
            node=story,
            principal_user_id=f"user-{HUMAN_USER_ID}",
            target_repository="some/other-repo",
            installation_resolved=True,
        )

        assert decision.permitted is True, f"a proposed policy was enforced before anyone accepted it: {decision.detail}"
        assert "no execution policy in force" in decision.detail
        # Stated as the inverse too: had the policy been live, THIS is the reason a
        # repository outside `repository_ids` would have produced.
        assert decision.reason is not DenyReason.REPOSITORY_NOT_PERMITTED

    async def test_the_same_dispatch_is_denied_once_the_policy_is_granted(self, session, registrar, access):
        """The control for the test above, and the one that makes it mean something.

        "Permitted" proves inertness only if the identical call would be REFUSED
        under the granted policy. Without this, an admission path that permitted
        everything unconditionally would satisfy the inertness test perfectly.
        """
        _result, submitted = await register_policy_bearing_draft(session, registrar)
        gate = (await nodes_by_ref(session))[ACCEPTANCE_GATE_REF]
        transformed, _address = transform_for_registration(submitted)

        outcome = await answer_acceptance_gate(session, access, node_id=gate.id, expected_plan_hash=plan_hash(transformed))
        assert outcome.status is GateAnswerStatus.APPLIED, outcome.message

        story = (await nodes_by_ref(session))["story-a"]
        decision = await authorize_node_dispatch(
            session,
            node=story,
            principal_user_id=f"user-{HUMAN_USER_ID}",
            target_repository="some/other-repo",
            installation_resolved=True,
        )

        assert decision.permitted is False, "the granted policy does not bound anything, so inertness proves nothing"
        assert decision.reason is DenyReason.REPOSITORY_NOT_PERMITTED

    async def test_nothing_dispatches_while_the_plan_is_unaccepted(self, session, registrar):
        """The plan is inert as a graph too, not merely as a policy.

        Registration's existing inertness property, re-asserted for the
        policy-bearing document specifically: the demotion must not have disturbed
        the acceptance gate that dominates every root. Run against the real
        `run_tick` and `run_dispatch_pass`.
        """
        from src.orchestration.dispatch_pass import run_dispatch_pass
        from src.orchestration.tick import run_tick

        await register_policy_bearing_draft(session, registrar)

        pending: list = []
        for _ in range(10):
            await run_tick(session)
            report = await run_dispatch_pass(session, dispatch_config())
            pending.extend(report.pending)

        assert pending == [], f"an unaccepted policy-bearing plan dispatched work: {pending}"


class TestOnlyABoundHumanAcceptanceGrants:
    """Four ways authority could be granted by something other than a deliberate,
    identified human act. Each is asserted separately because each would be a
    different bug with a different fix."""

    async def test_a_service_actor_still_cannot_accept_a_policy_directly(self, session, registrar):
        """The self-approval boundary, unchanged and re-pinned.

        `accept_execution_policy` is the single place that decides who may grant
        authority, and this story did not touch it. Asserted here anyway, because the
        demotion is precisely what makes a policy-bearing document registrable by a
        SERVICE actor now — so the guarantee that a SERVICE actor cannot *accept* one
        carries more weight than it did before, not less.
        """
        submitted = gateless_proposal().model_copy(update={"execution_policy": policy_for_these_fixtures()})

        with pytest.raises(PolicyNotAcceptableError):
            accept_execution_policy(
                submitted,
                decision=ApprovalContext(
                    org_id=ORG_A,
                    actor_id=AGENT_USER_ID,
                    actor_role="member",
                    actor_kind=ActorKind.SERVICE,
                    reason="An agent trying to grant itself authority.",
                ),
                decision_kind=DecisionKind.PLAN_ACCEPTED,
            )

    async def test_registration_records_a_service_actor_and_no_policy(self, session, registrar):
        """Stated at the route level too: the act that puts the policy on the graph is
        a SERVICE act and writes no grant.

        Two facts in one assertion on purpose — they are the same fact seen from the
        decision row and from the plan row, and separating them would let a reader
        think either alone was sufficient.
        """
        await register_policy_bearing_draft(session, registrar)

        row = (await session.execute(sa.select(OrchestrationDecision))).scalar_one()
        assert row.actor_kind == ActorKind.SERVICE.value
        assert row.kind != DecisionKind.PLAN_ACCEPTED.value
        assert (await in_force(session)).plan_document.get("execution_policy") is None

    async def test_a_bound_rejection_does_not_grant(self, session, registrar, access):
        """Rejecting a plan is a decision about it, not an acceptance of its bounds.

        The gate moves to `rejected_at_gate` and the policy stays inert. A grant here
        would be the worst shape of all: authority delegated by the act of declining
        to delegate it.
        """
        _result, submitted = await register_policy_bearing_draft(session, registrar)
        gate = (await nodes_by_ref(session))[ACCEPTANCE_GATE_REF]
        transformed, _address = transform_for_registration(submitted)

        outcome = await answer_acceptance_gate(session, access, node_id=gate.id, approve=False, expected_plan_hash=plan_hash(transformed))

        assert outcome.status is GateAnswerStatus.APPLIED, outcome.message
        assert (await in_force(session)).plan_document.get("execution_policy") is None
        assert len(await plan_versions(session)) == 1, "a rejection recorded a new plan version"

    async def test_a_wave_gates_approval_does_not_grant(self, session, registrar, access, monkeypatch):
        """A wave gate says "the previous wave's output is good".

        That is not a grant of standing authority over everything downstream, and it
        is answered by whoever is reviewing that wave — potentially many times, at
        many points, by different people. Only the plan's own acceptance gate is an
        acceptance of the plan.

        The wave gate is made answerable directly rather than by driving the graph to
        it. That is harness setup, not a shortcut around the behaviour under test: the
        question here is purely "does answering THIS node grant", and the real
        `apply_gate_answer_for_context` answers it against the real stored plan. Ticking
        the graph forward instead would make the assertion depend on dispatch ordering
        and would have to skip when it did not arrive — a test that skips is a test
        that stops discriminating.
        """
        monkeypatch.setenv(AUTONOMY_FLAG_ENV, "true")
        await register_policy_bearing_draft(session, registrar)
        nodes = await nodes_by_ref(session)
        assert WAVE_GATE_REF in nodes, "fixture must produce a wave gate to answer"

        wave_gate = nodes[WAVE_GATE_REF]
        wave_gate.state = _reg.NodeState.AWAITING_GATE.value
        await session.flush()

        outcome = await answer_acceptance_gate(session, access, node_id=wave_gate.id, expected_plan_hash=(await in_force(session)).plan_hash)

        assert outcome.status is GateAnswerStatus.APPLIED, outcome.message
        assert (await in_force(session)).plan_document.get("execution_policy") is None, "a wave gate's answer granted standing authority"
        assert len(await plan_versions(session)) == 1
        # The acceptance gate is still unanswered, which is the state that makes the
        # assertion above meaningful: the policy is inert because nobody accepted the
        # plan, and a wave gate's answer did not substitute for that.
        assert (await nodes_by_ref(session))[ACCEPTANCE_GATE_REF].state == _reg.NodeState.AWAITING_GATE.value


class TestAnUnboundApprovalCannotArmInertBounds:
    """The failure the demotion itself creates, refused rather than allowed.

    An unbound approval names no revision, so it cannot be read as accepting one —
    which means it cannot grant. But it WOULD pass the acceptance gate and arm every
    root behind it, leaving the policy inert: the graph runs under legacy unbounded
    semantics while its author believes it constrained to their repositories, actions,
    expiry and spend cap.

    Neither permissive option is acceptable. Granting would attribute standing
    authority to a review of content the approver never identified; approving without
    granting is the unbounded-execution case. So it is refused, with a message that
    tells the operator exactly what to do instead.
    """

    async def test_an_unbound_approval_of_a_policy_bearing_plan_is_refused(self, session, registrar, access):
        await register_policy_bearing_draft(session, registrar)
        gate = (await nodes_by_ref(session))[ACCEPTANCE_GATE_REF]

        outcome = await answer_acceptance_gate(session, access, node_id=gate.id, expected_plan_hash=None)

        assert outcome.status is GateAnswerStatus.REFUSED_UNBOUND_POLICY_GRANT, outcome.message
        assert "execution policy" in outcome.message
        # The remedy is named, because a refusal an operator cannot act on is an
        # outage. This one is fixed by re-answering with the reviewed revision.
        assert "hash" in outcome.message

    async def test_the_gate_does_not_move_and_nothing_arms(self, session, registrar, access):
        """The claim that matters is not the status code — it is that the graph did
        not start running with inert bounds."""
        from src.orchestration.dispatch_pass import run_dispatch_pass
        from src.orchestration.tick import run_tick

        await register_policy_bearing_draft(session, registrar)
        gate = (await nodes_by_ref(session))[ACCEPTANCE_GATE_REF]

        await answer_acceptance_gate(session, access, node_id=gate.id, expected_plan_hash=None)

        assert (await nodes_by_ref(session))[ACCEPTANCE_GATE_REF].state == _reg.NodeState.AWAITING_GATE.value
        pending: list = []
        for _ in range(6):
            await run_tick(session)
            report = await run_dispatch_pass(session, dispatch_config())
            pending.extend(report.pending)
        assert pending == [], f"an unbound approval armed a plan whose bounds stayed inert: {pending}"

    async def test_the_refusal_is_recorded_as_evidence(self, session, registrar, access):
        """Someone with approval authority tried to accept a plan in a way that could
        not carry its bounds. A reader of the decision log needs to see that rather
        than infer it from an absence — the same reasoning as the stale-plan refusal.
        """
        await register_policy_bearing_draft(session, registrar)
        gate = (await nodes_by_ref(session))[ACCEPTANCE_GATE_REF]

        await answer_acceptance_gate(session, access, node_id=gate.id, expected_plan_hash=None)

        refusals = [
            row
            for row in await OrchestrationRepository(session).list_decisions(org_id=ORG_A, flow_id=await flow_id_of(session))
            if row.kind == DecisionKind.TRANSITION_REJECTED.value
        ]
        assert len(refusals) == 1
        assert "execution policy" in refusals[0].rejection_reason
        assert refusals[0].actor_id == HUMAN_USER_ID
        # And no approval was recorded alongside it.
        approvals = [
            row
            for row in await OrchestrationRepository(session).list_decisions(org_id=ORG_A, flow_id=await flow_id_of(session))
            if row.kind == DecisionKind.GATE_APPROVED.value
        ]
        assert approvals == []

    async def test_a_policyless_plans_unbound_approval_is_untouched(self, session, registrar, access):
        """The scope of the refusal, asserted from the other side.

        Every flow that exists today is policyless, and both existing callers — the
        dashboard and the GitHub comment path — omit `expected_plan_hash`. If this
        refusal fired on anything wider than a plan that actually proposes a policy,
        it would be a total gate-approval outage dressed up as a safety property.
        """
        await seed_org(session)
        await seed_principal(session, org_id=ORG_A, role=_reg.AdminRole.ORG_ADMIN.value)
        await register_draft_proposal(session, gateless_proposal(), registrar)
        gate = (await nodes_by_ref(session))[ACCEPTANCE_GATE_REF]

        outcome = await answer_acceptance_gate(session, access, node_id=gate.id, expected_plan_hash=None)

        assert outcome.status is GateAnswerStatus.APPLIED, outcome.message
        assert (await nodes_by_ref(session))[ACCEPTANCE_GATE_REF].state == _reg.NodeState.PASSED.value

    async def test_an_unbound_rejection_of_a_policy_bearing_plan_still_works(self, session, registrar, access):
        """Declining a plan needs no revision binding.

        A rejection grants nothing, so it cannot leave bounds inert — there is
        nothing to arm. Refusing it would trap a policy-bearing plan that an operator
        has decided against, leaving the only exit a binding ceremony for a document
        they are declining.
        """
        await register_policy_bearing_draft(session, registrar)
        gate = (await nodes_by_ref(session))[ACCEPTANCE_GATE_REF]

        outcome = await answer_acceptance_gate(session, access, node_id=gate.id, approve=False, expected_plan_hash=None)

        assert outcome.status is GateAnswerStatus.APPLIED, outcome.message
        assert (await nodes_by_ref(session))[ACCEPTANCE_GATE_REF].state == _reg.NodeState.REJECTED_AT_GATE.value


class TestABoundAcceptanceGrants:
    """The positive case, and the ordering consequence it forces."""

    async def test_the_reviewed_revisions_hash_is_what_registration_put_in_force(self, session, registrar):
        """The binding is satisfiable, which is the enabling property for all of
        this. Because the draft path stamps nothing, the hash of the transformed
        document IS the hash in force — so `--expect-plan-hash` now covers the one
        class of plan it previously could not, the class carrying the most delegated
        authority.
        """
        _result, submitted = await register_policy_bearing_draft(session, registrar)
        transformed, _address = transform_for_registration(submitted)

        assert (await in_force(session)).plan_hash == plan_hash(transformed)

    async def test_a_bound_approval_grants_the_policy_at_a_new_version(self, session, registrar, access):
        """The grant itself: promoted, stamped with the approver as principal, and
        recorded as a NEW version rather than a mutation.

        A new version because the revision the human bound to must stay readable
        exactly as they reviewed it — their decision row references it, and rewriting
        it in place would leave that reference pointing at a document that no longer
        exists.
        """
        _result, submitted = await register_policy_bearing_draft(session, registrar)
        gate = (await nodes_by_ref(session))[ACCEPTANCE_GATE_REF]
        transformed, _address = transform_for_registration(submitted)
        reviewed_hash = plan_hash(transformed)

        outcome = await answer_acceptance_gate(session, access, node_id=gate.id, expected_plan_hash=reviewed_hash)
        assert outcome.status is GateAnswerStatus.APPLIED, outcome.message

        versions = await plan_versions(session)
        assert [plan.version for plan in versions] == [1, 2]
        assert versions[0].plan_hash == reviewed_hash, "the reviewed revision must remain readable"
        assert versions[0].superseded_at is not None
        assert versions[1].superseded_at is None

        granted = versions[1].plan_document["execution_policy"]
        assert granted is not None, "the bound acceptance did not grant the policy"
        assert versions[1].plan_document.get("proposed_execution_policy") is None, "the proposal outlived its grant"
        # Server-stamped provenance the acceptor did not supply and cannot influence.
        assert granted["principal_id"] == HUMAN_USER_ID
        assert granted["policy_id"] and granted["policy_hash"]

    async def test_the_grant_is_attributed_to_the_decision_that_made_it(self, session, registrar, access):
        """The new version references the approval it was accepted by, so "who
        granted this authority" is answerable from the plan row alone."""
        _result, submitted = await register_policy_bearing_draft(session, registrar)
        gate = (await nodes_by_ref(session))[ACCEPTANCE_GATE_REF]
        transformed, _address = transform_for_registration(submitted)

        outcome = await answer_acceptance_gate(session, access, node_id=gate.id, expected_plan_hash=plan_hash(transformed))

        granted_version = (await plan_versions(session))[1]
        assert granted_version.accepted_by_decision_id == outcome.decision_id

    async def test_admission_enforces_the_policy_only_after_the_grant(self, session, registrar, access):
        """End to end, through the layer that decides authority.

        `load_in_force_policy` reports no policy before, and the granted policy
        after. This is the same assertion as `TestADraftsPolicyGrantsNothing`'s,
        completed: the field moved from one nothing reads to the one everything does.
        """
        _result, submitted = await register_policy_bearing_draft(session, registrar)
        flow_id = await flow_id_of(session)
        assert (await load_in_force_policy(session, org_id=ORG_A, flow_id=flow_id)).policy is None

        gate = (await nodes_by_ref(session))[ACCEPTANCE_GATE_REF]
        transformed, _address = transform_for_registration(submitted)
        await answer_acceptance_gate(session, access, node_id=gate.id, expected_plan_hash=plan_hash(transformed))

        inputs = await load_in_force_policy(session, org_id=ORG_A, flow_id=flow_id)
        assert inputs.policy is not None, "the accepted policy is not in force"
        assert inputs.plan_version == 2
        assert inputs.policy.repository_ids == submitted.execution_policy.repository_ids
        assert Action.DEVELOP in inputs.policy.allowed_actions

    async def test_retrying_the_bound_approval_after_the_grant_is_a_replay(self, session, registrar, access):
        """The ordering bug this design creates, pinned.

        Granting records a new plan version, so the moment the approval succeeds the
        hash it was bound to is no longer in force — by design, because what is in
        force now includes authority the reviewed revision only proposed. A caller
        whose HTTP response was lost then retries the identical request. Under a
        staleness-first ordering it would be told its revision was stale: a refusal
        recorded against a decision that had already taken effect, sending an operator
        to re-read a plan whose approval already succeeded.

        So the replay identity — (actor, verb, exact bound hash, gate) — is consulted
        BEFORE staleness, and it deliberately does not depend on what is currently in
        force. A recorded decision by this actor, this verb, this gate, bound to this
        hash is proof this person made this decision about this document.
        """
        _result, submitted = await register_policy_bearing_draft(session, registrar)
        gate = (await nodes_by_ref(session))[ACCEPTANCE_GATE_REF]
        transformed, _address = transform_for_registration(submitted)
        reviewed_hash = plan_hash(transformed)

        first = await answer_acceptance_gate(session, access, node_id=gate.id, expected_plan_hash=reviewed_hash)
        assert first.status is GateAnswerStatus.APPLIED, first.message
        # The premise of the whole test: the bound hash really is no longer in force.
        assert (await in_force(session)).plan_hash != reviewed_hash

        retry = await answer_acceptance_gate(session, access, node_id=gate.id, expected_plan_hash=reviewed_hash)

        assert retry.status is GateAnswerStatus.IDEMPOTENT_REPLAY, retry.message
        assert retry.decision_id == first.decision_id, "the retry must return the original decision"

    async def test_the_retry_grants_no_second_policy_version(self, session, registrar, access):
        """The consequence that makes the replay worth having. A second grant would
        re-stamp the policy and record a third plan version, so "how many times was
        this authority delegated" would depend on how many times an HTTP response was
        lost."""
        _result, submitted = await register_policy_bearing_draft(session, registrar)
        gate = (await nodes_by_ref(session))[ACCEPTANCE_GATE_REF]
        transformed, _address = transform_for_registration(submitted)
        reviewed_hash = plan_hash(transformed)

        await answer_acceptance_gate(session, access, node_id=gate.id, expected_plan_hash=reviewed_hash)
        await answer_acceptance_gate(session, access, node_id=gate.id, expected_plan_hash=reviewed_hash)

        assert [plan.version for plan in await plan_versions(session)] == [1, 2]
        approvals = [
            row
            for row in await OrchestrationRepository(session).list_decisions(org_id=ORG_A, flow_id=await flow_id_of(session))
            if row.kind == DecisionKind.GATE_APPROVED.value
        ]
        assert len(approvals) == 1, "the retry wrote a second approval"

    async def test_a_second_bound_approval_by_another_human_is_not_a_replay(self, session, registrar, access):
        """The replay key includes the actor, so a different person presenting the
        same reviewed hash is a conflict rather than an inherited grant. Otherwise
        anyone who could read the plan could claim to have made the acceptance."""
        _result, submitted = await register_policy_bearing_draft(session, registrar)
        gate = (await nodes_by_ref(session))[ACCEPTANCE_GATE_REF]
        transformed, _address = transform_for_registration(submitted)
        reviewed_hash = plan_hash(transformed)

        first = await answer_acceptance_gate(session, access, node_id=gate.id, expected_plan_hash=reviewed_hash)
        assert first.status is GateAnswerStatus.APPLIED, first.message

        second = await answer_acceptance_gate(session, access, node_id=gate.id, expected_plan_hash=reviewed_hash, user_id="another-human")

        assert second.status is not GateAnswerStatus.IDEMPOTENT_REPLAY
        assert [plan.version for plan in await plan_versions(session)] == [1, 2]

    async def test_an_authored_accept_gate_deeper_in_the_graph_does_not_grant(self, session, registrar, access):
        """`acceptance_gate_address` is structural, not a suffix match.

        An author can name a node anything, including `accept`. If the grant were
        keyed on the last address segment — the obvious implementation, and the one a
        reader of `ACCEPTANCE_GATE_REF` would reach for — an author could place their
        own `accept` gate mid-graph and have its approval activate standing authority
        over the whole plan. Whoever reviews that mid-graph point is reviewing a
        wave's output, not consenting to the plan's bounds.

        The identification therefore requires the SOLE ROOT of the graph, which only
        the server's inserted gate can be: `insert_acceptance_gate` points the new
        gate at every prior root, and rule 3 rejects cycles, so a valid transformed
        document has exactly one.
        """
        # The author's gate is named `accept` and sits in wave 2, so its address
        # differs from the server's fixed `{flow}/epic-1/wave-1/accept` while its
        # node_ref is identical. That is precisely the collision a suffix match would
        # not survive.
        authors_gate_address = address(ACCEPTANCE_GATE_REF, wave="wave-2")
        authored = author_gated_proposal()
        authored = authored.model_copy(
            update={
                "nodes": [
                    node.model_copy(update={"address": authors_gate_address}) if node.address.endswith("/my-gate") else node
                    for node in authored.nodes
                ],
                "edges": [
                    edge.model_copy(
                        update={
                            "from_address": authors_gate_address if edge.from_address.endswith("/my-gate") else edge.from_address,
                            "to_address": authors_gate_address if edge.to_address.endswith("/my-gate") else edge.to_address,
                        }
                    )
                    for edge in authored.edges
                ],
            }
        )
        _result, submitted = await register_policy_bearing_draft(session, registrar, proposal=authored)
        _transformed, server_gate = transform_for_registration(submitted)
        assert server_gate != authors_gate_address, "fixture must make the two gates distinct addresses"

        # Answered directly, for the same reason as the wave-gate test: the question is
        # whether answering THIS node grants, and driving the graph here would make the
        # assertion depend on dispatch ordering.
        authors_gate_node = next(
            node
            for node in await OrchestrationRepository(session).list_nodes(org_id=ORG_A, flow_id=await flow_id_of(session))
            if f"{FLOW}/{node.epic_ref}/{node.wave_ref}/{node.node_ref}" == authors_gate_address
        )
        authors_gate_node.state = _reg.NodeState.AWAITING_GATE.value
        await session.flush()

        outcome = await answer_acceptance_gate(session, access, node_id=authors_gate_node.id, expected_plan_hash=(await in_force(session)).plan_hash)

        assert outcome.status is GateAnswerStatus.APPLIED, outcome.message
        assert (await in_force(session)).plan_document.get("execution_policy") is None, (
            "an author-named 'accept' gate mid-graph granted standing authority over the whole plan"
        )
        assert len(await plan_versions(session)) == 1

    def test_only_the_sole_root_is_identified_as_the_acceptance_gate(self):
        """The structural rule, asserted on the resolver directly.

        The end-to-end test above is necessary but NOT sufficient, and finding that out
        is the reason this one exists. `insert_acceptance_gate` prepends the server's
        gate to the node list, so a resolver that merely scanned for the first
        gate-kinded node whose address ends in `/accept` returns the correct node for
        every document these fixtures produce — the end-to-end assertion passes under
        the wrong implementation. Verified by reverting `acceptance_gate_address` to
        exactly that suffix scan: all 24 tests in this file still passed.

        So the discrimination has to be made where the ambiguity is. Here the author's
        `accept`-named gate is the *only* one present and the document has several
        roots, which is what an untransformed or hand-built document looks like. A
        suffix scan returns the author's gate; the structural rule returns None,
        because no single node dominates the graph and therefore no node's approval
        can mean "this whole plan may run".
        """
        from src.orchestration.registration import acceptance_gate_address

        authored = author_gated_proposal()
        # Rename the author's gate to the reserved ref, leaving the graph otherwise
        # untouched — so `story-a` and the author's gate are both roots.
        renamed = address(ACCEPTANCE_GATE_REF, wave="wave-2")
        several_roots = authored.model_copy(
            update={
                "nodes": [node.model_copy(update={"address": renamed}) if node.address.endswith("/my-gate") else node for node in authored.nodes],
                "edges": [edge for edge in authored.edges if "/my-gate" not in (edge.from_address, edge.to_address)],
            }
        )

        assert [node.address for node in several_roots.nodes if node.address.endswith(f"/{ACCEPTANCE_GATE_REF}")] == [renamed], (
            "fixture must present exactly one accept-named gate, so a suffix scan would find it"
        )
        assert acceptance_gate_address(several_roots) is None, "a gate that does not dominate the graph was identified as the plan's acceptance gate"

    def test_the_transformed_documents_sole_root_is_identified(self):
        """The positive half, so the rule above is not satisfiable by returning None.

        A resolver that always returned None would pass every negative test in this
        file — and would silently disable the grant path entirely, turning a
        policy-bearing plan into one whose bounds can never be activated.
        """
        from src.orchestration.registration import acceptance_gate_address

        transformed, gate_address = transform_for_registration(
            gateless_proposal().model_copy(update={"execution_policy": policy_for_these_fixtures()})
        )

        assert acceptance_gate_address(transformed) == gate_address


class TestBoundsAlreadyExpiredAreNotGranted:
    """Authority whose lifetime is already over is refused, not granted (#5331).

    A proposed policy carries an `expires_at` chosen when the plan was derived, and a
    plan can sit at its acceptance gate for as long as its approver takes to read it.
    So "the bounds were in the future when they were written" is not the same claim as
    "the bounds are in the future now", and the grant path is the only place the second
    one can be checked — `stamp_policy` binds a principal and a hash and says nothing
    about lifetime.

    Granting a dead grant is not a cosmetic wrong. The gate passes, every root behind
    it arms, the plan reads as authorized — and then `authorize_action` denies every
    single dispatch with `POLICY_EXPIRED`, because that is exactly what admission is
    supposed to do with an expired policy. The flow is live, approved, and incapable
    of doing anything, with nothing in the approver's view explaining why.

    It is refused rather than **silently re-clocked to a fresh expiry**, which is the
    tempting repair and the wrong one. The approver read a document that said when the
    authority lapses; issuing different bounds than the ones they read would attribute
    to them a grant they never reviewed — the precise misattribution the revision
    binding exists to prevent. Refusing costs them one round trip and a re-derived
    plan they can read.
    """

    @staticmethod
    def _expired_policy():
        """Bounds whose lifetime ended an hour ago.

        Relative to the real clock rather than a fixed past date, because an expiry
        check compares against `now`: a literal like 2020 would still be in the past
        in 2030, but it would also let a check that compared against the *wrong* now
        (a hardcoded epoch, a naive-vs-aware mixup) pass for the wrong reason.
        """
        from datetime import UTC, datetime, timedelta

        return policy_for_these_fixtures(expires_at=datetime.now(tz=UTC) - timedelta(hours=1))

    async def test_a_bound_approval_of_expired_bounds_is_refused(self, session, registrar, access):
        """The refusal, raised out of the gate answer rather than returned.

        `PolicyNotAcceptableError` and not a refusal outcome, because the caller's
        transaction must not commit: the gate move and the decision row are already
        written at this point, and the whole reason this is raised is so they roll back
        with the grant instead of persisting an approval that armed nothing.
        """
        await register_policy_bearing_draft(session, registrar, policy=self._expired_policy())
        gate = (await nodes_by_ref(session))[ACCEPTANCE_GATE_REF]

        with pytest.raises(PolicyNotAcceptableError, match="expired"):
            await answer_acceptance_gate(session, access, node_id=gate.id, expected_plan_hash=(await in_force(session)).plan_hash)

    async def test_nothing_is_granted_and_no_new_version_is_recorded(self, session, registrar, access):
        """The claim that matters is not the exception type — it is that no authority
        exists afterwards.

        Asserted through `load_in_force_policy`, the function admission actually reads,
        for the same reason `TestADraftsPolicyGrantsNothing` does: an assertion about
        which JSON key holds the policy would pass even if something had started
        enforcing the refused one.

        The `commit` after registration and the `rollback` after the refusal are what
        make this the real shape rather than a convenient one. In production the
        registration request commits and the gate answer is a separate transaction the
        route abandons on an exception — here both share one session, so without the
        commit the rollback would discard the registration too and every assertion
        below would pass against an empty database.
        """
        await register_policy_bearing_draft(session, registrar, policy=self._expired_policy())
        await session.commit()
        gate = (await nodes_by_ref(session))[ACCEPTANCE_GATE_REF]

        with pytest.raises(PolicyNotAcceptableError):
            await answer_acceptance_gate(session, access, node_id=gate.id, expected_plan_hash=(await in_force(session)).plan_hash)
        await session.rollback()

        assert (await load_in_force_policy(session, org_id=ORG_A, flow_id=await flow_id_of(session))).policy is None
        assert len(await plan_versions(session)) == 1, "a refused grant recorded a new plan version"
        assert (await nodes_by_ref(session))[ACCEPTANCE_GATE_REF].state == _reg.NodeState.AWAITING_GATE.value, (
            "the gate did not stay answerable, so the approver cannot retry after re-deriving the plan"
        )

    async def test_the_refusal_names_the_remedy(self, session, registrar, access):
        """A refusal an operator cannot act on is an outage.

        The message has to distinguish this from the other things that refuse a grant
        (a service acceptor, a stale hash, an inert field) and say what to do — the
        plan needs re-deriving, not re-answering, because re-answering the same
        document would produce the same dead bounds.
        """
        await register_policy_bearing_draft(session, registrar, policy=self._expired_policy())
        gate = (await nodes_by_ref(session))[ACCEPTANCE_GATE_REF]

        with pytest.raises(PolicyNotAcceptableError) as error:
            await answer_acceptance_gate(session, access, node_id=gate.id, expected_plan_hash=(await in_force(session)).plan_hash)

        message = str(error.value)
        assert "expired" in message
        assert "plan" in message, f"the refusal does not tell the approver a new plan is needed: {message!r}"

    def test_a_direct_acceptance_of_expired_bounds_is_refused_too(self):
        """The same refusal at the function every acceptance path shares.

        The gate path is not the only way a policy reaches `accept_execution_policy` —
        the amendment path and the direct-submission path both call it, and a
        hand-authored or file-prepared document can carry a past expiry from its
        author with no planning session involved at all. Asserted here so the guarantee
        belongs to the single authority point rather than to one of its callers.
        """
        submitted = gateless_proposal().model_copy(update={"execution_policy": self._expired_policy()})

        with pytest.raises(PolicyNotAcceptableError, match="expired"):
            accept_execution_policy(
                submitted,
                decision=ApprovalContext(org_id=ORG_A, actor_id=HUMAN_USER_ID, actor_role="org_admin", actor_kind=ActorKind.HUMAN),
                decision_kind=DecisionKind.PLAN_ACCEPTED,
            )

    def test_unexpired_bounds_are_still_accepted(self):
        """The scope of the refusal, from the other side.

        A guard that refused everything would pass every test above and disable the
        grant path entirely — the inverse failure, and the one that turns a safety
        check into an outage.
        """
        submitted = gateless_proposal().model_copy(update={"execution_policy": policy_for_these_fixtures()})

        granted = accept_execution_policy(
            submitted,
            decision=ApprovalContext(org_id=ORG_A, actor_id=HUMAN_USER_ID, actor_role="org_admin", actor_kind=ActorKind.HUMAN),
            decision_kind=DecisionKind.PLAN_ACCEPTED,
        )

        assert granted.execution_policy is not None and granted.execution_policy.policy_id


class TestTenantIsolationOfAGrant:
    """A grant is scoped to the tenant whose plan it is."""

    async def test_a_foreign_tenants_gate_cannot_reach_this_plan(self, session, registrar, access):
        """The node is re-resolved under the answering caller's org in SQL, so a node
        id from another tenant resolves to nothing. Asserted on the policy
        specifically because this story added a new write (a plan version) behind the
        gate answer, and a new write is a new place isolation can be missed.
        """
        _result, _submitted = await register_policy_bearing_draft(session, registrar)
        gate = (await nodes_by_ref(session))[ACCEPTANCE_GATE_REF]

        outcome = await apply_gate_answer_for_context(
            session,
            context=token_context(_reg.ORG_B, user_id=HUMAN_USER_ID),
            node_id=gate.id,
            approve=True,
            reason="A different tenant answering someone else's gate.",
            access=access,
            input_path=InputPath.DASHBOARD,
            expected_plan_hash=(await in_force(session)).plan_hash,
        )

        assert outcome.status is GateAnswerStatus.REFUSED_NOT_FOUND
        assert (await in_force(session)).plan_document.get("execution_policy") is None
        assert len(await plan_versions(session)) == 1


class TestResourceRefIsNotFakedHere:
    """A guard on this test module itself, not on the code under test.

    `ResourceRef` is imported so the admission assertions above can be read against
    the real type rather than a dict that happens to have the right keys. Asserted so
    the import cannot rot into an unused one that a future edit silently replaces with
    a hand-rolled stub.
    """

    def test_resource_ref_is_the_real_policy_type(self):
        from src.orchestration import execution_policy

        assert ResourceRef is execution_policy.ResourceRef


@pytest.mark.asyncio
async def test_registration_retry_after_policy_promotion_preserves_the_grant(session, registrar, access):
    first, submitted = await register_policy_bearing_draft(session, registrar)
    gate = (await nodes_by_ref(session))[ACCEPTANCE_GATE_REF]
    answer = await answer_acceptance_gate(session, access, node_id=gate.id, expected_plan_hash=first.plan_hash)
    assert answer.status is GateAnswerStatus.APPLIED
    granted = await in_force(session)
    assert granted.plan_hash != first.plan_hash
    before_versions = len(await plan_versions(session))
    retry, _ = await register_draft_proposal(session, submitted, registrar)
    assert retry.already_compiled
    assert retry.plan_hash == first.plan_hash
    assert retry.flow_id == first.flow_id
    assert retry.nodes_created == retry.edges_created == 0
    assert (await in_force(session)).id == granted.id
    assert len(await plan_versions(session)) == before_versions
    replay = await answer_acceptance_gate(session, access, node_id=gate.id, expected_plan_hash=retry.plan_hash)
    assert replay.status is GateAnswerStatus.IDEMPOTENT_REPLAY
    assert replay.decision_id == answer.decision_id


@pytest.mark.parametrize("actor_kind", [ActorKind.HUMAN, ActorKind.SERVICE])
def test_direct_acceptance_cannot_leave_proposed_bounds_unenforced(actor_kind):
    submitted = gateless_proposal().model_copy(update={"execution_policy": policy_for_these_fixtures()})
    draft, _ = transform_for_registration(submitted)
    with pytest.raises(PolicyNotAcceptableError, match="cannot remain inert"):
        accept_execution_policy(
            draft,
            decision=ApprovalContext(org_id=ORG_A, actor_id=HUMAN_USER_ID, actor_role="org_admin", actor_kind=actor_kind),
            decision_kind=DecisionKind.PLAN_ACCEPTED,
        )


@pytest.mark.asyncio
async def test_inert_registration_can_preserve_an_evaluation_specification(session, registrar, access):
    submitted = gateless_proposal()
    evaluation = next(node for node in submitted.nodes if node.kind == "eval")
    # A valid human evaluation specification, as the shared contract defines it.
    from tests.orchestration.test_evaluation_proposal import with_suite

    evaluation.evaluation = next(node for node in with_suite().nodes if node.kind == "eval").evaluation
    result, _ = await register_policy_bearing_draft(session, registrar, proposal=submitted)
    stored = await in_force(session)
    assert stored.plan_document["execution_policy"] is None
    assert next(node for node in stored.plan_document["nodes"] if node["kind"] == "eval")["evaluation"] == evaluation.evaluation
    gate = (await nodes_by_ref(session))[ACCEPTANCE_GATE_REF]
    assert gate.state == "awaiting_gate"
    assert result.nodes_created > 0
