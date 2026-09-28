"""Tests for three-valued cost aggregation by graph address.

Issue #4207 (EPIC #4191). The load-bearing tests here are the **adversarial**
ones, because both failure modes this story fixes are invisible in production:

  - **AC-19**: joining on the DynamoDB attribute named `run_id` (the KEDA pod
    name) instead of `event_id` returns zero rows *successfully*, which every
    formatter downstream renders as `$0.00`. A wrong number that looks right.
  - **AC-20**: reading `budget_usage` as a cost source under-reports by the
    sub-cent long tail, silently, in a direction nobody notices.
  - **AC-21**: an aggregate containing an unmeasured node is a **lower bound**,
    and rendering it as a total is how decisions get made on wrong figures.
  - **AC-22**: a node with no ledger row is `unknown`, never `0`.

Session fixture mirrors `test_compile.py`'s in-memory SQLite over
`Base.metadata.create_all`.
"""

import inspect as py_inspect
import re
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.orchestration import cost as cost_module
from src.orchestration.cost import (
    COST_SCOPE_LABEL,
    CostStatus,
    JoinKeyError,
    NodeCost,
    UnknownReason,
    assert_join_key_is_event_id,
    get_cost_by_address,
    get_flow_cost,
)
from src.orchestration.models import OrchestrationFlow, OrchestrationNode
from src.shared.models.base import Base
from src.shared.models.usage import UsageLog

ORG_A = "org-alpha"
ORG_B = "org-beta"
FLOW = "demo-flow"


def _executable_source(module) -> str:
    """A module's source with docstrings and comments stripped.

    The source-level assertions below check that certain names are absent from the
    cost read path. They must look at CODE only: this module's prose deliberately
    explains why `budget_usage` is excluded and what the DynamoDB `run_id` trap
    is, and a naive substring check trips on the very documentation it is
    enforcing. Stripping prose is what keeps the assertion about behaviour instead
    of about wording — otherwise the only way to pass is to delete the comments
    that make the trap survivable for the next reader.
    """
    source = py_inspect.getsource(module)
    # Docstrings (module, class and function) — triple-quoted, either quote style.
    source = re.sub(r'"""(?:.|\n)*?"""', "", source)
    source = re.sub(r"'''(?:.|\n)*?'''", "", source)
    # Whole-line and trailing comments.
    source = re.sub(r"#[^\n]*", "", source)
    return source


@pytest.fixture
async def session():
    """In-memory SQLite session. See test_compile.py for the pysqlite hooks."""
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


def _usage(
    *,
    org_id: str = ORG_A,
    address: str | None,
    cost: str,
    run_id: str = "evt-abc-123",
    tokens: int = 100,
) -> UsageLog:
    """One ledger row. `cost` is a string so the Decimal is exact."""
    return UsageLog(
        org_id=org_id,
        department_id="dept",
        team_id="team",
        user_id="user",
        account_type="service",
        model="anthropic.claude-3-5-sonnet",
        input_tokens=tokens,
        output_tokens=0,
        cost_usd=Decimal(cost),
        latency_ms=100,
        status_code=200,
        agent_run_id=run_id,
        graph_address=address,
    )


async def _seed_flow(session, *, org_id: str = ORG_A, nodes: list[tuple[str, str, str, str]]) -> tuple[OrchestrationFlow, list[OrchestrationNode]]:
    """A flow plus nodes given as (epic_ref, wave_ref, node_ref, kind)."""
    flow = OrchestrationFlow(org_id=org_id, slug=FLOW, title="Demo flow")
    session.add(flow)
    await session.flush()

    created = []
    for epic_ref, wave_ref, node_ref, kind in nodes:
        node = OrchestrationNode(
            org_id=org_id,
            flow_id=flow.id,
            epic_ref=epic_ref,
            wave_ref=wave_ref,
            node_ref=node_ref,
            kind=kind,
            title=f"{node_ref} title",
        )
        session.add(node)
        created.append(node)
    await session.flush()
    return flow, created


class TestJoinKeyGuard:
    """AC-19 — adversarial, load-bearing. The failure mode it prevents is invisible.

    `usage_logs.agent_run_id` == DynamoDB `event_id` (the worker's `message_id`):
    entrypoint.py:834 sets ADP_MESSAGE_ID → sigv4-proxy.ts:35 reads it → injected
    as X-Agent-RunId → proxy/routes.py:141 → the column. The attribute *named*
    `run_id` is the KEDA job name (entrypoint.py:1495 `run_id=_keda_job_name`),
    which the UI labels "Run / Job ID". The wrong key has the more convincing name.
    """

    @pytest.mark.parametrize(
        "pod_name",
        [
            "agent-gateway-worker-abc12",  # the deployed webhook ScaledJob
            "chat-agent-worker-xyz98",  # the deployed chat ScaledJob
            "arc-runner-7f3d9",
            "worker-1",
        ],
    )
    def test_keda_pod_names_raise_rather_than_return_zero_dollars(self, pod_name):
        """The whole point: this must RAISE, not return a successful $0.00."""
        with pytest.raises(JoinKeyError):
            assert_join_key_is_event_id([pod_name])

    def test_error_message_names_the_correct_key(self):
        """An exception a reader cannot act on just moves the confusion."""
        with pytest.raises(JoinKeyError, match="event_id"):
            assert_join_key_is_event_id(["agent-gateway-worker-abc12"])

    def test_one_bad_id_among_good_ones_still_raises(self):
        """A partially-wrong list is still a wrong join — it would silently
        under-count rather than fail."""
        with pytest.raises(JoinKeyError):
            assert_join_key_is_event_id(["evt-1", "agent-gateway-worker-abc12", "evt-2"])

    @pytest.mark.parametrize(
        "event_id",
        [
            "550e8400-e29b-41d4-a716-446655440000",  # uuid4, the real shape
            "evt-abc-123",
            "9f8b7c6d5e4f",
        ],
    )
    def test_real_event_ids_pass(self, event_id):
        """No false positives: an event_id is a uuid4 and contains no such word."""
        assert_join_key_is_event_id([event_id]) is None

    def test_empty_list_is_fine(self):
        assert_join_key_is_event_id([]) is None

    @pytest.mark.asyncio
    async def test_querying_by_pod_name_finds_nothing_which_is_why_the_guard_exists(self, session):
        """Demonstrates the silent failure the guard prevents.

        The ledger row is addressed and has a real cost, but a query keyed on the
        pod name matches nothing — and `SUM` over zero rows is `0`, not an error.
        This test documents *why* an explicit guard is required rather than
        trusting the query to complain.
        """
        session.add(_usage(address=f"{FLOW}/epic-1/wave-1/story-1", cost="1.50", run_id="evt-abc-123"))
        await session.flush()

        total = (
            await session.execute(sa.select(sa.func.count(UsageLog.id)).where(UsageLog.agent_run_id == "agent-gateway-worker-abc12"))
        ).scalar_one()
        assert total == 0  # Zero rows. No error. This is the bug.


class TestBudgetUsageIsNeverACostSource:
    """AC-20 — adversarial, source-level.

    `budget_usage` is a budget-enforcement counter, not a ledger: it is a
    per-(entity, period) rolling accumulator with no graph address and no run
    identifier, so it cannot answer "what did THIS node cost" at any address.
    That is the durable reason it is never a cost source, and it holds regardless
    of the column's precision.

    A source-level assertion is what makes a future change reintroducing it fail
    CI, rather than under-reporting silently in production.

    Historical note: this class originally rested on `total_cost_usd` being
    `Numeric(10, 2)` and therefore dropping the sub-cent long tail. #4287/#4291
    widened it to `Numeric(14, 6)` precisely to fix that rounding, so the
    precision premise is now obsolete — but the aggregation-shape argument above
    was always the load-bearing one, and it is unaffected.
    """

    def test_cost_module_source_never_mentions_budget_usage(self):
        body = _executable_source(cost_module)
        assert "budget_usage" not in body
        assert "BudgetUsage" not in body

    def test_cost_module_does_not_import_the_budget_model(self):
        assert not hasattr(cost_module, "BudgetUsage")

    def test_only_usage_logs_is_queried(self):
        """R-O6a: `usage_logs` is the only cost source."""
        body = _executable_source(cost_module)
        assert "UsageLog" in body

    def test_budget_usage_cannot_address_a_graph_node(self):
        """Guards the real premise: `budget_usage` has no per-node identity.

        It carries neither a graph address nor a run identifier, so it cannot be
        joined to a node however precise its money column becomes. This is why
        AC-20 holds independently of #4287/#4291 widening `total_cost_usd`.
        """
        from src.shared.models.budget import BudgetUsage

        columns = set(BudgetUsage.__table__.columns.keys())
        assert "graph_address" not in columns
        assert "agent_run_id" not in columns

    def test_the_ledger_keeps_sub_cent_precision(self):
        """`usage_logs.cost_usd` is the 6dp ledger the aggregation reads."""
        assert UsageLog.__table__.columns["cost_usd"].type.scale == 6


class TestThreeValuedCost:
    """AC-22 and the none_incurred/unknown distinction."""

    @pytest.mark.asyncio
    async def test_node_with_no_usage_row_is_unknown_never_zero(self, session):
        """AC-22. The single most important assertion in this file.

        A node with no ledger row has an *unmeasured* cost. Reporting `0` claims
        it was free, which is a statement the data does not support.
        """
        flow, nodes = await _seed_flow(session, nodes=[("epic-1", "wave-1", "story-1", "story")])

        result = await get_flow_cost(session, org_id=ORG_A, flow=flow, nodes=nodes)

        assert result.nodes[0].status is CostStatus.UNKNOWN
        assert result.nodes[0].amount_usd is None  # NOT Decimal(0)
        assert result.nodes[0].reason is not None

    @pytest.mark.asyncio
    async def test_none_incurred_is_distinguishable_from_unknown(self, session):
        """A real zero and an absent measurement must not be the same value.

        This is the pair that makes the whole story coherent: both total zero
        dollars, and they mean opposite things.
        """
        flow, nodes = await _seed_flow(
            session,
            nodes=[("epic-1", "wave-1", "measured-zero", "story"), ("epic-1", "wave-1", "no-rows", "story")],
        )
        # A row that exists and genuinely cost nothing.
        session.add(_usage(address=f"{FLOW}/epic-1/wave-1/measured-zero", cost="0"))
        await session.flush()

        result = await get_flow_cost(session, org_id=ORG_A, flow=flow, nodes=nodes)
        by_ref = {node.address.rsplit("/", 1)[-1]: node for node in result.nodes}

        assert by_ref["measured-zero"].status is CostStatus.NONE_INCURRED
        assert by_ref["measured-zero"].amount_usd == Decimal("0")
        assert by_ref["no-rows"].status is CostStatus.UNKNOWN
        assert by_ref["no-rows"].amount_usd is None
        assert by_ref["measured-zero"].status is not by_ref["no-rows"].status

    @pytest.mark.asyncio
    async def test_known_cost_carries_the_exact_amount(self, session):
        """Sub-cent precision survives: `Numeric(10, 6)`, summed as Decimal."""
        flow, nodes = await _seed_flow(session, nodes=[("epic-1", "wave-1", "story-1", "story")])
        session.add(_usage(address=f"{FLOW}/epic-1/wave-1/story-1", cost="0.001234"))
        session.add(_usage(address=f"{FLOW}/epic-1/wave-1/story-1", cost="0.002766"))
        await session.flush()

        result = await get_flow_cost(session, org_id=ORG_A, flow=flow, nodes=nodes)

        assert result.nodes[0].status is CostStatus.KNOWN
        assert result.nodes[0].amount_usd == Decimal("0.004000")
        assert result.nodes[0].call_count == 2

    def test_unknown_cannot_be_constructed_with_an_amount(self):
        """Enforced in the type, not left to callers.

        An UNKNOWN carrying `0` is exactly the shape that gets formatted as
        `$0.00` three layers away from here.
        """
        with pytest.raises(ValueError, match="must not carry an amount"):
            NodeCost(address="a/b/c/d", status=CostStatus.UNKNOWN, amount_usd=Decimal(0), reason=UnknownReason.NOT_STARTED)

    def test_unknown_requires_a_reason(self):
        """Bare "unknown" with no explanation reads as a bug to whoever sees it."""
        with pytest.raises(ValueError, match="must carry a reason"):
            NodeCost(address="a/b/c/d", status=CostStatus.UNKNOWN)

    def test_known_requires_an_amount(self):
        with pytest.raises(ValueError, match="must carry an amount"):
            NodeCost(address="a/b/c/d", status=CostStatus.KNOWN)

    @pytest.mark.asyncio
    async def test_gate_and_eval_nodes_report_not_costable(self, session):
        """The reason distinguishes "nothing to bill" from "has not run yet".

        Both are UNKNOWN, but they are not the same news to an operator.
        """
        flow, nodes = await _seed_flow(
            session,
            nodes=[("epic-1", "wave-1", "gate-1", "gate"), ("epic-1", "wave-1", "story-1", "story")],
        )

        result = await get_flow_cost(session, org_id=ORG_A, flow=flow, nodes=nodes)
        by_ref = {node.address.rsplit("/", 1)[-1]: node for node in result.nodes}

        assert by_ref["gate-1"].reason is UnknownReason.NOT_COSTABLE
        assert by_ref["story-1"].reason is UnknownReason.NOT_STARTED


class TestPartialAggregates:
    """AC-21 — a partial total is a lower bound and must say so."""

    @pytest.mark.asyncio
    async def test_aggregate_with_an_unknown_node_is_partial(self, session):
        flow, nodes = await _seed_flow(
            session,
            nodes=[("epic-1", "wave-1", "measured", "story"), ("epic-1", "wave-1", "unmeasured", "story")],
        )
        session.add(_usage(address=f"{FLOW}/epic-1/wave-1/measured", cost="2.00"))
        await session.flush()

        result = await get_flow_cost(session, org_id=ORG_A, flow=flow, nodes=nodes)

        assert result.partial is True
        assert result.unknown_node_count == 1
        assert result.amount_usd == Decimal("2.00")  # a lower bound, not a total

    @pytest.mark.asyncio
    async def test_aggregate_with_every_node_measured_is_complete(self, session):
        """The negative case: complete must be reachable, or `partial` is noise."""
        flow, nodes = await _seed_flow(
            session,
            nodes=[("epic-1", "wave-1", "one", "story"), ("epic-1", "wave-1", "two", "story")],
        )
        session.add(_usage(address=f"{FLOW}/epic-1/wave-1/one", cost="1.00"))
        session.add(_usage(address=f"{FLOW}/epic-1/wave-1/two", cost="0.50"))
        await session.flush()

        result = await get_flow_cost(session, org_id=ORG_A, flow=flow, nodes=nodes)

        assert result.partial is False
        assert result.unknown_node_count == 0
        assert result.amount_usd == Decimal("1.50")
        assert result.status is CostStatus.KNOWN

    @pytest.mark.asyncio
    async def test_aggregate_with_no_measured_nodes_is_unknown_not_zero(self, session):
        """An untouched flow has not been established to be free."""
        flow, nodes = await _seed_flow(session, nodes=[("epic-1", "wave-1", "story-1", "story")])

        result = await get_flow_cost(session, org_id=ORG_A, flow=flow, nodes=nodes)

        assert result.status is CostStatus.UNKNOWN
        assert result.amount_usd is None
        assert result.partial is True

    def test_partial_is_structurally_implied_by_an_unknown_node(self):
        """Belt and braces: the invariant is enforced in the type too."""
        from src.orchestration.cost import AggregateCost

        with pytest.raises(ValueError, match="partial by definition"):
            AggregateCost(address=FLOW, status=CostStatus.KNOWN, amount_usd=Decimal(1), unknown_node_count=1, partial=False)


class TestScopeLabel:
    """R-N5c — a figure silently excluding non-run cost reads as a total."""

    @pytest.mark.asyncio
    async def test_every_node_figure_carries_the_scope_label(self, session):
        flow, nodes = await _seed_flow(session, nodes=[("epic-1", "wave-1", "story-1", "story")])
        session.add(_usage(address=f"{FLOW}/epic-1/wave-1/story-1", cost="1.00"))
        await session.flush()

        result = await get_flow_cost(session, org_id=ORG_A, flow=flow, nodes=nodes)

        assert result.scope == COST_SCOPE_LABEL
        assert all(node.scope == COST_SCOPE_LABEL for node in result.nodes)

    def test_scope_label_names_what_is_excluded(self):
        """ "Agent run costs" alone would not tell a reader build/infra is missing."""
        assert "excludes" in COST_SCOPE_LABEL
        assert "infra" in COST_SCOPE_LABEL


class TestSingleGroupedQuery:
    """R-O6c — one grouped query, no enumerate-then-IN, no DynamoDB.

    Asserting the query COUNT is what makes a regression to the old shape fail:
    enumerate-then-`IN` is functionally correct on day 1 and silently partial on
    day 31, so no behavioural assertion catches it.
    """

    @pytest.mark.asyncio
    async def test_aggregation_issues_exactly_one_ledger_query(self, session):
        flow, nodes = await _seed_flow(
            session,
            nodes=[(f"epic-{i}", "wave-1", f"story-{i}", "story") for i in range(5)],
        )
        for i in range(5):
            session.add(_usage(address=f"{FLOW}/epic-{i}/wave-1/story-{i}", cost="0.10"))
        await session.flush()

        statements: list[str] = []

        @event.listens_for(session.sync_session, "do_orm_execute")
        def _record(orm_execute_state):
            statements.append(str(orm_execute_state.statement))

        try:
            await get_flow_cost(session, org_id=ORG_A, flow=flow, nodes=nodes)
        finally:
            event.remove(session.sync_session, "do_orm_execute", _record)

        # One query for FIVE nodes. A per-node query would make this 5, and an
        # enumerate-then-IN shape would add a DDB round trip that is not a
        # statement at all — hence the explicit source check below as well.
        assert len(statements) == 1, f"expected 1 grouped query, got {len(statements)}: {statements}"
        assert "GROUP BY" in statements[0].upper()

    @pytest.mark.asyncio
    async def test_query_count_does_not_grow_with_node_count(self, session):
        """The N+1 guard: 20 nodes must still be one query."""
        flow, nodes = await _seed_flow(
            session,
            nodes=[("epic-1", "wave-1", f"story-{i}", "story") for i in range(20)],
        )
        statements: list[str] = []

        @event.listens_for(session.sync_session, "do_orm_execute")
        def _record(orm_execute_state):
            statements.append(str(orm_execute_state.statement))

        try:
            await get_flow_cost(session, org_id=ORG_A, flow=flow, nodes=nodes)
        finally:
            event.remove(session.sync_session, "do_orm_execute", _record)

        assert len(statements) == 1

    def test_cost_module_has_no_dynamodb_dependency(self):
        """The 30-day cliff came from DDB being on the read path at all.

        Asserted as "no client, no table name" rather than "the string 'dynamodb'
        never appears": the `JoinKeyError` message legitimately explains that the
        DynamoDB attribute named `run_id` is the wrong join key, and that sentence
        is the most valuable prose in the module. A check that forbade the word
        would be satisfied only by deleting the explanation — trading a real
        safeguard for a cosmetic one.
        """
        body = _executable_source(cost_module).lower()
        # An actual client or resource handle is the dependency that would put DDB
        # back on the read path and reintroduce the expiry horizon.
        for forbidden in ("boto3", "import aioboto3", "webhook-events", "webhook_events", ".query(", ".scan("):
            assert forbidden not in body, f"{forbidden!r} is back on the cost read path"

    def test_cost_module_imports_nothing_from_the_activity_ddb_layer(self):
        """`activity/service.py` is the DDB reader — importing it re-couples them."""
        body = _executable_source(cost_module)
        assert "activity" not in body

    def test_aggregation_does_not_take_a_run_ids_list(self):
        """The `run_ids: list[str]` parameter IS the enumerate-then-IN shape."""
        for fn in (get_cost_by_address, get_flow_cost):
            assert "run_ids" not in py_inspect.signature(fn).parameters


class TestTenantIsolation:
    """Every cost query filters on org_id."""

    @pytest.mark.asyncio
    async def test_another_orgs_usage_rows_are_not_counted(self, session):
        """The blast-radius case: cross-tenant cost disclosure."""
        flow, nodes = await _seed_flow(session, nodes=[("epic-1", "wave-1", "story-1", "story")])
        address = f"{FLOW}/epic-1/wave-1/story-1"
        session.add(_usage(org_id=ORG_A, address=address, cost="1.00"))
        # Same address, different tenant. Two orgs may run identically-named flows.
        session.add(_usage(org_id=ORG_B, address=address, cost="99.00"))
        await session.flush()

        result = await get_flow_cost(session, org_id=ORG_A, flow=flow, nodes=nodes)

        assert result.amount_usd == Decimal("1.00")  # not 100.00

    @pytest.mark.asyncio
    async def test_querying_as_another_org_returns_no_rows_not_someone_elses_costs(self, session):
        session.add(_usage(org_id=ORG_A, address=f"{FLOW}/epic-1/wave-1/story-1", cost="5.00"))
        await session.flush()

        costs = await get_cost_by_address(session, org_id=ORG_B, address_prefix=FLOW)

        assert costs == []


class TestAddressPrefixMatching:
    """The prefix must not leak into sibling subtrees."""

    @pytest.mark.asyncio
    async def test_epic_1_does_not_match_epic_10(self, session):
        """Off-by-one prefix matching would silently merge two EPICs' totals."""
        session.add(_usage(address=f"{FLOW}/epic-1/wave-1/story-1", cost="1.00"))
        session.add(_usage(address=f"{FLOW}/epic-10/wave-1/story-1", cost="50.00"))
        await session.flush()

        costs = await get_cost_by_address(session, org_id=ORG_A, address_prefix=f"{FLOW}/epic-1")

        assert len(costs) == 1
        assert costs[0].amount_usd == Decimal("1.00")

    @pytest.mark.asyncio
    async def test_exact_address_matches_itself(self, session):
        address = f"{FLOW}/epic-1/wave-1/story-1"
        session.add(_usage(address=address, cost="3.00"))
        await session.flush()

        costs = await get_cost_by_address(session, org_id=ORG_A, address_prefix=address)

        assert [c.address for c in costs] == [address]

    @pytest.mark.asyncio
    async def test_like_wildcards_in_the_prefix_are_escaped(self, session):
        """A `%` in the requested prefix must not widen the match.

        Without escaping, `%` would match across subtrees and report another
        EPIC's spend under this one's address.
        """
        session.add(_usage(address=f"{FLOW}/epic-1/wave-1/story-1", cost="1.00"))
        await session.flush()

        costs = await get_cost_by_address(session, org_id=ORG_A, address_prefix=f"{FLOW}/%")

        assert costs == []

    @pytest.mark.asyncio
    async def test_unaddressed_rows_are_excluded(self, session):
        """Null-address rows (every historical row) belong to no aggregate.

        The no-backfill contract means these exist in bulk; counting them into a
        flow's total would attribute unrelated spend to it.
        """
        session.add(_usage(address=None, cost="77.00"))
        session.add(_usage(address=f"{FLOW}/epic-1/wave-1/story-1", cost="1.00"))
        await session.flush()

        costs = await get_cost_by_address(session, org_id=ORG_A, address_prefix=FLOW)

        assert len(costs) == 1
        assert costs[0].amount_usd == Decimal("1.00")
