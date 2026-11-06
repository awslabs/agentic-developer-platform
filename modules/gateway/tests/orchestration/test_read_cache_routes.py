"""Versioned list and graph projections keep live authorization and cost reads."""

# ruff: noqa: F811 -- imported pytest fixtures are requested by the tests

import asyncio
from decimal import Decimal

import fakeredis.aioredis
import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic.migration import MigrationContext
from alembic.operations import Operations
from src.orchestration import read_cache
from src.orchestration.repository import OrchestrationRepository
from src.orchestration.routes import router
from src.shared.database import get_db
from src.shared.exceptions import BedrockGatewayError
from src.shared.models.base import Base
from tests.migrations.conftest_postgres import pg_server, pg_url, to_async_url  # noqa: F401
from tests.orchestration.decision_projection_fixture import apply_migration, migration
from tests.orchestration.test_cost import _usage
from tests.orchestration.test_list_flows import ORG_A, ORG_B, client_for, seed_flow, seed_node


@pytest.fixture
async def route_database(pg_url, monkeypatch):
    engine = create_async_engine(to_async_url(pg_url))

    def create(connection):
        Base.metadata.create_all(connection, tables=[table for name, table in Base.metadata.tables.items() if name != "orchestration_read_revisions"])
        connection.commit()
        apply_migration(connection)
        context = MigrationContext.configure(connection)
        with Operations.context(context), context.begin_transaction():
            migration("143_flow_read_revisions").upgrade()

    async with engine.connect() as connection:
        await connection.run_sync(create)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    cache = read_cache.FlowReadCache(fakeredis.aioredis.FakeRedis(), "route-database")
    monkeypatch.setattr(read_cache, "flow_read_cache", lambda: cache)
    app = FastAPI()
    app.include_router(router)

    @app.exception_handler(BedrockGatewayError)
    async def gateway_error(_request: Request, exc: BedrockGatewayError):
        return JSONResponse(status_code=exc.status_code, content={"error": exc.error})

    async def request_db():
        async with factory() as db:
            db.info["http_request_session"] = True
            yield db

    app.dependency_overrides[get_db] = request_db
    client_for(app).close()
    async with factory() as db, AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield db, client, app
    await cache.close()
    await engine.dispose()


async def test_list_reuses_structure_but_costs_permissions_and_worker_changes_are_current(route_database, monkeypatch):
    db, client, app = route_database
    flow = await seed_flow(db, slug="flow")
    node = await seed_node(db, flow, node_ref="story", epic="epic", wave="wave")
    usage = _usage(org_id=ORG_A, address="flow/epic/wave/story", cost="1")
    db.add(usage)
    await db.commit()
    original = OrchestrationRepository.list_flows_page_with_aggregates
    calls = 0

    async def count(self, **kwargs):
        nonlocal calls
        calls += 1
        return await original(self, **kwargs)

    monkeypatch.setattr(OrchestrationRepository, "list_flows_page_with_aggregates", count)
    first = await client.get("/orchestration/flows")
    assert first.status_code == 200, first.text
    assert Decimal(first.json()["flows"][0]["delivery_cost"]["amount_usd"]) == 1
    usage.cost_usd = Decimal(2)
    await db.commit()
    second = await client.get("/orchestration/flows")
    assert second.status_code == 200, second.text
    assert calls == 1, "Warm structure must avoid repeating the aggregate query"
    assert Decimal(second.json()["flows"][0]["delivery_cost"]["amount_usd"]) == 2

    # A background worker's raw SQL must invalidate without a UI/API callback.
    await db.execute(text("UPDATE orchestration_nodes SET state='passed' WHERE id=:id"), {"id": node.id})
    await db.commit()
    updated = await client.get("/orchestration/flows")
    assert calls == 2
    assert updated.json()["flows"][0]["completed_story_count"] == 1
    graph = await client.get(f"/orchestration/flows/{flow.id}?include_activity=false")
    assert graph.status_code == 200, graph.text
    assert graph.json()["nodes"][0]["state"] == "passed"

    client_for(app, org_id=ORG_B).close()
    assert (await client.get(f"/orchestration/flows/{flow.id}?include_activity=false")).status_code == 404
    foreign = await client.get("/orchestration/flows")
    assert foreign.json()["flows"] == []
    client_for(app, permitted=False).close()
    assert (await client.get("/orchestration/flows")).status_code == 403


async def test_cancelled_cost_query_rolls_back_without_hiding_flows(route_database, monkeypatch):
    from src.orchestration import cost_cache

    db, client, _app = route_database
    flow = await seed_flow(db, slug="cost-timeout")
    await seed_node(db, flow, node_ref="story", epic="epic", wave="wave")
    db.add(_usage(org_id=ORG_A, address="cost-timeout/epic/wave/story", cost="1.25"))
    await db.commit()
    cache = cost_cache.DisplayCostCache(None, "cost-timeout-test")
    monkeypatch.setattr(cost_cache, "display_cost_cache", lambda: cache)
    monkeypatch.setattr(cost_cache, "LOAD_SECONDS", 0.05)
    original = cost_cache.get_cost_by_address_prefixes

    async def slow_ledger(snapshot, **_kwargs):
        await snapshot.execute(text("SELECT pg_sleep(1)"))
        raise AssertionError("The ledger read should have timed out")

    monkeypatch.setattr(cost_cache, "get_cost_by_address_prefixes", slow_ledger)
    response = await client.get("/orchestration/flows")
    assert response.status_code == 200
    result = response.json()
    assert result["total"] == 1
    assert result["flows"][0]["total_nodes"] == 1
    cost = result["flows"][0]["delivery_cost"]
    assert cost["status"] == "unknown" and cost["amount_usd"] is None
    assert cost["reason"] == "cost_refresh_unavailable"

    monkeypatch.setattr(cost_cache, "get_cost_by_address_prefixes", original)
    monkeypatch.setattr(cost_cache, "LOAD_SECONDS", 6)
    async with asyncio.timeout(3):
        response = await client.get("/orchestration/flows")
    assert response.status_code == 200
    cost = response.json()["flows"][0]["delivery_cost"]
    assert cost["status"] == "known" and Decimal(cost["amount_usd"]) == Decimal("1.25")


@pytest.mark.parametrize("include_activity", [False, True])
@pytest.mark.parametrize("failure", ["lease_busy", "query_cancelled"])
async def test_unavailable_cost_keeps_graph_and_recovers(route_database, monkeypatch, caplog, include_activity, failure):
    from src.orchestration import cost_cache

    db, client, app = route_database
    flow = await seed_flow(db, slug="graph-cost-unavailable")
    story = await seed_node(db, flow, node_ref="story", epic="epic", wave="wave", state="passed")
    gate = await seed_node(db, flow, node_ref="gate", epic="epic", wave="wave", kind="gate")
    db.add(_usage(org_id=ORG_A, address="graph-cost-unavailable/epic/wave/story", cost="1.25"))
    await db.commit()
    cache = cost_cache.DisplayCostCache(fakeredis.aioredis.FakeRedis(), "graph-cost-unavailable")
    monkeypatch.setattr(cost_cache, "display_cost_cache", lambda: cache)
    original = cost_cache.get_cost_by_address_prefixes
    lease, _ = cache.keys(ORG_A, [flow.slug])
    if failure == "lease_busy":
        await cache.client.set(lease, "another-request", px=8000)
        monkeypatch.setattr(cost_cache, "WAIT_SECONDS", 0.05)
    else:
        monkeypatch.setattr(cost_cache, "LOAD_SECONDS", 0.05)

        async def slow_ledger(snapshot, **_kwargs):
            await snapshot.execute(text("SELECT pg_sleep(1)"))
            raise AssertionError("The ledger read should have timed out")

        monkeypatch.setattr(cost_cache, "get_cost_by_address_prefixes", slow_ledger)

    path = f"/orchestration/flows/{flow.id}?include_activity={str(include_activity).lower()}"
    response = await client.get(path)
    assert response.status_code == 200, response.text
    graph = response.json()
    assert graph["execution_paused"] is True
    assert graph["activity_included"] is include_activity
    nodes = {node["id"]: node for node in graph["nodes"]}
    assert nodes[story.id]["state"] == "passed"
    assert nodes[gate.id]["state"] == "pending"
    assert nodes[story.id]["cost"]["reason"] == "cost_refresh_unavailable"
    assert nodes[gate.id]["cost"]["reason"] == "not_costable"
    cost = graph["cost"]
    assert cost["status"] == "unknown" and cost["amount_usd"] is None
    assert cost["reason"] == "cost_refresh_unavailable"
    assert cost["partial"] is True and cost["unknown_node_count"] == cost["node_count"] == 2
    assert all(node["cost"]["amount_usd"] is None for node in graph["nodes"])
    assert cost["nodes"] == [node["cost"] for node in graph["nodes"]]
    diagnostic = next(record for record in caplog.records if getattr(record, "event", None) == "flow_display_cost_unavailable")
    assert diagnostic.reason == "cost_refresh_unavailable" and diagnostic.flow_id == flow.id

    # A display fallback cannot turn missing flow or permission into success.
    client_for(app, org_id=ORG_B).close()
    assert (await client.get(path)).status_code == 404
    client_for(app, permitted=False).close()
    assert (await client.get(path)).status_code == 403
    client_for(app).close()

    await cache.client.delete(lease)
    monkeypatch.setattr(cost_cache, "get_cost_by_address_prefixes", original)
    monkeypatch.setattr(cost_cache, "LOAD_SECONDS", 6)
    async with asyncio.timeout(3):
        recovered = await client.get(path)
    assert recovered.status_code == 200, recovered.text
    cost = recovered.json()["cost"]
    assert cost["status"] == "known" and Decimal(cost["amount_usd"]) == Decimal("1.25")
    assert {node["id"]: node["state"] for node in recovered.json()["nodes"]} == {story.id: "passed", gate.id: "pending"}
    await cache.close()


@pytest.mark.parametrize("source", ["structure", "cost"])
async def test_graph_preserves_unrelated_service_errors(route_database, monkeypatch, source):
    from unittest.mock import AsyncMock

    from src.orchestration import routes

    db, client, _app = route_database
    flow = await seed_flow(db, slug="unrelated-failure")
    await db.commit()
    fail = AsyncMock(side_effect=HTTPException(503, detail={"code": "unrelated_dependency_unavailable"}))
    if source == "structure":
        monkeypatch.setattr(OrchestrationRepository, "get_flow", fail)
    else:
        monkeypatch.setattr(routes, "get_display_flow_cost", fail)
    response = await client.get(f"/orchestration/flows/{flow.id}?include_activity=false")
    assert response.status_code == 503
    assert response.json() == {"detail": {"code": "unrelated_dependency_unavailable"}}
