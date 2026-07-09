"""Tests for credential-authorization binding extension (Issue #3477).

Extends binding to: proxy-request, credential-materialize, user-credentials.
Follows the same pattern established in test_credential_authorization_binding.py
for raw-read and assume-role.

Coverage:
  - TestProxyRequestBinding: enforce + shadow mode for proxy-request
  - TestMaterializeBinding: enforce + shadow mode for credential-materialize
  - TestListCredentialsBinding: enforce + shadow mode for user-credentials GET
  - TestConfigDefault: enforce_credential_binding defaults to True
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.internal.credential_routes import get_secrets_manager as cr_get_secrets_manager
from src.internal.credential_routes import router as credential_router
from src.shared.database import get_db
from src.shared.models.base import Base
from src.shared.models.organization import Department, Organization, Team, User
from src.shared.models.vault import UserCredential

# ---------------------------------------------------------------------------
# Test infrastructure
# ---------------------------------------------------------------------------

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"
_VALID_KEY = "test-internal-api-key"

_USER_ALICE_ID = "user-alice-binding-ext"
_USER_BOB_ID = "user-bob-binding-ext"
_INVOCATION_ID = "inv-ext-abc123-def456"


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
            id="org-binding-ext",
            name="Binding Extension Test Org",
            aws_accounts=[],
            role_mappings={},
            settings={},
            github_installation_ids=[],
            cognito_client_ids=[],
        )
        dept = Department(id="dept-eng-ext", org_id="org-binding-ext", name="Engineering")
        team = Team(id="team-eng-ext", org_id="org-binding-ext", department_id="dept-eng-ext", name="Eng")
        alice = User(
            id=_USER_ALICE_ID,
            org_id="org-binding-ext",
            team_id="team-eng-ext",
            email="alice-ext@test.com",
        )
        bob = User(
            id=_USER_BOB_ID,
            org_id="org-binding-ext",
            team_id="team-eng-ext",
            email="bob-ext@test.com",
        )
        session.add_all([org, dept, team, alice, bob])
        await session.flush()

        # Seed credentials for alice.
        bearer_cred = UserCredential(
            id="cred-ext-bearer",
            org_id="org-binding-ext",
            user_id=_USER_ALICE_ID,
            service="github",
            label="main",
            credential_type="bearer",
            secret_arn="arn:aws:secretsmanager:us-east-1:123:secret:ext-bearer",
        )
        ssh_cred = UserCredential(
            id="cred-ext-ssh",
            org_id="org-binding-ext",
            user_id=_USER_ALICE_ID,
            service="ssh",
            label="deploy",
            credential_type="ssh_key",
            secret_arn="arn:aws:secretsmanager:us-east-1:123:secret:ext-ssh",
        )
        session.add_all([bearer_cred, ssh_cred])
        await session.commit()
        yield session


def _make_app(db_session: AsyncSession, mock_sm=None) -> TestClient:
    """Build a minimal FastAPI test app with the credential routes router."""
    app = FastAPI()
    app.include_router(credential_router)

    async def _get_db():
        yield db_session

    app.dependency_overrides[get_db] = _get_db
    if mock_sm is not None:
        app.dependency_overrides[cr_get_secrets_manager] = lambda: mock_sm
    return TestClient(app, raise_server_exceptions=False)


def _settings_mock(*, enforce: bool = False):
    """Create a settings mock with credential binding config."""
    s = MagicMock()
    s.internal_api_key = _VALID_KEY
    s.aws_region = "us-east-1"
    s.enforce_credential_binding = enforce
    s.webhook_events_table = "adp-test-webhook-events"
    s.vault_proxy_host_allowlist = "api.github.com,httpbin.org"
    s.vault_materialization_bucket = "test-bucket"
    s.vault_raw_read_enabled = True
    return s


def _mock_ddb_query_response(authorized_user_id: str | None = None, arrived_at: str = "2026-07-08T12:00:00Z"):
    """Create a mock for DDB Query response."""
    if authorized_user_id is None:
        return {"Items": [], "Count": 0, "ScannedCount": 0}
    if authorized_user_id == "":
        return {
            "Items": [{"event_id": _INVOCATION_ID, "arrived_at": arrived_at}],
            "Count": 1,
            "ScannedCount": 1,
        }
    return {
        "Items": [
            {
                "event_id": _INVOCATION_ID,
                "arrived_at": arrived_at,
                "authorized_user_id": authorized_user_id,
            }
        ],
        "Count": 1,
        "ScannedCount": 1,
    }


# ---------------------------------------------------------------------------
# Tests: proxy-request with binding
# ---------------------------------------------------------------------------


class TestProxyRequestBinding:
    """Tests for proxy-request with credential-authorization binding (#3477)."""

    @pytest.mark.asyncio
    async def test_enforce_missing_invocation_id_returns_403(self, db):
        """Enforce mode: missing invocation_id -> 403 (not a fallback to body user_id)."""
        settings = _settings_mock(enforce=True)

        with (
            patch("src.internal.routes.get_settings", return_value=settings),
            patch("src.internal.auth_deps.get_settings", return_value=settings),
            patch("src.internal.credential_routes.get_settings", return_value=settings),
        ):
            client = _make_app(db)
            resp = client.post(
                "/internal/v1/proxy-request",
                json={
                    "user_id": _USER_ALICE_ID,
                    "agent_id": "developer",
                    "task_id": "task-proxy-1",
                    "service": "github",
                    "method": "GET",
                    "url": "https://api.github.com/user",
                    # No invocation_id!
                },
                headers={"X-Internal-Api-Key": _VALID_KEY},
            )

        assert resp.status_code == 403
        detail = resp.json()["detail"]
        assert detail["error"] == "credential_binding_failed"
        assert "invocation_id" in detail["message"]

    @pytest.mark.asyncio
    async def test_enforce_drift_returns_403(self, db):
        """Enforce mode: invocation_id whose registry authorized_user_id != body user_id -> 403."""
        settings = _settings_mock(enforce=True)

        with (
            patch("src.internal.routes.get_settings", return_value=settings),
            patch("src.internal.auth_deps.get_settings", return_value=settings),
            patch("src.internal.credential_routes.get_settings", return_value=settings),
            patch("src.internal.credential_binding._get_dynamodb_table") as mock_get_table,
        ):
            mock_table = MagicMock()
            mock_get_table.return_value = mock_table
            # Registry says alice, body says bob — spoof attempt.
            mock_table.query.return_value = _mock_ddb_query_response(_USER_ALICE_ID)

            client = _make_app(db)
            resp = client.post(
                "/internal/v1/proxy-request",
                json={
                    "user_id": _USER_BOB_ID,  # Mismatch!
                    "agent_id": "developer",
                    "task_id": "task-proxy-2",
                    "service": "github",
                    "method": "GET",
                    "url": "https://api.github.com/user",
                    "invocation_id": _INVOCATION_ID,
                },
                headers={"X-Internal-Api-Key": _VALID_KEY},
            )

        assert resp.status_code == 403
        detail = resp.json()["detail"]
        assert detail["error"] == "credential_authorization_drift"

    @pytest.mark.asyncio
    async def test_enforce_valid_invocation_returns_200(self, db):
        """Enforce mode: valid matching invocation_id -> 200, credential resolved for authorized user."""
        mock_sm = MagicMock()
        mock_sm.get_secret.return_value = "ghp_secret_token_value"

        settings = _settings_mock(enforce=True)

        with (
            patch("src.internal.routes.get_settings", return_value=settings),
            patch("src.internal.auth_deps.get_settings", return_value=settings),
            patch("src.internal.credential_routes.get_settings", return_value=settings),
            patch("src.internal.credential_binding._get_dynamodb_table") as mock_get_table,
            patch("httpx.AsyncClient.request") as mock_http,
        ):
            mock_table = MagicMock()
            mock_get_table.return_value = mock_table
            mock_table.query.return_value = _mock_ddb_query_response(_USER_ALICE_ID)

            # Mock the upstream HTTP response.
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.headers = {"content-type": "application/json"}
            mock_response.text = '{"login":"alice"}'
            mock_http.return_value = mock_response

            client = _make_app(db, mock_sm)
            resp = client.post(
                "/internal/v1/proxy-request",
                json={
                    "user_id": _USER_ALICE_ID,
                    "agent_id": "developer",
                    "task_id": "task-proxy-3",
                    "service": "github",
                    "label": "main",
                    "method": "GET",
                    "url": "https://api.github.com/user",
                    "invocation_id": _INVOCATION_ID,
                },
                headers={"X-Internal-Api-Key": _VALID_KEY},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == 200
        assert "provenance_id" in data

    @pytest.mark.asyncio
    async def test_shadow_mode_fallback_uses_body(self, db):
        """Shadow mode (flag false): no invocation_id -> still returns body user_id (no block)."""
        mock_sm = MagicMock()
        mock_sm.get_secret.return_value = "ghp_secret_token_value"

        settings = _settings_mock(enforce=False)

        with (
            patch("src.internal.routes.get_settings", return_value=settings),
            patch("src.internal.auth_deps.get_settings", return_value=settings),
            patch("src.internal.credential_routes.get_settings", return_value=settings),
            patch("httpx.AsyncClient.request") as mock_http,
        ):
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.headers = {"content-type": "application/json"}
            mock_response.text = '{"login":"alice"}'
            mock_http.return_value = mock_response

            client = _make_app(db, mock_sm)
            resp = client.post(
                "/internal/v1/proxy-request",
                json={
                    "user_id": _USER_ALICE_ID,
                    "agent_id": "developer",
                    "task_id": "task-proxy-4",
                    "service": "github",
                    "label": "main",
                    "method": "GET",
                    "url": "https://api.github.com/user",
                    # No invocation_id — shadow mode should fall back.
                },
                headers={"X-Internal-Api-Key": _VALID_KEY},
            )

        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Tests: credential-materialize with binding
# ---------------------------------------------------------------------------


class TestMaterializeBinding:
    """Tests for credential-materialize with credential-authorization binding (#3477)."""

    @pytest.mark.asyncio
    async def test_enforce_missing_invocation_id_returns_403(self, db):
        """Enforce mode: missing invocation_id -> 403."""
        settings = _settings_mock(enforce=True)

        with (
            patch("src.internal.routes.get_settings", return_value=settings),
            patch("src.internal.auth_deps.get_settings", return_value=settings),
            patch("src.internal.credential_routes.get_settings", return_value=settings),
        ):
            client = _make_app(db)
            resp = client.post(
                "/internal/v1/credential-materialize",
                json={
                    "user_id": _USER_ALICE_ID,
                    "agent_id": "developer",
                    "task_id": "task-mat-1",
                    "service": "ssh",
                    "label": "deploy",
                    # No invocation_id!
                },
                headers={
                    "X-Internal-Api-Key": _VALID_KEY,
                    "X-Agent-Scopes": "credential:materialize",
                },
            )

        assert resp.status_code == 403
        detail = resp.json()["detail"]
        assert detail["error"] == "credential_binding_failed"
        assert "invocation_id" in detail["message"]

    @pytest.mark.asyncio
    async def test_enforce_drift_returns_403(self, db):
        """Enforce mode: registry user != body user -> 403 (spoof blocked)."""
        settings = _settings_mock(enforce=True)

        with (
            patch("src.internal.routes.get_settings", return_value=settings),
            patch("src.internal.auth_deps.get_settings", return_value=settings),
            patch("src.internal.credential_routes.get_settings", return_value=settings),
            patch("src.internal.credential_binding._get_dynamodb_table") as mock_get_table,
        ):
            mock_table = MagicMock()
            mock_get_table.return_value = mock_table
            mock_table.query.return_value = _mock_ddb_query_response(_USER_ALICE_ID)

            client = _make_app(db)
            resp = client.post(
                "/internal/v1/credential-materialize",
                json={
                    "user_id": _USER_BOB_ID,  # Mismatch!
                    "agent_id": "developer",
                    "task_id": "task-mat-2",
                    "service": "ssh",
                    "label": "deploy",
                    "invocation_id": _INVOCATION_ID,
                },
                headers={
                    "X-Internal-Api-Key": _VALID_KEY,
                    "X-Agent-Scopes": "credential:materialize",
                },
            )

        assert resp.status_code == 403
        detail = resp.json()["detail"]
        assert detail["error"] == "credential_authorization_drift"

    @pytest.mark.asyncio
    async def test_enforce_valid_invocation_returns_201(self, db):
        """Enforce mode: valid invocation_id -> 201, materialize succeeds."""
        mock_sm = MagicMock()
        mock_sm.get_secret.return_value = "ssh-rsa AAAA..."

        settings = _settings_mock(enforce=True)

        with (
            patch("src.internal.routes.get_settings", return_value=settings),
            patch("src.internal.auth_deps.get_settings", return_value=settings),
            patch("src.internal.credential_routes.get_settings", return_value=settings),
            patch("src.internal.credential_binding._get_dynamodb_table") as mock_get_table,
            patch("boto3.client") as mock_boto3,
        ):
            mock_table = MagicMock()
            mock_get_table.return_value = mock_table
            mock_table.query.return_value = _mock_ddb_query_response(_USER_ALICE_ID)

            # Mock S3 upload + presign.
            mock_s3 = MagicMock()
            mock_boto3.return_value = mock_s3
            mock_s3.generate_presigned_url.return_value = "https://s3.example.com/presigned"

            client = _make_app(db, mock_sm)
            resp = client.post(
                "/internal/v1/credential-materialize",
                json={
                    "user_id": _USER_ALICE_ID,
                    "agent_id": "developer",
                    "task_id": "task-mat-3",
                    "service": "ssh",
                    "label": "deploy",
                    "invocation_id": _INVOCATION_ID,
                },
                headers={
                    "X-Internal-Api-Key": _VALID_KEY,
                    "X-Agent-Scopes": "credential:materialize",
                },
            )

        assert resp.status_code == 201
        data = resp.json()
        assert "materialize_url" in data
        assert "provenance_id" in data

    @pytest.mark.asyncio
    async def test_shadow_mode_fallback_uses_body(self, db):
        """Shadow mode: no invocation_id -> still resolves via body user_id (no block)."""
        mock_sm = MagicMock()
        mock_sm.get_secret.return_value = "ssh-rsa AAAA..."

        settings = _settings_mock(enforce=False)

        with (
            patch("src.internal.routes.get_settings", return_value=settings),
            patch("src.internal.auth_deps.get_settings", return_value=settings),
            patch("src.internal.credential_routes.get_settings", return_value=settings),
            patch("boto3.client") as mock_boto3,
        ):
            mock_s3 = MagicMock()
            mock_boto3.return_value = mock_s3
            mock_s3.generate_presigned_url.return_value = "https://s3.example.com/presigned"

            client = _make_app(db, mock_sm)
            resp = client.post(
                "/internal/v1/credential-materialize",
                json={
                    "user_id": _USER_ALICE_ID,
                    "agent_id": "developer",
                    "task_id": "task-mat-4",
                    "service": "ssh",
                    "label": "deploy",
                    # No invocation_id — shadow mode.
                },
                headers={
                    "X-Internal-Api-Key": _VALID_KEY,
                    "X-Agent-Scopes": "credential:materialize",
                },
            )

        assert resp.status_code == 201


# ---------------------------------------------------------------------------
# Tests: user-credentials (GET) with binding
# ---------------------------------------------------------------------------


class TestListCredentialsBinding:
    """Tests for user-credentials GET with credential-authorization binding (#3477)."""

    @pytest.mark.asyncio
    async def test_enforce_missing_invocation_id_returns_403(self, db):
        """Enforce mode: missing invocation_id -> 403."""
        settings = _settings_mock(enforce=True)

        with (
            patch("src.internal.routes.get_settings", return_value=settings),
            patch("src.internal.auth_deps.get_settings", return_value=settings),
            patch("src.internal.credential_routes.get_settings", return_value=settings),
        ):
            client = _make_app(db)
            resp = client.get(
                f"/internal/v1/user-credentials?user_id={_USER_ALICE_ID}",
                headers={"X-Internal-Api-Key": _VALID_KEY},
            )

        assert resp.status_code == 403
        detail = resp.json()["detail"]
        assert detail["error"] == "credential_binding_failed"
        assert "invocation_id" in detail["message"]

    @pytest.mark.asyncio
    async def test_enforce_drift_returns_403(self, db):
        """Enforce mode: registry user != query user_id -> 403."""
        settings = _settings_mock(enforce=True)

        with (
            patch("src.internal.routes.get_settings", return_value=settings),
            patch("src.internal.auth_deps.get_settings", return_value=settings),
            patch("src.internal.credential_routes.get_settings", return_value=settings),
            patch("src.internal.credential_binding._get_dynamodb_table") as mock_get_table,
        ):
            mock_table = MagicMock()
            mock_get_table.return_value = mock_table
            # Registry says alice, query says bob.
            mock_table.query.return_value = _mock_ddb_query_response(_USER_ALICE_ID)

            client = _make_app(db)
            resp = client.get(
                f"/internal/v1/user-credentials?user_id={_USER_BOB_ID}&invocation_id={_INVOCATION_ID}",
                headers={"X-Internal-Api-Key": _VALID_KEY},
            )

        assert resp.status_code == 403
        detail = resp.json()["detail"]
        assert detail["error"] == "credential_authorization_drift"

    @pytest.mark.asyncio
    async def test_enforce_valid_invocation_returns_200(self, db):
        """Enforce mode: valid invocation_id -> 200, credentials listed for authorized user."""
        settings = _settings_mock(enforce=True)

        with (
            patch("src.internal.routes.get_settings", return_value=settings),
            patch("src.internal.auth_deps.get_settings", return_value=settings),
            patch("src.internal.credential_routes.get_settings", return_value=settings),
            patch("src.internal.credential_binding._get_dynamodb_table") as mock_get_table,
        ):
            mock_table = MagicMock()
            mock_get_table.return_value = mock_table
            mock_table.query.return_value = _mock_ddb_query_response(_USER_ALICE_ID)

            client = _make_app(db)
            resp = client.get(
                f"/internal/v1/user-credentials?user_id={_USER_ALICE_ID}&invocation_id={_INVOCATION_ID}",
                headers={"X-Internal-Api-Key": _VALID_KEY},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        # Alice has 2 credentials seeded.
        assert len(data) == 2
        services = {c["service"] for c in data}
        assert "github" in services
        assert "ssh" in services

    @pytest.mark.asyncio
    async def test_shadow_mode_fallback_uses_body(self, db):
        """Shadow mode: no invocation_id -> returns credentials for body user_id."""
        settings = _settings_mock(enforce=False)

        with (
            patch("src.internal.routes.get_settings", return_value=settings),
            patch("src.internal.auth_deps.get_settings", return_value=settings),
            patch("src.internal.credential_routes.get_settings", return_value=settings),
        ):
            client = _make_app(db)
            resp = client.get(
                f"/internal/v1/user-credentials?user_id={_USER_ALICE_ID}",
                headers={"X-Internal-Api-Key": _VALID_KEY},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2

    @pytest.mark.asyncio
    async def test_enforce_resolves_to_authorized_user_not_body(self, db):
        """When body and registry differ, credentials resolved for AUTHORIZED user (from registry)."""
        settings = _settings_mock(enforce=False)

        with (
            patch("src.internal.routes.get_settings", return_value=settings),
            patch("src.internal.auth_deps.get_settings", return_value=settings),
            patch("src.internal.credential_routes.get_settings", return_value=settings),
            patch("src.internal.credential_binding._get_dynamodb_table") as mock_get_table,
        ):
            mock_table = MagicMock()
            mock_get_table.return_value = mock_table
            # Registry says alice even though body says bob. Shadow mode: no block,
            # but the resolved user should be alice (registry wins).
            mock_table.query.return_value = _mock_ddb_query_response(_USER_ALICE_ID)

            client = _make_app(db)
            resp = client.get(
                f"/internal/v1/user-credentials?user_id={_USER_BOB_ID}&invocation_id={_INVOCATION_ID}",
                headers={"X-Internal-Api-Key": _VALID_KEY},
            )

        # Shadow mode: no block. Uses registry (alice), so returns alice's creds.
        assert resp.status_code == 200
        data = resp.json()
        # Alice has 2 credentials, bob has 0.
        assert len(data) == 2


# ---------------------------------------------------------------------------
# Tests: config default
# ---------------------------------------------------------------------------


class TestConfigDefault:
    """Verify enforce_credential_binding code default is True (Issue #3477)."""

    def test_config_default_is_true(self):
        """Fresh Settings() with no env override should have enforce=True."""
        from src.shared.config import Settings

        # Create with explicit overrides for required fields to avoid env interference.
        # The key assertion is that enforce_credential_binding is True by default.
        s = Settings(
            internal_api_key="test",
            aws_region="us-east-1",
            database_url="sqlite:///test.db",
        )
        assert s.enforce_credential_binding is True
