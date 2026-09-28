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
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.internal.assume_role_routes import get_secrets_manager, router
from src.orchestration.models import OrchestrationAcceptedPlan  # noqa: F401 — register before fixture create_all
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


def _make_app(db_session: AsyncSession, mock_sm=None, *, user="user-alice") -> TestClient:
    """Build a minimal FastAPI test app with the assume-role router."""
    app = FastAPI()
    app.include_router(router)

    async def _get_db():
        yield db_session

    app.dependency_overrides[get_db] = _get_db
    if mock_sm is not None:
        app.dependency_overrides[get_secrets_manager] = lambda: mock_sm
    from tests.internal.broker_fixture import install_broker_fixture

    install_broker_fixture(app, user=user, run="assume-run", tenant="org-test", expected_key=_VALID_KEY)
    return TestClient(app, raise_server_exceptions=False)


def _settings_mock() -> MagicMock:
    s = MagicMock()
    s.internal_api_key = _VALID_KEY
    s.aws_region = "us-east-1"
    # Issue #3175: credential binding defaults to shadow mode (off).
    s.enforce_credential_binding = False
    s.webhook_events_table = "adp-test-webhook-events"
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
    # Both kinds must be ones the platform actually mints. `"human_event"` was neither in
    # `RECOGNIZED_AUTHORITY_KINDS` nor minted anywhere in `src/`; the case passed only
    # because `authorize_worker_credential` fail-open-permitted every unrecognized kind,
    # which #4529 closed. `github_event` is the kind this case means — a customer role
    # delivered to a worker rooted in a verified GitHub event, with no engine policy.
    @pytest.mark.parametrize("authority_kind", ["github_event", "gate_decision"])
    async def test_protected_worker_preserves_existing_customer_role_delivery_without_execution_policy(self, db, monkeypatch, authority_kind):
        """Protected authentication must preserve user-created deployment roles.

        The real broker dependency and STS service run here. The external pod
        verifier, event store, Secrets Manager and STS response are test doubles.
        No session policy or permissions boundary is attached to the customer
        session; the configured external ID, duration, tags and region survive.
        """
        from src.agentauth.broker_identity import verify_broker_worker
        from src.agentauth.grants import AuthorityReference, DelegatedGrant
        from src.internal.auth_deps import verify_internal_or_irsa

        await _seed_aws_role_credential(db)
        grant = DelegatedGrant(
            grant_id="grant:customer-deploy:1",
            tenant_id="org-test",
            principal="customer-deploy#1",
            authority=AuthorityReference(authority_kind, "human-deployment-approval", "user-alice", "org-test"),
            allowed_actions=frozenset(),
            flow_id="existing-flow-without-policy",
            expires_at=datetime.now(UTC) + timedelta(hours=2),
        )
        execution = {"arrived_at": {"S": "2026-09-15T10:00:00Z"}}
        event_client = MagicMock()
        event_client.get_item.return_value = {"Item": {"authorized_user_id": {"S": "user-alice"}}}
        caller = SimpleNamespace(tenant_id="org-test", invocation_id="customer-deploy", principal="customer-deploy#1")
        runtime = SimpleNamespace(
            authenticate=lambda *_: (None, caller, None, grant),
            validate_flow=AsyncMock(),
            store=SimpleNamespace(_read=lambda *_: execution, client=event_client),
        )

        class SessionContext:
            async def __aenter__(self):
                return db

            async def __aexit__(self, *_):
                pass

        monkeypatch.setenv("AGENT_AUTHORITY_ENABLED", "true")
        monkeypatch.setattr("src.agentauth.routes.get_agent_runtime", lambda: runtime)
        monkeypatch.setattr("src.shared.database.get_session_factory", lambda: SessionContext)
        mock_sm = MagicMock()
        mock_sm.get_secret.return_value = _ROLE_SECRET_JSON
        client = _make_app(db, mock_sm)

        async def verified_transport(request: Request):
            request.state.token_context = SimpleNamespace(user_id="test-worker", credential_scopes=["credential:assume-role"])
            await verify_broker_worker(request)

        client.app.dependency_overrides[verify_internal_or_irsa] = verified_transport
        with (
            patch("src.internal.assume_role_routes.get_settings", return_value=_settings_mock()),
            patch("src.internal.sts_assume_service.boto3") as mock_boto3,
        ):
            mock_boto3.client.return_value.assume_role.return_value = _mock_sts_response()
            response = client.post(
                "/internal/v1/credential-assume-role",
                json={
                    "user_id": "user-alice",
                    "agent_id": "operations",
                    "task_id": "deploy-task",
                    "service": "aws",
                    "label": "prod",
                    "invocation_id": "customer-deploy",
                },
            )
        assert response.status_code == 200, response.text
        assert response.json()["profile_name"] == "adp-aws-prod"
        assert response.json()["region"] == "us-west-2"
        call = mock_boto3.client.return_value.assume_role.call_args.kwargs
        assert call["RoleArn"] == "arn:aws:iam::123456789012:role/ADPDeployAgent"
        assert call["ExternalId"] == "adp-dev-hosted-agent"
        assert call["DurationSeconds"] == 1800
        assert {tag["Key"]: tag["Value"] for tag in call["Tags"]}["adp:user_id"] == "user-alice"
        assert "Policy" not in call and "PolicyArns" not in call
        runtime.validate_flow.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_valid_request_returns_temp_credentials(self, db):
        await _seed_aws_role_credential(db)
        mock_sm = MagicMock()
        mock_sm.get_secret.return_value = _ROLE_SECRET_JSON

        with (
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
                    "invocation_id": "assume-run",
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
                    "invocation_id": "assume-run",
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
                    "invocation_id": "assume-run",
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
                    "invocation_id": "assume-run",
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
                    "invocation_id": "assume-run",
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
    async def test_deploy_tier_request_rejects_linked_steady_state_role_before_secret_read(self, db):
        cred = await _seed_aws_role_credential(db)
        cred.scopes = {"permission_tier": "routing"}
        await db.commit()
        sm = MagicMock()
        with (
            patch("src.internal.auth_deps.get_settings", return_value=_settings_mock()),
            patch("src.internal.assume_role_routes.get_settings", return_value=_settings_mock()),
        ):
            response = _make_app(db, sm).post(
                "/internal/v1/credential-assume-role",
                json={
                    "user_id": "user-alice",
                    "invocation_id": "assume-run",
                    "agent_id": "github-workflow",
                    "task_id": "deploy-1",
                    "label": "prod",
                    "permission_tier": "deploy-bootstrap",
                },
                headers={"X-Internal-Api-Key": _VALID_KEY},
            )
        assert response.status_code == 403
        assert response.json()["detail"]["error"] == "permission_tier_mismatch"
        sm.get_secret.assert_not_called()

    @pytest.mark.asyncio
    async def test_deploy_credential_requires_explicit_tier_selection(self, db):
        cred = await _seed_aws_role_credential(db)
        cred.scopes = {"permission_tier": "deploy-bootstrap"}
        await db.commit()
        sm = MagicMock()
        with (
            patch("src.internal.auth_deps.get_settings", return_value=_settings_mock()),
            patch("src.internal.assume_role_routes.get_settings", return_value=_settings_mock()),
        ):
            response = _make_app(db, sm).post(
                "/internal/v1/credential-assume-role",
                json={"user_id": "user-alice", "invocation_id": "assume-run", "agent_id": "developer", "task_id": "task-1", "label": "prod"},
                headers={"X-Internal-Api-Key": _VALID_KEY},
            )
        assert response.status_code == 403
        sm.get_secret.assert_not_called()

    @pytest.mark.asyncio
    async def test_user_not_found_returns_404(self, db):
        mock_sm = MagicMock()

        with (
            patch("src.internal.auth_deps.get_settings", return_value=_settings_mock()),
            patch("src.internal.assume_role_routes.get_settings", return_value=_settings_mock()),
        ):
            client = _make_app(db, mock_sm, user="user-unknown")
            resp = client.post(
                "/internal/v1/credential-assume-role",
                json={
                    "user_id": "user-unknown",
                    "invocation_id": "assume-run",
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
            patch("src.internal.auth_deps.get_settings", return_value=_settings_mock()),
            patch("src.internal.assume_role_routes.get_settings", return_value=_settings_mock()),
        ):
            client = _make_app(db, mock_sm)
            resp = client.post(
                "/internal/v1/credential-assume-role",
                json={
                    "user_id": "user-alice",
                    "invocation_id": "assume-run",
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
            patch("src.internal.auth_deps.get_settings", return_value=_settings_mock()),
            patch("src.internal.assume_role_routes.get_settings", return_value=_settings_mock()),
        ):
            client = _make_app(db, mock_sm)
            resp = client.post(
                "/internal/v1/credential-assume-role",
                json={
                    "user_id": "user-alice",
                    "invocation_id": "assume-run",
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
            patch("src.internal.auth_deps.get_settings", return_value=_settings_mock()),
        ):
            client = _make_app(db, mock_sm)
            resp = client.post(
                "/internal/v1/credential-assume-role",
                json={
                    "user_id": "user-alice",
                    "invocation_id": "assume-run",
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
                    "invocation_id": "assume-run",
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
                    "invocation_id": "assume-run",
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
            patch("src.internal.auth_deps.get_settings", return_value=_settings_mock()),
            patch("src.internal.assume_role_routes.get_settings", return_value=_settings_mock()),
            patch("src.internal.sts_assume_service.boto3") as mock_boto3,
        ):
            client = _make_app(db, mock_sm, user="user-canonical-700")
            mock_sts_client = MagicMock()
            mock_boto3.client.return_value = mock_sts_client
            mock_sts_client.assume_role.return_value = _mock_sts_response()

            resp = client.post(
                "/internal/v1/credential-assume-role",
                json={
                    "user_id": "user-canonical-700",
                    "invocation_id": "assume-run",
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


class TestWorkspaceBrokerIntegration:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("account,external_id", [("123456789012", "tenant-a-external"), ("210987654321", "tenant-b-external")])
    async def test_authorized_stored_workspace_reaches_sts_with_identity_tags(self, db, account, external_id):
        cred = await _seed_aws_role_credential(db)
        cred.scopes = {"account_id": account, "status": "verified"}
        await db.commit()
        stored = json.loads(_ROLE_SECRET_JSON)
        stored.update(account_id=account, role_arn=f"arn:aws:iam::{account}:role/Workspace", external_id=external_id)
        sm = MagicMock()
        sm.get_secret.return_value = json.dumps(stored)
        with (
            patch("src.internal.auth_deps.get_settings", return_value=_settings_mock()),
            patch("src.internal.assume_role_routes.get_settings", return_value=_settings_mock()),
            patch("src.auth.sts_client.get_settings", return_value=_settings_mock()),
            patch("src.auth.sts_client.boto3.client") as client_factory,
            patch("src.internal.assume_role_routes.assume_role") as legacy,
        ):
            client_factory.return_value.assume_role.return_value = _mock_sts_response()
            response = _make_app(db, sm).post(
                "/internal/v1/credential-assume-role",
                json={
                    "user_id": "user-alice",
                    "invocation_id": "assume-run",
                    "agent_id": "developer",
                    "task_id": "task-xyz",
                    "label": "prod",
                },
                headers={"X-Internal-Api-Key": _VALID_KEY},
            )
        assert response.status_code == 200, response.text
        legacy.assert_not_called()
        call = client_factory.return_value.assume_role.call_args.kwargs
        assert call["RoleArn"] == stored["role_arn"]
        assert call["ExternalId"] == external_id
        assert call["DurationSeconds"] == 1800
        assert {tag["Key"]: tag["Value"] for tag in call["Tags"]} == {
            "adp:user_id": "user-alice",
            "adp:agent_id": "developer",
            "adp:task_id": "task-xyz",
            "adp:persona": "developer",
        }
        assert response.json()["region"] == "us-west-2"
        assert external_id not in response.text and stored["role_arn"] not in response.text
        audit = (await db.execute(select(AuditLog).where(AuditLog.event_type == "vault_aws_role_assumed"))).scalar_one()
        assert audit.details["success"] is True
        assert audit.details["authorized_user_id"] == "user-alice"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "overrides",
        [
            {"external_id": None},
            {"external_id": 123},
            {"external_id": " "},
            {"account_id": "210987654321"},
            {"role_arn": "arn:aws:iam::210987654321:role/Other"},
            {"account_id": "210987654321", "role_arn": "arn:aws:iam::210987654321:role/Other"},
        ],
    )
    async def test_invalid_workspace_metadata_refuses_before_sts_and_audits(self, db, overrides):
        cred = await _seed_aws_role_credential(db)
        cred.scopes = {"account_id": "123456789012"}
        await db.commit()
        stored = {**json.loads(_ROLE_SECRET_JSON), "account_id": "123456789012", **overrides}
        sm = MagicMock()
        sm.get_secret.return_value = json.dumps(stored)
        with (
            patch("src.internal.auth_deps.get_settings", return_value=_settings_mock()),
            patch("src.internal.assume_role_routes.get_settings", return_value=_settings_mock()),
            patch("src.auth.sts_client.get_settings", return_value=_settings_mock()),
            patch("src.auth.sts_client.boto3.client") as client_factory,
            patch("src.internal.assume_role_routes.assume_role") as legacy,
        ):
            response = _make_app(db, sm).post(
                "/internal/v1/credential-assume-role",
                json={
                    "user_id": "user-alice",
                    "invocation_id": "assume-run",
                    "agent_id": "developer",
                    "task_id": "task-xyz",
                    "label": "prod",
                },
                headers={"X-Internal-Api-Key": _VALID_KEY},
            )
        assert response.status_code == 502, response.text
        client_factory.return_value.assume_role.assert_not_called()
        legacy.assert_not_called()
        assert stored["role_arn"] not in response.text
        audit = (await db.execute(select(AuditLog).where(AuditLog.event_type == "vault_aws_role_assumed"))).scalar_one()
        assert audit.details["success"] is False

    @pytest.mark.asyncio
    async def test_imported_role_retains_optional_external_id(self, db):
        cred = await _seed_aws_role_credential(db)
        cred.scopes = {"account_id": "123456789012", "source": "imported_role"}
        await db.commit()
        stored = {**json.loads(_ROLE_SECRET_JSON), "account_id": "123456789012", "external_id": ""}
        sm = MagicMock()
        sm.get_secret.return_value = json.dumps(stored)
        with (
            patch("src.internal.auth_deps.get_settings", return_value=_settings_mock()),
            patch("src.internal.assume_role_routes.get_settings", return_value=_settings_mock()),
            patch("src.internal.assume_role_routes.STSClient") as broker,
            patch("src.internal.sts_assume_service.boto3.client") as client_factory,
        ):
            client_factory.return_value.assume_role.return_value = _mock_sts_response()
            response = _make_app(db, sm).post(
                "/internal/v1/credential-assume-role",
                json={
                    "user_id": "user-alice",
                    "invocation_id": "assume-run",
                    "agent_id": "developer",
                    "task_id": "task-xyz",
                    "label": "prod",
                },
                headers={"X-Internal-Api-Key": _VALID_KEY},
            )
        assert response.status_code == 200, response.text
        broker.assert_not_called()
        assert "ExternalId" not in client_factory.return_value.assume_role.call_args.kwargs
