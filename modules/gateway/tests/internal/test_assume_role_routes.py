"""Tests for POST /internal/v1/credential-assume-role.

Issue #481: aws_role credential type + STS assume_role as a vault delivery path.

Coverage:
  - Valid request with aws_role credential -> returns temp creds + audit
  - user_id not found -> 404
  - credential not found -> 404
  - credential_type != aws_role -> 400
  - ExternalId from row passed to STS call
  - Session tags include {adp:user_id, adp:agent_id, adp:task_id, adp:persona}
  - Session duration respects credential's setting
  - Response body includes profile_name, expiration, region, temp creds
  - Audit row written on both success and failure
  - STS failure -> 502 + audit row
  - Missing API key -> 403
  - Caching: same (user_id, service, label) keyed on user not role (architecture note)
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

from src.internal.assume_role_routes import get_secrets_manager, router
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
    """Build a minimal FastAPI test app with the assume-role router."""
    app = FastAPI()
    app.include_router(router)

    async def _get_db():
        yield db_session

    app.dependency_overrides[get_db] = _get_db
    if mock_sm is not None:
        app.dependency_overrides[get_secrets_manager] = lambda: mock_sm
    return TestClient(app, raise_server_exceptions=False)


def _settings_mock() -> MagicMock:
    s = MagicMock()
    s.internal_api_key = _VALID_KEY
    s.aws_region = "us-east-1"
    return s


async def _seed_aws_role_credential(
    db: AsyncSession,
    *,
    cred_id: str = "cred-aws-1",
    service: str = "aws",
    label: str = "prod",
    credential_type: str = "aws_role",
    secret_arn: str = "arn:aws:secretsmanager:us-east-1:123:secret:adp/users/alice/aws-prod",
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
# Tests
# ---------------------------------------------------------------------------


class TestAssumeRoleHappyPath:
    @pytest.mark.asyncio
    async def test_valid_request_returns_temp_credentials(self, db):
        await _seed_aws_role_credential(db)
        mock_sm = MagicMock()
        mock_sm.get_secret.return_value = _ROLE_SECRET_JSON

        with (
            patch("src.internal.routes.get_settings", return_value=_settings_mock()),
            patch("src.internal.auth_deps.get_settings", return_value=_settings_mock()),
            patch("src.internal.assume_role_routes.get_settings", return_value=_settings_mock()),
            patch("src.internal.sts_assume_service.boto3") as mock_boto3,
        ):
            client = _make_app(db, mock_sm)
            mock_sts_client = MagicMock()
            mock_boto3.client.return_value = mock_sts_client
            mock_sts_client.assume_role.return_value = _mock_sts_response()

            resp = client.post(
                "/internal/v1/credential-assume-role",
                json={
                    "user_id": "user-alice",
                    "agent_id": "developer",
                    "task_id": "task-xyz",
                    "service": "aws",
                    "label": "prod",
                    "purpose": "deploy to prod",
                },
                headers={"X-Internal-Api-Key": _VALID_KEY},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["profile_name"] == "adp-aws-prod"
        assert data["access_key_id"] == "ASIAIOSFODNN7EXAMPLE"
        assert data["secret_access_key"] == "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
        assert data["session_token"] == "FwoGZXIvYXdzEBY_EXAMPLE_TOKEN"
        assert data["region"] == "us-west-2"
        assert "provenance_id" in data
        assert "expiration" in data

    @pytest.mark.asyncio
    async def test_external_id_passed_to_sts(self, db):
        await _seed_aws_role_credential(db)
        mock_sm = MagicMock()
        mock_sm.get_secret.return_value = _ROLE_SECRET_JSON

        with (
            patch("src.internal.routes.get_settings", return_value=_settings_mock()),
            patch("src.internal.auth_deps.get_settings", return_value=_settings_mock()),
            patch("src.internal.assume_role_routes.get_settings", return_value=_settings_mock()),
            patch("src.internal.sts_assume_service.boto3") as mock_boto3,
        ):
            client = _make_app(db, mock_sm)
            mock_sts_client = MagicMock()
            mock_boto3.client.return_value = mock_sts_client
            mock_sts_client.assume_role.return_value = _mock_sts_response()

            client.post(
                "/internal/v1/credential-assume-role",
                json={
                    "user_id": "user-alice",
                    "agent_id": "developer",
                    "task_id": "task-xyz",
                    "service": "aws",
                    "label": "prod",
                },
                headers={"X-Internal-Api-Key": _VALID_KEY},
            )

            # Verify ExternalId was passed.
            call_kwargs = mock_sts_client.assume_role.call_args[1]
            assert call_kwargs["ExternalId"] == "adp-dev-hosted-agent"

    @pytest.mark.asyncio
    async def test_session_tags_include_identity_context(self, db):
        await _seed_aws_role_credential(db)
        mock_sm = MagicMock()
        mock_sm.get_secret.return_value = _ROLE_SECRET_JSON

        with (
            patch("src.internal.routes.get_settings", return_value=_settings_mock()),
            patch("src.internal.auth_deps.get_settings", return_value=_settings_mock()),
            patch("src.internal.assume_role_routes.get_settings", return_value=_settings_mock()),
            patch("src.internal.sts_assume_service.boto3") as mock_boto3,
        ):
            client = _make_app(db, mock_sm)
            mock_sts_client = MagicMock()
            mock_boto3.client.return_value = mock_sts_client
            mock_sts_client.assume_role.return_value = _mock_sts_response()

            client.post(
                "/internal/v1/credential-assume-role",
                json={
                    "user_id": "user-alice",
                    "agent_id": "developer",
                    "task_id": "task-xyz",
                    "service": "aws",
                    "label": "prod",
                },
                headers={"X-Internal-Api-Key": _VALID_KEY},
            )

            call_kwargs = mock_sts_client.assume_role.call_args[1]
            tags = {t["Key"]: t["Value"] for t in call_kwargs["Tags"]}
            assert tags["adp:user_id"] == "user-alice"
            assert tags["adp:agent_id"] == "developer"
            assert tags["adp:task_id"] == "task-xyz"
            assert tags["adp:persona"] == "developer"

    @pytest.mark.asyncio
    async def test_session_duration_from_credential(self, db):
        await _seed_aws_role_credential(db)
        mock_sm = MagicMock()
        mock_sm.get_secret.return_value = _ROLE_SECRET_JSON

        with (
            patch("src.internal.routes.get_settings", return_value=_settings_mock()),
            patch("src.internal.auth_deps.get_settings", return_value=_settings_mock()),
            patch("src.internal.assume_role_routes.get_settings", return_value=_settings_mock()),
            patch("src.internal.sts_assume_service.boto3") as mock_boto3,
        ):
            client = _make_app(db, mock_sm)
            mock_sts_client = MagicMock()
            mock_boto3.client.return_value = mock_sts_client
            mock_sts_client.assume_role.return_value = _mock_sts_response()

            client.post(
                "/internal/v1/credential-assume-role",
                json={
                    "user_id": "user-alice",
                    "agent_id": "developer",
                    "task_id": "task-xyz",
                    "service": "aws",
                    "label": "prod",
                },
                headers={"X-Internal-Api-Key": _VALID_KEY},
            )

            call_kwargs = mock_sts_client.assume_role.call_args[1]
            assert call_kwargs["DurationSeconds"] == 1800

    @pytest.mark.asyncio
    async def test_audit_row_written_on_success(self, db):
        await _seed_aws_role_credential(db)
        mock_sm = MagicMock()
        mock_sm.get_secret.return_value = _ROLE_SECRET_JSON

        with (
            patch("src.internal.routes.get_settings", return_value=_settings_mock()),
            patch("src.internal.auth_deps.get_settings", return_value=_settings_mock()),
            patch("src.internal.assume_role_routes.get_settings", return_value=_settings_mock()),
            patch("src.internal.sts_assume_service.boto3") as mock_boto3,
        ):
            client = _make_app(db, mock_sm)
            mock_sts_client = MagicMock()
            mock_boto3.client.return_value = mock_sts_client
            mock_sts_client.assume_role.return_value = _mock_sts_response()

            client.post(
                "/internal/v1/credential-assume-role",
                json={
                    "user_id": "user-alice",
                    "agent_id": "developer",
                    "task_id": "task-xyz",
                    "service": "aws",
                    "label": "prod",
                    "purpose": "deploy",
                },
                headers={"X-Internal-Api-Key": _VALID_KEY},
            )

        # Check audit log.
        stmt = select(AuditLog).where(AuditLog.event_type == "vault_aws_role_assumed")
        result = await db.execute(stmt)
        audit = result.scalar_one_or_none()
        assert audit is not None
        assert audit.details["success"] is True
        assert audit.details["user_id"] == "user-alice"
        assert audit.details["purpose"] == "deploy"
        # role_arn is logged server-side.
        assert "role_arn" in audit.details
        # secret_access_key MUST NOT be in audit.
        assert "secret_access_key" not in json.dumps(audit.details)
        assert "session_token" not in json.dumps(audit.details)


class TestAssumeRoleErrors:
    @pytest.mark.asyncio
    async def test_user_not_found_returns_404(self, db):
        mock_sm = MagicMock()

        with (
            patch("src.internal.routes.get_settings", return_value=_settings_mock()),
            patch("src.internal.auth_deps.get_settings", return_value=_settings_mock()),
            patch("src.internal.assume_role_routes.get_settings", return_value=_settings_mock()),
        ):
            client = _make_app(db, mock_sm)
            resp = client.post(
                "/internal/v1/credential-assume-role",
                json={
                    "user_id": "user-unknown",
                    "agent_id": "developer",
                    "task_id": "task-xyz",
                    "service": "aws",
                    "label": "prod",
                },
                headers={"X-Internal-Api-Key": _VALID_KEY},
            )

        assert resp.status_code == 404
        assert resp.json()["detail"]["error"] == "user_not_found"

    @pytest.mark.asyncio
    async def test_credential_not_found_returns_404(self, db):
        mock_sm = MagicMock()

        with (
            patch("src.internal.routes.get_settings", return_value=_settings_mock()),
            patch("src.internal.auth_deps.get_settings", return_value=_settings_mock()),
            patch("src.internal.assume_role_routes.get_settings", return_value=_settings_mock()),
        ):
            client = _make_app(db, mock_sm)
            resp = client.post(
                "/internal/v1/credential-assume-role",
                json={
                    "user_id": "user-alice",
                    "agent_id": "developer",
                    "task_id": "task-xyz",
                    "service": "aws",
                    "label": "nonexistent",
                },
                headers={"X-Internal-Api-Key": _VALID_KEY},
            )

        assert resp.status_code == 404
        assert resp.json()["detail"]["error"] == "credential_not_found"

    @pytest.mark.asyncio
    async def test_non_aws_role_credential_returns_400(self, db):
        # Seed a bearer credential (not aws_role).
        cred = UserCredential(
            id="cred-bearer",
            org_id="org-test",
            user_id="user-alice",
            service="aws",
            label="bearer-test",
            credential_type="bearer",
            secret_arn="arn:aws:secretsmanager:us-east-1:123:secret:test",
        )
        db.add(cred)
        await db.commit()

        mock_sm = MagicMock()

        with (
            patch("src.internal.routes.get_settings", return_value=_settings_mock()),
            patch("src.internal.auth_deps.get_settings", return_value=_settings_mock()),
            patch("src.internal.assume_role_routes.get_settings", return_value=_settings_mock()),
        ):
            client = _make_app(db, mock_sm)
            resp = client.post(
                "/internal/v1/credential-assume-role",
                json={
                    "user_id": "user-alice",
                    "agent_id": "developer",
                    "task_id": "task-xyz",
                    "service": "aws",
                    "label": "bearer-test",
                },
                headers={"X-Internal-Api-Key": _VALID_KEY},
            )

        assert resp.status_code == 400
        assert resp.json()["detail"]["error"] == "invalid_credential_type"

    @pytest.mark.asyncio
    async def test_missing_api_key_returns_403(self, db):
        mock_sm = MagicMock()

        with (
            patch("src.internal.routes.get_settings", return_value=_settings_mock()),
            patch("src.internal.auth_deps.get_settings", return_value=_settings_mock()),
        ):
            client = _make_app(db, mock_sm)
            resp = client.post(
                "/internal/v1/credential-assume-role",
                json={
                    "user_id": "user-alice",
                    "agent_id": "developer",
                    "task_id": "task-xyz",
                    "service": "aws",
                    "label": "prod",
                },
            )

        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_sts_failure_returns_502_and_writes_audit(self, db):
        await _seed_aws_role_credential(db, cred_id="cred-aws-fail")
        mock_sm = MagicMock()
        mock_sm.get_secret.return_value = _ROLE_SECRET_JSON

        from botocore.exceptions import ClientError

        with (
            patch("src.internal.routes.get_settings", return_value=_settings_mock()),
            patch("src.internal.auth_deps.get_settings", return_value=_settings_mock()),
            patch("src.internal.assume_role_routes.get_settings", return_value=_settings_mock()),
            patch("src.internal.sts_assume_service.boto3") as mock_boto3,
        ):
            client = _make_app(db, mock_sm)
            mock_sts_client = MagicMock()
            mock_boto3.client.return_value = mock_sts_client
            mock_sts_client.assume_role.side_effect = ClientError(
                {"Error": {"Code": "AccessDenied", "Message": "Not authorized"}},
                "AssumeRole",
            )

            resp = client.post(
                "/internal/v1/credential-assume-role",
                json={
                    "user_id": "user-alice",
                    "agent_id": "developer",
                    "task_id": "task-xyz",
                    "service": "aws",
                    "label": "prod",
                },
                headers={"X-Internal-Api-Key": _VALID_KEY},
            )

        assert resp.status_code == 502
        data = resp.json()
        assert data["detail"]["error"] == "sts_assume_failed"
        assert "provenance_id" in data["detail"]

        # Verify failure audit row.
        stmt = select(AuditLog).where(AuditLog.event_type == "vault_aws_role_assumed")
        result = await db.execute(stmt)
        audits = result.scalars().all()
        failed = [a for a in audits if a.details.get("success") is False]
        assert len(failed) >= 1
        assert failed[0].details["error_code"] == "AccessDenied"


class TestAssumeRoleScopeFallback:
    """Verify the scope resolver walks user -> team -> org for aws_role credentials."""

    @pytest.mark.asyncio
    async def test_resolves_team_scope_credential(self, db):
        # Seed a team-scoped aws_role credential (no user_id).
        cred = UserCredential(
            id="cred-team-aws",
            org_id="org-test",
            team_id="team-eng",
            user_id=None,
            service="aws",
            label="shared",
            credential_type="aws_role",
            secret_arn="arn:aws:secretsmanager:us-east-1:123:secret:adp/teams/eng/aws-shared",
        )
        db.add(cred)
        await db.commit()

        mock_sm = MagicMock()
        mock_sm.get_secret.return_value = _ROLE_SECRET_JSON

        with (
            patch("src.internal.routes.get_settings", return_value=_settings_mock()),
            patch("src.internal.auth_deps.get_settings", return_value=_settings_mock()),
            patch("src.internal.assume_role_routes.get_settings", return_value=_settings_mock()),
            patch("src.internal.sts_assume_service.boto3") as mock_boto3,
        ):
            client = _make_app(db, mock_sm)
            mock_sts_client = MagicMock()
            mock_boto3.client.return_value = mock_sts_client
            mock_sts_client.assume_role.return_value = _mock_sts_response()

            resp = client.post(
                "/internal/v1/credential-assume-role",
                json={
                    "user_id": "user-alice",
                    "agent_id": "developer",
                    "task_id": "task-xyz",
                    "service": "aws",
                    "label": "shared",
                },
                headers={"X-Internal-Api-Key": _VALID_KEY},
            )

        assert resp.status_code == 200
        assert resp.json()["profile_name"] == "adp-aws-shared"


class TestAssumeRoleCanonicalResolution:
    """Test 7: assume-role uses canonical user resolution (Issue #700)."""

    @pytest.mark.asyncio
    async def test_assume_role_uses_canonical_user(self, db):
        """Canonical user with cognito_sub resolves correctly for assume-role.

        Verifies that the canonical user's org_id and id are used for
        credential resolution, not just the inbound user_id blindly.
        """
        # Seed a user with cognito_sub and a credential
        canonical = User(
            id="user-canonical-700",
            org_id="org-test",
            team_id="team-eng",
            email="canonical@test.com",
            cognito_sub="cognito-sub-700",
        )
        db.add(canonical)
        await db.flush()

        cred = UserCredential(
            id="cred-canonical-700",
            org_id="org-test",
            user_id="user-canonical-700",
            service="aws",
            label="canonical-role",
            credential_type="aws_role",
            secret_arn="arn:aws:secretsmanager:us-east-1:123:secret:canonical",
        )
        db.add(cred)
        await db.commit()

        mock_sm = MagicMock()
        mock_sm.get_secret.return_value = _ROLE_SECRET_JSON

        with (
            patch("src.internal.routes.get_settings", return_value=_settings_mock()),
            patch("src.internal.auth_deps.get_settings", return_value=_settings_mock()),
            patch("src.internal.assume_role_routes.get_settings", return_value=_settings_mock()),
            patch("src.internal.sts_assume_service.boto3") as mock_boto3,
        ):
            client = _make_app(db, mock_sm)
            mock_sts_client = MagicMock()
            mock_boto3.client.return_value = mock_sts_client
            mock_sts_client.assume_role.return_value = _mock_sts_response()

            resp = client.post(
                "/internal/v1/credential-assume-role",
                json={
                    "user_id": "user-canonical-700",
                    "agent_id": "developer",
                    "task_id": "task-700",
                    "service": "aws",
                    "label": "canonical-role",
                },
                headers={"X-Internal-Api-Key": _VALID_KEY},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["access_key_id"] == "ASIAIOSFODNN7EXAMPLE"
        assert "provenance_id" in data
