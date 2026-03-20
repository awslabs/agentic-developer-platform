"""
Unit tests for the AuthService module.

These tests cover the core authentication service functionality including
credential exchange, token validation, and revocation with comprehensive
mocking to avoid external dependencies.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from src.auth.auth_service import AuthService
from src.auth.exceptions import AuthenticationError, TokenValidationError
from src.auth.schemas import TenantInfo
from src.shared.exceptions import InvalidCredentialsError, UnknownOrganizationError, UnregisteredServiceAccountError
from src.shared.schemas.auth import AuthExchangeRequest, AuthExchangeResponse, TokenContext


@pytest.mark.unit
class TestAuthService:
    """Test suite for AuthService."""

    def test_init_with_dependencies(self, mock_sts_client, tenant_resolver, token_manager):
        """Test AuthService initialization with provided dependencies."""
        auth_service = AuthService(sts_client=mock_sts_client, tenant_resolver=tenant_resolver, token_manager=token_manager)

        assert auth_service.sts_client == mock_sts_client
        assert auth_service.tenant_resolver == tenant_resolver
        assert auth_service.token_manager == token_manager

    def test_init_with_default_dependencies(self):
        """Test AuthService initialization with default dependencies."""
        auth_service = AuthService()

        assert auth_service.sts_client is not None
        assert auth_service.tenant_resolver is not None
        assert auth_service.token_manager is not None

    @pytest.mark.asyncio
    async def test_exchange_credentials_success(self, db_session, auth_service: AuthService, sample_tenant_info: TenantInfo):
        """Test successful credential exchange."""
        request = AuthExchangeRequest(
            aws_access_key_id="AKIAIOSFODNN7EXAMPLE",
            aws_secret_access_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYzEXAMPLEKEY",
            aws_session_token="session-token",
        )

        # Mock the tenant resolver to return sample tenant info
        with patch.object(auth_service.tenant_resolver, "resolve_tenant", return_value=sample_tenant_info):
            # Mock token manager to return a token
            with patch.object(auth_service.token_manager, "generate_token") as mock_generate:
                with patch.object(auth_service.token_manager, "store_token") as mock_store:
                    expires_at = datetime.now(UTC) + timedelta(hours=12)
                    mock_generate.return_value = ("mock-jwt-token", expires_at)
                    mock_store.return_value = "token-id-123"

                    response = await auth_service.exchange_credentials(request, db_session)

                    assert isinstance(response, AuthExchangeResponse)
                    assert response.token == "mock-jwt-token"
                    assert response.expires_at == expires_at
                    assert response.user_id == sample_tenant_info.entity_id
                    assert response.org_id == sample_tenant_info.org_id
                    assert response.account_type == sample_tenant_info.account_type

    @pytest.mark.asyncio
    async def test_exchange_credentials_invalid_aws_credentials(self, db_session, auth_service: AuthService):
        """Test credential exchange with invalid AWS credentials."""
        request = AuthExchangeRequest(aws_access_key_id="invalid", aws_secret_access_key="invalid", aws_session_token="invalid")

        # Mock STS client to raise an exception
        with patch.object(auth_service.sts_client, "get_caller_identity", side_effect=Exception("Invalid credentials")):
            with pytest.raises(InvalidCredentialsError) as exc_info:
                await auth_service.exchange_credentials(request, db_session)

            assert "Invalid AWS credentials provided" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_exchange_credentials_unknown_organization(self, db_session, unknown_org_auth_service: AuthService):
        """Test credential exchange with unknown organization."""
        request = AuthExchangeRequest(
            aws_access_key_id="AKIAIOSFODNN7EXAMPLE",
            aws_secret_access_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYzEXAMPLEKEY",
            aws_session_token="session-token",
        )

        with pytest.raises(UnknownOrganizationError) as exc_info:
            await unknown_org_auth_service.exchange_credentials(request, db_session)

        assert "999888777666" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_exchange_credentials_unregistered_service_account(
        self, db_session, unregistered_sa_auth_service: AuthService, sample_organization
    ):
        """Test credential exchange with unregistered service account."""
        request = AuthExchangeRequest(
            aws_access_key_id="AKIAIOSFODNN7EXAMPLE",
            aws_secret_access_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYzEXAMPLEKEY",
            aws_session_token="session-token",
        )

        with pytest.raises(UnregisteredServiceAccountError) as exc_info:
            await unregistered_sa_auth_service.exchange_credentials(request, db_session)

        assert "unregistered-role" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_exchange_credentials_token_generation_failure(self, db_session, auth_service: AuthService, sample_tenant_info: TenantInfo):
        """Test credential exchange when token generation fails."""
        request = AuthExchangeRequest(
            aws_access_key_id="AKIAIOSFODNN7EXAMPLE",
            aws_secret_access_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYzEXAMPLEKEY",
            aws_session_token="session-token",
        )

        with patch.object(auth_service.tenant_resolver, "resolve_tenant", return_value=sample_tenant_info):
            with patch.object(auth_service.token_manager, "generate_token", side_effect=Exception("Token generation failed")):
                with pytest.raises(AuthenticationError) as exc_info:
                    await auth_service.exchange_credentials(request, db_session)

                assert "Failed to generate authentication token" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_exchange_credentials_token_storage_failure(self, db_session, auth_service: AuthService, sample_tenant_info: TenantInfo):
        """Test credential exchange when token storage fails (should continue)."""
        request = AuthExchangeRequest(
            aws_access_key_id="AKIAIOSFODNN7EXAMPLE",
            aws_secret_access_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYzEXAMPLEKEY",
            aws_session_token="session-token",
        )

        with patch.object(auth_service.tenant_resolver, "resolve_tenant", return_value=sample_tenant_info):
            expires_at = datetime.now(UTC) + timedelta(hours=12)
            with patch.object(auth_service.token_manager, "generate_token", return_value=("mock-token", expires_at)):
                with patch.object(auth_service.token_manager, "store_token", side_effect=Exception("Storage failed")):
                    # Should succeed despite storage failure
                    response = await auth_service.exchange_credentials(request, db_session)

                    assert isinstance(response, AuthExchangeResponse)
                    assert response.token == "mock-token"

    @pytest.mark.asyncio
    async def test_validate_token_success(self, db_session, auth_service: AuthService):
        """Test successful token validation."""
        token = "mock-jwt-token"
        expected_context = TokenContext(
            user_id="user-123",
            org_id="org-456",
            team_id="team-789",
            department_id="dept-abc",
            account_type="human",
            is_admin=False,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )

        with patch.object(auth_service.token_manager, "validate_token", return_value=expected_context):
            context = await auth_service.validate_token(token, db_session)

            assert context == expected_context

    @pytest.mark.asyncio
    async def test_validate_token_failure(self, db_session, auth_service: AuthService):
        """Test token validation failure."""
        token = "invalid-token"

        with patch.object(auth_service.token_manager, "validate_token", side_effect=TokenValidationError("Invalid token")):
            with pytest.raises(TokenValidationError) as exc_info:
                await auth_service.validate_token(token, db_session)

            assert "Invalid token" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_validate_token_unexpected_error(self, db_session, auth_service: AuthService):
        """Test token validation with unexpected error."""
        token = "token"

        with patch.object(auth_service.token_manager, "validate_token", side_effect=Exception("Unexpected error")):
            with pytest.raises(TokenValidationError) as exc_info:
                await auth_service.validate_token(token, db_session)

            assert "Token validation failed" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_revoke_token_success(self, db_session, auth_service: AuthService):
        """Test successful token revocation."""
        token = "token-to-revoke"

        with patch.object(auth_service.token_manager, "revoke_token", return_value=True) as mock_revoke:
            await auth_service.revoke_token(token, db_session)

            mock_revoke.assert_called_once_with(token, db_session)

    @pytest.mark.asyncio
    async def test_revoke_token_not_found(self, db_session, auth_service: AuthService):
        """Test revoking token that doesn't exist."""
        token = "non-existent-token"

        with patch.object(auth_service.token_manager, "revoke_token", return_value=False):
            # Should not raise an exception
            await auth_service.revoke_token(token, db_session)

    @pytest.mark.asyncio
    async def test_revoke_token_failure(self, db_session, auth_service: AuthService):
        """Test token revocation failure."""
        token = "token"

        with patch.object(auth_service.token_manager, "revoke_token", side_effect=Exception("Revocation failed")):
            with pytest.raises(TokenValidationError) as exc_info:
                await auth_service.revoke_token(token, db_session)

            assert "Token revocation failed" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_revoke_all_user_tokens_success(self, db_session, auth_service: AuthService):
        """Test successful revocation of all user tokens."""
        entity_id = "user-123"
        org_id = "org-456"

        with patch.object(auth_service.token_manager, "revoke_all_user_tokens", return_value=3) as mock_revoke:
            count = await auth_service.revoke_all_user_tokens(entity_id, org_id, db_session)

            assert count == 3
            mock_revoke.assert_called_once_with(entity_id, org_id, db_session)

    @pytest.mark.asyncio
    async def test_revoke_all_user_tokens_failure(self, db_session, auth_service: AuthService):
        """Test revoke all user tokens failure."""
        entity_id = "user-123"
        org_id = "org-456"

        with patch.object(auth_service.token_manager, "revoke_all_user_tokens", side_effect=Exception("Revocation failed")):
            with pytest.raises(Exception):  # Should raise AuthorizationError
                await auth_service.revoke_all_user_tokens(entity_id, org_id, db_session)

    @pytest.mark.asyncio
    async def test_cleanup_expired_tokens_success(self, db_session, auth_service: AuthService):
        """Test successful cleanup of expired tokens."""
        with patch.object(auth_service.token_manager, "cleanup_expired_tokens", return_value=5) as mock_cleanup:
            count = await auth_service.cleanup_expired_tokens(db_session)

            assert count == 5
            mock_cleanup.assert_called_once_with(db_session)

    @pytest.mark.asyncio
    async def test_cleanup_expired_tokens_failure(self, db_session, auth_service: AuthService):
        """Test cleanup expired tokens failure (should not raise exception)."""
        with patch.object(auth_service.token_manager, "cleanup_expired_tokens", side_effect=Exception("Cleanup failed")):
            count = await auth_service.cleanup_expired_tokens(db_session)

            # Should return 0 on failure, not raise exception
            assert count == 0

    def test_get_token_info_without_validation(self, auth_service: AuthService):
        """Test extracting token info without validation."""
        token = "mock-token"
        expected_claims = {"sub": "user-123", "org_id": "org-456"}

        with patch.object(auth_service.token_manager, "extract_claims_without_verification", return_value=expected_claims):
            claims = auth_service.get_token_info_without_validation(token)

            assert claims == expected_claims
