"""Keep WA peer reuse bound to the authorized gateway project."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from l5_services.publication.client import WellArchitectedConsumerClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.l5_integration import backend

SCOPE = {"tenant_id": "tenant-example", "product_id": "product-example", "project_id": "project-example"}


def consumer(destination, scope=SCOPE, *, reuse=True):
    prefix = "context" if destination == "product-context" else "insights"
    peer = {
        "kind": "well_architected_result",
        "schema_version": "1.0",
        "private_read_fencing": True,
        "task_fencing": reuse,
        "scope": scope,
        "path": f"/internal/v1/{prefix}/private-contributions",
        ("query_path" if prefix == "context" else "usage_path"): f"/internal/v1/{prefix}/private-reuse",
    }
    return WellArchitectedConsumerClient(object(), "private-delegation", scope=scope, destination=destination, peer_admission=peer)


@pytest.mark.asyncio
async def test_gateway_binds_both_admitted_consumers_to_only_the_current_project(monkeypatch):
    monkeypatch.delenv("L5_WORK_QUEUE_URL", raising=False)
    clients = {destination: consumer(destination) for destination in ("product-context", "engineering-insights")}
    resolved = []

    def resolver(scope):
        resolved.append(scope)
        return clients

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with async_sessionmaker(engine)() as session:
            actor = SimpleNamespace(org_id=SCOPE["tenant_id"], user_id="design-owner", account_type="human")
            request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(l5_wa_consumer_resolver=resolver)))
            gateway = backend("well_architected", request, session, actor, x_l5_delegation_id=None)
            run = SimpleNamespace(run_id="run-example")
            gateway.list_runs = AsyncMock(return_value=[run])
            gateway.run_source_rights = AsyncMock(return_value=[run])
            store = await gateway.result_store(SCOPE["product_id"], SCOPE["project_id"], "well_architected")
            assert store.wa_context_client is clients["product-context"]
            assert store.wa_insights_client is clients["engineering-insights"]
            assert store.actor_id == "design-owner"
            assert await store.wa_source_authority(run)
            assert resolved == [SCOPE]

            sibling = await gateway.result_store(SCOPE["product_id"], "sibling-project", "well_architected")
            assert sibling.wa_context_client is None and sibling.wa_insights_client is None
            gateway.list_runs = AsyncMock(return_value=[])
            assert (await gateway.result_store(SCOPE["product_id"], "not-visible", "well_architected")).wa_context_client is None
            assert resolved == [SCOPE, {**SCOPE, "project_id": "sibling-project"}]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_gateway_rejects_unadmitted_reuse_and_nonhuman_peer_access(monkeypatch):
    monkeypatch.delenv("L5_WORK_QUEUE_URL", raising=False)
    unavailable = {
        "product-context": consumer("product-context", reuse=False),
        "engineering-insights": consumer("engineering-insights", {**SCOPE, "project_id": "foreign"}),
    }
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with async_sessionmaker(engine)() as session:
            actor = SimpleNamespace(org_id=SCOPE["tenant_id"], user_id="design-owner", account_type="human")
            request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(l5_wa_consumer_resolver=lambda scope: unavailable)))
            gateway = backend("well_architected", request, session, actor, x_l5_delegation_id=None)
            gateway.list_runs = AsyncMock(return_value=[SimpleNamespace(run_id="run-example", service_id="well_architected")])
            store = await gateway.result_store(SCOPE["product_id"], SCOPE["project_id"], "well_architected")
            assert store.wa_context_client is None and store.wa_insights_client is None
            store.get = AsyncMock(return_value={"run_id": "run-example"})
            with pytest.raises(HTTPException) as denied:
                await store.use_wa_result("result-example", "later-task")
            assert denied.value.status_code == 404

            gateway.actor = SimpleNamespace(org_id=SCOPE["tenant_id"], user_id="service", account_type="service")
            store = await gateway.result_store(SCOPE["product_id"], SCOPE["project_id"], "well_architected")
            assert store.actor_id is None and store.wa_context_client is None
    finally:
        await engine.dispose()
