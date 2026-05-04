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
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.internal.credential_injector import (
    FILE_CREDENTIAL_TYPES,
    UnsupportedCredentialTypeError,
    inject_credential,
)
from src.internal.credential_routes import get_secrets_manager, router
from src.shared.database import get_db
from src.shared.models.audit import AuditLog
from src.shared.models.base import Base
from src.shared.models.organization import Department, Organization, Team, User
from src.shared.models.vault import UserCredential

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


def _make_app(db_session: AsyncSession, mock_sm=None) -> TestClient:
    """Build a minimal FastAPI test app with the credential router."""
    app = FastAPI()
    app.include_router(router)

    async def _get_db():
        yield db_session

    app.dependency_overrides[get_db] = _get_db
    # Auth: always pass if correct key supplied (mirrors real _verify_internal_key)
    # We rely on the real dependency but patch settings below per test.
    if mock_sm is not None:
        app.dependency_overrides[get_secrets_manager] = lambda: mock_sm
    return TestClient(app, raise_server_exceptions=False)


def _settings_mock(*, raw_read_enabled: bool = False, bucket: str = "test-bucket") -> MagicMock:
    s = MagicMock()
    s.internal_api_key = _VALID_KEY
    s.vault_raw_read_enabled = raw_read_enabled
    s.vault_materialization_bucket = bucket
    s.aws_region = "us-east-1"
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

        with patch("src.internal.routes.get_settings", return_value=_settings_mock()):
            client = _make_app(db)
            resp = client.get(
                "/internal/v1/user-credentials",
                params={"user_id": "user-alice", "service": "openai"},
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

        with patch("src.internal.routes.get_settings", return_value=_settings_mock()):
            client = _make_app(db)
            resp = client.get(
                "/internal/v1/user-credentials",
                params={"user_id": "user-nobody", "service": "github"},
                headers={"X-Internal-Api-Key": _VALID_KEY},
            )
        assert resp.status_code == 404

    def test_missing_api_key_returns_403(self, db: AsyncSession):
        with patch("src.internal.routes.get_settings", return_value=_settings_mock()):
            client = _make_app(db)
            resp = client.get(
                "/internal/v1/user-credentials",
                params={"user_id": "user-alice", "service": "github"},
            )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# POST /internal/v1/proxy-request
# ---------------------------------------------------------------------------


class TestProxyRequest:
    def _mock_sm(self, secret_value: str) -> MagicMock:
        sm = MagicMock()
        sm.get_secret.return_value = secret_value
        return sm

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
            patch("src.internal.routes.get_settings", return_value=_settings_mock()),
            patch("httpx.AsyncClient") as mock_client_cls,
        ):
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.request = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            client = _make_app(db, mock_sm=mock_sm)
            resp = client.post(
                "/internal/v1/proxy-request",
                json={
                    "user_id": "user-alice",
                    "agent_id": "agent-001",
                    "task_id": "task-001",
                    "service": "github",
                    "label": "pat",
                    "method": "GET",
                    "url": "https://api.github.com/user",
                },
                headers={"X-Internal-Api-Key": _VALID_KEY},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == 200
        assert "provenance_id" in body
        assert body["body"] == '{"id": 42}'

    @patch("src.internal.credential_routes.get_settings")
    def test_api_key_injected_into_forwarded_headers(self, mock_settings, db: AsyncSession):
        mock_settings.return_value = _settings_mock()

        asyncio.get_event_loop().run_until_complete(
            _seed_credential(db, cred_id="cred-proxy-apikey", service="openai", label="default", credential_type="api_key")
        )
        mock_sm = self._mock_sm("sk-openai-key")

        captured_headers: dict = {}

        async def _fake_request(*, method, url, headers=None, content=None):
            captured_headers.update(headers or {})
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.headers = {}
            mock_response.text = "{}"
            return mock_response

        with (
            patch("src.internal.routes.get_settings", return_value=_settings_mock()),
            patch("httpx.AsyncClient") as mock_client_cls,
        ):
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.request = _fake_request
            mock_client_cls.return_value = mock_client

            client = _make_app(db, mock_sm=mock_sm)
            resp = client.post(
                "/internal/v1/proxy-request",
                json={
                    "user_id": "user-alice",
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

        async def _fake_request(*, method, url, headers=None, content=None):
            captured_headers.update(headers or {})
            r = MagicMock()
            r.status_code = 200
            r.headers = {}
            r.text = "{}"
            return r

        with (
            patch("src.internal.routes.get_settings", return_value=_settings_mock()),
            patch("httpx.AsyncClient") as mock_client_cls,
        ):
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.request = _fake_request
            mock_client_cls.return_value = mock_client

            client = _make_app(db, mock_sm=mock_sm)
            resp = client.post(
                "/internal/v1/proxy-request",
                json={
                    "user_id": "user-alice",
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
            patch("src.internal.routes.get_settings", return_value=_settings_mock()),
            patch("httpx.AsyncClient") as mock_client_cls,
        ):
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.request = AsyncMock(return_value=r)
            mock_client_cls.return_value = mock_client

            client = _make_app(db, mock_sm=mock_sm)
            client.post(
                "/internal/v1/proxy-request",
                json={
                    "user_id": "user-alice",
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
            patch("src.internal.routes.get_settings", return_value=_settings_mock()),
            patch("httpx.AsyncClient") as mock_client_cls,
        ):
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.request = AsyncMock(return_value=r)
            mock_client_cls.return_value = mock_client

            client = _make_app(db, mock_sm=mock_sm)
            client.post(
                "/internal/v1/proxy-request",
                json={
                    "user_id": "user-alice",
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
            patch("src.internal.routes.get_settings", return_value=_settings_mock()),
            patch("httpx.AsyncClient") as mock_client_cls,
        ):
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.request = AsyncMock(side_effect=_httpx.ConnectError("Connection refused"))
            mock_client_cls.return_value = mock_client

            client = _make_app(db, mock_sm=mock_sm)
            resp = client.post(
                "/internal/v1/proxy-request",
                json={
                    "user_id": "user-alice",
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
# POST /internal/v1/credential-materialize
# ---------------------------------------------------------------------------


class TestCredentialMaterialize:
    def _mock_sm(self, secret_value: str) -> MagicMock:
        sm = MagicMock()
        sm.get_secret.return_value = secret_value
        return sm

    @patch("src.internal.credential_routes.get_settings")
    def test_ssh_key_returns_presigned_url(self, mock_settings, db: AsyncSession):
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

        fake_url = "https://s3.amazonaws.com/test-bucket/vault/materialize/xyz?X-Amz-Signature=abc"

        def _mock_upload_and_sign():
            return fake_url

        with (
            patch("src.internal.routes.get_settings", return_value=_settings_mock()),
            patch("asyncio.to_thread", new=AsyncMock(return_value=fake_url)),
        ):
            client = _make_app(db, mock_sm=mock_sm)
            resp = client.post(
                "/internal/v1/credential-materialize",
                json={
                    "user_id": "user-alice",
                    "agent_id": "agent-001",
                    "task_id": "task-mat-01",
                    "service": "github",
                    "label": "deploy-key",
                },
                headers={
                    "X-Internal-Api-Key": _VALID_KEY,
                    "X-Agent-Scopes": "credential:materialize",
                },
            )

        assert resp.status_code == 201
        body = resp.json()
        assert "materialize_url" in body
        assert "expires_at" in body
        assert "provenance_id" in body

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

        with patch("src.internal.routes.get_settings", return_value=_settings_mock()):
            client = _make_app(db, mock_sm=mock_sm)
            resp = client.post(
                "/internal/v1/credential-materialize",
                json={
                    "user_id": "user-alice",
                    "agent_id": "agent-001",
                    "task_id": "task-mat-02",
                    "service": "api-svc",
                    "label": "default",
                },
                headers={
                    "X-Internal-Api-Key": _VALID_KEY,
                    "X-Agent-Scopes": "credential:materialize",
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

        with patch("src.internal.routes.get_settings", return_value=_settings_mock()):
            client = _make_app(db)
            resp = client.post(
                "/internal/v1/credential-materialize",
                json={
                    "user_id": "user-alice",
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

        with patch("src.internal.routes.get_settings", return_value=_settings_mock()):
            client = _make_app(db)
            resp = client.post(
                "/internal/v1/credential-materialize",
                json={
                    "user_id": "user-alice",
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

        with patch("src.internal.routes.get_settings", return_value=_settings_mock()):
            client = _make_app(db, mock_sm=mock_sm)
            resp = client.post(
                "/internal/v1/credential-materialize",
                json={
                    "user_id": "user-alice",
                    "agent_id": "agent-001",
                    "task_id": "task-mat-05",
                    "service": "svc-x",
                    "label": "cert",
                },
                headers={
                    "X-Internal-Api-Key": _VALID_KEY,
                    "X-Agent-Scopes": "credential:materialize",
                },
            )

        assert resp.status_code == 503

    @patch("src.internal.credential_routes.get_settings")
    def test_last_used_at_updated_on_materialize(self, mock_settings, db: AsyncSession):
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

        fake_url = "https://s3.amazonaws.com/test-bucket/vault/materialize/xyz"

        with (
            patch("src.internal.routes.get_settings", return_value=_settings_mock()),
            patch("asyncio.to_thread", new=AsyncMock(return_value=fake_url)),
        ):
            client = _make_app(db, mock_sm=mock_sm)
            resp = client.post(
                "/internal/v1/credential-materialize",
                json={
                    "user_id": "user-alice",
                    "agent_id": "agent-001",
                    "task_id": "task-mat-lu",
                    "service": "ssh-svc",
                    "label": "key",
                },
                headers={
                    "X-Internal-Api-Key": _VALID_KEY,
                    "X-Agent-Scopes": "credential:materialize",
                },
            )

        assert resp.status_code == 201

        async def _check():
            stmt = select(UserCredential).where(UserCredential.id == "cred-mat-lastused")
            result = await db.execute(stmt)
            cred = result.scalar_one()
            assert cred.last_used_at is not None

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

        with patch("src.internal.routes.get_settings", return_value=_settings_mock()):
            client = _make_app(db, mock_sm=mock_sm)
            resp = client.post(
                "/internal/v1/credential-raw-read",
                json={
                    "user_id": "user-alice",
                    "agent_id": "agent-001",
                    "task_id": "task-raw-1",
                    "service": "custom-api",
                    "label": "token",
                    "purpose": "integration test",
                },
                headers={
                    "X-Internal-Api-Key": _VALID_KEY,
                    "X-Agent-Scopes": "credential:raw-read",
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

        with patch("src.internal.routes.get_settings", return_value=_settings_mock()):
            client = _make_app(db)
            resp = client.post(
                "/internal/v1/credential-raw-read",
                json={
                    "user_id": "user-alice",
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

        with patch("src.internal.routes.get_settings", return_value=_settings_mock()):
            client = _make_app(db)
            resp = client.post(
                "/internal/v1/credential-raw-read",
                json={
                    "user_id": "user-alice",
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

        with patch("src.internal.routes.get_settings", return_value=_settings_mock()):
            client = _make_app(db, mock_sm=mock_sm)
            client.post(
                "/internal/v1/credential-raw-read",
                json={
                    "user_id": "user-alice",
                    "agent_id": "agent-audit-raw",
                    "task_id": "task-raw-audit",
                    "service": "audit-svc",
                    "label": "key",
                    "purpose": "unit test",
                },
                headers={
                    "X-Internal-Api-Key": _VALID_KEY,
                    "X-Agent-Scopes": "credential:raw-read",
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

        with patch("src.internal.routes.get_settings", return_value=_settings_mock()):
            client = _make_app(db, mock_sm=mock_sm)
            client.post(
                "/internal/v1/credential-raw-read",
                json={
                    "user_id": "user-alice",
                    "agent_id": "agent-001",
                    "task_id": "task-raw-lu",
                    "service": "lu-svc",
                },
                headers={
                    "X-Internal-Api-Key": _VALID_KEY,
                    "X-Agent-Scopes": "credential:raw-read",
                },
            )

        async def _check():
            stmt = select(UserCredential).where(UserCredential.id == "cred-raw-lastused")
            result = await db.execute(stmt)
            cred = result.scalar_one()
            assert cred.last_used_at is not None

        asyncio.get_event_loop().run_until_complete(_check())
