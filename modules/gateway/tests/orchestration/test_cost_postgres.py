"""Cost semantics and covering-index access for populated PostgreSQL ledgers."""

# ruff: noqa: F811 -- imported pytest fixtures are requested by the tests

from decimal import Decimal

import psycopg2
import pytest
from sqlalchemy import event, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic.migration import MigrationContext
from alembic.operations import Operations
from src.orchestration.cost import CostStatus, _cost_by_address_query, get_cost_by_address, get_cost_by_address_prefixes
from src.shared.models.usage import UsageLog
from tests.migrations.conftest_postgres import pg_server, pg_url, to_async_url  # noqa: F401
from tests.orchestration.decision_projection_fixture import migration
from tests.orchestration.test_cost import _usage


@pytest.fixture
async def cost_session(pg_url):
    engine = create_async_engine(to_async_url(pg_url))

    def create(connection):
        UsageLog.__table__.create(connection)
        connection.commit()
        context = MigrationContext.configure(connection)
        with Operations.context(context), context.begin_transaction():
            migration("105_flow_cost_covering").upgrade()

    try:
        async with engine.connect() as connection:
            await connection.run_sync(create)
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            yield db
    finally:
        await engine.dispose()


@pytest.mark.parametrize(
    "prefixes",
    [
        ["flow", "flow", "flow/epic", "flow/epic/story", "other"],
        ["flow/epic", "flow/epic-1"],
        ["flow_%", "flow_"],
        ["flow\\name"],
        ["équipe", "团队"],
        ["trailing/", "trailing//child"],
        ["", "/child"],
        ["missing"],
        [],
    ],
)
async def test_postgres_cost_prefixes_match_literal_subtrees_once(cost_session, prefixes):
    addresses = [
        "flow",
        "flow/epic",
        "flow/epic/story",
        "flow/epic-1",
        "flow/epic-10/story",
        "flow2/story",
        "other/story",
        "flow_%",
        "flow_%/story",
        "flow_/story",
        "flowXanything/story",
        "flow\\name/story",
        "flowname/story",
        "équipe/story",
        "团队/story",
        "trailing/",
        "trailing//child",
        "trailing/child",
        "",
        "/child",
        "/child/story",
        None,
    ]
    expected = {}
    for index, address in enumerate(addresses):
        # Two calls per address, including a verified zero; unknown is absence.
        amount = Decimal(index) / 1_000_000
        cost_session.add(_usage(org_id="fixture-a", address=address, cost=str(amount), tokens=3))
        second = _usage(org_id="fixture-a", address=address, cost=str(amount), tokens=4)
        second.output_tokens = 7
        cost_session.add(second)
        cost_session.add(_usage(org_id="fixture-b", address=address, cost="900", tokens=900))
        if address is not None and any(address == prefix or address.startswith(prefix + "/") for prefix in prefixes):
            expected[address] = (amount * 2, 14, 2, CostStatus.KNOWN if amount else CostStatus.NONE_INCURRED)
    await cost_session.commit()

    statements = []

    def record(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    event.listen(cost_session.bind.sync_engine, "before_cursor_execute", record)
    try:
        costs = await get_cost_by_address_prefixes(cost_session, org_id="fixture-a", address_prefixes=prefixes)
    finally:
        event.remove(cost_session.bind.sync_engine, "before_cursor_execute", record)
    assert len(statements) == (1 if prefixes else 0)
    assert len(costs) == len(expected), "Duplicate/overlapping prefixes must not duplicate an address"
    assert {cost.address: (cost.amount_usd, cost.total_tokens, cost.call_count, cost.status) for cost in costs} == expected
    if len(prefixes) == 1:
        detail = await get_cost_by_address(cost_session, org_id="fixture-a", address_prefix=prefixes[0])
        assert sorted(detail, key=lambda cost: cost.address) == sorted(costs, key=lambda cost: cost.address)


def plan_nodes(node):
    yield node
    for child in node.get("Plans", []):
        yield from plan_nodes(child)


@pytest.mark.parametrize("prefix_count", [1, 25, 100])
@pytest.mark.parametrize("plan_mode", ["force_custom_plan", "force_generic_plan"])
async def test_cost_reads_use_covering_index_for_each_range(cost_session, pg_url, prefix_count, plan_mode):
    # Most of the ledger belongs to other flows or tenants. A wide unused column
    # models why bitmap heap reads are expensive even when the index is cached.
    await cost_session.execute(
        text("""INSERT INTO usage_logs
        (id, timestamp, department_id, team_id, user_id, account_type, model, input_tokens, output_tokens,
         cost_usd, latency_ms, status_code, org_id, graph_address, chat_log_s3_key)
        SELECT 'fixture-' || n, now(), 'fixture', 'fixture', 'fixture', 'human', 'fixture', 2, 3,
               0.000001, 1, 200, CASE WHEN n <= 20000 THEN 'fixture-a' ELSE 'fixture-b' END,
               'flow-' || (n % 100) || CASE WHEN n % 3 = 0 THEN '' ELSE '/epic/story-' || (n % 7) END,
               repeat('x', 900)
        FROM generate_series(1, 40000) AS n""")
    )
    await cost_session.commit()
    prefixes = [f"flow-{index}" for index in range(prefix_count)]
    query = _cost_by_address_query(cost_session, org_id="fixture-a", address_prefixes=prefixes)
    compiled = query.compile(dialect=postgresql.dialect(paramstyle="numeric_dollar"))
    params = [compiled.params[key] for key in compiled.positiontup]
    connection = psycopg2.connect(pg_url)
    connection.autocommit = True
    try:
        with connection.cursor() as cursor:
            cursor.execute("VACUUM ANALYZE usage_logs")
            cursor.execute(f"SET plan_cache_mode = {plan_mode}")
            cursor.execute("PREPARE flow_cost AS " + str(compiled))
            execute = "EXECUTE flow_cost (" + ", ".join(["%s"] * len(params)) + ")"
            cursor.execute(execute, params)
            actual = cursor.fetchall()
            cursor.execute("""SELECT graph_address, sum(cost_usd), sum(input_tokens + output_tokens), count(id)
                FROM usage_logs WHERE org_id = 'fixture-a' GROUP BY graph_address""")
            expected = [row for row in cursor.fetchall() if row[0].split("/")[0] in prefixes]
            assert sorted(actual) == sorted(expected)
            cursor.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + execute, params)
            plan = cursor.fetchone()[0][0]["Plan"]
            scans = [node for node in plan_nodes(plan) if node.get("Relation Name") == "usage_logs"]
            assert scans and all(
                node["Node Type"] == "Index Only Scan"
                and node["Index Name"] == "ix_usage_flow_cost_covering"
                and node["Heap Fetches"] == 0
                and node.get("Rows Removed by Filter", 0) == 0
                for node in scans
            ), plan
            # Each ledger row is read only once, including at the 100-flow limit.
            assert sum(node["Actual Rows"] * node["Actual Loops"] for node in scans) <= sum(row[3] for row in expected) + 2 * prefix_count
            if plan_mode == "force_generic_plan":
                cursor.execute("SELECT generic_plans FROM pg_prepared_statements WHERE name = 'flow_cost'")
                assert cursor.fetchone()[0] >= 2
    finally:
        connection.close()
