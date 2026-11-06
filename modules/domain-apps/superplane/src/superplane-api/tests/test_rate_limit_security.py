import uuid

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.middleware.auth import create_access_token
from app.middleware.rate_limit import RateLimitMiddleware


def limited_app(limit=3, capacity=100):
    app = FastAPI()

    @app.get("/workspaces/{workspace_id}")
    async def absent(workspace_id: str):
        from fastapi.responses import JSONResponse

        return JSONResponse({"detail": "Not found"}, status_code=404)

    middleware = RateLimitMiddleware(app, requests_per_minute=limit, max_buckets=capacity)
    return middleware


@pytest.mark.asyncio
async def test_workspace_and_token_rotation_do_not_reset_verified_principal_quota():
    org, user = uuid.uuid4(), uuid.uuid4()
    app = limited_app()
    async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
        for i in range(3):
            token, _ = create_access_token(org, user_id=user, role=("viewer", "editor", "admin")[i])
            assert (await client.get(f"/workspaces/{uuid.uuid4()}", headers={"Authorization": f"Bearer {token}"})).status_code == 404
        token, _ = create_access_token(org, user_id=user)
        assert (await client.get("/workspaces/another", headers={"Authorization": f"Bearer {token}"})).status_code == 429
        other, _ = create_access_token(org, user_id=uuid.uuid4())
        assert (await client.get("/workspaces/another", headers={"Authorization": f"Bearer {other}"})).status_code == 404
    assert len(app._buckets) == 2


@pytest.mark.asyncio
async def test_unverified_tokens_and_resource_ids_cannot_mint_buckets():
    app = limited_app(limit=5)
    async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
        for i in range(50):
            response = await client.get(f"/workspaces/{i}", headers={"Authorization": f"Bearer forged-{i}"})
            assert response.status_code == (404 if i < 5 else 429)
    assert len(app._buckets) == 1


@pytest.mark.asyncio
async def test_capacity_and_expiry_preserve_live_buckets(monkeypatch):
    from app.middleware import rate_limit

    clock = [100.0]
    monkeypatch.setattr(rate_limit.time, "monotonic", lambda: clock[0])
    app = limited_app(limit=1, capacity=2)
    for host, expected in [("192.0.2.1", 404), ("192.0.2.2", 404), ("192.0.2.3", 429), ("192.0.2.1", 429)]:
        async with AsyncClient(transport=ASGITransport(app, client=(host, 123)), base_url="http://test") as client:
            assert (await client.get("/workspaces/a")).status_code == expected
    assert len(app._buckets) == 2
    clock[0] += 61
    async with AsyncClient(transport=ASGITransport(app, client=("192.0.2.3", 123)), base_url="http://test") as client:
        assert (await client.get("/workspaces/a")).status_code == 404
    assert len(app._buckets) == 1
