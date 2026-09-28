"""`@agent-engine accept amendment <draft-id>` on the command pass (#4529, EPIC #4191).

The parser half of this verb is covered in `test_github_commands_parser.py`; this file
covers what `engine_commands._apply_command` does once the verb arrives — the only
comment-driven path from a pending draft to promotion state.

The properties here are the ones that make an *additive* verb on a public comment
surface safe:

  - **Plain `accept` is byte-identical.** It answers the gate and can never select an
    amendment. Asserted by applying `accept` on a flow that has a pending draft and
    checking that no plan version was written — the regression this guards against
    ("accept picks up the latest amendment") would be invisible in the reply.
  - **The permission check runs before anything is read.** `PLAN_APPROVE`, the same
    permission every other verb here gates on, resolved from the server-resolved human
    identity. An unauthorized caller gets the uniform refusal and no draft state moves.
  - **Only a named draft.** `accept amendment` with no id is answered with instructions,
    not with a resolved "latest" — an unnamed accept would let a mistyped command apply
    a plan nobody read.
  - **Flow scope.** A draft belonging to another flow in the same tenant cannot be
    applied to the flow the human commented on, and is indistinguishable from absent.
  - **Stale base refuses and writes nothing.** The version that landed in between is not
    discarded.
  - **A repeated accept replays.** Same reply shape, no second version.
  - **The gate diff reaches the human.** A removed gate is a removed human decision
    point, and the reply must say so out loud.

`_apply_command` is exercised directly rather than through `run_engine_command_pass`,
because the DynamoDB read/verify/ack machinery around it is #4527's and #4539's and is
already covered by `test_command_attribution.py` and `test_engine_command_quarantine.py`.
What is new in #4529 is the branch, so the branch is what is driven.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.admin.access_control import AccessControl
from src.admin.config import AdminRole
from src.admin.exceptions import AccessDeniedError
from src.orchestration.adapters.github_commands import CommandVerb, EngineCommand, parse_engine_command
from src.orchestration.engine_commands import _UNIFORM_REFUSAL, _apply_command
from src.orchestration.models import (
    OrchestrationAcceptedPlan,
    OrchestrationPendingAmendment,
    PendingAmendmentState,
)
from src.orchestration.pending_amendments import accept_amendment, register_amendment_draft
from src.shared.models.base import Base

from .test_pending_amendments import (
    AUTHOR_RUN,
    ORG_A,
    accepted_flow,
    amended_proposal,
    amender,
    base_proposal,
    open_request,
)

HUMAN = "cognito-sub-approver"


@pytest.fixture
async def session():
    """In-memory SQLite session with working SAVEPOINTs.

    Same two pysqlite hooks as `test_pending_amendments.py`, and for the same reason:
    `accept_amendment` does its base check and its write inside `begin_nested()`, and
    without these the SAVEPOINT would silently become the outermost unit of work — so
    the "a refusal writes no plan version" assertions would pass for the wrong reason.
    """
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


def token(org_id: str = ORG_A, user_id: str = HUMAN):
    """The commenter's identity **as the pass resolved it**, not as the comment claimed.

    Built here the way `_resolve_platform_identity` builds it: a real platform user in
    one org. Nothing in a comment body reaches these fields, which is why the acceptance
    below is legitimately `actor_kind=HUMAN`.
    """
    from datetime import UTC, datetime, timedelta

    from src.shared.schemas.auth import TokenContext

    return TokenContext(
        user_id=user_id,
        org_id=org_id,
        team_id="",
        department_id="",
        account_type="user",
        scope="",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


def access_control(*, permitted: bool = True, role: str = AdminRole.ORG_ADMIN.value):
    """Stubbed access control. Denial is expressed as `PLAN_APPROVE`, the real gate."""
    from src.admin.config import Permission

    access = MagicMock(spec=AccessControl)
    if permitted:
        access.check_permission = AsyncMock(return_value=True)
    else:
        access.check_permission = AsyncMock(
            side_effect=AccessDeniedError(
                message=f"Permission '{Permission.PLAN_APPROVE.value}' is required for this operation",
                required_permission=Permission.PLAN_APPROVE.value,
                user_role=role,
            )
        )
    access.get_user_role = AsyncMock(return_value=(AdminRole(role), ORG_A, None))
    return access


async def pending_draft(session, flow_id, *, org_id: str = ORG_A, proposal=None, request=None, author_run_id: str = AUTHOR_RUN):
    """One registered pending amendment on `flow_id`. Returns its draft id."""
    req = request if request is not None else await open_request(session, flow_id, org_id=org_id)
    draft = await register_amendment_draft(
        session,
        org_id=org_id,
        request=req,
        author_run_id=req.author_run_id or author_run_id,
        proposal=proposal if proposal is not None else amended_proposal(org_id=org_id),
    )
    await session.commit()
    return draft.draft_id


async def plan_versions(session, *, org_id: str = ORG_A) -> int:
    return (
        await session.execute(select(func.count()).select_from(OrchestrationAcceptedPlan.__table__).where(OrchestrationAcceptedPlan.org_id == org_id))
    ).scalar_one()


async def apply(session, command: EngineCommand, *, flow_id: str, org_id: str = ORG_A, permitted: bool = True):
    return await _apply_command(
        session,
        command=command,
        org_id=org_id,
        flow_id=flow_id,
        context=token(org_id),
        access=access_control(permitted=permitted),
    )


def accept_amendment_command(draft_id: str | None) -> EngineCommand:
    """Built by the REAL parser, from the text a human would type.

    Not hand-constructed: the point of this file is the path a comment actually takes,
    and hand-building the dataclass would let the applier be tested against a shape the
    parser never produces.
    """
    body = "@agent-engine accept amendment" + (f" {draft_id}" if draft_id else "")
    command = parse_engine_command(body)
    assert command is not None, body
    assert command.verb is CommandVerb.ACCEPT_AMENDMENT
    return command


class TestAuthorization:
    async def test_an_unauthorized_commenter_moves_no_draft_and_no_plan(self, session):
        flow_id = await accepted_flow(session)
        draft_id = await pending_draft(session, flow_id)
        before = await plan_versions(session)

        applied, message = await apply(session, accept_amendment_command(draft_id), flow_id=flow_id, permitted=False)

        assert applied is False
        # The uniform constant, so "you lack permission" and "no such draft" are
        # indistinguishable to whoever commented.
        assert message == _UNIFORM_REFUSAL
        assert await plan_versions(session) == before
        draft = (await session.execute(select(OrchestrationPendingAmendment).where(OrchestrationPendingAmendment.id == draft_id))).scalar_one()
        assert draft.state == PendingAmendmentState.PENDING.value
        assert draft.accepted_by is None

    async def test_the_permission_checked_is_plan_approve(self, session):
        from src.admin.config import Permission

        flow_id = await accepted_flow(session)
        draft_id = await pending_draft(session, flow_id)
        access = access_control(permitted=True)

        await _apply_command(
            session,
            command=accept_amendment_command(draft_id),
            org_id=ORG_A,
            flow_id=flow_id,
            context=token(),
            access=access,
        )

        # Not PLAN_DRAFT. Accepting an amendment writes promotion state, so it must gate
        # on the permission #4200 minted for that — the authoring permission is strictly
        # weaker and is held by agent principals.
        (_ctx, permission), kwargs = access.check_permission.call_args
        assert permission is Permission.PLAN_APPROVE
        assert kwargs["target_org_id"] == ORG_A

    async def test_a_draft_in_another_tenant_is_indistinguishable_from_absent(self, session):
        their_flow = await accepted_flow(session, org_id="org-beta", proposal=base_proposal(org_id="org-beta"))
        their_draft = await pending_draft(
            session,
            their_flow,
            org_id="org-beta",
            proposal=amended_proposal(org_id="org-beta"),
        )
        my_flow = await accepted_flow(session)

        applied, message = await apply(session, accept_amendment_command(their_draft), flow_id=my_flow)

        assert applied is False
        assert message == f"no pending amendment `{their_draft}` on this plan."
        theirs = (await session.execute(select(OrchestrationPendingAmendment).where(OrchestrationPendingAmendment.id == their_draft))).scalar_one()
        assert theirs.state == PendingAmendmentState.PENDING.value

    async def test_a_draft_on_another_flow_in_my_tenant_is_refused(self, session):
        """The comment names a flow by being posted on its issue. That scope is enforced.

        Without the `flow_id` argument, an approver on flow A could apply an amendment
        authored for flow B by pasting its id — a cross-flow write authorized by a
        permission that was checked against the tenant, not the plan.
        """
        flow_a = await accepted_flow(session)
        flow_b = await accepted_flow(session, proposal=base_proposal(flow="other-flow"))
        draft_on_b = await pending_draft(session, flow_b, proposal=amended_proposal(flow="other-flow"))

        applied, message = await apply(session, accept_amendment_command(draft_on_b), flow_id=flow_a)

        assert applied is False
        assert message == f"no pending amendment `{draft_on_b}` on this plan."
        draft = (await session.execute(select(OrchestrationPendingAmendment).where(OrchestrationPendingAmendment.id == draft_on_b))).scalar_one()
        assert draft.state == PendingAmendmentState.PENDING.value


class TestPlainAcceptIsUnchanged:
    async def test_plain_accept_never_selects_a_pending_amendment(self, session):
        """The core additivity property.

        `accept` on a flow that has exactly one pending amendment must still mean "answer
        the outstanding gate". If it ever resolved the draft instead, the reply would look
        reasonable and the plan the human was replacing would have been replaced — the
        exact failure the issue forbids.
        """
        command = parse_engine_command("@agent-engine accept")
        assert command is not None and command.verb is CommandVerb.ACCEPT
        assert command.draft_ref is None

        # Two flows, identical plans. Only one has a pending amendment on it.
        without_draft = await accepted_flow(session)
        with_draft = await accepted_flow(session, proposal=base_proposal(flow="gated-flow"))
        draft_id = await pending_draft(session, with_draft, proposal=amended_proposal(flow="gated-flow"))
        before = await plan_versions(session)

        control_applied, control_message = await apply(session, command, flow_id=without_draft)
        applied, message = await apply(session, command, flow_id=with_draft)

        # The property is *indifference*: the presence of a pending amendment changes
        # nothing about what `accept` does or says. Whether the gate answer itself
        # succeeds is `test_command_attribution.py`'s subject; that both calls agree is
        # this file's.
        assert (applied, message) == (control_applied, control_message)
        assert draft_id not in message
        # And nothing moved: no new plan version, draft still pending, no acceptance
        # actor invented for it.
        assert await plan_versions(session) == before
        draft = (await session.execute(select(OrchestrationPendingAmendment).where(OrchestrationPendingAmendment.id == draft_id))).scalar_one()
        assert draft.state == PendingAmendmentState.PENDING.value
        assert draft.accepted_by is None

    async def test_accept_amendment_without_an_id_asks_for_one(self, session):
        """A recognised shape with nothing named resolves nothing."""
        flow_id = await accepted_flow(session)
        draft_id = await pending_draft(session, flow_id)
        before = await plan_versions(session)

        applied, message = await apply(session, accept_amendment_command(None), flow_id=flow_id)

        assert applied is False
        assert "accept amendment <draft-id>" in message
        assert await plan_versions(session) == before
        draft = (await session.execute(select(OrchestrationPendingAmendment).where(OrchestrationPendingAmendment.id == draft_id))).scalar_one()
        assert draft.state == PendingAmendmentState.PENDING.value


class TestAcceptance:
    async def test_a_named_draft_is_applied_as_the_resolved_human(self, session):
        flow_id = await accepted_flow(session)
        draft_id = await pending_draft(session, flow_id)

        applied, message = await apply(session, accept_amendment_command(draft_id), flow_id=flow_id)

        assert applied is True
        assert draft_id in message
        draft = (await session.execute(select(OrchestrationPendingAmendment).where(OrchestrationPendingAmendment.id == draft_id))).scalar_one()
        assert draft.state == PendingAmendmentState.ACCEPTED.value
        # The accepting actor is the commenter the pass resolved server-side — never the
        # authoring run that filed the draft.
        assert draft.accepted_by == HUMAN
        assert draft.accepted_plan_version is not None
        assert draft.accepted_by_decision_id

    async def test_the_reply_names_the_gate_the_amendment_adds(self, session):
        flow_id = await accepted_flow(session)
        draft_id = await pending_draft(session, flow_id)

        _applied, message = await apply(session, accept_amendment_command(draft_id), flow_id=flow_id)

        assert "spend-gate" in message
        assert "Gates added" in message

    async def test_the_reply_shouts_about_a_removed_gate(self, session):
        """A dropped gate is a dropped human decision point and must not read as an edit.

        The amendment here keeps the work and removes `deploy-gate`. Nothing else in the
        reply distinguishes it from a benign change, so the wording is the control.
        """
        flow_id = await accepted_flow(session)
        no_gate = amended_proposal(extra_gate=False)
        no_gate = no_gate.model_copy(
            update={
                "nodes": [node for node in no_gate.nodes if node.kind != "gate"],
                "edges": [edge for edge in no_gate.edges if "deploy-gate" not in edge.to_address],
            }
        )
        draft_id = await pending_draft(session, flow_id, proposal=no_gate)

        applied, message = await apply(session, accept_amendment_command(draft_id), flow_id=flow_id)

        assert applied is True
        assert "REMOVED" in message
        assert "deploy-gate" in message

    async def test_an_amendment_that_moves_no_gate_says_so(self, session):
        flow_id = await accepted_flow(session)
        draft_id = await pending_draft(session, flow_id, proposal=amended_proposal(extra_gate=False))

        applied, message = await apply(session, accept_amendment_command(draft_id), flow_id=flow_id)

        assert applied is True
        assert "Gate placement is unchanged." in message


class TestConflicts:
    async def test_a_stale_draft_is_refused_and_discards_nothing(self, session):
        """v2 landed after the draft was authored against v1. Refused, never rebased."""
        flow_id = await accepted_flow(session)
        stale = await pending_draft(session, flow_id)

        # A second, independent amendment lands first.
        other_request = await open_request(session, flow_id, decision_id="decision-2", run_id="orch:author-run-2")
        winner = await register_amendment_draft(
            session,
            org_id=ORG_A,
            request=other_request,
            author_run_id=other_request.author_run_id,
            proposal=amended_proposal(extra_gate=False),
        )
        await session.commit()
        await accept_amendment(session, draft_id=winner.draft_id, actor=amender(), flow_id=flow_id)
        await session.commit()
        versions_after_winner = await plan_versions(session)

        applied, message = await apply(session, accept_amendment_command(stale), flow_id=flow_id)

        assert applied is False
        # Accepting the stale one would have discarded `winner`'s amendment while
        # reporting success. It must not, and the reply must route the human to replan.
        assert "replan" in message
        assert await plan_versions(session) == versions_after_winner
        row = (await session.execute(select(OrchestrationPendingAmendment).where(OrchestrationPendingAmendment.id == stale))).scalar_one()
        # `superseded` by the winning acceptance, not `rejected`: nobody declined it.
        assert row.state == PendingAmendmentState.SUPERSEDED.value
        assert row.superseded_by_draft_id == winner.draft_id

    async def test_a_repeated_accept_replays_instead_of_amending_again(self, session):
        """A human re-sending the comment, or a duplicated delivery.

        Asserted after the acceptance has already superseded the base, which is the case
        a naive check order gets wrong: the accepted draft's own recorded base is
        guaranteed not to match what is in force, so a base-first comparison would answer
        every repeat with a stale-base conflict.
        """
        flow_id = await accepted_flow(session)
        draft_id = await pending_draft(session, flow_id)

        first_applied, _first = await apply(session, accept_amendment_command(draft_id), flow_id=flow_id)
        await session.commit()
        assert first_applied is True
        versions = await plan_versions(session)

        second_applied, second = await apply(session, accept_amendment_command(draft_id), flow_id=flow_id)

        assert second_applied is True
        assert "already accepted" in second
        assert await plan_versions(session) == versions

    async def test_two_pending_amendments_admit_exactly_one_successor(self, session):
        flow_id = await accepted_flow(session)
        first = await pending_draft(session, flow_id)
        second_request = await open_request(session, flow_id, decision_id="decision-2", run_id="orch:author-run-2")
        second = await pending_draft(session, flow_id, request=second_request, proposal=amended_proposal(extra_gate=False))

        applied, message = await apply(session, accept_amendment_command(first), flow_id=flow_id)

        assert applied is True
        # The human is told the other draft is gone, so they do not sit waiting to accept
        # something that can now only be refused.
        assert "superseded" in message
        loser = (await session.execute(select(OrchestrationPendingAmendment).where(OrchestrationPendingAmendment.id == second))).scalar_one()
        assert loser.state == PendingAmendmentState.SUPERSEDED.value
        assert loser.superseded_by_draft_id == first
        # Superseding is not deciding: no acceptance actor was invented for it.
        assert loser.accepted_by is None

    async def test_an_unknown_draft_id_writes_nothing(self, session):
        flow_id = await accepted_flow(session)
        before = await plan_versions(session)

        applied, message = await apply(
            session,
            accept_amendment_command("3f2504e0-4f89-11d3-9a0c-0305e82c3301"),
            flow_id=flow_id,
        )

        assert applied is False
        assert "no pending amendment" in message
        assert await plan_versions(session) == before
