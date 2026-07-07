"""Tests for credential-authorization binding (S2).

Issue #3175: gateway resolves user from registry, ENFORCE_CREDENTIAL_BINDING flag
+ drift metrics.

Coverage:
  - A1: raw-read rejects mismatched user_id (enforce mode) -> 403
  - A2: assume-role rejects mismatched user_id (enforce mode) -> 403
  - A7: raw-read uses registry not body (match -> 200)
  - test_missing_invocation_id_rejected (enforce) -> 403
  - test_fallback_mode_uses_body (flag off) -> body used, no block
  - test_drift_detection_audit_logged: mismatch in shadow -> audit + metric, no block

Fixtures use access-token-shaped IDs (not id-token-shaped).
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.internal.assume_role_routes import get_secrets_manager as ar_get_secrets_manager
from src.internal.assume_role_routes import router as assume_role_router
from src.internal.credential_routes import get_secrets_manager as cr_get_secrets_manager
from src.internal.credential_routes import router as credential_router
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

# Access-token-shaped user IDs (cognito sub format, not id-token email).
_USER_ALICE_ID = "user-alice-binding"
_USER_BOB_ID = "user-bob-binding"
_INVOCATION_ID = "inv-abc123-def456"

_ROLE_SECRET_JSON = json.dumps(
    {
        "role_arn": "arn:aws:iam::123456789012:role/ADPDeployAgent",
        "external_id": "adp-dev-hosted-agent",
        "session_duration_seconds": 1800,
        "default_region": "us-west-2",
    }
)


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
            id="org-binding",
            name="Binding Test Org",
            aws_accounts=[],
            role_mappings={},
            settings={},
            github_installation_ids=[],
            cognito_client_ids=[],
        )
        dept = Department(id="dept-eng-b", org_id="org-binding", name="Engineering")
        team = Team(id="team-eng-b", org_id="org-binding", department_id="dept-eng-b", name="Eng")
        alice = User(
            id=_USER_ALICE_ID,
            org_id="org-binding",
            team_id="team-eng-b",
            email="alice-binding@test.com",
        )
        bob = User(
            id=_USER_BOB_ID,
            org_id="org-binding",
            team_id="team-eng-b",
            email="bob-binding@test.com",
        )
        session.add_all([org, dept, team, alice, bob])
        await session.flush()

        # Seed credentials for alice (bearer for raw-read, aws_role for assume-role).
        bearer_cred = UserCredential(
            id="cred-binding-bearer",
            org_id="org-binding",
            user_id=_USER_ALICE_ID,
            service="github",
            label="main",
            credential_type="bearer",
            secret_arn="arn:aws:secretsmanager:us-east-1:123:secret:binding-bearer",
        )
        aws_cred = UserCredential(
            id="cred-binding-aws",
            org_id="org-binding",
            user_id=_USER_ALICE_ID,
            service="aws",
            label="prod",
            credential_type="aws_role",
            secret_arn="arn:aws:secretsmanager:us-east-1:123:secret:binding-aws",
        )
        session.add_all([bearer_cred, aws_cred])
        await session.commit()
        yield session


def _make_raw_read_app(db_session: AsyncSession, mock_sm=None) -> TestClient:
    """Build a minimal FastAPI test app with the credential routes router."""
    app = FastAPI()
    app.include_router(credential_router)

    async def _get_db():
        yield db_session

    app.dependency_overrides[get_db] = _get_db
    if mock_sm is not None:
        app.dependency_overrides[cr_get_secrets_manager] = lambda: mock_sm
    return TestClient(app, raise_server_exceptions=False)


def _make_assume_role_app(db_session: AsyncSession, mock_sm=None) -> TestClient:
    """Build a minimal FastAPI test app with the assume-role router."""
    app = FastAPI()
    app.include_router(assume_role_router)

    async def _get_db():
        yield db_session

    app.dependency_overrides[get_db] = _get_db
    if mock_sm is not None:
        app.dependency_overrides[ar_get_secrets_manager] = lambda: mock_sm
    return TestClient(app, raise_server_exceptions=False)


def _settings_mock(*, enforce: bool = False, raw_read_enabled: bool = True):
    """Create a settings mock with credential binding config."""
    s = MagicMock()
    s.internal_api_key = _VALID_KEY
    s.aws_region = "us-east-1"
    s.vault_raw_read_enabled = raw_read_enabled
    s.enforce_credential_binding = enforce
    s.webhook_events_table = "adp-test-webhook-events"
    return s


def _mock_ddb_get_item(authorized_user_id: str | None = None):
    """Create a mock for DDB GetItem response.

    If authorized_user_id is None, simulates item not found.
    """
    if authorized_user_id is None:
        return {"ResponseMetadata": {"HTTPStatusCode": 200}}
    if authorized_user_id == "":
        return {"Item": {"event_id": _INVOCATION_ID}}
    return {
        "Item": {
            "event_id": _INVOCATION_ID,
            "authorized_user_id": authorized_user_id,
        }
    }


def _mock_sts_response():
    """Return a mock STS AssumeRole response dict."""
    return {
        "Credentials": {
            "AccessKeyId": "ASIAIOSFODNN7EXAMPLE",
            "SecretAccessKey": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            "SessionToken": "FwoGZXIvYXdzEBY_EXAMPLE_TOKEN",
            "Expiration": datetime(2026, 5, 7, 17, 0, 0, tzinfo=UTC),
        },
        "AssumedRoleUser": {
            "AssumedRoleId": "AROAIDIODR4TAW7QUMT3D:adp-developer-task-xyz",
            "Arn": "arn:aws:sts::123456789012:assumed-role/ADPDeployAgent/adp-developer-task-xyz",
        },
    }


# ---------------------------------------------------------------------------
# Tests: credential-raw-read with binding
# ---------------------------------------------------------------------------


class TestRawReadBinding:
    """Tests for credential-raw-read with credential-authorization binding."""

    @pytest.mark.asyncio
    async def test_raw_read_uses_registry_not_body(self, db):
        """A7: When registry returns matching authorized_user_id, endpoint uses it
        for credential resolution and returns 200."""
        mock_sm = MagicMock()
        mock_sm.get_secret.return_value = "ghp_secret_token_value"

        settings = _settings_mock(enforce=False)

        with (
            patch("src.internal.routes.get_settings", return_value=settings),
            patch("src.internal.auth_deps.get_settings", return_value=settings),
            patch("src.internal.credential_routes.get_settings", return_value=settings),
            patch("src.internal.credential_binding._get_dynamodb_table") as mock_get_table,
        ):
            mock_table = MagicMock()
            mock_get_table.return_value = mock_table
            mock_table.get_item.return_value = _mock_ddb_get_item(_USER_ALICE_ID)

            client = _make_raw_read_app(db, mock_sm)
            resp = client.post(
                "/internal/v1/credential-raw-read",
                json={
                    "user_id": _USER_ALICE_ID,
                    "agent_id": "developer",
                    "task_id": "task-binding-1",
                    "service": "github",
                    "label": "main",
                    "invocation_id": _INVOCATION_ID,
                },
                headers={
                    "X-Internal-Api-Key": _VALID_KEY,
                    "X-Agent-Scopes": "credential:raw-read",
                },
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["value"] == "ghp_secret_token_value"
        assert data["credential_type"] == "bearer"

        # Verify DDB was queried with the invocation_id.
        mock_table.get_item.assert_called_once_with(
            Key={"event_id": _INVOCATION_ID},
            ProjectionExpression="authorized_user_id",
            ConsistentRead=False,
        )

    @pytest.mark.asyncio
    async def test_raw_read_rejects_mismatched_user_id(self, db):
        """A1: enforce mode + body user != registry user -> 403 drift."""
        settings = _settings_mock(enforce=True)

        with (
            patch("src.internal.routes.get_settings", return_value=settings),
            patch("src.internal.auth_deps.get_settings", return_value=settings),
            patch("src.internal.credential_routes.get_settings", return_value=settings),
            patch("src.internal.credential_binding._get_dynamodb_table") as mock_get_table,
        ):
            mock_table = MagicMock()
            mock_get_table.return_value = mock_table
            # Registry says alice, but body says bob.
            mock_table.get_item.return_value = _mock_ddb_get_item(_USER_ALICE_ID)

            client = _make_raw_read_app(db)
            resp = client.post(
                "/internal/v1/credential-raw-read",
                json={
                    "user_id": _USER_BOB_ID,  # Mismatch!
                    "agent_id": "developer",
                    "task_id": "task-binding-2",
                    "service": "github",
                    "label": "main",
                    "invocation_id": _INVOCATION_ID,
                },
                headers={
                    "X-Internal-Api-Key": _VALID_KEY,
                    "X-Agent-Scopes": "credential:raw-read",
                },
            )

        assert resp.status_code == 403
        detail = resp.json()["detail"]
        assert detail["error"] == "credential_authorization_drift"

    @pytest.mark.asyncio
    async def test_missing_invocation_id_rejected_enforce(self, db):
        """Enforce mode: missing invocation_id -> 403."""
        settings = _settings_mock(enforce=True)

        with (
            patch("src.internal.routes.get_settings", return_value=settings),
            patch("src.internal.auth_deps.get_settings", return_value=settings),
            patch("src.internal.credential_routes.get_settings", return_value=settings),
        ):
            client = _make_raw_read_app(db)
            resp = client.post(
                "/internal/v1/credential-raw-read",
                json={
                    "user_id": _USER_ALICE_ID,
                    "agent_id": "developer",
                    "task_id": "task-binding-3",
                    "service": "github",
                    "label": "main",
                    # No invocation_id!
                },
                headers={
                    "X-Internal-Api-Key": _VALID_KEY,
                    "X-Agent-Scopes": "credential:raw-read",
                },
            )

        assert resp.status_code == 403
        detail = resp.json()["detail"]
        assert detail["error"] == "credential_binding_failed"
        assert "invocation_id" in detail["message"]

    @pytest.mark.asyncio
    async def test_fallback_mode_uses_body(self, db):
        """Shadow mode (default flag off): no invocation_id -> uses body user_id, no block."""
        mock_sm = MagicMock()
        mock_sm.get_secret.return_value = "ghp_secret_token_value"

        settings = _settings_mock(enforce=False)

        with (
            patch("src.internal.routes.get_settings", return_value=settings),
            patch("src.internal.auth_deps.get_settings", return_value=settings),
            patch("src.internal.credential_routes.get_settings", return_value=settings),
        ):
            client = _make_raw_read_app(db, mock_sm)
            resp = client.post(
                "/internal/v1/credential-raw-read",
                json={
                    "user_id": _USER_ALICE_ID,
                    "agent_id": "developer",
                    "task_id": "task-binding-4",
                    "service": "github",
                    "label": "main",
                    # No invocation_id — shadow mode should fall back to body.
                },
                headers={
                    "X-Internal-Api-Key": _VALID_KEY,
                    "X-Agent-Scopes": "credential:raw-read",
                },
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["value"] == "ghp_secret_token_value"

    @pytest.mark.asyncio
    async def test_drift_detection_audit_logged(self, db):
        """Shadow mode: mismatch in shadow -> audit + drift info, no block."""
        mock_sm = MagicMock()
        mock_sm.get_secret.return_value = "ghp_secret_token_value"

        settings = _settings_mock(enforce=False)

        with (
            patch("src.internal.routes.get_settings", return_value=settings),
            patch("src.internal.auth_deps.get_settings", return_value=settings),
            patch("src.internal.credential_routes.get_settings", return_value=settings),
            patch("src.internal.credential_binding._get_dynamodb_table") as mock_get_table,
        ):
            mock_table = MagicMock()
            mock_get_table.return_value = mock_table
            # Registry says alice, body says bob — drift! But shadow mode = no block.
            mock_table.get_item.return_value = _mock_ddb_get_item(_USER_ALICE_ID)

            client = _make_raw_read_app(db, mock_sm)
            resp = client.post(
                "/internal/v1/credential-raw-read",
                json={
                    "user_id": _USER_BOB_ID,  # Mismatch with registry
                    "agent_id": "developer",
                    "task_id": "task-binding-5",
                    "service": "github",
                    "label": "main",
                    "invocation_id": _INVOCATION_ID,
                },
                headers={
                    "X-Internal-Api-Key": _VALID_KEY,
                    "X-Agent-Scopes": "credential:raw-read",
                },
            )

        # Shadow mode: request succeeds (uses registry user = alice's credential).
        assert resp.status_code == 200

        # Verify audit log records drift.
        stmt = select(AuditLog).where(AuditLog.event_type == "vault_credential_raw_read")
        result = await db.execute(stmt)
        audits = result.scalars().all()
        # Find the audit with binding drift.
        drift_audit = [a for a in audits if a.details.get("binding_drift_detected") is True]
        assert len(drift_audit) >= 1
        audit = drift_audit[0]
        assert audit.details["user_id"] == _USER_BOB_ID
        assert audit.details["authorized_user_id"] == _USER_ALICE_ID
        assert audit.details["binding_from_registry"] is True
        assert audit.details["invocation_id"] == _INVOCATION_ID


# ---------------------------------------------------------------------------
# Tests: credential-assume-role with binding
# ---------------------------------------------------------------------------


class TestAssumeRoleBinding:
    """Tests for credential-assume-role with credential-authorization binding."""

    @pytest.mark.asyncio
    async def test_assume_role_rejects_mismatched_user_id(self, db):
        """A2: enforce mode + body user != registry user -> 403 drift."""
        settings = _settings_mock(enforce=True)

        with (
            patch("src.internal.routes.get_settings", return_value=settings),
            patch("src.internal.auth_deps.get_settings", return_value=settings),
            patch("src.internal.assume_role_routes.get_settings", return_value=settings),
            patch("src.internal.credential_binding._get_dynamodb_table") as mock_get_table,
        ):
            mock_table = MagicMock()
            mock_get_table.return_value = mock_table
            # Registry says alice, body says bob.
            mock_table.get_item.return_value = _mock_ddb_get_item(_USER_ALICE_ID)

            client = _make_assume_role_app(db)
            resp = client.post(
                "/internal/v1/credential-assume-role",
                json={
                    "user_id": _USER_BOB_ID,  # Mismatch!
                    "agent_id": "developer",
                    "task_id": "task-binding-ar-1",
                    "service": "aws",
                    "label": "prod",
                    "invocation_id": _INVOCATION_ID,
                },
                headers={"X-Internal-Api-Key": _VALID_KEY},
            )

        assert resp.status_code == 403
        detail = resp.json()["detail"]
        assert detail["error"] == "credential_authorization_drift"

    @pytest.mark.asyncio
    async def test_assume_role_uses_registry_user_for_sts_tags(self, db):
        """Verify STS session tags use authorized_user_id from registry."""
        mock_sm = MagicMock()
        mock_sm.get_secret.return_value = _ROLE_SECRET_JSON

        settings = _settings_mock(enforce=False)

        with (
            patch("src.internal.routes.get_settings", return_value=settings),
            patch("src.internal.auth_deps.get_settings", return_value=settings),
            patch("src.internal.assume_role_routes.get_settings", return_value=settings),
            patch("src.internal.credential_binding._get_dynamodb_table") as mock_get_table,
            patch("src.internal.sts_assume_service.boto3") as mock_boto3,
        ):
            mock_table = MagicMock()
            mock_get_table.return_value = mock_table
            mock_table.get_item.return_value = _mock_ddb_get_item(_USER_ALICE_ID)

            mock_sts_client = MagicMock()
            mock_boto3.client.return_value = mock_sts_client
            mock_sts_client.assume_role.return_value = _mock_sts_response()

            client = _make_assume_role_app(db, mock_sm)
            resp = client.post(
                "/internal/v1/credential-assume-role",
                json={
                    "user_id": _USER_ALICE_ID,
                    "agent_id": "developer",
                    "task_id": "task-binding-ar-2",
                    "service": "aws",
                    "label": "prod",
                    "invocation_id": _INVOCATION_ID,
                },
                headers={"X-Internal-Api-Key": _VALID_KEY},
            )

        assert resp.status_code == 200

        # Verify STS session tags use the registry user_id.
        call_kwargs = mock_sts_client.assume_role.call_args[1]
        tags = {t["Key"]: t["Value"] for t in call_kwargs["Tags"]}
        assert tags["adp:user_id"] == _USER_ALICE_ID

    @pytest.mark.asyncio
    async def test_assume_role_missing_invocation_id_enforce(self, db):
        """Enforce mode: missing invocation_id -> 403."""
        settings = _settings_mock(enforce=True)

        with (
            patch("src.internal.routes.get_settings", return_value=settings),
            patch("src.internal.auth_deps.get_settings", return_value=settings),
            patch("src.internal.assume_role_routes.get_settings", return_value=settings),
        ):
            client = _make_assume_role_app(db)
            resp = client.post(
                "/internal/v1/credential-assume-role",
                json={
                    "user_id": _USER_ALICE_ID,
                    "agent_id": "developer",
                    "task_id": "task-binding-ar-3",
                    "service": "aws",
                    "label": "prod",
                    # No invocation_id!
                },
                headers={"X-Internal-Api-Key": _VALID_KEY},
            )

        assert resp.status_code == 403
        detail = resp.json()["detail"]
        assert detail["error"] == "credential_binding_failed"

    @pytest.mark.asyncio
    async def test_assume_role_fallback_shadow_mode(self, db):
        """Shadow mode: no invocation_id -> body user_id used, no block."""
        mock_sm = MagicMock()
        mock_sm.get_secret.return_value = _ROLE_SECRET_JSON

        settings = _settings_mock(enforce=False)

        with (
            patch("src.internal.routes.get_settings", return_value=settings),
            patch("src.internal.auth_deps.get_settings", return_value=settings),
            patch("src.internal.assume_role_routes.get_settings", return_value=settings),
            patch("src.internal.sts_assume_service.boto3") as mock_boto3,
        ):
            mock_sts_client = MagicMock()
            mock_boto3.client.return_value = mock_sts_client
            mock_sts_client.assume_role.return_value = _mock_sts_response()

            client = _make_assume_role_app(db, mock_sm)
            resp = client.post(
                "/internal/v1/credential-assume-role",
                json={
                    "user_id": _USER_ALICE_ID,
                    "agent_id": "developer",
                    "task_id": "task-binding-ar-4",
                    "service": "aws",
                    "label": "prod",
                    # No invocation_id — shadow mode fallback.
                },
                headers={"X-Internal-Api-Key": _VALID_KEY},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["access_key_id"] == "ASIAIOSFODNN7EXAMPLE"

        # STS tags should use body user_id (fallback).
        call_kwargs = mock_sts_client.assume_role.call_args[1]
        tags = {t["Key"]: t["Value"] for t in call_kwargs["Tags"]}
        assert tags["adp:user_id"] == _USER_ALICE_ID
