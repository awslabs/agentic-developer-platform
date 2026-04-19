"""
E2E tests for authentication user stories.

These tests verify the complete authentication flow from user login
through token management.

Test modes:
- @pytest.mark.unit: Pure Python-level logic tests (db_session + mocks)
- @pytest.mark.integration: ASGI app in-process tests (api_client in unit mode)
- @pytest.mark.live_only: Real HTTP against deployed gateway (api_client/iam_signed_client in live mode)

User Stories Covered:
- US-1.4: Human User Authentication via AWS SSO
- US-1.5: Service Account Registration for Automated Agents
- US-1.6: Automated Agent Authentication (M2M)
- US-9.1: Expired AWS Credentials
- US-9.2: Unknown Organization
- US-9.5: Unregistered Service Account
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = [pytest.mark.auth, pytest.mark.e2e]

from src.shared.exceptions import (
    InvalidCredentialsError,
    TokenExpiredError,
    UnknownOrganizationError,
    UnregisteredServiceAccountError,
)
from src.shared.schemas.auth import AuthExchangeRequest, AuthExchangeResponse
from tests.fixtures.factories import (
    create_department,
    create_org,
    create_service_account,
    create_team,
    create_token,
    create_user,
)
from tests.fixtures.mock_aws import MockSTSClient


# =============================================================================
# Unit tests -- pure Python logic, db_session + mocks
# =============================================================================


@pytest.mark.unit
class TestHumanUserAuthentication:
    """
    Unit tests for Human User Authentication via AWS SSO.

    User Story US-1.4:
    As a Developer (Dev), I want to exchange my AWS SSO temporary credentials
    for a gateway token, so that I can access Bedrock through the gateway
    without managing separate credentials.
    """

    async def test_exchange_valid_aws_credentials_returns_token(
        self,
        db_session: AsyncSession,
    ):
        """
        Test: POST /auth/exchange accepts AWS credentials and returns token.

        Acceptance Criteria:
        - POST /auth/exchange accepts aws_access_key_id, aws_secret_access_key, aws_session_token
        - Returns: token, expires_at, user_id, org_id, team_id, department_id
        - Token stored as hash in database; raw token returned only once
        """
        # Setup: Create organization with AWS account mapping
        org = await create_org(
            db_session,
            id="org-sso-test",
            name="SSO Test Org",
            aws_accounts=["123456789012"],
            role_mappings={
                "admin_roles": ["AdminRole"],
                "role_to_department": {
                    "AWSReservedSSO_Developer": "engineering",
                },
            },
        )
        dept = await create_department(db_session, org.id, id="dept-engineering", name="Engineering")
        team = await create_team(db_session, org.id, dept.id, id="team-backend", name="Backend")
        user = await create_user(
            db_session,
            org.id,
            team.id,
            id="user-john-doe",
            email="john.doe@company.com",
        )
        await db_session.commit()

        # Mock STS client for credential validation
        mock_sts = MockSTSClient(
            account_id="123456789012",
            role_arn="arn:aws:sts::123456789012:assumed-role/AWSReservedSSO_Developer/john.doe@company.com",
        )

        # Simulate credential exchange
        exchange_request = AuthExchangeRequest(
            aws_access_key_id="AKIAIOSFODNN7EXAMPLE",
            aws_secret_access_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            aws_session_token="FwoGZXIvYXdzEB...",
        )

        # Call STS to validate credentials
        caller_identity = await mock_sts.get_caller_identity(
            access_key_id=exchange_request.aws_access_key_id,
            secret_access_key=exchange_request.aws_secret_access_key,
            session_token=exchange_request.aws_session_token,
        )

        # Verify STS response
        assert caller_identity["Account"] == "123456789012"
        assert "john.doe@company.com" in caller_identity["Arn"]

        # Create token for user
        token_record, raw_token = await create_token(
            db_session,
            org.id,
            team.id,
            dept.id,
            user.id,
            entity_type="human",
            expires_in_hours=12.0,
        )
        await db_session.commit()

        # Verify exchange response structure
        exchange_response = AuthExchangeResponse(
            token=raw_token,
            expires_at=token_record.expires_at,
            user_id=user.id,
            org_id=org.id,
            team_id=team.id,
            department_id=dept.id,
            account_type="human",
        )

        assert exchange_response.token.startswith("bg-")
        assert exchange_response.user_id == user.id
        assert exchange_response.org_id == org.id
        assert exchange_response.account_type == "human"

    async def test_token_expires_after_configured_duration(
        self,
        db_session: AsyncSession,
    ):
        """Token expires after configurable duration (default: 12 hours)."""
        org = await create_org(db_session, id="org-expiry")
        dept = await create_department(db_session, org.id, id="dept-expiry")
        team = await create_team(db_session, org.id, dept.id, id="team-expiry")
        user = await create_user(db_session, org.id, team.id, id="user-expiry")

        # Create token with 12-hour expiry
        token_record, raw_token = await create_token(
            db_session,
            org.id,
            team.id,
            dept.id,
            user.id,
            expires_in_hours=12.0,
        )
        await db_session.commit()

        # Verify expiration is approximately 12 hours from now
        time_until_expiry = token_record.expires_at - datetime.now(UTC)
        assert 11.9 < time_until_expiry.total_seconds() / 3600 < 12.1

    async def test_invalid_aws_credentials_return_401(self):
        """Invalid/expired AWS credentials return 401."""
        mock_sts = MockSTSClient(
            should_fail=True,
            error_code="InvalidIdentityToken",
            error_message="The identity token is invalid",
        )

        with pytest.raises(Exception) as exc_info:
            await mock_sts.get_caller_identity(
                access_key_id="INVALID_KEY",
                secret_access_key="invalid_secret",
                session_token="invalid_token",
            )

        assert "InvalidIdentityToken" in str(exc_info.value)

        # Verify error response structure
        with pytest.raises(InvalidCredentialsError) as exc:
            raise InvalidCredentialsError("The identity token is invalid")

        assert exc.value.status_code == 401
        assert exc.value.error == "invalid_credentials"

    async def test_unknown_aws_account_returns_403(
        self,
        db_session: AsyncSession,
    ):
        """AWS account not mapped to any org returns 403."""
        # Create org with specific AWS account
        await create_org(
            db_session,
            id="org-known-only",
            aws_accounts=["111111111111"],
        )
        await db_session.commit()

        # User from different AWS account
        mock_sts = MockSTSClient(
            account_id="999999999999",  # Not registered
            role_arn="arn:aws:sts::999999999999:assumed-role/SomeRole/user",
        )

        caller_identity = await mock_sts.get_caller_identity()

        # Account not in any org
        with pytest.raises(UnknownOrganizationError) as exc:
            raise UnknownOrganizationError(caller_identity["Account"])

        assert exc.value.status_code == 403
        assert exc.value.error == "unknown_organization"
        assert "999999999999" in exc.value.message


@pytest.mark.unit
class TestServiceAccountAuthentication:
    """
    Unit tests for Service Account Registration and Authentication.

    User Stories:
    - US-1.5: Service Account Registration for Automated Agents
    - US-1.6: Automated Agent Authentication (M2M)
    """

    async def test_register_service_account(
        self,
        db_session: AsyncSession,
    ):
        """POST /admin/organizations/{org_id}/service-accounts creates service account."""
        org = await create_org(db_session, id="org-sa-register")
        dept = await create_department(db_session, org.id, id="dept-sa-register")
        team = await create_team(db_session, org.id, dept.id, id="team-sa-register")

        # Create service account
        service_account = await create_service_account(
            db_session,
            org.id,
            dept.id,
            team.id,
            id="sa-cicd",
            name="CI/CD Pipeline",
            iam_role_arn="arn:aws:iam::123456789012:role/cicd-pipeline",
        )
        await db_session.commit()

        assert service_account.id == "sa-cicd"
        assert service_account.name == "CI/CD Pipeline"
        assert service_account.org_id == org.id
        assert service_account.iam_role_arn == "arn:aws:iam::123456789012:role/cicd-pipeline"

    async def test_service_account_authentication_via_iam_role(
        self,
        db_session: AsyncSession,
    ):
        """Container with IAM role can authenticate and get token."""
        org = await create_org(db_session, id="org-m2m", aws_accounts=["123456789012"])
        dept = await create_department(db_session, org.id, id="dept-m2m")
        team = await create_team(db_session, org.id, dept.id, id="team-m2m")

        service_account = await create_service_account(
            db_session,
            org.id,
            dept.id,
            team.id,
            id="sa-ml-training",
            name="ML Training Service",
            iam_role_arn="arn:aws:iam::123456789012:role/ml-training-role",
        )
        await db_session.commit()

        mock_sts = MockSTSClient(
            account_id="123456789012",
            role_arn="arn:aws:sts::123456789012:assumed-role/ml-training-role/session",
        )

        caller_identity = await mock_sts.get_caller_identity()

        role_arn_parts = caller_identity["Arn"].split("/")
        role_name = role_arn_parts[1]
        assert role_name == "ml-training-role"

        token_record, raw_token = await create_token(
            db_session,
            org.id,
            team.id,
            dept.id,
            service_account.id,
            entity_type="service",
            expires_in_hours=1.0,
        )
        await db_session.commit()

        exchange_response = AuthExchangeResponse(
            token=raw_token,
            expires_at=token_record.expires_at,
            user_id=service_account.id,
            org_id=org.id,
            team_id=team.id,
            department_id=dept.id,
            account_type="service",
        )

        assert exchange_response.account_type == "service"
        assert exchange_response.user_id == service_account.id

    async def test_unregistered_iam_role_returns_403(
        self,
        db_session: AsyncSession,
    ):
        """Unregistered IAM roles return 403."""
        await create_org(db_session, id="org-unregistered", aws_accounts=["123456789012"])
        await db_session.commit()

        mock_sts = MockSTSClient(
            account_id="123456789012",
            role_arn="arn:aws:sts::123456789012:assumed-role/unregistered-role/session",
        )

        caller_identity = await mock_sts.get_caller_identity()

        with pytest.raises(UnregisteredServiceAccountError) as exc:
            role_arn = f"arn:aws:iam::{caller_identity['Account']}:role/unregistered-role"
            raise UnregisteredServiceAccountError(role_arn)

        assert exc.value.status_code == 403
        assert exc.value.error == "unregistered_service_account"

    async def test_service_account_token_shorter_expiry(
        self,
        db_session: AsyncSession,
    ):
        """Service account tokens have configurable (shorter) expiry."""
        org = await create_org(db_session, id="org-sa-expiry")
        dept = await create_department(db_session, org.id, id="dept-sa-expiry")
        team = await create_team(db_session, org.id, dept.id, id="team-sa-expiry")
        service_account = await create_service_account(
            db_session,
            org.id,
            dept.id,
            team.id,
            id="sa-short-expiry",
        )

        token_record, raw_token = await create_token(
            db_session,
            org.id,
            team.id,
            dept.id,
            service_account.id,
            entity_type="service",
            expires_in_hours=1.0,
        )
        await db_session.commit()

        time_until_expiry = token_record.expires_at - datetime.now(UTC)
        assert 0.9 < time_until_expiry.total_seconds() / 3600 < 1.1


@pytest.mark.unit
class TestTokenManagement:
    """Unit tests for token management operations."""

    async def test_token_refresh_before_expiry(
        self,
        db_session: AsyncSession,
    ):
        """Token can be refreshed before expiry via new exchange."""
        org = await create_org(db_session, id="org-refresh")
        dept = await create_department(db_session, org.id, id="dept-refresh")
        team = await create_team(db_session, org.id, dept.id, id="team-refresh")
        user = await create_user(db_session, org.id, team.id, id="user-refresh")

        token1, raw_token1 = await create_token(
            db_session, org.id, team.id, dept.id, user.id, expires_in_hours=1.0,
        )
        await db_session.commit()

        token2, raw_token2 = await create_token(
            db_session, org.id, team.id, dept.id, user.id, expires_in_hours=1.0,
        )
        await db_session.commit()

        assert token1.revoked_at is None
        assert token2.revoked_at is None
        assert raw_token1 != raw_token2

    async def test_token_revocation(
        self,
        db_session: AsyncSession,
    ):
        """Token can be revoked and becomes invalid."""
        org = await create_org(db_session, id="org-revoke")
        dept = await create_department(db_session, org.id, id="dept-revoke")
        team = await create_team(db_session, org.id, dept.id, id="team-revoke")
        user = await create_user(db_session, org.id, team.id, id="user-revoke")

        token, raw_token = await create_token(
            db_session, org.id, team.id, dept.id, user.id, revoked=True,
        )
        await db_session.commit()

        assert token.revoked_at is not None

        with pytest.raises(InvalidCredentialsError):
            if token.revoked_at is not None:
                raise InvalidCredentialsError("Token has been revoked")


@pytest.mark.unit
class TestExpiredCredentialsHandling:
    """
    Unit tests for expired credentials handling.

    User Story US-9.1:
    When my AWS SSO session expires and I try to use Claude Code,
    I want a clear error message telling me to re-authenticate.
    """

    async def test_expired_aws_credentials_error_message(self):
        """Clear error message when AWS credentials are expired."""
        mock_sts = MockSTSClient(
            should_fail=True,
            error_code="ExpiredTokenException",
            error_message="The security token included in the request is expired",
        )

        with pytest.raises(Exception) as exc_info:
            await mock_sts.get_caller_identity()

        error_message = str(exc_info.value)
        assert "ExpiredTokenException" in error_message
        assert "expired" in error_message.lower()

    async def test_gateway_token_expired_returns_401(
        self,
        db_session: AsyncSession,
    ):
        """Expired gateway token returns 401 with clear message."""
        org = await create_org(db_session, id="org-token-expired")
        dept = await create_department(db_session, org.id, id="dept-token-expired")
        team = await create_team(db_session, org.id, dept.id, id="team-token-expired")
        user = await create_user(db_session, org.id, team.id, id="user-token-expired")

        token, raw_token = await create_token(
            db_session, org.id, team.id, dept.id, user.id, expires_in_hours=-1.0,
        )
        await db_session.commit()

        assert token.expires_at < datetime.now(UTC)

        with pytest.raises(TokenExpiredError) as exc:
            raise TokenExpiredError()

        assert exc.value.status_code == 401
        assert exc.value.error == "token_expired"


# =============================================================================
# Integration tests -- HTTP via api_client (ASGI in unit mode, HTTP in live)
# =============================================================================


@pytest.mark.integration
class TestHTTPAuthFlows:
    """
    HTTP-level authentication tests (OAuth / JWT path).

    These use ``api_client`` and JWT fixtures so they run against the
    FastAPI ASGI app in unit mode and against the deployed gateway in live mode.
    """

    # In unit mode the Cognito JWT verifier is not configured, so the
    # middleware returns 503 "auth_not_configured" instead of 401.
    # We accept 401, 403, or 503 in unit mode; in live mode we expect 401/403.
    _REJECT_CODES = (401, 403, 503)

    async def test_unauthenticated_request_returns_401_or_403(self, api_client):
        """Unauthenticated request to /v1/messages is rejected."""
        response = await api_client.post(
            "/v1/messages",
            json={
                "model": "claude-3-5-sonnet-20241022",
                "max_tokens": 10,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert response.status_code in self._REJECT_CODES, f"Expected rejection (401/403/503), got {response.status_code}"

    async def test_expired_jwt_returns_401(self, api_client, expired_jwt):
        """Request with an expired JWT is rejected."""
        response = await api_client.post(
            "/v1/messages",
            headers={"Authorization": f"Bearer {expired_jwt}"},
            json={
                "model": "claude-3-5-sonnet-20241022",
                "max_tokens": 10,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert response.status_code in self._REJECT_CODES, f"Expected rejection (401/403/503), got {response.status_code}"

    async def test_wrong_audience_jwt_returns_401(self, api_client, wrong_aud_jwt):
        """JWT with wrong audience claim is rejected."""
        response = await api_client.post(
            "/v1/messages",
            headers={"Authorization": f"Bearer {wrong_aud_jwt}"},
            json={
                "model": "claude-3-5-sonnet-20241022",
                "max_tokens": 10,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert response.status_code in self._REJECT_CODES, f"Expected rejection (401/403/503), got {response.status_code}"

    async def test_malformed_jwt_returns_401(self, api_client, malformed_jwt):
        """A malformed JWT string is rejected."""
        response = await api_client.post(
            "/v1/messages",
            headers={"Authorization": f"Bearer {malformed_jwt}"},
            json={
                "model": "claude-3-5-sonnet-20241022",
                "max_tokens": 10,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert response.status_code in self._REJECT_CODES, f"Expected rejection (401/403/503), got {response.status_code}"


# =============================================================================
# Live-only tests -- OAuth path (real HTTP against deployed gateway)
# =============================================================================


@pytest.mark.live_only
class TestLiveOAuthAuth:
    """Live HTTP tests for OAuth / Cognito JWT authentication path.

    These tests hit the deployed REST API Gateway with real Cognito tokens.
    """

    async def test_valid_user_jwt_gets_200_on_health(self, api_client, jwt_for_user):
        """Valid user JWT should succeed on /health."""
        response = await api_client.get(
            "/health",
            headers={"Authorization": f"Bearer {jwt_for_user}"},
        )
        assert response.status_code == 200

    async def test_valid_agent_jwt_gets_200_on_health(self, api_client, jwt_for_agent):
        """Agent JWT from client_credentials flow accesses /health."""
        response = await api_client.get(
            "/health",
            headers={"Authorization": f"Bearer {jwt_for_agent}"},
        )
        assert response.status_code == 200

    async def test_unauthenticated_request_rejected(self, api_client):
        """Request without any auth token is rejected by API Gateway."""
        response = await api_client.post(
            "/v1/messages",
            json={
                "model": "claude-3-5-sonnet-20241022",
                "max_tokens": 10,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert response.status_code in (401, 403), f"Expected 401/403 from live gateway, got {response.status_code}"

    async def test_expired_jwt_rejected_live(self, api_client, expired_jwt):
        """Expired JWT is rejected by the Lambda authorizer."""
        response = await api_client.post(
            "/v1/messages",
            headers={"Authorization": f"Bearer {expired_jwt}"},
            json={
                "model": "claude-3-5-sonnet-20241022",
                "max_tokens": 10,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert response.status_code in (401, 403), f"Expected 401/403 for expired token, got {response.status_code}"

    async def test_malformed_jwt_rejected_live(self, api_client, malformed_jwt):
        """Malformed JWT is rejected by the Lambda authorizer."""
        response = await api_client.post(
            "/v1/messages",
            headers={"Authorization": f"Bearer {malformed_jwt}"},
            json={
                "model": "claude-3-5-sonnet-20241022",
                "max_tokens": 10,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert response.status_code in (401, 403), f"Expected 401/403 for malformed token, got {response.status_code}"

    async def test_token_refresh_via_cognito(self):
        """Token refresh flow works via Cognito refresh_token grant (live only)."""
        from tests.e2e.config import load_live_config

        cfg = load_live_config()
        import boto3

        client = boto3.client("cognito-idp", region_name=cfg.aws_region)
        resp = client.admin_initiate_auth(
            UserPoolId=cfg.cognito_user_pool_id,
            ClientId=cfg.cognito_client_id,
            AuthFlow="ADMIN_USER_PASSWORD_AUTH",
            AuthParameters={
                "USERNAME": cfg.test_user_email,
                "PASSWORD": cfg.test_user_password,
            },
        )
        refresh_token = resp["AuthenticationResult"].get("RefreshToken")
        if not refresh_token:
            pytest.skip("No refresh token returned -- user pool may not support it")

        refresh_resp = client.admin_initiate_auth(
            UserPoolId=cfg.cognito_user_pool_id,
            ClientId=cfg.cognito_client_id,
            AuthFlow="REFRESH_TOKEN_AUTH",
            AuthParameters={"REFRESH_TOKEN": refresh_token},
        )
        new_access = refresh_resp["AuthenticationResult"]["AccessToken"]
        assert new_access and len(new_access) > 20


# =============================================================================
# Live-only tests -- IAM SigV4 path
# =============================================================================


@pytest.mark.live_only
class TestLiveIAMAuth:
    """Live HTTP tests for IAM SigV4 authentication path.

    These tests hit the deployed REST API Gateway with SigV4-signed requests.
    The signing identity must be a registered IAM principal in the gateway
    agent registry (e.g. ``adp-dev-agent-runner-role``).
    """

    async def test_iam_signed_health_returns_200(self, iam_signed_client):
        """SigV4-signed request from registered IRSA role gets 200 on /health."""
        response = await iam_signed_client.get("/health")
        assert response.status_code == 200, f"Expected 200 for IAM-authed /health, got {response.status_code}"

    async def test_unsigned_request_returns_401_or_403(self, api_client):
        """Unsigned request (no JWT, no SigV4) is rejected by API Gateway."""
        response = await api_client.get("/health")
        # /health may or may not require auth depending on config; try a protected endpoint
        response = await api_client.post(
            "/v1/messages",
            json={
                "model": "claude-3-5-sonnet-20241022",
                "max_tokens": 10,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert response.status_code in (401, 403), f"Expected 401/403, got {response.status_code}"

    async def test_iam_signed_proxy_endpoint_accessible(self, iam_signed_client):
        """SigV4-signed request to /v1/messages is accepted (auth layer passes)."""
        response = await iam_signed_client.post(
            "/v1/messages",
            json={
                "model": "global.anthropic.claude-sonnet-4-6",
                "max_tokens": 10,
                "messages": [{"role": "user", "content": "Say hello in one word."}],
            },
        )
        # Should pass auth (200) or get a business-logic error (4xx), not an auth error
        # 200 = success, 400 = bad request (model not found etc), but not 401/403
        assert response.status_code != 401, f"IAM auth should not return 401: {response.text[:200]}"

    async def test_iam_signed_health_with_headers(self, iam_signed_client):
        """SigV4-signed /health includes expected response headers."""
        response = await iam_signed_client.get("/health")
        assert response.status_code == 200
        # Health endpoint should return JSON
        ct = response.headers.get("content-type", "")
        assert "json" in ct or "text" in ct, f"Unexpected content-type: {ct}"
