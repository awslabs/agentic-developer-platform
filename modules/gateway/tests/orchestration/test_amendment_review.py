"""The review lifecycle a draft needs before anyone can accept it (#5329).

#4529 built the store and the two ends of the loop: an author files a `PENDING` draft, a
named human accepts it. Between those two there was nothing. A reviewer could not list
what was waiting, read one, disagree with it, or change it — `PendingAmendmentState.REJECTED`
was declared with no writer at all, so "no" was not a thing anyone could say.

This file covers the four operations that close that gap, and the properties that make
them safe:

  - **list / show are reads.** No lock, no write, no state change, and scoped exactly as
    acceptance is — cross-tenant and wrong-flow are indistinguishable from absent, because
    `show` is the easier call to make in an enumeration loop.
  - **reject is terminal and attributable, and writes no plan.** Asserted by row counts:
    declining a proposal must not touch the graph. An already-rejected draft is not
    re-stamped, so a duplicated request cannot rewrite who declined it.
  - **edit produces a new revision and invalidates the old one.** This is the core
    property of the whole interaction: a human who reviewed revision 1 and types its id
    must be refused, not silently given revision 2. Asserted directly against
    `accept_amendment`.
  - **an approval binds to the exact revision it reviewed.** `expected_proposal_hash` is
    asserted to refuse a mismatch, and to refuse it *before* the staleness check, because
    "you are looking at an old revision" and "the plan moved" send a human to two
    different places.
  - **edits do not refresh a stale base.** An edited draft inherits the original's
    recorded base, so editing cannot be used to launder a draft past the conflict check
    that exists to catch it.

The session fixture and the plan fixtures are `test_pending_amendments.py`'s, reused
rather than re-derived so these operations are exercised against the same shapes #4529's
own guarantees are asserted against.
"""

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.orchestration.amend import amend_plan
from src.orchestration.compile import plan_hash
from src.orchestration.models import (
    OrchestrationAcceptedPlan,
    OrchestrationDecision,
    OrchestrationEdge,
    OrchestrationNode,
    OrchestrationPendingAmendment,
    PendingAmendmentState,
)
from src.orchestration.pending_amendments import (
    AmendmentConflictError,
    AmendmentDraftNotFoundError,
    accept_amendment,
    assign_author_run,
    get_pending_amendment,
    list_pending_amendments,
    record_replan_request,
    register_amendment_draft,
    reject_amendment,
    resolve_authoring_request,
    revise_amendment,
)
from src.orchestration.proposal import ProposedEdge, ProposedNode
from src.shared.models.base import Base
from tests.orchestration.legacy_plan import persist_legacy_plan
from tests.orchestration.test_pending_amendments import (
    ORG_A,
    ORG_B,
    address,
    amended_proposal,
    amender,
    approval,
    base_proposal,
)

REJECTER = "cognito-sub-reviewer"
EDITOR = "cognito-sub-editor"


@pytest.fixture
async def session():
    """In-memory SQLite session with working SAVEPOINTs. See test_amend.py's fixture."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _disable_pysqlite_implicit_begin(dbapi_connection, _record):
        dbapi_connection.isolation_level = None

    @event.listens_for(engine.sync_engine, "begin")
    def _emit_explicit_begin(connection):
        connection.exec_driver_sql("BEGIN")

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


async def accepted_flow(session, *, org_id: str = ORG_A) -> str:
    result = await persist_legacy_plan(session, base_proposal(org_id=org_id), approval(org_id))
    await session.commit()
    return result.flow_id


async def open_request(session, flow_id: str, *, org_id: str = ORG_A, decision_id: str = "decision-1", run_id: str = "orch:author-run-1"):
    request = await record_replan_request(
        session,
        org_id=org_id,
        flow_id=flow_id,
        replan_decision_id=decision_id,
        requested_by="cognito-sub-asker",
        request_text="gate the deploy wave",
    )
    await assign_author_run(session, org_id=org_id, request_id=request.id, author_run_id=run_id)
    await session.commit()
    return await resolve_authoring_request(session, org_id=org_id, request_id=request.id, author_run_id=run_id)


async def pending_draft(session, flow_id: str, *, org_id: str = ORG_A, decision_id: str = "decision-1", run_id: str = "orch:author-run-1", **kw):
    """One PENDING draft on `flow_id`, from a real authoring assignment."""
    request = await open_request(session, flow_id, org_id=org_id, decision_id=decision_id, run_id=run_id)
    draft = await register_amendment_draft(
        session,
        org_id=org_id,
        request=request,
        author_run_id=run_id,
        proposal=amended_proposal(org_id=org_id, **kw),
    )
    await session.commit()
    return draft


async def stale_pending_draft(session, flow_id: str, *, org_id: str = ORG_A, **kw):
    """A draft that is still PENDING but whose recorded base is no longer in force.

    Built by moving the plan with `amend_plan` **directly** rather than by accepting a
    sibling draft. A sibling's acceptance would supersede this draft, and `SUPERSEDED` is
    checked before staleness — so that route produces `draft_not_pending` and cannot
    exercise the stale-base path at all.

    The base is recorded when the human *asks* (`record_replan_request`), not when the
    document is authored, so the request must be opened before the plan moves.
    """
    request = await open_request(session, flow_id, org_id=org_id, **kw)
    draft = await register_amendment_draft(
        session,
        org_id=org_id,
        request=request,
        author_run_id=kw.get("run_id", "orch:author-run-1"),
        proposal=amended_proposal(org_id=org_id, extra_gate=True),
    )
    await session.commit()

    await amend_plan(session, flow_id, base_proposal(org_id=org_id, title="Renamed by another operator"), amender(org_id))
    await session.commit()
    return draft


def edited_proposal(*, org_id: str = ORG_A):
    """The reviewer's edit: the amendment, plus a cleanup story they asked for.

    A real edit rather than a trivial field change, so the revision it produces has a
    genuinely different graph and its hash differs for the right reason.
    """
    base = amended_proposal(org_id=org_id, extra_gate=False)
    return amended_proposal(
        org_id=org_id,
        extra_gate=False,
        nodes=[*base.nodes, ProposedNode(address=address("cleanup"), kind="story", title="Cleanup the old path", issue_ref="5330")],
        edges=[*base.edges, ProposedEdge(from_address=address("cleanup"), to_address=address("eval"))],
    )


async def _graph_counts(session) -> dict[str, int]:
    """Row counts of every table a review action must NOT write to."""
    tables = {
        "nodes": OrchestrationNode,
        "edges": OrchestrationEdge,
        "decisions": OrchestrationDecision,
        "accepted_plans": OrchestrationAcceptedPlan,
    }
    return {name: (await session.execute(select(func.count()).select_from(model.__table__))).scalar_one() for name, model in tables.items()}


async def _row(session, draft_id: str) -> OrchestrationPendingAmendment:
    """The stored row, for the columns the returned snapshots do not carry."""
    return (await session.execute(select(OrchestrationPendingAmendment).where(OrchestrationPendingAmendment.id == draft_id))).scalar_one()


async def _state_of(session, draft_id: str) -> str:
    return (await _row(session, draft_id)).state


class TestList:
    """What a reviewer opens the queue with."""

    async def test_a_flows_drafts_are_listed_newest_first(self, session):
        """Newest first: the queue's recent end is what a reviewer is being asked about.

        Asserting the exact sequence, not set membership — a set assertion here would pass
        under any ordering at all, including a nondeterministic one, which is exactly the
        bug this test exists to catch.
        """
        flow_id = await accepted_flow(session)
        first = await pending_draft(session, flow_id, decision_id="d-1", run_id="run-1", extra_gate=False)
        second = await pending_draft(session, flow_id, decision_id="d-2", run_id="run-2", extra_gate=True)

        listed = await list_pending_amendments(session, org_id=ORG_A, flow_id=flow_id)

        assert [view.draft_id for view in listed] == [second.draft_id, first.draft_id]

    async def test_terminal_drafts_are_listed_too_by_default(self, session):
        """A reviewer asking "what happened to the one I rejected" needs the terminal rows.

        A default that hid them would make `list` look like the draft vanished, which is
        indistinguishable from the store having lost it.
        """
        flow_id = await accepted_flow(session)
        draft = await pending_draft(session, flow_id)
        await reject_amendment(session, org_id=ORG_A, draft_id=draft.draft_id, rejected_by=REJECTER)
        await session.commit()

        listed = await list_pending_amendments(session, org_id=ORG_A, flow_id=flow_id)

        assert [(view.draft_id, view.state) for view in listed] == [(draft.draft_id, PendingAmendmentState.REJECTED.value)]

    async def test_states_can_be_restricted(self, session):
        flow_id = await accepted_flow(session)
        kept = await pending_draft(session, flow_id, decision_id="d-1", run_id="run-1", extra_gate=False)
        declined = await pending_draft(session, flow_id, decision_id="d-2", run_id="run-2", extra_gate=True)
        await reject_amendment(session, org_id=ORG_A, draft_id=declined.draft_id, rejected_by=REJECTER)
        await session.commit()

        listed = await list_pending_amendments(session, org_id=ORG_A, flow_id=flow_id, states=[PendingAmendmentState.PENDING.value])

        assert [view.draft_id for view in listed] == [kept.draft_id]

    async def test_list_omits_the_documents(self, session):
        """A flow's drafts are whole plan documents; twenty of them is a multi-megabyte list."""
        flow_id = await accepted_flow(session)
        await pending_draft(session, flow_id)

        listed = await list_pending_amendments(session, org_id=ORG_A, flow_id=flow_id)

        assert listed[0].proposal_document is None

    async def test_each_view_carries_the_human_ask_it_answers(self, session):
        """A draft read without the request that commissioned it is a plan with no rationale."""
        flow_id = await accepted_flow(session)
        await pending_draft(session, flow_id)

        listed = await list_pending_amendments(session, org_id=ORG_A, flow_id=flow_id)

        assert listed[0].requested_by == "cognito-sub-asker"
        assert listed[0].request_text == "gate the deploy wave"

    async def test_another_tenants_drafts_are_not_listed(self, session):
        flow_id = await accepted_flow(session)
        await pending_draft(session, flow_id)

        assert await list_pending_amendments(session, org_id=ORG_B, flow_id=flow_id) == []

    async def test_listing_writes_nothing(self, session):
        flow_id = await accepted_flow(session)
        draft = await pending_draft(session, flow_id)
        before = await _graph_counts(session)

        await list_pending_amendments(session, org_id=ORG_A, flow_id=flow_id)

        assert await _graph_counts(session) == before
        assert await _state_of(session, draft.draft_id) == PendingAmendmentState.PENDING.value


class TestShow:
    """One draft, in full, for the human who has to decide about it."""

    async def test_show_returns_the_document_and_the_revision_identity(self, session):
        """`proposal_hash` is what an acceptance binds to, so every view must carry it."""
        flow_id = await accepted_flow(session)
        draft = await pending_draft(session, flow_id)

        view = await get_pending_amendment(session, org_id=ORG_A, draft_id=draft.draft_id)

        assert view.proposal_document is not None
        assert view.proposal_hash == draft.proposal_hash

    async def test_show_reports_whether_the_recorded_base_still_holds(self, session):
        """Told before they try, not after acceptance refuses."""
        flow_id = await accepted_flow(session)
        draft = await pending_draft(session, flow_id)

        assert (await get_pending_amendment(session, org_id=ORG_A, draft_id=draft.draft_id)).base_is_current

        # A different amendment lands, moving the plan out from under this draft.
        other = await pending_draft(session, flow_id, decision_id="d-2", run_id="run-2", extra_gate=False)
        await accept_amendment(session, draft_id=other.draft_id, actor=amender(), flow_id=flow_id)
        await session.commit()

        assert not (await get_pending_amendment(session, org_id=ORG_A, draft_id=draft.draft_id)).base_is_current

    async def test_show_reports_the_gate_diff(self, session):
        """Gate placement is the one consequence a plan-document diff does not reveal."""
        flow_id = await accepted_flow(session)
        draft = await pending_draft(session, flow_id, extra_gate=True)

        view = await get_pending_amendment(session, org_id=ORG_A, draft_id=draft.draft_id)

        assert view.gate_diff.added == [address("spend-gate")]
        assert view.gate_diff.changes_gating

    async def test_another_tenants_draft_is_not_found(self, session):
        """Indistinguishable from absent: a distinguishable answer is an enumeration oracle."""
        flow_id = await accepted_flow(session)
        draft = await pending_draft(session, flow_id)

        with pytest.raises(AmendmentDraftNotFoundError):
            await get_pending_amendment(session, org_id=ORG_B, draft_id=draft.draft_id)

    async def test_a_draft_on_another_flow_is_not_found_when_a_flow_is_named(self, session):
        flow_id = await accepted_flow(session)
        draft = await pending_draft(session, flow_id)

        with pytest.raises(AmendmentDraftNotFoundError):
            await get_pending_amendment(session, org_id=ORG_A, draft_id=draft.draft_id, flow_id="another-flow")


class TestReject:
    """Saying no. The state #4529 declared and left unwritten."""

    async def test_rejecting_marks_the_draft_rejected_and_writes_no_plan(self, session):
        """Declining a proposal must not touch the graph — that is the whole point."""
        flow_id = await accepted_flow(session)
        draft = await pending_draft(session, flow_id)
        before = await _graph_counts(session)

        view = await reject_amendment(session, org_id=ORG_A, draft_id=draft.draft_id, rejected_by=REJECTER, reason="wrong wave")
        await session.commit()

        assert view.state == PendingAmendmentState.REJECTED.value
        assert await _graph_counts(session) == before

    async def test_a_rejected_draft_carries_no_acceptance_actor(self, session):
        """A terminal draft that was never applied must not claim an accepting human."""
        flow_id = await accepted_flow(session)
        draft = await pending_draft(session, flow_id)

        await reject_amendment(session, org_id=ORG_A, draft_id=draft.draft_id, rejected_by=REJECTER)
        await session.commit()

        row = await _row(session, draft.draft_id)
        assert row.accepted_by is None
        assert row.accepted_by_decision_id is None
        assert row.accepted_plan_version is None
        assert row.decided_at is not None

    async def test_a_rejected_draft_cannot_then_be_accepted(self, session):
        """Terminal means terminal: "no" is not a state acceptance can read past."""
        flow_id = await accepted_flow(session)
        draft = await pending_draft(session, flow_id)
        await reject_amendment(session, org_id=ORG_A, draft_id=draft.draft_id, rejected_by=REJECTER)
        await session.commit()

        with pytest.raises(AmendmentConflictError) as excinfo:
            await accept_amendment(session, draft_id=draft.draft_id, actor=amender(), flow_id=flow_id)

        assert excinfo.value.code == "draft_not_pending"

    async def test_rejecting_twice_does_not_rewrite_who_declined_it(self, session):
        """A duplicated request must not re-stamp the decider or the decision time."""
        flow_id = await accepted_flow(session)
        draft = await pending_draft(session, flow_id)
        first = await reject_amendment(session, org_id=ORG_A, draft_id=draft.draft_id, rejected_by=REJECTER)
        await session.commit()

        second = await reject_amendment(session, org_id=ORG_A, draft_id=draft.draft_id, rejected_by="someone-else")
        await session.commit()

        assert second.state == PendingAmendmentState.REJECTED.value
        assert second.decided_at == first.decided_at

    async def test_an_accepted_draft_cannot_be_rejected_afterwards(self, session):
        """Its content is in force; a late rejection would report a decision that did not happen."""
        flow_id = await accepted_flow(session)
        draft = await pending_draft(session, flow_id)
        await accept_amendment(session, draft_id=draft.draft_id, actor=amender(), flow_id=flow_id)
        await session.commit()

        with pytest.raises(AmendmentConflictError) as excinfo:
            await reject_amendment(session, org_id=ORG_A, draft_id=draft.draft_id, rejected_by=REJECTER)

        assert excinfo.value.code == "draft_already_accepted"

    async def test_another_tenant_cannot_reject(self, session):
        flow_id = await accepted_flow(session)
        draft = await pending_draft(session, flow_id)

        with pytest.raises(AmendmentDraftNotFoundError):
            await reject_amendment(session, org_id=ORG_B, draft_id=draft.draft_id, rejected_by=REJECTER)

        assert await _state_of(session, draft.draft_id) == PendingAmendmentState.PENDING.value


class TestRevise:
    """Editing produces a new revision. The property the whole interaction rests on."""

    async def test_editing_creates_a_new_draft_and_supersedes_the_original(self, session):
        flow_id = await accepted_flow(session)
        draft = await pending_draft(session, flow_id, extra_gate=False)

        revision = await revise_amendment(
            session, org_id=ORG_A, draft_id=draft.draft_id, proposal=edited_proposal(), revised_by=EDITOR, flow_id=flow_id
        )
        await session.commit()

        assert revision.draft_id != draft.draft_id
        assert revision.superseded_draft_id == draft.draft_id
        assert await _state_of(session, draft.draft_id) == PendingAmendmentState.SUPERSEDED.value
        assert await _state_of(session, revision.draft_id) == PendingAmendmentState.PENDING.value

    async def test_accepting_the_edited_draft_is_refused_and_applies_nothing(self, session):
        """The core property: an approval of revision 1 must not apply revision 2.

        A human who read the original and typed its id gets a refusal, not a silent
        substitution. If this passed by applying the revision, "preview then approve"
        would guarantee nothing at all.
        """
        flow_id = await accepted_flow(session)
        draft = await pending_draft(session, flow_id, extra_gate=False)
        await revise_amendment(session, org_id=ORG_A, draft_id=draft.draft_id, proposal=edited_proposal(), revised_by=EDITOR, flow_id=flow_id)
        await session.commit()
        versions_before = (await session.execute(select(func.count()).select_from(OrchestrationAcceptedPlan.__table__))).scalar_one()

        with pytest.raises(AmendmentConflictError) as excinfo:
            await accept_amendment(session, draft_id=draft.draft_id, actor=amender(), flow_id=flow_id)

        assert excinfo.value.code == "draft_not_pending"
        assert (await session.execute(select(func.count()).select_from(OrchestrationAcceptedPlan.__table__))).scalar_one() == versions_before

    async def test_the_refusal_names_the_revision_to_read_instead(self, session):
        """An edited draft has a live successor, so the human is sent to it rather than to replan."""
        flow_id = await accepted_flow(session)
        draft = await pending_draft(session, flow_id, extra_gate=False)
        revision = await revise_amendment(
            session, org_id=ORG_A, draft_id=draft.draft_id, proposal=edited_proposal(), revised_by=EDITOR, flow_id=flow_id
        )
        await session.commit()

        with pytest.raises(AmendmentConflictError) as excinfo:
            await accept_amendment(session, draft_id=draft.draft_id, actor=amender(), flow_id=flow_id)

        assert revision.draft_id in excinfo.value.message

    async def test_a_draft_displaced_by_someone_elses_acceptance_routes_to_replan_instead(self, session):
        """No successor to read: the winner is already in force, so only a replan resolves it.

        Distinguished from the edit case deliberately. Pointing this human at the accepted
        draft would be telling them to review the plan of record.
        """
        flow_id = await accepted_flow(session)
        loser = await pending_draft(session, flow_id, decision_id="d-1", run_id="run-1", extra_gate=True)
        winner = await pending_draft(session, flow_id, decision_id="d-2", run_id="run-2", extra_gate=False)
        await accept_amendment(session, draft_id=winner.draft_id, actor=amender(), flow_id=flow_id)
        await session.commit()

        with pytest.raises(AmendmentConflictError) as excinfo:
            await accept_amendment(session, draft_id=loser.draft_id, actor=amender(), flow_id=flow_id)

        assert "replan" in excinfo.value.message
        assert winner.draft_id not in excinfo.value.message

    async def test_the_revision_is_acceptable_and_applies_the_edited_content(self, session):
        """An edit that could not then be accepted would make the review loop a dead end."""
        flow_id = await accepted_flow(session)
        draft = await pending_draft(session, flow_id, extra_gate=False)
        revision = await revise_amendment(
            session, org_id=ORG_A, draft_id=draft.draft_id, proposal=edited_proposal(), revised_by=EDITOR, flow_id=flow_id
        )
        await session.commit()

        result = await accept_amendment(session, draft_id=revision.draft_id, actor=amender(), flow_id=flow_id)
        await session.commit()

        assert result.plan_version == 2
        nodes = (await session.execute(select(OrchestrationNode.node_ref).where(OrchestrationNode.flow_id == flow_id))).scalars().all()
        assert "cleanup" in set(nodes)

    async def test_reverting_to_terminal_content_preserves_the_pending_revision(self, session):
        flow_id = await accepted_flow(session)
        original = amended_proposal(extra_gate=False)
        draft = await pending_draft(session, flow_id, extra_gate=False)
        revision = await revise_amendment(
            session, org_id=ORG_A, draft_id=draft.draft_id, proposal=edited_proposal(), revised_by=EDITOR, flow_id=flow_id
        )
        await session.commit()

        with pytest.raises(AmendmentConflictError) as error:
            await revise_amendment(session, org_id=ORG_A, draft_id=revision.draft_id, proposal=original, revised_by=EDITOR, flow_id=flow_id)

        assert error.value.code == "draft_not_pending"
        assert "replan" in error.value.message
        assert await _state_of(session, draft.draft_id) == PendingAmendmentState.SUPERSEDED.value
        assert await _state_of(session, revision.draft_id) == PendingAmendmentState.PENDING.value
        with pytest.raises(AmendmentConflictError):
            await accept_amendment(session, draft_id=draft.draft_id, actor=amender(), flow_id=flow_id)
        result = await accept_amendment(session, draft_id=revision.draft_id, actor=amender(), flow_id=flow_id)
        assert result.plan_version == 2

    async def test_a_revision_inherits_the_originals_recorded_base(self, session):
        """Editing changes what is proposed, not what it was proposed against."""
        flow_id = await accepted_flow(session)
        stale = await stale_pending_draft(session, flow_id)
        assert stale.base_plan_version == 1  # the edit must not move this to v2

        revision = await revise_amendment(
            session, org_id=ORG_A, draft_id=stale.draft_id, proposal=edited_proposal(), revised_by=EDITOR, flow_id=flow_id
        )
        await session.commit()

        original = await _row(session, stale.draft_id)
        revised = await _row(session, revision.draft_id)
        assert (revised.base_plan_version, revised.base_plan_hash) == (original.base_plan_version, original.base_plan_hash)

    async def test_editing_cannot_launder_a_stale_draft_past_the_conflict_check(self, session):
        """The property above, asserted through the path that would be exploited.

        If `revise_amendment` re-read the base instead of inheriting it, editing a stale
        draft would "refresh" it into one that sails through the conflict check that exists
        to catch it — applying an amendment against a plan nobody reviewed it against.
        """
        flow_id = await accepted_flow(session)
        stale = await stale_pending_draft(session, flow_id)
        revision = await revise_amendment(
            session, org_id=ORG_A, draft_id=stale.draft_id, proposal=edited_proposal(), revised_by=EDITOR, flow_id=flow_id
        )
        await session.commit()

        with pytest.raises(AmendmentConflictError) as excinfo:
            await accept_amendment(session, draft_id=revision.draft_id, actor=amender(), flow_id=flow_id)

        assert excinfo.value.code == "stale_base"

    async def test_an_edit_producing_an_identical_document_writes_no_new_revision(self, session):
        """A no-op edit must not churn the revision a reviewer is reading."""
        flow_id = await accepted_flow(session)
        draft = await pending_draft(session, flow_id, extra_gate=True)

        revision = await revise_amendment(
            session,
            org_id=ORG_A,
            draft_id=draft.draft_id,
            proposal=amended_proposal(extra_gate=True),
            revised_by=EDITOR,
            flow_id=flow_id,
        )
        await session.commit()

        assert revision.unchanged
        assert revision.draft_id == draft.draft_id
        assert await _state_of(session, draft.draft_id) == PendingAmendmentState.PENDING.value

    async def test_editing_writes_no_plan_version(self, session):
        """An edit is still a proposal. It must reach the graph only through acceptance."""
        flow_id = await accepted_flow(session)
        draft = await pending_draft(session, flow_id, extra_gate=False)
        before = await _graph_counts(session)

        await revise_amendment(session, org_id=ORG_A, draft_id=draft.draft_id, proposal=edited_proposal(), revised_by=EDITOR, flow_id=flow_id)
        await session.commit()

        assert await _graph_counts(session) == before

    async def test_a_terminal_draft_cannot_be_edited(self, session):
        flow_id = await accepted_flow(session)
        draft = await pending_draft(session, flow_id)
        await reject_amendment(session, org_id=ORG_A, draft_id=draft.draft_id, rejected_by=REJECTER)
        await session.commit()

        with pytest.raises(AmendmentConflictError) as excinfo:
            await revise_amendment(session, org_id=ORG_A, draft_id=draft.draft_id, proposal=edited_proposal(), revised_by=EDITOR)

        assert excinfo.value.code == "draft_not_pending"

    async def test_an_accepted_draft_cannot_be_edited(self, session):
        """Its content is the plan of record; editing it would misrepresent what is in force."""
        flow_id = await accepted_flow(session)
        draft = await pending_draft(session, flow_id)
        await accept_amendment(session, draft_id=draft.draft_id, actor=amender(), flow_id=flow_id)
        await session.commit()

        with pytest.raises(AmendmentConflictError):
            await revise_amendment(session, org_id=ORG_A, draft_id=draft.draft_id, proposal=edited_proposal(), revised_by=EDITOR)

    async def test_another_tenant_cannot_edit(self, session):
        flow_id = await accepted_flow(session)
        draft = await pending_draft(session, flow_id)

        with pytest.raises(AmendmentDraftNotFoundError):
            await revise_amendment(session, org_id=ORG_B, draft_id=draft.draft_id, proposal=edited_proposal(), revised_by=EDITOR)

        assert await _state_of(session, draft.draft_id) == PendingAmendmentState.PENDING.value

    async def test_a_revision_records_the_draft_it_came_from(self, session):
        """So a reviewer reading the current revision can see it was edited, and from what."""
        flow_id = await accepted_flow(session)
        draft = await pending_draft(session, flow_id, extra_gate=False)
        revision = await revise_amendment(
            session, org_id=ORG_A, draft_id=draft.draft_id, proposal=edited_proposal(), revised_by=EDITOR, flow_id=flow_id
        )
        await session.commit()

        view = await get_pending_amendment(session, org_id=ORG_A, draft_id=revision.draft_id)

        assert view.revision_of_draft_id == draft.draft_id

    async def test_an_authored_draft_is_not_reported_as_a_revision(self, session):
        flow_id = await accepted_flow(session)
        draft = await pending_draft(session, flow_id)

        assert (await get_pending_amendment(session, org_id=ORG_A, draft_id=draft.draft_id)).revision_of_draft_id is None


class TestExactRevisionAcceptance:
    """An approval names the revision it reviewed, and applies only that one."""

    async def test_accepting_with_the_reviewed_hash_succeeds(self, session):
        flow_id = await accepted_flow(session)
        draft = await pending_draft(session, flow_id)

        result = await accept_amendment(
            session,
            draft_id=draft.draft_id,
            actor=amender(),
            flow_id=flow_id,
            expected_proposal_hash=draft.proposal_hash,
            expected_base_plan_version=draft.base_plan_version,
        )
        await session.commit()

        assert result.plan_version == 2

    async def test_a_hash_from_a_different_revision_is_refused(self, session):
        """The non-superseding case: same id, content the approver never saw.

        `revise_amendment` supersedes the old row, so this is defence for any other path
        that could change a draft's content — the check is on the content, not on the
        lifecycle that changed it.
        """
        flow_id = await accepted_flow(session)
        draft = await pending_draft(session, flow_id)

        with pytest.raises(AmendmentConflictError) as excinfo:
            await accept_amendment(
                session,
                draft_id=draft.draft_id,
                actor=amender(),
                flow_id=flow_id,
                expected_proposal_hash=plan_hash(edited_proposal()),
            )

        assert excinfo.value.code == "revision_mismatch"

    async def test_a_revision_mismatch_applies_nothing(self, session):
        flow_id = await accepted_flow(session)
        draft = await pending_draft(session, flow_id)
        before = await _graph_counts(session)

        with pytest.raises(AmendmentConflictError):
            await accept_amendment(session, draft_id=draft.draft_id, actor=amender(), flow_id=flow_id, expected_proposal_hash="0" * 64)
        await session.rollback()

        assert await _graph_counts(session) == before

    async def test_a_wrong_expected_base_version_is_refused(self, session):
        """Catches a mistyped id that happens to name a real draft on the same flow."""
        flow_id = await accepted_flow(session)
        draft = await pending_draft(session, flow_id)

        with pytest.raises(AmendmentConflictError) as excinfo:
            await accept_amendment(
                session,
                draft_id=draft.draft_id,
                actor=amender(),
                flow_id=flow_id,
                expected_base_plan_version=(draft.base_plan_version or 0) + 7,
            )

        assert excinfo.value.code == "base_version_mismatch"

    async def test_the_revision_check_runs_before_the_staleness_check(self, session):
        """Order matters: the two send a human to two different places.

        A draft that is BOTH stale and not the revision the approver read is doubly wrong.
        The revision mismatch is reported, because "you are reading an old revision" means
        re-read and accept the current one, while "the plan moved" means go request a fresh
        replan. Leading with staleness would send someone off to replan when a corrected
        revision was sitting right there, pending.
        """
        flow_id = await accepted_flow(session)
        stale = await stale_pending_draft(session, flow_id)

        with pytest.raises(AmendmentConflictError) as excinfo:
            await accept_amendment(session, draft_id=stale.draft_id, actor=amender(), flow_id=flow_id, expected_proposal_hash="0" * 64)

        assert excinfo.value.code == "revision_mismatch"

    async def test_a_stale_but_correctly_named_revision_still_reports_staleness(self, session):
        """The revision check must not mask the conflict it sits in front of."""
        flow_id = await accepted_flow(session)
        stale = await stale_pending_draft(session, flow_id)

        with pytest.raises(AmendmentConflictError) as excinfo:
            await accept_amendment(session, draft_id=stale.draft_id, actor=amender(), flow_id=flow_id, expected_proposal_hash=stale.proposal_hash)

        assert excinfo.value.code == "stale_base"

    async def test_omitting_the_hash_keeps_the_comment_path_working(self, session):
        """The GitHub comment path cannot carry a hash; it is protected by supersession instead."""
        flow_id = await accepted_flow(session)
        draft = await pending_draft(session, flow_id)

        result = await accept_amendment(session, draft_id=draft.draft_id, actor=amender(), flow_id=flow_id)
        await session.commit()

        assert result.plan_version == 2
