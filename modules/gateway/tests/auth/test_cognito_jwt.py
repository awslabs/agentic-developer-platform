"""
Tests for Cognito JWT validation.

Tests the CognitoJWTValidator class that validates JWT tokens issued by AWS Cognito.
"""

import time
from unittest.mock import MagicMock, patch

import jwt
import pytest

from src.auth.cognito_jwt import (
    CognitoJWTValidator,
    CognitoTokenClaims,
    get_cognito_validator,
    validate_cognito_token,
)


@pytest.fixture
def mock_settings():
    """Mock settings with Cognito configuration."""
    with patch("src.auth.cognito_jwt.get_settings") as mock:
        settings = MagicMock()
        settings.cognito_user_pool_id = "us-east-1_testpool"
        settings.cognito_client_id = "test-client-id"
        settings.aws_region = "us-east-1"
        mock.return_value = settings
        yield settings


@pytest.fixture
def validator(mock_settings):
    """Create a CognitoJWTValidator instance."""
    return CognitoJWTValidator(
        user_pool_id="us-east-1_testpool",
        client_id="test-client-id",
        region="us-east-1",
    )


class TestCognitoJWTValidator:
    """Tests for CognitoJWTValidator."""

    def test_init_builds_correct_urls(self, validator):
        """Test that initialization builds correct Cognito URLs."""
        assert validator.issuer == "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_testpool"
        assert validator.jwks_url == "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_testpool/.well-known/jwks.json"

    def test_init_raises_without_user_pool_id(self, mock_settings):
        """Test that initialization fails without user pool ID."""
        mock_settings.cognito_user_pool_id = ""
        with pytest.raises(ValueError, match="User Pool ID must be configured"):
            CognitoJWTValidator(user_pool_id="", client_id="test", region="us-east-1")

    def test_init_accepts_empty_client_id_for_m2m_auth(self, mock_settings):
        """Test that initialization accepts empty client ID (Issue #119: M2M auth).

        Issue #119 changed the validator to accept any client from the user pool
        when no specific client_id is configured. This enables M2M authentication
        where agents get their own Cognito App Clients.
        """
        mock_settings.cognito_client_id = ""
        # This should NOT raise - empty client_id means accept any client from the user pool
        validator = CognitoJWTValidator(user_pool_id="us-east-1_test", client_id="", region="us-east-1")
        # With empty client_id, allowed_client_ids set should be empty (accept any)
        assert len(validator.allowed_client_ids) == 0


class TestCognitoTokenClaims:
    """Tests for CognitoTokenClaims model."""

    def test_creates_claims_from_access_token_payload(self):
        """Test parsing access token claims."""
        payload = {
            "sub": "user-123",
            "iss": "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_testpool",
            "client_id": "test-client-id",
            "token_use": "access",
            "scope": "openid email profile",
            "auth_time": 1234567890,
            "exp": 1234571490,
            "iat": 1234567890,
            "jti": "token-id-123",
            "username": "testuser",
            "cognito:groups": ["admins", "users"],
        }

        claims = CognitoTokenClaims(**payload, cognito_groups=payload.get("cognito:groups", []))

        assert claims.sub == "user-123"
        assert claims.token_use == "access"
        assert claims.username == "testuser"
        assert claims.cognito_groups == ["admins", "users"]

    def test_creates_claims_with_custom_attributes(self):
        """Test parsing ID token claims with custom attributes."""
        payload = {
            "sub": "user-123",
            "iss": "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_testpool",
            "client_id": "test-client-id",
            "token_use": "id",
            "auth_time": 1234567890,
            "exp": 1234571490,
            "iat": 1234567890,
            "jti": "token-id-123",
            "username": "testuser",
            "email": "test@example.com",
            "name": "Test User",
            "custom:org_id": "org-456",
            "custom:department_id": "dept-789",
            "custom:team_id": "team-001",
            "custom:role": "platform_admin",
        }

        claims = CognitoTokenClaims(
            sub=payload["sub"],
            iss=payload["iss"],
            client_id=payload["client_id"],
            token_use=payload["token_use"],
            auth_time=payload["auth_time"],
            exp=payload["exp"],
            iat=payload["iat"],
            jti=payload["jti"],
            username=payload["username"],
            email=payload.get("email"),
            name=payload.get("name"),
            org_id=payload.get("custom:org_id"),
            department_id=payload.get("custom:department_id"),
            team_id=payload.get("custom:team_id"),
            role=payload.get("custom:role"),
        )

        assert claims.email == "test@example.com"
        assert claims.name == "Test User"
        assert claims.org_id == "org-456"
        assert claims.department_id == "dept-789"
        assert claims.team_id == "team-001"
        assert claims.role == "platform_admin"


class TestDecodeWithoutVerification:
    """Tests for decode_without_verification method."""

    def test_decodes_valid_token(self, validator):
        """Test decoding a token without verification."""
        payload = {
            "sub": "user-123",
            "iss": "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_testpool",
            "exp": int(time.time()) + 3600,
        }
        token = jwt.encode(payload, "secret", algorithm="HS256")

        result = validator.decode_without_verification(token)

        assert result is not None
        assert result["sub"] == "user-123"

    def test_returns_none_for_invalid_token(self, validator):
        """Test that invalid token returns None."""
        result = validator.decode_without_verification("invalid-token")
        assert result is None


class TestGetCognitoValidator:
    """Tests for singleton validator getter."""

    def test_returns_singleton_instance(self, mock_settings):
        """Test that get_cognito_validator returns singleton."""
        # Reset the singleton
        import src.auth.cognito_jwt as cognito_module

        cognito_module._validator = None

        # Mock the validator creation to avoid JWKS client issues
        with patch.object(CognitoJWTValidator, "__init__", return_value=None):
            validator1 = get_cognito_validator()
            validator2 = get_cognito_validator()

            assert validator1 is validator2

        # Reset for other tests
        cognito_module._validator = None


class TestValidateCognitoToken:
    """Tests for the convenience validate_cognito_token function."""

    def test_validates_using_singleton(self, mock_settings):
        """Test that validate_cognito_token uses singleton validator."""
        import src.auth.cognito_jwt as cognito_module

        cognito_module._validator = None

        # This will fail because we can't actually validate tokens without JWKS
        # But we're testing that the function uses the singleton
        with patch.object(CognitoJWTValidator, "validate_token") as mock_validate:
            mock_validate.return_value = CognitoTokenClaims(
                sub="user-123",
                iss="https://test",
                client_id="test",
                token_use="access",
                auth_time=0,
                exp=0,
                iat=0,
                jti="test",
                username="test",
            )

            with patch.object(CognitoJWTValidator, "__init__", return_value=None):
                result = validate_cognito_token("test-token")

            assert result.sub == "user-123"

        # Reset for other tests
        cognito_module._validator = None


# =============================================================================
# Issue #119: Additional tests for unified Cognito JWT auth
# =============================================================================


class TestCognitoJWTValidatorMultipleClients:
    """Tests for supporting multiple client IDs (Issue #119)."""

    @pytest.fixture
    def mock_settings(self):
        """Mock settings with Cognito configuration."""
        with patch("src.auth.cognito_jwt.get_settings") as mock:
            settings = MagicMock()
            settings.cognito_user_pool_id = "us-east-1_testpool"
            settings.cognito_client_id = "main-client-id"
            settings.aws_region = "us-east-1"
            mock.return_value = settings
            yield settings

    def test_init_with_allowed_client_ids(self, mock_settings):
        """Test initialization with explicit allowed client IDs.

        Issue #127: Changed behavior - settings.cognito_client_id is no longer
        auto-added to allowed_client_ids. Only explicitly passed allowed_client_ids
        are used. This enables accepting any client from the user pool by default.
        """
        validator = CognitoJWTValidator(
            user_pool_id="us-east-1_testpool",
            client_id="main-client-id",  # This is stored but NOT added to allowed_client_ids
            allowed_client_ids=["agent-client-1", "agent-client-2"],
            region="us-east-1",
        )

        # Only explicitly passed allowed_client_ids are in the set
        # main-client-id is NOT automatically added anymore
        assert "agent-client-1" in validator.allowed_client_ids
        assert "agent-client-2" in validator.allowed_client_ids
        assert len(validator.allowed_client_ids) == 2

    def test_init_without_primary_client_id(self, mock_settings):
        """Test initialization without a primary client ID (accept all)."""
        mock_settings.cognito_client_id = ""

        validator = CognitoJWTValidator(
            user_pool_id="us-east-1_testpool",
            client_id="",  # No primary client
            region="us-east-1",
        )

        # When no client IDs are specified, the allowed_client_ids set should be empty
        # This means any client from the user pool is accepted
        assert len(validator.allowed_client_ids) == 0

    def test_default_accepts_any_client_from_user_pool(self, mock_settings):
        """Test that default initialization accepts any client from user pool.

        Issue #127: When creating a validator with default settings (no explicit
        allowed_client_ids), it should accept tokens from ANY App Client in the
        same User Pool. This is critical for M2M authentication where agent clients
        have different client_ids than the main web/CLI client.
        """
        # Even when cognito_client_id is set in settings, the validator should
        # not restrict to only that client by default
        mock_settings.cognito_client_id = "web-client-id"

        validator = CognitoJWTValidator(
            user_pool_id="us-east-1_testpool",
            # client_id defaults to settings.cognito_client_id ("web-client-id")
            region="us-east-1",
        )

        # allowed_client_ids should be empty, meaning accept any client
        assert len(validator.allowed_client_ids) == 0

        # The client_id from settings is stored but NOT used for restriction
        assert validator.client_id == "web-client-id"


class TestCognitoTokenClaimsServiceAccount:
    """Tests for service account (client_credentials) claims (Issue #119)."""

    def test_service_account_claims(self):
        """Test parsing claims from client_credentials token."""
        claims = CognitoTokenClaims(
            sub="agent-client-123",  # For client_credentials, sub is client_id
            iss="https://cognito-idp.us-east-1.amazonaws.com/us-east-1_testpool",
            client_id="agent-client-123",
            token_use="access",
            scope="bedrockgw/invoke",
            exp=1234571490,
            iat=1234567890,
            # username is empty for client_credentials
            username="",
            # Custom claims injected by Pre Token Generation Lambda
            org_id="org-456",
            team_id="team-001",
            account_type="service",
            agent_name="my-data-pipeline",
        )

        assert claims.sub == "agent-client-123"
        assert claims.account_type == "service"
        assert claims.agent_name == "my-data-pipeline"
        assert claims.scope == "bedrockgw/invoke"
        assert claims.username == ""  # Empty for client_credentials

    def test_human_user_claims_with_account_type(self):
        """Test parsing claims for human user with account_type."""
        claims = CognitoTokenClaims(
            sub="user-123",
            iss="https://cognito-idp.us-east-1.amazonaws.com/us-east-1_testpool",
            client_id="web-client-id",
            token_use="access",
            auth_time=1234567890,
            exp=1234571490,
            iat=1234567890,
            jti="token-id-123",
            username="john@example.com",
            email="john@example.com",
            org_id="org-456",
            team_id="team-001",
            account_type="human",
            role="admin",
        )

        assert claims.account_type == "human"
        assert claims.username == "john@example.com"
        assert claims.role == "admin"


class TestMiddlewareHelperFunctions:
    """Tests for middleware helper functions (Issue #119)."""

    def test_cognito_claims_to_context_human(self):
        """Test converting Cognito claims to TokenContext for human user."""
        from src.auth.middleware import _cognito_claims_to_context

        claims = CognitoTokenClaims(
            sub="user-123",
            iss="https://test",
            client_id="web-client",
            token_use="access",
            exp=1234571490,
            iat=1234567890,
            username="john@example.com",
            org_id="org-456",
            team_id="team-001",
            department_id="dept-789",
            account_type="human",
            role="admin",
            cognito_groups=["users"],
        )

        context = _cognito_claims_to_context(claims)

        assert context.user_id == "user-123"
        assert context.org_id == "org-456"
        assert context.team_id == "team-001"
        assert context.department_id == "dept-789"
        assert context.account_type == "human"
        assert context.is_admin is True  # role == "admin"

    def test_cognito_claims_to_context_service(self):
        """Test converting Cognito claims to TokenContext for service account."""
        from src.auth.middleware import _cognito_claims_to_context

        claims = CognitoTokenClaims(
            sub="agent-client-123",
            iss="https://test",
            client_id="agent-client-123",
            token_use="access",
            exp=1234571490,
            iat=1234567890,
            username="",  # Empty for client_credentials
            org_id="org-456",
            team_id="team-001",
            account_type="service",
        )

        context = _cognito_claims_to_context(claims)

        # For service accounts without username, user_id should be client_id
        assert context.user_id == "agent-client-123"
        assert context.account_type == "service"
        assert context.is_admin is False

    def test_cognito_claims_to_context_platform_admin(self):
        """Test that platform_admin role grants admin privileges."""
        from src.auth.middleware import _cognito_claims_to_context

        claims = CognitoTokenClaims(
            sub="user-123",
            iss="https://test",
            client_id="web-client",
            token_use="access",
            exp=1234571490,
            iat=1234567890,
            username="admin@example.com",
            org_id="platform",
            role="platform_admin",
            cognito_groups=[],
        )

        context = _cognito_claims_to_context(claims)

        assert context.is_admin is True

    def test_cognito_claims_to_context_admins_group(self):
        """Test that admins group grants admin privileges."""
        from src.auth.middleware import _cognito_claims_to_context

        claims = CognitoTokenClaims(
            sub="user-123",
            iss="https://test",
            client_id="web-client",
            token_use="access",
            exp=1234571490,
            iat=1234567890,
            username="user@example.com",
            org_id="org-456",
            cognito_groups=["users", "admins"],
        )

        context = _cognito_claims_to_context(claims)

        assert context.is_admin is True
