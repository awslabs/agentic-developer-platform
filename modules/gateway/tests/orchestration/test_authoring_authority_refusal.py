"""An authoring run re-proves its assignment on every call, or is refused (#4529).

`validate_authoring_authority` is the only thing standing between a `replan_request`
grant and the model credentials it can spend. It had **no tests at all** — the fences in
`test_authority_kind_fences.py` prove other surfaces *refuse* this kind, and
`test_replan_authoring_dispatch.py` proves the assignment is built and published
correctly, but nothing covered what happens when the run comes back and presents itself.

That gap matters because of what the grant is: minted when the human's `replan:` comment
was handled, valid for seven days, carrying `MONITOR` on `SELF` and a `flow_id`. It is
not narrow enough to be safe on its own. Everything that bounds an authoring run to *one
assignment at one base revision* is re-derived from live state on every call, in this one
function. So each of those derivations needs a test that fails when it is removed.

## Driven through the production entry point

Every case here goes through `AgentRuntime.validate_flow` — the method
`model_identity`'s middleware calls before a request reaches a provider — on a genuinely
provisioned protected execution:

    a real `replan:` comment  ->  `_apply_command`        (the real replan branch)
                              ->  `publish_authoring`      (the real post-commit publish)
                              ->  `provision_authoring`    (the real authority write)
                              ->  `store.bind`             (the real pod binding)
                              ->  `store.live_grant`       (the real grant re-read)
                              ->  `runtime.validate_flow`  (the surface under test)

Nothing is hand-assembled. That is not thoroughness for its own sake: the authority kind
is recorded in the `AUTHORITY#` row *and* in every grant derived from it and cross-checked
between them, and the request id is read from the execution record rather than from the
caller. A hand-written grant or execution dict would be refused by a consistency check
somewhere upstream of the fence under test — and from the outside that is
indistinguishable from the fence working, because the call raises either way. The tests
would be green and would prove nothing.

## One refusal message, deliberately

Every negative case below asserts the *same* string. That is the contract, not laziness:
`validate_authoring_authority`'s docstring requires that a caller cannot tell "no such
request" from "not your request" from "your base moved", because a distinguishing refusal
lets a run map other tenants' flow structure by probing. `test_every_refusal_is_the_same_
sentence` pins it as a property so a future edit that adds a helpful detail fails here.

The one exception is the outer `except`, which says "authoring authority unavailable" —
a different sentence because it means "we could not decide", not "we decided no". Also
asserted, because collapsing the two would hide a broken database behind a denial.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import boto3
import pytest
from moto import mock_aws
from sqlalchemy import update

from src.agentauth.bootstrap import BootstrapRefusedError, BootstrapStore, envelope_digest
from src.agentauth.engine import AUTHORING_PERSONA, AUTHORING_REQUEST_ATTRIBUTE, EngineAuthorityWriter
from src.agentauth.grants import (
    AUTHORITY_GATE_DECISION,
    AUTHORITY_GITHUB_EVENT,
    AUTHORITY_REPLAN_REQUEST,
    AUTHORITY_SERVICE_POLICY,
    RECOGNIZED_AUTHORITY_KINDS,
    AgentAction,
    TargetRelationship,
)
from src.agentauth.routes import AgentRuntime
from src.agentauth.workload import VerifiedPod
from src.orchestration.authoring_dispatch import publish_authoring
from src.orchestration.engine_commands import EngineCommandReport
from src.orchestration.models import (
    AmendmentRequestState,
    DecisionKind,
    OrchestrationAcceptedPlan,
    OrchestrationAmendmentRequest,
    OrchestrationFlow,
)

from .test_pending_amendments import ORG_A, accepted_flow, base_proposal
from .test_replan_authoring_dispatch import ASKER, FakeSQS, asker, flow_with_asker, replan
from .test_replan_authoring_dispatch import session as _session_fixture

#: Rebound rather than imported under its own name, for the reason given in
#: `test_authoring_recovery.py`: every test taking `session` as a parameter would
#: otherwise read to ruff as redefining an import (F811), and blanketing the signatures
#: with `noqa` would suppress that check where it is real.
session = _session_fixture

pytestmark = pytest.mark.asyncio

#: The refusal. One sentence for every way the assignment can fail to hold, so a caller
#: cannot distinguish them. Spelled once here and asserted everywhere.
REFUSED = "authoring assignment is no longer authorized"

#: The *other* refusal: the state needed to decide was unavailable. A different sentence
#: because it is a different fact, and conflating them would hide an outage as a denial.
UNAVAILABLE = "authoring authority unavailable"

#: A second tenant, with its own human. The user id is per-tenant because `users.id` is
#: the primary key: one shared id cannot name a person in two organizations, and the
#: resolver filters on `org_id` in SQL, so reusing it would leave tenant B's replan
#: unattributable rather than cross-attributed.
ORG_B = "org-beta"


def _pod(label: str) -> VerifiedPod:
    """A distinct pod per authoring run.

    Not cosmetic. `BootstrapStore.bind` writes a `POD#<uid>` binding row and refuses a
    second invocation presenting a uid already bound to another one — a pod runs one
    agent invocation, and re-binding is how a finished run's credentials would be
    handed to a new one. So the tests that need *two* live runs (to steal one's
    assignment, or to reach across tenants) need two pods, exactly as production would.
    """
    return VerifiedPod(f"author-pod-{label}", f"author-worker-{label}", "adp-agents", "agent-scaledjob-sa", "10.0.1.2")


@pytest.fixture
def protected(monkeypatch):
    """The real authority store and writer over moto, with protected mode ON.

    `AGENT_AUTHORITY_ENABLED=true` is what makes `publish_authoring` call
    `provision_authoring` — without it the publish path skips authority provisioning
    entirely and there would be no grant to present. Same fixture shape as
    `test_dispatch_pass.protected_engine`, deliberately: one description of what a
    protected execution looks like.
    """
    with mock_aws():
        ddb = boto3.client("dynamodb", region_name="us-east-1")
        for table, pk, sk in [("authority", "pk", "sk"), ("events", "event_id", "arrived_at")]:
            ddb.create_table(
                TableName=table,
                BillingMode="PAY_PER_REQUEST",
                KeySchema=[{"AttributeName": pk, "KeyType": "HASH"}, {"AttributeName": sk, "KeyType": "RANGE"}],
                AttributeDefinitions=[{"AttributeName": pk, "AttributeType": "S"}, {"AttributeName": sk, "AttributeType": "S"}],
            )
        store = BootstrapStore(table_name="authority", dynamodb_client=ddb)
        writer = EngineAuthorityWriter(store=store, events_table="events")
        monkeypatch.setenv("AGENT_AUTHORITY_ENABLED", "true")
        monkeypatch.setattr("src.agentauth.engine.get_engine_authority_writer", lambda: writer)
        yield store, writer


async def authoring_run(session, protected, monkeypatch, *, org_id: str = ORG_A, flow_id: str | None = None, label: str = "primary"):
    """One real, live, protected authoring run, ready to present itself.

    The whole chain, in the order production runs it. Returns everything a test needs to
    interrogate or tamper with live state: the `AgentRuntime` whose `validate_flow` is
    the surface under test, the bound execution record, the re-read grant, the published
    envelope, and the ids.

    The `session.rollback()` before publishing is not incidental — `publish_authoring` is
    genuinely post-commit code and opens its own session, which here shares one in-memory
    SQLite connection with this one. See `test_replan_authoring_dispatch.flush`.
    """
    store, _ = protected
    user_id = ASKER if org_id == ORG_A else f"cognito-sub-asker-{org_id}"
    if flow_id is None:
        flow_id = await flow_with_asker(session, org_id=org_id, user_id=user_id)
    else:
        # A caller-supplied flow still needs its tenant's human seeded: `accepted_flow`
        # compiles a plan, it does not create people, and the assignment cannot be
        # attributed without one.
        await asker(session, org_id=org_id, user_id=user_id)
        await session.commit()
    report = EngineCommandReport()
    applied, _ = await replan(session, report, flow_id=flow_id, org_id=org_id, user_id=user_id)
    assert applied is True
    assert len(report.pending_authoring) == 1, "the fixture must produce a real assignment, not the unqueued path"
    pending = report.pending_authoring[0]

    await session.rollback()
    factory = session.info["factory"]
    sqs = FakeSQS()
    published = await publish_authoring(pending, session_factory=factory, client=sqs, queue_url="https://sqs.test/authoring.fifo")
    assert published is True, "the fixture must publish, or there is no authority row to present"
    envelope = sqs.envelope()

    record = store.bind(invocation_id=envelope["message_id"], digest=envelope_digest(envelope), pod=_pod(label), now=datetime.now(UTC))
    grant = store.live_grant(invocation_id=record.invocation_id, tenant_id=org_id, attempt=1, now=datetime.now(UTC))
    monkeypatch.setattr("src.shared.database.get_session_factory", lambda: factory)

    return SimpleNamespace(
        runtime=AgentRuntime(store=store, workloads=None),
        store=store,
        record=record,
        grant=grant,
        envelope=envelope,
        org_id=org_id,
        flow_id=flow_id,
        request_id=pending.request_id,
        run_id=pending.author_run_id,
        user_id=user_id,
        factory=factory,
    )


async def validate(work, session):
    """Call the production entry point, with the outer session's transaction released.

    The `rollback` is an artefact of the harness, not of the code: `validate_flow` opens
    its own session, which here shares one in-memory SQLite connection with the test's.
    A test still holding a read transaction — which `session.get` is enough to start —
    makes the inner `BEGIN` fail with "cannot start a transaction within a transaction",
    and that surfaces as `authoring authority unavailable`. Which would be an
    *unavailability* refusal masquerading as a fence, exactly the false green this file's
    docstring warns about, so it is released deliberately rather than left to luck.
    """
    await session.rollback()
    return await work.runtime.validate_flow(work.record, work.grant)


async def amend_request(session, request_id: str, **values) -> None:
    """Move live request state, then commit. How every "it moved" case is driven.

    An UPDATE rather than a rewritten grant, because the property under test is that the
    function reads the *database* now instead of trusting what the grant said when it was
    minted. Tampering with the grant would test a forged credential — a real concern, but
    a different one, and one `live_grant` already refuses upstream.
    """
    await session.execute(update(OrchestrationAmendmentRequest).where(OrchestrationAmendmentRequest.id == request_id).values(**values))
    await session.commit()


def repoint_execution(work, request_id: str) -> None:
    """Rewrite the server-written request attribute on the live execution record.

    This is the *forged assignment* probe, and it has to be written to DynamoDB rather
    than passed as an argument because `validate_flow` re-reads the execution from the
    store itself. So this is exactly as much power as an attacker who could write the
    execution row would have — strictly more than a worker actually has, which is what
    makes a refusal here meaningful.
    """
    work.store.client.update_item(
        TableName=work.store.table,
        Key={"pk": {"S": f"TENANT#{work.org_id}"}, "sk": {"S": f"EXEC#{work.record.invocation_id}"}},
        UpdateExpression=f"SET {AUTHORING_REQUEST_ATTRIBUTE} = :value",
        ExpressionAttributeValues={":value": {"S": request_id}},
    )


def strip_execution_attribute(work) -> None:
    """Remove the request attribute entirely: the missing-assignment case."""
    work.store.client.update_item(
        TableName=work.store.table,
        Key={"pk": {"S": f"TENANT#{work.org_id}"}, "sk": {"S": f"EXEC#{work.record.invocation_id}"}},
        UpdateExpression=f"REMOVE {AUTHORING_REQUEST_ATTRIBUTE}",
    )


# ---------------------------------------------------------------------------------
# The assignment holds
# ---------------------------------------------------------------------------------


class TestALiveAssignmentIsAdmitted:
    """The positive control, without which every refusal below could be vacuous.

    If the fixture produced something `validate_flow` refuses for an unrelated reason,
    every negative test would pass while proving nothing about the fence it names. This
    class is what rules that out.
    """

    async def test_a_live_authoring_run_is_admitted_and_attributed_to_no_node(self, session, protected, monkeypatch):
        """Admitted, and returns None rather than a graph attribution.

        The None is an answer, not a gap. An authoring run reads a request and files a
        proposal; it executes no graph node, so there is nothing to charge its model
        spend to, and inventing an attribution would report another node's cost with the
        authority of a measurement. `test_authority_kind_fences` proves the middleware
        keeps it None; this proves the validator produces it for a genuinely live run.
        """
        work = await authoring_run(session, protected, monkeypatch)

        assert await validate(work, session) is None

    async def test_admission_is_repeatable_because_it_derives_nothing(self, session, protected, monkeypatch):
        """Called twice, admitted twice: validation consumes no state.

        Worth pinning because the alternative design — marking the request "validated",
        or advancing it out of `QUEUED` here — would make an authoring run's *second*
        model call fail. The run makes many. A one-shot validator would look correct in
        a single-call test and break the feature in production.
        """
        work = await authoring_run(session, protected, monkeypatch)

        assert await validate(work, session) is None
        assert await validate(work, session) is None

        row = await session.get(OrchestrationAmendmentRequest, work.request_id)
        assert row.state == AmendmentRequestState.DISPATCHED.value, "published, and validation did not move it"

    async def test_a_dispatched_request_is_still_authorized(self, session, protected, monkeypatch):
        """`DISPATCHED` is the normal state at call time, not a terminal one.

        The request is marked `DISPATCHED` by the publish that created this run, so if
        the accepted-state set excluded it, no authoring run could ever make a single
        model call. This is the test that catches tightening the set to `{QUEUED}`.
        """
        work = await authoring_run(session, protected, monkeypatch)
        row = await session.get(OrchestrationAmendmentRequest, work.request_id)
        assert row.state == AmendmentRequestState.DISPATCHED.value

        assert await validate(work, session) is None

    async def test_a_state_outside_the_authorized_set_is_refused(self, session, protected, monkeypatch):
        """The state check is an allowlist, so a state it has never heard of denies.

        Today `AmendmentRequestState` has exactly two members and both are authorized,
        which makes the `not in {...}` unobservable — every reachable state passes it.
        That is precisely why it needs pinning now: `state` is a plain `String(16)` with
        no CHECK constraint and the enum's own docstring anticipates growth ("a future
        member needs no DDL"). The day someone adds `ANSWERED` or `ABANDONED`, the
        question is whether a run holding a seven-day grant may still spend against a
        request that has been closed — and the answer has to be no, by default, without
        anyone remembering to come back here.

        Written as an allowlist rather than a denylist for that reason, and asserted with
        a value no code writes so the test states the *shape* of the rule rather than
        guessing which state gets added.
        """
        work = await authoring_run(session, protected, monkeypatch)
        await amend_request(session, work.request_id, state="answered")

        with pytest.raises(BootstrapRefusedError, match=REFUSED):
            await validate(work, session)

    async def test_a_first_plan_author_with_no_base_revision_is_not_refused(self, session, protected, monkeypatch):
        """Both-NULL compares equal, so a flow with no accepted plan is authorable.

        The base-revision check is an equality against the plan in force, and a flow
        that has never been accepted has none. Comparing "no base" as a mismatch would
        refuse exactly the run that is supposed to produce the first plan — a fence that
        blocks the feature's opening move.
        """
        work = await authoring_run(session, protected, monkeypatch)
        # Drop the recorded base AND the plan in force together, which is the shape a
        # never-accepted flow really has. Dropping only one would be the mismatch case.
        await amend_request(session, work.request_id, base_plan_version=None, base_plan_hash=None)
        await session.execute(
            update(OrchestrationAcceptedPlan).where(OrchestrationAcceptedPlan.flow_id == work.flow_id).values(superseded_at=datetime.now(UTC))
        )
        await session.commit()

        assert await validate(work, session) is None


# ---------------------------------------------------------------------------------
# The assignment does not hold
# ---------------------------------------------------------------------------------


class TestTheAssignmentMustStillBeThisRuns:
    """Missing, forged, and stolen: the three ways the *identity* can fail."""

    async def test_an_execution_with_no_assignment_is_refused(self, session, protected, monkeypatch):
        """The missing-request case, and it must deny rather than skip.

        A `replan_request` grant whose execution names no assignment is an authority with
        *unbounded* scope: every per-assignment check below is keyed off the request, so
        "no request" means "nothing to check". Passing it through would be the fail-open
        shape #4529 exists to remove — and it is the shape a permissive `if request_id:`
        would produce.
        """
        work = await authoring_run(session, protected, monkeypatch)
        strip_execution_attribute(work)

        with pytest.raises(BootstrapRefusedError, match=REFUSED):
            await validate(work, session)

    async def test_an_assignment_that_does_not_exist_is_refused(self, session, protected, monkeypatch):
        """A forged request id resolves to nothing, and nothing is a refusal."""
        work = await authoring_run(session, protected, monkeypatch)
        repoint_execution(work, "00000000-0000-0000-0000-000000000000")

        with pytest.raises(BootstrapRefusedError, match=REFUSED):
            await validate(work, session)

    async def test_another_runs_assignment_cannot_be_answered(self, session, protected, monkeypatch):
        """THE stolen-assignment case. A request id is not a secret; the binding is.

        Two real authoring runs in one tenant, and the first is re-pointed at the
        second's assignment. Both requests exist, both are live, both are this tenant's
        and this human's — the *only* thing wrong is that the server wrote a different
        `author_run_id` onto that request. Which is the whole point: the id travels in
        envelopes and logs, so what makes it useless to another run is that the server
        recorded who may answer it.
        """
        work = await authoring_run(session, protected, monkeypatch)
        other_flow = await accepted_flow(session, org_id=ORG_A, proposal=base_proposal(org_id=ORG_A, flow="second-flow", intent_ref="4530"))
        await session.commit()
        theirs = await authoring_run(session, protected, monkeypatch, flow_id=other_flow, label="thief-victim")
        assert theirs.request_id != work.request_id
        assert theirs.run_id != work.run_id
        repoint_execution(work, theirs.request_id)

        with pytest.raises(BootstrapRefusedError, match=REFUSED):
            await validate(work, session)

        # And the theft did not damage the victim: its own run still validates.
        assert await validate(theirs, session) is None

    async def test_a_reassigned_author_run_refuses_the_original(self, session, protected, monkeypatch):
        """The run binding is re-read, not remembered from when the grant was minted.

        `author_run_id` moving is the narrowest possible statement of "this is no longer
        your assignment", isolated from flow, tenant and revision. A validator that
        trusted the grant's principal — which still looks perfectly valid — would admit
        this.
        """
        work = await authoring_run(session, protected, monkeypatch)
        await amend_request(session, work.request_id, author_run_id="replan:somebody-else")

        with pytest.raises(BootstrapRefusedError, match=REFUSED):
            await validate(work, session)


class TestTheAssignmentMustStillBeThisFlowAndTenant:
    async def test_another_tenants_assignment_is_indistinguishable_from_an_absent_one(self, session, protected, monkeypatch):
        """Cross-tenant, with the tenant filter *inside* the query.

        A genuinely separate tenant, with its own org, its own human and its own flow —
        then tenant A's execution is pointed at tenant B's request id. The filter being
        in the WHERE clause rather than an `if` after the read is what makes this refusal
        carry no information: A cannot tell that B's request exists.

        Asserted by the refusal being the SAME sentence as the not-found case above, not
        merely by there being a refusal.
        """
        work = await authoring_run(session, protected, monkeypatch)
        theirs = await authoring_run(session, protected, monkeypatch, org_id=ORG_B, label="other-tenant")
        assert theirs.org_id != work.org_id
        assert theirs.user_id != work.user_id, "each tenant's request must be asked by its own human, or this is not a cross-tenant case"
        repoint_execution(work, theirs.request_id)

        with pytest.raises(BootstrapRefusedError, match=REFUSED):
            await validate(work, session)

        # B's request really does exist and really is live — so the refusal above was
        # the tenant fence, not an artefact of a broken fixture.
        assert await validate(theirs, session) is None

    async def test_only_the_tenant_differs_and_that_alone_is_enough(self, session, protected, monkeypatch):
        """The tenant filter in isolation, with every other check arranged to pass.

        The cross-tenant test above refuses even without the `org_id` filter, because
        two tenants' requests also name two different flows — so it passes while the
        filter does nothing, and mutation-testing found exactly that. This one moves
        *this* request to another tenant and changes nothing else: flow, run binding,
        root decision, human, state and base revision all still match the grant, and
        the flow itself is still this tenant's. Drop `org_id` from the WHERE clause and
        the run is **admitted**.

        So this is the test that makes the filter load-bearing, and it is worth
        isolating because the filter is what carries the no-information property: a
        check applied after the read can leak through timing or through a distinguishing
        refusal, and one applied in SQL cannot.
        """
        work = await authoring_run(session, protected, monkeypatch)
        await amend_request(session, work.request_id, org_id=ORG_B)

        row = await session.get(OrchestrationAmendmentRequest, work.request_id)
        await session.refresh(row)
        assert row.org_id == ORG_B
        assert row.flow_id == work.flow_id, "only the tenant may differ, or this no longer isolates the filter"
        assert row.author_run_id == work.run_id
        assert row.state in {AmendmentRequestState.QUEUED.value, AmendmentRequestState.DISPATCHED.value}

        with pytest.raises(BootstrapRefusedError, match=REFUSED):
            await validate(work, session)

    async def test_an_assignment_that_names_another_flow_is_refused(self, session, protected, monkeypatch):
        """Cross-flow. A grant naming one flow and a request naming another is refused.

        Not reconciled, and the direction matters: the grant's `flow_id` is what
        `model_identity` meters spend against and what `register_amendment_draft` files
        output into. If those two could disagree, an authoring run would be budgeted
        against one flow and amend a different one.
        """
        work = await authoring_run(session, protected, monkeypatch)
        other_flow = await accepted_flow(session, org_id=ORG_A, proposal=base_proposal(org_id=ORG_A, flow="third-flow", intent_ref="4531"))
        await amend_request(session, work.request_id, flow_id=other_flow)

        with pytest.raises(BootstrapRefusedError, match=REFUSED):
            await validate(work, session)

    async def test_a_deleted_flow_refuses_the_run_it_had_authorized(self, session, protected, monkeypatch):
        """The flow is re-read too, so a grant can outlive what it was scoped to.

        Deliberately separate from the request checks: everything about the assignment
        still holds, and the thing that vanished is what it was an assignment *about*.
        Authoring against a flow that no longer exists could only produce a draft
        nothing can accept.
        """
        work = await authoring_run(session, protected, monkeypatch)
        await session.execute(update(OrchestrationFlow).where(OrchestrationFlow.id == work.flow_id).values(org_id="org-somewhere-else"))
        await session.commit()

        with pytest.raises(BootstrapRefusedError, match=REFUSED):
            await validate(work, session)


class TestTheAssignmentMustStillBeAtItsBaseRevision:
    """The stale-request cases. Refused *before* the run can spend anything."""

    @pytest.mark.parametrize(
        "moved",
        [
            pytest.param({"base_plan_version": 99}, id="version-moved"),
            pytest.param({"base_plan_hash": "0" * 64}, id="hash-moved"),
            pytest.param({"base_plan_version": 99, "base_plan_hash": "0" * 64}, id="both-moved"),
        ],
    )
    async def test_a_run_whose_base_revision_moved_is_refused(self, session, protected, monkeypatch, moved):
        """Version and hash are checked independently, and each one alone refuses.

        Both, because they answer different questions: the version catches an
        acceptance that advanced the plan, and the hash catches a document that changed
        without the version moving — which is the one a version-only check would miss.
        """
        work = await authoring_run(session, protected, monkeypatch)
        await amend_request(session, work.request_id, **moved)

        with pytest.raises(BootstrapRefusedError, match=REFUSED):
            await validate(work, session)

    async def test_an_amended_plan_refuses_the_author_that_was_asked_against_the_old_one(self, session, protected, monkeypatch):
        """The same refusal, driven from the *plan* side rather than the request side.

        Someone else accepted an amendment while this author was working. The request row
        is untouched — it still records the base the human asked against — and what moved
        is the plan in force. This is the realistic shape of the stale case, and the
        reason the check is here rather than left to `accept_amendment`: refusing at
        acceptance means the run has already spent a full authoring job's model budget
        producing a proposal that will be rejected as a conflict.
        """
        work = await authoring_run(session, protected, monkeypatch)
        await session.execute(
            update(OrchestrationAcceptedPlan)
            .where(OrchestrationAcceptedPlan.flow_id == work.flow_id, OrchestrationAcceptedPlan.superseded_at.is_(None))
            .values(version=7, plan_hash="f" * 64)
        )
        await session.commit()

        with pytest.raises(BootstrapRefusedError, match=REFUSED):
            await validate(work, session)


class TestTheAssignmentMustStillBeThisHumansAsk:
    async def test_an_assignment_attributed_to_another_human_is_refused(self, session, protected, monkeypatch):
        """The authority names the human who asked; the request must agree.

        This is what stops a live authoring grant being re-used for work a *different*
        person requested. The authority record is what carries revocation — disable the
        human and their authority goes inactive — so a run whose request now names
        someone else would be operating under an authority that cannot be revoked by the
        person actually being served.
        """
        work = await authoring_run(session, protected, monkeypatch)
        await amend_request(session, work.request_id, requested_by="cognito-sub-somebody-else")

        with pytest.raises(BootstrapRefusedError, match=REFUSED):
            await validate(work, session)

    async def test_an_assignment_rooted_in_another_decision_is_refused(self, session, protected, monkeypatch):
        """The root decision is re-checked, so the authority and the request agree.

        `replan_decision_id` is the committed `REPLAN_REQUESTED` row the grant's
        authority reference points at — the durable evidence a human asked. A request
        that has been re-rooted at a different decision is no longer the one this
        authority was minted for, even though every other field still matches.
        """
        work = await authoring_run(session, protected, monkeypatch)
        await amend_request(session, work.request_id, replan_decision_id="decision-somewhere-else")

        with pytest.raises(BootstrapRefusedError, match=REFUSED):
            await validate(work, session)


class TestTheRefusalTellsTheCallerNothing:
    async def test_every_refusal_is_the_same_sentence(self, session, protected, monkeypatch):
        """A property, not a per-case assertion: all seven denials are indistinguishable.

        Collected and compared as a set, because the hazard is *drift* — a future edit
        that adds "(base revision moved)" to one message would be a helpful-looking
        change that lets a run probe another tenant's flow structure by reading which
        refusal it gets. Asserting each case's message separately would not catch two
        messages diverging from each other.
        """
        cases = {
            "no-assignment": None,
            "forged-id": None,
            "wrong-run": {"author_run_id": "replan:not-me"},
            "wrong-flow": {"flow_id": "flow-somewhere-else"},
            "wrong-human": {"requested_by": "somebody-else"},
            "wrong-decision": {"replan_decision_id": "decision-else"},
            "moved-base": {"base_plan_version": 99},
        }
        messages = {}
        for name, mutation in cases.items():
            work = await authoring_run(session, protected, monkeypatch, flow_id=await _fresh_flow(session, name), label=name)
            if name == "no-assignment":
                strip_execution_attribute(work)
            elif name == "forged-id":
                repoint_execution(work, "11111111-1111-1111-1111-111111111111")
            else:
                await amend_request(session, work.request_id, **mutation)
            with pytest.raises(BootstrapRefusedError) as refusal:
                await validate(work, session)
            messages[name] = str(refusal.value)

        assert len(messages) == len(cases), "every case must have produced a refusal"
        assert set(messages.values()) == {REFUSED}, f"refusals are distinguishable: {messages}"

    async def test_an_unavailable_database_is_not_reported_as_a_denial(self, session, protected, monkeypatch):
        """ "We could not decide" must not read as "we decided no".

        The outer `except` exists so a broken read cannot fail *open*, and it uses a
        different sentence so an operator can tell an outage from a refusal. Both halves
        are asserted: it still raises, and it says something else.

        The unavailability sentence is asserted *exactly*, not as "either of the two
        unavailable messages". Accepting `validate_flow`'s own relabelling here would
        have hidden the defect this issue fixed: that wrapper used to convert every
        deliberate refusal into "engine authority unavailable", so a loose assertion
        would pass whether the distinction survives the call or not — which is the only
        thing this test exists to check.
        """
        work = await authoring_run(session, protected, monkeypatch)
        monkeypatch.setattr(
            "src.orchestration.pending_amendments.in_force_plan",
            _raising("database unavailable"),
        )

        with pytest.raises(BootstrapRefusedError) as refusal:
            await validate(work, session)
        assert str(refusal.value) == UNAVAILABLE
        assert REFUSED not in str(refusal.value)

    async def test_a_refusal_is_not_relabelled_as_an_outage_on_its_way_out(self, session, protected, monkeypatch):
        """The refusal reason must survive `validate_flow`, or it is unreachable in prod.

        `validate_authoring_authority` ends in `except BootstrapRefusedError: raise`
        precisely so "we decided no" stays distinguishable from "we could not decide" —
        but that is undone if its caller re-wraps everything. It did: every refusal above
        reached the middleware as "engine authority unavailable", so an operator paged by
        a run presenting a stale assignment would have gone looking for a broken
        database, and the two have opposite responses.

        Asserted at the boundary the middleware actually calls, with both sentences named
        positively and negatively, because the bug was invisible from inside the
        validator — its own unit behaviour was already correct.
        """
        work = await authoring_run(session, protected, monkeypatch)
        await amend_request(session, work.request_id, author_run_id="replan:somebody-else")

        with pytest.raises(BootstrapRefusedError) as refusal:
            await validate(work, session)
        assert str(refusal.value) == REFUSED, "the specific refusal must reach the caller, not an outage label"
        assert "unavailable" not in str(refusal.value)


def _raising(message: str):
    async def _raise(*_args, **_kwargs):
        raise RuntimeError(message)

    return _raise


async def _fresh_flow(session, label: str) -> str:
    """A distinct flow per case, because `(org_id, slug)` is unique.

    `uq_orchestration_flows_org_slug` means two cases sharing a slug collide on insert,
    and the intent_ref differs too so each case's flow is genuinely its own.

    The intent_ref is derived from the label rather than `hash()`, which is salted per
    process and so would make a collision reproduce only on some runs.
    """
    slug = f"flow-{label}"
    intent_ref = str(4600 + sum(label.encode()) % 300)
    flow_id = await flow_with_asker(session, org_id=ORG_A, proposal=base_proposal(org_id=ORG_A, flow=slug, intent_ref=intent_ref))
    return flow_id


# ---------------------------------------------------------------------------------
# The authority is bounded, and stays bounded
# ---------------------------------------------------------------------------------


class TestTheAuthorityIsMonitorOnly:
    """Negative space: what an admitted authoring run still cannot do.

    Every test above is about *which* assignment a run may answer. These are about what
    the grant permits even when everything holds — and they are assertions about
    absence, which is the kind that rots silently. A future edit adding `DISPATCH` here
    to "let the author kick off validation" would break no test that only checks the
    happy path.
    """

    async def test_an_admitted_authoring_grant_holds_monitor_and_nothing_else(self, session, protected, monkeypatch):
        """Read from the live re-read grant, not from the mint site.

        `store.live_grant` rehydrates from DynamoDB, so this is what the middleware
        actually sees on every call — not what `provision_authoring` intended. The two
        could disagree through a serialisation bug, and the one that matters is this one.
        """
        work = await authoring_run(session, protected, monkeypatch)

        assert work.grant.allowed_actions == frozenset({AgentAction.MONITOR})
        assert AgentAction.DISPATCH not in work.grant.allowed_actions

    async def test_an_authoring_grant_can_delegate_nothing(self, session, protected, monkeypatch):
        """Nothing delegable and no descendants: with no dispatch there are none.

        A `DESCENDANT` relationship that can never resolve is authority waiting to be
        misread — some future surface treating a non-empty relationship set as "this may
        act on children" would find one here if it were populated.
        """
        work = await authoring_run(session, protected, monkeypatch)

        assert work.grant.delegable_actions == frozenset()
        assert work.grant.target_relationships == frozenset({TargetRelationship.SELF})
        assert TargetRelationship.DESCENDANT not in work.grant.target_relationships

    async def test_an_authoring_grant_is_capped_at_zero_dispatches(self, session, protected, monkeypatch):
        """The numeric caps agree with the action set, independently.

        Two mechanisms, so neither is load-bearing alone: the missing `DISPATCH` action
        and a concurrency cap of zero. A code path that checked only the cap, or only the
        action, is still refused.
        """
        work = await authoring_run(session, protected, monkeypatch)

        assert work.grant.max_dispatch_concurrency == 0
        assert work.grant.max_chain_depth == 0

    async def test_an_authoring_grant_is_scoped_to_one_flow_and_one_repo(self, session, protected, monkeypatch):
        """Scoped, and the scope is the assignment's — not the tenant's.

        `flow_id` being set is what `model_identity` meters against (see
        `test_authority_kind_fences.TestMeteringFence`), so its presence here is the
        other half of that contract: the metering branch has something to meter.
        """
        work = await authoring_run(session, protected, monkeypatch)

        assert work.grant.flow_id == work.flow_id
        assert work.grant.repo_scope == frozenset({work.envelope["source_ref"]["repo"]})
        assert work.grant.tenant_id == work.org_id

    async def test_the_authoring_persona_is_the_only_one_provisioned(self, session, protected, monkeypatch):
        """An execution persona holding a non-executing authority is a shape nothing should produce.

        `developer` under `replan_request` would be a run that expects to write code
        holding an authority that cannot dispatch — refused at provisioning rather than
        left to fail confusingly later.
        """
        work = await authoring_run(session, protected, monkeypatch)
        execution = work.store._read(f"TENANT#{work.org_id}", f"EXEC#{work.record.invocation_id}")

        assert execution["persona"] == {"S": AUTHORING_PERSONA}
        assert work.envelope["persona"] == AUTHORING_PERSONA

    async def test_a_replan_request_cannot_root_executing_graph_work(self):
        """`REPLAN_REQUESTED` is absent from the approval vocabulary, by construction.

        This is the structural reason the authority above can never widen: genesis
        resolves executing work from `APPROVAL_DECISION_KINDS`, and the decision an
        authoring authority is rooted in is not a member. So even a grant that somehow
        acquired `DISPATCH` would find no approval to dispatch under.
        """
        from src.orchestration.genesis import APPROVAL_DECISION_KINDS

        assert DecisionKind.REPLAN_REQUESTED.value not in APPROVAL_DECISION_KINDS

    async def test_the_envelope_carries_an_assignment_and_no_credential(self, session, protected, monkeypatch):
        """No node, no attempt, no graph address — and nothing secret.

        The absences are what stop an authoring run's spend being attributed to work it
        did not do. The credential assertion is here because this envelope is published
        to SQS: anything secret in it would be at rest in a queue.
        """
        work = await authoring_run(session, protected, monkeypatch)
        body = json.dumps(work.envelope)

        assert set(work.envelope["orchestration"]) == {"flow_id", "request_id", "root_decision_id", "base_plan_version", "base_plan_hash"}
        assert "node_id" not in body and "graph_address" not in body
        assert "credential" not in work.envelope and "credential" not in body


# ---------------------------------------------------------------------------------
# The sibling kinds still work
# ---------------------------------------------------------------------------------


class TestTheSiblingKindsAreUnaffected:
    """Regression at the entry point whose enumeration this issue converted.

    `routes.AgentRuntime.validate_flow` is where the four kinds are routed, and it is
    the surface where the bare-string comparisons lived. Breaking `github_event` or
    `service_policy` while fixing authoring would be worse than the original defect, so
    each kind's routing is asserted here rather than inferred from the constants being
    named correctly.
    """

    @staticmethod
    def _grant(kind: str):
        return SimpleNamespace(authority=SimpleNamespace(kind=kind), flow_id="flow")

    @pytest.mark.parametrize("kind", [AUTHORITY_GITHUB_EVENT, AUTHORITY_SERVICE_POLICY])
    async def test_a_non_engine_kind_needs_no_flow_validation(self, kind):
        """Returns None immediately, without reading SQL or DynamoDB.

        The `store` and `session` are deliberately absent from this runtime: if the
        early return were removed, the call would raise on the missing store rather than
        returning None, so this test fails for the right reason.
        """
        runtime = AgentRuntime(store=None, workloads=None)

        assert await runtime.validate_flow(record=None, grant=self._grant(kind)) is None

    async def test_an_unrecognized_kind_is_refused_rather_than_waved_through(self):
        """The fail-closed half. A kind nobody taught this surface about is denied.

        This is the enumeration's whole purpose: a fifth authority kind added to
        `grants.py` without visiting this function is refused here, rather than falling
        through whichever branch happens to be last.
        """
        runtime = AgentRuntime(store=None, workloads=None)

        with pytest.raises(BootstrapRefusedError, match="unsupported authority source"):
            await runtime.validate_flow(record=None, grant=self._grant("some_future_authority_kind"))

    async def test_the_enumeration_covers_the_whole_vocabulary(self):
        """No recognized kind falls into the unsupported branch.

        Derived from `RECOGNIZED_AUTHORITY_KINDS` rather than listing four names, so a
        kind added to the vocabulary without a decision here fails this test. It does not
        assert *what* each kind does — the tests above do that — only that every one of
        them was considered.
        """
        runtime = AgentRuntime(store=None, workloads=None)
        unsupported = []
        for kind in sorted(RECOGNIZED_AUTHORITY_KINDS):
            try:
                await runtime.validate_flow(record=None, grant=self._grant(kind))
            except BootstrapRefusedError as refusal:
                if "unsupported authority source" in str(refusal):
                    unsupported.append(kind)
            except Exception:
                # Any other failure means the kind WAS routed somewhere and then failed
                # on this deliberately hollow runtime, which is the expected outcome for
                # the two kinds that do real work.
                pass

        assert unsupported == [], f"recognized kinds reaching the unsupported branch: {unsupported}"
        assert AUTHORITY_GATE_DECISION in RECOGNIZED_AUTHORITY_KINDS
        assert AUTHORITY_REPLAN_REQUEST in RECOGNIZED_AUTHORITY_KINDS
