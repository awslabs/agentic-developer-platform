"""Tests for AWS account connect flow (Issue #562).

Coverage:
  - connect_start creates pending row + SM secret, returns valid launch URL
  - connect_start URL is under 8000 chars (browser limit)
  - connect_start template is valid YAML
  - verify success when assume_role succeeds → status flips to verified
  - verify failure propagates user-friendly reason
  - verify is idempotent (second call on verified row is no-op)
  - tenant isolation: user A cannot verify user B's pending credential
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch
from urllib.parse import unquote

import boto3
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from moto import mock_aws
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

# The cfn_template module reads ADP_CFN_TEMPLATE_BUCKET from env to decide
# which bucket to pre-sign against.  Set it BEFORE the module is imported
# anywhere in the test process so the first call gets the right value.
os.environ.setdefault("ADP_CFN_TEMPLATE_BUCKET", "adp-test-cfn-templates")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")

from src.auth.aws_connect_routes import router
from src.auth.middleware import get_current_user_context
from src.auth.vault_routes import get_secrets_manager
from src.shared.database import get_db
from src.shared.models.base import Base
from src.shared.models.organization import Organization, Team
from src.shared.models.vault import UserCredential  # noqa: F401 — needed for metadata.create_all
from src.shared.schemas.auth import TokenContext

# ---------------------------------------------------------------------------
# Test database setup
# ---------------------------------------------------------------------------

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


def make_engine():
    return create_async_engine(
        TEST_DATABASE_URL,
        echo=False,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_context(
    user_id: str = "user-alice",
    org_id: str = "org-acme",
    team_id: str = "team-eng",
    is_admin: bool = False,
) -> TokenContext:
    return TokenContext(
        user_id=user_id,
        org_id=org_id,
        team_id=team_id,
        department_id="dept-eng",
        account_type="human",
        is_admin=is_admin,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


# IMPORTANT: TokenContext.user_id is the Cognito sub (a UUID). The handler
# resolves it to the Postgres users.id via cognito_sub, which is why we seed
# a matching `users` row per test context below.
ALICE_COGNITO_SUB = "sub-alice-cognito"
ALICE_DB_ID = "db-id-alice"
BOB_COGNITO_SUB = "sub-bob-cognito"
BOB_DB_ID = "db-id-bob"

ALICE = _make_context(user_id=ALICE_COGNITO_SUB)
BOB = _make_context(user_id=BOB_COGNITO_SUB)
# Issue #600: User with empty org_id in token (GitHub-federated)
ALICE_EMPTY_ORG = _make_context(user_id=ALICE_COGNITO_SUB, org_id="")


class MockSecretsManager:
    """Mock SM that stores secrets in a dict."""

    def __init__(self):
        self._secrets: dict[str, str] = {}
        self._counter = 0

    def create_secret(self, service: str, label: str, payload: str | dict, **kwargs) -> str:
        self._counter += 1
        arn = f"arn:aws:secretsmanager:us-east-1:123456789012:secret:test-{self._counter}"
        if isinstance(payload, dict):
            payload = json.dumps(payload)
        self._secrets[arn] = payload
        return arn

    def get_secret(self, secret_arn: str) -> str:
        return self._secrets[secret_arn]

    def delete_secret(self, secret_arn: str, **kwargs) -> None:
        self._secrets.pop(secret_arn, None)

    def update_secret(self, secret_arn: str, payload: str | dict) -> None:
        if isinstance(payload, dict):
            payload = json.dumps(payload)
        self._secrets[secret_arn] = payload


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _moto_s3():
    """Stub S3 so ``generate_presigned_url`` returns a URL for a real (mock) bucket."""
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=os.environ["ADP_CFN_TEMPLATE_BUCKET"])
        s3.put_object(
            Bucket=os.environ["ADP_CFN_TEMPLATE_BUCKET"],
            Key="cfn-templates/aws_role_v1.yaml",
            Body=b"placeholder",
        )
        yield


@pytest.fixture
def mock_sm():
    return MockSecretsManager()


@pytest.fixture
def app_and_client(mock_sm):
    """Create a test app with the AWS connect router + in-memory DB."""
    engine = make_engine()
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    import asyncio

    async def _setup():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async with session_factory() as session:
            from src.shared.models.organization import User

            org = Organization(id="org-acme", name="Acme Corp")
            session.add(org)
            team = Team(id="team-eng", name="Eng Team", org_id="org-acme", department_id="dept-eng")
            session.add(team)
            # Seed Postgres users rows keyed by cognito_sub — the handler
            # looks these up via User.cognito_sub to resolve the FK for
            # user_credentials.user_id.
            session.add(
                User(
                    id=ALICE_DB_ID,
                    org_id="org-acme",
                    team_id="team-eng",
                    email="alice@example.com",
                    name="Alice",
                    cognito_sub=ALICE_COGNITO_SUB,
                )
            )
            session.add(
                User(
                    id=BOB_DB_ID,
                    org_id="org-acme",
                    team_id="team-eng",
                    email="bob@example.com",
                    name="Bob",
                    cognito_sub=BOB_COGNITO_SUB,
                )
            )
            await session.commit()

    asyncio.run(_setup())

    app = FastAPI()
    app.include_router(router)

    # Override dependencies
    async def get_test_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = get_test_db
    app.dependency_overrides[get_secrets_manager] = lambda: mock_sm

    client = TestClient(app)
    return app, client


@pytest.fixture
def alice_client(app_and_client):
    app, client = app_and_client
    app.dependency_overrides[get_current_user_context] = lambda: ALICE
    return client


@pytest.fixture
def bob_client(app_and_client):
    app, client = app_and_client
    app.dependency_overrides[get_current_user_context] = lambda: BOB
    return client


# ---------------------------------------------------------------------------
# Tests: connect/start
# ---------------------------------------------------------------------------


class TestConnectStart:
    def test_creates_pending_row_and_returns_launch_url(self, alice_client, mock_sm):
        resp = alice_client.post(
            "/auth/credentials/aws/connect",
            json={"nickname": "prod-readonly", "account_id": "123456789012"},
        )
        assert resp.status_code == 201
        body = resp.json()
        assert "credential_id" in body
        assert "launch_url" in body
        assert "console.aws.amazon.com/cloudformation" in body["launch_url"]
        assert "quickcreate" in body["launch_url"]

        # Verify SM was called
        assert len(mock_sm._secrets) == 1
        secret_value = list(mock_sm._secrets.values())[0]
        parsed = json.loads(secret_value)
        assert parsed["account_id"] == "123456789012"
        assert parsed["role_arn"] == "arn:aws:iam::123456789012:role/ADP-Agent-prod-readonly"
        assert "external_id" in parsed

    def test_launch_url_includes_correct_params(self, alice_client):
        resp = alice_client.post(
            "/auth/credentials/aws/connect",
            json={"nickname": "my-role", "account_id": "111222333444"},
        )
        assert resp.status_code == 201
        url = resp.json()["launch_url"]

        # Check key parameters are in the URL
        assert "stackName=ADP-Agent-my-role" in url
        assert "param_Nickname=my-role" in url
        # UserSessionTag must be the Postgres users.id (what STS sees in the
        # session tag), not the Cognito sub — see handler comment.
        assert f"param_UserSessionTag={ALICE_DB_ID}" in url
        assert "param_GatewayRolePrincipal=" in url
        assert "param_GatewayAccountId=" in url

    def test_launch_url_under_browser_limit(self, alice_client):
        """URL must be under 8000 chars to fit in browser URL bars."""
        # Use worst-case parameter lengths
        long_nickname = "a" * 64
        resp = alice_client.post(
            "/auth/credentials/aws/connect",
            json={"nickname": long_nickname, "account_id": "999888777666"},
        )
        assert resp.status_code == 201
        url = resp.json()["launch_url"]
        assert len(url) < 8000, f"Launch URL exceeds browser limit: {len(url)} chars"

    def test_launch_url_uses_s3_presigned_template_url(self, alice_client):
        """AWS Console requires templateURL, and CFN only accepts S3 hosts."""
        resp = alice_client.post(
            "/auth/credentials/aws/connect",
            json={"nickname": "test", "account_id": "123456789012"},
        )
        url = resp.json()["launch_url"]
        fragment_query = url.split("quickcreate?", 1)[1]

        assert "templateURL=" in fragment_query
        assert "templateBody=" not in fragment_query

        template_url_param = next(p for p in fragment_query.split("&") if p.startswith("templateURL="))
        decoded = unquote(template_url_param[len("templateURL=") :])
        # Virtual-hosted S3 host + pre-signed query params.  Match the bucket
        # from the test-env ADP_CFN_TEMPLATE_BUCKET setting (see conftest).
        assert ".s3." in decoded or ".s3.amazonaws.com" in decoded
        assert "X-Amz-Signature=" in decoded or "Signature=" in decoded
        assert "cfn-templates/aws_role_v1.yaml" in decoded

    def test_rejects_invalid_account_id(self, alice_client):
        resp = alice_client.post(
            "/auth/credentials/aws/connect",
            json={"nickname": "test", "account_id": "12345"},
        )
        assert resp.status_code == 422

    def test_rejects_empty_nickname(self, alice_client):
        resp = alice_client.post(
            "/auth/credentials/aws/connect",
            json={"nickname": "", "account_id": "123456789012"},
        )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Tests: verify
# ---------------------------------------------------------------------------


class TestConnectVerify:
    def _create_pending_credential(self, client) -> str:
        """Helper: create a pending credential and return its ID."""
        resp = client.post(
            "/auth/credentials/aws/connect",
            json={"nickname": "verify-test", "account_id": "123456789012"},
        )
        assert resp.status_code == 201
        return resp.json()["credential_id"]

    @patch("src.auth.aws_connect_routes.assume_role")
    def test_verify_success_flips_status(self, mock_assume, alice_client):
        """When STS AssumeRole succeeds, status becomes verified."""
        mock_assume.return_value = MagicMock(
            access_key_id="AKIA...",
            secret_access_key="secret",
            session_token="token",
            expiration="2026-01-01T00:00:00Z",
            region="us-east-1",
            profile_name="adp-aws-verify-test",
        )

        cred_id = self._create_pending_credential(alice_client)
        resp = alice_client.post(
            "/auth/credentials/aws/verify",
            json={"credential_id": cred_id},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "verified"

    @patch("src.auth.aws_connect_routes.assume_role")
    def test_verify_failure_returns_reason(self, mock_assume, alice_client):
        """When STS fails with NoSuchEntity, return user-friendly reason."""
        from src.internal.sts_assume_service import STSAssumeError

        mock_assume.side_effect = STSAssumeError("role not found", code="NoSuchEntity")

        cred_id = self._create_pending_credential(alice_client)
        resp = alice_client.post(
            "/auth/credentials/aws/verify",
            json={"credential_id": cred_id},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "failed"
        assert "not been created yet" in body["reason"]

    @patch("src.auth.aws_connect_routes.assume_role")
    def test_verify_access_denied_reason(self, mock_assume, alice_client):
        """AccessDenied returns a trust-policy-related message."""
        from src.internal.sts_assume_service import STSAssumeError

        mock_assume.side_effect = STSAssumeError("access denied", code="AccessDenied")

        cred_id = self._create_pending_credential(alice_client)
        resp = alice_client.post(
            "/auth/credentials/aws/verify",
            json={"credential_id": cred_id},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "failed"
        assert "trust policy" in body["reason"]

    @patch("src.auth.aws_connect_routes.assume_role")
    def test_verify_is_idempotent(self, mock_assume, alice_client):
        """Second verify on an already-verified row is a no-op."""
        mock_assume.return_value = MagicMock()

        cred_id = self._create_pending_credential(alice_client)

        # First verify
        resp1 = alice_client.post(
            "/auth/credentials/aws/verify",
            json={"credential_id": cred_id},
        )
        assert resp1.json()["status"] == "verified"

        # Second verify — no STS call needed
        mock_assume.reset_mock()
        resp2 = alice_client.post(
            "/auth/credentials/aws/verify",
            json={"credential_id": cred_id},
        )
        assert resp2.json()["status"] == "verified"
        # Should NOT have called assume_role again
        mock_assume.assert_not_called()

    def test_verify_returns_404_for_nonexistent(self, alice_client):
        resp = alice_client.post(
            "/auth/credentials/aws/verify",
            json={"credential_id": "nonexistent-id"},
        )
        assert resp.status_code == 404

    def test_tenant_isolation_bob_cannot_verify_alice(self, app_and_client, mock_sm):
        """User B cannot verify user A's pending credential."""
        app, client = app_and_client

        # Create as Alice
        app.dependency_overrides[get_current_user_context] = lambda: ALICE
        resp = client.post(
            "/auth/credentials/aws/connect",
            json={"nickname": "isolation-test", "account_id": "123456789012"},
        )
        assert resp.status_code == 201
        cred_id = resp.json()["credential_id"]

        # Verify as Bob — should 404
        app.dependency_overrides[get_current_user_context] = lambda: BOB
        resp = client.post(
            "/auth/credentials/aws/verify",
            json={"credential_id": cred_id},
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Tests: Issue #600 — org_id fallback for GitHub-federated users
# ---------------------------------------------------------------------------


class TestOrgIdFallback:
    """Regression tests for Issue #600: empty org_id in token falls back to users.org_id."""

    def test_connect_start_uses_db_org_id_when_token_empty(self, app_and_client, mock_sm):
        """When token_context.org_id is empty, credential is written with users.org_id."""
        app, client = app_and_client
        app.dependency_overrides[get_current_user_context] = lambda: ALICE_EMPTY_ORG

        resp = client.post(
            "/auth/credentials/aws/connect",
            json={"nickname": "fallback-test", "account_id": "123456789012"},
        )
        assert resp.status_code == 201
        cred_id = resp.json()["credential_id"]

        # Verify endpoint should find the credential even with empty org_id
        # token (because both write and read fall back to DB org_id)
        with patch("src.auth.aws_connect_routes.assume_role") as mock_assume:
            mock_assume.return_value = MagicMock()
            resp2 = client.post(
                "/auth/credentials/aws/verify",
                json={"credential_id": cred_id},
            )
        assert resp2.status_code == 200
        assert resp2.json()["status"] == "verified"

    @patch("src.auth.aws_connect_routes.assume_role")
    def test_verify_uses_db_org_id_when_token_empty(self, mock_assume, app_and_client, mock_sm):
        """Verify endpoint resolves org_id from DB when token is empty."""
        app, client = app_and_client
        mock_assume.return_value = MagicMock()

        # Create credential with normal token (has org_id)
        app.dependency_overrides[get_current_user_context] = lambda: ALICE
        resp = client.post(
            "/auth/credentials/aws/connect",
            json={"nickname": "verify-fallback", "account_id": "123456789012"},
        )
        assert resp.status_code == 201
        cred_id = resp.json()["credential_id"]

        # Now verify with empty org_id token — should still find it via fallback
        app.dependency_overrides[get_current_user_context] = lambda: ALICE_EMPTY_ORG
        resp2 = client.post(
            "/auth/credentials/aws/verify",
            json={"credential_id": cred_id},
        )
        assert resp2.status_code == 200
        assert resp2.json()["status"] == "verified"
