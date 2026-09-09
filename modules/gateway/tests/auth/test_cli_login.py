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

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.cli_login import (
    CliTokenMinter,
    _cognito_token_endpoint,
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
COGNITO_DOMAIN = "bedrockgw-test-auth"
TOKEN_ENDPOINT = f"https://{COGNITO_DOMAIN}.auth.us-east-1.amazoncognito.com/oauth2/token"


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
    """Records mint/refresh calls; can be told to fail."""

    def __init__(self, fail_times: int = 0, refresh_error: Exception | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.refresh_calls: list[tuple[str, str]] = []
        self._fail_times = fail_times
        self._refresh_error = refresh_error

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

    async def refresh(self, refresh_token: str, cli_client_id: str) -> dict[str, Any]:
        self.refresh_calls.append((refresh_token, cli_client_id))
        if self._refresh_error is not None:
            raise self._refresh_error
        return {
            "AccessToken": "refreshed-access",
            "IdToken": "refreshed-id",
            "RefreshToken": "rotated-refresh",
            "ExpiresIn": 3600,
        }


class StubSettings:
    cognito_cli_client_id = CLI_CLIENT_ID
    cognito_user_pool_id = USER_POOL_ID
    aws_region = "us-east-1"
    cognito_domain = COGNITO_DOMAIN


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


# ---------------------------------------------------------------------------
# /refresh (credential-less renewal through the gateway)
#
# The CLI app client has refresh-token ROTATION enabled, and BOTH
# AdminInitiateAuth and InitiateAuth reject REFRESH_TOKEN_AUTH on a rotating
# client with UnsupportedOperationException. The OAuth2 token endpoint is the
# only mechanism that works (#4873).
#
# These tests therefore drive the REAL CliTokenMinter against a mock token
# ENDPOINT via httpx.MockTransport. Stubbing the Cognito SDK — which is what the
# original tests did — is precisely what let the broken admin-API refresh ship
# green: the real API's rejection was never exercised.
# ---------------------------------------------------------------------------


class ExplodingCognitoClient:
    """Any attribute access is a test failure.

    Guard for #4873: refresh must never touch the cognito-idp SDK. If someone
    reintroduces `admin_initiate_auth`/`initiate_auth` on the refresh path, the
    attribute lookup raises here and the refresh tests fail loudly rather than
    passing against a friendly stub.
    """

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(
            f"refresh must use the OAuth2 token endpoint, not the cognito-idp SDK "
            f"(attempted call: {name!r}). Rotation-enabled clients cannot be "
            f"refreshed via Admin/InitiateAuth — see #4873."
        )


def _recording_minter(
    handler: Any,
    *,
    domain: str = COGNITO_DOMAIN,
    region: str = "us-east-1",
) -> tuple[CliTokenMinter, list[httpx.Request]]:
    """A real CliTokenMinter whose HTTP calls hit `handler` instead of Cognito."""
    seen: list[httpx.Request] = []

    def _record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    class _Settings(StubSettings):
        cognito_domain = domain
        aws_region = region

    minter = CliTokenMinter.__new__(CliTokenMinter)
    minter._user_pool_id = USER_POOL_ID
    minter._region = _Settings.aws_region
    minter._cognito_domain = _Settings.cognito_domain
    minter._client = ExplodingCognitoClient()
    minter._http_client = httpx.AsyncClient(transport=httpx.MockTransport(_record))
    return minter, seen


def _token_response(**overrides: Any) -> httpx.Response:
    body = {
        "access_token": "refreshed-access",
        "id_token": "refreshed-id",
        "refresh_token": "rotated-refresh",
        "expires_in": 3600,
        "token_type": "Bearer",
    }
    body.update(overrides)
    return httpx.Response(200, json=body)


class TestTokenEndpointUrl:
    """The URL must be right or every refresh 502s — same outage, silently."""

    def test_domain_prefix_gets_the_regional_host(self) -> None:
        assert _cognito_token_endpoint("bedrockgw-dev-auth-18057152", "us-east-1") == (
            "https://bedrockgw-dev-auth-18057152.auth.us-east-1.amazoncognito.com/oauth2/token"
        )

    def test_prefix_honours_the_configured_region(self) -> None:
        assert _cognito_token_endpoint("pool-prefix", "eu-west-2") == "https://pool-prefix.auth.eu-west-2.amazoncognito.com/oauth2/token"

    def test_custom_domain_fqdn_is_used_as_is(self) -> None:
        """A dot means custom domain — appending the regional suffix breaks DNS."""
        assert _cognito_token_endpoint("auth.example.com", "us-east-1") == "https://auth.example.com/oauth2/token"


class TestRefreshTokenEndpointContract:
    """CliTokenMinter.refresh against a mock token endpoint."""

    @pytest.mark.asyncio
    async def test_posts_form_encoded_grant_to_the_token_endpoint(self) -> None:
        minter, seen = _recording_minter(lambda _r: _token_response())

        result = await minter.refresh("the-refresh-token", CLI_CLIENT_ID)

        assert len(seen) == 1
        request = seen[0]
        assert str(request.url) == TOKEN_ENDPOINT
        assert request.method == "POST"
        assert request.headers["content-type"] == "application/x-www-form-urlencoded"

        form = dict(pair.split("=", 1) for pair in request.content.decode().split("&"))
        assert form["grant_type"] == "refresh_token"
        assert form["client_id"] == CLI_CLIENT_ID
        assert form["refresh_token"] == "the-refresh-token"
        # Public client (GenerateSecret=null): no secret, no SECRET_HASH.
        assert "client_secret" not in form
        assert "SECRET_HASH" not in form
        # No AWS credentials are needed, so no SigV4 signature is attached.
        assert "authorization" not in request.headers

        # Normalized to AuthenticationResult keys so the route mapping is unchanged.
        assert result == {
            "AccessToken": "refreshed-access",
            "IdToken": "refreshed-id",
            "ExpiresIn": 3600,
            "RefreshToken": "rotated-refresh",
        }

    @pytest.mark.asyncio
    async def test_omits_refresh_token_when_endpoint_returns_none(self) -> None:
        """Never synthesize a rotated token the endpoint did not hand back."""
        body = {"access_token": "a", "id_token": "i", "expires_in": 3600}
        minter, _ = _recording_minter(lambda _r: httpx.Response(200, json=body))

        result = await minter.refresh("rt", CLI_CLIENT_ID)
        assert "RefreshToken" not in result

    @pytest.mark.asyncio
    async def test_custom_domain_posts_to_that_host(self) -> None:
        minter, seen = _recording_minter(lambda _r: _token_response(), domain="auth.example.com")
        await minter.refresh("rt", CLI_CLIENT_ID)
        assert str(seen[0].url) == "https://auth.example.com/oauth2/token"

    @pytest.mark.asyncio
    async def test_invalid_grant_raises_expired(self) -> None:
        from src.auth.cli_login import CliRefreshExpiredError

        minter, _ = _recording_minter(lambda _r: httpx.Response(400, json={"error": "invalid_grant"}))
        with pytest.raises(CliRefreshExpiredError):
            await minter.refresh("dead-token", CLI_CLIENT_ID)

    @pytest.mark.asyncio
    async def test_server_error_raises_unavailable(self) -> None:
        from src.auth.cli_login import CliRefreshUnavailableError

        minter, _ = _recording_minter(lambda _r: httpx.Response(500, text="boom"))
        with pytest.raises(CliRefreshUnavailableError):
            await minter.refresh("rt", CLI_CLIENT_ID)

    @pytest.mark.asyncio
    async def test_200_without_access_token_raises_unavailable(self) -> None:
        from src.auth.cli_login import CliRefreshUnavailableError

        minter, _ = _recording_minter(lambda _r: httpx.Response(200, json={"expires_in": 3600}))
        with pytest.raises(CliRefreshUnavailableError):
            await minter.refresh("rt", CLI_CLIENT_ID)


def _endpoint_app(
    db_session: AsyncSession,
    handler: Any,
    *,
    domain: str = COGNITO_DOMAIN,
) -> tuple[TestClient, list[httpx.Request]]:
    """The real router + the real minter + a mock token endpoint."""
    minter, seen = _recording_minter(handler, domain=domain)
    return _make_app(db_session, minter), seen  # type: ignore[arg-type]


class TestRefreshRoute:
    """/auth/cli/refresh end-to-end over the mock token endpoint."""

    def _refresh(self, client: TestClient, token: str = "old-refresh-token-value") -> Any:
        return client.post("/auth/cli/refresh", json={"refresh_token": token})

    def test_refresh_renews_without_auth_header(self, db_session: AsyncSession) -> None:
        # No Authorization header: the access token is expired by the time the
        # CLI refreshes, so the refresh token in the body is the sole credential.
        client, seen = _endpoint_app(db_session, lambda _r: _token_response())
        response = self._refresh(client, token="the-refresh-token")
        assert response.status_code == 200, response.text

        tokens = response.json()
        assert tokens["token_type"] == "Bearer"
        assert tokens["access_token"] == "refreshed-access"
        assert tokens["id_token"] == "refreshed-id"
        # Rotation returns a NEW refresh token that the CLI must persist.
        assert tokens["refresh_token"] == "rotated-refresh"
        assert tokens["expires_in"] == 3600
        # Refreshed on the CLI app client, through the token endpoint.
        assert len(seen) == 1
        assert str(seen[0].url) == TOKEN_ENDPOINT

    def test_dead_refresh_token_is_401(self, db_session: AsyncSession) -> None:
        """A rotated-away / revoked token is terminal — the CLI must re-login."""
        client, _ = _endpoint_app(
            db_session,
            lambda _r: httpx.Response(400, json={"error": "invalid_grant", "error_description": "Invalid Refresh Token"}),
        )
        response = self._refresh(client)
        assert response.status_code == 401
        assert response.json()["detail"]["error"] == "refresh_expired"

    def test_server_error_is_502(self, db_session: AsyncSession) -> None:
        """A retryable hiccup maps to 502, not 401 — the CLI retries."""
        client, _ = _endpoint_app(db_session, lambda _r: httpx.Response(500, text="internal error"))
        response = self._refresh(client)
        assert response.status_code == 502
        assert response.json()["detail"]["error"] == "refresh_failed"

    def test_timeout_is_502(self, db_session: AsyncSession) -> None:
        def _timeout(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("token endpoint timed out", request=request)

        client, _ = _endpoint_app(db_session, _timeout)
        response = self._refresh(client)
        assert response.status_code == 502
        assert response.json()["detail"]["error"] == "refresh_failed"

    def test_connection_error_is_502(self, db_session: AsyncSession) -> None:
        def _refused(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        client, _ = _endpoint_app(db_session, _refused)
        assert self._refresh(client).status_code == 502

    def test_malformed_400_is_502_not_401(self, db_session: AsyncSession) -> None:
        """Only invalid_grant means "expired"; other 400s are our bug, so retryable-shaped."""
        client, _ = _endpoint_app(db_session, lambda _r: httpx.Response(400, json={"error": "invalid_request"}))
        response = self._refresh(client)
        assert response.status_code == 502
        assert response.json()["detail"]["error"] == "refresh_failed"

    def test_refresh_503_when_client_id_not_configured(self, db_session: AsyncSession, monkeypatch) -> None:
        class Unconfigured(StubSettings):
            cognito_cli_client_id = ""

        monkeypatch.setattr("src.auth.cli_login.get_settings", lambda: Unconfigured())
        client, seen = _endpoint_app(db_session, lambda _r: _token_response())
        response = self._refresh(client)
        assert response.status_code == 503
        assert response.json()["detail"]["error"] == "cli_login_not_configured"
        assert seen == []  # refused before any outbound call

    def test_refresh_503_when_domain_not_configured(self, db_session: AsyncSession, monkeypatch) -> None:
        """Without cognito_domain there is no token endpoint to POST to."""

        class NoDomain(StubSettings):
            cognito_domain = ""

        monkeypatch.setattr("src.auth.cli_login.get_settings", lambda: NoDomain())
        client, seen = _endpoint_app(db_session, lambda _r: _token_response(), domain="")
        response = self._refresh(client)
        assert response.status_code == 503
        assert response.json()["detail"]["error"] == "cli_login_not_configured"
        assert seen == []

    def test_refresh_never_calls_the_cognito_sdk(self, db_session: AsyncSession) -> None:
        """Guard for #4873: Admin/InitiateAuth cannot refresh a rotating client.

        The minter's SDK client raises on ANY attribute access, so if refresh
        reverts to admin_initiate_auth this returns 502 instead of 200.
        """
        client, seen = _endpoint_app(db_session, lambda _r: _token_response())
        assert self._refresh(client).status_code == 200
        assert len(seen) == 1  # exactly one outbound call: the token endpoint
