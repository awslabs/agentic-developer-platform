"""Real display routes share only cost data; authorization and admission stay live."""

# ruff: noqa: F811 -- imported pytest fixtures are requested by the test

import time
from decimal import Decimal
from unittest.mock import AsyncMock

from httpx import ASGITransport, AsyncClient

from src.orchestration import cost_cache
from src.orchestration.cost import get_cost_by_address
from tests.orchestration.test_cost import _usage
from tests.orchestration.test_cost_cache import caches  # noqa: F401
from tests.orchestration.test_list_flows import (
    ORG_A,
    ORG_B,
    app_with_router,  # noqa: F401
    client_for,
    seed_flow,
    seed_node,
    session,  # noqa: F401
)


async def test_list_detail_share_cache_but_permissions_scope_and_raw_cost_are_current(session, app_with_router, caches, monkeypatch):
    flow = await seed_flow(session, slug="flow")
    await seed_node(session, flow, node_ref="story", epic="epic", wave="wave")
    other = await seed_flow(session, slug="flow", org_id=ORG_B)
    await seed_node(session, other, node_ref="story", epic="epic", wave="wave", org_id=ORG_B)
    usage = _usage(org_id=ORG_A, address="flow/epic/wave/story", cost="0.000001")
    session.add_all([usage, _usage(org_id=ORG_B, address="flow/epic/wave/story", cost="900")])
    await session.commit()

    # This fixture uses SQLite; explicitly mark this test-owned display snapshot
    # to exercise the cache through the real routes in the same event loop.
    session.info[cost_cache.SNAPSHOT_STARTED] = time.monotonic()
    monkeypatch.setattr(cost_cache, "display_cost_cache", lambda: caches[0])
    cache_calls = AsyncMock(wraps=caches[0].get)
    monkeypatch.setattr(caches[0], "get", cache_calls)
    ledger_reads = AsyncMock(wraps=cost_cache.get_cost_by_address_prefixes)
    monkeypatch.setattr(cost_cache, "get_cost_by_address_prefixes", ledger_reads)
    client_for(app_with_router).close()
    async with AsyncClient(transport=ASGITransport(app=app_with_router), base_url="http://test") as client:
        listing = await client.get("/orchestration/flows")
        assert listing.status_code == 200
        assert Decimal(listing.json()["flows"][0]["delivery_cost"]["amount_usd"]) == Decimal("0.000001")
        usage.cost_usd = Decimal("1.234567")
        await session.commit()
        detail = await client.get(f"/orchestration/flows/{flow.id}?include_activity=false")
        assert detail.status_code == 200
        assert Decimal(detail.json()["cost"]["amount_usd"]) == Decimal("0.000001")
        assert ledger_reads.await_count == 1

        # The explicit cost endpoint and the reader used by policy admission
        # still observe new committed ledger rows, even with a warm UI cache.
        current = await client.get(f"/orchestration/flows/{flow.id}/cost")
        assert current.status_code == 200
        assert Decimal(current.json()["amount_usd"]) == Decimal("1.234567")
        assert (await get_cost_by_address(session, org_id=ORG_A, address_prefix="flow"))[0].amount_usd == Decimal("1.234567")

        count = cache_calls.await_count
        client_for(app_with_router, permitted=False).close()
        assert (await client.get("/orchestration/flows")).status_code == 403
        assert (await client.get(f"/orchestration/flows/{flow.id}")).status_code == 403
        assert cache_calls.await_count == count

        client_for(app_with_router, org_id=ORG_B).close()
        assert (await client.get(f"/orchestration/flows/{flow.id}")).status_code == 404
        assert cache_calls.await_count == count
        foreign_list = await client.get("/orchestration/flows")
        assert foreign_list.status_code == 200
        assert Decimal(foreign_list.json()["flows"][0]["delivery_cost"]["amount_usd"]) == Decimal("900")
