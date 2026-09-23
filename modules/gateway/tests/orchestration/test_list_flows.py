"""Tests for the flows list endpoint: `GET /orchestration/flows`.

Issue #4869. This is the endpoint that makes the orchestration engine reachable at
all: the graph view is addressable only by flow id, so before this route a flow
nobody had the id for was invisible along with every gate waiting on a human.

The guarantees under test, in rough order of how much damage their absence does:

  - **`list_flows` is still unbounded.** Three regression tests with **30** flows —
    more than one page — because bounding it would make `compile._resolve_flow`
    miss an existing slug and create a duplicate flow under it (there is no
    `uq(org_id, slug)` to stop it), and would disable `registration`'s in-force
    plan conflict guard. This is the highest-consequence failure in the issue and
    it is a silent one, so it is tested first.
  - **The filtered `total` counts every match, not the page.** A `total` describing
    only the current page makes the pager lie about how much is there.
  - **Query count is constant in page size.** Asserted by counting statements, not
    by timing. Deliberately *not* pinned to a literal: a specific number invites
    satisfying it with a correlated subquery, which re-executes per row while
    still looking like one statement.
  - **Waves come back in first-appearance order at 14 waves.** `wave-10` must not
    precede `wave-2`. The bug is invisible below 10 waves and appears exactly at
    the scale the rail is designed for.
  - **`sort=updated` does not lead with never-updated flows.** Raw NULL
    `updated_at` sorts first under PostgreSQL `DESC` and last under SQLite, so the
    unfixed version passes locally and misorders in production.
  - **Stalled is decision-derived, latest-wins.** Stall detection writes `failed`
    (`stall.py`), so a stall and a plain failure share an engine state; and a node
    that stalled, was resumed, then halted must not still read as stalled.
  - **Chips are unfiltered and agree with the rows.** One derivation function for
    both, or a chip claims `1 stalled` while no row shows it.
  - **A `_` in a slug does not merge two flows' costs.** `_` is a legal slug
    character *and* the single-char LIKE wildcard.
  - **Zero-node flows appear**, as `status=empty` — proving LEFT, not inner, join.
  - Tenant isolation, `403` before any query, `limit=101` → 422, empty org → 200.

Session, app and client fixtures mirror `test_read_api.py`, including its two
pysqlite hooks; `client_for` gates on `USAGE_READ` because that is the permission
this route checks.
"""

import inspect
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.admin.config import Permission
from src.orchestration.compile import _resolve_flow, plan_hash
from src.orchestration.cost import COST_SCOPE_LABEL, get_cost_by_address_prefixes
from src.orchestration.display_state import DISPLAY_TO_ENGINE, FlowStatus, derive_flow_status
from src.orchestration.models import (
    DecisionKind,
    NodeKind,
    OrchestrationDecision,
    OrchestrationFlow,
    OrchestrationNode,
)
from src.orchestration.proposal import LoopProposal
from src.orchestration.registration import DraftFlowConflictError, _refuse_if_flow_is_live
from src.orchestration.repository import OrchestrationRepository
from src.orchestration.state import ActorKind, NodeState
from src.shared.models.base import Base
from src.shared.models.usage import UsageLog
from src.shared.schemas.auth import TokenContext

ROUTE = "/orchestration/flows"

ORG_A = "org-alpha"
ORG_B = "org-beta"
USER_ID = "cognito-sub-operator"
SPEC_REVISION = "issue-4120-r1"

# Fixed base instant. Every seeded row gets an explicit `created_at` derived from
# it, because several tests here assert an *order* — and rows created in one
# transaction can otherwise share a timestamp, making the assertion depend on
# insertion luck rather than on the ORDER BY under test.
BASE_TIME = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
async def session():
    """In-memory SQLite session with working SAVEPOINTs. See test_read_api.py."""
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


def _token_context(org_id: str, *, user_id: str = USER_ID) -> TokenContext:
    """An authenticated caller in `org_id`.

    `org_id` is an authenticated Cognito claim and is never writable by a request
    header, which is what lets the route use it as the tenant directly.
    """
    return TokenContext(
        user_id=user_id,
        org_id=org_id,
        team_id="",
        department_id="",
        account_type="human",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


@pytest.fixture
def app_with_router(session):
    """A minimal app carrying only the orchestration router.

    Deliberately not `create_app()`: that pulls the whole middleware stack and
    would make an authz assertion here depend on all of it.
    """
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse

    from src.auth.dependencies import get_current_user
    from src.orchestration.routes import router as orchestration_router
    from src.shared.database import get_db
    from src.shared.exceptions import BedrockGatewayError

    app = FastAPI()
    app.include_router(orchestration_router)

    @app.exception_handler(BedrockGatewayError)
    async def _gateway_error_handler(_request: Request, exc: BedrockGatewayError):
        return JSONResponse(status_code=exc.status_code, content={"error": exc.error, "message": exc.message})

    async def override_db():
        yield session

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = lambda: _token_context(ORG_A)
    return app


def client_for(app, *, permitted: bool = True, org_id: str = ORG_A):
    """A TestClient whose access control is stubbed to permit or deny.

    Denial is expressed as `USAGE_READ` because that is the permission this route
    gates on — asserting a `PLAN_APPROVE` denial here would test a permission the
    route does not check.
    """
    from unittest.mock import AsyncMock, MagicMock

    from fastapi.testclient import TestClient

    from src.admin.access_control import AccessControl
    from src.admin.config import AdminRole
    from src.admin.exceptions import AccessDeniedError
    from src.auth.dependencies import get_current_user
    from src.orchestration.routes import get_access_control

    access = MagicMock(spec=AccessControl)
    if permitted:
        access.check_permission = AsyncMock(return_value=True)
    else:
        access.check_permission = AsyncMock(
            side_effect=AccessDeniedError(
                message="Permission 'usage:read' is required for this operation",
                required_permission=Permission.USAGE_READ.value,
                user_role="member",
            )
        )
    access.get_user_role = AsyncMock(return_value=(AdminRole("org_admin"), org_id, None))

    app.dependency_overrides[get_access_control] = lambda: access
    app.dependency_overrides[get_current_user] = lambda: _token_context(org_id)
    return TestClient(app, raise_server_exceptions=False)


# -- seed helpers ------------------------------------------------------------


async def seed_flow(
    session: AsyncSession,
    *,
    slug: str,
    title: str | None = None,
    org_id: str = ORG_A,
    intent_ref: str | None = None,
    created_offset: int = 0,
    updated_offset: int | None = None,
    description: str | None = None,
    design_history: dict | None = None,
) -> OrchestrationFlow:
    """One flow with an explicit `created_at`, so ordering assertions are stable.

    `updated_offset=None` leaves `updated_at` NULL — the never-updated case the
    `sort=updated` COALESCE exists for, and the default because most flows are in
    it.

    `description` / `design_history` default to NULL (#4885), which is both the
    honest value for a flow whose design loop was never recorded and the state
    every other test in this file wants — so the design-capture fields cannot
    quietly acquire a default without the absence tests below failing.
    """
    flow = OrchestrationFlow(
        org_id=org_id,
        slug=slug,
        title=title if title is not None else f"Flow {slug}",
        intent_ref=intent_ref,
        description=description,
        design_history=design_history,
        created_at=BASE_TIME + timedelta(minutes=created_offset),
        updated_at=(BASE_TIME + timedelta(minutes=updated_offset)) if updated_offset is not None else None,
    )
    session.add(flow)
    await session.flush()
    return flow


async def seed_node(
    session: AsyncSession,
    flow: OrchestrationFlow,
    *,
    node_ref: str,
    state: str = NodeState.PENDING.value,
    epic: str = "epic-1",
    wave: str = "wave-1",
    kind: str = NodeKind.STORY.value,
    org_id: str = ORG_A,
    created_offset: int = 0,
) -> OrchestrationNode:
    """One node, with an explicit `created_at` — the wave rail's ordering key."""
    node = OrchestrationNode(
        org_id=org_id,
        flow_id=flow.id,
        epic_ref=epic,
        wave_ref=wave,
        node_ref=node_ref,
        kind=kind,
        state=state,
        title=f"Node {node_ref}",
        created_at=BASE_TIME + timedelta(minutes=created_offset),
    )
    session.add(node)
    await session.flush()
    return node


async def seed_decision(
    session: AsyncSession,
    flow: OrchestrationFlow,
    node: OrchestrationNode,
    *,
    kind: str,
    org_id: str = ORG_A,
    created_offset: int = 0,
) -> OrchestrationDecision:
    """One decision row, with an explicit `created_at` for latest-wins ordering."""
    decision = OrchestrationDecision(
        org_id=org_id,
        flow_id=flow.id,
        node_id=node.id,
        kind=kind,
        actor_id="engine",
        actor_role="service",
        actor_kind=ActorKind.SERVICE.value,
        created_at=BASE_TIME + timedelta(minutes=created_offset),
    )
    session.add(decision)
    await session.flush()
    return decision


async def seed_usage(
    session: AsyncSession,
    *,
    address: str,
    cost_usd: Decimal,
    org_id: str = ORG_A,
) -> None:
    """One ledger row at a graph address.

    `graph_address` is the join key the cost rollup groups by, and `agent_run_id`
    is the DynamoDB `event_id` (a uuid4) — never the KEDA pod name.
    """
    session.add(
        UsageLog(
            org_id=org_id,
            department_id="dept",
            team_id="team",
            user_id=USER_ID,
            account_type="service",
            model="anthropic.claude-3-5-sonnet",
            input_tokens=100,
            output_tokens=50,
            cost_usd=cost_usd,
            latency_ms=100,
            status_code=200,
            graph_address=address,
            agent_run_id="4f1d2a90-1111-4222-8333-444455556666",
        )
    )
    await session.flush()


def proposal_for(slug: str) -> LoopProposal:
    """The minimum document `_resolve_flow` and `_refuse_if_flow_is_live` read.

    Only `flow_slug` is consulted by either, but the real model is built so the
    tests exercise the object production passes rather than a stand-in.
    """
    return LoopProposal(
        flow_slug=slug,
        title="Delivery loop",
        org_id=ORG_A,
        spec_revision=SPEC_REVISION,
    )


def statement_recorder(session: AsyncSession) -> tuple[list[str], Callable[[], None]]:
    """Record every ORM-executed statement. Returns `(statements, stop)`.

    Counting statements is the only sound way to assert the absence of an N+1:
    timing is flaky, and a correlated subquery hides inside a single statement —
    which is why the tests below also assert on the SQL text.
    """
    statements: list[str] = []

    @event.listens_for(session.sync_session, "do_orm_execute")
    def _record(orm_execute_state):
        statements.append(str(orm_execute_state.statement))

    def stop() -> None:
        event.remove(session.sync_session, "do_orm_execute", _record)

    return statements, stop


class TestListFlowsGuardrail:
    """`repository.list_flows` must stay unbounded. The issue's blocking regression.

    Not a style preference: `compile._resolve_flow` and `registration` both scan
    the full list to resolve a flow by slug, and `orchestration_flows` has no
    `uq(org_id, slug)` to catch a duplicate if the scan misses. 30 flows throughout
    — more than one page — so a `limit` added to `list_flows` fails here rather
    than corrupting a tenant's graph in production.
    """

    async def test_list_flows_returns_all_thirty_flows_unbounded(self, session):
        for index in range(30):
            await seed_flow(session, slug=f"flow-{index:02d}", created_offset=index)

        flows = await OrchestrationRepository(session).list_flows(org_id=ORG_A)

        assert len(flows) == 30, "list_flows must be unbounded; a page-sized limit here creates duplicate flows"

    async def test_list_flows_signature_takes_only_org_id(self):
        """A defaulted `limit` is as dangerous as a required one — it just fails later.

        Asserted on the signature as well as on behaviour because the behavioural
        failure needs more than one page of flows to show up at all, which is
        exactly why the bug would ship.
        """
        parameters = inspect.signature(OrchestrationRepository.list_flows).parameters

        assert set(parameters) == {"self", "org_id"}, f"list_flows must take only org_id; got {list(parameters)}"

    async def test_compile_resolves_a_slug_older_than_one_page_without_duplicating(self, session):
        """`_resolve_flow` must find a slug buried 30 flows deep, not create a second.

        `list_flows` orders by `created_at DESC`, so the OLDEST flow is the one a
        limit would drop. That is the flow this test asks for.
        """
        repo = OrchestrationRepository(session)
        target = await seed_flow(session, slug="oldest-loop", created_offset=0)
        for index in range(1, 30):
            await seed_flow(session, slug=f"flow-{index:02d}", created_offset=index)

        resolved = await _resolve_flow(repo, proposal=proposal_for("oldest-loop"), org_id=ORG_A)

        assert resolved.id == target.id, "a slug older than one page was not found, so a duplicate flow was created under it"
        assert len([flow for flow in await repo.list_flows(org_id=ORG_A) if flow.slug == "oldest-loop"]) == 1

    async def test_registration_conflict_guard_still_fires_for_a_flow_older_than_one_page(self, session):
        """The in-force-plan conflict guard must not be disabled by pagination.

        A miss here means a second plan silently lands over one already accepted:
        the guard resolves no flow, concludes "nothing to conflict with", and
        permits the registration.
        """
        repo = OrchestrationRepository(session)
        target = await seed_flow(session, slug="oldest-loop", created_offset=0)
        for index in range(1, 30):
            await seed_flow(session, slug=f"flow-{index:02d}", created_offset=index)

        await repo.record_accepted_plan(
            org_id=ORG_A,
            flow_id=target.id,
            plan_document={"flow_slug": "oldest-loop"},
            plan_hash="hash-of-the-plan-already-in-force",
        )

        proposal = proposal_for("oldest-loop")
        with pytest.raises(DraftFlowConflictError):
            await _refuse_if_flow_is_live(session, proposal=proposal, org_id=ORG_A, document_hash=plan_hash(proposal))


class TestPaginationAndTotal:
    """`total` describes the filters, not the page."""

    async def test_total_counts_matches_across_all_pages_not_the_page_length(self, session):
        """30 flows, a filter matching 12, page size 5 → total == 12."""
        for index in range(30):
            # 12 of the 30 carry the searchable token.
            slug = f"needle-{index:02d}" if index < 12 else f"other-{index:02d}"
            await seed_flow(session, slug=slug, created_offset=index)

        page = await OrchestrationRepository(session).list_flows_page_with_aggregates(org_id=ORG_A, limit=5, q="needle")

        assert len(page.flows) == 5
        assert page.total == 12, "total must count every filtered match, not the returned page"

    async def test_unfiltered_total_is_the_whole_tenant(self, session):
        for index in range(30):
            await seed_flow(session, slug=f"flow-{index:02d}", created_offset=index)

        page = await OrchestrationRepository(session).list_flows_page_with_aggregates(org_id=ORG_A, limit=5)

        assert page.total == 30

    async def test_offset_walks_the_list_without_repeating_or_dropping_rows(self, session):
        """The `id` tiebreaker's purpose: every row appears on exactly one page.

        All 12 flows share a `created_at` here, so `created_at DESC` alone leaves
        their order undefined and rows can repeat across pages or vanish entirely.
        """
        for index in range(12):
            await seed_flow(session, slug=f"flow-{index:02d}", created_offset=0)

        repo = OrchestrationRepository(session)
        seen: list[str] = []
        for offset in (0, 4, 8):
            page = await repo.list_flows_page_with_aggregates(org_id=ORG_A, limit=4, offset=offset)
            seen.extend(aggregate.flow.id for aggregate in page.flows)

        assert len(seen) == 12
        assert len(set(seen)) == 12, "a row appeared on two pages — the id tiebreaker is missing"

    async def test_offset_past_the_end_is_an_empty_page(self, session):
        for index in range(3):
            await seed_flow(session, slug=f"flow-{index}", created_offset=index)

        page = await OrchestrationRepository(session).list_flows_page_with_aggregates(org_id=ORG_A, limit=25, offset=100)

        assert page.flows == ()
        # `COUNT(*) OVER ()` rides on the rows, and there are none to read it from.
        # Stated here rather than papered over: a client only reaches this by paging
        # past a total it already has.
        assert page.total == 0


class TestQueryCount:
    """No N+1, and no correlated subquery pretending not to be one."""

    async def test_statement_count_is_the_same_for_one_flow_and_for_twenty_five(self, session):
        """Constant in page size. Asserted by comparison, not against a literal.

        A literal ("must be 4") invites satisfying the number with a correlated
        scalar subquery in the SELECT list, which re-executes per row while the
        statement counter still sees one statement. Comparing two page sizes cannot
        be satisfied that way, and the SQL-text assertion below closes the rest of
        the gap.
        """
        repo = OrchestrationRepository(session)

        single = await seed_flow(session, slug="only-flow", created_offset=0)
        await seed_node(session, single, node_ref="n1", state=NodeState.RUNNING.value)

        statements, stop = statement_recorder(session)
        try:
            await repo.list_flows_page_with_aggregates(org_id=ORG_A, limit=25)
            await repo.count_flows_by_status(org_id=ORG_A)
        finally:
            stop()
        one_flow = len(statements)

        for index in range(1, 25):
            flow = await seed_flow(session, slug=f"flow-{index:02d}", created_offset=index)
            await seed_node(session, flow, node_ref="n1", state=NodeState.RUNNING.value)
            await seed_node(session, flow, node_ref="n2", state=NodeState.PASSED.value, wave="wave-2")

        statements, stop = statement_recorder(session)
        try:
            page = await repo.list_flows_page_with_aggregates(org_id=ORG_A, limit=25)
            await repo.count_flows_by_status(org_id=ORG_A)
        finally:
            stop()

        assert len(page.flows) == 25
        assert len(statements) == one_flow, f"query count grew with page size: {one_flow} → {len(statements)}"

    async def test_page_query_has_no_correlated_subquery_in_the_select_list(self, session):
        """A correlated scalar subquery re-executes per row — an N+1 in disguise.

        Detected by looking for a nested SELECT before the outer `FROM`. The grouped
        subqueries this design uses appear *after* it, in the JOIN clauses, so they
        do not trip this.
        """
        flow = await seed_flow(session, slug="one", created_offset=0)
        await seed_node(session, flow, node_ref="n1")

        statements, stop = statement_recorder(session)
        try:
            await OrchestrationRepository(session).list_flows_page_with_aggregates(org_id=ORG_A, limit=25)
        finally:
            stop()

        select_list = statements[0].upper().split("\nFROM", 1)[0]

        assert select_list.count("SELECT") == 1, f"correlated subquery in the SELECT list: {select_list}"

    async def test_wave_aggregate_is_one_grouped_query_for_the_whole_page(self, session):
        for index in range(10):
            flow = await seed_flow(session, slug=f"flow-{index}", created_offset=index)
            await seed_node(session, flow, node_ref="n1", wave="wave-1")
            await seed_node(session, flow, node_ref="n2", wave="wave-2")

        repo = OrchestrationRepository(session)
        flow_ids = [flow.id for flow in await repo.list_flows(org_id=ORG_A)]

        statements, stop = statement_recorder(session)
        try:
            waves = await repo.list_wave_aggregates(org_id=ORG_A, flow_ids=flow_ids)
        finally:
            stop()

        assert len(statements) == 1, "one GROUP BY for the page, not one query per flow"
        assert sum(len(value) for value in waves.values()) == 20

    async def test_wave_aggregate_issues_no_query_for_an_empty_page(self, session):
        statements, stop = statement_recorder(session)
        try:
            waves = await OrchestrationRepository(session).list_wave_aggregates(org_id=ORG_A, flow_ids=[])
        finally:
            stop()

        assert waves == {}
        assert statements == []


class TestWaveOrdering:
    """First-appearance order, at the scale where getting it wrong shows."""

    async def test_fourteen_waves_come_back_in_first_appearance_order(self, session):
        """`wave-10` must not precede `wave-2`.

        Waves are seeded 1..14 in creation order but their refs sort
        lexicographically as wave-1, wave-10, wave-11, ..., wave-2 — so a
        lexicographic ORDER BY produces a visibly different sequence and this test
        fails. Below 10 waves the two orders coincide, which is why the bug hides.
        """
        flow = await seed_flow(session, slug="fourteen", created_offset=0)
        for index in range(1, 15):
            await seed_node(session, flow, node_ref=f"n{index}", wave=f"wave-{index}", created_offset=index)

        waves = await OrchestrationRepository(session).list_wave_aggregates(org_id=ORG_A, flow_ids=[flow.id])

        assert [wave.wave_ref for wave in waves[flow.id]] == [f"wave-{index}" for index in range(1, 15)]

    async def test_non_numeric_wave_refs_are_ordered_too(self, session):
        """Nothing constrains `wave_ref` to `wave-<n>` — the address grammar permits
        `wave-as`. So the ordering cannot parse an integer out of the ref: this flow
        would raise, or fall back to an arbitrary order, if it did."""
        flow = await seed_flow(session, slug="lettered", created_offset=0)
        for offset, wave in enumerate(("wave-as", "wave-bs", "wave-cs"), start=1):
            await seed_node(session, flow, node_ref=f"n{offset}", wave=wave, created_offset=offset)

        waves = await OrchestrationRepository(session).list_wave_aggregates(org_id=ORG_A, flow_ids=[flow.id])

        assert [wave.wave_ref for wave in waves[flow.id]] == ["wave-as", "wave-bs", "wave-cs"]

    async def test_current_wave_ref_is_the_first_unfinished_wave_in_rail_order(self, session):
        """Parity between `current_wave_ref` and the rail the client renders.

        Waves 1–2 are done, 3 is running, 4+ are queued, and wave-10 exists so a
        lexicographic order would pick visibly the wrong one.
        """
        flow = await seed_flow(session, slug="progressing", created_offset=0)
        for index in range(1, 11):
            if index <= 2:
                state = NodeState.PASSED.value
            elif index == 3:
                state = NodeState.RUNNING.value
            else:
                state = NodeState.PENDING.value
            await seed_node(session, flow, node_ref=f"n{index}", wave=f"wave-{index}", state=state, created_offset=index)

        page = await OrchestrationRepository(session).list_flows_page_with_aggregates(org_id=ORG_A)
        aggregate = page.flows[0]

        assert aggregate.current_wave_ref == "wave-3"
        first_unfinished = next(wave.wave_ref for wave in aggregate.waves if not wave.finished)
        assert aggregate.current_wave_ref == first_unfinished

    async def test_a_fully_complete_flow_has_no_current_wave(self, session):
        """Naming the last wave would read as "this is where the work is"."""
        flow = await seed_flow(session, slug="done", created_offset=0)
        await seed_node(session, flow, node_ref="n1", state=NodeState.PASSED.value)

        page = await OrchestrationRepository(session).list_flows_page_with_aggregates(org_id=ORG_A)

        assert page.flows[0].current_wave_ref is None
        assert page.flows[0].status is FlowStatus.COMPLETE

    async def test_wave_and_epic_counts_come_from_the_grouping(self, session):
        flow = await seed_flow(session, slug="two-epics", created_offset=0)
        await seed_node(session, flow, node_ref="n1", epic="epic-1", wave="wave-1", created_offset=1)
        await seed_node(session, flow, node_ref="n2", epic="epic-1", wave="wave-2", created_offset=2)
        await seed_node(session, flow, node_ref="n3", epic="epic-2", wave="wave-1", created_offset=3)

        page = await OrchestrationRepository(session).list_flows_page_with_aggregates(org_id=ORG_A)

        assert len(page.flows[0].waves) == 3, "waves group by (epic_ref, wave_ref), so two epics' wave-1 are two waves"
        assert page.flows[0].epic_count == 2


class TestSorting:
    async def test_default_sort_is_newest_created_first(self, session):
        await seed_flow(session, slug="oldest", created_offset=0)
        await seed_flow(session, slug="newest", created_offset=10)

        page = await OrchestrationRepository(session).list_flows_page_with_aggregates(org_id=ORG_A)

        assert [aggregate.flow.slug for aggregate in page.flows] == ["newest", "oldest"]

    async def test_sort_updated_does_not_lead_with_a_never_updated_flow(self, session):
        """The NULL-`updated_at` trap, and it is dialect-dependent.

        Raw NULLs sort FIRST under PostgreSQL `DESC` and LAST under SQLite, so
        sorting the raw column passes on SQLite and misorders in production. Here
        the never-updated flow is the newer by `created_at` while the other's
        `updated_at` is later still — so only a COALESCE produces this order.
        """
        await seed_flow(session, slug="never-updated", created_offset=10)
        await seed_flow(session, slug="recently-updated", created_offset=0, updated_offset=20)

        page = await OrchestrationRepository(session).list_flows_page_with_aggregates(org_id=ORG_A, sort="updated")

        assert [aggregate.flow.slug for aggregate in page.flows] == ["recently-updated", "never-updated"]

    async def test_sort_stalled_ranks_the_most_stalled_flow_first(self, session):
        quiet = await seed_flow(session, slug="quiet", created_offset=10)
        await seed_node(session, quiet, node_ref="n1", state=NodeState.RUNNING.value)

        stalled = await seed_flow(session, slug="stalled-two", created_offset=0)
        for index in (1, 2):
            node = await seed_node(session, stalled, node_ref=f"n{index}", state=NodeState.FAILED.value)
            await seed_decision(session, stalled, node, kind=DecisionKind.NODE_STALLED.value)

        page = await OrchestrationRepository(session).list_flows_page_with_aggregates(org_id=ORG_A, sort="stalled")

        # `quiet` is the newer flow, so it leads under the default sort; asking for
        # `stalled` has to override that rather than merely tie-break within it.
        assert [aggregate.flow.slug for aggregate in page.flows] == ["stalled-two", "quiet"]
        assert page.flows[0].stalled_count == 2

    async def test_an_unknown_sort_is_rejected_rather_than_silently_ignored(self, session):
        """Silently falling back would rank by something the caller did not ask for.

        `cost` is the realistic mistake — cut from v1 because cost lives in
        `usage_logs` and cannot participate in the paginated statement.
        """
        with pytest.raises(ValueError, match="unknown sort"):
            await OrchestrationRepository(session).list_flows_page_with_aggregates(org_id=ORG_A, sort="cost")


class TestDisplayCountsAndStatus:
    async def test_superseded_nodes_are_in_no_bucket_and_not_in_total_nodes(self, session):
        """Counting a superseded attempt makes one piece of work appear twice."""
        flow = await seed_flow(session, slug="amended", created_offset=0)
        await seed_node(session, flow, node_ref="n1", state=NodeState.SUPERSEDED.value)
        await seed_node(session, flow, node_ref="n2", state=NodeState.RUNNING.value)

        page = await OrchestrationRepository(session).list_flows_page_with_aggregates(org_id=ORG_A)
        counts = page.flows[0].display_counts

        assert counts.total == 1
        assert counts.in_progress == 1
        assert (counts.queued, counts.gate, counts.stalled, counts.complete) == (0, 0, 0, 0)

    def test_superseded_is_absent_from_every_derived_filter_list(self):
        """Structural exclusion: it is in no bucket, so no predicate must mention it.

        Asserted on the derived mapping rather than on a query, because the point is
        that there is no `!= 'superseded'` clause to keep working — the state simply
        appears nowhere, and absence cannot be dropped by a later edit.
        """
        for states in DISPLAY_TO_ENGINE.values():
            assert NodeState.SUPERSEDED.value not in states

    async def test_a_zero_node_flow_appears_as_empty(self, session):
        """Proves LEFT, not inner, join.

        An inner join drops this row entirely — and a flow that compiled to an empty
        graph is exactly the thing an operator needs to be able to see.
        """
        await seed_flow(session, slug="no-nodes", created_offset=0)

        page = await OrchestrationRepository(session).list_flows_page_with_aggregates(org_id=ORG_A)

        assert len(page.flows) == 1
        assert page.flows[0].status is FlowStatus.EMPTY
        assert page.flows[0].display_counts.total == 0

    async def test_a_flow_whose_every_node_is_superseded_is_also_empty(self, session):
        """Same news to an operator: there is nothing live here."""
        flow = await seed_flow(session, slug="all-superseded", created_offset=0)
        await seed_node(session, flow, node_ref="n1", state=NodeState.SUPERSEDED.value)

        page = await OrchestrationRepository(session).list_flows_page_with_aggregates(org_id=ORG_A)

        assert page.flows[0].status is FlowStatus.EMPTY

    @pytest.mark.parametrize(
        ("counts", "expected"),
        [
            ({"stalled": 1, "gate": 1, "in_progress": 1, "queued": 1, "complete": 1}, FlowStatus.ATTENTION_NEEDED),
            ({"gate": 1, "in_progress": 1, "queued": 1, "complete": 1}, FlowStatus.AWAITING_YOU),
            ({"in_progress": 1, "queued": 1, "complete": 1}, FlowStatus.RUNNING),
            ({"queued": 1, "complete": 1}, FlowStatus.QUEUED),
            ({"complete": 2}, FlowStatus.COMPLETE),
            ({}, FlowStatus.EMPTY),
        ],
    )
    def test_status_precedence_is_worst_news_first(self, counts, expected):
        """A stall behind running work must not be reported as `running`."""
        defaults = {"queued": 0, "in_progress": 0, "gate": 0, "stalled": 0, "complete": 0}

        assert derive_flow_status(**{**defaults, **counts}) is expected

    async def test_a_stalled_and_gated_flow_reports_attention_needed_but_needs_me_either_way(self, session):
        """`needs_me` is not an alias of `status in (attention_needed, awaiting_you)`.

        This flow has both a stall and an open gate. `status` is first-match-wins so
        it reports only `attention_needed`, while `needs_me` must be true on either
        ground — and the gate count must still be surfaced so the card can say
        "1 waiting on you".
        """
        flow = await seed_flow(session, slug="both", created_offset=0)
        failed = await seed_node(session, flow, node_ref="n1", state=NodeState.FAILED.value)
        await seed_decision(session, flow, failed, kind=DecisionKind.NODE_STALLED.value)
        await seed_node(session, flow, node_ref="n2", state=NodeState.AWAITING_GATE.value)

        page = await OrchestrationRepository(session).list_flows_page_with_aggregates(org_id=ORG_A)
        aggregate = page.flows[0]

        assert aggregate.status is FlowStatus.ATTENTION_NEEDED
        assert aggregate.needs_me is True
        assert aggregate.awaiting_gate_count == 1
        assert aggregate.stalled_count == 1

    async def test_status_filter_respects_precedence(self, session):
        """Filtering for `running` must not return a flow that reports as stalled.

        Otherwise a flow appears under a filter for a status it does not have — the
        SQL predicate and the Python derivation disagreeing.
        """
        stalled = await seed_flow(session, slug="stalled-and-running", created_offset=0)
        node = await seed_node(session, stalled, node_ref="n1", state=NodeState.FAILED.value)
        await seed_decision(session, stalled, node, kind=DecisionKind.NODE_STALLED.value)
        await seed_node(session, stalled, node_ref="n2", state=NodeState.RUNNING.value)

        plain = await seed_flow(session, slug="just-running", created_offset=1)
        await seed_node(session, plain, node_ref="n1", state=NodeState.RUNNING.value)

        repo = OrchestrationRepository(session)
        running = await repo.list_flows_page_with_aggregates(org_id=ORG_A, status=FlowStatus.RUNNING)
        attention = await repo.list_flows_page_with_aggregates(org_id=ORG_A, status=FlowStatus.ATTENTION_NEEDED)

        assert [aggregate.flow.slug for aggregate in running.flows] == ["just-running"]
        assert [aggregate.flow.slug for aggregate in attention.flows] == ["stalled-and-running"]

    async def test_every_status_filter_agrees_with_the_derived_status(self, session):
        """Exhaustive: for each of the six statuses, the filter returns exactly the
        rows that report it.

        This is what keeps `_status_predicate` and `derive_flow_status` from
        drifting apart — one is SQL, the other Python, and nothing but this test
        makes them the same function.
        """
        await seed_flow(session, slug="s-empty", created_offset=0)
        flow_complete = await seed_flow(session, slug="s-complete", created_offset=1)
        await seed_node(session, flow_complete, node_ref="n1", state=NodeState.PASSED.value)
        flow_queued = await seed_flow(session, slug="s-queued", created_offset=2)
        await seed_node(session, flow_queued, node_ref="n1", state=NodeState.PENDING.value)
        flow_running = await seed_flow(session, slug="s-running", created_offset=3)
        await seed_node(session, flow_running, node_ref="n1", state=NodeState.RUNNING.value)
        flow_gate = await seed_flow(session, slug="s-gate", created_offset=4)
        await seed_node(session, flow_gate, node_ref="n1", state=NodeState.AWAITING_GATE.value)
        flow_stalled = await seed_flow(session, slug="s-stalled", created_offset=5)
        node = await seed_node(session, flow_stalled, node_ref="n1", state=NodeState.FAILED.value)
        await seed_decision(session, flow_stalled, node, kind=DecisionKind.NODE_STALLED.value)

        repo = OrchestrationRepository(session)
        unfiltered = await repo.list_flows_page_with_aggregates(org_id=ORG_A, limit=100)
        expected = {status: sorted(a.flow.slug for a in unfiltered.flows if a.status is status) for status in FlowStatus}
        # Guard the guard: if the seed above stopped covering all six statuses, this
        # test would keep passing while asserting almost nothing.
        assert all(expected[status] for status in FlowStatus), f"seed no longer covers every status: {expected}"

        for status in FlowStatus:
            page = await repo.list_flows_page_with_aggregates(org_id=ORG_A, status=status, limit=100)
            assert sorted(a.flow.slug for a in page.flows) == expected[status], f"filter for {status.value} disagrees with the rows"
            assert page.total == len(expected[status])


class TestCurrentAttentionCount:
    """Every current failure or hold has one consistent display count."""

    async def test_a_failed_node_with_a_stall_decision_counts_as_stalled(self, session):
        flow = await seed_flow(session, slug="stalled", created_offset=0)
        node = await seed_node(session, flow, node_ref="n1", state=NodeState.FAILED.value)
        await seed_decision(session, flow, node, kind=DecisionKind.NODE_STALLED.value)

        page = await OrchestrationRepository(session).list_flows_page_with_aggregates(org_id=ORG_A)

        assert page.flows[0].stalled_count == 1
        assert page.flows[0].status is FlowStatus.ATTENTION_NEEDED

    async def test_a_plain_failure_counts_in_the_same_attention_bucket(self, session):
        """The summary counts all work needing help; node badges retain the reason."""
        flow = await seed_flow(session, slug="just-failed", created_offset=0)
        await seed_node(session, flow, node_ref="n1", state=NodeState.FAILED.value)

        page = await OrchestrationRepository(session).list_flows_page_with_aggregates(org_id=ORG_A)

        assert page.flows[0].stalled_count == page.flows[0].display_counts.stalled == 1

    async def test_a_halted_node_still_needs_attention_after_a_prior_stall(self, session):
        """A current halt needs attention regardless of the older stall record."""
        flow = await seed_flow(session, slug="halted-eventually", created_offset=0)
        node = await seed_node(session, flow, node_ref="n1", state=NodeState.HALTED.value)
        await seed_decision(session, flow, node, kind=DecisionKind.NODE_STALLED.value, created_offset=1)
        await seed_decision(session, flow, node, kind=DecisionKind.NODE_HALTED.value, created_offset=2)

        page = await OrchestrationRepository(session).list_flows_page_with_aggregates(org_id=ORG_A)

        assert page.flows[0].stalled_count == 1

    async def test_halted_then_stalled_again_does_read_as_stalled(self, session):
        """The other direction of latest-wins, so the test above cannot be passed by
        ignoring stall decisions altogether."""
        flow = await seed_flow(session, slug="stalled-again", created_offset=0)
        node = await seed_node(session, flow, node_ref="n1", state=NodeState.FAILED.value)
        await seed_decision(session, flow, node, kind=DecisionKind.NODE_HALTED.value, created_offset=1)
        await seed_decision(session, flow, node, kind=DecisionKind.NODE_STALLED.value, created_offset=2)

        page = await OrchestrationRepository(session).list_flows_page_with_aggregates(org_id=ORG_A)

        assert page.flows[0].stalled_count == 1

    async def test_decisions_of_other_kinds_never_count_as_stalls(self, session):
        """A gate approval is not a stall signal."""
        flow = await seed_flow(session, slug="approved", created_offset=0)
        node = await seed_node(session, flow, node_ref="n1", state=NodeState.PASSED.value)
        await seed_decision(session, flow, node, kind=DecisionKind.GATE_APPROVED.value)

        page = await OrchestrationRepository(session).list_flows_page_with_aggregates(org_id=ORG_A)

        assert page.flows[0].stalled_count == 0

    async def test_a_later_decision_of_an_unrelated_kind_does_not_clear_a_stall(self, session):
        """Latest-wins is over the two node-outcome kinds only.

        A subsequent gate approval must not be read as "the stall was resolved" —
        the node is still wedged, and a cleared count would hide it from the one
        person who can unwedge it.
        """
        flow = await seed_flow(session, slug="still-stalled", created_offset=0)
        node = await seed_node(session, flow, node_ref="n1", state=NodeState.FAILED.value)
        await seed_decision(session, flow, node, kind=DecisionKind.NODE_STALLED.value, created_offset=1)
        await seed_decision(session, flow, node, kind=DecisionKind.GATE_APPROVED.value, created_offset=2)

        page = await OrchestrationRepository(session).list_flows_page_with_aggregates(org_id=ORG_A)

        assert page.flows[0].stalled_count == 1

    async def test_stalls_count_nodes_not_decision_rows(self, session):
        """A node that stalled three times is one stalled node.

        The figure drives "2 nodes need you", so counting rows would overstate how
        much work is actually waiting on a human.
        """
        flow = await seed_flow(session, slug="repeat-staller", created_offset=0)
        first = await seed_node(session, flow, node_ref="n1", state=NodeState.FAILED.value)
        second = await seed_node(session, flow, node_ref="n2", state=NodeState.FAILED.value)
        for offset in (1, 2, 3):
            await seed_decision(session, flow, first, kind=DecisionKind.NODE_STALLED.value, created_offset=offset)
        await seed_decision(session, flow, second, kind=DecisionKind.NODE_STALLED.value, created_offset=4)

        page = await OrchestrationRepository(session).list_flows_page_with_aggregates(org_id=ORG_A)

        assert page.flows[0].stalled_count == 2

    async def test_a_flow_level_decision_with_no_node_is_never_counted(self, session):
        """`node_id` is NULL for flow-level decisions such as accepting a plan.

        Counting one would attribute a stall to no node at all, and there would be
        nothing for an operator to go and look at.
        """
        flow = await seed_flow(session, slug="plan-accepted", created_offset=0)
        await seed_node(session, flow, node_ref="n1", state=NodeState.RUNNING.value)
        session.add(
            OrchestrationDecision(
                org_id=ORG_A,
                flow_id=flow.id,
                node_id=None,
                kind=DecisionKind.NODE_STALLED.value,
                actor_id="engine",
                actor_role="service",
                actor_kind=ActorKind.SERVICE.value,
                created_at=BASE_TIME,
            )
        )
        await session.flush()

        page = await OrchestrationRepository(session).list_flows_page_with_aggregates(org_id=ORG_A)

        assert page.flows[0].stalled_count == 0
        assert page.flows[0].status is FlowStatus.RUNNING


class TestNeedsMeFilter:
    async def test_needs_me_matches_a_gate_and_a_stall_but_not_quiet_work(self, session):
        gated = await seed_flow(session, slug="gated", created_offset=0)
        await seed_node(session, gated, node_ref="n1", state=NodeState.AWAITING_GATE.value)

        stalled = await seed_flow(session, slug="stalled", created_offset=1)
        node = await seed_node(session, stalled, node_ref="n1", state=NodeState.FAILED.value)
        await seed_decision(session, stalled, node, kind=DecisionKind.NODE_STALLED.value)

        running = await seed_flow(session, slug="running", created_offset=2)
        await seed_node(session, running, node_ref="n1", state=NodeState.RUNNING.value)

        page = await OrchestrationRepository(session).list_flows_page_with_aggregates(org_id=ORG_A, needs_me=True)

        assert sorted(aggregate.flow.slug for aggregate in page.flows) == ["gated", "stalled"]
        assert page.total == 2

    async def test_needs_me_surfaces_both_grounds_on_one_row(self, session):
        """One row, one status, two grounds for needing a human.

        A `needs_me` implemented as a status alias returns the same row here, so the
        assertion is on the counts — both grounds have to stay visible for the card
        to say what is actually wanted.
        """
        flow = await seed_flow(session, slug="both", created_offset=0)
        node = await seed_node(session, flow, node_ref="n1", state=NodeState.FAILED.value)
        await seed_decision(session, flow, node, kind=DecisionKind.NODE_STALLED.value)
        await seed_node(session, flow, node_ref="n2", state=NodeState.AWAITING_GATE.value)

        page = await OrchestrationRepository(session).list_flows_page_with_aggregates(org_id=ORG_A, needs_me=True)

        assert len(page.flows) == 1
        assert page.flows[0].awaiting_gate_count == 1
        assert page.flows[0].stalled_count == 1


class TestSearch:
    async def test_a_mid_string_token_matches_both_title_and_slug(self, session):
        """The mockup's own example: searching `4645` must find both fields.

        Prefix-only matching fails this, which is why the search is a substring.
        """
        await seed_flow(session, slug="aidlc-delivery-loop-4645", title="Delivery loop for #4645", created_offset=0)
        await seed_flow(session, slug="unrelated", title="Something else", created_offset=1)

        page = await OrchestrationRepository(session).list_flows_page_with_aggregates(org_id=ORG_A, q="4645")

        assert [aggregate.flow.slug for aggregate in page.flows] == ["aidlc-delivery-loop-4645"]

    async def test_search_is_case_insensitive(self, session):
        await seed_flow(session, slug="loop", title="Delivery Loop", created_offset=0)

        page = await OrchestrationRepository(session).list_flows_page_with_aggregates(org_id=ORG_A, q="DELIVERY")

        assert len(page.flows) == 1

    async def test_search_matches_intent_ref(self, session):
        """An operator arriving from an issue searches the issue number."""
        await seed_flow(session, slug="loop", title="Delivery loop", intent_ref="4120", created_offset=0)
        await seed_flow(session, slug="other", title="Other", intent_ref="9999", created_offset=1)

        page = await OrchestrationRepository(session).list_flows_page_with_aggregates(org_id=ORG_A, q="4120")

        assert [aggregate.flow.slug for aggregate in page.flows] == ["loop"]

    async def test_a_flow_with_no_intent_ref_is_not_dropped_by_a_search(self, session):
        """`intent_ref` is nullable, and NULL is not false in SQL.

        A NULL in one disjunct of the `or_()` must not suppress a title match — a
        hand-run flow has no intent issue and would silently become unsearchable.
        """
        await seed_flow(session, slug="hand-run", title="Delivery loop", intent_ref=None, created_offset=0)

        page = await OrchestrationRepository(session).list_flows_page_with_aggregates(org_id=ORG_A, q="delivery")

        assert [aggregate.flow.slug for aggregate in page.flows] == ["hand-run"]

    async def test_a_percent_in_the_search_box_does_not_match_everything(self, session):
        """Unescaped, `%` turns any search into "select all" — a filter that lies."""
        await seed_flow(session, slug="loop-a", created_offset=0)
        await seed_flow(session, slug="loop-b", created_offset=1)

        page = await OrchestrationRepository(session).list_flows_page_with_aggregates(org_id=ORG_A, q="%")

        assert page.flows == ()
        assert page.total == 0

    async def test_an_underscore_in_the_search_box_is_a_literal_underscore(self, session):
        """`_` is a single-char wildcard; unescaped, `loop_a` also matches `loop-a`."""
        await seed_flow(session, slug="loop_a", created_offset=0)
        await seed_flow(session, slug="loop-a", created_offset=1)

        page = await OrchestrationRepository(session).list_flows_page_with_aggregates(org_id=ORG_A, q="loop_a")

        assert [aggregate.flow.slug for aggregate in page.flows] == ["loop_a"]


class TestStatusChips:
    """Unfiltered, tenant-wide, and always in agreement with the rows."""

    async def test_chip_counts_equal_the_number_of_rows_reporting_each_status(self, session):
        flow_gate = await seed_flow(session, slug="gated", created_offset=0)
        await seed_node(session, flow_gate, node_ref="n1", state=NodeState.AWAITING_GATE.value)
        flow_running = await seed_flow(session, slug="running", created_offset=1)
        await seed_node(session, flow_running, node_ref="n1", state=NodeState.RUNNING.value)
        flow_done = await seed_flow(session, slug="done", created_offset=2)
        await seed_node(session, flow_done, node_ref="n1", state=NodeState.PASSED.value)
        await seed_flow(session, slug="empty", created_offset=3)

        repo = OrchestrationRepository(session)
        chips = await repo.count_flows_by_status(org_id=ORG_A)
        rows = await repo.list_flows_page_with_aggregates(org_id=ORG_A, limit=100)

        for status in FlowStatus:
            expected = sum(1 for aggregate in rows.flows if aggregate.status is status)
            assert chips[status] == expected, f"chip for {status.value} disagrees with the rows"

    async def test_chips_include_zeroes_for_statuses_nothing_is_in(self, session):
        """A chip reading 0 is information; omitting it makes the chip row's shape
        jump around as work moves."""
        flow = await seed_flow(session, slug="running", created_offset=0)
        await seed_node(session, flow, node_ref="n1", state=NodeState.RUNNING.value)

        chips = await OrchestrationRepository(session).count_flows_by_status(org_id=ORG_A)

        assert set(chips) == set(FlowStatus)
        assert chips[FlowStatus.QUEUED] == 0

    async def test_chips_ignore_the_active_filters(self, session):
        """The chips describe the population being chosen among, so with a filter on
        they still total the whole tenant — that is what makes "Showing 1 of 5"
        readable, and what stops every unselected chip reading 0."""
        gated = await seed_flow(session, slug="gated", created_offset=0)
        await seed_node(session, gated, node_ref="n1", state=NodeState.AWAITING_GATE.value)
        for index in range(4):
            flow = await seed_flow(session, slug=f"running-{index}", created_offset=index + 1)
            await seed_node(session, flow, node_ref="n1", state=NodeState.RUNNING.value)

        repo = OrchestrationRepository(session)
        filtered = await repo.list_flows_page_with_aggregates(org_id=ORG_A, needs_me=True)
        chips = await repo.count_flows_by_status(org_id=ORG_A)

        assert filtered.total == 1
        assert sum(chips.values()) == 5, "chips must describe the whole tenant, not the filtered set"

    async def test_chips_are_scoped_to_the_tenant(self, session):
        await seed_flow(session, slug="mine", org_id=ORG_A, created_offset=0)
        await seed_flow(session, slug="theirs", org_id=ORG_B, created_offset=1)

        chips = await OrchestrationRepository(session).count_flows_by_status(org_id=ORG_A)

        assert sum(chips.values()) == 1


class TestDeliveryCost:
    """Cost per flow, three-valued, one ledger query for the page."""

    async def test_a_flows_spend_is_summed_from_its_address_prefix(self, session, app_with_router):
        flow = await seed_flow(session, slug="delivery-loop", created_offset=0)
        await seed_node(session, flow, node_ref="n1")
        await seed_usage(session, address="delivery-loop/epic-1/wave-1/n1", cost_usd=Decimal("1.50"))
        await seed_usage(session, address="delivery-loop/epic-1/wave-1/n2", cost_usd=Decimal("0.37"))

        cost = client_for(app_with_router).get(ROUTE).json()["flows"][0]["delivery_cost"]

        assert cost["status"] == "known"
        assert Decimal(cost["amount_usd"]) == Decimal("1.87")

    async def test_a_flow_with_no_ledger_rows_is_unknown_and_carries_no_amount(self, session, app_with_router):
        """`unknown` must never render `$0.00`.

        Absence is not a measured zero: a flow that has not started must not read
        like one that ran for free.
        """
        flow = await seed_flow(session, slug="untouched", created_offset=0)
        await seed_node(session, flow, node_ref="n1")

        cost = client_for(app_with_router).get(ROUTE).json()["flows"][0]["delivery_cost"]

        assert cost["status"] == "unknown"
        assert cost["amount_usd"] is None
        assert cost["reason"] == "no_usage_rows"

    async def test_a_measured_zero_is_none_incurred_not_unknown(self, session, app_with_router):
        """Rows exist and total zero: that is a verified zero, a different fact."""
        flow = await seed_flow(session, slug="free-loop", created_offset=0)
        await seed_node(session, flow, node_ref="n1")
        await seed_usage(session, address="free-loop/epic-1/wave-1/n1", cost_usd=Decimal("0"))

        cost = client_for(app_with_router).get(ROUTE).json()["flows"][0]["delivery_cost"]

        assert cost["status"] == "none_incurred"
        assert Decimal(cost["amount_usd"]) == Decimal("0")

    async def test_every_cost_figure_carries_its_scope_caption(self, session, app_with_router):
        """These totals exclude build and infrastructure spend, so the figure must
        never travel without saying so."""
        flow = await seed_flow(session, slug="loop", created_offset=0)
        await seed_node(session, flow, node_ref="n1")

        cost = client_for(app_with_router).get(ROUTE).json()["flows"][0]["delivery_cost"]

        assert cost["scope"] == COST_SCOPE_LABEL

    async def test_a_slug_containing_an_underscore_does_not_absorb_a_similar_flows_spend(self, session, app_with_router):
        """The LIKE-escape test.

        `_` is a legal slug character AND a single-char wildcard, so unescaped,
        `loop_4645` also matches `loop-4645/...` and the two flows' spend merges
        into one figure with nothing on the card indicating it.
        """
        await seed_flow(session, slug="loop-4645", created_offset=0)
        await seed_flow(session, slug="loop_4645", created_offset=1)
        await seed_usage(session, address="loop_4645/epic-1/wave-1/n1", cost_usd=Decimal("1.00"))
        await seed_usage(session, address="loop-4645/epic-1/wave-1/n1", cost_usd=Decimal("50.00"))

        body = client_for(app_with_router).get(ROUTE).json()
        by_slug = {flow["slug"]: flow["delivery_cost"] for flow in body["flows"]}

        assert Decimal(by_slug["loop_4645"]["amount_usd"]) == Decimal("1.00")
        assert Decimal(by_slug["loop-4645"]["amount_usd"]) == Decimal("50.00")

    async def test_a_sibling_slug_sharing_a_prefix_does_not_leak_spend(self, session, app_with_router):
        """The trailing `/` boundary: `loop-4645` must not absorb `loop-46450`."""
        await seed_flow(session, slug="loop-4645", created_offset=0)
        await seed_flow(session, slug="loop-46450", created_offset=1)
        await seed_usage(session, address="loop-46450/epic-1/wave-1/n1", cost_usd=Decimal("99.00"))

        body = client_for(app_with_router).get(ROUTE).json()
        by_slug = {flow["slug"]: flow["delivery_cost"] for flow in body["flows"]}

        assert by_slug["loop-4645"]["status"] == "unknown"
        assert Decimal(by_slug["loop-46450"]["amount_usd"]) == Decimal("99.00")

    async def test_the_whole_page_costs_one_ledger_query(self, session):
        """One grouped query for every flow on the page, not one per flow — the N+1
        the address-keyed cost model exists to avoid."""
        for index in range(10):
            await seed_flow(session, slug=f"loop-{index}", created_offset=index)
            await seed_usage(session, address=f"loop-{index}/epic-1/wave-1/n1", cost_usd=Decimal("1.00"))

        prefixes = [f"loop-{index}" for index in range(10)]
        statements, stop = statement_recorder(session)
        try:
            costs = await get_cost_by_address_prefixes(session, org_id=ORG_A, address_prefixes=prefixes)
        finally:
            stop()

        assert len(statements) == 1
        assert len(costs) == 10

    async def test_no_prefixes_issues_no_query(self, session):
        statements, stop = statement_recorder(session)
        try:
            costs = await get_cost_by_address_prefixes(session, org_id=ORG_A, address_prefixes=[])
        finally:
            stop()

        assert costs == []
        assert statements == []

    async def test_another_orgs_ledger_rows_are_never_counted(self, session):
        """Two tenants may run identically-named flows, so an address prefix alone
        does not identify whose spend it is."""
        await seed_usage(session, address="shared-slug/epic-1/wave-1/n1", cost_usd=Decimal("1.00"), org_id=ORG_A)
        await seed_usage(session, address="shared-slug/epic-1/wave-1/n1", cost_usd=Decimal("99.00"), org_id=ORG_B)

        costs = await get_cost_by_address_prefixes(session, org_id=ORG_A, address_prefixes=["shared-slug"])

        assert sum(cost.amount_usd for cost in costs) == Decimal("1.00")


class TestDesignCaptureInTheResponse:
    """The design story reaches the card — and costs no extra query (#4885).

    Both fields live on `orchestration_flows`, so they ride the row the page query
    already fetches. That is the whole reason the issue could add them without a
    join: if either had needed its own read, the constant-query-count guarantee
    above would have had to be renegotiated.

    `null` must survive the serialiser as `null`. The card renders no stage strip
    at all for an unknown history, and it can only make that distinction if the
    absence is transmitted rather than defaulted to `{}` on the way out.
    """

    HISTORY = {
        "scope": "poc",
        "stages": [
            {"name": "intent-capture", "state": "approved", "approved_at": "2026-09-01T12:05:00Z"},
            {"name": "reverse-engineering", "state": "skipped"},
            {"name": "requirements-analysis", "state": "approved", "approved_at": "2026-09-01T12:30:00Z"},
            {"name": "delivery-planning", "state": "open"},
            {"name": "loop-proposal", "state": "not_reached"},
        ],
    }

    async def test_both_fields_are_returned_when_present(self, session, app_with_router):
        await seed_flow(
            session,
            slug="captured-loop",
            created_offset=0,
            description="Delivery plans showed what they were doing but not what they were for.",
            design_history=self.HISTORY,
        )

        response = client_for(app_with_router).get(ROUTE)

        assert response.status_code == 200
        summary = response.json()["flows"][0]
        assert summary["description"] == "Delivery plans showed what they were doing but not what they were for."
        assert summary["design_history"]["scope"] == "poc"

    async def test_skipped_and_not_reached_stay_distinct_through_the_response(self, session, app_with_router):
        """The card strikes through one and greys the other; merging them misreads.

        A skipped stage rendered as pending shows an operator work that is never
        coming, on a flow that is progressing exactly as intended.
        """
        await seed_flow(session, slug="poc-loop", created_offset=0, design_history=self.HISTORY)

        response = client_for(app_with_router).get(ROUTE)

        states = {stage["name"]: stage["state"] for stage in response.json()["flows"][0]["design_history"]["stages"]}
        assert states["reverse-engineering"] == "skipped"
        assert states["loop-proposal"] == "not_reached"
        assert states["delivery-planning"] == "open"

    async def test_an_uncaptured_flow_returns_null_for_both_not_an_empty_object(self, session, app_with_router):
        """Every flow registered before #4885 is in this state, so it is the common case.

        `null` and not `{}`: the frontend renders nothing at all for an unknown
        history, and an empty object would instead render an empty stage strip —
        five pending gates for a design loop that may have fully completed. The
        keys must still be *present* and null, because `FlowSummaryResponse` is
        `extra="forbid"` and the client types both as nullable.
        """
        await seed_flow(session, slug="historic-loop", created_offset=0)

        response = client_for(app_with_router).get(ROUTE)

        summary = response.json()["flows"][0]
        assert "description" in summary and "design_history" in summary
        assert summary["description"] is None
        assert summary["design_history"] is None

    async def test_the_fields_add_no_query_to_the_page(self, session, app_with_router):
        """They ride the flow row, so the statement count is unchanged from #4869.

        Compared against an identical page with both fields NULL rather than against
        a literal: a literal would need updating whenever the page's query plan
        legitimately changes, and would stop testing this.
        """
        repo = OrchestrationRepository(session)
        for index in range(5):
            await seed_flow(session, slug=f"bare-{index}", created_offset=index)

        statements, stop = statement_recorder(session)
        try:
            await repo.list_flows_page_with_aggregates(org_id=ORG_A, limit=25)
        finally:
            stop()
        without_capture = len(statements)

        for index in range(5, 10):
            await seed_flow(
                session,
                slug=f"captured-{index}",
                created_offset=index,
                description="A one-line use case.",
                design_history=self.HISTORY,
            )

        statements, stop = statement_recorder(session)
        try:
            page = await repo.list_flows_page_with_aggregates(org_id=ORG_A, limit=25)
        finally:
            stop()

        assert len(page.flows) == 10
        assert len(statements) == without_capture, f"the design-capture fields cost an extra query: {without_capture} → {len(statements)}"

    async def test_a_long_description_does_not_break_the_search_filter(self, session, app_with_router):
        """`q` filters title / slug / intent_ref — deliberately NOT description.

        Searching a 500-char free-text column on every keystroke is a different
        feature with a different cost, and the issue scopes it out. Pinned so a
        later "helpful" widening is a deliberate decision.
        """
        await seed_flow(session, slug="alpha-loop", title="Alpha", created_offset=0, description="a distinctive needle phrase")
        await seed_flow(session, slug="beta-loop", title="Beta", created_offset=1)

        response = client_for(app_with_router).get(ROUTE, params={"q": "needle"})

        assert response.status_code == 200
        assert response.json()["flows"] == [], "description became searchable; that is out of scope for #4885"


class TestEndpoint:
    """The HTTP surface: status codes, payload shape, authz."""

    async def test_an_empty_org_is_an_empty_list_not_a_404(self, session, app_with_router):
        """ "This org has no flows" is a true and complete answer to the question."""
        response = client_for(app_with_router).get(ROUTE)

        assert response.status_code == 200
        body = response.json()
        assert body["flows"] == []
        assert body["total"] == 0

    async def test_the_payload_carries_every_field_the_card_renders(self, session, app_with_router):
        flow = await seed_flow(
            session,
            slug="aidlc-delivery-loop-4645",
            title="Delivery loop for #4645",
            intent_ref="4645",
            created_offset=0,
        )
        await seed_node(session, flow, node_ref="n1", state=NodeState.AWAITING_GATE.value, created_offset=1)
        await seed_node(session, flow, node_ref="n2", state=NodeState.PENDING.value, created_offset=2)
        await seed_node(session, flow, node_ref="n3", state=NodeState.PENDING.value, created_offset=3)

        body = client_for(app_with_router).get(ROUTE).json()
        summary = body["flows"][0]

        assert summary["id"] == flow.id
        assert summary["slug"] == "aidlc-delivery-loop-4645"
        assert summary["title"] == "Delivery loop for #4645"
        assert summary["intent_ref"] == "4645"
        assert summary["status"] == FlowStatus.AWAITING_YOU.value
        assert summary["awaiting_gate_count"] == 1
        assert summary["stalled_count"] == summary["display_counts"]["stalled"] == 0
        assert summary["display_counts"] == {"queued": 2, "in_progress": 0, "gate": 1, "stalled": 0, "complete": 0}
        assert summary["total_nodes"] == 3
        assert summary["epic_count"] == 1
        assert summary["wave_count"] == 1
        assert summary["current_wave_ref"] == "wave-1"
        assert summary["waves"] == [
            {
                "epic_ref": "epic-1",
                "wave_ref": "wave-1",
                "total": 3,
                "done": 0,
                "story_count": 3,
                "gate_count": 0,
                "eval_count": 0,
                "display_counts": {"queued": 2, "in_progress": 0, "gate": 1, "stalled": 0, "complete": 0},
            }
        ]
        assert summary["created_at"] == flow.created_at.isoformat()
        assert summary["updated_at"] is None
        assert body["limit"] == 25
        assert body["offset"] == 0

    async def test_the_flow_state_column_is_not_surfaced(self, session, app_with_router):
        """`OrchestrationFlow.state` has no writer and is permanently "pending".

        Publishing it would put a meaningless word exactly where operators look for
        status.
        """
        flow = await seed_flow(session, slug="loop", created_offset=0)
        await seed_node(session, flow, node_ref="n1")

        summary = client_for(app_with_router).get(ROUTE).json()["flows"][0]

        assert "state" not in summary

    async def test_status_counts_are_returned_for_the_chips(self, session, app_with_router):
        flow = await seed_flow(session, slug="gated", created_offset=0)
        await seed_node(session, flow, node_ref="n1", state=NodeState.AWAITING_GATE.value)

        body = client_for(app_with_router).get(ROUTE).json()

        assert body["status_counts"]["awaiting_you"] == 1
        assert body["status_counts"]["queued"] == 0, "zero-count chips are still information"
        assert set(body["status_counts"]) == {status.value for status in FlowStatus}

    async def test_limit_over_the_cap_is_a_422_not_a_silent_clamp(self, session, app_with_router):
        """A client that asked for 500 and silently got 100 pages as though it had
        500, and skips four fifths of the list."""
        response = client_for(app_with_router).get(ROUTE, params={"limit": 101})

        assert response.status_code == 422

    async def test_limit_at_the_cap_is_accepted(self, session, app_with_router):
        assert client_for(app_with_router).get(ROUTE, params={"limit": 100}).status_code == 200

    async def test_a_zero_limit_or_negative_offset_is_rejected(self, session, app_with_router):
        client = client_for(app_with_router)

        assert client.get(ROUTE, params={"limit": 0}).status_code == 422
        assert client.get(ROUTE, params={"offset": -1}).status_code == 422

    async def test_an_unknown_status_is_rejected_rather_than_ignored(self, session, app_with_router):
        """Silently ignoring it would show the operator an unfiltered list they
        believe is filtered."""
        assert client_for(app_with_router).get(ROUTE, params={"status": "on_fire"}).status_code == 422

    async def test_an_unknown_sort_is_rejected(self, session, app_with_router):
        """Including `cost`, which is cut from v1: it cannot participate in the
        paginated statement, so accepting it would rank one page while appearing to
        rank everything."""
        assert client_for(app_with_router).get(ROUTE, params={"sort": "cost"}).status_code == 422

    async def test_filters_are_honoured_over_http(self, session, app_with_router):
        gated = await seed_flow(session, slug="gated", created_offset=0)
        await seed_node(session, gated, node_ref="n1", state=NodeState.AWAITING_GATE.value)
        running = await seed_flow(session, slug="running", created_offset=1)
        await seed_node(session, running, node_ref="n1", state=NodeState.RUNNING.value)

        client = client_for(app_with_router)

        needs_me = client.get(ROUTE, params={"needs_me": "true"}).json()
        assert [flow["slug"] for flow in needs_me["flows"]] == ["gated"]
        assert needs_me["total"] == 1
        # Chips stay tenant-wide under a filter — "Showing 1 of 2".
        assert sum(needs_me["status_counts"].values()) == 2

        by_status = client.get(ROUTE, params={"status": "running"}).json()
        assert [flow["slug"] for flow in by_status["flows"]] == ["running"]

        searched = client.get(ROUTE, params={"q": "gat"}).json()
        assert [flow["slug"] for flow in searched["flows"]] == ["gated"]

    async def test_pagination_is_honoured_over_http(self, session, app_with_router):
        for index in range(30):
            await seed_flow(session, slug=f"flow-{index:02d}", created_offset=index)

        body = client_for(app_with_router).get(ROUTE, params={"limit": 5, "offset": 5}).json()

        assert len(body["flows"]) == 5
        assert body["total"] == 30
        assert body["limit"] == 5
        assert body["offset"] == 5

    async def test_another_tenants_flows_are_never_listed(self, session, app_with_router):
        await seed_flow(session, slug="theirs", org_id=ORG_B, created_offset=0)
        await seed_flow(session, slug="mine", org_id=ORG_A, created_offset=1)

        body = client_for(app_with_router, org_id=ORG_A).get(ROUTE).json()

        assert [flow["slug"] for flow in body["flows"]] == ["mine"]
        assert body["total"] == 1

    async def test_another_tenants_nodes_do_not_inflate_this_tenants_counts(self, session, app_with_router):
        """The aggregates filter `org_id` in SQL, not just the flow row.

        A same-`flow_id` node belonging to another org should not arise in practice,
        but the aggregate joins on `flow_id` — so scoping only the flows table would
        make this a real cross-tenant read of how much work exists.
        """
        mine = await seed_flow(session, slug="mine", org_id=ORG_A, created_offset=0)
        await seed_node(session, mine, node_ref="n1", state=NodeState.RUNNING.value, org_id=ORG_A)
        await seed_node(session, mine, node_ref="intruder", state=NodeState.RUNNING.value, org_id=ORG_B)

        summary = client_for(app_with_router, org_id=ORG_A).get(ROUTE).json()["flows"][0]

        assert summary["total_nodes"] == 1
        assert summary["display_counts"]["in_progress"] == 1
        assert summary["wave_count"] == 1

    async def test_another_tenants_decisions_do_not_inflate_the_stall_count(self, session, app_with_router):
        mine = await seed_flow(session, slug="mine", org_id=ORG_A, created_offset=0)
        node = await seed_node(session, mine, node_ref="n1", state=NodeState.RUNNING.value, org_id=ORG_A)
        await seed_decision(session, mine, node, kind=DecisionKind.NODE_STALLED.value, org_id=ORG_B)

        summary = client_for(app_with_router, org_id=ORG_A).get(ROUTE).json()["flows"][0]

        assert summary["stalled_count"] == summary["display_counts"]["stalled"] == 0
        assert summary["status"] == FlowStatus.RUNNING.value

    async def test_without_usage_read_the_request_is_denied(self, session, app_with_router):
        await seed_flow(session, slug="mine", created_offset=0)

        response = client_for(app_with_router, permitted=False).get(ROUTE)

        assert response.status_code == 403

    async def test_the_permission_check_runs_before_any_query(self, session, app_with_router):
        """A denied caller must not learn whether anything exists — and must not be
        able to make the server do the aggregate work on their behalf either."""
        await seed_flow(session, slug="mine", created_offset=0)

        statements, stop = statement_recorder(session)
        try:
            response = client_for(app_with_router, permitted=False).get(ROUTE)
        finally:
            stop()

        assert response.status_code == 403
        assert statements == [], f"queries ran before the permission check: {statements}"


async def test_evaluation_story_counts_include_only_current_issue_linked_work(session, app_with_router):
    flow = await seed_flow(session, slug="task-api-5792")
    for index in range(9):
        await seed_node(session, flow, node_ref=f"t{index}", state="passed" if index == 0 else "pending")
    for index in range(5):
        node = await seed_node(session, flow, node_ref=f"v{index}", kind="eval", state="passed" if index == 0 else "pending")
        node.issue_ref = str(5802 + index)
    await seed_node(session, flow, node_ref="checkpoint", kind="eval", state="passed")
    await seed_node(session, flow, node_ref="accept", kind="gate", state="passed")
    for ref, state, org, issue in [
        ("old-evaluation", "superseded", ORG_A, "5802"),
        ("foreign-evaluation", "passed", ORG_B, "5802"),
        ("empty-checkpoint", "passed", ORG_A, ""),
        ("blank-checkpoint", "passed", ORG_A, "   "),
    ]:
        node = await seed_node(session, flow, node_ref=ref, kind="eval", state=state, org_id=org)
        node.issue_ref = issue
    await session.flush()

    response = client_for(app_with_router).get(ROUTE)
    assert response.status_code == 200
    summary = response.json()["flows"][0]
    assert summary["story_count"] == 9  # Engine-kind count remains compatible.
    assert summary["eval_story_count"] == 5
    assert summary["eval_count"] == 8  # Five stories and three unlinked checkpoints.
    assert summary["completed_story_count"] == 1
    assert summary["completed_eval_story_count"] == 1
    assert summary["story_count"] + summary["eval_story_count"] == 14


class TestRequestedChangesSummary:
    async def test_superplane_counts_feedback_and_attention_filters_agree(self, session, app_with_router):
        flow = await seed_flow(session, slug="superplane", intent_ref="4910")
        gate = await seed_node(session, flow, node_ref="accept", kind="gate", state="rejected_at_gate")
        for wave, stories, gates in [(1, 4, 3), (2, 6, 3), (3, 12, 2), (4, 5, 5)]:
            for i in range(stories):
                await seed_node(session, flow, node_ref=f"story-{wave}-{i}", wave=f"wave-{wave}", created_offset=wave)
            for i in range(gates):
                await seed_node(session, flow, node_ref=f"gate-{wave}-{i}", kind="gate", wave=f"wave-{wave}", created_offset=wave)
            await seed_node(session, flow, node_ref=f"eval-{wave}", kind="eval", wave=f"wave-{wave}", created_offset=wave)
        await seed_node(session, flow, node_ref="old-story", state="superseded")
        await seed_node(session, flow, node_ref="old-gate", kind="gate", state="superseded")
        await seed_node(session, flow, node_ref="foreign", org_id=ORG_B)
        await OrchestrationRepository(session).append_decision(
            org_id=ORG_A,
            flow_id=flow.id,
            node_id=gate.id,
            kind="gate_rejected",
            actor_id=USER_ID,
            actor_role="admin",
            actor_kind="human",
            reason="Clarify the migration acceptance criteria.",
            from_state="awaiting_gate",
            to_state="rejected_at_gate",
        )
        client = client_for(app_with_router)
        body = client.get(ROUTE).json()
        summary = body["flows"][0]
        assert summary["status"] == "attention_needed"
        assert summary["changes_requested_count"] == 1
        assert summary["stalled_count"] == summary["display_counts"]["stalled"] == 1
        assert (summary["story_count"], summary["gate_count"], summary["eval_count"], summary["total_nodes"]) == (27, 14, 4, 45)
        assert [w["story_count"] for w in summary["waves"]] == [4, 6, 12, 5]
        assert body["status_counts"]["attention_needed"] == 1
        assert body["status_counts"]["queued"] == 0
        assert client.get(ROUTE, params={"needs_me": True}).json()["total"] == 1
        assert client.get(ROUTE, params={"status": "attention_needed"}).json()["total"] == 1
        assert client.get(ROUTE, params={"status": "queued"}).json()["total"] == 0
        graph = client.get(f"{ROUTE}/{flow.id}").json()
        feedback = next(n for n in graph["nodes"] if n["id"] == gate.id)["last_gate_decision"]
        assert feedback["action"] == "changes_requested"
        assert feedback["reason"] == "Clarify the migration acceptance criteria."
        assert feedback["created_at"]

    @pytest.mark.parametrize("state", ["failed", "halted", "rejected_at_gate"])
    async def test_a_node_needing_intervention_is_never_queued_or_complete(self, session, state):
        flow = await seed_flow(session, slug="needs-action")
        await seed_node(session, flow, node_ref="problem", state=state)
        await seed_node(session, flow, node_ref="done", state="passed")
        repo = OrchestrationRepository(session)
        page = await repo.list_flows_page_with_aggregates(org_id=ORG_A, needs_me=True)
        assert page.total == 1
        assert page.flows[0].status == FlowStatus.ATTENTION_NEEDED
        assert page.flows[0].needs_me
        assert (await repo.list_flows_page_with_aggregates(org_id=ORG_A, status=FlowStatus.COMPLETE)).total == 0
