"""Cost boundaries and prepared-plan index use on real PostgreSQL."""

from decimal import Decimal

import pytest
from sqlalchemy import event, literal, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.orchestration.cost import get_cost_by_address, get_cost_by_address_prefixes
from tests.migrations.conftest_postgres import pg_server, pg_url, to_async_url, upgrade  # noqa: F401


@pytest.mark.asyncio
async def test_literal_subtrees_and_generic_index_bounds(pg_url):  # noqa: F811
    upgrade(pg_url, "105_flow_cost_covering")
    engine = create_async_engine(to_async_url(pg_url))
    prefixes = ["flow_1", "flow%2", "flow\\3", "café", "flow/epic", "Flow", "flow/", ""]
    addresses = [None]
    for prefix in prefixes:
        addresses.extend([prefix, prefix + "/", prefix + "/wave/node", prefix + "0", prefix + "X/wave/node"])
    addresses = list(dict.fromkeys(addresses))
    try:
        async with engine.begin() as conn:
            for org in ("fixture-a", "fixture-b"):
                await conn.execute(
                    text("""INSERT INTO usage_logs
                    (id, timestamp, department_id, team_id, user_id, account_type, model,
                     input_tokens, output_tokens, cost_usd, latency_ms, status_code, org_id, graph_address)
                    VALUES (:id, now(), 'fixture', 'fixture', 'fixture', 'human', 'fixture',
                            2, 3, .01, 1, 200, :org, :address)"""),
                    [{"id": f"{org}-{i}", "org": org, "address": address} for i, address in enumerate(addresses)],
                )
            await conn.execute(
                text("""INSERT INTO usage_logs
                (id, timestamp, department_id, team_id, user_id, account_type, model,
                 input_tokens, output_tokens, cost_usd, latency_ms, status_code, org_id, graph_address)
                SELECT 'noise-' || n, now(), 'fixture', 'fixture', 'fixture', 'human', 'fixture',
                       2, 3, .01, 1, 200, 'fixture-a', 'unrelated-' || n || '/e/w/n'
                FROM generate_series(1, 20000) AS n""")
            )
            await conn.execute(text("ANALYZE usage_logs"))
        factory = async_sessionmaker(engine)
        async with factory() as db:
            await db.execute(text("SET TRANSACTION READ ONLY"))
            await db.execute(text("SET LOCAL plan_cache_mode='force_generic_plan'"))
            for prefix in prefixes:
                expected = {address for address in addresses if address is not None and (address == prefix or address.startswith(prefix + "/"))}
                rows = await get_cost_by_address(db, org_id="fixture-a", address_prefix=prefix)
                assert {row.address for row in rows} == expected
                assert all((r.amount_usd, r.total_tokens, r.call_count) == (Decimal(".01"), 5, 1) for r in rows)
            # Nested/duplicate prefixes must not double-count a ledger row.
            selected = ["flow", "flow/epic", "flow", "flow_1", "café"]
            rows = await get_cost_by_address_prefixes(db, org_id="fixture-a", address_prefixes=selected)
            expected = {a for a in addresses if a is not None and any(a == p or a.startswith(p + "/") for p in selected)}
            assert {r.address for r in rows} == expected
            assert all(r.call_count == 1 for r in rows)
            assert await get_cost_by_address_prefixes(db, org_id="absent", address_prefixes=selected) == []
            captured = []

            def capture(conn, cursor, sql, params, context, many):
                # Capture the actual call, including a query with a derived
                # outer SELECT. SQL projection spelling must not hide reads.
                captured.append((sql, params))

            event.listen(engine.sync_engine, "before_cursor_execute", capture)
            try:
                await get_cost_by_address_prefixes(db, org_id="fixture-a", address_prefixes=["flow_1", "café"])
            finally:
                event.remove(engine.sync_engine, "before_cursor_execute", capture)
            assert len(captured) == 1
            connection = await db.connection()
            raw = (await connection.get_raw_connection()).driver_connection
            sql, params = captured[0]
            # EXPLAIN with bound parameters alone can still produce a custom plan.
            # A named prepared statement plus force_generic_plan exercises the
            # cached plan that production connections eventually select.
            await raw.execute("PREPARE cost_prefix_probe AS " + sql)
            arguments = ",".join(
                str(literal(value).compile(dialect=db.get_bind().dialect, compile_kwargs={"literal_binds": True})) for value in params
            )
            try:
                plan = (await raw.fetchval("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) EXECUTE cost_prefix_probe(" + arguments + ")"))[0]["Plan"]
            finally:
                await raw.execute("DEALLOCATE cost_prefix_probe")

            def nodes(node):
                yield node
                for child in node.get("Plans", []):
                    yield from nodes(child)

            ranges = [node for node in nodes(plan) if "~>=~" in node.get("Index Cond", "") and "~<~" in node.get("Index Cond", "")]
            assert ranges, "Prepared queries must restrict the indexed subtree, not filter the tenant ledger"
            assert not any(node.get("Rows Removed by Filter", 0) >= 20000 for node in nodes(plan))
    finally:
        await engine.dispose()
