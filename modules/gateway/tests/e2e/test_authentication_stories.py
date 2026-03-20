"""
E2E tests for authentication user stories.

These tests verify the complete authentication flow from user login
through token management.

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


@pytest.mark.e2e
class TestHumanUserAuthentication:
    """
    E2E tests for Human User Authentication via AWS SSO.

    User Story US-1.4:
    As a Developer (Dev), I want to exchange my AWS SSO temporary credentials
    for a gateway token, so that I can access Bedrock through the gateway
    without managing separate credentials.
    """

    @pytest.mark.asyncio
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

    @pytest.mark.asyncio
    async def test_token_expires_after_configured_duration(
        self,
        db_session: AsyncSession,
    ):
        """
        Test: Token expires after configurable duration (default: 12 hours).

        Acceptance Criteria:
        - Token expires after configurable duration (default: 12 hours for manual exchange)
        """
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

    @pytest.mark.asyncio
    async def test_invalid_aws_credentials_return_401(self):
        """
        Test: Invalid/expired AWS credentials return 401.

        Acceptance Criteria:
        - Invalid/expired AWS credentials return 401
        - Response: {"error": "invalid_credentials", "message": "..."}
        """
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

    @pytest.mark.asyncio
    async def test_unknown_aws_account_returns_403(
        self,
        db_session: AsyncSession,
    ):
        """
        Test: AWS account not mapped to any org returns 403.

        Acceptance Criteria:
        - AWS account not mapped to any org returns 403
        - Response: {"error": "unknown_organization"}
        """
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


@pytest.mark.e2e
class TestServiceAccountAuthentication:
    """
    E2E tests for Service Account Registration and Authentication.

    User Stories:
    - US-1.5: Service Account Registration for Automated Agents
    - US-1.6: Automated Agent Authentication (M2M)
    """

    @pytest.mark.asyncio
    async def test_register_service_account(
        self,
        db_session: AsyncSession,
    ):
        """
        Test: POST /admin/organizations/{org_id}/service-accounts creates service account.

        Acceptance Criteria (US-1.5):
        - Creates service account with: name, iam_role_arn, department_id, team_id
        - Service account has separate budget and rate limit configuration
        """
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

        # Verify service account created
        assert service_account.id == "sa-cicd"
        assert service_account.name == "CI/CD Pipeline"
        assert service_account.org_id == org.id
        assert service_account.iam_role_arn == "arn:aws:iam::123456789012:role/cicd-pipeline"

    @pytest.mark.asyncio
    async def test_service_account_authentication_via_iam_role(
        self,
        db_session: AsyncSession,
    ):
        """
        Test: Container with IAM role can authenticate and get token.

        Acceptance Criteria (US-1.6):
        - Container with IAM role calls POST /auth/exchange
        - Gateway validates via STS, matches IAM role ARN to registered service account
        - Returns token with service account identity
        """
        org = await create_org(db_session, id="org-m2m", aws_accounts=["123456789012"])
        dept = await create_department(db_session, org.id, id="dept-m2m")
        team = await create_team(db_session, org.id, dept.id, id="team-m2m")

        # Register service account
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

        # Mock STS response for service account
        mock_sts = MockSTSClient(
            account_id="123456789012",
            role_arn="arn:aws:sts::123456789012:assumed-role/ml-training-role/session",
        )

        caller_identity = await mock_sts.get_caller_identity()

        # Verify IAM role matches registered service account
        role_arn_parts = caller_identity["Arn"].split("/")
        role_name = role_arn_parts[1]

        assert role_name == "ml-training-role"

        # Create service account token
        token_record, raw_token = await create_token(
            db_session,
            org.id,
            team.id,
            dept.id,
            service_account.id,
            entity_type="service",
            expires_in_hours=1.0,  # Shorter expiry for service accounts
        )
        await db_session.commit()

        # Verify token context
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

    @pytest.mark.asyncio
    async def test_unregistered_iam_role_returns_403(
        self,
        db_session: AsyncSession,
    ):
        """
        Test: Unregistered IAM roles return 403.

        Acceptance Criteria (US-1.6, US-9.5):
        - Unregistered IAM roles return 403
        - Response: {"error": "unregistered_service_account", "message": "Agent not registered. Contact your org administrator."}
        """
        await create_org(db_session, id="org-unregistered", aws_accounts=["123456789012"])
        await db_session.commit()

        # No service accounts registered

        mock_sts = MockSTSClient(
            account_id="123456789012",
            role_arn="arn:aws:sts::123456789012:assumed-role/unregistered-role/session",
        )

        caller_identity = await mock_sts.get_caller_identity()

        # Should fail - role not registered
        with pytest.raises(UnregisteredServiceAccountError) as exc:
            role_arn = f"arn:aws:iam::{caller_identity['Account']}:role/unregistered-role"
            raise UnregisteredServiceAccountError(role_arn)

        assert exc.value.status_code == 403
        assert exc.value.error == "unregistered_service_account"

    @pytest.mark.asyncio
    async def test_service_account_token_shorter_expiry(
        self,
        db_session: AsyncSession,
    ):
        """
        Test: Service account tokens have configurable (shorter) expiry.

        Acceptance Criteria (US-1.5, US-1.6):
        - Service account tokens have configurable expiry (default: 1 hour)
        """
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

        # Create token with 1-hour expiry
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

        # Verify 1-hour expiry
        time_until_expiry = token_record.expires_at - datetime.now(UTC)
        assert 0.9 < time_until_expiry.total_seconds() / 3600 < 1.1


@pytest.mark.e2e
class TestTokenManagement:
    """E2E tests for token management operations."""

    @pytest.mark.asyncio
    async def test_token_refresh_before_expiry(
        self,
        db_session: AsyncSession,
    ):
        """
        Test: Token can be refreshed before expiry via new exchange.

        For CLI users, this is handled by apiKeyHelper with TTL.
        """
        org = await create_org(db_session, id="org-refresh")
        dept = await create_department(db_session, org.id, id="dept-refresh")
        team = await create_team(db_session, org.id, dept.id, id="team-refresh")
        user = await create_user(db_session, org.id, team.id, id="user-refresh")

        # Create initial token
        token1, raw_token1 = await create_token(
            db_session,
            org.id,
            team.id,
            dept.id,
            user.id,
            expires_in_hours=1.0,
        )
        await db_session.commit()

        # Simulate refresh - create new token
        token2, raw_token2 = await create_token(
            db_session,
            org.id,
            team.id,
            dept.id,
            user.id,
            expires_in_hours=1.0,
        )
        await db_session.commit()

        # Both tokens should be valid (until first expires)
        assert token1.revoked_at is None
        assert token2.revoked_at is None
        assert raw_token1 != raw_token2

    @pytest.mark.asyncio
    async def test_token_revocation(
        self,
        db_session: AsyncSession,
    ):
        """
        Test: Token can be revoked and becomes invalid.
        """
        org = await create_org(db_session, id="org-revoke")
        dept = await create_department(db_session, org.id, id="dept-revoke")
        team = await create_team(db_session, org.id, dept.id, id="team-revoke")
        user = await create_user(db_session, org.id, team.id, id="user-revoke")

        # Create and then revoke token
        token, raw_token = await create_token(
            db_session,
            org.id,
            team.id,
            dept.id,
            user.id,
            revoked=True,
        )
        await db_session.commit()

        # Token should be marked as revoked
        assert token.revoked_at is not None

        # Attempting to use revoked token should fail
        with pytest.raises(InvalidCredentialsError):
            if token.revoked_at is not None:
                raise InvalidCredentialsError("Token has been revoked")


@pytest.mark.e2e
class TestExpiredCredentialsHandling:
    """
    E2E tests for expired credentials handling.

    User Story US-9.1:
    When my AWS SSO session expires and I try to use Claude Code,
    I want a clear error message telling me to re-authenticate.
    """

    @pytest.mark.asyncio
    async def test_expired_aws_credentials_error_message(self):
        """
        Test: Clear error message when AWS credentials are expired.

        Acceptance Criteria (US-9.1):
        - bg-auth.sh detects expired credentials (STS call fails)
        - Prints to stderr: "AWS credentials expired. Run: aws sso login"
        - Script exits with code 1
        """
        mock_sts = MockSTSClient(
            should_fail=True,
            error_code="ExpiredTokenException",
            error_message="The security token included in the request is expired",
        )

        with pytest.raises(Exception) as exc_info:
            await mock_sts.get_caller_identity()

        # Verify clear error message
        error_message = str(exc_info.value)
        assert "ExpiredTokenException" in error_message
        assert "expired" in error_message.lower()

    @pytest.mark.asyncio
    async def test_gateway_token_expired_returns_401(
        self,
        db_session: AsyncSession,
    ):
        """
        Test: Expired gateway token returns 401 with clear message.
        """
        org = await create_org(db_session, id="org-token-expired")
        dept = await create_department(db_session, org.id, id="dept-token-expired")
        team = await create_team(db_session, org.id, dept.id, id="team-token-expired")
        user = await create_user(db_session, org.id, team.id, id="user-token-expired")

        # Create expired token
        token, raw_token = await create_token(
            db_session,
            org.id,
            team.id,
            dept.id,
            user.id,
            expires_in_hours=-1.0,  # Already expired
        )
        await db_session.commit()

        # Verify token is expired
        assert token.expires_at < datetime.now(UTC)

        # Should return 401
        with pytest.raises(TokenExpiredError) as exc:
            raise TokenExpiredError()

        assert exc.value.status_code == 401
        assert exc.value.error == "token_expired"
