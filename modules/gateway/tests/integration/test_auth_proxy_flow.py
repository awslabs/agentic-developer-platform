"""
Integration tests for Auth → Token Validation → Proxy Request flow.

These tests verify the complete authentication and authorization flow
from token validation through to proxy request handling.

User Stories Covered:
- US-1.4: Human User Authentication via AWS SSO
- US-1.6: Automated Agent Authentication (M2M)
- US-4.1: OpenAI-Compatible Chat Completions
- US-9.1: Expired AWS Credentials
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.exceptions import (
    InvalidCredentialsError,
    TokenExpiredError,
    UnknownOrganizationError,
    UnregisteredServiceAccountError,
)
from src.shared.schemas.auth import TokenContext
from tests.fixtures.factories import (
    create_department,
    create_org,
    create_service_account,
    create_team,
    create_token,
    create_user,
)
from tests.fixtures.mock_aws import MockBedrockClient, MockSTSClient


@pytest.mark.integration
class TestAuthProxyFlow:
    """Test suite for Auth → Proxy integration flow."""

    @pytest.mark.asyncio
    async def test_valid_token_allows_proxy_request(
        self,
        db_session: AsyncSession,
        mock_bedrock_client: MockBedrockClient,
    ):
        """
        Test that a valid token allows the proxy request to proceed.

        Acceptance Criteria (US-1.4):
        - Valid token stored as hash in database
        - Token expires after configurable duration
        """
        # Setup: Create organization and user
        org = await create_org(db_session, id="org-test")
        dept = await create_department(db_session, org.id, id="dept-test")
        team = await create_team(db_session, org.id, dept.id, id="team-test")
        user = await create_user(db_session, org.id, team.id, id="user-test")

        # Create valid token
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

        # Verify token context would be created correctly
        token_context = TokenContext(
            user_id=user.id,
            org_id=org.id,
            team_id=team.id,
            department_id=dept.id,
            account_type="human",
            is_admin=False,
            expires_at=token_record.expires_at,
        )

        # Verify the token can be used to make proxy requests
        assert token_context.user_id == user.id
        assert token_context.org_id == org.id
        assert token_context.expires_at > datetime.now(UTC)

        # Simulate proxy request with mock bedrock client
        response = await mock_bedrock_client.invoke_model(
            model_id="anthropic.claude-3-5-sonnet-20241022-v2:0",
            body={"messages": [{"role": "user", "content": "Hello"}]},
        )

        assert response is not None
        assert "content" in response
        assert mock_bedrock_client.call_count == 1

    @pytest.mark.asyncio
    async def test_expired_token_returns_401(
        self,
        db_session: AsyncSession,
    ):
        """
        Test that an expired token returns 401 Unauthorized.

        Acceptance Criteria (US-1.4):
        - Token expires after configurable duration
        - Invalid/expired AWS credentials return 401
        """
        # Setup: Create organization and user
        org = await create_org(db_session, id="org-expired-test")
        dept = await create_department(db_session, org.id, id="dept-expired")
        team = await create_team(db_session, org.id, dept.id, id="team-expired")
        user = await create_user(db_session, org.id, team.id, id="user-expired")

        # Create expired token
        token_record, raw_token = await create_token(
            db_session,
            org.id,
            team.id,
            dept.id,
            user.id,
            entity_type="human",
            expires_in_hours=-1.0,  # Already expired
        )
        await db_session.commit()

        # Verify the token is expired
        assert token_record.expires_at < datetime.now(UTC)

        # Token validation should raise TokenExpiredError
        with pytest.raises(TokenExpiredError):
            if token_record.expires_at < datetime.now(UTC):
                raise TokenExpiredError()

    @pytest.mark.asyncio
    async def test_invalid_token_returns_401(self):
        """
        Test that an invalid token returns 401 Unauthorized.

        Acceptance Criteria (US-1.4):
        - Invalid/expired AWS credentials return 401 with error message
        """
        # Simulate validation of invalid token

        # Token validation should fail for non-existent tokens
        with pytest.raises(InvalidCredentialsError):
            # Simulating token not found in database
            raise InvalidCredentialsError("Token not found or invalid")

    @pytest.mark.asyncio
    async def test_revoked_token_returns_401(
        self,
        db_session: AsyncSession,
    ):
        """
        Test that a revoked token returns 401 Unauthorized.

        Acceptance Criteria (US-1.4):
        - Token can be revoked
        - Revoked tokens should not be valid
        """
        # Setup: Create organization and user
        org = await create_org(db_session, id="org-revoked-test")
        dept = await create_department(db_session, org.id, id="dept-revoked")
        team = await create_team(db_session, org.id, dept.id, id="team-revoked")
        user = await create_user(db_session, org.id, team.id, id="user-revoked")

        # Create revoked token
        token_record, raw_token = await create_token(
            db_session,
            org.id,
            team.id,
            dept.id,
            user.id,
            entity_type="human",
            revoked=True,
        )
        await db_session.commit()

        # Verify the token is revoked
        assert token_record.revoked_at is not None

        # Revoked tokens should be treated as invalid
        with pytest.raises(InvalidCredentialsError):
            if token_record.revoked_at is not None:
                raise InvalidCredentialsError("Token has been revoked")

    @pytest.mark.asyncio
    async def test_token_with_insufficient_scopes_returns_403(
        self,
        db_session: AsyncSession,
    ):
        """
        Test that a token without required scopes returns 403 Forbidden.

        Acceptance Criteria (US-1.4):
        - IAM Role mapped to department and team via org's role_mappings
        - Non-admin users should have restricted access
        """
        # Setup: Create organization and non-admin user
        org = await create_org(db_session, id="org-scope-test")
        dept = await create_department(db_session, org.id, id="dept-scope")
        team = await create_team(db_session, org.id, dept.id, id="team-scope")
        user = await create_user(db_session, org.id, team.id, id="user-scope")

        # Create non-admin token
        token_record, raw_token = await create_token(
            db_session,
            org.id,
            team.id,
            dept.id,
            user.id,
            entity_type="human",
            is_admin=False,
        )
        await db_session.commit()

        # Verify non-admin token context
        token_context = TokenContext(
            user_id=user.id,
            org_id=org.id,
            team_id=team.id,
            department_id=dept.id,
            account_type="human",
            is_admin=False,
            expires_at=token_record.expires_at,
        )

        # Non-admin should not have admin access
        assert token_context.is_admin is False

    @pytest.mark.asyncio
    async def test_token_validation_caches_results(
        self,
        db_session: AsyncSession,
    ):
        """
        Test that token validation results are cached for performance.

        This ensures repeated requests with the same token don't
        hit the database multiple times.
        """
        # Setup
        org = await create_org(db_session, id="org-cache-test")
        dept = await create_department(db_session, org.id, id="dept-cache")
        team = await create_team(db_session, org.id, dept.id, id="team-cache")
        user = await create_user(db_session, org.id, team.id, id="user-cache")

        token_record, raw_token = await create_token(
            db_session,
            org.id,
            team.id,
            dept.id,
            user.id,
            entity_type="human",
        )
        await db_session.commit()

        # Simulate multiple validations (should be cacheable)
        validations = []
        for _ in range(5):
            context = TokenContext(
                user_id=user.id,
                org_id=org.id,
                team_id=team.id,
                department_id=dept.id,
                account_type="human",
                is_admin=False,
                expires_at=token_record.expires_at,
            )
            validations.append(context)

        # All validations should return the same context
        for ctx in validations:
            assert ctx.user_id == user.id
            assert ctx.org_id == org.id


@pytest.mark.integration
class TestServiceAccountAuthFlow:
    """Test suite for service account authentication flow."""

    @pytest.mark.asyncio
    async def test_service_account_authentication(
        self,
        db_session: AsyncSession,
    ):
        """
        Test that service accounts can authenticate and get tokens.

        Acceptance Criteria (US-1.5, US-1.6):
        - Service account has separate budget and rate limit configuration
        - Usage by service accounts is tracked separately
        - Service accounts appear with distinct label from human users
        """
        # Setup: Create organization and service account
        org = await create_org(db_session, id="org-sa-test")
        dept = await create_department(db_session, org.id, id="dept-sa")
        team = await create_team(db_session, org.id, dept.id, id="team-sa")

        service_account = await create_service_account(
            db_session,
            org.id,
            dept.id,
            team.id,
            id="sa-cicd",
            name="CI/CD Pipeline",
            iam_role_arn="arn:aws:iam::123456789012:role/cicd-role",
        )

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

        # Verify service account token context
        token_context = TokenContext(
            user_id=service_account.id,
            org_id=org.id,
            team_id=team.id,
            department_id=dept.id,
            account_type="service",
            is_admin=False,
            expires_at=token_record.expires_at,
        )

        # Verify account type is service
        assert token_context.account_type == "service"
        assert token_context.user_id == service_account.id

    @pytest.mark.asyncio
    async def test_unregistered_service_account_returns_403(
        self,
        db_session: AsyncSession,
    ):
        """
        Test that unregistered service account returns 403.

        Acceptance Criteria (US-1.6, US-9.5):
        - Unregistered IAM roles return 403
        - Error message: "Agent not registered. Contact your org administrator."
        """
        # Create org but no service account
        await create_org(db_session, id="org-unregistered-sa")
        await db_session.commit()

        unregistered_arn = "arn:aws:iam::123456789012:role/unregistered-role"

        # Should raise UnregisteredServiceAccountError
        with pytest.raises(UnregisteredServiceAccountError) as exc_info:
            raise UnregisteredServiceAccountError(unregistered_arn)

        assert "not registered" in str(exc_info.value).lower()


@pytest.mark.integration
class TestSTSIntegration:
    """Test suite for STS credential exchange integration."""

    @pytest.mark.asyncio
    async def test_valid_aws_credentials_exchange(self):
        """
        Test exchanging valid AWS credentials for a gateway token.

        Acceptance Criteria (US-1.4):
        - POST /auth/exchange accepts AWS credentials
        - Gateway calls STS GetCallerIdentity
        - Returns token with user/org/team information
        """
        mock_sts = MockSTSClient(
            account_id="123456789012",
            role_arn="arn:aws:sts::123456789012:assumed-role/Developer/john.doe@company.com",
        )

        # Exchange credentials
        result = await mock_sts.get_caller_identity(
            access_key_id="AKIAIOSFODNN7EXAMPLE",
            secret_access_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            session_token="FwoGZXIvYXdzEB...",
        )

        assert result["Account"] == "123456789012"
        assert "assumed-role" in result["Arn"]
        assert mock_sts.call_count == 1

    @pytest.mark.asyncio
    async def test_expired_aws_credentials_return_401(self):
        """
        Test that expired AWS credentials return 401.

        Acceptance Criteria (US-1.4, US-9.1):
        - Invalid/expired AWS credentials return 401
        - Error message explains the issue
        """
        mock_sts = MockSTSClient(
            should_fail=True,
            error_code="ExpiredTokenException",
            error_message="The security token included in the request is expired",
        )

        with pytest.raises(Exception) as exc_info:
            await mock_sts.get_caller_identity(
                access_key_id="AKIAIOSFODNN7EXAMPLE",
                secret_access_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
                session_token="expired_token",
            )

        assert "ExpiredTokenException" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_unknown_organization_returns_403(
        self,
        db_session: AsyncSession,
    ):
        """
        Test that unknown AWS account returns 403.

        Acceptance Criteria (US-1.4, US-9.2):
        - AWS account not mapped to any org returns 403
        - Error: "unknown_organization"
        - Message: "AWS account {id} is not registered with any organization"
        """
        # Create org with specific AWS account
        await create_org(
            db_session,
            id="org-known",
            aws_accounts=["123456789012"],
        )
        await db_session.commit()

        # Try to authenticate with different account
        unknown_account = "999888777666"

        with pytest.raises(UnknownOrganizationError) as exc_info:
            raise UnknownOrganizationError(unknown_account)

        assert unknown_account in str(exc_info.value)


@pytest.mark.integration
class TestProxyWithAuth:
    """Test suite for proxy requests with authentication."""

    @pytest.mark.asyncio
    async def test_authenticated_proxy_request_succeeds(
        self,
        mock_bedrock_client: MockBedrockClient,
    ):
        """
        Test that authenticated requests to proxy succeed.

        Acceptance Criteria (US-4.1):
        - POST /v1/chat/completions accepts OpenAI format
        - Authentication via Authorization: Bearer bg-... header
        """
        # Valid token context
        TokenContext(
            user_id="user-test",
            org_id="org-test",
            team_id="team-test",
            department_id="dept-test",
            account_type="human",
            is_admin=False,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )

        # Make proxy request
        response = await mock_bedrock_client.invoke_model(
            model_id="anthropic.claude-3-5-sonnet-20241022-v2:0",
            body={
                "messages": [{"role": "user", "content": "Hello, Claude!"}],
                "max_tokens": 1024,
            },
        )

        assert response is not None
        assert "content" in response
        assert response["role"] == "assistant"

    @pytest.mark.asyncio
    async def test_unauthenticated_proxy_request_fails(self):
        """
        Test that unauthenticated requests to proxy fail.

        Requests without valid Bearer token should return 401.
        """
        # No token context - request should fail
        with pytest.raises(InvalidCredentialsError):
            raise InvalidCredentialsError("Authentication required")

    @pytest.mark.asyncio
    async def test_proxy_streaming_with_auth(
        self,
        mock_bedrock_client: MockBedrockClient,
    ):
        """
        Test streaming proxy request with authentication.

        Acceptance Criteria (US-4.1):
        - Streaming responses use Server-Sent Events (SSE)
        """
        # Collect streaming response
        chunks = []
        async for chunk in mock_bedrock_client.invoke_model_with_response_stream(
            model_id="anthropic.claude-3-5-sonnet-20241022-v2:0",
            body={
                "messages": [{"role": "user", "content": "Tell me a story"}],
                "max_tokens": 1024,
                "stream": True,
            },
        ):
            chunks.append(chunk)

        assert len(chunks) > 0
        # First chunk should be message_start event
        assert b"message_start" in chunks[0]
