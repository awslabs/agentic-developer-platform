"""Tests for /internal/v1/user-credentials, proxy-request, credential-materialize,
and credential-raw-read endpoints.

Issue #136: Vault Phase 3 — service-to-service endpoints + delivery paths

Coverage:
  credential_injector.py:
    - inject_credential: bearer / oauth_token → Authorization: Bearer
    - inject_credential: api_key → Authorization: ApiKey
    - inject_credential: basic_auth → Authorization: Basic base64
    - inject_credential: does not mutate input dict
    - inject_credential: file type raises UnsupportedCredentialTypeError
    - inject_credential: unknown type raises UnsupportedCredentialTypeError

  GET /internal/v1/user-credentials:
    - happy path returns metadata list
    - secret_arn is absent from response
    - unknown user → 404
    - missing API key → 403

  POST /internal/v1/proxy-request:
    - bearer injection + successful upstream → 200
    - api_key injection verified in forwarded headers
    - basic_auth injection verified
    - last_used_at updated after proxy call
    - audit log entry written
    - upstream HTTP error (502) propagates cleanly

  POST /internal/v1/credential-materialize:
    - file type (ssh_key) → 201 + presigned URL
    - certificate type → 201
    - non-file type → 400
    - missing scope → 403
    - missing materialization bucket → 503
    - last_used_at updated

  POST /internal/v1/credential-raw-read:
    - happy path returns value
    - feature flag disabled → 403
    - missing scope → 403
    - every call writes audit log
    - last_used_at updated
"""

from __future__ import annotations

import asyncio
import base64
import socket
from contextlib import nullcontext
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.internal.credential_egress import (
    allowed_hosts_for,
    host_matches,
    is_binding_enforced,
)
from src.internal.credential_injector import (
    FILE_CREDENTIAL_TYPES,
    UnsupportedCredentialTypeError,
    inject_credential,
)
from src.internal.credential_routes import _validate_proxy_url, get_secrets_manager, router
from src.shared.config import Settings
from src.shared.database import get_db
from src.shared.models.audit import AuditLog
from src.shared.models.base import Base
from src.shared.models.organization import Department, Organization, Team, User
from src.shared.models.vault import UserCredential
from tests.auth.proxy_stream import stream_from_request

# ---------------------------------------------------------------------------
# Test infrastructure
# ---------------------------------------------------------------------------

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"
_VALID_KEY = "test-internal-api-key"


def _make_engine():
    return create_async_engine(
        TEST_DB_URL,
        echo=False,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )


@pytest.fixture(scope="module")
def event_loop():
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
async def engine():
    eng = _make_engine()
    async with eng.begin() as conn:
        import src.shared.models.audit  # noqa: F401
        import src.shared.models.vault  # noqa: F401

        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture
async def db(engine) -> AsyncSession:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        org = Organization(
            id="org-test",
            name="Test Org",
            aws_accounts=[],
            role_mappings={},
            settings={},
            github_installation_ids=[],
            cognito_client_ids=[],
        )
        dept = Department(id="dept-eng", org_id="org-test", name="Engineering")
        team = Team(id="team-eng", org_id="org-test", department_id="dept-eng", name="Eng")
        alice = User(
            id="user-alice",
            org_id="org-test",
            team_id="team-eng",
            email="alice@test.com",
        )
        session.add_all([org, dept, team, alice])
        await session.commit()
        yield session


def _make_app(db_session: AsyncSession, mock_sm=None, *, token_context=None, user="user-alice") -> TestClient:
    """Build a minimal FastAPI test app with the credential router.

    Issue #6050: when token_context is provided, the auth override sets it on
    request.state to simulate a verified IRSA identity with registry-granted
    credential_scopes.  This is required for the raw-read and materialize
    endpoints, which now check credential_scopes from token_context instead
    of the X-Agent-Scopes header.
    """
    app = FastAPI()
    app.include_router(router)

    async def _get_db():
        yield db_session

    app.dependency_overrides[get_db] = _get_db

    # Auth: override verify_internal_or_irsa with a faithful stand-in that
    # enforces the shared-key check against _VALID_KEY. The real dependency
    # reads settings.internal_api_key inside src.internal.auth_deps (NOT in
    # credential_routes/routes), so the per-test `patch(...get_settings)` calls
    # never reached it — every call 503'd ("not configured"). Overriding the
    # dependency itself is the robust fix and still exercises the 403 path.
    from fastapi import Header, HTTPException

    from src.internal.auth_deps import verify_internal_or_irsa

    if token_context is not None:
        # Issue #6050: when a token_context is provided, use middleware to set
        # it on request.state before the endpoint runs.
        from starlette.middleware.base import BaseHTTPMiddleware

        class _TokenContextMiddleware(BaseHTTPMiddleware):
            async def dispatch(self, request, call_next):
                request.state.token_context = token_context
                return await call_next(request)

        app.add_middleware(_TokenContextMiddleware)

    async def _verify(request: Request, x_internal_api_key: str | None = Header(default=None)) -> None:
        if x_internal_api_key != _VALID_KEY:
            raise HTTPException(status_code=403, detail={"error": "forbidden"})
        from src.internal.credential_binding import BindingResult

        request.state.agent_credential_binding = BindingResult(user, True, False, user, "delivery-run", "org-test")

    app.dependency_overrides[verify_internal_or_irsa] = _verify

    if mock_sm is not None:
        app.dependency_overrides[get_secrets_manager] = lambda: mock_sm
    return TestClient(app, raise_server_exceptions=False)


def _fake_token_context(*, credential_scopes: list[str] | None = None) -> MagicMock:
    """Issue #6050: build a minimal mock TokenContext for registry scope gating."""
    ctx = MagicMock()
    ctx.credential_scopes = credential_scopes if credential_scopes is not None else []
    ctx.scope = "internal"
    ctx.user_id = "worker-test"
    ctx.org_id = "org-test"
    ctx.requires_run_identity = False
    return ctx


def _settings_mock(
    *,
    raw_read_enabled: bool = False,
    bucket: str = "test-bucket",
    proxy_host_allowlist: str = "api.github.com,api.openai.com,*.atlassian.net,api.stripe.com,slack.com,jira.example.com,bad.host.invalid",
    proxy_require_https: bool = True,
    enforce_host_binding: bool = True,
) -> MagicMock:
    s = MagicMock()
    s.internal_api_key = _VALID_KEY
    s.vault_raw_read_enabled = raw_read_enabled
    s.vault_materialization_bucket = bucket
    s.aws_region = "us-east-1"
    s.vault_proxy_host_allowlist = proxy_host_allowlist
    s.vault_proxy_require_https = proxy_require_https
    # Match the enforcing application default; shadow tests opt out explicitly.
    s.vault_enforce_credential_host_binding = enforce_host_binding
    # Issue #3175: credential binding defaults to shadow mode (off).
    s.enforce_credential_binding = False
    s.webhook_events_table = "adp-test-webhook-events"
    return s


async def _seed_credential(
    db: AsyncSession,
    *,
    cred_id: str = "cred-1",
    service: str = "github",
    label: str = "default",
    credential_type: str = "bearer",
    secret_arn: str = "arn:aws:secretsmanager:us-east-1:123:secret:adp/users/alice/github-abc123",
) -> UserCredential:
    cred = UserCredential(
        agent_consent=True,
        id=cred_id,
        org_id="org-test",
        user_id="user-alice",
        service=service,
        label=label,
        credential_type=credential_type,
        secret_arn=secret_arn,
    )
    db.add(cred)
    await db.commit()
    await db.refresh(cred)
    return cred


# ---------------------------------------------------------------------------
# Unit tests: credential_injector
# ---------------------------------------------------------------------------


class TestCredentialInjector:
    def test_bearer_injects_authorization_bearer(self):
        result = inject_credential("bearer", "my-token", {})
        assert result["Authorization"] == "Bearer my-token"

    def test_oauth_token_injects_authorization_bearer(self):
        result = inject_credential("oauth_token", "oauth-tok", {})
        assert result["Authorization"] == "Bearer oauth-tok"

    def test_api_key_injects_authorization_apikey(self):
        result = inject_credential("api_key", "key-abc", {})
        assert result["Authorization"] == "ApiKey key-abc"

    def test_basic_auth_injects_authorization_basic_base64(self):
        value = "user:p@$$w0rd"
        expected = base64.b64encode(value.encode()).decode()
        result = inject_credential("basic_auth", value, {})
        assert result["Authorization"] == f"Basic {expected}"

    def test_does_not_mutate_original_headers(self):
        original = {"X-Custom": "yes"}
        inject_credential("bearer", "tok", original)
        assert "Authorization" not in original

    def test_preserves_existing_headers(self):
        result = inject_credential("api_key", "k", {"Accept": "application/json"})
        assert result["Accept"] == "application/json"
        assert result["Authorization"] == "ApiKey k"

    def test_file_type_raises_unsupported_error(self):
        for ftype in FILE_CREDENTIAL_TYPES:
            with pytest.raises(UnsupportedCredentialTypeError, match="materialize"):
                inject_credential(ftype, "val", {})

    def test_unknown_type_raises_unsupported_error(self):
        with pytest.raises(UnsupportedCredentialTypeError, match="Unknown"):
            inject_credential("magic_wand", "val", {})


# ---------------------------------------------------------------------------
# GET /internal/v1/user-credentials
# ---------------------------------------------------------------------------


class TestListUserCredentials:
    @patch("src.internal.credential_routes.get_settings")
    def test_returns_metadata_list(self, mock_settings, db: AsyncSession):
        mock_settings.return_value = _settings_mock()

        asyncio.get_event_loop().run_until_complete(_seed_credential(db, cred_id="cred-list-1", service="openai", label="chat"))

        with nullcontext():
            client = _make_app(db)
            resp = client.get(
                "/internal/v1/user-credentials",
                params={"user_id": "user-alice", "invocation_id": "delivery-run", "service": "openai"},
                headers={"X-Internal-Api-Key": _VALID_KEY},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) >= 1
        item = next(d for d in data if d["id"] == "cred-list-1")
        assert item["service"] == "openai"
        assert item["label"] == "chat"
        assert item["credential_type"] == "bearer"
        # secret_arn must NOT be in the response
        assert "secret_arn" not in item

    @patch("src.internal.credential_routes.get_settings")
    def test_unknown_user_returns_404(self, mock_settings, db: AsyncSession):
        mock_settings.return_value = _settings_mock()

        with nullcontext():
            client = _make_app(db, user="user-nobody")
            resp = client.get(
                "/internal/v1/user-credentials",
                params={"invocation_id": "delivery-run", "user_id": "user-nobody", "service": "github"},
                headers={"X-Internal-Api-Key": _VALID_KEY},
            )
        assert resp.status_code == 404

    def test_missing_api_key_returns_403(self, db: AsyncSession):
        with nullcontext():
            client = _make_app(db)
            resp = client.get(
                "/internal/v1/user-credentials",
                params={"user_id": "user-alice", "invocation_id": "delivery-run", "service": "github"},
            )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# POST /internal/v1/proxy-request
# ---------------------------------------------------------------------------


def _fake_getaddrinfo_public(host, port, *args, **kwargs):
    """Return a fake public IP for any hostname (avoids real DNS in tests)."""
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]


def _fake_getaddrinfo_private(host, port, *args, **kwargs):
    """Return a private IP (simulates SSRF to internal address)."""
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 0))]


class TestProxyRequest:
    def _mock_sm(self, secret_value: str) -> MagicMock:
        sm = MagicMock()
        sm.get_secret.return_value = secret_value
        return sm

    @patch("src.internal.credential_routes.socket.getaddrinfo", _fake_getaddrinfo_public)
    @patch("src.internal.credential_routes.get_settings")
    def test_bearer_injection_and_successful_upstream(self, mock_settings, db: AsyncSession):
        mock_settings.return_value = _settings_mock()

        asyncio.get_event_loop().run_until_complete(
            _seed_credential(db, cred_id="cred-proxy-bearer", service="github", label="pat", credential_type="bearer")
        )
        mock_sm = self._mock_sm("secret-token-abc")

        # Mock the httpx call.
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "application/json"}
        mock_response.text = '{"id": 42}'

        with (
            patch("httpx.AsyncClient") as mock_client_cls,
        ):
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.request = AsyncMock(return_value=mock_response)
            mock_client.stream = lambda **kwargs: stream_from_request(mock_client, **kwargs)
            mock_client_cls.return_value = mock_client

            client = _make_app(db, mock_sm=mock_sm)
            resp = client.post(
                "/internal/v1/proxy-request",
                json={
                    "user_id": "user-alice",
                    "invocation_id": "delivery-run",
                    "agent_id": "agent-001",
                    "task_id": "task-001",
                    "service": "github",
                    "label": "pat",
                    "method": "GET",
                    "url": "https://api.github.com/user",
                    "headers": {"hOsT": "attacker.example"},
                },
                headers={"X-Internal-Api-Key": _VALID_KEY},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == 200
        assert "provenance_id" in body
        assert body["body"] == '{"id": 42}'
        mock_client_cls.assert_called_once_with(timeout=30.0, trust_env=False, follow_redirects=False)
        forwarded = mock_client.request.call_args.kwargs
        assert str(forwarded["url"]) == "https://93.184.216.34/user"
        assert forwarded["headers"]["Host"] == "api.github.com"
        assert "hOsT" not in forwarded["headers"]
        assert forwarded["extensions"] == {"sni_hostname": "api.github.com"}

    @patch("src.internal.credential_routes.socket.getaddrinfo", _fake_getaddrinfo_public)
    @patch("src.internal.credential_routes.get_settings")
    def test_api_key_injected_into_forwarded_headers(self, mock_settings, db: AsyncSession):
        mock_settings.return_value = _settings_mock()

        asyncio.get_event_loop().run_until_complete(
            _seed_credential(db, cred_id="cred-proxy-apikey", service="openai", label="default", credential_type="api_key")
        )
        mock_sm = self._mock_sm("sk-openai-key")

        captured_headers: dict = {}

        async def _fake_request(*, method, url, headers=None, content=None, extensions=None):
            captured_headers.update(headers or {})
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.headers = {}
            mock_response.text = "{}"
            return mock_response

        with (
            patch("httpx.AsyncClient") as mock_client_cls,
        ):
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.request = _fake_request
            mock_client.stream = lambda **kwargs: stream_from_request(mock_client, **kwargs)
            mock_client_cls.return_value = mock_client

            client = _make_app(db, mock_sm=mock_sm)
            resp = client.post(
                "/internal/v1/proxy-request",
                json={
                    "user_id": "user-alice",
                    "invocation_id": "delivery-run",
                    "agent_id": "agent-001",
                    "task_id": "task-002",
                    "service": "openai",
                    "label": "default",
                    "method": "POST",
                    "url": "https://api.openai.com/v1/chat/completions",
                    "body": "{}",
                },
                headers={"X-Internal-Api-Key": _VALID_KEY},
            )

        assert resp.status_code == 200
        assert captured_headers.get("Authorization") == "ApiKey sk-openai-key"

    @patch("src.internal.credential_routes.socket.getaddrinfo", _fake_getaddrinfo_public)
    @patch("src.internal.credential_routes.get_settings")
    def test_basic_auth_injection(self, mock_settings, db: AsyncSession):
        mock_settings.return_value = _settings_mock()

        asyncio.get_event_loop().run_until_complete(
            _seed_credential(
                db,
                cred_id="cred-proxy-basic",
                service="jira",
                label="default",
                credential_type="basic_auth",
            )
        )
        mock_sm = self._mock_sm("user:p4ssw0rd")

        captured_headers: dict = {}

        async def _fake_request(*, method, url, headers=None, content=None, extensions=None):
            captured_headers.update(headers or {})
            r = MagicMock()
            r.status_code = 200
            r.headers = {}
            r.text = "{}"
            return r

        with (
            patch("httpx.AsyncClient") as mock_client_cls,
        ):
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.request = _fake_request
            mock_client.stream = lambda **kwargs: stream_from_request(mock_client, **kwargs)
            mock_client_cls.return_value = mock_client

            client = _make_app(db, mock_sm=mock_sm)
            resp = client.post(
                "/internal/v1/proxy-request",
                json={
                    "user_id": "user-alice",
                    "invocation_id": "delivery-run",
                    "agent_id": "agent-001",
                    "task_id": "task-003",
                    "service": "jira",
                    "method": "GET",
                    "url": "https://jira.example.com/rest/api/2/issue/PROJ-1",
                },
                headers={"X-Internal-Api-Key": _VALID_KEY},
            )

        assert resp.status_code == 200
        expected = "Basic " + base64.b64encode(b"user:p4ssw0rd").decode()
        assert captured_headers.get("Authorization") == expected

    @patch("src.internal.credential_routes.socket.getaddrinfo", _fake_getaddrinfo_public)
    @patch("src.internal.credential_routes.get_settings")
    def test_last_used_at_updated(self, mock_settings, db: AsyncSession):
        mock_settings.return_value = _settings_mock()

        asyncio.get_event_loop().run_until_complete(
            _seed_credential(
                db,
                cred_id="cred-proxy-lastused",
                service="stripe",
                label="default",
                credential_type="bearer",
            )
        )
        mock_sm = self._mock_sm("sk-stripe")

        r = MagicMock()
        r.status_code = 200
        r.headers = {}
        r.text = "{}"

        with (
            patch("httpx.AsyncClient") as mock_client_cls,
        ):
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.request = AsyncMock(return_value=r)
            mock_client.stream = lambda **kwargs: stream_from_request(mock_client, **kwargs)
            mock_client_cls.return_value = mock_client

            client = _make_app(db, mock_sm=mock_sm)
            client.post(
                "/internal/v1/proxy-request",
                json={
                    "user_id": "user-alice",
                    "invocation_id": "delivery-run",
                    "agent_id": "agent-001",
                    "task_id": "task-lastused",
                    "service": "stripe",
                    "method": "GET",
                    "url": "https://api.stripe.com/v1/charges",
                },
                headers={"X-Internal-Api-Key": _VALID_KEY},
            )

        async def _check():
            stmt = select(UserCredential).where(UserCredential.id == "cred-proxy-lastused")
            result = await db.execute(stmt)
            cred = result.scalar_one()
            assert cred.last_used_at is not None

        asyncio.get_event_loop().run_until_complete(_check())

    @patch("src.internal.credential_routes.socket.getaddrinfo", _fake_getaddrinfo_public)
    @patch("src.internal.credential_routes.get_settings")
    def test_audit_log_written(self, mock_settings, db: AsyncSession):
        mock_settings.return_value = _settings_mock()

        asyncio.get_event_loop().run_until_complete(
            _seed_credential(
                db,
                cred_id="cred-proxy-audit",
                service="slack",
                label="default",
                credential_type="bearer",
            )
        )
        mock_sm = self._mock_sm("xoxb-slack-token")

        r = MagicMock()
        r.status_code = 200
        r.headers = {}
        r.text = '{"ok": true}'

        with (
            patch("httpx.AsyncClient") as mock_client_cls,
        ):
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.request = AsyncMock(return_value=r)
            mock_client.stream = lambda **kwargs: stream_from_request(mock_client, **kwargs)
            mock_client_cls.return_value = mock_client

            client = _make_app(db, mock_sm=mock_sm)
            client.post(
                "/internal/v1/proxy-request",
                json={
                    "user_id": "user-alice",
                    "invocation_id": "delivery-run",
                    "agent_id": "agent-audit",
                    "task_id": "task-audit",
                    "service": "slack",
                    "method": "POST",
                    "url": "https://slack.com/api/chat.postMessage",
                },
                headers={"X-Internal-Api-Key": _VALID_KEY},
            )

        async def _check():
            stmt = select(AuditLog).where(
                AuditLog.event_type == "vault_proxy_request",
                AuditLog.org_id == "org-test",
            )
            result = await db.execute(stmt)
            logs = result.scalars().all()
            assert len(logs) >= 1
            log = logs[-1]
            assert log.details["agent_id"] == "agent-audit"
            assert log.details["service"] == "slack"
            assert "provenance_id" in log.details

        asyncio.get_event_loop().run_until_complete(_check())

    @patch("src.internal.credential_routes.socket.getaddrinfo", _fake_getaddrinfo_public)
    @patch("src.internal.credential_routes.get_settings")
    def test_upstream_network_error_returns_502(self, mock_settings, db: AsyncSession):
        mock_settings.return_value = _settings_mock()

        asyncio.get_event_loop().run_until_complete(
            _seed_credential(
                db,
                cred_id="cred-proxy-502",
                service="badhost",
                label="default",
                credential_type="api_key",
            )
        )
        mock_sm = self._mock_sm("key")

        import httpx as _httpx

        with (
            patch("httpx.AsyncClient") as mock_client_cls,
        ):
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.request = AsyncMock(side_effect=_httpx.ConnectError("Connection refused"))
            mock_client.stream = lambda **kwargs: stream_from_request(mock_client, **kwargs)
            mock_client_cls.return_value = mock_client

            client = _make_app(db, mock_sm=mock_sm)
            resp = client.post(
                "/internal/v1/proxy-request",
                json={
                    "user_id": "user-alice",
                    "invocation_id": "delivery-run",
                    "agent_id": "agent-001",
                    "task_id": "task-502",
                    "service": "badhost",
                    "method": "GET",
                    "url": "https://bad.host.invalid/api",
                },
                headers={"X-Internal-Api-Key": _VALID_KEY},
            )

        assert resp.status_code == 502
        detail = resp.json()["detail"]
        assert detail["error"] == "upstream_error"


# ---------------------------------------------------------------------------
# POST /internal/v1/proxy-request — Host Allowlist Validation (Issue #1158)
# ---------------------------------------------------------------------------


class TestProxyRequestAllowlist:
    """Tests for the SSRF host allowlist on /internal/v1/proxy-request."""

    def _mock_sm(self, secret_value: str) -> MagicMock:
        sm = MagicMock()
        sm.get_secret.return_value = secret_value
        return sm

    @patch("src.internal.credential_routes.socket.getaddrinfo", _fake_getaddrinfo_public)
    @patch("src.internal.credential_routes.get_settings")
    def test_allowed_host_passes(self, mock_settings, db: AsyncSession):
        """Request to an allowlisted host succeeds (positive test)."""
        mock_settings.return_value = _settings_mock(proxy_host_allowlist="api.github.com")

        asyncio.get_event_loop().run_until_complete(_seed_credential(db, cred_id="cred-allowlist-pass", service="github", credential_type="bearer"))
        mock_sm = self._mock_sm("tok")

        r = MagicMock()
        r.status_code = 200
        r.headers = {}
        r.text = "{}"

        with (
            patch("httpx.AsyncClient") as mock_client_cls,
        ):
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.request = AsyncMock(return_value=r)
            mock_client.stream = lambda **kwargs: stream_from_request(mock_client, **kwargs)
            mock_client_cls.return_value = mock_client

            client = _make_app(db, mock_sm=mock_sm)
            resp = client.post(
                "/internal/v1/proxy-request",
                json={
                    "user_id": "user-alice",
                    "invocation_id": "delivery-run",
                    "agent_id": "agent-001",
                    "task_id": "task-al-pass",
                    "service": "github",
                    "method": "GET",
                    "url": "https://api.github.com/user",
                },
                headers={"X-Internal-Api-Key": _VALID_KEY},
            )

        assert resp.status_code == 200

    @patch("src.internal.credential_routes.socket.getaddrinfo", _fake_getaddrinfo_public)
    @patch("src.internal.credential_routes.get_settings")
    def test_denied_host_returns_403(self, mock_settings, db: AsyncSession):
        """Request to a non-allowlisted host is rejected with 403."""
        mock_settings.return_value = _settings_mock(proxy_host_allowlist="api.github.com")

        asyncio.get_event_loop().run_until_complete(_seed_credential(db, cred_id="cred-allowlist-deny", service="evil", credential_type="bearer"))

        with nullcontext():
            client = _make_app(db)
            resp = client.post(
                "/internal/v1/proxy-request",
                json={
                    "user_id": "user-alice",
                    "invocation_id": "delivery-run",
                    "agent_id": "agent-001",
                    "task_id": "task-al-deny",
                    "service": "evil",
                    "method": "GET",
                    "url": "https://attacker.example.com/exfil",
                },
                headers={"X-Internal-Api-Key": _VALID_KEY},
            )

        assert resp.status_code == 403
        detail = resp.json()["detail"]
        assert detail["error"] == "proxy_host_denied"
        assert "attacker.example.com" in detail["message"]

    @patch("src.internal.credential_routes.socket.getaddrinfo", _fake_getaddrinfo_private)
    @patch("src.internal.credential_routes.get_settings")
    def test_private_ip_returns_400(self, mock_settings, db: AsyncSession):
        """Request resolving to a private/link-local IP is rejected (anti-SSRF)."""
        mock_settings.return_value = _settings_mock(proxy_host_allowlist="metadata.internal")

        asyncio.get_event_loop().run_until_complete(_seed_credential(db, cred_id="cred-allowlist-priv", service="aws", credential_type="bearer"))

        with nullcontext():
            client = _make_app(db)
            resp = client.post(
                "/internal/v1/proxy-request",
                json={
                    "user_id": "user-alice",
                    "invocation_id": "delivery-run",
                    "agent_id": "agent-001",
                    "task_id": "task-al-priv",
                    "service": "aws",
                    "method": "GET",
                    "url": "https://metadata.internal/latest/meta-data/",
                },
                headers={"X-Internal-Api-Key": _VALID_KEY},
            )

        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert detail["error"] == "invalid_url"
        assert "private" in detail["message"].lower()

    @patch("src.internal.credential_routes.socket.getaddrinfo", _fake_getaddrinfo_public)
    @patch("src.internal.credential_routes.get_settings")
    def test_http_url_rejected_when_https_required(self, mock_settings, db: AsyncSession):
        """HTTP URL is rejected when vault_proxy_require_https=True."""
        mock_settings.return_value = _settings_mock(proxy_host_allowlist="api.github.com", proxy_require_https=True)

        asyncio.get_event_loop().run_until_complete(_seed_credential(db, cred_id="cred-allowlist-http", service="github", credential_type="bearer"))

        with nullcontext():
            client = _make_app(db)
            resp = client.post(
                "/internal/v1/proxy-request",
                json={
                    "user_id": "user-alice",
                    "invocation_id": "delivery-run",
                    "agent_id": "agent-001",
                    "task_id": "task-al-http",
                    "service": "github",
                    "method": "GET",
                    "url": "http://api.github.com/user",
                },
                headers={"X-Internal-Api-Key": _VALID_KEY},
            )

        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert detail["error"] == "invalid_url"
        assert "https" in detail["message"].lower()

    @patch("src.internal.credential_routes.socket.getaddrinfo", _fake_getaddrinfo_public)
    @patch("src.internal.credential_routes.get_settings")
    def test_empty_allowlist_returns_403_fail_closed(self, mock_settings, db: AsyncSession):
        """Empty allowlist means ALL proxy-requests are denied (fail-closed)."""
        mock_settings.return_value = _settings_mock(proxy_host_allowlist="")

        asyncio.get_event_loop().run_until_complete(_seed_credential(db, cred_id="cred-allowlist-empty", service="github", credential_type="bearer"))

        with nullcontext():
            client = _make_app(db)
            resp = client.post(
                "/internal/v1/proxy-request",
                json={
                    "user_id": "user-alice",
                    "invocation_id": "delivery-run",
                    "agent_id": "agent-001",
                    "task_id": "task-al-empty",
                    "service": "github",
                    "method": "GET",
                    "url": "https://api.github.com/user",
                },
                headers={"X-Internal-Api-Key": _VALID_KEY},
            )

        assert resp.status_code == 403
        detail = resp.json()["detail"]
        assert detail["error"] == "proxy_host_denied"
        assert "no proxy host allowlist" in detail["message"].lower()

    @patch("src.internal.credential_routes.socket.getaddrinfo", _fake_getaddrinfo_public)
    @patch("src.internal.credential_routes.get_settings")
    def test_embedded_credentials_in_url_rejected(self, mock_settings, db: AsyncSession):
        """URL with embedded user:pass is rejected."""
        mock_settings.return_value = _settings_mock(proxy_host_allowlist="api.github.com")

        asyncio.get_event_loop().run_until_complete(_seed_credential(db, cred_id="cred-allowlist-creds", service="github", credential_type="bearer"))

        with nullcontext():
            client = _make_app(db)
            resp = client.post(
                "/internal/v1/proxy-request",
                json={
                    "user_id": "user-alice",
                    "invocation_id": "delivery-run",
                    "agent_id": "agent-001",
                    "task_id": "task-al-creds",
                    "service": "github",
                    "method": "GET",
                    "url": "https://user:pass@api.github.com/user",
                },
                headers={"X-Internal-Api-Key": _VALID_KEY},
            )

        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert detail["error"] == "invalid_url"
        assert "embedded credentials" in detail["message"].lower()

    @patch("src.internal.credential_routes.socket.getaddrinfo", _fake_getaddrinfo_public)
    @patch("src.internal.credential_routes.get_settings")
    def test_wildcard_suffix_match_positive(self, mock_settings, db: AsyncSession):
        """Wildcard *.atlassian.net matches sub.atlassian.net."""
        mock_settings.return_value = _settings_mock(proxy_host_allowlist="*.atlassian.net")

        asyncio.get_event_loop().run_until_complete(_seed_credential(db, cred_id="cred-allowlist-wild-pos", service="jira", credential_type="bearer"))
        mock_sm = self._mock_sm("tok")

        r = MagicMock()
        r.status_code = 200
        r.headers = {}
        r.text = "{}"

        with (
            patch("httpx.AsyncClient") as mock_client_cls,
        ):
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.request = AsyncMock(return_value=r)
            mock_client.stream = lambda **kwargs: stream_from_request(mock_client, **kwargs)
            mock_client_cls.return_value = mock_client

            client = _make_app(db, mock_sm=mock_sm)
            resp = client.post(
                "/internal/v1/proxy-request",
                json={
                    "user_id": "user-alice",
                    "invocation_id": "delivery-run",
                    "agent_id": "agent-001",
                    "task_id": "task-al-wild-pos",
                    "service": "jira",
                    "method": "GET",
                    "url": "https://myorg.atlassian.net/rest/api/2/issue/PROJ-1",
                },
                headers={"X-Internal-Api-Key": _VALID_KEY},
            )

        assert resp.status_code == 200

    @patch("src.internal.credential_routes.socket.getaddrinfo", _fake_getaddrinfo_public)
    @patch("src.internal.credential_routes.get_settings")
    def test_wildcard_suffix_match_negative(self, mock_settings, db: AsyncSession):
        """Wildcard *.atlassian.net does NOT match evil-atlassian.net."""
        mock_settings.return_value = _settings_mock(proxy_host_allowlist="*.atlassian.net")

        asyncio.get_event_loop().run_until_complete(_seed_credential(db, cred_id="cred-allowlist-wild-neg", service="jira", credential_type="bearer"))

        with nullcontext():
            client = _make_app(db)
            resp = client.post(
                "/internal/v1/proxy-request",
                json={
                    "user_id": "user-alice",
                    "invocation_id": "delivery-run",
                    "agent_id": "agent-001",
                    "task_id": "task-al-wild-neg",
                    "service": "jira",
                    "method": "GET",
                    "url": "https://evil-atlassian.net/exfil",
                },
                headers={"X-Internal-Api-Key": _VALID_KEY},
            )

        assert resp.status_code == 403
        detail = resp.json()["detail"]
        assert detail["error"] == "proxy_host_denied"


# ---------------------------------------------------------------------------
# Issue #4076: credential -> host egress binding
# ---------------------------------------------------------------------------


class TestHostMatcher:
    """T5 — pin the EXISTING correct matcher behaviour.

    These pass pre-fix, and that is the point: they are the anti-regression pin
    for extracting the matcher out of _validate_proxy_url into
    credential_egress.host_matches. Not the failing-test evidence for the fix.
    """

    @pytest.mark.parametrize(
        ("hostname", "expected"),
        [
            ("myorg.atlassian.net", True),  # wildcard subdomain
            ("atlassian.net", True),  # bare apex
            ("foo.bar.atlassian.net", True),  # multi-label subdomain
            ("evil-atlassian.net", False),  # THE bypass — must stay denied
            ("myorg.atlassian.net.", False),  # trailing-dot FQDN — fails closed
            ("x.atlassian.net.evil.com", False),  # suffix-in-the-middle
            ("notatlassian.net", False),
        ],
    )
    def test_wildcard_matcher_table(self, hostname: str, expected: bool):
        assert host_matches(hostname, frozenset({"*.atlassian.net"})) is expected

    @pytest.mark.parametrize(
        ("hostname", "expected"),
        [
            ("api.github.com", True),
            ("API.GITHUB.COM", True),  # case-insensitive
            ("evil.api.github.com", False),  # exact pattern is not a suffix match
            ("api.github.com.evil.com", False),
        ],
    )
    def test_exact_matcher_table(self, hostname: str, expected: bool):
        assert host_matches(hostname, frozenset({"api.github.com"})) is expected

    def test_empty_pattern_set_matches_nothing(self):
        assert host_matches("api.github.com", frozenset()) is False


class TestCredentialEgressMap:
    """Unit tests for the service -> host binding map."""

    def test_mapped_service_is_enforced(self):
        assert is_binding_enforced("github") is True

    def test_unmapped_free_form_service_is_not_enforced(self):
        # `service` is deliberately free-form; custom values must stay unbound.
        assert is_binding_enforced("custom-api-foo") is False
        assert allowed_hosts_for("custom-api-foo") == frozenset()

    def test_deployment_specific_services_stay_unmapped(self):
        # jira/atlassian and aws host sets are per-deployment — binding them in a
        # process-global constant would break tenants. Wave 3 handles them.
        assert is_binding_enforced("jira") is False
        assert is_binding_enforced("aws") is False

    def test_lookup_is_case_insensitive(self):
        assert is_binding_enforced("GitHub") is True
        assert allowed_hosts_for("GITHUB") == allowed_hosts_for("github")


class TestCredentialHostBinding:
    """Credential -> host egress binding on POST /internal/v1/proxy-request.

    T1/T2/T3/T6/T8 all FAIL on pre-fix code (no binding exists, so every one of
    them gets a 200 with the github secret delivered to slack.com).
    """

    def _mock_sm(self, secret_value: str = "ghp_supersecret") -> MagicMock:
        sm = MagicMock()
        sm.get_secret.return_value = secret_value
        return sm

    def _post(
        self,
        db: AsyncSession,
        *,
        mock_sm: MagicMock,
        settings: Settings | MagicMock,
        body_service: str,
        url: str,
        task_id: str,
    ):
        """POST proxy-request with the upstream HTTP call mocked out."""
        r = MagicMock()
        r.status_code = 200
        r.headers = {}
        r.text = "{}"

        with (
            patch("src.internal.credential_routes.get_settings", return_value=settings),
            patch("httpx.AsyncClient") as mock_client_cls,
        ):
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.request = AsyncMock(return_value=r)
            mock_client.stream = lambda **kwargs: stream_from_request(mock_client, **kwargs)
            mock_client_cls.return_value = mock_client

            client = _make_app(db, mock_sm=mock_sm)
            return client.post(
                "/internal/v1/proxy-request",
                json={
                    "user_id": "user-alice",
                    "invocation_id": "delivery-run",
                    "agent_id": "agent-egress",
                    "task_id": task_id,
                    "service": body_service,
                    "method": "GET",
                    "url": url,
                },
                headers={"X-Internal-Api-Key": _VALID_KEY},
            )

    @pytest.mark.parametrize(
        "service,host,expected_status",
        [
            ("github", "api.github.com", 200),
            ("openai", "api.openai.com", 200),
            ("anthropic", "api.anthropic.com", 200),
            ("stripe", "api.stripe.com", 200),
            ("slack", "slack.com", 200),
            ("jira", "jira.example.com", 200),
            ("github", "slack.com", 403),
            ("openai", "slack.com", 403),
            ("anthropic", "slack.com", 403),
            ("stripe", "slack.com", 403),
            ("slack", "api.github.com", 403),
        ],
    )
    @patch("src.internal.credential_routes.socket.getaddrinfo", _fake_getaddrinfo_public)
    def test_default_configuration_binds_credentials(self, db, monkeypatch, service, host, expected_status):
        """Exercise the real default, including refusal before secret retrieval."""
        monkeypatch.delenv("BG_VAULT_ENFORCE_CREDENTIAL_HOST_BINDING", raising=False)
        settings = Settings(
            _env_file=None,
            internal_api_key=_VALID_KEY,
            vault_proxy_host_allowlist="api.github.com,api.openai.com,api.anthropic.com,api.stripe.com,slack.com,jira.example.com",
        )
        asyncio.get_event_loop().run_until_complete(_seed_credential(db, service=service))
        mock_sm = self._mock_sm()

        resp = self._post(
            db,
            mock_sm=mock_sm,
            settings=settings,
            body_service=service,
            url=f"https://{host}/test",
            task_id="task-default-binding",
        )

        assert resp.status_code == expected_status
        if expected_status == 403:
            assert resp.json()["detail"]["error"] == "credential_host_binding_denied"
            mock_sm.get_secret.assert_not_called()
        else:
            mock_sm.get_secret.assert_called_once()

    @patch("src.internal.credential_routes.socket.getaddrinfo", _fake_getaddrinfo_public)
    @patch("src.internal.credential_routes.get_settings")
    def test_github_credential_to_slack_denied(self, mock_settings, db: AsyncSession):
        """T1 (headline) — a github credential may not be sent to slack.com.

        Both hosts are allowlisted, so pre-fix this returns 200 and the github
        token is delivered to slack.com. That is the exploit.
        """
        settings = _settings_mock(enforce_host_binding=True)
        mock_settings.return_value = settings

        asyncio.get_event_loop().run_until_complete(_seed_credential(db, cred_id="cred-egress-t1", service="github", credential_type="bearer"))

        resp = self._post(
            db,
            mock_sm=self._mock_sm(),
            settings=settings,
            body_service="github",
            url="https://slack.com/api/chat.postMessage",
            task_id="task-egress-t1",
        )

        assert resp.status_code == 403
        assert resp.json()["detail"]["error"] == "credential_host_binding_denied"

    @patch("src.internal.credential_routes.socket.getaddrinfo", _fake_getaddrinfo_public)
    @patch("src.internal.credential_routes.get_settings")
    def test_binding_denial_writes_audit_row(self, mock_settings, db: AsyncSession):
        """T2 — the 403 lands in the same denial audit path as the allowlist 403."""
        settings = _settings_mock(enforce_host_binding=True)
        mock_settings.return_value = settings

        asyncio.get_event_loop().run_until_complete(_seed_credential(db, cred_id="cred-egress-t2", service="github", credential_type="bearer"))

        resp = self._post(
            db,
            mock_sm=self._mock_sm(),
            settings=settings,
            body_service="github",
            url="https://slack.com/api/chat.postMessage",
            task_id="task-egress-t2",
        )
        assert resp.status_code == 403

        async def _check():
            stmt = select(AuditLog).where(
                AuditLog.event_type == "vault_proxy_request_denied",
                AuditLog.org_id == "org-test",
            )
            logs = (await db.execute(stmt)).scalars().all()
            reasons = [log.details.get("reason") for log in logs]
            assert "credential_host_binding_denied" in reasons

        asyncio.get_event_loop().run_until_complete(_check())

    @patch("src.internal.credential_routes.socket.getaddrinfo", _fake_getaddrinfo_public)
    @patch("src.internal.credential_routes.get_settings")
    def test_secret_never_fetched_on_binding_denial(self, mock_settings, db: AsyncSession):
        """T3 — a denied request must not pull the secret out of Secrets Manager.

        Proves the check sits BEFORE _fetch_secret, not after it.
        """
        settings = _settings_mock(enforce_host_binding=True)
        mock_settings.return_value = settings

        asyncio.get_event_loop().run_until_complete(_seed_credential(db, cred_id="cred-egress-t3", service="github", credential_type="bearer"))
        mock_sm = self._mock_sm()

        resp = self._post(
            db,
            mock_sm=mock_sm,
            settings=settings,
            body_service="github",
            url="https://slack.com/api/chat.postMessage",
            task_id="task-egress-t3",
        )

        assert resp.status_code == 403
        mock_sm.get_secret.assert_not_called()

    @patch("src.internal.credential_routes.socket.getaddrinfo", _fake_getaddrinfo_public)
    @patch("src.internal.credential_routes.get_settings")
    def test_bound_service_to_own_host_still_allowed(self, mock_settings, db: AsyncSession):
        """T4 (positive control) — github -> api.github.com keeps working.

        Without this, T1 would be satisfiable by "deny everything".
        """
        settings = _settings_mock(enforce_host_binding=True)
        mock_settings.return_value = settings

        asyncio.get_event_loop().run_until_complete(_seed_credential(db, cred_id="cred-egress-t4", service="github", credential_type="bearer"))

        resp = self._post(
            db,
            mock_sm=self._mock_sm(),
            settings=settings,
            body_service="github",
            url="https://api.github.com/user",
            task_id="task-egress-t4",
        )

        assert resp.status_code == 200

    @patch("src.internal.credential_routes.socket.getaddrinfo", _fake_getaddrinfo_public)
    @patch("src.internal.credential_routes.get_settings")
    def test_unmapped_service_falls_back_to_allowlist(self, mock_settings, db: AsyncSession):
        """Unmapped free-form services fail OPEN — the allowlist is their control.

        `jira` is unmapped (its host set is deployment-specific), so a jira
        credential to the allowlisted jira.example.com must still succeed.
        """
        settings = _settings_mock(enforce_host_binding=True)
        mock_settings.return_value = settings

        asyncio.get_event_loop().run_until_complete(
            _seed_credential(db, cred_id="cred-egress-unmapped", service="jira", credential_type="basic_auth")
        )

        resp = self._post(
            db,
            mock_sm=self._mock_sm("user:pass"),
            settings=settings,
            body_service="jira",
            url="https://jira.example.com/rest/api/2/issue/PROJ-1",
            task_id="task-egress-unmapped",
        )

        assert resp.status_code == 200

    @patch("src.internal.credential_routes.socket.getaddrinfo", _fake_getaddrinfo_public)
    @patch("src.internal.credential_routes.get_settings")
    def test_resolved_cred_service_drives_decision_not_body_service(self, mock_settings, db: AsyncSession):
        """T6 (self-assertion guard) — the caller cannot flip the outcome.

        The stored credential is `github`. A caller claiming service="slack" to
        make slack.com look legitimate must NEVER get a 200 with the github
        secret: resolution is by body.service, so it 404s (no slack credential)
        rather than resolving the github row. Either way, never 200.
        """
        settings = _settings_mock(enforce_host_binding=True)
        mock_settings.return_value = settings

        asyncio.get_event_loop().run_until_complete(_seed_credential(db, cred_id="cred-egress-t6", service="github", credential_type="bearer"))
        mock_sm = self._mock_sm()

        resp = self._post(
            db,
            mock_sm=mock_sm,
            settings=settings,
            body_service="slack",  # lie: no slack credential exists
            url="https://slack.com/api/chat.postMessage",
            task_id="task-egress-t6",
        )

        assert resp.status_code != 200
        assert resp.status_code in (403, 404)
        mock_sm.get_secret.assert_not_called()

    @patch("src.internal.credential_routes.socket.getaddrinfo", _fake_getaddrinfo_public)
    @patch("src.internal.credential_routes.get_settings")
    def test_shadow_mode_allows_violation_and_warns(self, mock_settings, db: AsyncSession, caplog):
        """T8a — flag off (the default): violation is logged, not blocked."""
        settings = _settings_mock(enforce_host_binding=False)
        mock_settings.return_value = settings

        asyncio.get_event_loop().run_until_complete(_seed_credential(db, cred_id="cred-egress-t8a", service="github", credential_type="bearer"))

        with caplog.at_level("WARNING", logger="src.internal.credential_routes"):
            resp = self._post(
                db,
                mock_sm=self._mock_sm(),
                settings=settings,
                body_service="github",
                url="https://slack.com/api/chat.postMessage",
                task_id="task-egress-t8a",
            )

        assert resp.status_code == 200
        assert "shadow mode" in caplog.text

    @patch("src.internal.credential_routes.socket.getaddrinfo", _fake_getaddrinfo_public)
    @patch("src.internal.credential_routes.get_settings")
    def test_enforce_mode_blocks_same_violation(self, mock_settings, db: AsyncSession):
        """T8b — flag on: the same request 403s. Proves the off-switch is real,
        i.e. rollback is a config flip, not an 8-15 min redeploy."""
        settings = _settings_mock(enforce_host_binding=True)
        mock_settings.return_value = settings

        asyncio.get_event_loop().run_until_complete(_seed_credential(db, cred_id="cred-egress-t8b", service="github", credential_type="bearer"))

        resp = self._post(
            db,
            mock_sm=self._mock_sm(),
            settings=settings,
            body_service="github",
            url="https://slack.com/api/chat.postMessage",
            task_id="task-egress-t8b",
        )

        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# POST /internal/v1/credential-materialize
# ---------------------------------------------------------------------------


class TestCredentialMaterialize:
    def _mock_sm(self, secret_value: str) -> MagicMock:
        sm = MagicMock()
        sm.get_secret.return_value = secret_value
        return sm

    @patch("src.internal.credential_routes.get_settings")
    def test_file_delivery_refuses_before_secret_or_upload(self, mock_settings, db: AsyncSession):
        mock_settings.return_value = _settings_mock(bucket="test-bucket")

        asyncio.get_event_loop().run_until_complete(
            _seed_credential(
                db,
                cred_id="cred-mat-ssh",
                service="github",
                label="deploy-key",
                credential_type="ssh_key",
            )
        )
        mock_sm = self._mock_sm("-----BEGIN RSA PRIVATE KEY-----\n...\n-----END RSA PRIVATE KEY-----")

        with patch("boto3.client") as aws:
            client = _make_app(db, mock_sm=mock_sm, token_context=_fake_token_context(credential_scopes=["credential:materialize"]))
            resp = client.post(
                "/internal/v1/credential-materialize",
                json={
                    "user_id": "user-alice",
                    "invocation_id": "delivery-run",
                    "agent_id": "agent-001",
                    "task_id": "task-mat-01",
                    "service": "github",
                    "label": "deploy-key",
                },
                headers={
                    "X-Internal-Api-Key": _VALID_KEY,
                },
            )

        assert resp.status_code == 503
        assert resp.json()["detail"]["error"] == "credential_materialization_cleanup_unavailable"
        assert "materialize_url" not in resp.text
        mock_sm.get_secret.assert_not_called()
        aws.assert_not_called()

    @patch("src.internal.credential_routes.get_settings")
    def test_non_file_type_returns_400(self, mock_settings, db: AsyncSession):
        mock_settings.return_value = _settings_mock(bucket="test-bucket")

        asyncio.get_event_loop().run_until_complete(
            _seed_credential(
                db,
                cred_id="cred-mat-bearer",
                service="api-svc",
                label="default",
                credential_type="bearer",
            )
        )
        mock_sm = self._mock_sm("tok")

        with nullcontext():
            client = _make_app(db, mock_sm=mock_sm, token_context=_fake_token_context(credential_scopes=["credential:materialize"]))
            resp = client.post(
                "/internal/v1/credential-materialize",
                json={
                    "user_id": "user-alice",
                    "invocation_id": "delivery-run",
                    "agent_id": "agent-001",
                    "task_id": "task-mat-02",
                    "service": "api-svc",
                    "label": "default",
                },
                headers={
                    "X-Internal-Api-Key": _VALID_KEY,
                },
            )

        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert detail["error"] == "invalid_credential_type"

    @patch("src.internal.credential_routes.get_settings")
    def test_missing_scope_returns_403(self, mock_settings, db: AsyncSession):
        mock_settings.return_value = _settings_mock(bucket="test-bucket")

        asyncio.get_event_loop().run_until_complete(
            _seed_credential(
                db,
                cred_id="cred-mat-noscope",
                service="s3",
                label="key",
                credential_type="certificate",
            )
        )

        with nullcontext():
            client = _make_app(db)
            resp = client.post(
                "/internal/v1/credential-materialize",
                json={
                    "user_id": "user-alice",
                    "invocation_id": "delivery-run",
                    "agent_id": "agent-001",
                    "task_id": "task-mat-03",
                    "service": "s3",
                    "label": "key",
                },
                headers={"X-Internal-Api-Key": _VALID_KEY},
                # No X-Agent-Scopes header
            )

        assert resp.status_code == 403
        assert resp.json()["detail"]["error"] == "insufficient_scope"

    @patch("src.internal.credential_routes.get_settings")
    def test_wrong_scope_returns_403(self, mock_settings, db: AsyncSession):
        mock_settings.return_value = _settings_mock(bucket="test-bucket")

        asyncio.get_event_loop().run_until_complete(
            _seed_credential(
                db,
                cred_id="cred-mat-wrongscope",
                service="s3-cfg",
                label="key",
                credential_type="config_file",
            )
        )

        with nullcontext():
            client = _make_app(db)
            resp = client.post(
                "/internal/v1/credential-materialize",
                json={
                    "user_id": "user-alice",
                    "invocation_id": "delivery-run",
                    "agent_id": "agent-001",
                    "task_id": "task-mat-04",
                    "service": "s3-cfg",
                    "label": "key",
                },
                headers={
                    "X-Internal-Api-Key": _VALID_KEY,
                    "X-Agent-Scopes": "credential:raw-read",  # wrong scope
                },
            )

        assert resp.status_code == 403

    @patch("src.internal.credential_routes.get_settings")
    def test_missing_bucket_returns_503(self, mock_settings, db: AsyncSession):
        mock_settings.return_value = _settings_mock(bucket="")  # bucket not configured

        asyncio.get_event_loop().run_until_complete(
            _seed_credential(
                db,
                cred_id="cred-mat-nobucket",
                service="svc-x",
                label="cert",
                credential_type="certificate",
            )
        )
        mock_sm = self._mock_sm("cert-data")

        with nullcontext():
            client = _make_app(db, mock_sm=mock_sm, token_context=_fake_token_context(credential_scopes=["credential:materialize"]))
            resp = client.post(
                "/internal/v1/credential-materialize",
                json={
                    "user_id": "user-alice",
                    "invocation_id": "delivery-run",
                    "agent_id": "agent-001",
                    "task_id": "task-mat-05",
                    "service": "svc-x",
                    "label": "cert",
                },
                headers={
                    "X-Internal-Api-Key": _VALID_KEY,
                },
            )

        assert resp.status_code == 503

    @patch("src.internal.credential_routes.get_settings")
    def test_refused_materialization_does_not_mark_last_used(self, mock_settings, db: AsyncSession):
        mock_settings.return_value = _settings_mock(bucket="test-bucket")

        asyncio.get_event_loop().run_until_complete(
            _seed_credential(
                db,
                cred_id="cred-mat-lastused",
                service="ssh-svc",
                label="key",
                credential_type="ssh_key",
            )
        )
        mock_sm = self._mock_sm("-----BEGIN RSA PRIVATE KEY-----\n...\n-----END")

        with patch("boto3.client") as aws:
            client = _make_app(db, mock_sm=mock_sm, token_context=_fake_token_context(credential_scopes=["credential:materialize"]))
            resp = client.post(
                "/internal/v1/credential-materialize",
                json={
                    "user_id": "user-alice",
                    "invocation_id": "delivery-run",
                    "agent_id": "agent-001",
                    "task_id": "task-mat-lu",
                    "service": "ssh-svc",
                    "label": "key",
                },
                headers={
                    "X-Internal-Api-Key": _VALID_KEY,
                },
            )

        assert resp.status_code == 503
        assert resp.json()["detail"]["error"] == "credential_materialization_cleanup_unavailable"
        mock_sm.get_secret.assert_not_called()
        aws.assert_not_called()

        async def _check():
            stmt = select(UserCredential).where(UserCredential.id == "cred-mat-lastused")
            result = await db.execute(stmt)
            cred = result.scalar_one()
            assert cred.last_used_at is None

        asyncio.get_event_loop().run_until_complete(_check())


# ---------------------------------------------------------------------------
# POST /internal/v1/credential-raw-read
# ---------------------------------------------------------------------------


class TestCredentialRawRead:
    def _mock_sm(self, secret_value: str) -> MagicMock:
        sm = MagicMock()
        sm.get_secret.return_value = secret_value
        return sm

    @patch("src.internal.credential_routes.get_settings")
    def test_happy_path_returns_value(self, mock_settings, db: AsyncSession):
        mock_settings.return_value = _settings_mock(raw_read_enabled=True)

        asyncio.get_event_loop().run_until_complete(
            _seed_credential(
                db,
                cred_id="cred-raw-1",
                service="custom-api",
                label="token",
                credential_type="api_key",
            )
        )
        mock_sm = self._mock_sm("super-secret-value")

        with nullcontext():
            client = _make_app(db, mock_sm=mock_sm, token_context=_fake_token_context(credential_scopes=["credential:raw-read"]))
            resp = client.post(
                "/internal/v1/credential-raw-read",
                json={
                    "user_id": "user-alice",
                    "invocation_id": "delivery-run",
                    "agent_id": "agent-001",
                    "task_id": "task-raw-1",
                    "service": "custom-api",
                    "label": "token",
                    "purpose": "integration test",
                },
                headers={
                    "X-Internal-Api-Key": _VALID_KEY,
                },
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["value"] == "super-secret-value"
        assert body["credential_type"] == "api_key"
        assert "provenance_id" in body

    @patch("src.internal.credential_routes.get_settings")
    def test_feature_flag_disabled_returns_403(self, mock_settings, db: AsyncSession):
        mock_settings.return_value = _settings_mock(raw_read_enabled=False)

        asyncio.get_event_loop().run_until_complete(
            _seed_credential(
                db,
                cred_id="cred-raw-flagoff",
                service="svc-flagoff",
                label="default",
                credential_type="bearer",
            )
        )

        with nullcontext():
            client = _make_app(db)
            resp = client.post(
                "/internal/v1/credential-raw-read",
                json={
                    "user_id": "user-alice",
                    "invocation_id": "delivery-run",
                    "agent_id": "agent-001",
                    "task_id": "task-raw-flag",
                    "service": "svc-flagoff",
                },
                headers={
                    "X-Internal-Api-Key": _VALID_KEY,
                    "X-Agent-Scopes": "credential:raw-read",
                },
            )

        assert resp.status_code == 403
        assert resp.json()["detail"]["error"] == "feature_disabled"

    @patch("src.internal.credential_routes.get_settings")
    def test_missing_scope_returns_403(self, mock_settings, db: AsyncSession):
        mock_settings.return_value = _settings_mock(raw_read_enabled=True)

        asyncio.get_event_loop().run_until_complete(
            _seed_credential(
                db,
                cred_id="cred-raw-noscope",
                service="svc-noscope",
                label="default",
                credential_type="bearer",
            )
        )

        with nullcontext():
            client = _make_app(db)
            resp = client.post(
                "/internal/v1/credential-raw-read",
                json={
                    "user_id": "user-alice",
                    "invocation_id": "delivery-run",
                    "agent_id": "agent-001",
                    "task_id": "task-raw-noscope",
                    "service": "svc-noscope",
                },
                headers={"X-Internal-Api-Key": _VALID_KEY},
                # No X-Agent-Scopes
            )

        assert resp.status_code == 403

    @patch("src.internal.credential_routes.get_settings")
    def test_audit_log_written_on_raw_read(self, mock_settings, db: AsyncSession):
        mock_settings.return_value = _settings_mock(raw_read_enabled=True)

        asyncio.get_event_loop().run_until_complete(
            _seed_credential(
                db,
                cred_id="cred-raw-audit",
                service="audit-svc",
                label="key",
                credential_type="api_key",
            )
        )
        mock_sm = self._mock_sm("audit-secret")

        with nullcontext():
            client = _make_app(db, mock_sm=mock_sm, token_context=_fake_token_context(credential_scopes=["credential:raw-read"]))
            client.post(
                "/internal/v1/credential-raw-read",
                json={
                    "user_id": "user-alice",
                    "invocation_id": "delivery-run",
                    "agent_id": "agent-audit-raw",
                    "task_id": "task-raw-audit",
                    "service": "audit-svc",
                    "label": "key",
                    "purpose": "unit test",
                },
                headers={
                    "X-Internal-Api-Key": _VALID_KEY,
                },
            )

        async def _check():
            stmt = select(AuditLog).where(
                AuditLog.event_type == "vault_credential_raw_read",
                AuditLog.org_id == "org-test",
            )
            result = await db.execute(stmt)
            logs = result.scalars().all()
            assert len(logs) >= 1
            log = logs[-1]
            assert log.details["agent_id"] == "agent-audit-raw"
            assert log.details["service"] == "audit-svc"
            assert log.details["purpose"] == "unit test"
            assert "provenance_id" in log.details

        asyncio.get_event_loop().run_until_complete(_check())

    @patch("src.internal.credential_routes.get_settings")
    def test_last_used_at_updated_on_raw_read(self, mock_settings, db: AsyncSession):
        mock_settings.return_value = _settings_mock(raw_read_enabled=True)

        asyncio.get_event_loop().run_until_complete(
            _seed_credential(
                db,
                cred_id="cred-raw-lastused",
                service="lu-svc",
                label="default",
                credential_type="bearer",
            )
        )
        mock_sm = self._mock_sm("lu-token")

        with nullcontext():
            client = _make_app(db, mock_sm=mock_sm, token_context=_fake_token_context(credential_scopes=["credential:raw-read"]))
            client.post(
                "/internal/v1/credential-raw-read",
                json={
                    "user_id": "user-alice",
                    "invocation_id": "delivery-run",
                    "agent_id": "agent-001",
                    "task_id": "task-raw-lu",
                    "service": "lu-svc",
                },
                headers={
                    "X-Internal-Api-Key": _VALID_KEY,
                },
            )

        async def _check():
            stmt = select(UserCredential).where(UserCredential.id == "cred-raw-lastused")
            result = await db.execute(stmt)
            cred = result.scalar_one()
            assert cred.last_used_at is not None

        asyncio.get_event_loop().run_until_complete(_check())


@pytest.mark.parametrize("address", ["127.0.0.1", "169.254.169.254", "10.0.0.1", "100.64.0.1", "::1", "::ffff:127.0.0.1", "ff02::1"])
def test_proxy_rejects_non_public_dns_answers(address):
    answers = _fake_getaddrinfo_public("api.github.com", None) + [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", (address, 0))]
    with patch("src.internal.credential_routes.socket.getaddrinfo", return_value=answers):
        with pytest.raises(HTTPException) as denied:
            _validate_proxy_url("https://api.github.com/user", _settings_mock())
    assert denied.value.status_code == 400


@pytest.mark.parametrize("answers", [[], socket.gaierror("no DNS")])
def test_proxy_dns_failure_is_closed(answers):
    with patch("src.internal.credential_routes.socket.getaddrinfo") as dns:
        if isinstance(answers, Exception):
            dns.side_effect = answers
        else:
            dns.return_value = answers
        with pytest.raises(HTTPException) as denied:
            _validate_proxy_url("https://api.github.com/user", _settings_mock())
    assert denied.value.status_code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize("address", ["93.184.216.34", "2606:4700:4700::1111"])
async def test_pinned_transport_preserves_tls_identity_without_resolving_again(address):
    # Exercise real HTTPX/HTTPCore request handling, replacing only the socket.
    stream = MagicMock()
    stream.read = AsyncMock(return_value=b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nOK")
    stream.write = AsyncMock()
    stream.aclose = AsyncMock()
    stream.start_tls = AsyncMock(return_value=stream)
    stream.get_extra_info.return_value = None
    answers = [(socket.AF_INET6 if ":" in address else socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 0))]
    with patch("src.internal.credential_routes.socket.getaddrinfo", side_effect=[answers, socket.gaierror("rebound")]) as dns:
        target = _validate_proxy_url("https://api.github.com:8443/user?q=1", _settings_mock())
        with patch("httpcore._backends.anyio.AnyIOBackend.connect_tcp", new_callable=AsyncMock, return_value=stream) as connect:
            async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
                response = await client.get(target, headers={"Host": "api.github.com:8443"}, extensions={"sni_hostname": "api.github.com"})
        assert response.text == "OK"
        assert connect.call_args.args[0] == address
        assert connect.call_args.args[1] == 8443
        assert stream.start_tls.call_args.kwargs["server_hostname"] == "api.github.com"
        assert b"Host: api.github.com:8443" in stream.write.call_args_list[0].args[0]
        dns.assert_called_once()


@pytest.mark.asyncio
async def test_identity_pointer_proxies_jira_as_user_without_exposing_token(db):
    credential = await _seed_credential(
        db,
        cred_id="jira-oauth",
        service="jira",
        label="AgentCore OAuth",
        credential_type="oauth_token",
        secret_arn="agentcore-identity:adp-tools-dev-oauth-user:atlassian",
    )
    credential.provider = "agentcore-identity"
    credential.scopes = {"oauth_scopes": ["read:jira-work"]}
    credential.agent_consent = True
    await db.commit()
    identity = MagicMock()
    identity.get_workload_access_token_for_user_id.return_value = {"workloadAccessToken": "private-workload"}
    identity.get_resource_oauth2_token.return_value = {"accessToken": "private-jira-token"}
    response = MagicMock(status_code=200, headers={"x-upstream": "private-jira-token"}, text='{"user":"alice","echo":"private-jira-token"}')
    sm = MagicMock()
    client = _make_app(db, sm)
    settings = _settings_mock()
    settings.oauth_workload_name = "adp-tools-dev-oauth-user"
    with (
        patch("src.internal.credential_routes.get_settings", return_value=settings),
        patch("src.internal.credential_routes.get_identity_client", return_value=identity),
        patch("src.internal.credential_routes.socket.getaddrinfo", _fake_getaddrinfo_public),
        patch("httpx.AsyncClient") as http_client,
    ):
        upstream = AsyncMock()
        upstream.__aenter__ = AsyncMock(return_value=upstream)
        upstream.__aexit__ = AsyncMock(return_value=None)
        upstream.request = AsyncMock(return_value=response)
        upstream.stream = lambda **kwargs: stream_from_request(upstream, **kwargs)
        http_client.return_value = upstream
        result = client.post(
            "/internal/v1/proxy-request",
            json={
                "user_id": "user-alice",
                "invocation_id": "delivery-run",
                "agent_id": "agent-001",
                "task_id": "task-001",
                "service": "jira",
                "method": "GET",
                "url": "https://team.atlassian.net/rest/api/3/myself",
                "headers": {"authorization": "Bearer attacker"},
            },
            headers={"X-Internal-Api-Key": _VALID_KEY},
        )
    assert result.status_code == 200, result.text
    assert result.json()["status"] == 200
    assert "private-jira-token" not in result.text
    forwarded = upstream.request.call_args.kwargs
    assert forwarded["headers"]["Authorization"] == "Bearer private-jira-token"
    assert "authorization" not in forwarded["headers"]
    assert forwarded["headers"]["Host"] == "team.atlassian.net"
    assert forwarded["extensions"] == {"sni_hostname": "team.atlassian.net"}
    assert identity.get_resource_oauth2_token.call_args.kwargs["oauth2Flow"] == "USER_FEDERATION"
    assert identity.get_resource_oauth2_token.call_args.kwargs["scopes"] == ["read:jira-work"]
    sm.get_secret.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scopes,allowed",
    [
        ({"oauth_scopes": ["users:read"]}, True),
        ({"oauth_scopes": ["users:read", "chat:write"]}, True),
        ({"oauth_scopes": ["chat:write", "users:read"]}, True),
        ({"oauth_scopes": ["users:read", "admin"]}, False),
        ({"oauth_scopes": ["read:user"]}, False),
        ({"oauth_scopes": ["users:read", "users:read"]}, False),
        ({"oauth_scopes": ["users:read", ["chat:write"]]}, False),
        ({"oauth_scopes": "users:read"}, False),
        ({"oauth_scopes": []}, False),
        ({"oauth_scopes": ["users:read"], "requested_scopes": ["chat:write"]}, False),
        ({}, False),
    ],
)
async def test_slack_broker_uses_only_actual_allowlisted_grants(db, scopes, allowed):
    """Combined approved grants retain read; defaults or malformed grants cannot add authority."""
    credential = await _seed_credential(
        db,
        cred_id="slack-scopes",
        service="slack",
        label="AgentCore OAuth",
        credential_type="oauth_token",
        secret_arn="agentcore-identity:adp-tools-dev-oauth-user:slack",
    )
    credential.provider = "agentcore-identity"
    credential.scopes = scopes
    credential.agent_consent = True
    await db.commit()
    identity = MagicMock()
    identity.get_workload_access_token_for_user_id.return_value = {"workloadAccessToken": "private-workload"}
    identity.get_resource_oauth2_token.return_value = {"accessToken": "private-slack-token"}
    sm = MagicMock()
    upstream = AsyncMock()
    upstream.__aenter__ = AsyncMock(return_value=upstream)
    upstream.__aexit__ = AsyncMock(return_value=None)
    upstream.request = AsyncMock(return_value=MagicMock(status_code=200, headers={}, text='{"ok":true}'))
    upstream.stream = lambda **kwargs: stream_from_request(upstream, **kwargs)
    settings = _settings_mock(enforce_host_binding=True)
    settings.oauth_workload_name = "adp-tools-dev-oauth-user"
    client = _make_app(db, sm)
    with (
        patch("src.internal.credential_routes.get_settings", return_value=settings),
        patch("src.internal.credential_routes.get_identity_client", return_value=identity),
        patch("src.internal.credential_routes.socket.getaddrinfo", _fake_getaddrinfo_public),
        patch("httpx.AsyncClient", return_value=upstream),
        # A future consent default change must never expand an existing grant.
        patch.dict("src.internal.credential_routes.PROVIDER_SCOPES", {"slack": ("slack", ["users:read", "chat:write"])}),
    ):
        result = client.post(
            "/internal/v1/proxy-request",
            json={
                "user_id": "user-alice",
                "invocation_id": "delivery-run",
                "agent_id": "agent-001",
                "task_id": "task-001",
                "service": "slack",
                "method": "GET",
                "url": "https://slack.com/api/users.info?user=U001",
            },
            headers={"X-Internal-Api-Key": _VALID_KEY},
        )
    assert result.status_code == (200 if allowed else 403), result.text
    if allowed:
        identity.get_workload_access_token_for_user_id.assert_called_once_with(workloadName="adp-tools-dev-oauth-user", userId="user-alice")
        identity.get_resource_oauth2_token.assert_called_once_with(
            workloadIdentityToken="private-workload",
            resourceCredentialProviderName="adp-tools-dev-slack-user",
            scopes=scopes["oauth_scopes"],
            oauth2Flow="USER_FEDERATION",
        )
        assert upstream.request.call_args.kwargs["headers"]["Authorization"] == "Bearer private-slack-token"
    else:
        assert result.json()["detail"]["error"] == "not_connected"
        identity.get_workload_access_token_for_user_id.assert_not_called()
        identity.get_resource_oauth2_token.assert_not_called()
        upstream.request.assert_not_awaited()
    assert "private-slack-token" not in result.text
    sm.get_secret.assert_not_called()


@pytest.mark.asyncio
async def test_identity_pointer_cannot_fetch_token_before_egress_or_owner_checks(db):
    credential = await _seed_credential(
        db,
        cred_id="untrusted-oauth",
        service="slack",
        credential_type="oauth_token",
        secret_arn="agentcore-identity:adp-tools-dev-oauth-user:slack",
    )
    credential.provider = "agentcore-identity"
    credential.scopes = {"oauth_scopes": ["users:read"]}
    await db.commit()
    identity = MagicMock()
    sm = MagicMock()
    client = _make_app(db, sm)
    settings = _settings_mock(enforce_host_binding=True)
    settings.oauth_workload_name = "adp-tools-dev-oauth-user"
    with (
        patch("src.internal.credential_routes.get_settings", return_value=settings),
        patch("src.internal.credential_routes.get_identity_client", return_value=identity),
        patch("src.internal.credential_routes.socket.getaddrinfo", _fake_getaddrinfo_public),
    ):
        payload = {
            "user_id": "user-alice",
            "invocation_id": "delivery-run",
            "agent_id": "agent-001",
            "task_id": "task-001",
            "service": "slack",
            "method": "GET",
            "url": "https://api.github.com/user",
        }
        denied = client.post("/internal/v1/proxy-request", json=payload, headers={"X-Internal-Api-Key": _VALID_KEY})
        assert denied.status_code == 403
        payload["url"] = "https://slack.com/api/users.info"
        credential.user_id = "user-bob"
        await db.commit()
        denied_owner = client.post("/internal/v1/proxy-request", json=payload, headers={"X-Internal-Api-Key": _VALID_KEY})
        assert denied_owner.status_code in {403, 404}
    identity.get_resource_oauth2_token.assert_not_called()
    sm.get_secret.assert_not_called()


@pytest.mark.asyncio
async def test_jira_proxy_after_pointer_deletion_returns_not_connected(db):
    pointer = await _seed_credential(
        db,
        cred_id="oauth-disconnected",
        service="jira",
        label="AgentCore OAuth",
        credential_type="oauth_token",
        secret_arn="agentcore-identity:adp-tools-dev-oauth-user:atlassian",
    )
    pointer.provider = "agentcore-identity"
    await db.commit()
    await db.delete(pointer)
    await db.commit()
    sm = MagicMock()
    client = _make_app(db, sm)
    settings = _settings_mock()
    settings.oauth_workload_name = "adp-tools-dev-oauth-user"
    with (
        patch("src.internal.credential_routes.get_settings", return_value=settings),
        patch("src.internal.credential_routes.get_identity_client") as identity,
        patch("src.internal.credential_routes.socket.getaddrinfo", _fake_getaddrinfo_public),
    ):
        response = client.post(
            "/internal/v1/proxy-request",
            json={
                "user_id": "user-alice",
                "invocation_id": "delivery-run",
                "agent_id": "agent-001",
                "task_id": "task-001",
                "service": "jira",
                "label": "AgentCore OAuth",
                "method": "GET",
                "url": "https://team.atlassian.net/rest/api/3/myself",
            },
            headers={"X-Internal-Api-Key": _VALID_KEY},
        )
    assert response.status_code == 403 and response.json()["detail"]["error"] == "not_connected"
    identity.assert_not_called()
    sm.get_secret.assert_not_called()


@pytest.mark.asyncio
async def test_expired_identity_token_refreshes_without_user_action_or_cached_fallback(db):
    pointer = await _seed_credential(
        db,
        cred_id="jira-refresh",
        service="jira",
        label="AgentCore OAuth",
        credential_type="oauth_token",
        secret_arn="agentcore-identity:adp-tools-dev-oauth-user:atlassian",
    )
    pointer.provider = "agentcore-identity"
    pointer.scopes = {"oauth_scopes": ["read:jira-work"]}
    pointer.agent_consent = True
    await db.commit()
    sm = MagicMock()
    identity = MagicMock()
    identity.get_workload_access_token_for_user_id.return_value = {"workloadAccessToken": "user-bound-workload"}
    identity.get_resource_oauth2_token.side_effect = [
        {"accessToken": "before-expiry"},
        {"accessToken": "identity-refreshed"},
        {"authorizationUrl": "https://identity.example.com/consent", "sessionStatus": "IN_PROGRESS"},
    ]
    upstream = AsyncMock()
    upstream.__aenter__ = AsyncMock(return_value=upstream)
    upstream.__aexit__ = AsyncMock(return_value=None)
    upstream.request = AsyncMock(return_value=MagicMock(status_code=200, headers={}, text='{"ok":true}'))
    upstream.stream = lambda **kwargs: stream_from_request(upstream, **kwargs)
    settings = _settings_mock()
    settings.oauth_workload_name = "adp-tools-dev-oauth-user"
    client = _make_app(db, sm)
    payload = {
        "user_id": "user-alice",
        "invocation_id": "delivery-run",
        "agent_id": "agent-001",
        "task_id": "task-001",
        "service": "jira",
        "label": "AgentCore OAuth",
        "method": "GET",
        "url": "https://team.atlassian.net/rest/api/3/myself",
    }
    with (
        patch("src.internal.credential_routes.get_settings", return_value=settings),
        patch("src.internal.credential_routes.get_identity_client", return_value=identity),
        patch("src.internal.credential_routes.socket.getaddrinfo", _fake_getaddrinfo_public),
        patch("httpx.AsyncClient", return_value=upstream),
    ):
        first = client.post("/internal/v1/proxy-request", json=payload, headers={"X-Internal-Api-Key": _VALID_KEY})
        first_header = upstream.request.call_args.kwargs["headers"]["Authorization"]
        second = client.post("/internal/v1/proxy-request", json=payload, headers={"X-Internal-Api-Key": _VALID_KEY})
        second_header = upstream.request.call_args.kwargs["headers"]["Authorization"]
        third = client.post("/internal/v1/proxy-request", json=payload, headers={"X-Internal-Api-Key": _VALID_KEY})
    assert first.status_code == 200 and second.status_code == 200
    assert first_header == "Bearer before-expiry"
    assert second_header == "Bearer identity-refreshed"
    assert "before-expiry" not in first.text and "identity-refreshed" not in second.text
    assert third.status_code == 403 and third.json()["detail"]["error"] == "not_connected"
    assert "consent" not in third.text
    assert upstream.request.await_count == 2
    assert identity.get_workload_access_token_for_user_id.call_count == 3
    assert identity.get_resource_oauth2_token.call_count == 3
    sm.get_secret.assert_not_called()


@pytest.mark.parametrize("case", ["wire", "decoded", "compressed", "declared"])
def test_proxy_stream_caps_response_before_generic_receipt_or_further_chunks(case, db, monkeypatch):
    class Upstream(httpx.AsyncByteStream):
        def __init__(self):
            self.chunks = 0
            self.closed = False

        async def __aiter__(self):
            for _ in range(17 if case == "decoded" else 64):
                self.chunks += 1
                yield (b"\xe9" * 32768) if case == "decoded" else (b"x" * 65536)

        async def aclose(self):
            self.closed = True

    stream = Upstream()
    calls = []

    async def handler(request):
        calls.append(request)
        headers = {"content-type": "text/plain; charset=iso-8859-1" if case == "decoded" else "text/plain"}
        if case == "compressed":
            headers["content-encoding"] = "gzip"
        if case == "declared":
            headers["content-length"] = str(4 * 1048576)
        return httpx.Response(200, headers=headers, stream=stream)

    actual_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: actual_client(transport=httpx.MockTransport(handler), **kwargs))
    monkeypatch.setattr("src.internal.credential_routes.socket.getaddrinfo", _fake_getaddrinfo_public)
    monkeypatch.setattr("src.internal.credential_routes.get_settings", lambda: _settings_mock())
    asyncio.get_event_loop().run_until_complete(_seed_credential(db, cred_id=f"proxy-stream-{case}"))
    secret = MagicMock()
    secret.get_secret.return_value = "synthetic-private-value"
    client = _make_app(db, mock_sm=secret)
    result = client.post(
        "/internal/v1/proxy-request",
        json={
            "user_id": "user-alice",
            "invocation_id": "delivery-run",
            "agent_id": "agent-001",
            "task_id": "task-001",
            "service": "github",
            "method": "GET",
            "url": "https://api.github.com/user",
        },
        headers={"X-Internal-Api-Key": _VALID_KEY},
    )

    assert result.status_code == 502
    assert result.json()["detail"] == (
        "credential proxy response encoding unsupported" if case == "compressed" else "credential proxy response too large"
    )
    assert stream.chunks == {"wire": 17, "decoded": 17, "compressed": 0, "declared": 0}[case]
    assert stream.closed
    assert len(calls) == 1
    assert calls[0].headers["accept-encoding"] == "identity"
    assert "synthetic-private-value" not in result.text


def test_proxy_deadline_never_outlives_verified_assertion(monkeypatch):
    from types import SimpleNamespace

    from src.internal.credential_binding import GatewayCredentialAuthority
    from src.internal.credential_routes import _proxy_total_timeout

    monkeypatch.setattr("src.internal.credential_routes.time.time", lambda: 100.0)
    authority = GatewayCredentialAuthority(
        user_id="user-alice",
        tenant_id="org-test",
        agent_id="tools-facade",
        credential_ids=frozenset({"fixture"}),
        assertion_expires_at=107,
    )
    request = SimpleNamespace(state=SimpleNamespace(agent_tools_credential_authority=authority))
    assert _proxy_total_timeout(request) == 7.0
    assert _proxy_total_timeout(SimpleNamespace(state=SimpleNamespace())) == 30.0
    expired = GatewayCredentialAuthority(
        user_id="user-alice",
        tenant_id="org-test",
        agent_id="tools-facade",
        credential_ids=frozenset({"fixture"}),
        assertion_expires_at=100,
    )
    with pytest.raises(HTTPException, match="expired tools assertion"):
        _proxy_total_timeout(SimpleNamespace(state=SimpleNamespace(agent_tools_credential_authority=expired)))
