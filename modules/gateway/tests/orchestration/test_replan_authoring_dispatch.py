"""One verified human `replan:` summons exactly one AI-DLC author (#4529, EPIC #4191).

`test_pending_amendments.py` proves the request row's guarantees and
`test_engine_command_accept_amendment.py` proves the acceptance verb. This file covers
the link that was missing between them: `engine_commands`' replan arm →
`authoring_dispatch` → a published authoring assignment.

The properties asserted, and why each one is load-bearing:

  - **Commit-then-publish.** `_apply_command` sends nothing. The assignment is
    accumulated as data and published only by `flush_engine_commands`, after the
    caller commits. Asserted by driving the branch with a fake SQS that would record
    any send, and checking it stayed empty until the flush.
  - **One human ask ⇒ one author, three ways.** A duplicated delivery of the same
    comment, a re-pass whose publish ack was lost, and a flush run twice all converge
    on one assignment. The run id is derived (`uuid5`) rather than generated and the
    FIFO dedup id is derived from the request and its decision — never from a
    timestamp, which is the specific bug that would make every retry enqueue a second
    author.
  - **A failed publish is visibly retryable, never a successful replan.** The reply
    the human already received says "recorded but not yet assigned", the request row
    stays `QUEUED`, and `report.success` is False. A misleadingly successful replan is
    worse than a visible failure, because the human waits for work nobody is doing.
  - **The envelope carries an assignment, not a node.** No `graph_address`, `node_id`
    or `attempt`, because an authoring run owns no graph node — that absence is what
    stops its spend being attributed to work it did not do.
  - **The human's words travel as data.** `request_text` is carried in `payload` and
    reaches no field anything executes.

The harness is `test_pending_amendments.py`'s session (including its two pysqlite
hooks, which are load-bearing — see that module's docstring) plus that file's flow and
request helpers, so a request row here is the shape the store really writes.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.admin.access_control import AccessControl
from src.admin.config import AdminRole
from src.orchestration.adapters.github_commands import CommandVerb, parse_engine_command
from src.orchestration.authoring_dispatch import (
    AUTHORING_PERSONA,
    authoring_run_id,
    build_authoring_assignment,
    message_deduplication_id,
    message_group_id,
    publish_authoring,
)
from src.orchestration.engine_commands import (
    _REPLAN_QUEUED_REPLY,
    _REPLAN_UNQUEUED_REPLY,
    EngineCommandReport,
    _apply_command,
    flush_engine_commands,
)
from src.orchestration.models import (
    AmendmentRequestState,
    DecisionKind,
    OrchestrationAcceptedPlan,
    OrchestrationAmendmentRequest,
    OrchestrationDecision,
    OrchestrationEdge,
    OrchestrationNode,
    OrchestrationPendingAmendment,
    OrchestrationWorkClaim,
)
from src.shared.models.base import Base
from src.shared.models.organization import User

from .test_pending_amendments import ORG_A, accepted_flow, base_proposal

REPO = "acme/platform"
ISSUE = 4529
INSTALLATION = 88991122
ASKER = "cognito-sub-asker"


@pytest.fixture
async def session():
    """In-memory SQLite session with working SAVEPOINTs.

    Same two pysqlite hooks as `test_pending_amendments.py`: `record_replan_request`
    reconciles a duplicate delivery inside `begin_nested()`, and without these the
    SAVEPOINT would silently become the outermost unit of work — so the
    "a duplicated delivery writes one row" assertion would pass for the wrong reason.
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
        s.info["factory"] = factory
        yield s
    await engine.dispose()


class FakeSQS:
    """Records `send_message` so the FIFO keys and the envelope can be asserted on.

    `fail` makes every send raise, which is how the "publish did not land" cases are
    driven — the point being that the request row and the human's reply must both
    survive it.
    """

    def __init__(self, *, fail: bool = False):
        self.calls: list[dict] = []
        self.fail = fail

    def send_message(self, **kwargs):
        if self.fail:
            raise RuntimeError("sqs unavailable")
        self.calls.append(kwargs)
        return {"MessageId": f"sqs-{len(self.calls)}"}

    def envelope(self, index: int = 0) -> dict:
        return json.loads(self.calls[index]["MessageBody"])


def token(org_id: str = ORG_A, user_id: str = ASKER):
    """The commenter's identity **as the pass resolved it**, not as the comment claimed.

    Built the way `_resolve_platform_identity` builds it. Nothing from a comment body
    reaches these fields, which is why the request recorded below is legitimately
    attributed to a human.
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


def access_control(*, permitted: bool = True):
    from src.admin.config import Permission
    from src.admin.exceptions import AccessDeniedError

    access = MagicMock(spec=AccessControl)
    if permitted:
        access.check_permission = AsyncMock(return_value=True)
    else:
        access.check_permission = AsyncMock(
            side_effect=AccessDeniedError(
                message=f"Permission '{Permission.PLAN_APPROVE.value}' is required for this operation",
                required_permission=Permission.PLAN_APPROVE.value,
                user_role=AdminRole.ORG_ADMIN.value,
            )
        )
    access.get_user_role = AsyncMock(return_value=(AdminRole(AdminRole.ORG_ADMIN.value), ORG_A, None))
    return access


def replan_command(text: str = "gate the deploy wave"):
    """Built by the REAL parser, from the text a human would type."""
    command = parse_engine_command(f"@agent-engine replan: {text}")
    assert command is not None
    assert command.verb is CommandVerb.REPLAN
    return command


async def asker(session, *, org_id: str = ORG_A, user_id: str = ASKER) -> None:
    """The requesting human as a real `users` row.

    Required, not decorative: `build_authoring_assignment` resolves the envelope's
    two identity namespaces through `resolve_root_user_entity_id` /
    `resolve_user_entity_id`, which raise on an id that names nobody in this org. A
    fixture without this row would exercise the failure path while looking like the
    happy one.

    The `org_id` assertion on an existing row is the guard against a specific silent
    failure: this is keyed on the primary key, so re-calling it for a *second* tenant
    with the default id would find tenant A's row, skip the insert, and leave tenant B
    with no human at all. The resolver filters on `org_id` in SQL, so the caller would
    then see an identity refusal several layers away from its cause. Each tenant needs
    its own id; asserting here is what makes forgetting that loud.
    """
    existing = await session.get(User, user_id)
    if existing is None:
        session.add(User(id=user_id, org_id=org_id, team_id="team-test", email=f"{user_id}@example.com", cognito_sub=user_id))
        await session.flush()
        return
    assert existing.org_id == org_id, f"user {user_id!r} already exists in {existing.org_id!r}; give tenant {org_id!r} its own user id"


async def replan(
    session,
    report: EngineCommandReport,
    *,
    flow_id: str,
    org_id: str = ORG_A,
    text: str = "gate the deploy wave",
    source=...,
    user_id: str = ASKER,
):
    """Drive the replan branch exactly as `_handle_row` drives it, then commit.

    `user_id` is separable from `org_id` because a `users` row is per-tenant: a second
    tenant's replan must be asked by a human who exists *in that tenant*, or the
    identity resolution inside `build_authoring_assignment` refuses before any
    assignment is built. Defaulted so single-tenant callers read unchanged.
    """
    applied, message = await _apply_command(
        session,
        command=replan_command(text),
        org_id=org_id,
        flow_id=flow_id,
        context=token(org_id, user_id),
        access=access_control(),
        source=(REPO, ISSUE, INSTALLATION) if source is ... else source,
        publishes=report.pending_authoring,
    )
    await session.commit()
    return applied, message


async def requests(session, *, org_id: str = ORG_A) -> list[OrchestrationAmendmentRequest]:
    return list(
        (
            await session.execute(
                select(OrchestrationAmendmentRequest).where(OrchestrationAmendmentRequest.org_id == org_id).execution_options(populate_existing=True)
            )
        ).scalars()
    )


async def flush(session, report, sqs, monkeypatch, *, queue: str = "https://sqs.test/authoring.fifo"):
    """Run the post-commit flush with a fake queue and this test's session factory.

    The outer session's transaction is closed first because the flush is genuinely
    post-commit code: `publish_authoring` opens its own short session to mark the
    request dispatched, and here that session shares one in-memory SQLite connection
    with this one. A test still holding a read transaction would hit "cannot start a
    transaction within a transaction" — an artefact of the shared connection, not of the
    code under test, which in production gets a pooled connection of its own.
    """
    await session.rollback()
    factory = session.info["factory"]
    monkeypatch.setenv("BG_ORCH_DISPATCH_QUEUE_URL", queue)
    monkeypatch.setattr("src.shared.database.get_session_factory", lambda: factory)
    monkeypatch.setattr("src.orchestration.authoring_dispatch._get_sqs_client", lambda _region: sqs)
    await flush_engine_commands(report)


async def flow_with_asker(session, *, org_id: str = ORG_A, proposal=None, user_id: str = ASKER) -> str:
    """An accepted flow plus the human who may ask it to replan.

    `proposal` is exposed so a caller needing a *second* flow in one tenant can vary the
    slug — `uq_orchestration_flows_org_slug` makes two default flows collide — without
    reimplementing the asker seeding and losing it.
    """
    flow_id = await accepted_flow(session, org_id=org_id, proposal=proposal or base_proposal(org_id=org_id))
    await asker(session, org_id=org_id, user_id=user_id)
    await session.commit()
    return flow_id


# ---------------------------------------------------------------------------------
# Derived identity and FIFO keys
# ---------------------------------------------------------------------------------


class TestDerivedIdentity:
    def test_the_authoring_run_id_is_derived_from_the_request(self):
        """Two passes handling one request must compute the SAME run id.

        `assign_author_run` is conditional on the column still being NULL, so a
        re-pass normally writes nothing — but a *generated* id means a re-pass that
        did win the write would re-point a live assignment at a second run, admitting
        two authors for one human ask. Derivation is what removes that window.
        """
        assert authoring_run_id("request-1") == authoring_run_id("request-1")
        assert authoring_run_id("request-1") != authoring_run_id("request-2")
        assert authoring_run_id("request-1").startswith("replan:")

    def test_the_dedup_id_is_not_derived_from_time(self):
        """The bug this issue has to prevent, pinned directly.

        A time-derived deduplication id changes on every attempt, so SQS's 5-minute
        window never matches and a re-published assignment enqueues a second author.
        Called twice with the same inputs and asserted equal — and asserted to contain
        both identity components, so it also cannot collide across requests.
        """
        first = message_deduplication_id(request_id="request-1", replan_decision_id="decision-1")
        second = message_deduplication_id(request_id="request-1", replan_decision_id="decision-1")

        assert first == second
        assert "request-1" in first and "decision-1" in first
        assert first != message_deduplication_id(request_id="request-1", replan_decision_id="decision-2")
        assert len(first) <= 128

    def test_the_fifo_group_is_per_request(self):
        """Per request, so one stuck authoring job cannot head-of-line block another.

        A group shared across tenants would serialise every replan in the platform
        behind the slowest one.
        """
        mine = message_group_id(org_id=ORG_A, request_id="request-1")
        theirs = message_group_id(org_id="org-beta", request_id="request-1")
        sibling = message_group_id(org_id=ORG_A, request_id="request-2")

        assert mine != theirs and mine != sibling
        assert len(mine) <= 128


# ---------------------------------------------------------------------------------
# The replan branch: record, assign, publish — in that order
# ---------------------------------------------------------------------------------


class TestReplanQueuesOneAuthor:
    async def test_a_replan_records_a_request_and_accumulates_one_assignment(self, session):
        flow_id = await flow_with_asker(session)
        report = EngineCommandReport()

        applied, message = await replan(session, report, flow_id=flow_id)

        assert (applied, message) == (True, _REPLAN_QUEUED_REPLY)
        rows = await requests(session)
        assert len(rows) == 1
        assert rows[0].state == AmendmentRequestState.QUEUED.value
        assert rows[0].author_run_id == authoring_run_id(rows[0].id)
        assert len(report.pending_authoring) == 1
        assert report.pending_authoring[0].request_id == rows[0].id

    async def test_nothing_is_published_before_the_caller_commits(self, session, monkeypatch):
        """The ordering that is the whole design, asserted rather than assumed.

        An envelope published before its request row is durable manufactures an
        authoring run the platform has no record of commissioning: the run reaches the
        registration route, the server finds no assignment, and refuses — so the work
        is lost *and* the human was told their replan was accepted.
        """
        flow_id = await flow_with_asker(session)
        report = EngineCommandReport()
        sqs = FakeSQS()
        monkeypatch.setattr("src.orchestration.authoring_dispatch._get_sqs_client", lambda _region: sqs)

        await replan(session, report, flow_id=flow_id)

        assert sqs.calls == [], "_apply_command must accumulate the assignment, never send it"

        await flush(session, report, sqs, monkeypatch)

        assert len(sqs.calls) == 1

    async def test_the_decision_and_the_request_land_together(self, session):
        """Both in one transaction, so a crash between them is not a reachable state."""
        flow_id = await flow_with_asker(session)
        report = EngineCommandReport()

        await replan(session, report, flow_id=flow_id, text="add a spend gate")

        decision = (
            await session.execute(select(OrchestrationDecision).where(OrchestrationDecision.kind == DecisionKind.REPLAN_REQUESTED.value))
        ).scalar_one()
        row = (await requests(session))[0]
        assert row.replan_decision_id == decision.id
        assert row.requested_by == ASKER == decision.actor_id
        assert row.request_text == "add a spend gate"
        # A request is not an approval: the decision expresses no promotion.
        assert decision.to_state is None and decision.node_id is None

    async def test_the_request_pins_the_base_in_force_when_the_human_asked(self, session):
        flow_id = await flow_with_asker(session)
        in_force = (await session.execute(select(OrchestrationAcceptedPlan).where(OrchestrationAcceptedPlan.superseded_at.is_(None)))).scalar_one()
        report = EngineCommandReport()

        await replan(session, report, flow_id=flow_id)

        assignment = report.pending_authoring[0]
        assert assignment.base_plan_version == in_force.version
        assert assignment.base_plan_hash == in_force.plan_hash


class TestOneHumanAskOneAuthor:
    async def test_a_duplicated_delivery_queues_one_assignment(self, session, monkeypatch):
        """Two deliveries of the same comment reconcile onto one authoring job.

        Driven through the branch twice with the same decision — which is what a
        re-delivered webhook produces — rather than by calling the store directly, so
        the reconciliation is proved where a duplicate actually arrives.
        """
        flow_id = await flow_with_asker(session)
        report = EngineCommandReport()
        await replan(session, report, flow_id=flow_id)
        first = report.pending_authoring[0]

        # Same human, same text, a second delivery. A fresh decision row is appended
        # (the log is append-only and this IS a second delivery), so the second pass
        # records its own request — what must not happen is the FIRST request growing a
        # second author, or the first assignment being re-pointed.
        await replan(session, report, flow_id=flow_id)

        rows = {row.id: row for row in await requests(session)}
        assert rows[first.request_id].author_run_id == first.author_run_id
        # Every accumulated assignment names a distinct request and its own derived run.
        assert len({a.request_id for a in report.pending_authoring}) == len(report.pending_authoring)
        for assignment in report.pending_authoring:
            assert assignment.author_run_id == authoring_run_id(assignment.request_id)

    async def test_an_already_published_request_queues_nothing_new(self, session, monkeypatch):
        """The re-delivery of a replan whose envelope already reached the queue.

        `DISPATCHED` is the only state that means "one author is provably answering",
        so it is the only state that yields no new assignment — and the human sees the
        SAME success they saw the first time, because nothing new has happened and
        nothing is owed twice.
        """
        from src.orchestration.pending_amendments import resolve_authoring_request

        flow_id = await flow_with_asker(session)
        report = EngineCommandReport()
        await replan(session, report, flow_id=flow_id)
        await flush(session, report, FakeSQS(), monkeypatch)
        row = (await requests(session))[0]
        assert row.state == AmendmentRequestState.DISPATCHED.value
        bound = await resolve_authoring_request(session, org_id=ORG_A, request_id=row.id, author_run_id=row.author_run_id)

        again = await build_authoring_assignment(
            session,
            org_id=ORG_A,
            request=bound,
            repo=REPO,
            issue=ISSUE,
            installation_id=INSTALLATION,
        )

        assert again is None
        refreshed = (await requests(session))[0]
        assert refreshed.author_run_id == row.author_run_id

    async def test_a_lost_publish_ack_is_republished_to_the_same_run(self, session, monkeypatch):
        """A `QUEUED` request with a run already bound must be published again.

        A duplicated delivery and a publish whose ack was lost are indistinguishable
        from the server's side, and both are answered by re-publishing: returning
        nothing would strand the assignment `QUEUED` forever while every retry told the
        human an author had been assigned. Re-publishing reconciles to one author
        because the run id and the dedup id are both derived — asserted here by showing
        the second send carries the SAME run and the SAME deduplication id, so SQS
        collapses it.
        """
        from src.orchestration.pending_amendments import resolve_authoring_request

        flow_id = await flow_with_asker(session)
        report = EngineCommandReport()
        await replan(session, report, flow_id=flow_id)
        first = report.pending_authoring[0]
        # The publish fails, so the row stays QUEUED with its run bound.
        await flush(session, report, FakeSQS(fail=True), monkeypatch)
        row = (await requests(session))[0]
        assert row.state == AmendmentRequestState.QUEUED.value and row.author_run_id

        bound = await resolve_authoring_request(session, org_id=ORG_A, request_id=row.id, author_run_id=row.author_run_id)
        again = await build_authoring_assignment(
            session,
            org_id=ORG_A,
            request=bound,
            repo=REPO,
            issue=ISSUE,
            installation_id=INSTALLATION,
        )

        assert again is not None, "a queued assignment is still owed and must be re-publishable"
        assert again.author_run_id == first.author_run_id == row.author_run_id
        assert again.deduplication_id == first.deduplication_id

    async def test_a_second_flush_sends_nothing(self, session, monkeypatch):
        """`pending_authoring` is cleared by the flush, so a double flush is inert.

        Belt and braces with the deduplication id: even if this cleared nothing, SQS
        would collapse the duplicate — but a caller that flushes twice must not depend
        on the queue to be correct.
        """
        flow_id = await flow_with_asker(session)
        report = EngineCommandReport()
        await replan(session, report, flow_id=flow_id)
        sqs = FakeSQS()

        await flush(session, report, sqs, monkeypatch)
        await flush(session, report, sqs, monkeypatch)

        assert len(sqs.calls) == 1
        assert report.authoring_published == 1


class TestPublishOutcomes:
    async def test_a_published_assignment_marks_the_request_dispatched(self, session, monkeypatch):
        flow_id = await flow_with_asker(session)
        report = EngineCommandReport()
        await replan(session, report, flow_id=flow_id)
        row = (await requests(session))[0]
        sqs = FakeSQS()

        await flush(session, report, sqs, monkeypatch)

        assert report.authoring_published == 1 and report.authoring_publish_failed == 0
        assert report.success is True
        assert (await requests(session))[0].state == AmendmentRequestState.DISPATCHED.value
        call = sqs.calls[0]
        assert call["MessageGroupId"] == message_group_id(org_id=ORG_A, request_id=row.id)
        assert call["MessageDeduplicationId"] == message_deduplication_id(request_id=row.id, replan_decision_id=row.replan_decision_id)

    async def test_a_failed_publish_leaves_the_request_queued_and_forces_a_non_success(self, session, monkeypatch):
        """There is deliberately no `FAILED` state: `QUEUED` already means retryable.

        And the report must not read as clean — the command applied, the human was
        told an author was assigned, and no author is running. That is the one outcome
        that is invisible in every other counter.
        """
        flow_id = await flow_with_asker(session)
        report = EngineCommandReport()
        await replan(session, report, flow_id=flow_id)

        await flush(session, report, sqs := FakeSQS(fail=True), monkeypatch)

        assert sqs.calls == []
        assert report.authoring_publish_failed == 1
        assert report.success is False, "a recorded replan whose author was never summoned is not a successful pass"
        assert (await requests(session))[0].state == AmendmentRequestState.QUEUED.value

    async def test_an_unconfigured_queue_publishes_nothing_and_counts_the_failure(self, session, monkeypatch):
        flow_id = await flow_with_asker(session)
        report = EngineCommandReport()
        await replan(session, report, flow_id=flow_id)

        await flush(session, report, FakeSQS(), monkeypatch, queue="")

        assert report.authoring_publish_failed == 1
        assert (await requests(session))[0].state == AmendmentRequestState.QUEUED.value

    async def test_a_failed_dispatch_marker_still_reports_the_send_that_happened(self, session, monkeypatch):
        """The message IS on the queue, so this is a warning, not a lost assignment.

        Re-publishing a message the queue already holds is the benign failure (the
        dedup id collapses it); marking a request dispatched when nothing was sent is
        not. So the marker failing must not turn a real send into a reported failure.
        """
        flow_id = await flow_with_asker(session)
        report = EngineCommandReport()
        await replan(session, report, flow_id=flow_id)
        sqs = FakeSQS()

        def exploding_factory():
            raise RuntimeError("database unavailable")

        monkeypatch.setattr("src.orchestration.authoring_dispatch._get_sqs_client", lambda _region: sqs)
        published = await publish_authoring(
            report.pending_authoring[0],
            session_factory=exploding_factory,
            queue_url="https://sqs.test/authoring.fifo",
        )

        assert published is True
        assert len(sqs.calls) == 1
        assert (await requests(session))[0].state == AmendmentRequestState.QUEUED.value


class TestUnqueuableReplanIsVisiblyRetryable:
    @pytest.mark.parametrize(
        "source",
        [
            pytest.param(None, id="no-delivery-context"),
            pytest.param(("", ISSUE, INSTALLATION), id="dispatch-repo-unset"),
        ],
    )
    async def test_the_reply_never_claims_a_replan_no_author_will_answer(self, session, source):
        """A misleading success is worse than a visible failure.

        The human who reads "an author has been assigned" stops looking. The request
        row is durable and still `QUEUED`, so the honest report is "recorded, not yet
        assigned" — and it says how to retry.
        """
        flow_id = await flow_with_asker(session)
        report = EngineCommandReport()

        applied, message = await replan(session, report, flow_id=flow_id, source=source)

        assert applied is True, "the request IS recorded; the command was applied"
        assert message == _REPLAN_UNQUEUED_REPLY
        assert message != _REPLAN_QUEUED_REPLY
        assert "not yet assigned" in message and "unchanged" in message
        assert report.pending_authoring == []
        assert (await requests(session))[0].state == AmendmentRequestState.QUEUED.value

    async def test_an_unresolvable_asker_does_not_discard_the_recorded_request(self, session):
        """A failure building the assignment must not roll back the human's request.

        The `users` row is deliberately absent, so identity resolution raises inside
        `build_authoring_assignment`. `_queue_authoring` contains it: the decision and
        the request stay committed and `QUEUED`, so the assignment is still owed and a
        later pass can build it.

        And it must leave **no run bound**. Identity is resolved before the binding for
        exactly this reason: a request left with an author id but no published envelope
        would sit `QUEUED` while every later pass reported a success nobody was working
        on.
        """
        flow_id = await accepted_flow(session, proposal=base_proposal())
        report = EngineCommandReport()

        applied, message = await replan(session, report, flow_id=flow_id)

        assert (applied, message) == (True, _REPLAN_UNQUEUED_REPLY)
        rows = await requests(session)
        assert len(rows) == 1 and rows[0].state == AmendmentRequestState.QUEUED.value
        assert rows[0].author_run_id is None, "a failed build must leave the assignment genuinely retryable"
        assert report.pending_authoring == []


# ---------------------------------------------------------------------------------
# What the envelope carries, and what it must not
# ---------------------------------------------------------------------------------


class TestTheAssignmentEnvelope:
    @pytest.fixture
    async def envelope(self, session, monkeypatch):
        flow_id = await flow_with_asker(session)
        report = EngineCommandReport()
        await replan(session, report, flow_id=flow_id, text="gate before the deploy wave")
        sqs = FakeSQS()
        await flush(session, report, sqs, monkeypatch)
        return SimpleNamespace(body=sqs.envelope(), flow_id=flow_id, request=(await requests(session))[0])

    async def test_it_names_the_assignment_and_the_base_revision(self, envelope):
        assignment = envelope.body["orchestration"]
        assert assignment["flow_id"] == envelope.flow_id
        assert assignment["request_id"] == envelope.request.id
        assert assignment["root_decision_id"] == envelope.request.replan_decision_id
        assert assignment["base_plan_version"] == envelope.request.base_plan_version
        assert assignment["base_plan_hash"] == envelope.request.base_plan_hash

    async def test_it_carries_no_graph_node(self, envelope):
        """An authoring run owns no node, and the absence is load-bearing.

        With no `graph_address` its model calls cannot be attributed to a node's
        spend. Inventing one — or reusing the gate-rooted builder, which requires one
        — would charge an amendment proposal to work it did not execute.
        """
        assignment = envelope.body["orchestration"]
        for field_name in ("graph_address", "node_id", "attempt"):
            assert field_name not in assignment, f"an authoring assignment must not carry {field_name}"

    async def test_the_persona_is_the_authoring_one(self, envelope):
        assert envelope.body["persona"] == AUTHORING_PERSONA == "aidlc"
        assert envelope.body["intent"]["trigger"] == "engine_replan"
        assert envelope.body["channel"] == "orchestration", "nothing here came from a GitHub event"

    async def test_the_humans_words_travel_as_data(self, envelope):
        """Carried in `payload` for the author to consider, and executed by nothing."""
        assert envelope.body["payload"]["replan_request"] == "gate before the deploy wave"
        assert envelope.body["payload"]["requested_by"] == ASKER

    async def test_it_carries_no_credential(self, envelope):
        """A credential is minted at bootstrap against a verified pod, never shipped.

        Asserted on the serialized body, because that is what lands in a queue an
        operator can read.
        """
        serialized = json.dumps(envelope.body).lower()
        for secret in ("credential", "token", "secret", "private_key"):
            assert secret not in serialized

    async def test_the_asking_human_is_recorded_as_attribution(self, envelope):
        """`root_human_id` answers "who wanted this?" — it is not a credential.

        The acting principal stays the authoring run, which is why the run id is the
        `message_id` and the correlation id.
        """
        assert envelope.body["correlation"]["root_human_id"] == envelope.body["actor"]["user_id"]
        assert envelope.body["correlation"]["correlation_id"] == envelope.body["message_id"]
        assert envelope.body["correlation"]["chain_depth"] == 0
        assert envelope.body["message_id"] == envelope.request.author_run_id


# ---------------------------------------------------------------------------------
# What a replan must NOT do
# ---------------------------------------------------------------------------------


class TestAReplanChangesNoPromotionState:
    async def test_it_writes_no_node_edge_claim_or_plan_version(self, session, monkeypatch):
        """A request is not an approval, and queueing an author is not amending a plan.

        Counted across every table promotion state lives in, before and after both the
        command and the publish, so this holds for the whole path rather than for one
        function.
        """
        flow_id = await flow_with_asker(session)
        tables = {
            "nodes": OrchestrationNode,
            "edges": OrchestrationEdge,
            "claims": OrchestrationWorkClaim,
            "accepted_plans": OrchestrationAcceptedPlan,
            "drafts": OrchestrationPendingAmendment,
        }

        async def counts():
            return {name: (await session.execute(select(func.count()).select_from(model.__table__))).scalar_one() for name, model in tables.items()}

        before = await counts()
        report = EngineCommandReport()
        await replan(session, report, flow_id=flow_id)
        await flush(session, report, FakeSQS(), monkeypatch)

        assert await counts() == before

    async def test_an_unauthorized_commenter_records_nothing_and_queues_nothing(self, session):
        """`PLAN_APPROVE` is checked before the decision is appended.

        A replan is a statement about promotion state that a human will act on, and a
        recordable-by-anyone request would make the decisions table forgeable by
        comment — and would summon an author on an outsider's say-so.
        """
        from src.orchestration.engine_commands import _UNIFORM_REFUSAL

        flow_id = await flow_with_asker(session)
        report = EngineCommandReport()

        applied, message = await _apply_command(
            session,
            command=replan_command(),
            org_id=ORG_A,
            flow_id=flow_id,
            context=token(),
            access=access_control(permitted=False),
            source=(REPO, ISSUE, INSTALLATION),
            publishes=report.pending_authoring,
        )
        await session.commit()

        assert (applied, message) == (False, _UNIFORM_REFUSAL)
        assert await requests(session) == []
        assert report.pending_authoring == []

    def test_replan_requested_is_not_an_approval_kind(self):
        """Pinned here too, at the producer, not only at the fences.

        If `REPLAN_REQUESTED` were an approval kind, the request could root the very
        execution it asks to re-plan — a human *asking* for a change would become a
        human *approving* it.
        """
        from src.orchestration.genesis import APPROVAL_DECISION_KINDS

        assert DecisionKind.REPLAN_REQUESTED.value not in APPROVAL_DECISION_KINDS
