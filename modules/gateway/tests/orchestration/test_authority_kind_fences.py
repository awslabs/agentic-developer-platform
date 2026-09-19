"""Recognized-authority handling at the four surfaces that used to fail open (#4529).

Why this file exists
--------------------

Four decision points asked "is this the engine authority kind?" by inequality and
treated every other answer as unrestricted rather than unrecognized:

======================================  ======================================
Surface                                 Fail-open behaviour before #4529
======================================  ======================================
`runtime_policy.authorize_worker_       `!= "gate_decision"` returned an
credential`                             unconditional `permit`, at the function
                                        fronting GitHub installation-token
                                        minting and model credentials.
`dispatch.DispatchService.prepare`      A verified graph assignment was required
                                        only *of* `gate_decision`, so another
                                        kind dispatched without proving one.
`model_identity` middleware             Policy admission and budget binding were
                                        entered only for `gate_decision`, and
                                        `flow_id` was dropped from the run
                                        binding otherwise — spend happened, just
                                        unattributed to the flow causing it.
`work_admission.admit_pending`          Any other kind filed its claim as
                                        `DIRECT_DISPATCH`, an `owner_kind`
                                        nothing can correctly reconcile.
======================================  ======================================

Each is now an enumeration plus a refusal. These tests are organised **one class per
fence**, and that structure is the point rather than tidiness: the standard for this
work is that reverting any single fence makes a *specific* named test fail while the
others keep passing. A test that failed for every mutation would prove the fences
exist collectively and localise none of them.

Every case drives the **production entry point** and presents the authority kind the
way the real system would: rewritten in the persisted authority and grant records for
the DynamoDB-backed surfaces, carried on the live grant for the SQL one. Asserting on
module source instead would pass against a fence that could never actually be reached.

Getting that probe wrong is the specific trap here, and it is worth naming because the
first version of this file fell into it. The authority kind is recorded in more than
one place and cross-checked between them, so rewriting it in only one produces a
refusal from the *consistency* check rather than from the fence — and from the outside
that is indistinguishable from success: the call raises, nothing publishes, the test
passes. `_store_kind` exists to keep the kind the only difference from a legitimate
caller, and the mutation table is what confirms it: reverting each fence must fail its
own tests and no others.

`replan_request` is the kind these fences were written for, so it is the probe
throughout — but every case also covers an entirely unknown kind, because the
guarantee being pinned is "an authority kind nobody taught this surface about is
refused", not "this one string is refused".

The sibling-kind regressions live here too. `github_event`, `service_policy` and
`gate_decision` are load-bearing for other stories, and breaking one of them while
fixing this would be worse than the original defect.
"""

import json
from dataclasses import replace

import pytest

from src.agentauth.bootstrap import BootstrapRefusedError
from src.agentauth.grants import (
    AUTHORITY_GATE_DECISION,
    AUTHORITY_GITHUB_EVENT,
    AUTHORITY_REPLAN_REQUEST,
    AUTHORITY_SERVICE_POLICY,
    RECOGNIZED_AUTHORITY_KINDS,
    AuthorityReference,
    DelegatedGrant,
)
from src.orchestration.execution_policy import DenyReason
from src.orchestration.runtime_policy import authorize_worker_credential
from src.orchestration.state import NodeState
from src.orchestration.work_claims import WorkClaimError
from src.shared.models.audit import AuditLog  # noqa: F401 — register before fixture creates schema
from tests.agentauth import test_human_dispatch as human
from tests.agentauth.test_model_identity import PROOF, call
from tests.agentauth.test_model_identity import context as model_context_fixture
from tests.agentauth.test_model_identity import runtime as model_runtime_fixture
from tests.orchestration import test_work_claims as sql
from tests.orchestration.test_policy_admission import _limits, _policy
from tests.orchestration.test_policy_admission import (
    engine as engine_fixture,
)
from tests.orchestration.test_policy_admission import (
    healthy_policy_reservations as reservations_fixture,
)
from tests.orchestration.test_policy_admission import (
    policy_budget_initializers as initializers_fixture,
)
from tests.orchestration.test_policy_admission import (
    session as session_fixture,
)
from tests.orchestration.test_runtime_policy import _assignment

# Fence 1 (SQL credential boundary) and fences 2/4 (DynamoDB dispatch + claims) need
# different harnesses, so both existing ones are imported rather than a third
# invented. A lookalike fixture that disagreed with the real harness about what a
# protected execution looks like would be the easiest way to write passing tests that
# prove nothing.
engine = engine_fixture
healthy_policy_reservations = reservations_fixture
session = session_fixture
policy_budget_initializers = initializers_fixture

context = model_context_fixture
runtime = model_runtime_fixture

store = human.store
child_dispatch = human.child_dispatch
claims_engine = sql.engine
session_factory = sql.session_factory
claims_session = sql.session

GITHUB = "/internal/v1/github-installation-token"

# An authority kind this platform does not issue at all. Present alongside
# `replan_request` in every case because the fences must refuse the *unrecognized*,
# not one specific known-bounded kind. A future kind added to `grants.py` without
# visiting these surfaces behaves like this one.
UNKNOWN_KIND = "some_future_authority_kind"


def _with_kind(grant: DelegatedGrant, kind: str) -> DelegatedGrant:
    """The same grant, presenting a different authority kind.

    Only `kind` changes: the reference id, human and tenant stay real and resolvable.
    So a refusal below is attributable to the kind alone, not to a grant that became
    malformed in some other way at the same time — which is the confound that would
    make these tests prove nothing.
    """
    authority = AuthorityReference(kind, grant.authority.reference_id, grant.authority.human_id, grant.authority.org_id)
    return replace(grant, authority=authority)


def _store_kind(store, invocation: str, kind: str, *, tenant: str = "tenant", attempt: int = 1) -> None:
    """Restate one tenant's authority under a different kind, consistently.

    The DynamoDB-backed surfaces re-read their grant through `live_grant` on every call
    and hydrate `AuthorityReference.kind` from it, so rewriting the stored kind is how
    an unrecognized authority reaches them in production.

    It has to be rewritten in *every* place the kind is recorded — the `AUTHORITY#`
    record and each grant derived from it — because `live_grant` cross-checks the two
    and a delegated child re-walks its whole lineage doing the same. A partial rewrite
    is refused with a generic "authority refused" before the surface under test is
    reached, which looks exactly like success from the outside: the call raises, the
    queue stays empty, the test goes green, and the fence was never exercised. Writing
    all of them keeps the kind the single difference from a legitimate caller, so a
    refusal below is attributable to the kind and nothing else.

    Two things stay deliberately untouched: signature/expiry/reservations, and the
    grants' allowed actions. A grant that also lost `DISPATCH` would confound fence 2
    with the independent action check that sits beside it.
    """
    grant_row = store._read(f"TENANT#{tenant}", f"GRANT#{invocation}#{attempt}")
    reference = grant_row["authority_reference_id"]["S"]
    page = store.client.query(
        TableName=store.table,
        KeyConditionExpression="pk = :pk",
        ExpressionAttributeValues={":pk": {"S": f"TENANT#{tenant}"}},
    )
    # Every grant derived from this authority, plus the authority record itself. A
    # delegated child's validation re-walks its lineage and re-checks each ancestor
    # against the SAME authority record, so updating one row in isolation makes some
    # link in that chain disagree with it.
    targets = [f"AUTHORITY#{reference}"] + [
        item["sk"]["S"]
        for item in page.get("Items", [])
        if item["sk"]["S"].startswith("GRANT#") and item.get("authority_reference_id") == {"S": reference}
    ]
    for sk in targets:
        store.client.update_item(
            TableName=store.table,
            Key={"pk": {"S": f"TENANT#{tenant}"}, "sk": {"S": sk}},
            UpdateExpression="SET authority_kind = :kind",
            ExpressionAttributeValues={":kind": {"S": kind}},
        )


# ---------------------------------------------------------------------------------
# The vocabulary itself
# ---------------------------------------------------------------------------------


class TestRecognizedVocabulary:
    def test_the_four_kinds_are_the_whole_vocabulary(self):
        """Pinned as a set so adding a kind is a deliberate, reviewable edit.

        `AuthorityReference.kind`'s docstring says a new initiation path "adds a kind
        rather than loosening this one". This assertion is what makes that a rule
        instead of an aspiration: a new kind fails here first, which is the prompt to
        go and decide what each of the four surfaces below should do about it.
        """
        assert RECOGNIZED_AUTHORITY_KINDS == {
            AUTHORITY_GATE_DECISION,
            AUTHORITY_GITHUB_EVENT,
            AUTHORITY_SERVICE_POLICY,
            AUTHORITY_REPLAN_REQUEST,
        }

    def test_replan_request_cannot_root_an_execution_chain(self):
        """The ruling's explicit prohibition, pinned where it is cheapest to check.

        A replan is a human *asking* for a plan change. If `REPLAN_REQUESTED` were an
        approval kind, that request could authorize the very change it asks for, and
        the distinction the whole gate model rests on would collapse.
        """
        from src.orchestration.genesis import APPROVAL_DECISION_KINDS
        from src.orchestration.models import DecisionKind

        assert DecisionKind.REPLAN_REQUESTED.value not in APPROVAL_DECISION_KINDS


# ---------------------------------------------------------------------------------
# Fence 1 — the worker credential boundary
# ---------------------------------------------------------------------------------


class TestCredentialFence:
    """`runtime_policy.authorize_worker_credential`.

    The most consequential of the four: it fronts installation-token minting and model
    credentials, and its fail-open arm returned `permit` unconditionally.
    """

    @pytest.fixture
    async def assignment(self, session):
        return await _assignment(session, policy=_policy(limits=_limits(max_wall_clock_seconds=7200)))

    async def test_gate_decision_still_obtains_its_credential(self, session, assignment):
        """Regression: the one kind that legitimately passes here is unaffected."""
        decision = await authorize_worker_credential(session, execution=assignment.execution, grant=assignment.grant, broker_path=GITHUB)
        assert decision.permitted

    @pytest.mark.parametrize("kind", [AUTHORITY_GITHUB_EVENT, AUTHORITY_SERVICE_POLICY])
    async def test_pre_existing_non_engine_kinds_keep_their_legacy_permit(self, session, assignment, kind):
        """Regression, and the reason this fence enumerates rather than denies all.

        These two kinds have always reached here with no accepted engine policy and
        been permitted on that basis. That behaviour is other stories' contract; the
        #4529 change must not tighten it. Only the *unrecognized* case changes.
        """
        decision = await authorize_worker_credential(
            session, execution=assignment.execution, grant=_with_kind(assignment.grant, kind), broker_path=GITHUB
        )
        assert decision.permitted
        assert decision.reason is None

    @pytest.mark.parametrize("kind", [AUTHORITY_REPLAN_REQUEST, UNKNOWN_KIND])
    async def test_unrecognized_kind_is_denied_a_worker_credential(self, session, assignment, kind):
        """THE fence. Reverting the deny in `runtime_policy` fails exactly this test.

        A `permit` here would hand an authoring run — or any future kind — the path to
        installation-token minting. The typed reason matters as much as the refusal:
        `AUTHORITY_KIND_NOT_RECOGNIZED` records that nothing was evaluated about what
        the caller wanted to do, which is a different audit fact from "it asked for too
        much".
        """
        decision = await authorize_worker_credential(
            session, execution=assignment.execution, grant=_with_kind(assignment.grant, kind), broker_path=GITHUB
        )
        assert not decision.permitted
        assert decision.reason is DenyReason.AUTHORITY_KIND_NOT_RECOGNIZED

    @pytest.mark.parametrize(
        "path",
        [
            GITHUB,
            "model",
            "/internal/v1/credential-raw-read",
            "/internal/v1/worker-task-credentials",
            "/internal/v1/user-credentials",
        ],
    )
    async def test_an_authoring_kind_is_denied_at_every_broker_path(self, session, assignment, path):
        """The refusal is not path-specific, checked rather than assumed.

        The fence is the first statement in the function, so no broker path can reach
        the credential-scoping logic below it. Asserted across the paths that mint or
        materialise real provider/user credentials, because "denied at the GitHub path"
        would be a much weaker guarantee than the one being claimed.
        """
        decision = await authorize_worker_credential(
            session, execution=assignment.execution, grant=_with_kind(assignment.grant, AUTHORITY_REPLAN_REQUEST), broker_path=path
        )
        assert not decision.permitted
        assert decision.reason is DenyReason.AUTHORITY_KIND_NOT_RECOGNIZED

    async def test_the_refusal_precedes_any_assignment_read(self, session, assignment):
        """Denied before the function looks at execution state at all.

        Checked by handing it an execution that is *also* invalid in another way: a node
        id that does not exist. If the kind fence ran after the assignment read, the
        reason would be an assignment refusal instead. Order matters because a refusal
        that first queried the graph would let an unrecognized caller distinguish "this
        node exists" from "it does not" by the shape of its rejection.
        """
        assignment.execution["orchestration_node_id"] = {"S": "no-such-node"}
        decision = await authorize_worker_credential(
            session, execution=assignment.execution, grant=_with_kind(assignment.grant, AUTHORITY_REPLAN_REQUEST), broker_path=GITHUB
        )
        assert decision.reason is DenyReason.AUTHORITY_KIND_NOT_RECOGNIZED

    async def test_a_denied_authority_kind_leaves_the_accepted_graph_untouched(self, session, assignment):
        """A refusal writes nothing.

        Stated because a fence that mutated state on denial would be a worse defect
        than the fail-open it replaced: an unrecognized caller could then move a real
        flow's node simply by being refused.
        """
        from sqlalchemy import select

        from src.orchestration.models import OrchestrationNode

        await authorize_worker_credential(
            session, execution=assignment.execution, grant=_with_kind(assignment.grant, AUTHORITY_REPLAN_REQUEST), broker_path=GITHUB
        )
        node = await session.scalar(select(OrchestrationNode).where(OrchestrationNode.id == assignment.node.id))
        assert node.state == NodeState.RUNNING.value
        assert node.attempts == 1


# ---------------------------------------------------------------------------------
# Fence 2 — dispatch
# ---------------------------------------------------------------------------------


class TestDispatchFence:
    """`DispatchService.prepare`, driven through the real `dispatch()` entry point.

    The probe rewrites the persisted `authority_kind` of the caller's own grant, which
    is the attribute `live_grant` hydrates on every dispatch — so the refusal below is
    the one production reaches, not a constructed object handed past the door.
    """

    def test_a_recognized_dispatching_kind_still_queues_its_child(self, store, child_dispatch):
        """Regression: the webhook path's developer→reviewer dispatch is unaffected.

        The fixture's parent holds `github_event`, so this is the control for every
        mutation below: same call, same harness, one recorded kind different.
        """
        result = human.send_child(child_dispatch)
        assert result["status"] == "accepted"
        messages = child_dispatch.sqs.receive_message(QueueUrl=child_dispatch.queue, MaxNumberOfMessages=10)["Messages"]
        assert len(messages) == 1

    @pytest.mark.parametrize("kind", [AUTHORITY_REPLAN_REQUEST, UNKNOWN_KIND])
    def test_unrecognized_kind_cannot_dispatch_and_publishes_nothing(self, store, child_dispatch, kind):
        """THE fence. Reverting the deny in `dispatch.prepare` fails exactly this test.

        For `replan_request` this is the substantive prohibition: an authoring run
        exists to *propose* a plan change, and a proposal that could spawn executing
        work would be an amendment applying itself. The queue assertion is the part
        that matters — a refusal that still published would have commissioned the work
        it claimed to refuse.

        This is the second of two independent fences on that. The grant minted for an
        authoring assignment also omits `DISPATCH`, so neither one alone is
        load-bearing.
        """
        _store_kind(store, child_dispatch.invocation, kind)
        with pytest.raises(BootstrapRefusedError, match="authority kind may not dispatch"):
            human.send_child(child_dispatch)
        assert not child_dispatch.sqs.receive_message(QueueUrl=child_dispatch.queue).get("Messages")

    def test_a_refused_kind_cannot_publish_by_retrying(self, store, child_dispatch):
        """Redelivery does not accumulate into a dispatch.

        The dispatch path is deliberately idempotent on a caller-chosen `request_id`, so
        the interesting question is not whether one refusal holds but whether the
        *second* attempt finds a reserved intent from the first and completes it. It
        must not: the refusal precedes the reservation.
        """
        _store_kind(store, child_dispatch.invocation, AUTHORITY_REPLAN_REQUEST)
        for _ in range(2):
            with pytest.raises(BootstrapRefusedError, match="authority kind may not dispatch"):
                human.send_child(child_dispatch)
        assert not child_dispatch.sqs.receive_message(QueueUrl=child_dispatch.queue).get("Messages")

    def test_the_refusal_does_not_consume_the_callers_dispatch_budget(self, store, child_dispatch):
        """A kind that may not dispatch at all is refused before reserving concurrency.

        Ordering evidence, and the reason the fence sits above the eligibility read: if
        it were below, an unrecognized caller could exhaust a legitimate parent's
        in-flight reservations by repeatedly being refused.
        """
        _store_kind(store, child_dispatch.invocation, UNKNOWN_KIND)
        with pytest.raises(BootstrapRefusedError, match="authority kind may not dispatch"):
            human.send_child(child_dispatch)
        parent_grant = store._read("TENANT#tenant", f"GRANT#{child_dispatch.invocation}#1")["grant_id"]["S"]
        counter = store._read("TENANT#tenant", f"RESV#{parent_grant}") or {}
        assert counter.get("in_flight", {"N": "0"}) == {"N": "0"}
        assert counter.get("total_dispatched", {"N": "0"}) == {"N": "0"}


# ---------------------------------------------------------------------------------
# Fence 3 — model metering
# ---------------------------------------------------------------------------------


class TestMeteringFence:
    """The `model_identity` middleware, driven as the ASGI boundary it is.

    This fence differs from the other three in kind, not just in degree: an authoring
    run genuinely **needs** model access, so the correct handling is to *meter* it, not
    to deny it. The defect was that a non-`gate_decision` kind spent money with
    `flow_id` dropped from its run binding — invisible to flow-level accounting rather
    than blocked.
    """

    @staticmethod
    def _present(runtime, kind: str) -> None:
        """Make the authenticated grant present `kind` to the middleware."""
        runtime.authenticate("credential", "pod")[3].authority.kind = kind

    async def test_an_unrecognized_kind_never_reaches_the_provider(self, context, runtime):
        """THE deny half of this fence. Reverting it fails exactly this test.

        `not consumed` is the load-bearing assertion: the request body was never even
        read, so nothing could have been forwarded upstream and no cost was incurred.
        """
        self._present(runtime, UNKNOWN_KIND)
        sent, consumed = await call(context, PROOF)
        assert sent[0]["status"] == 403
        assert json.loads(sent[1]["body"]) == {"error": "worker_identity_refused"}
        assert not consumed
        assert context._protected_run_binding is None

    async def test_an_authoring_run_is_metered_against_the_flow_it_will_amend(self, context, runtime):
        """THE meter half. Reverting the `flow_id` branch fails exactly this test.

        The ruling requires a replan author to work under existing applicable budget
        constraints. With `flow_id` dropped, the spend still happened — it simply could
        not be seen or capped at the flow that caused it, which is worse than a refusal
        because nothing signals it.

        `is_human_rooted` is asserted alongside because a human typed the replan
        request; an authoring run must not be attributed like a scheduled service run.
        """
        self._present(runtime, AUTHORITY_REPLAN_REQUEST)
        sent, _ = await call(context, PROOF)
        assert sent[0]["status"] == 200
        binding = context._protected_run_binding
        assert binding.flow_id == "flow"
        assert binding.is_human_rooted is True
        assert binding.tenant_id == "tenant"

    async def test_an_authoring_run_is_not_asked_for_a_worker_credential(self, context, runtime, monkeypatch):
        """It owns no graph node, so the assignment-level check does not apply to it.

        Asserted rather than left implicit because the alternative implementation —
        calling `authorize_worker_credential` for every engine kind — would deadlock the
        feature: fence 1 denies `replan_request` outright, so an authoring run would be
        refused every model call. The two fences have to agree about which one owns this
        kind, and this is where that agreement is checked.
        """
        from unittest.mock import AsyncMock

        from src.orchestration.execution_policy import Decision

        checked = AsyncMock(return_value=Decision.permit())
        monkeypatch.setattr("src.orchestration.runtime_policy.authorize_worker_credential", checked)
        self._present(runtime, AUTHORITY_REPLAN_REQUEST)
        sent, _ = await call(context, PROOF)
        assert sent[0]["status"] == 200
        assert checked.await_count == 0

    async def test_service_policy_remains_the_only_unattributed_non_human_kind(self, context, runtime):
        """Regression: scheduled coordination keeps exactly its current binding.

        Both halves matter. `is_human_rooted is False` is the pre-existing contract for
        a service run, and `flow_id is None` is the pre-existing contract this change
        deliberately did **not** extend to it — widening the metering branch to every
        non-gate kind would have been the easy over-fix.
        """
        self._present(runtime, AUTHORITY_SERVICE_POLICY)
        sent, _ = await call(context, PROOF)
        assert sent[0]["status"] == 200
        binding = context._protected_run_binding
        assert binding.is_human_rooted is False
        assert binding.flow_id is None


# ---------------------------------------------------------------------------------
# Fence 4 — work admission
# ---------------------------------------------------------------------------------


class TestWorkAdmissionFence:
    """`work_admission.admit_pending`, driven against real dispatch and claim rows.

    A work claim is an exclusivity lease on an issue. The fail-open arm filed every
    unrecognized kind under `DIRECT_DISPATCH`, the owner nothing can reconcile against
    the graph.

    The child is dispatched with claims disabled and admitted afterwards with them
    enabled, because fence 2 refuses an unrecognized kind before `_publish` is ever
    reached. The two fences are independent, so this one has to be exercised on its own
    rather than through a dispatch that cannot get that far — which is also the check
    that neither fence is silently carrying the other.
    """

    @pytest.fixture
    def pending_child(self, monkeypatch, store, child_dispatch, session_factory):
        monkeypatch.setenv("AGENT_AUTHORITY_ENABLED", "true")
        monkeypatch.setenv("ADP_WORK_CLAIMS_ENABLED", "false")
        monkeypatch.setattr("src.shared.database.get_session_factory", lambda: session_factory)
        store.client.update_item(
            TableName=store.table,
            Key={"pk": {"S": "TENANT#tenant"}, "sk": {"S": f"EXEC#{child_dispatch.invocation}"}},
            UpdateExpression="SET provider_repository_id = :repo",
            ExpressionAttributeValues={":repo": {"N": "1234"}},
        )
        child = human.send_child(child_dispatch)["invocation_id"]
        # The dispatch path copies the parent's immutable repository identity onto the
        # child only when claims are enabled, and they were not for the publish above.
        # Written here so admission reads the same committed value it would in
        # production rather than resolving the repository live — an unstubbed provider
        # lookup would make every case below fail for a reason that is not the fence.
        store.client.update_item(
            TableName=store.table,
            Key={"pk": {"S": "TENANT#tenant"}, "sk": {"S": f"EXEC#{child}"}},
            UpdateExpression="SET provider_repository_id = :repo",
            ExpressionAttributeValues={":repo": {"N": "1234"}},
        )
        monkeypatch.setenv("ADP_WORK_CLAIMS_ENABLED", "true")
        return child

    async def test_a_recognized_kind_still_claims_its_issue(self, store, pending_child, claims_session):
        """Regression control: the same call, one grant attribute different below."""
        from src.orchestration.work_admission import admit_pending

        receipt = await admit_pending(store, pending_child, session=claims_session)
        assert receipt["invocation_id"] == pending_child
        assert receipt["claim_id"]

    @pytest.mark.parametrize("kind", [AUTHORITY_REPLAN_REQUEST, UNKNOWN_KIND])
    async def test_unrecognized_kind_cannot_take_an_exclusivity_lease(self, store, pending_child, claims_session, kind):
        """THE fence. Reverting the deny in `work_admission` fails exactly this test.

        For `replan_request` the refusal is behavioural, not bookkeeping: the flow's
        intent issue is where the flow's real work runs, and an authoring run holding
        that lease would block or displace the very plan it was asked to propose a
        change to.

        The typed code is asserted because callers branch on it — `dispatch._publish`
        renders it into a 409 — so a generic failure here would surface as an
        unattributable refusal.
        """
        from src.orchestration.work_admission import admit_pending

        _store_kind(store, pending_child, kind)
        with pytest.raises(WorkClaimError) as refusal:
            await admit_pending(store, pending_child, session=claims_session)
        assert refusal.value.code == "authority_kind_not_recognized"

    async def test_a_refused_kind_leaves_the_issue_unclaimed(self, store, pending_child, claims_session):
        """No row is written, so a legitimate owner can still claim the issue.

        The consequential half of the refusal. A fence that denied but still inserted
        the claim would deadlock the flow's own work against a lease nobody holds — the
        exact reconciliation failure the `owner_kind` mapping exists to prevent.
        """
        from sqlalchemy import select

        from src.orchestration.models import OrchestrationWorkClaim
        from src.orchestration.work_admission import admit_pending

        _store_kind(store, pending_child, AUTHORITY_REPLAN_REQUEST)
        with pytest.raises(WorkClaimError):
            await admit_pending(store, pending_child, session=claims_session)
        assert (await claims_session.scalars(select(OrchestrationWorkClaim))).all() == []

    @pytest.mark.parametrize("kind", [AUTHORITY_REPLAN_REQUEST, UNKNOWN_KIND])
    async def test_a_refused_kind_cannot_claim_by_deferring(self, store, pending_child, claims_session, kind):
        """`allow_defer` is not a way around the kind fence.

        The deferred path exists so an authorized child of an active run can *wait* for
        its parent's lease instead of being refused. An unrecognized kind must not reach
        it: waiting for a lease is a weaker form of holding one, and a queued waiter is
        still a claim on the issue's future.
        """
        from src.orchestration.work_admission import admit_pending

        _store_kind(store, pending_child, kind)
        with pytest.raises(WorkClaimError) as refusal:
            await admit_pending(store, pending_child, session=claims_session, allow_defer=True)
        assert refusal.value.code == "authority_kind_not_recognized"

    def test_each_claiming_kind_maps_to_its_reconcilable_owner(self):
        """Regression: the two owner kinds keep exactly their current membership.

        The behavioural tests above cover admission and refusal; this pins the
        *mapping*, because silently moving `github_event` from `DIRECT_DISPATCH` to
        `ENGINE_FLOW` would keep every test above green while making stuck claims
        irreconcilable.
        """
        from src.orchestration.work_admission import _CLAIM_OWNER_KINDS
        from src.orchestration.work_claims import OwnerKind

        assert _CLAIM_OWNER_KINDS == {
            AUTHORITY_GATE_DECISION: OwnerKind.ENGINE_FLOW,
            AUTHORITY_GITHUB_EVENT: OwnerKind.DIRECT_DISPATCH,
            AUTHORITY_SERVICE_POLICY: OwnerKind.DIRECT_DISPATCH,
        }


# ---------------------------------------------------------------------------------
# Cross-cutting: nothing silently re-opens by string comparison
# ---------------------------------------------------------------------------------


class TestNoBareStringComparisons:
    """The regression that would quietly undo all of this.

    Each fence is correct today. What makes it *stay* correct is that the four modules
    reference the shared constants rather than re-spelling `"gate_decision"` inline — a
    future edit that adds a bare-string branch is how a fence gets re-opened without
    anyone noticing, because a string literal is invisible to the vocabulary test at
    the top of this file.
    """

    @pytest.mark.parametrize(
        "module_path",
        [
            "src.orchestration.runtime_policy",
            "src.agentauth.dispatch",
            "src.agentauth.model_identity",
            "src.orchestration.work_admission",
        ],
    )
    def test_converted_surfaces_compare_against_the_shared_constants(self, module_path):
        import ast
        import importlib
        import inspect

        module = importlib.import_module(module_path)
        tree = ast.parse(inspect.getsource(module))

        # An AST walk, not a regex. These modules legitimately quote the old
        # `"gate_decision"` pattern in their explanatory comments and docstrings, and a
        # textual scan cannot tell that from a live comparison — it would either report
        # findings that do not exist or be relaxed until it reported nothing.
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            for operand in (node.left, *node.comparators):
                if isinstance(operand, ast.Constant) and operand.value in RECOGNIZED_AUTHORITY_KINDS:
                    offenders.append((module_path, operand.lineno, operand.value))

        assert offenders == [], f"authority kind compared as a bare string literal: {offenders}"
