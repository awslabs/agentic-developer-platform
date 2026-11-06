"""Budget mutations through the real CLI HTTP transport and gateway routes."""

import asyncio
import socket

import pytest
import uvicorn
from sqlalchemy import select

from src.shared.models.audit import AuditLog
from src.shared.models.budget import BudgetConfig
from tests.cli.test_adp_product_budget import product as cli
from tests.products.test_project_budgets import meter as meter_fixture
from tests.products.test_project_budgets import product as product_fixture

meter = meter_fixture
product = product_fixture


@pytest.fixture
async def budget_gateway(app, test_client, monkeypatch):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_config=None, access_log=False))
    running = asyncio.create_task(server.serve(sockets=[listener]))
    requests = []

    @app.middleware("http")
    async def record_request(request, call_next):
        requests.append((request.method, request.url.path))
        return await call_next(request)

    monkeypatch.setattr(cli.common, "gateway_url", lambda: f"http://127.0.0.1:{listener.getsockname()[1]}")
    monkeypatch.setattr(cli.common, "access_token", lambda: "synthetic-human-token")
    for key in ("ADP_CREW_TOOLS_URL", "ADP_CREW_TOOLS_TOKEN", "ADP_CREW_RUN_ID", "SIGV4_PROXY_TARGET"):
        monkeypatch.delenv(key, raising=False)
    try:
        async with asyncio.timeout(5):
            while not server.started:
                if running.done():
                    await running
                    raise AssertionError("Budget test server did not start")
                await asyncio.sleep(0.01)
        yield requests
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(running, 5)
        finally:
            listener.close()


async def test_mismatched_selection_cannot_mutate_budget(budget_gateway, product, meter, db_session, capsys):
    result = await asyncio.to_thread(
        cli.main,
        [
            "budget",
            "set",
            "--product",
            "other-product",
            "--project",
            product["project_id"],
            "--budget-usd",
            "250.00",
            "--reason",
            "Approved limit",
            "--json",
        ],
    )

    assert result == 5, capsys.readouterr().out
    assert not any(method == "PUT" for method, _ in budget_gateway)
    assert await db_session.scalar(select(BudgetConfig).where(BudgetConfig.entity_id == product["project_id"])) is None
    assert await db_session.scalar(select(AuditLog).where(AuditLog.event_type == "project_budget_changed")) is None


@pytest.mark.parametrize("reason", ["Approved — 预算增加 🚀", "🚀" * 500])
async def test_unicode_reason_survives_real_transport_and_audit(budget_gateway, product, meter, db_session, capsys, reason):
    result = await asyncio.to_thread(
        cli.main,
        [
            "budget",
            "set",
            "--product",
            product["product_id"],
            "--project",
            product["project_id"],
            "--budget-usd",
            "250.00",
            "--reason",
            reason,
            "--json",
        ],
    )

    assert result == 0, capsys.readouterr().out
    assert sum(method == "PUT" for method, _ in budget_gateway) == 1
    audit = await db_session.scalar(select(AuditLog).where(AuditLog.event_type == "project_budget_changed"))
    assert audit.details["reason"] == reason
    assert audit.details["budget_usd"] == "250.00"
