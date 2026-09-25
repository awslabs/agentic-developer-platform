"""Tests for registry-based credential capability gates on raw-read and materialize.

Issue #6050 (S12 continuation): verifies that require_credential_capability()
enforces registry-granted credential_scopes from verified token_context, NOT
caller-supplied X-Agent-Scopes headers or body fields.

Coverage:
  - Registered identity with exact credential_scopes -> 200/201
  - Registered identity with NO scopes -> 403 + zero secret fetch
  - Raw-read-only identity requesting materialize -> 403
  - Materialize-only identity requesting raw-read -> 403
  - Forged X-Agent-Scopes header with no registry scope -> 403
  - Missing token_context (shared-key-only path) -> 403
  - Valid registry grants without any X-Agent-Scopes header -> 200/201
  - Feature-disabled raw-read refusal preserved
  - Protected (broker-verified) and non-protected registry contexts
  - Mutation guard: gate cannot be bypassed by header or body
  - Real POST route/method coverage via TestClient
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.internal.credential_authorization import require_credential_capability
from src.internal.credential_routes import get_secrets_manager, router
from src.shared.database import get_db
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
            id="org-cap",
            name="Cap Org",
            aws_accounts=[],
            role_mappings={},
            settings={},
            github_installation_ids=[],
            cognito_client_ids=[],
        )
        dept = Department(id="dept-cap", org_id="org-cap", name="Engineering")
        team = Team(id="team-cap", org_id="org-cap", department_id="dept-cap", name="Cap Team")
        user = User(
            id="user-cap",
            org_id="org-cap",
            team_id="team-cap",
            email="cap@test.com",
        )
        session.add_all([org, dept, team, user])
        await session.commit()
        yield session


def _fake_token_context(*, credential_scopes: list[str] | None = None, scope: str = "internal"):
    """Build a minimal mock TokenContext with the given credential_scopes."""
    ctx = MagicMock()
    ctx.credential_scopes = credential_scopes if credential_scopes is not None else []
    ctx.scope = scope
    ctx.user_id = "worker-test"
    ctx.org_id = "org-cap"
    ctx.requires_run_identity = False
    return ctx


def _make_app(
    db_session: AsyncSession,
    mock_sm=None,
    *,
    token_context=None,
) -> TestClient:
    """Build a minimal FastAPI test app with the credential router.

    When token_context is provided, the auth override sets it on request.state
    to simulate a verified IRSA identity with registry-granted scopes.
    When token_context is None, no identity is set (shared-key-only path).
    """
    app = FastAPI()
    app.include_router(router)

    async def _get_db():
        yield db_session

    app.dependency_overrides[get_db] = _get_db

    from src.internal.auth_deps import verify_internal_or_irsa

    async def _verify(request: Request) -> None:
        """Simulates successful authentication and optionally sets token_context."""
        if token_context is not None:
            request.state.token_context = token_context
            from src.internal.credential_binding import BindingResult

            request.state.agent_credential_binding = BindingResult("user-cap", True, False, "user-cap", "cap-run", "org-cap")

    app.dependency_overrides[verify_internal_or_irsa] = _verify

    if mock_sm is not None:
        app.dependency_overrides[get_secrets_manager] = lambda: mock_sm
    return TestClient(app, raise_server_exceptions=False)


def _settings_mock(*, raw_read_enabled: bool = True, bucket: str = "test-bucket") -> MagicMock:
    s = MagicMock()
    s.internal_api_key = _VALID_KEY
    s.vault_raw_read_enabled = raw_read_enabled
    s.vault_materialization_bucket = bucket
    s.aws_region = "us-east-1"
    s.vault_proxy_host_allowlist = ""
    s.vault_proxy_require_https = True
    s.vault_enforce_credential_host_binding = False
    s.enforce_credential_binding = False
    s.webhook_events_table = "adp-test-webhook-events"
    return s


def _mock_sm(value: str = "secret-value") -> MagicMock:
    sm = MagicMock()
    sm.get_secret.return_value = value
    return sm


async def _seed_credential(
    db: AsyncSession,
    *,
    cred_id: str = "cred-cap-1",
    service: str = "github",
    label: str = "default",
    credential_type: str = "bearer",
    secret_arn: str = "arn:aws:secretsmanager:us-east-1:123:secret:test",
) -> UserCredential:
    cred = UserCredential(
        id=cred_id,
        org_id="org-cap",
        user_id="user-cap",
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
# Unit tests: require_credential_capability helper
# ---------------------------------------------------------------------------


class TestRequireCredentialCapability:
    """Direct unit tests for the helper function itself."""

    def test_no_identity_raises_403(self):
        """Missing token_context -> fail closed."""
        request = MagicMock(spec=["state", "url"])
        request.state = MagicMock(spec=[])  # no token_context attr
        request.url.path = "/internal/v1/credential-raw-read"
        with pytest.raises(Exception) as exc_info:
            require_credential_capability(request, "credential:raw-read")
        assert exc_info.value.status_code == 403
        assert exc_info.value.detail["error"] == "insufficient_scope"

    def test_empty_scopes_raises_403(self):
        """Identity present but credential_scopes is empty -> denied."""
        request = MagicMock(spec=["state", "url"])
        ctx = _fake_token_context(credential_scopes=[])
        request.state.token_context = ctx
        request.url.path = "/internal/v1/credential-materialize"
        with pytest.raises(Exception) as exc_info:
            require_credential_capability(request, "credential:materialize")
        assert exc_info.value.status_code == 403

    def test_wrong_scope_raises_403(self):
        """Identity has raw-read but materialize is required -> denied."""
        request = MagicMock(spec=["state", "url"])
        ctx = _fake_token_context(credential_scopes=["credential:raw-read"])
        request.state.token_context = ctx
        request.url.path = "/internal/v1/credential-materialize"
        with pytest.raises(Exception) as exc_info:
            require_credential_capability(request, "credential:materialize")
        assert exc_info.value.status_code == 403

    def test_exact_scope_passes(self):
        """Identity has the required scope -> no exception."""
        request = MagicMock(spec=["state", "url"])
        ctx = _fake_token_context(credential_scopes=["credential:raw-read"])
        request.state.token_context = ctx
        request.url.path = "/internal/v1/credential-raw-read"
        # Should not raise
        require_credential_capability(request, "credential:raw-read")

    def test_multiple_scopes_passes(self):
        """Identity has both scopes -> either check passes."""
        request = MagicMock(spec=["state", "url"])
        ctx = _fake_token_context(credential_scopes=["credential:raw-read", "credential:materialize"])
        request.state.token_context = ctx
        request.url.path = "/internal/v1/credential-materialize"
        require_credential_capability(request, "credential:materialize")
        require_credential_capability(request, "credential:raw-read")

    def test_none_scopes_attr_raises_403(self):
        """credential_scopes is None (not just empty) -> denied."""
        request = MagicMock(spec=["state", "url"])
        ctx = _fake_token_context()
        ctx.credential_scopes = None
        request.state.token_context = ctx
        request.url.path = "/internal/v1/credential-raw-read"
        with pytest.raises(Exception) as exc_info:
            require_credential_capability(request, "credential:raw-read")
        assert exc_info.value.status_code == 403


# ---------------------------------------------------------------------------
# Integration tests: POST /internal/v1/credential-raw-read
# ---------------------------------------------------------------------------


class TestRawReadCapabilityGate:
    """Test the raw-read endpoint through the mounted router with real HTTP."""

    RAW_READ_BODY = {
        "invocation_id": "cap-run",
        "user_id": "user-cap",
        "agent_id": "agent-cap-001",
        "task_id": "task-rr-01",
        "service": "github",
        "label": "default",
        "purpose": "test",
    }

    @patch("src.internal.credential_routes.get_settings")
    def test_registry_granted_raw_read_succeeds(self, mock_settings, db: AsyncSession):
        """Caller with registry credential:raw-read -> 200 + raw value returned."""
        mock_settings.return_value = _settings_mock(raw_read_enabled=True)
        asyncio.get_event_loop().run_until_complete(_seed_credential(db, cred_id="cred-rr-ok", service="github", label="default"))
        sm = _mock_sm("gh-secret-token")
        ctx = _fake_token_context(credential_scopes=["credential:raw-read"])

        with patch("src.internal.routes.get_settings", return_value=_settings_mock()):
            client = _make_app(db, mock_sm=sm, token_context=ctx)
            resp = client.post("/internal/v1/credential-raw-read", json=self.RAW_READ_BODY)

        assert resp.status_code == 200
        body = resp.json()
        assert body["value"] == "gh-secret-token"
        assert "provenance_id" in body
        assert body["credential_type"] == "bearer"

    @patch("src.internal.credential_routes.get_settings")
    def test_no_scopes_returns_403_and_no_secret_fetch(self, mock_settings, db: AsyncSession):
        """Identity with empty scopes -> 403. SecretsManager never called."""
        mock_settings.return_value = _settings_mock(raw_read_enabled=True)
        asyncio.get_event_loop().run_until_complete(_seed_credential(db, cred_id="cred-rr-noscope", service="github", label="default"))
        sm = _mock_sm("should-not-reach")
        ctx = _fake_token_context(credential_scopes=[])

        with patch("src.internal.routes.get_settings", return_value=_settings_mock()):
            client = _make_app(db, mock_sm=sm, token_context=ctx)
            resp = client.post("/internal/v1/credential-raw-read", json=self.RAW_READ_BODY)

        assert resp.status_code == 403
        assert resp.json()["detail"]["error"] == "insufficient_scope"
        sm.get_secret.assert_not_called()

    @patch("src.internal.credential_routes.get_settings")
    def test_materialize_only_scope_returns_403(self, mock_settings, db: AsyncSession):
        """Identity has materialize but not raw-read -> 403."""
        mock_settings.return_value = _settings_mock(raw_read_enabled=True)
        asyncio.get_event_loop().run_until_complete(_seed_credential(db, cred_id="cred-rr-wrong", service="github", label="default"))
        sm = _mock_sm("should-not-reach")
        ctx = _fake_token_context(credential_scopes=["credential:materialize"])

        with patch("src.internal.routes.get_settings", return_value=_settings_mock()):
            client = _make_app(db, mock_sm=sm, token_context=ctx)
            resp = client.post("/internal/v1/credential-raw-read", json=self.RAW_READ_BODY)

        assert resp.status_code == 403
        sm.get_secret.assert_not_called()

    @patch("src.internal.credential_routes.get_settings")
    def test_forged_header_with_no_registry_scope_returns_403(self, mock_settings, db: AsyncSession):
        """Caller sends X-Agent-Scopes header but has no registry scopes -> 403.

        This is the core security assertion: the old header-based gate would
        have accepted this request; the new registry-based gate must reject it.
        """
        mock_settings.return_value = _settings_mock(raw_read_enabled=True)
        asyncio.get_event_loop().run_until_complete(_seed_credential(db, cred_id="cred-rr-forged", service="github", label="default"))
        sm = _mock_sm("should-not-reach")
        ctx = _fake_token_context(credential_scopes=[])  # no registry scopes

        with patch("src.internal.routes.get_settings", return_value=_settings_mock()):
            client = _make_app(db, mock_sm=sm, token_context=ctx)
            resp = client.post(
                "/internal/v1/credential-raw-read",
                json=self.RAW_READ_BODY,
                headers={"X-Agent-Scopes": "credential:raw-read"},  # forged header
            )

        assert resp.status_code == 403
        sm.get_secret.assert_not_called()

    @patch("src.internal.credential_routes.get_settings")
    def test_no_identity_shared_key_only_returns_403(self, mock_settings, db: AsyncSession):
        """Shared-key-only caller (no token_context) -> 403."""
        mock_settings.return_value = _settings_mock(raw_read_enabled=True)
        asyncio.get_event_loop().run_until_complete(_seed_credential(db, cred_id="cred-rr-noident", service="github", label="default"))
        sm = _mock_sm("should-not-reach")
        # No token_context -> shared-key-only path
        with patch("src.internal.routes.get_settings", return_value=_settings_mock()):
            client = _make_app(db, mock_sm=sm, token_context=None)
            resp = client.post("/internal/v1/credential-raw-read", json=self.RAW_READ_BODY)

        assert resp.status_code == 403
        sm.get_secret.assert_not_called()

    @patch("src.internal.credential_routes.get_settings")
    def test_valid_grant_without_header_succeeds(self, mock_settings, db: AsyncSession):
        """Caller with registry scope but NO X-Agent-Scopes header -> 200.

        Proves the header is not required when registry scopes are present.
        """
        mock_settings.return_value = _settings_mock(raw_read_enabled=True)
        asyncio.get_event_loop().run_until_complete(_seed_credential(db, cred_id="cred-rr-noheader", service="github", label="default"))
        sm = _mock_sm("real-secret")
        ctx = _fake_token_context(credential_scopes=["credential:raw-read"])

        with patch("src.internal.routes.get_settings", return_value=_settings_mock()):
            client = _make_app(db, mock_sm=sm, token_context=ctx)
            # Explicitly no X-Agent-Scopes header
            resp = client.post("/internal/v1/credential-raw-read", json=self.RAW_READ_BODY)

        assert resp.status_code == 200
        assert resp.json()["value"] == "real-secret"

    @patch("src.internal.credential_routes.get_settings")
    def test_feature_disabled_still_returns_403(self, mock_settings, db: AsyncSession):
        """Raw-read feature flag disabled -> 403 even with correct scopes.

        The feature flag fires BEFORE the scope gate, so the scope check
        is never reached.
        """
        mock_settings.return_value = _settings_mock(raw_read_enabled=False)
        asyncio.get_event_loop().run_until_complete(_seed_credential(db, cred_id="cred-rr-flagoff", service="github", label="default"))
        sm = _mock_sm("should-not-reach")
        ctx = _fake_token_context(credential_scopes=["credential:raw-read"])

        with patch("src.internal.routes.get_settings", return_value=_settings_mock(raw_read_enabled=False)):
            client = _make_app(db, mock_sm=sm, token_context=ctx)
            resp = client.post("/internal/v1/credential-raw-read", json=self.RAW_READ_BODY)

        assert resp.status_code == 403
        assert resp.json()["detail"]["error"] == "feature_disabled"
        sm.get_secret.assert_not_called()

    @patch("src.internal.credential_routes.get_settings")
    def test_both_scopes_grants_raw_read(self, mock_settings, db: AsyncSession):
        """Identity with BOTH scopes -> raw-read succeeds."""
        mock_settings.return_value = _settings_mock(raw_read_enabled=True)
        asyncio.get_event_loop().run_until_complete(_seed_credential(db, cred_id="cred-rr-both", service="github", label="default"))
        sm = _mock_sm("both-secret")
        ctx = _fake_token_context(credential_scopes=["credential:raw-read", "credential:materialize"])

        with patch("src.internal.routes.get_settings", return_value=_settings_mock()):
            client = _make_app(db, mock_sm=sm, token_context=ctx)
            resp = client.post("/internal/v1/credential-raw-read", json=self.RAW_READ_BODY)

        assert resp.status_code == 200
        assert resp.json()["value"] == "both-secret"

    @patch("src.internal.credential_routes.get_settings")
    def test_forged_body_scopes_ignored(self, mock_settings, db: AsyncSession):
        """Caller injects a 'scopes' field in the request body -> still 403."""
        mock_settings.return_value = _settings_mock(raw_read_enabled=True)
        asyncio.get_event_loop().run_until_complete(_seed_credential(db, cred_id="cred-rr-bodyscope", service="github", label="default"))
        sm = _mock_sm("should-not-reach")
        ctx = _fake_token_context(credential_scopes=[])

        body_with_forged_scopes = {
            **self.RAW_READ_BODY,
            "scopes": "credential:raw-read",
            "credential_scopes": ["credential:raw-read"],
        }

        with patch("src.internal.routes.get_settings", return_value=_settings_mock()):
            client = _make_app(db, mock_sm=sm, token_context=ctx)
            resp = client.post("/internal/v1/credential-raw-read", json=body_with_forged_scopes)

        assert resp.status_code == 403
        sm.get_secret.assert_not_called()


# ---------------------------------------------------------------------------
# Integration tests: POST /internal/v1/credential-materialize
# ---------------------------------------------------------------------------


class TestMaterializeCapabilityGate:
    """Test the materialize endpoint through the mounted router with real HTTP."""

    MATERIALIZE_BODY = {
        "invocation_id": "cap-run",
        "user_id": "user-cap",
        "agent_id": "agent-cap-001",
        "task_id": "task-mat-01",
        "service": "github",
        "label": "deploy-key",
    }

    @patch("src.internal.credential_routes.get_settings")
    def test_registry_granted_materialize_succeeds(self, mock_settings, db: AsyncSession):
        """Caller with registry credential:materialize -> 201 + presigned URL."""
        mock_settings.return_value = _settings_mock(bucket="test-bucket")
        asyncio.get_event_loop().run_until_complete(
            _seed_credential(
                db,
                cred_id="cred-mat-ok",
                service="github",
                label="deploy-key",
                credential_type="ssh_key",
            )
        )
        sm = _mock_sm("-----BEGIN RSA PRIVATE KEY-----\ntest\n-----END RSA PRIVATE KEY-----")
        ctx = _fake_token_context(credential_scopes=["credential:materialize"])

        fake_url = "https://s3.amazonaws.com/test-bucket/vault/materialize/test"

        from src.internal.credential_binding import resolve_credential_binding

        async def _selective_to_thread(fn, *args, **kwargs):
            if fn is resolve_credential_binding:
                return fn(*args, **kwargs)
            return fake_url

        with (
            patch("src.internal.routes.get_settings", return_value=_settings_mock()),
            patch("asyncio.to_thread", new=_selective_to_thread),
        ):
            client = _make_app(db, mock_sm=sm, token_context=ctx)
            resp = client.post("/internal/v1/credential-materialize", json=self.MATERIALIZE_BODY)

        assert resp.status_code == 201
        body = resp.json()
        assert "materialize_url" in body
        assert "expires_at" in body
        assert "provenance_id" in body

    @patch("src.internal.credential_routes.get_settings")
    def test_no_scopes_returns_403_and_no_secret_fetch(self, mock_settings, db: AsyncSession):
        """Identity with empty scopes -> 403. SecretsManager never called."""
        mock_settings.return_value = _settings_mock(bucket="test-bucket")
        asyncio.get_event_loop().run_until_complete(
            _seed_credential(
                db,
                cred_id="cred-mat-noscope",
                service="github",
                label="deploy-key",
                credential_type="ssh_key",
            )
        )
        sm = _mock_sm("should-not-reach")
        ctx = _fake_token_context(credential_scopes=[])

        with patch("src.internal.routes.get_settings", return_value=_settings_mock()):
            client = _make_app(db, mock_sm=sm, token_context=ctx)
            resp = client.post("/internal/v1/credential-materialize", json=self.MATERIALIZE_BODY)

        assert resp.status_code == 403
        assert resp.json()["detail"]["error"] == "insufficient_scope"
        sm.get_secret.assert_not_called()

    @patch("src.internal.credential_routes.get_settings")
    def test_raw_read_only_scope_returns_403(self, mock_settings, db: AsyncSession):
        """Identity has raw-read but NOT materialize -> 403."""
        mock_settings.return_value = _settings_mock(bucket="test-bucket")
        asyncio.get_event_loop().run_until_complete(
            _seed_credential(
                db,
                cred_id="cred-mat-wrongscope",
                service="github",
                label="deploy-key",
                credential_type="ssh_key",
            )
        )
        sm = _mock_sm("should-not-reach")
        ctx = _fake_token_context(credential_scopes=["credential:raw-read"])

        with patch("src.internal.routes.get_settings", return_value=_settings_mock()):
            client = _make_app(db, mock_sm=sm, token_context=ctx)
            resp = client.post("/internal/v1/credential-materialize", json=self.MATERIALIZE_BODY)

        assert resp.status_code == 403
        sm.get_secret.assert_not_called()

    @patch("src.internal.credential_routes.get_settings")
    def test_forged_header_with_no_registry_scope_returns_403(self, mock_settings, db: AsyncSession):
        """Caller sends X-Agent-Scopes header but has no registry scopes -> 403."""
        mock_settings.return_value = _settings_mock(bucket="test-bucket")
        asyncio.get_event_loop().run_until_complete(
            _seed_credential(
                db,
                cred_id="cred-mat-forged",
                service="github",
                label="deploy-key",
                credential_type="ssh_key",
            )
        )
        sm = _mock_sm("should-not-reach")
        ctx = _fake_token_context(credential_scopes=[])

        with patch("src.internal.routes.get_settings", return_value=_settings_mock()):
            client = _make_app(db, mock_sm=sm, token_context=ctx)
            resp = client.post(
                "/internal/v1/credential-materialize",
                json=self.MATERIALIZE_BODY,
                headers={"X-Agent-Scopes": "credential:materialize"},
            )

        assert resp.status_code == 403
        sm.get_secret.assert_not_called()

    @patch("src.internal.credential_routes.get_settings")
    def test_no_identity_shared_key_only_returns_403(self, mock_settings, db: AsyncSession):
        """Shared-key-only caller (no token_context) -> 403."""
        mock_settings.return_value = _settings_mock(bucket="test-bucket")
        asyncio.get_event_loop().run_until_complete(
            _seed_credential(
                db,
                cred_id="cred-mat-noident",
                service="github",
                label="deploy-key",
                credential_type="ssh_key",
            )
        )
        sm = _mock_sm("should-not-reach")

        with patch("src.internal.routes.get_settings", return_value=_settings_mock()):
            client = _make_app(db, mock_sm=sm, token_context=None)
            resp = client.post("/internal/v1/credential-materialize", json=self.MATERIALIZE_BODY)

        assert resp.status_code == 403
        sm.get_secret.assert_not_called()

    @patch("src.internal.credential_routes.get_settings")
    def test_valid_grant_without_header_succeeds(self, mock_settings, db: AsyncSession):
        """Caller with registry scope but NO X-Agent-Scopes header -> 201."""
        mock_settings.return_value = _settings_mock(bucket="test-bucket")
        asyncio.get_event_loop().run_until_complete(
            _seed_credential(
                db,
                cred_id="cred-mat-noheader",
                service="github",
                label="deploy-key",
                credential_type="ssh_key",
            )
        )
        sm = _mock_sm("-----BEGIN CERT-----\ntest\n-----END CERT-----")
        ctx = _fake_token_context(credential_scopes=["credential:materialize"])

        fake_url = "https://s3.amazonaws.com/test-bucket/vault/materialize/noheader"

        from src.internal.credential_binding import resolve_credential_binding

        async def _selective_to_thread(fn, *args, **kwargs):
            if fn is resolve_credential_binding:
                return fn(*args, **kwargs)
            return fake_url

        with (
            patch("src.internal.routes.get_settings", return_value=_settings_mock()),
            patch("asyncio.to_thread", new=_selective_to_thread),
        ):
            client = _make_app(db, mock_sm=sm, token_context=ctx)
            # No X-Agent-Scopes header
            resp = client.post("/internal/v1/credential-materialize", json=self.MATERIALIZE_BODY)

        assert resp.status_code == 201

    @patch("src.internal.credential_routes.get_settings")
    def test_non_file_type_still_blocked_after_scope_check(self, mock_settings, db: AsyncSession):
        """Non-file credential type -> 400 (after scope check passes).

        Verifies the credential type gate is preserved and runs after the scope gate.
        """
        mock_settings.return_value = _settings_mock(bucket="test-bucket")
        asyncio.get_event_loop().run_until_complete(
            _seed_credential(
                db,
                cred_id="cred-mat-bearer",
                service="github",
                label="deploy-key",
                credential_type="bearer",  # not a file type
            )
        )
        sm = _mock_sm("tok")
        ctx = _fake_token_context(credential_scopes=["credential:materialize"])

        with patch("src.internal.routes.get_settings", return_value=_settings_mock()):
            client = _make_app(db, mock_sm=sm, token_context=ctx)
            resp = client.post("/internal/v1/credential-materialize", json=self.MATERIALIZE_BODY)

        assert resp.status_code == 400
        assert resp.json()["detail"]["error"] == "invalid_credential_type"

    @patch("src.internal.credential_routes.get_settings")
    def test_both_scopes_grants_materialize(self, mock_settings, db: AsyncSession):
        """Identity with BOTH scopes -> materialize succeeds."""
        mock_settings.return_value = _settings_mock(bucket="test-bucket")
        asyncio.get_event_loop().run_until_complete(
            _seed_credential(
                db,
                cred_id="cred-mat-both",
                service="github",
                label="deploy-key",
                credential_type="certificate",
            )
        )
        sm = _mock_sm("cert-data")
        ctx = _fake_token_context(credential_scopes=["credential:raw-read", "credential:materialize"])

        fake_url = "https://s3.amazonaws.com/test-bucket/vault/materialize/both"

        from src.internal.credential_binding import resolve_credential_binding

        async def _selective_to_thread(fn, *args, **kwargs):
            if fn is resolve_credential_binding:
                return fn(*args, **kwargs)
            return fake_url

        with (
            patch("src.internal.routes.get_settings", return_value=_settings_mock()),
            patch("asyncio.to_thread", new=_selective_to_thread),
        ):
            client = _make_app(db, mock_sm=sm, token_context=ctx)
            resp = client.post("/internal/v1/credential-materialize", json=self.MATERIALIZE_BODY)

        assert resp.status_code == 201


# ---------------------------------------------------------------------------
# Mutation guard tests: verify gate omission or fallback is rejected
# ---------------------------------------------------------------------------


class TestCapabilityGateMutationGuards:
    """Verify the gate cannot be bypassed by header or body manipulation.

    These tests exist to catch regressions where the registry gate is removed
    or accidentally falls back to the old header-based check.
    """

    BODY = {
        "user_id": "user-cap",
        "agent_id": "agent-cap-001",
        "task_id": "task-guard-01",
        "service": "github",
        "label": "default",
    }

    @patch("src.internal.credential_routes.get_settings")
    def test_header_alone_cannot_authorize_raw_read(self, mock_settings, db: AsyncSession):
        """Even with a valid header, an empty registry scope must fail."""
        mock_settings.return_value = _settings_mock(raw_read_enabled=True)
        asyncio.get_event_loop().run_until_complete(_seed_credential(db, cred_id="cred-guard-rr", service="github", label="default"))
        sm = _mock_sm("should-not-reach")
        ctx = _fake_token_context(credential_scopes=[])

        with patch("src.internal.routes.get_settings", return_value=_settings_mock()):
            client = _make_app(db, mock_sm=sm, token_context=ctx)
            resp = client.post(
                "/internal/v1/credential-raw-read",
                json={**self.BODY, "purpose": "test"},
                headers={"X-Agent-Scopes": "credential:raw-read"},
            )

        assert resp.status_code == 403
        sm.get_secret.assert_not_called()

    @patch("src.internal.credential_routes.get_settings")
    def test_header_alone_cannot_authorize_materialize(self, mock_settings, db: AsyncSession):
        """Even with a valid header, an empty registry scope must fail."""
        mock_settings.return_value = _settings_mock(bucket="test-bucket")
        asyncio.get_event_loop().run_until_complete(
            _seed_credential(
                db,
                cred_id="cred-guard-mat",
                service="github",
                label="default",
                credential_type="ssh_key",
            )
        )
        sm = _mock_sm("should-not-reach")
        ctx = _fake_token_context(credential_scopes=[])

        with patch("src.internal.routes.get_settings", return_value=_settings_mock()):
            client = _make_app(db, mock_sm=sm, token_context=ctx)
            resp = client.post(
                "/internal/v1/credential-materialize",
                json=self.BODY,
                headers={"X-Agent-Scopes": "credential:materialize"},
            )

        assert resp.status_code == 403
        sm.get_secret.assert_not_called()

    @patch("src.internal.credential_routes.get_settings")
    def test_multiple_forged_headers_cannot_bypass(self, mock_settings, db: AsyncSession):
        """Caller sends scope via multiple header mechanisms -> still 403."""
        mock_settings.return_value = _settings_mock(raw_read_enabled=True)
        asyncio.get_event_loop().run_until_complete(_seed_credential(db, cred_id="cred-guard-multi", service="github", label="default"))
        sm = _mock_sm("should-not-reach")
        ctx = _fake_token_context(credential_scopes=[])

        with patch("src.internal.routes.get_settings", return_value=_settings_mock()):
            client = _make_app(db, mock_sm=sm, token_context=ctx)
            resp = client.post(
                "/internal/v1/credential-raw-read",
                json={**self.BODY, "purpose": "test"},
                headers={
                    "X-Agent-Scopes": "credential:raw-read",
                    "X-Agent-Manifest-Scopes": "credential:raw-read",
                },
            )

        assert resp.status_code == 403
        sm.get_secret.assert_not_called()


@pytest.mark.parametrize("granted", [False, True])
def test_real_registry_auth_to_raw_read(db, monkeypatch, granted):
    """Resolve production IAM identity and grants; only external storage is mocked."""
    from src.internal.auth_deps import verify_internal_or_irsa

    monkeypatch.setenv("AGENT_AUTHORITY_ENABLED", "false")
    settings = _settings_mock()
    settings.trust_apigw_headers = True
    settings.apigw_provenance_secret = "test-edge-proof"
    for module in ("src.internal.auth_deps", "src.auth.middleware", "src.internal.credential_routes"):
        monkeypatch.setattr(f"{module}.get_settings", lambda: settings)
    registry = MagicMock()
    registry.get_agent_by_role_arn.return_value = {
        "agent_id": "test-legacy-worker",
        "agent_name": "server-worker",
        "org_id": "org-cap",
        "team_id": "team-cap",
        "scope": "internal",
        "credential_scopes": ["credential:raw-read"] if granted else [],
    }
    monkeypatch.setattr("src.auth.agent_registry.get_agent_registry_service", lambda: registry)
    asyncio.get_event_loop().run_until_complete(_seed_credential(db))
    sm = _mock_sm()
    client = _make_app(db, mock_sm=sm)
    del client.app.dependency_overrides[verify_internal_or_irsa]
    response = client.post(
        "/internal/v1/credential-raw-read",
        json={**TestRawReadCapabilityGate.RAW_READ_BODY, "agent_id": "forged-admin", "credential_scopes": ["credential:raw-read"]},
        headers={
            "X-Caller-Identity": "arn:aws:sts::123456789012:assumed-role/test-worker/session",
            "X-Adp-Edge-Provenance": "test-edge-proof",
            **({} if granted else {"X-Agent-Scopes": "credential:raw-read"}),
        },
    )
    registry.get_agent_by_role_arn.assert_called_once_with("arn:aws:iam::123456789012:role/test-worker")
    assert response.status_code == 503, response.text
    assert response.json()["detail"] == "agent authority is not enabled"
    sm.get_secret.assert_not_called()
