"""
Unit tests for the GitHub Auth Broker Lambda handler.

Issue #520: Lambda broker for GitHub sign-in.
"""

import hashlib
import hmac as hmac_module
import json
import time
from unittest.mock import MagicMock, patch

import pytest

# Patch environment before importing handler
ENV_VARS = {
    "GITHUB_CLIENT_ID": "test-client-id",
    "GITHUB_CLIENT_SECRET_ARN": "arn:aws:secretsmanager:us-east-1:123456:secret:test",
    "COGNITO_USER_POOL_ID": "us-east-1_TestPool",
    "COGNITO_CLIENT_ID": "test-cognito-client-id",
    "CALLBACK_URL": "https://example.com/api/auth/github/callback",
    "FRONTEND_URL": "https://example.com",
    "ALLOWLIST_MODE": "open",
    "ALLOWED_ORGS": "my-org",
    "GITHUB_TOKEN_SECRET_ARN": "",
    "LOG_LEVEL": "DEBUG",
}


@pytest.fixture(autouse=True)
def env_setup(monkeypatch):
    """Set up environment variables for all tests."""
    for key, value in ENV_VARS.items():
        monkeypatch.setenv(key, value)
    # Reset cached secrets and module-level config between tests
    import handler as h

    h._github_client_secret = None
    h._github_org_token = None
    h.ALLOWLIST_MODE = "open"


@pytest.fixture
def mock_secrets():
    """Mock Secrets Manager client."""
    with patch("handler.boto3.client") as mock_client:
        sm = MagicMock()
        mock_client.return_value = sm
        sm.get_secret_value.return_value = {"SecretString": json.dumps({"client_id": "test-client-id", "client_secret": "test-secret-123"})}
        yield sm


def _make_valid_state(secret: str = "test-secret-123") -> str:
    """Create a valid state token for testing."""
    import secrets as sec

    nonce = sec.token_urlsafe(24)
    timestamp = str(int(time.time()))
    payload = f"{nonce}.{timestamp}"
    signature = hmac_module.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()[:16]
    return f"{payload}.{signature}"


class TestHandlerRouting:
    """Test request routing."""

    def test_start_route(self, mock_secrets):
        """Test that /start path routes correctly."""
        import handler

        event = {"rawPath": "/api/auth/github/start", "requestContext": {"http": {"method": "GET"}}}
        response = handler.handler(event, None)
        assert response["statusCode"] == 302
        assert "github.com/login/oauth/authorize" in response["headers"]["Location"]

    def test_callback_route_missing_code(self, mock_secrets):
        """Test callback with missing code redirects with error."""
        import handler

        event = {
            "rawPath": "/api/auth/github/callback",
            "requestContext": {"http": {"method": "GET"}},
            "queryStringParameters": {},
            "cookies": [],
        }
        response = handler.handler(event, None)
        assert response["statusCode"] == 302
        assert "error=missing_code" in response["headers"]["Location"]

    def test_unknown_path_returns_404(self, mock_secrets):
        """Test unknown path returns 404."""
        import handler

        event = {"rawPath": "/unknown", "requestContext": {"http": {"method": "GET"}}}
        response = handler.handler(event, None)
        assert response["statusCode"] == 404


class TestStartEndpoint:
    """Test the /start endpoint."""

    def test_redirects_to_github(self, mock_secrets):
        """Test that /start redirects to GitHub OAuth authorize."""
        import handler

        event = {"rawPath": "/start", "requestContext": {"http": {"method": "GET"}}}
        response = handler.handler(event, None)

        assert response["statusCode"] == 302
        location = response["headers"]["Location"]
        assert "github.com/login/oauth/authorize" in location
        assert "client_id=test-client-id" in location
        assert "state=" in location

    def test_sets_state_cookie(self, mock_secrets):
        """Test that /start sets a state cookie."""
        import handler

        event = {"rawPath": "/start", "requestContext": {"http": {"method": "GET"}}}
        response = handler.handler(event, None)

        cookie = response["headers"]["Set-Cookie"]
        assert "gh_oauth_state=" in cookie
        assert "HttpOnly" in cookie
        assert "Secure" in cookie
        assert "SameSite=Lax" in cookie


class TestCallbackEndpoint:
    """Test the /callback endpoint."""

    def test_rejects_bad_state_cookie(self, mock_secrets):
        """Missing or mismatched state returns error redirect, no Cognito calls."""
        import handler

        event = {
            "rawPath": "/callback",
            "requestContext": {"http": {"method": "GET"}},
            "queryStringParameters": {"code": "test-code", "state": "bad-state"},
            "cookies": ["gh_oauth_state=different-state"],
        }
        response = handler.handler(event, None)
        assert response["statusCode"] == 302
        assert "error=state_mismatch" in response["headers"]["Location"]

    def test_rejects_missing_state(self, mock_secrets):
        """Missing state cookie redirects with error."""
        import handler

        event = {
            "rawPath": "/callback",
            "requestContext": {"http": {"method": "GET"}},
            "queryStringParameters": {"code": "test-code", "state": "some-state"},
            "cookies": [],
        }
        response = handler.handler(event, None)
        assert response["statusCode"] == 302
        assert "error=missing_state" in response["headers"]["Location"]

    def test_github_error_param(self, mock_secrets):
        """GitHub error parameter is handled gracefully."""
        import handler

        event = {
            "rawPath": "/callback",
            "requestContext": {"http": {"method": "GET"}},
            "queryStringParameters": {
                "error": "access_denied",
                "error_description": "User denied access",
            },
            "cookies": [],
        }
        response = handler.handler(event, None)
        assert response["statusCode"] == 302
        assert "error=" in response["headers"]["Location"]

    @patch("handler.exchange_code_for_token")
    @patch("handler.get_github_user")
    @patch("handler.provision_and_authenticate")
    def test_exchange_code_happy_path(self, mock_provision, mock_get_user, mock_exchange, mock_secrets):
        """Happy path: valid state + code → tokens returned via redirect."""
        import handler

        # Set up the cached secret so state verification works
        handler._github_client_secret = "test-secret-123"

        state = _make_valid_state("test-secret-123")

        mock_exchange.return_value = "gh-access-token"
        mock_get_user.return_value = {
            "id": 12345,
            "login": "testuser",
            "email": "test@example.com",
            "name": "Test User",
            "avatar_url": "https://avatars.githubusercontent.com/u/12345",
        }
        mock_provision.return_value = {
            "id_token": "cognito-id-token",
            "access_token": "cognito-access-token",
            "refresh_token": "cognito-refresh-token",
            "expires_in": 3600,
        }

        event = {
            "rawPath": "/callback",
            "requestContext": {"http": {"method": "GET"}},
            "queryStringParameters": {"code": "github-auth-code", "state": state},
            "cookies": [f"gh_oauth_state={state}"],
        }
        response = handler.handler(event, None)

        assert response["statusCode"] == 302
        location = response["headers"]["Location"]
        assert "example.com/auth/callback" in location
        assert "id_token=cognito-id-token" in location
        assert "access_token=cognito-access-token" in location
        assert "refresh_token=cognito-refresh-token" in location
        assert "source=github_broker" in location

        # Verify the correct calls were made
        mock_exchange.assert_called_once_with("github-auth-code", "test-client-id", "test-secret-123")
        mock_get_user.assert_called_once_with("gh-access-token")
        mock_provision.assert_called_once_with(
            user_pool_id="us-east-1_TestPool",
            client_id="test-cognito-client-id",
            github_id=12345,
            github_login="testuser",
            email="test@example.com",
            name="Test User",
            avatar_url="https://avatars.githubusercontent.com/u/12345",
        )

    @patch("handler.exchange_code_for_token")
    @patch("handler.get_github_user")
    @patch("handler.check_org_membership")
    def test_allowlist_denies_non_org_member(self, mock_check_org, mock_get_user, mock_exchange, mock_secrets, monkeypatch):
        """User not in allowed org returns error redirect, no Cognito provision."""
        import handler

        monkeypatch.setenv("ALLOWLIST_MODE", "org")
        # Reload the module-level var
        handler.ALLOWLIST_MODE = "org"
        handler._github_client_secret = "test-secret-123"

        state = _make_valid_state("test-secret-123")

        mock_exchange.return_value = "gh-access-token"
        mock_get_user.return_value = {
            "id": 99999,
            "login": "outsider",
            "email": "outsider@example.com",
            "name": "Outsider",
            "avatar_url": "",
        }
        mock_check_org.return_value = False

        event = {
            "rawPath": "/callback",
            "requestContext": {"http": {"method": "GET"}},
            "queryStringParameters": {"code": "some-code", "state": state},
            "cookies": [f"gh_oauth_state={state}"],
        }
        response = handler.handler(event, None)

        assert response["statusCode"] == 302
        assert "error=not_authorized" in response["headers"]["Location"]

    @patch("handler.exchange_code_for_token")
    @patch("handler.get_github_user")
    @patch("handler.provision_and_authenticate")
    def test_idempotent_for_existing_user(self, mock_provision, mock_get_user, mock_exchange, mock_secrets):
        """Existing user still gets tokens (provision is idempotent)."""
        import handler

        handler._github_client_secret = "test-secret-123"
        state = _make_valid_state("test-secret-123")

        mock_exchange.return_value = "gh-access-token"
        mock_get_user.return_value = {
            "id": 12345,
            "login": "testuser",
            "email": "test@example.com",
            "name": "Test User",
            "avatar_url": "",
        }
        mock_provision.return_value = {
            "id_token": "id-tok",
            "access_token": "access-tok",
            "refresh_token": "refresh-tok",
            "expires_in": 3600,
        }

        event = {
            "rawPath": "/callback",
            "requestContext": {"http": {"method": "GET"}},
            "queryStringParameters": {"code": "code-123", "state": state},
            "cookies": [f"gh_oauth_state={state}"],
        }
        response = handler.handler(event, None)

        assert response["statusCode"] == 302
        assert "access_token=access-tok" in response["headers"]["Location"]
        mock_provision.assert_called_once()


class TestUsernameFormat:
    """Test that the username format is GitHub_<numeric-id>."""

    @patch("cognito_provisioner.boto3.client")
    def test_username_format(self, mock_boto_client):
        """Username must be GitHub_<numeric-id>, never the login."""
        from cognito_provisioner import provision_and_authenticate

        mock_cognito = MagicMock()
        mock_boto_client.return_value = mock_cognito

        # User doesn't exist
        from botocore.exceptions import ClientError

        mock_cognito.admin_get_user.side_effect = ClientError(
            {"Error": {"Code": "UserNotFoundException", "Message": "not found"}},
            "AdminGetUser",
        )
        mock_cognito.admin_initiate_auth.return_value = {
            "AuthenticationResult": {
                "IdToken": "id",
                "AccessToken": "access",
                "RefreshToken": "refresh",
                "ExpiresIn": 3600,
            }
        }

        provision_and_authenticate(
            user_pool_id="us-east-1_Test",
            client_id="client-123",
            github_id=42,
            github_login="octocat",
            email="octo@github.com",
            name="Octocat",
            avatar_url="https://avatars.githubusercontent.com/u/42",
        )

        # Verify username is GitHub_42, NOT "octocat"
        create_call = mock_cognito.admin_create_user.call_args
        assert create_call.kwargs["Username"] == "GitHub_42"


class TestCookieParsing:
    """Test cookie parsing from different event formats."""

    def test_parse_cookies_from_list(self):
        """Parse cookies from Lambda Function URL format (list)."""
        from handler import _parse_cookies

        event = {"cookies": ["gh_oauth_state=abc123", "other=value"]}
        cookies = _parse_cookies(event)
        assert cookies["gh_oauth_state"] == "abc123"
        assert cookies["other"] == "value"

    def test_parse_cookies_from_header(self):
        """Parse cookies from API Gateway v1 format (header)."""
        from handler import _parse_cookies

        event = {"cookies": [], "headers": {"cookie": "gh_oauth_state=xyz; other=val"}}
        cookies = _parse_cookies(event)
        assert cookies["gh_oauth_state"] == "xyz"


class TestRefreshTokenInResponse:
    """Test that refresh token is included in the redirect."""

    @patch("handler.exchange_code_for_token")
    @patch("handler.get_github_user")
    @patch("handler.provision_and_authenticate")
    def test_refresh_token_in_redirect(self, mock_provision, mock_get_user, mock_exchange, mock_secrets):
        """Response redirect includes refresh_token parameter."""
        import handler

        handler._github_client_secret = "test-secret-123"
        state = _make_valid_state("test-secret-123")

        mock_exchange.return_value = "gh-token"
        mock_get_user.return_value = {
            "id": 1,
            "login": "u",
            "email": "u@e.com",
            "name": "U",
            "avatar_url": "",
        }
        mock_provision.return_value = {
            "id_token": "idt",
            "access_token": "at",
            "refresh_token": "my-refresh-token",
            "expires_in": 3600,
        }

        event = {
            "rawPath": "/callback",
            "requestContext": {"http": {"method": "GET"}},
            "queryStringParameters": {"code": "c", "state": state},
            "cookies": [f"gh_oauth_state={state}"],
        }
        response = handler.handler(event, None)

        location = response["headers"]["Location"]
        assert "refresh_token=my-refresh-token" in location
