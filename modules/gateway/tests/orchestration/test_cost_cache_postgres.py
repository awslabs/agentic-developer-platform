"""Real PostgreSQL cost snapshots shared across independent Redis clients."""

# ruff: noqa: F811 -- imported pytest fixtures are requested by the tests

import asyncio
from decimal import Decimal
from unittest.mock import Mock

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.orchestration import cost_cache
from src.orchestration.cost import get_cost_by_address
from src.orchestration.routes import _flow_read_snapshot
from tests.orchestration.test_cost import _usage
from tests.orchestration.test_cost_cache import caches  # noqa: F401
from tests.orchestration.test_cost_postgres import cost_session, pg_server, pg_url  # noqa: F401


async def test_concurrent_committed_display_reads_share_sql_while_raw_reader_stays_current(cost_session, caches, monkeypatch):
    cost_session.add_all(
        [
            _usage(org_id="org-a", address="flow/e/w/one", cost="0.000001", tokens=3),
            _usage(org_id="org-a", address="flow/e/w/zero", cost="0", tokens=0),
            _usage(org_id="org-b", address="flow/e/w/one", cost="900", tokens=90),
        ]
    )
    await cost_session.commit()
    factory = async_sessionmaker(cost_session.bind, expire_on_commit=False)
    statements = []

    def capture(_conn, _cursor, statement, _params, _context, _many):
        if "usage_logs" in statement and statement.lstrip().startswith("SELECT"):
            statements.append(statement)

    event.listen(cost_session.bind.sync_engine, "before_cursor_execute", capture)
    try:

        async def display(cache, org="org-a"):
            async with factory() as db, _flow_read_snapshot(db) as snapshot:
                assert (await snapshot.execute(text("SHOW transaction_read_only"))).scalar() == "on"

                async def load(prefixes):
                    return await cost_cache.get_cost_by_address_prefixes(snapshot, org_id=org, address_prefixes=prefixes)

                return await cache.get(org_id=org, prefixes=["flow"], load=load, snapshot_started=snapshot.info[cost_cache.SNAPSHOT_STARTED])

        results = await asyncio.gather(*(display(caches[i % 12]) for i in range(50)))
        assert all(result == results[0] for result in results)
        assert {row.amount_usd for row in results[0]} == {Decimal("0.000001"), Decimal(0)}
        assert sum(row.call_count for row in results[0]) == 2
        assert len(statements) == 1
        assert (await display(caches[3], "org-b"))[0].amount_usd == Decimal(900)

        cost_session.add(_usage(org_id="org-a", address="flow/e/w/one", cost="1.234567", tokens=5))
        await cost_session.commit()
        assert await display(caches[4]) == results[0]
        fresh = await get_cost_by_address(cost_session, org_id="org-a", address_prefix="flow")
        assert sum(row.amount_usd for row in fresh) == Decimal("1.234568")
        assert sum(row.call_count for row in fresh) == 3
        assert sum(row.total_tokens for row in fresh) == 8

        # A caller transaction without the read-only display marker bypasses the
        # cache, including its own uncommitted changes; it cannot publish them.
        cost_session.add(_usage(org_id="org-a", address="flow/e/w/one", cost="99"))
        await cost_session.flush()
        factory_spy = Mock(side_effect=AssertionError("Uncommitted reader must not access shared cache"))
        monkeypatch.setattr(cost_cache, "display_cost_cache", factory_spy)
        current = await cost_cache.get_display_costs(cost_session, org_id="org-a", address_prefixes=["flow"])
        assert sum(row.amount_usd for row in current) == Decimal("100.234568")
        factory_spy.assert_not_called()
        await cost_session.rollback()
        assert await display(caches[5]) == results[0]
    finally:
        event.remove(cost_session.bind.sync_engine, "before_cursor_execute", capture)
