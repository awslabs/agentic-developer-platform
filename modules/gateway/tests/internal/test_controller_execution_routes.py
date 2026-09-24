"""The controller verifier consumes real shared leases and current ADP identity."""

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from src.internal import controller_execution_routes as routes
from src.internal.auth_deps import verify_internal_or_irsa
from tests.auth.test_vault_operation_authority_postgres import authority  # noqa: F401
from tests.migrations.conftest_postgres import pg_server, pg_url  # noqa: F401


@pytest.fixture
async def controller_client(authority, monkeypatch):  # noqa: F811
    factory, lease = authority
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[verify_internal_or_irsa] = lambda: None

    async def database():
        async with factory() as db:
            yield db

    app.dependency_overrides[routes.get_operation_db] = database
    monkeypatch.setattr(routes, "_granted_permissions", lambda request: frozenset({"workspace:provision"}))
    verifier = AsyncMock(return_value=("invocation#9", "org"))
    monkeypatch.setattr(routes, "_verified_executor", verifier)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, factory, lease, verifier


async def test_current_owned_lease_and_plan_return_without_credentials(controller_client):
    client, _, lease, verifier = controller_client
    response = await client.post("/internal/v1/controller-execution/authority", json={"operation_id": "operation"})
    assert response.status_code == 200
    result = response.json()
    assert result["holder"] == "invocation#9" and result["fence_token"] == lease.fence_token
    assert result["attempt_id"] == "execution-attempt" and result["job_id"] == "job"
    assert "token" not in result and "secret" not in result
    assert verifier.await_count == 2


@pytest.mark.parametrize("change", ["holder", "org", "expired", "closed", "budget", "digest"])
async def test_foreign_stale_or_unpaid_execution_refused(controller_client, change):
    client, factory, _, _ = controller_client
    statements = {
        "holder": "UPDATE harness_operation_leases SET holder='other#1'",
        "org": "UPDATE harness_operations SET org_id='other'",
        "expired": "UPDATE harness_operation_leases SET expires_at=clock_timestamp()-interval '1 second'",
        "closed": "UPDATE harness_operation_leases SET closed_at=clock_timestamp()",
        "budget": "UPDATE harness_approval_consumption SET reservation_state='released'",
        "digest": "UPDATE harness_operations SET request_payload='{}'",
    }
    async with factory() as db:
        await db.execute(text(statements[change]))
        await db.commit()
    response = await client.post("/internal/v1/controller-execution/authority", json={"operation_id": "operation"})
    assert response.status_code == 403


@pytest.mark.parametrize("change", ["run", "scope"])
async def test_revocation_during_database_read_refuses_response(controller_client, monkeypatch, change):
    client, _, _, verifier = controller_client
    if change == "run":
        verifier.side_effect = [("invocation#9", "org"), HTTPException(403, "revoked run")]
    else:
        permissions = iter([frozenset({"workspace:provision"}), frozenset()])
        monkeypatch.setattr(routes, "_granted_permissions", lambda request: next(permissions))
    response = await client.post("/internal/v1/controller-execution/authority", json={"operation_id": "operation"})
    assert response.status_code == 403


async def test_caller_cannot_supply_authority_claims(controller_client):
    client, _, _, verifier = controller_client
    response = await client.post("/internal/v1/controller-execution/authority", json={"operation_id": "operation", "holder": "invocation#9"})
    assert response.status_code == 422
    verifier.assert_not_awaited()
