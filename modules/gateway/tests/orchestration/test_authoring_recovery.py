"""A replan whose publish did not land is finished by a later tick (#4529, EPIC #4191).

`test_replan_authoring_dispatch.py` proves that one human `replan:` builds and publishes
exactly one authoring assignment. This file covers what happens when that publish
**fails** — the case `authoring_dispatch`'s docstring asserted ("a later pass
re-publishes it") and the code did not implement.

The defect, stated precisely, because every test here is a consequence of it. Publishing
is post-commit, which is right: the request row must be durable before an envelope
exists. But the same flush that publishes also **consumes the comment marker** that
caused the pass. So when a send failed, three things were true at once — the request row
sat in `QUEUED` (its retryable state), the marker was gone, and the human had been told
"an AI-DLC author has been assigned". Every later pass queried pending markers, found
none, and rebuilt nothing. The request was owed forever, by nobody, behind a reply that
said otherwise.

That is worse than a visible failure. A human told their replan failed re-issues it; a
human told it succeeded waits.

**How these tests are driven, because that is the evidence and not a detail.** Every test
goes through `run_engine_command_pass` and `flush_engine_commands` — the real tick — with
genuinely signed rows (`signed_command_rows`, the same contract vectors the webhook
signer is pinned against) in a marker table that *actually removes a row when it is
consumed*. So "the next tick sees no marker" is reproduced rather than stipulated: the
same table object is queried again by the second pass and returns nothing, because the
first flush consumed it. Driving `build_authoring_assignment` twice, or leaning on SQS's
five-minute dedup window, would both have passed against the broken code.

What is asserted, and why each property is load-bearing:

  - **The real next tick recovers it.** One table, two passes, marker consumed in
    between. This is the reproducer.
  - **A crash before the publish is the same case.** Nothing sent is
    indistinguishable, from the row's side, from a send that failed — and surviving that
    is the entire premise of committing first.
  - **Still exactly one author.** The run id is read from the row, the FIFO dedup id is
    derived from the request and its decision, and `mark_request_dispatched` is
    conditional on `QUEUED` — so a recovered publish addresses the run the server already
    commissioned, concurrent retries collapse, and one success removes the row from
    recovery's scope permanently.
  - **The first attempt is not raced.** A request whose own pass is still publishing it
    is excluded twice over: by an age boundary (a time heuristic) and by an exact
    identity check (not a heuristic). Both are proved, separately.
  - **Addressing comes from server state only.** The issue from the flow's own
    `intent_ref`, the installation from the org record through the same fail-closed
    resolver dispatch uses. A request that cannot be addressed stays `QUEUED` rather than
    being published somewhere nobody asked for.
  - **Recovery cannot cost the tick anything.** It runs after the command rows, and a
    failure inside it leaves owed requests `QUEUED` — the state that brings them back —
    without discarding the commands the pass correctly applied.
  - **The acknowledgement is honest.** The reply is composed before the publish is
    attempted, so a failed send downgrades *that comment's* not-yet-posted reply to the
    retryable wording. Asserted on what `_post_ack` actually received.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock
from uuid import NAMESPACE_URL, uuid5

import pytest
from sqlalchemy import select, update

from src.orchestration.authoring_dispatch import (
    RECOVERY_GRACE_SECONDS,
    RECOVERY_LIMIT,
    authoring_run_id,
    message_deduplication_id,
    publish_authoring,
    recover_owed_authoring,
)
from src.orchestration.engine_commands import (
    _REPLAN_QUEUED_REPLY,
    _REPLAN_UNQUEUED_REPLY,
    EngineCommandConfig,
    EngineCommandReport,
    _recover_authoring,
    flush_engine_commands,
    run_engine_command_pass,
)
from src.orchestration.models import (
    AmendmentRequestState,
    OrchestrationAmendmentRequest,
    OrchestrationFlow,
)
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Organization, User
from src.shared.models.vault import UserIdentity

from .signed_command_rows import envelope, signed_row, signing_keyring  # noqa: F401 - fixture used by name
from .test_pending_amendments import ORG_A, accepted_flow, base_proposal
from .test_replan_authoring_dispatch import (
    INSTALLATION,
    ISSUE,
    REPO,
    FakeSQS,
)
from .test_replan_authoring_dispatch import session as _session_fixture

#: `test_replan_authoring_dispatch`'s session, reused rather than rebuilt: its two pysqlite
#: hooks are load-bearing (see that module's docstring — without them a `begin_nested()`
#: SAVEPOINT silently becomes the outermost unit of work), and a second copy of that setup
#: is one edit away from diverging from the shape the store really writes.
#:
#: Rebound rather than imported under its own name because every test taking `session` as a
#: parameter would otherwise read to ruff as redefining an import (F811), and blanketing 30
#: test signatures with `noqa` to silence that would suppress the check where it is real.
session = _session_fixture

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("signing_keyring")]

QUEUE = "https://sqs.test/authoring.fifo"
CONFIG = EngineCommandConfig(enabled=True, table_name="events")

#: A second tenant, for the cross-tenant assertions. Recovery is deliberately not
#: tenant-scoped — it runs on the tick, which has no tenant of its own — so "each row is
#: addressed with ITS OWN tenant's installation" is a property that needs proving rather
#: than assuming.
ORG_B = "org-beta"
INSTALLATION_B = 77220011
ISSUE_B = 4530

#: The GitHub account the signed comment comes from, linked per tenant in
#: `user_identities`. The same account in two tenants is legitimate (unique per provider
#: *per org* since migration 021), which is why the cross-tenant tests can share it.
GITHUB_ID = "1042"


# ---------------------------------------------------------------------------------
# Harness: a real tick, real signed rows, and a marker table that really consumes
# ---------------------------------------------------------------------------------


class MarkerTable:
    """The events table, modelling the one behaviour this file turns on.

    `update_item` **removes** the row, so a later `query` genuinely returns nothing. That
    is the defect's precondition, and modelling it is what makes the reproducer a
    reproducer: a test that simply handed the second pass an empty item list would be
    asserting the premise instead of exercising it.
    """

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows: list[dict[str, Any]] = list(rows or [])
        self.consumed: list[str] = []

    def query(self, **_kwargs: Any) -> dict[str, Any]:
        return {"Items": list(self.rows)}

    def update_item(self, **kwargs: Any) -> dict[str, Any]:
        event_id = kwargs["Key"]["event_id"]
        self.consumed.append(event_id)
        self.rows = [row for row in self.rows if row["event_id"] != event_id]
        return {}


class SelectiveSQS(FakeSQS):
    """Fails only the sends whose FIFO group is named.

    Needed for the "one reply is rewritten, the other is not" case: with an
    all-or-nothing queue both assignments fail, and the test could not then tell a
    correction keyed on the assignment's own comment from one applied to whatever reply
    came first.
    """

    def __init__(self, *, failing_groups: set[str]) -> None:
        super().__init__()
        self.failing_groups = failing_groups

    def send_message(self, **kwargs: Any) -> dict[str, Any]:
        if kwargs.get("MessageGroupId") in self.failing_groups:
            raise RuntimeError("sqs unavailable for this group")
        return super().send_message(**kwargs)


def replan_row(
    *,
    org_id: str = ORG_A,
    issue: int = ISSUE,
    installation: int = INSTALLATION,
    text: str = "gate the deploy wave",
    event_id: str = "msg-0001",
) -> dict[str, Any]:
    """A genuinely signed `replan:` comment row, as the webhook Lambda writes one.

    Signed rather than hand-built: since #4539 the tick verifies attribution before it
    reads anything, so an unsigned row is quarantined and never reaches the replan branch
    at all — a test built on one would pass while exercising nothing.
    """
    return signed_row(
        envelope(
            tenant_id=org_id,
            repo=REPO,
            issue_number=issue,
            installation_id=str(installation),
            command_body=f"@agent-engine replan: {text}",
            sender_github_id=GITHUB_ID,
            sender_type="User",
            event_id=event_id,
            delivery_id=str(uuid5(NAMESPACE_URL, event_id)),
        )
    )


async def seed(
    session,
    *,
    org_id: str = ORG_A,
    installation: int = INSTALLATION,
    issue: int = ISSUE,
    flow_slug: str = "demo-flow",
) -> str:
    """One tenant able to run a real command: org, human, membership, identity, plan.

    Every row here is required rather than decorative, and each guards a different silent
    pass:

    * the **org** with exactly one installation — `resolve_installation_id` is fail-closed
      on zero and on more than one, so omitting it exercises the refusal path while
      looking like the happy one;
    * the **tenant membership** — authority comes from the database, not the comment, so
      without an `admin` row the commenter falls to least-privilege and the replan is
      refused;
    * the **GitHub identity** — how a signed `sender_github_id` becomes a platform
      identity inside ONE org;
    * the **accepted plan** whose `intent_ref` is this issue — what makes a comment on
      that issue address this flow at all.

    Distinct `flow_slug` per tenant because `uq_orchestration_flows_org_slug` is unique on
    `(org_id, slug)`; a shared slug silently reuses one flow and a test that thought it
    had built two would assert against one.
    """
    user_id = f"{org_id}-asker"
    session.add(Organization(id=org_id, name=f"Org {org_id}", github_installation_ids=[str(installation)]))
    session.add(User(id=user_id, org_id=org_id, team_id="team-test", email=f"{user_id}@example.com", cognito_sub=user_id))
    await session.flush()
    session.add(TenantMembership(user_id=user_id, tenant_id=org_id, role="admin", is_active=True))
    session.add(
        UserIdentity(
            user_id=user_id,
            org_id=org_id,
            team_id="team-test",
            provider="github",
            provider_user_id=GITHUB_ID,
            verification_method="oauth",
        )
    )
    flow_id = await accepted_flow(
        session,
        org_id=org_id,
        proposal=base_proposal(org_id=org_id, flow=flow_slug, intent_ref=str(issue)),
    )
    await session.commit()
    return flow_id


@pytest.fixture(autouse=True)
def dispatch_repo(monkeypatch):
    """The engine's configured dispatch repository, which an authoring run works in.

    Autouse because every test here depends on it and its absence is a silent no-op
    rather than a failure — a test that forgot it would pass for the wrong reason.
    """
    monkeypatch.setenv("BG_ORCH_DISPATCH_REPO", REPO)


async def engine_pass(session, table: MarkerTable) -> EngineCommandReport:
    """One real engine-command pass, committed the way the tick commits it."""
    report = await run_engine_command_pass(session, CONFIG, table=table)
    await session.commit()
    return report


async def flush(session, report: EngineCommandReport, *, table: MarkerTable, sqs: Any, monkeypatch) -> list[str]:
    """The real post-commit flush. Returns the reply text `_post_ack` actually received.

    The outer session's transaction is closed first because this is genuinely post-commit
    code: `publish_authoring` opens its own short session to mark the request dispatched,
    and here that session shares one in-memory SQLite connection with this one. A test
    still holding a read transaction would hit "cannot start a transaction within a
    transaction" — an artefact of the shared connection, not of the code under test,
    which in production gets a pooled connection of its own.
    """
    await session.rollback()
    factory = session.info["factory"]
    monkeypatch.setenv("BG_ORCH_DISPATCH_QUEUE_URL", QUEUE)
    monkeypatch.setattr("src.shared.database.get_session_factory", lambda: factory)
    monkeypatch.setattr("src.orchestration.authoring_dispatch._get_sqs_client", lambda _region: sqs)
    posted = AsyncMock()
    monkeypatch.setattr("src.orchestration.engine_commands._post_ack", posted)

    await flush_engine_commands(report, CONFIG, table=table)
    await session.rollback()

    return [call.args[0].message for call in posted.call_args_list]


async def requests(session, *, org_id: str | None = None) -> list[OrchestrationAmendmentRequest]:
    query = select(OrchestrationAmendmentRequest).order_by(OrchestrationAmendmentRequest.created_at.asc())
    if org_id is not None:
        query = query.where(OrchestrationAmendmentRequest.org_id == org_id)
    return list((await session.execute(query.execution_options(populate_existing=True))).scalars())


async def age_request(session, request_id: str, *, seconds: int | None = None) -> None:
    """Make a request old enough for recovery to consider it genuinely stuck.

    The grace boundary exists so recovery never races a first publish attempt, which
    means every "it recovers" test has to age its row past it. Written in terms of
    `RECOVERY_GRACE_SECONDS` rather than a literal, so retuning that constant does not
    silently turn these into tests of the grace window instead.
    """
    age = seconds if seconds is not None else RECOVERY_GRACE_SECONDS + 60
    await session.execute(
        update(OrchestrationAmendmentRequest)
        .where(OrchestrationAmendmentRequest.id == request_id)
        .values(created_at=datetime.now(UTC) - timedelta(seconds=age))
    )
    await session.commit()


async def stranded(
    session,
    monkeypatch,
    *,
    org_id: str = ORG_A,
    installation: int = INSTALLATION,
    issue: int = ISSUE,
    flow_slug: str = "demo-flow",
) -> tuple[MarkerTable, str, str]:
    """The exact state the defect left behind, produced through the real tick.

    A signed `replan:` is applied, the request row commits, the publish fails, and the
    marker is consumed anyway. Returns `(table, flow_id, request_id)`; on return the table
    holds no pending row, so the next pass genuinely is the next pass.
    """
    flow_id = await seed(session, org_id=org_id, installation=installation, issue=issue, flow_slug=flow_slug)
    table = MarkerTable([replan_row(org_id=org_id, issue=issue, installation=installation, event_id=f"{org_id}-replan")])

    report = await engine_pass(session, table)
    assert report.commands_applied == 1, "the fixture must apply a real command, not be quietly refused"
    replies = await flush(session, report, table=table, sqs=FakeSQS(fail=True), monkeypatch=monkeypatch)

    assert report.authoring_publish_failed == 1
    assert table.rows == [], "the marker is consumed by the same flush whose publish failed — the defect's precondition"
    assert replies == [_REPLAN_UNQUEUED_REPLY]
    rows = await requests(session, org_id=org_id)
    assert len(rows) == 1 and rows[0].state == AmendmentRequestState.QUEUED.value
    return table, flow_id, rows[0].id


# ---------------------------------------------------------------------------------
# The defect itself
# ---------------------------------------------------------------------------------


class TestAStrandedRequestIsRecovered:
    async def test_the_next_real_tick_publishes_the_owed_assignment(self, session, monkeypatch):
        """The reproducer. Nothing short of a second real pass demonstrates this.

        The table this pass queries is the same object the first flush consumed from, so
        there is no marker left to react to. Everything the rebuild needs comes from the
        durable row.
        """
        table, _flow_id, request_id = await stranded(session, monkeypatch)
        await age_request(session, request_id)

        report = await engine_pass(session, table)
        sqs = FakeSQS()
        replies = await flush(session, report, table=table, sqs=sqs, monkeypatch=monkeypatch)

        assert report.commands_read == 0, "no marker is left; this pass is driven entirely by the durable row"
        assert report.authoring_recovered == 1
        assert report.authoring_published == 1
        assert len(sqs.calls) == 1, "a durable QUEUED request was not retried once its marker had been consumed"
        assert (await requests(session))[0].state == AmendmentRequestState.DISPATCHED.value
        assert replies == [], "the comment was answered passes ago; recovery posts nothing new"

    async def test_nothing_published_at_all_recovers_too(self, session, monkeypatch):
        """The tick died before it sent anything — which the row cannot distinguish.

        Committed request, consumed marker, no send attempted and the in-memory report
        gone with the process. That is the scenario commit-then-publish exists for, and it
        is only actually survivable if something later reads the durable row back.
        """
        await seed(session)
        table = MarkerTable([replan_row()])
        report = await engine_pass(session, table)
        assert report.commands_applied == 1
        request_id = (await requests(session))[0].id
        # The marker goes (a concurrent tick's conditional write claimed it); nothing is
        # published, and the report is dropped on the floor as a dying process drops it.
        table.update_item(Key={"event_id": "msg-0001", "arrived_at": ""})
        await age_request(session, request_id)

        recovery = await engine_pass(session, table)
        sqs = FakeSQS()
        await flush(session, recovery, table=table, sqs=sqs, monkeypatch=monkeypatch)

        assert recovery.authoring_recovered == 1
        assert len(sqs.calls) == 1
        assert (await requests(session))[0].state == AmendmentRequestState.DISPATCHED.value

    async def test_recovery_addresses_the_run_the_server_already_commissioned(self, session, monkeypatch):
        """A rebuild must not mint a second authoring identity.

        The run id on the row was written in the request's own transaction and is what
        `resolve_authoring_request` checks, so a recovered envelope addressing a different
        run would produce an author the registration route refuses — work done and thrown
        away.
        """
        table, _flow_id, request_id = await stranded(session, monkeypatch)
        bound = (await requests(session))[0].author_run_id
        assert bound == authoring_run_id(request_id)
        await age_request(session, request_id)

        report = await engine_pass(session, table)
        sqs = FakeSQS()
        await flush(session, report, table=table, sqs=sqs, monkeypatch=monkeypatch)

        body = sqs.envelope(0)
        assert body["message_id"] == bound
        assert body["orchestration"]["request_id"] == request_id

    async def test_the_recovered_send_reuses_the_derived_deduplication_id(self, session, monkeypatch):
        """So the queue collapses a retry instead of enqueuing a second author.

        Derived from the request and its decision — never from a timestamp, which would
        change on every attempt and make the dedup window useless.
        """
        table, _flow_id, request_id = await stranded(session, monkeypatch)
        decision_id = (await requests(session))[0].replan_decision_id
        await age_request(session, request_id)

        report = await engine_pass(session, table)
        sqs = FakeSQS()
        await flush(session, report, table=table, sqs=sqs, monkeypatch=monkeypatch)

        assert sqs.calls[0]["MessageDeduplicationId"] == message_deduplication_id(request_id=request_id, replan_decision_id=decision_id)

    async def test_the_recovered_envelope_carries_no_credential(self, session, monkeypatch):
        """A rebuild is not a second, laxer envelope builder.

        Recovery goes through `build_authoring_assignment`, so every property
        `test_replan_authoring_dispatch` pins holds here too. This is the one worth
        re-asserting on the recovery path, because it is the one whose regression would be
        a published secret.
        """
        table, _flow_id, request_id = await stranded(session, monkeypatch)
        await age_request(session, request_id)

        report = await engine_pass(session, table)
        sqs = FakeSQS()
        await flush(session, report, table=table, sqs=sqs, monkeypatch=monkeypatch)

        serialized = json.dumps(sqs.envelope(0)).lower()
        for secret in ("credential", "token", "secret", "private_key"):
            assert secret not in serialized


class TestOneHumanAskStaysOneAuthor:
    async def test_a_successful_publish_takes_the_request_out_of_recovery_forever(self, session, monkeypatch):
        """`mark_request_dispatched` is conditional on `QUEUED`, so this is permanent.

        Asserted at both layers on purpose. `build_authoring_assignment` also returns None
        for a `DISPATCHED` request, so the end-to-end observation alone would still hold if
        the query stopped filtering on state — and the query would then read every
        dispatched request in the deployment on every tick. The state filter is the load
        bearing one, so it is named directly.
        """
        from src.orchestration.pending_amendments import owed_authoring_requests
        from src.shared.models.base import utcnow

        await seed(session)
        table = MarkerTable([replan_row()])
        report = await engine_pass(session, table)
        replies = await flush(session, report, table=table, sqs=FakeSQS(), monkeypatch=monkeypatch)
        assert replies == [_REPLAN_QUEUED_REPLY]
        row = (await requests(session))[0]
        assert row.state == AmendmentRequestState.DISPATCHED.value
        await age_request(session, row.id)

        owed = await owed_authoring_requests(session, limit=10, older_than=utcnow())
        recovery = await engine_pass(session, table)
        sqs = FakeSQS()
        await flush(session, recovery, table=table, sqs=sqs, monkeypatch=monkeypatch)

        assert owed == [], "a dispatched request must not even be READ as owed"
        assert recovery.authoring_recovered == 0
        assert sqs.calls == [], "a dispatched request must never be rebuilt"

    async def test_repeated_recovery_never_produces_a_second_assignment(self, session, monkeypatch):
        """Three failing ticks in a row: still one author owed, never two.

        The guarantee that matters when a queue is unreachable for a while. Each pass
        rebuilds the same assignment for the same run under the same dedup id, so the
        retry is safe however many times it happens.
        """
        table, _flow_id, request_id = await stranded(session, monkeypatch)
        await age_request(session, request_id)
        runs: set[str] = set()
        dedups: set[str] = set()

        for _attempt in range(3):
            report = await engine_pass(session, table)
            assert report.authoring_recovered == 1
            runs.update(a.author_run_id for a in report.pending_authoring)
            dedups.update(a.deduplication_id for a in report.pending_authoring)
            await flush(session, report, table=table, sqs=FakeSQS(fail=True), monkeypatch=monkeypatch)

        assert len(runs) == 1, f"recovery minted more than one authoring identity: {runs}"
        assert len(dedups) == 1, f"a retry that changes its dedup id would enqueue a second author: {dedups}"
        rows = await requests(session)
        assert len(rows) == 1 and rows[0].state == AmendmentRequestState.QUEUED.value

    async def test_two_concurrent_retries_collapse_onto_one_dispatch(self, session, monkeypatch):
        """Two ticks recovering the same request at once — the honest concurrency case.

        Both rebuild the same run under the same deduplication id, which is what makes the
        duplicate collapsible at the queue. And the state transition is one-way: the
        second `mark_request_dispatched` matches nothing, so it cannot rewrite the first
        one's `dispatched_at` and make an old dispatch look like it just happened.
        """
        _table, _flow_id, request_id = await stranded(session, monkeypatch)
        await age_request(session, request_id)

        first = await recover_owed_authoring(session)
        second = await recover_owed_authoring(session)
        assert len(first) == len(second) == 1
        assert first[0].author_run_id == second[0].author_run_id
        assert first[0].deduplication_id == second[0].deduplication_id

        await session.rollback()
        factory = session.info["factory"]
        sqs = FakeSQS()
        assert await publish_authoring(first[0], session_factory=factory, client=sqs, queue_url=QUEUE) is True
        dispatched_at = (await requests(session))[0].dispatched_at
        await session.rollback()
        assert await publish_authoring(second[0], session_factory=factory, client=sqs, queue_url=QUEUE) is True
        await session.rollback()

        assert {call["MessageDeduplicationId"] for call in sqs.calls} == {first[0].deduplication_id}
        rows = await requests(session)
        assert len(rows) == 1 and rows[0].state == AmendmentRequestState.DISPATCHED.value
        assert rows[0].dispatched_at == dispatched_at, "a one-way transition must not be rewritten by a losing retry"

    async def test_the_pass_that_created_a_request_does_not_also_recover_it(self, session, monkeypatch):
        """One tick, one message — recovery must not race the first attempt.

        Both guards are in play here and the age boundary alone would carry it, which is
        why the next test neutralises that one and checks the identity guard on its own.
        """
        await seed(session)
        table = MarkerTable([replan_row()])

        report = await engine_pass(session, table)
        sqs = FakeSQS()
        await flush(session, report, table=table, sqs=sqs, monkeypatch=monkeypatch)

        assert report.commands_applied == 1
        assert report.authoring_recovered == 0, "a request whose first publish is still in flight must not be rebuilt"
        assert len(sqs.calls) == 1

    async def test_the_identity_guard_holds_without_the_age_boundary(self, session, monkeypatch):
        """With the time heuristic neutralised, the exact request-id check is what holds.

        The boundary is a heuristic; this is an identity check. The invariant ("one human
        ask, one author") is worth both, and proving them separately is what stops one
        quietly becoming the only real guard. Driven by making the recovery read return
        the very assignment the pass already built — the collision the guard exists for,
        which the boundary would otherwise hide.
        """
        table, _flow_id, request_id = await stranded(session, monkeypatch)
        await age_request(session, request_id)
        report = await engine_pass(session, table)
        assert [a.request_id for a in report.pending_authoring] == [request_id]
        rebuilt = list(report.pending_authoring)

        monkeypatch.setattr("src.orchestration.authoring_dispatch.recover_owed_authoring", AsyncMock(return_value=rebuilt))
        await _recover_authoring(session, report)

        request_ids = [a.request_id for a in report.pending_authoring]
        assert request_ids == [request_id], f"the same request was queued for publication twice: {request_ids}"


# ---------------------------------------------------------------------------------
# Addressing: server state only
# ---------------------------------------------------------------------------------


class TestRecoveryAddressesFromServerState:
    async def test_the_issue_and_repo_come_from_server_state(self, session, monkeypatch):
        """The issue from the flow's own intent issue, the repo from engine configuration.

        Not from the consumed comment, which by now is gone anyway.
        """
        table, _flow_id, request_id = await stranded(session, monkeypatch)
        await age_request(session, request_id)

        report = await engine_pass(session, table)
        sqs = FakeSQS()
        await flush(session, report, table=table, sqs=sqs, monkeypatch=monkeypatch)

        assert sqs.envelope(0)["source_ref"]["issue"] == ISSUE
        assert sqs.envelope(0)["source_ref"]["repo"] == REPO

    async def test_a_hash_prefixed_intent_ref_is_still_addressed(self, session, monkeypatch):
        """Both `"4529"` and `"#4529"` occur in real proposals.

        Matching one spelling only would fail to recover half of all flows — and fail
        silently, because the row would simply stay `QUEUED`.
        """
        table, flow_id, request_id = await stranded(session, monkeypatch)
        await session.execute(update(OrchestrationFlow).where(OrchestrationFlow.id == flow_id).values(intent_ref=f"#{ISSUE}"))
        await age_request(session, request_id)

        report = await engine_pass(session, table)
        sqs = FakeSQS()
        await flush(session, report, table=table, sqs=sqs, monkeypatch=monkeypatch)

        assert sqs.envelope(0)["source_ref"]["issue"] == ISSUE

    @pytest.mark.parametrize(
        "intent_ref",
        [pytest.param(None, id="absent"), pytest.param("", id="empty"), pytest.param("see the epic", id="not-a-number")],
    )
    async def test_a_flow_with_no_usable_intent_issue_stays_queued(self, session, monkeypatch, intent_ref):
        """A skip, not a guess: an invented issue would post output nobody asked for."""
        table, flow_id, request_id = await stranded(session, monkeypatch)
        await session.execute(update(OrchestrationFlow).where(OrchestrationFlow.id == flow_id).values(intent_ref=intent_ref))
        await age_request(session, request_id)

        report = await engine_pass(session, table)
        sqs = FakeSQS()
        await flush(session, report, table=table, sqs=sqs, monkeypatch=monkeypatch)

        assert sqs.calls == []
        assert report.authoring_recovered == 0
        assert (await requests(session))[0].state == AmendmentRequestState.QUEUED.value

    @pytest.mark.parametrize(
        "installations",
        [pytest.param([], id="no-installation"), pytest.param([INSTALLATION, INSTALLATION_B], id="ambiguous-installation")],
    )
    async def test_an_unresolvable_installation_stays_queued(self, session, monkeypatch, installations):
        """The same fail-closed rule dispatch applies, on both zero and more than one.

        Guessing would address an authoring run into a repository nobody asked for, so the
        request stays owed until the ambiguity is resolved.
        """
        table, _flow_id, request_id = await stranded(session, monkeypatch)
        await session.execute(update(Organization).where(Organization.id == ORG_A).values(github_installation_ids=[str(i) for i in installations]))
        await age_request(session, request_id)

        report = await engine_pass(session, table)
        sqs = FakeSQS()
        await flush(session, report, table=table, sqs=sqs, monkeypatch=monkeypatch)

        assert sqs.calls == []
        assert report.authoring_recovered == 0
        assert (await requests(session))[0].state == AmendmentRequestState.QUEUED.value

    async def test_an_unset_dispatch_repository_rebuilds_nothing(self, session, monkeypatch):
        """Fail-closed, exactly as the command pass is: unaddressable is not published."""
        table, _flow_id, request_id = await stranded(session, monkeypatch)
        await age_request(session, request_id)
        monkeypatch.setenv("BG_ORCH_DISPATCH_REPO", "")

        report = await engine_pass(session, table)

        assert report.authoring_recovered == 0
        assert (await requests(session))[0].state == AmendmentRequestState.QUEUED.value

    async def test_a_requester_who_can_no_longer_be_resolved_stays_queued(self, session, monkeypatch):
        """An identity failure inside the rebuild is contained, and leaves it owed.

        The human has since been removed from the tenant, so the envelope's two identity
        namespaces cannot be resolved. `build_authoring_assignment` raises, and the honest
        outcome is that the request is still owed — not quietly discarded, and not the
        whole pass dying.
        """
        table, _flow_id, request_id = await stranded(session, monkeypatch)
        await session.execute(
            update(OrchestrationAmendmentRequest).where(OrchestrationAmendmentRequest.id == request_id).values(requested_by="ghost")
        )
        await age_request(session, request_id)

        report = await engine_pass(session, table)

        assert report.authoring_recovered == 0
        assert report.errors == 0, "a per-request identity failure is contained, not an error for the tick"
        assert (await requests(session))[0].state == AmendmentRequestState.QUEUED.value

    async def test_each_tenants_request_is_addressed_with_its_own_installation(self, session, monkeypatch):
        """Recovery is not tenant-scoped, so this is a property rather than an accident.

        It runs on the tick, which has no tenant of its own; `org_id` is returned per row
        and every downstream step re-derives its tenant from it. One installation applied
        to both rows would dispatch one tenant's authoring run on another tenant's
        credential.
        """
        table, _flow_a, request_a = await stranded(session, monkeypatch, org_id=ORG_A)
        _table_b, _flow_b, request_b = await stranded(
            session, monkeypatch, org_id=ORG_B, installation=INSTALLATION_B, issue=ISSUE_B, flow_slug="beta-flow"
        )
        await age_request(session, request_a)
        await age_request(session, request_b)

        report = await engine_pass(session, table)
        sqs = FakeSQS()
        await flush(session, report, table=table, sqs=sqs, monkeypatch=monkeypatch)

        assert report.authoring_recovered == 2
        bodies = {sqs.envelope(i)["orchestration"]["request_id"]: sqs.envelope(i) for i in range(len(sqs.calls))}
        assert bodies[request_a]["tenant_id"] == ORG_A
        assert bodies[request_b]["tenant_id"] == ORG_B
        assert bodies[request_a]["source_ref"]["installation_id"] == INSTALLATION
        assert bodies[request_b]["source_ref"]["installation_id"] == INSTALLATION_B
        assert bodies[request_a]["source_ref"]["issue"] == ISSUE
        assert bodies[request_b]["source_ref"]["issue"] == ISSUE_B


class TestTheGraceBoundary:
    async def test_a_request_younger_than_the_boundary_is_not_rebuilt(self, session, monkeypatch):
        _table, _flow_id, request_id = await stranded(session, monkeypatch)
        await age_request(session, request_id, seconds=RECOVERY_GRACE_SECONDS // 2)

        assert await recover_owed_authoring(session) == []

    async def test_a_request_older_than_the_boundary_is_rebuilt(self, session, monkeypatch):
        _table, _flow_id, request_id = await stranded(session, monkeypatch)
        await age_request(session, request_id)

        rebuilt = await recover_owed_authoring(session)

        assert [a.request_id for a in rebuilt] == [request_id]

    async def test_the_pass_is_bounded_and_answers_the_longest_waiting_first(self, session, monkeypatch):
        """A backlog must not make the tick unbounded, and must not starve anyone.

        `QUEUED` does not expire, so a remainder is picked up next wake. Oldest-first
        ordering is what stops the cap indefinitely starving one request behind newer
        arrivals — a fairness property, and the reason the cap is safe at all.
        """
        await seed(session)
        ids: list[str] = []
        for index in range(3):
            table = MarkerTable([replan_row(text=f"gate wave {index}", event_id=f"msg-wave-{index}")])
            report = await engine_pass(session, table)
            assert report.commands_applied == 1
            await flush(session, report, table=table, sqs=FakeSQS(fail=True), monkeypatch=monkeypatch)
            ids.append((await requests(session))[-1].id)

        # Descending age: the FIRST request recorded is made the OLDEST, so oldest-first
        # ordering has to return it even though newer rows exist.
        for index, request_id in enumerate(ids):
            await age_request(session, request_id, seconds=RECOVERY_GRACE_SECONDS + 3600 - (index * 600))

        rebuilt = await recover_owed_authoring(session, limit=2)

        assert len(rebuilt) == 2, "the per-pass cap was not applied"
        assert [a.request_id for a in rebuilt] == ids[:2], "requests were not answered oldest-first"

    async def test_the_defaults_are_bounded_and_not_inert(self):
        """A zero cap would make recovery inert; an unbounded one would stall the tick.

        `async` only because this module's `pytestmark` marks every test asyncio; there is
        nothing to await in a statement about two constants.
        """
        assert 0 < RECOVERY_LIMIT <= 100
        assert RECOVERY_GRACE_SECONDS > 0


class TestRecoveryCannotCostTheTick:
    """Three containment layers, each proved where the others cannot stand in for it.

    Recovery is wrapped at three depths — the pass's call into it, its own read, and each
    individual request — and they nest, so a test that raises at the innermost depth is
    satisfied by *any* one of them holding. Each test below therefore raises at the exact
    depth it names and, where a shallower layer would otherwise mask the result, calls
    `recover_owed_authoring` directly so that layer is not in play. Without that, removing
    two of the three changed nothing observable.
    """

    async def test_a_failure_inside_recovery_leaves_the_commands_applied(self, session, monkeypatch):
        """The outermost layer: the pass's own call into recovery.

        An unguarded raise here would reach the pass's caller, roll the session back, and
        throw away every command this invocation applied — trading a delayed retry for lost
        human decisions. Raised from `recover_owed_authoring` itself, which is past both
        inner layers, so only the pass's own guard can catch it.
        """
        table, _flow_id, request_id = await stranded(session, monkeypatch)
        await age_request(session, request_id)
        table.rows = [replan_row(text="another gate", event_id="msg-second")]
        monkeypatch.setattr(
            "src.orchestration.authoring_dispatch.recover_owed_authoring",
            AsyncMock(side_effect=RuntimeError("recovery is broken")),
        )

        report = await engine_pass(session, table)

        assert report.commands_applied == 1, "the second command must still be applied"
        assert report.authoring_recovered == 0
        assert report.errors == 0
        states = {row.state for row in await requests(session)}
        assert states == {AmendmentRequestState.QUEUED.value}, "owed requests must stay owed, not be marked done"

    async def test_an_unreadable_owed_query_rebuilds_nothing_and_does_not_raise(self, session, monkeypatch):
        """The middle layer: the durable read recovery starts from.

        Called directly rather than through the pass, so the pass's own guard cannot stand
        in for this one. A read that fails leaves every row `QUEUED`, so the next wake
        retries — which is why returning empty is the honest answer rather than propagating.
        """
        _table, _flow_id, request_id = await stranded(session, monkeypatch)
        await age_request(session, request_id)

        def explode(*_args, **_kwargs):
            raise RuntimeError("the owed-requests read is broken")

        monkeypatch.setattr("src.orchestration.authoring_dispatch.owed_authoring_requests", explode)

        assert await recover_owed_authoring(session) == []
        assert (await requests(session))[0].state == AmendmentRequestState.QUEUED.value

    async def test_one_request_that_cannot_be_rebuilt_does_not_stop_the_next(self, session, monkeypatch):
        """The innermost layer: one request raising must not abandon the rest of the batch.

        Distinct from the intent-ref and installation cases, which are deliberate `continue`
        skips rather than exceptions — those leave this guard untested. Here tenant A's
        requester genuinely cannot be resolved, so `build_authoring_assignment` raises, and
        B (later in the same oldest-first batch) must still be rebuilt. Called directly, so
        neither outer layer can account for the result.
        """
        _table, _flow_a, request_a = await stranded(session, monkeypatch, org_id=ORG_A)
        _table_b, _flow_b, request_b = await stranded(
            session, monkeypatch, org_id=ORG_B, installation=INSTALLATION_B, issue=ISSUE_B, flow_slug="beta-flow"
        )
        await session.execute(update(OrchestrationAmendmentRequest).where(OrchestrationAmendmentRequest.id == request_a).values(requested_by="ghost"))
        # A first, B second, so a guard that abandoned the batch would drop B.
        await age_request(session, request_a, seconds=RECOVERY_GRACE_SECONDS + 3600)
        await age_request(session, request_b, seconds=RECOVERY_GRACE_SECONDS + 60)

        rebuilt = await recover_owed_authoring(session)

        assert [a.request_id for a in rebuilt] == [request_b], "a raising request must be skipped, not end the batch"
        states = {row.id: row.state for row in await requests(session)}
        assert states[request_a] == AmendmentRequestState.QUEUED.value, "the skipped request is still owed"

    async def test_one_tenants_unrecoverable_request_does_not_strand_anothers(self, session, monkeypatch):
        """Per-request containment. Otherwise one broken tenant blocks every other.

        Tenant A's flow loses its addressable intent issue; tenant B's is fine. B must
        still be recovered on this same pass.
        """
        table, flow_a, request_a = await stranded(session, monkeypatch, org_id=ORG_A)
        _table_b, _flow_b, request_b = await stranded(
            session, monkeypatch, org_id=ORG_B, installation=INSTALLATION_B, issue=ISSUE_B, flow_slug="beta-flow"
        )
        await session.execute(update(OrchestrationFlow).where(OrchestrationFlow.id == flow_a).values(intent_ref=None))
        await age_request(session, request_a)
        await age_request(session, request_b)

        report = await engine_pass(session, table)
        sqs = FakeSQS()
        await flush(session, report, table=table, sqs=sqs, monkeypatch=monkeypatch)

        published = {sqs.envelope(i)["orchestration"]["request_id"] for i in range(len(sqs.calls))}
        assert published == {request_b}
        assert report.authoring_recovered == 1


# ---------------------------------------------------------------------------------
# The reply the human actually reads
# ---------------------------------------------------------------------------------


class TestTheReplyIsHonest:
    async def test_a_failed_publish_downgrades_its_own_reply_to_retryable(self, session, monkeypatch):
        """The reply is composed before the publish is attempted, so it starts optimistic.

        This is the correction. Without it the human is told an author was assigned while
        no author exists — and, being told it succeeded, they wait instead of re-issuing.
        Asserted on what `_post_ack` actually received, not on the report's intent.
        """
        await seed(session)
        table = MarkerTable([replan_row()])
        report = await engine_pass(session, table)

        replies = await flush(session, report, table=table, sqs=FakeSQS(fail=True), monkeypatch=monkeypatch)

        assert replies == [_REPLAN_UNQUEUED_REPLY]
        assert "not yet assigned" in replies[0] and "unchanged" in replies[0]
        assert report.success is False, "a recorded replan whose author was never summoned is not a successful pass"

    async def test_a_successful_publish_leaves_the_reply_alone(self, session, monkeypatch):
        await seed(session)
        table = MarkerTable([replan_row()])
        report = await engine_pass(session, table)

        replies = await flush(session, report, table=table, sqs=FakeSQS(), monkeypatch=monkeypatch)

        assert replies == [_REPLAN_QUEUED_REPLY]
        assert report.success is True

    async def test_only_the_failing_requests_reply_is_rewritten(self, session, monkeypatch):
        """Two tenants' replans in one pass; one publish fails. The other reply is untouched.

        Which is why the correction is keyed on the assignment's own event rather than
        applied to whatever reply happens to be first.
        """
        await seed(session)
        await seed(session, org_id=ORG_B, installation=INSTALLATION_B, issue=ISSUE_B, flow_slug="beta-flow")
        table = MarkerTable(
            [
                replan_row(event_id="msg-alpha"),
                replan_row(org_id=ORG_B, installation=INSTALLATION_B, issue=ISSUE_B, event_id="msg-beta"),
            ]
        )
        report = await engine_pass(session, table)
        assert report.commands_applied == 2
        failing = next(a.group_id for a in report.pending_authoring if a.org_id == ORG_B)

        replies = await flush(session, report, table=table, sqs=SelectiveSQS(failing_groups={failing}), monkeypatch=monkeypatch)

        assert replies == [_REPLAN_QUEUED_REPLY, _REPLAN_UNQUEUED_REPLY]

    async def test_a_recovered_assignment_rewrites_no_reply(self, session, monkeypatch):
        """There is no stale reply to correct — the comment was answered passes ago.

        Editing an old thread on every failed retry would be noise rather than news, and
        the request's visibility is already carried by the counters and by it staying
        `QUEUED`.
        """
        table, _flow_id, request_id = await stranded(session, monkeypatch)
        await age_request(session, request_id)

        report = await engine_pass(session, table)
        replies = await flush(session, report, table=table, sqs=FakeSQS(fail=True), monkeypatch=monkeypatch)

        assert report.authoring_ack_events == {}
        assert replies == []
        assert report.success is False, "a retry that did not land is still not a success"
