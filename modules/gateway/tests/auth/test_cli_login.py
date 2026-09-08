# tests/auth/test_cli_login.py
"""Tests for the web CLI login flow (src/auth/cli_login.py).

The flow replaces the "Reveal refresh token and paste it" panel: the CLI
starts a pending request, the signed-in browser user approves it, and the CLI
polls to redeem tokens minted on the CLI-specific app client. These tests
drive the real router against in-memory SQLite with the Cognito minter and
JWT validation stubbed at the dependency seams.
"""

import time
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.cli_login import (
    _get_cognito_claims,
    _start_times,
    get_token_minter,
    router,
)
from src.auth.cognito_jwt import CognitoTokenClaims
from src.shared.database import get_db
from src.shared.models.cli_auth import CliAuthRequest

CLI_CLIENT_ID = "cli-client-test-123"
USER_POOL_ID = "us-east-1_testpool"


def _make_claims(username: str = "github_12345", sub: str = "sub-uuid-1") -> CognitoTokenClaims:
    now = int(time.time())
    return CognitoTokenClaims(
        sub=sub,
        iss="https://cognito-idp.us-east-1.amazonaws.com/us-east-1_testpool",
        client_id="spa-client",
        token_use="access",
        exp=now + 3600,
        iat=now,
        username=username,
    )


class StubMinter:
    """Records mint calls; can be told to fail."""

    def __init__(self, fail_times: int = 0) -> None:
        self.calls: list[tuple[str, str]] = []
        self._fail_times = fail_times

    def mint(self, username: str, cli_client_id: str) -> dict[str, Any]:
        self.calls.append((username, cli_client_id))
        if self._fail_times > 0:
            self._fail_times -= 1
            raise RuntimeError("cognito exploded")
        return {
            "AccessToken": "minted-access",
            "IdToken": "minted-id",
            "RefreshToken": "minted-refresh",
            "ExpiresIn": 3600,
        }


class StubSettings:
    cognito_cli_client_id = CLI_CLIENT_ID
    cognito_user_pool_id = USER_POOL_ID
    aws_region = "us-east-1"


def _make_app(
    db_session: AsyncSession,
    minter: StubMinter,
    claims: CognitoTokenClaims | None = None,
) -> TestClient:
    app = FastAPI()
    app.include_router(router)

    async def _get_db():
        yield db_session

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_token_minter] = lambda: minter
    app.dependency_overrides[_get_cognito_claims] = lambda: claims or _make_claims()

    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _configured_settings(monkeypatch):
    """Point the router at a configured CLI client and reset the rate limiter."""
    monkeypatch.setattr("src.auth.cli_login.get_settings", lambda: StubSettings())
    _start_times.clear()
    yield
    _start_times.clear()


@pytest.fixture
def minter() -> StubMinter:
    return StubMinter()


async def _get_row(db_session: AsyncSession, user_code: str) -> CliAuthRequest | None:
    result = await db_session.execute(select(CliAuthRequest).where(CliAuthRequest.user_code == user_code))
    return result.scalars().first()


def _start(client: TestClient) -> dict:
    response = client.post("/auth/cli/start")
    assert response.status_code == 200, response.text
    return response.json()


def _approve(client: TestClient, user_code: str, action: str = "approve") -> Any:
    return client.post("/auth/cli/approve", json={"user_code": user_code, "action": action})


def _poll(client: TestClient, device_code: str) -> Any:
    return client.post("/auth/cli/token", json={"device_code": device_code})


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------


class TestStart:
    @pytest.mark.asyncio
    async def test_start_creates_pending_request(self, db_session: AsyncSession, minter: StubMinter) -> None:
        client = _make_app(db_session, minter)
        body = _start(client)

        assert body["verification_path"] == f"/cli-auth?code={body['user_code']}"
        assert body["interval"] >= 1
        assert len(body["device_code"]) >= 48
        # The user_code is short and legible; the device_code is not guessable.
        assert len(body["user_code"]) == 9  # XXXX-XXXX

        row = await _get_row(db_session, body["user_code"])
        assert row is not None
        assert row.status == "pending"
        # Only the hash of the device_code is at rest.
        assert body["device_code"] not in (row.device_code_hash or "")

    def test_start_503_when_not_configured(self, db_session: AsyncSession, minter: StubMinter, monkeypatch) -> None:
        class Unconfigured(StubSettings):
            cognito_cli_client_id = ""

        monkeypatch.setattr("src.auth.cli_login.get_settings", lambda: Unconfigured())
        client = _make_app(db_session, minter)
        response = client.post("/auth/cli/start")
        assert response.status_code == 503
        assert response.json()["detail"]["error"] == "cli_login_not_configured"

    def test_start_rate_limited_per_ip(self, db_session: AsyncSession, minter: StubMinter) -> None:
        client = _make_app(db_session, minter)
        for _ in range(10):
            assert client.post("/auth/cli/start").status_code == 200
        response = client.post("/auth/cli/start")
        assert response.status_code == 429


# ---------------------------------------------------------------------------
# /approve
# ---------------------------------------------------------------------------


class TestApprove:
    @pytest.mark.asyncio
    async def test_approve_records_the_browser_user(self, db_session: AsyncSession, minter: StubMinter) -> None:
        client = _make_app(db_session, minter, claims=_make_claims(username="github_777", sub="sub-777"))
        body = _start(client)

        response = _approve(client, body["user_code"])
        assert response.status_code == 200
        assert response.json() == {"status": "approved"}

        row = await _get_row(db_session, body["user_code"])
        assert row.status == "approved"
        assert row.approved_username == "github_777"
        assert row.approved_sub == "sub-777"

    def test_approve_normalizes_user_code(self, db_session: AsyncSession, minter: StubMinter) -> None:
        client = _make_app(db_session, minter)
        body = _start(client)
        sloppy = body["user_code"].replace("-", "").lower()
        assert _approve(client, sloppy).status_code == 200

    def test_password_user_cannot_approve(self, db_session: AsyncSession, minter: StubMinter) -> None:
        """A native-password user must be refused: minting resets the password."""
        client = _make_app(db_session, minter, claims=_make_claims(username="alice@example.com"))
        body = _start(client)
        response = _approve(client, body["user_code"])
        assert response.status_code == 403
        assert response.json()["detail"]["error"] == "password_login_required"

    def test_password_user_can_still_deny(self, db_session: AsyncSession, minter: StubMinter) -> None:
        client = _make_app(db_session, minter, claims=_make_claims(username="alice@example.com"))
        body = _start(client)
        assert _approve(client, body["user_code"], action="deny").status_code == 200

    def test_unknown_code_is_404(self, db_session: AsyncSession, minter: StubMinter) -> None:
        client = _make_app(db_session, minter)
        assert _approve(client, "ZZZZ-9999").status_code == 404

    @pytest.mark.asyncio
    async def test_expired_code_is_404(self, db_session: AsyncSession, minter: StubMinter) -> None:
        client = _make_app(db_session, minter)
        body = _start(client)
        row = await _get_row(db_session, body["user_code"])
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await db_session.commit()
        assert _approve(client, body["user_code"]).status_code == 404

    def test_double_decision_is_409(self, db_session: AsyncSession, minter: StubMinter) -> None:
        client = _make_app(db_session, minter)
        body = _start(client)
        assert _approve(client, body["user_code"]).status_code == 200
        assert _approve(client, body["user_code"]).status_code == 409


# ---------------------------------------------------------------------------
# /token (poll + redeem)
# ---------------------------------------------------------------------------


class TestToken:
    def test_pending_is_202(self, db_session: AsyncSession, minter: StubMinter) -> None:
        client = _make_app(db_session, minter)
        body = _start(client)
        response = _poll(client, body["device_code"])
        assert response.status_code == 202
        assert response.json()["detail"]["error"] == "authorization_pending"

    @pytest.mark.asyncio
    async def test_full_happy_path_mints_on_the_cli_client(self, db_session: AsyncSession, minter: StubMinter) -> None:
        client = _make_app(db_session, minter, claims=_make_claims(username="GitHub_42", sub="sub-42"))
        body = _start(client)
        assert _approve(client, body["user_code"]).status_code == 200

        response = _poll(client, body["device_code"])
        assert response.status_code == 200
        tokens = response.json()
        assert tokens["access_token"] == "minted-access"
        assert tokens["refresh_token"] == "minted-refresh"
        assert tokens["client_id"] == CLI_CLIENT_ID
        assert tokens["user_pool_id"] == USER_POOL_ID
        assert tokens["region"] == "us-east-1"
        # Minted for the approving user, on the CLI client.
        assert minter.calls == [("GitHub_42", CLI_CLIENT_ID)]

        row = await _get_row(db_session, body["user_code"])
        assert row.status == "consumed"

    def test_redeem_is_single_use(self, db_session: AsyncSession, minter: StubMinter) -> None:
        client = _make_app(db_session, minter)
        body = _start(client)
        _approve(client, body["user_code"])
        assert _poll(client, body["device_code"]).status_code == 200
        replay = _poll(client, body["device_code"])
        assert replay.status_code == 410
        assert len(minter.calls) == 1

    def test_denied_is_403(self, db_session: AsyncSession, minter: StubMinter) -> None:
        client = _make_app(db_session, minter)
        body = _start(client)
        _approve(client, body["user_code"], action="deny")
        assert _poll(client, body["device_code"]).status_code == 403

    @pytest.mark.asyncio
    async def test_expired_is_410(self, db_session: AsyncSession, minter: StubMinter) -> None:
        client = _make_app(db_session, minter)
        body = _start(client)
        row = await _get_row(db_session, body["user_code"])
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await db_session.commit()
        assert _poll(client, body["device_code"]).status_code == 410

    def test_wrong_device_code_is_410(self, db_session: AsyncSession, minter: StubMinter) -> None:
        client = _make_app(db_session, minter)
        _start(client)
        assert _poll(client, "x" * 64).status_code == 410

    @pytest.mark.asyncio
    async def test_mint_failure_is_retryable(self, db_session: AsyncSession) -> None:
        """A Cognito hiccup must not burn the approval — the next poll retries."""
        flaky = StubMinter(fail_times=1)
        client = _make_app(db_session, flaky)
        body = _start(client)
        _approve(client, body["user_code"])

        first = _poll(client, body["device_code"])
        assert first.status_code == 502
        assert first.json()["detail"]["error"] == "mint_failed"

        row = await _get_row(db_session, body["user_code"])
        assert row.status == "approved"  # given back

        second = _poll(client, body["device_code"])
        assert second.status_code == 200
