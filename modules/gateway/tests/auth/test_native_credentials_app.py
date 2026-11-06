"""Exercise native credential routes through the complete Gateway middleware."""

import pytest
from httpx import ASGITransport, AsyncClient

from src.app import create_app
from tests.auth.test_native_credentials_postgres import CONFIG, create_body, native, pg_server, pg_url  # noqa: F401


async def test_disabled_native_routes_remain_diagnosable(monkeypatch):
    monkeypatch.delenv("ADP_NATIVE_CREDENTIALS_ENABLED", raising=False)
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url=CONFIG.issuer) as client:
        response = await client.get("/auth/native-credentials/config")
        assert response.status_code == 503
        assert response.json() == {"detail": {"error": "native_credentials_unavailable"}}
        assert response.headers["cache-control"] == "no-store"
        assert (await client.get("/health")).status_code == 200


@pytest.mark.parametrize("client_id", ["native-client", "browser-client"])
async def test_app_keeps_native_and_browser_authority_distinct(native, client_id):  # noqa: F811
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url=CONFIG.issuer) as client:
        response = await client.post(
            "/auth/native-credentials/requests",
            headers={"Authorization": "Bearer " + native.token(client_id)},
            json=create_body(),
        )
        if client_id == "browser-client":
            assert response.status_code == 401
        else:
            assert response.status_code == 200
            result = response.json()
            status = await client.get(
                "/auth/native-credentials/requests/" + result["receipt_id"],
                headers={"Authorization": "Bearer " + native.token(jti="fresh-session")},
            )
            assert status.status_code == 200
            assert status.json() == {key: value for key, value in result.items() if key != "approval_url"}
        assert native.providers.effects == []
        assert response.headers["cache-control"] == "no-store"
