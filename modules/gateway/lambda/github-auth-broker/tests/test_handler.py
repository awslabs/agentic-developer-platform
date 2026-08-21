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

# Patch environment before importing handler.
# Issue #3986: the fixture used to pin ALLOWLIST_MODE="open", which would have
# masked the fail-closed default flip. It now mirrors the shipped default ("org").
ENV_VARS = {
    "GITHUB_CLIENT_ID": "test-client-id",
    "GITHUB_CLIENT_SECRET_ARN": "arn:aws:secretsmanager:us-east-1:123456:secret:test",
    "COGNITO_USER_POOL_ID": "us-east-1_TestPool",
    "COGNITO_CLIENT_ID": "test-cognito-client-id",
    "CALLBACK_URL": "https://example.com/api/auth/github/callback",
    "FRONTEND_URL": "https://example.com",
    "ALLOWLIST_MODE": "org",
    "ALLOWED_ORGS": "my-org",
    "ALLOW_OPEN_SIGNUP": "false",
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

    h._github_oauth_creds = None
    h._github_org_token = None
    h.ALLOWLIST_MODE = "org"
    h.ALLOWED_ORGS = "my-org"
    h.ALLOW_OPEN_SIGNUP = False


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

    @patch("handler.check_org_membership", return_value="allowed")
    @patch("handler.exchange_code_for_token")
    @patch("handler.get_github_user")
    @patch("handler.provision_and_authenticate")
    def test_exchange_code_happy_path(self, mock_provision, mock_get_user, mock_exchange, mock_check_org, mock_secrets):
        """Happy path: valid state + code + in-org user → tokens returned via redirect."""
        import handler

        # Set up the cached secret so state verification works
        handler._github_oauth_creds = {"client_id": "test-client-id", "client_secret": "test-secret-123"}

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

    @patch("handler.provision_and_authenticate")
    @patch("handler.exchange_code_for_token")
    @patch("handler.get_github_user")
    @patch("handler.check_org_membership")
    def test_allowlist_denies_non_org_member(self, mock_check_org, mock_get_user, mock_exchange, mock_provision, mock_secrets, monkeypatch):
        """User not in allowed org returns error redirect, no Cognito provision."""
        import handler

        monkeypatch.setenv("ALLOWLIST_MODE", "org")
        # Reload the module-level var
        handler.ALLOWLIST_MODE = "org"
        handler._github_oauth_creds = {"client_id": "test-client-id", "client_secret": "test-secret-123"}

        state = _make_valid_state("test-secret-123")

        mock_exchange.return_value = "gh-access-token"
        mock_get_user.return_value = {
            "id": 99999,
            "login": "outsider",
            "email": "outsider@example.com",
            "name": "Outsider",
            "avatar_url": "",
        }
        mock_check_org.return_value = "denied"

        event = {
            "rawPath": "/callback",
            "requestContext": {"http": {"method": "GET"}},
            "queryStringParameters": {"code": "some-code", "state": state},
            "cookies": [f"gh_oauth_state={state}"],
        }
        response = handler.handler(event, None)

        assert response["statusCode"] == 302
        assert "error=not_authorized" in response["headers"]["Location"]
        # The gate must run BEFORE provisioning: admin_create_user does not fire
        # PreSignUp_ExternalProvider, so a denied user must never reach Cognito.
        mock_provision.assert_not_called()

    @patch("handler.check_org_membership", return_value="allowed")
    @patch("handler.exchange_code_for_token")
    @patch("handler.get_github_user")
    @patch("handler.provision_and_authenticate")
    def test_idempotent_for_existing_user(self, mock_provision, mock_get_user, mock_exchange, mock_check_org, mock_secrets):
        """Existing user still gets tokens (provision is idempotent)."""
        import handler

        handler._github_oauth_creds = {"client_id": "test-client-id", "client_secret": "test-secret-123"}
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


class TestAllowlistGate:
    """Issue #3986: the broker allowlist must fail closed.

    Exercises _check_allowlist directly — it is the single decision point the
    callback consults before provisioning, so mode handling is tested here and
    the callback wiring is covered by TestCallbackEndpoint.
    """

    def test_default_mode_is_org_when_env_unset(self, monkeypatch):
        """An unset ALLOWLIST_MODE must enforce org membership, not allow everyone.

        Reloads the module with the env var absent so the assertion covers the
        real module-level default rather than the test fixture's value.
        """
        import importlib

        import handler

        monkeypatch.delenv("ALLOWLIST_MODE", raising=False)
        monkeypatch.delenv("ALLOW_OPEN_SIGNUP", raising=False)
        reloaded = importlib.reload(handler)
        try:
            assert reloaded.ALLOWLIST_MODE == "org"
            assert reloaded.ALLOW_OPEN_SIGNUP is False
        finally:
            # Restore the fixture's env for subsequent tests in this session.
            monkeypatch.setenv("ALLOWLIST_MODE", "org")
            monkeypatch.setenv("ALLOW_OPEN_SIGNUP", "false")
            importlib.reload(handler)

    @patch("handler.check_org_membership", return_value="allowed")
    def test_org_mode_allows_member(self, mock_check_org):
        """In-org user is allowed (regression: in-org login must keep working)."""
        import handler

        assert handler._check_allowlist("insider", "gh-token") is None

    @patch("handler.check_org_membership", return_value="denied")
    def test_org_mode_denies_non_member(self, mock_check_org):
        """Verified non-member gets not_authorized."""
        import handler

        assert handler._check_allowlist("outsider", "gh-token") == "not_authorized"

    @patch("handler.check_org_membership", return_value="unverified")
    def test_org_mode_unverifiable_is_distinct(self, mock_check_org):
        """A failed check is reported distinctly from a real denial."""
        import handler

        assert handler._check_allowlist("someone", "gh-token") == "org_check_unavailable"

    @patch("handler.check_org_membership", return_value="allowed")
    def test_org_mode_falls_back_to_user_token(self, mock_check_org):
        """With no org-token secret, the user's own OAuth token is used for the check."""
        import handler

        handler._github_org_token = None
        handler._check_allowlist("insider", "user-oauth-token")
        assert mock_check_org.call_args.args[2] == "user-oauth-token"

    def test_open_mode_denies_without_escape_hatch(self):
        """mode=open alone is a misconfiguration and must deny."""
        import handler

        handler.ALLOWLIST_MODE = "open"
        handler.ALLOW_OPEN_SIGNUP = False
        assert handler._check_allowlist("anyone", "gh-token") == "not_authorized"

    def test_open_mode_allows_with_escape_hatch(self):
        """ALLOW_OPEN_SIGNUP=true is the documented, explicit opt-in."""
        import handler

        handler.ALLOWLIST_MODE = "open"
        handler.ALLOW_OPEN_SIGNUP = True
        assert handler._check_allowlist("anyone", "gh-token") is None

    def test_explicit_mode_denies(self):
        """explicit mode is unimplemented in the broker; it must deny, not allow."""
        import handler

        handler.ALLOWLIST_MODE = "explicit"
        assert handler._check_allowlist("anyone", "gh-token") == "not_authorized"

    def test_unknown_mode_denies(self):
        """A typo'd mode must deny (mirrors pre-signup's unknown-mode→deny)."""
        import handler

        handler.ALLOWLIST_MODE = "orgg"
        assert handler._check_allowlist("anyone", "gh-token") == "not_authorized"

    def test_empty_mode_denies(self):
        """An explicitly blank mode must deny rather than fall through."""
        import handler

        handler.ALLOWLIST_MODE = ""
        assert handler._check_allowlist("anyone", "gh-token") == "not_authorized"

    @patch("handler.check_org_membership", return_value="allowed")
    def test_mode_is_case_and_space_insensitive(self, mock_check_org):
        """Operator-supplied ' ORG ' still resolves to org enforcement."""
        import handler

        handler.ALLOWLIST_MODE = " ORG "
        assert handler._check_allowlist("insider", "gh-token") is None


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


class TestAPIGatewayV1EventShape:
    """Test handler with API Gateway v1 REST event format (Issue #525)."""

    def test_start_route_via_path_field(self, mock_secrets):
        """API Gateway v1 uses 'path' instead of 'rawPath'."""
        import handler

        event = {
            "path": "/api/auth/github/start",
            "httpMethod": "GET",
            "requestContext": {
                "resourcePath": "/auth/github/{proxy+}",
                "httpMethod": "GET",
            },
            "headers": {},
            "queryStringParameters": None,
        }
        response = handler.handler(event, None)
        assert response["statusCode"] == 302
        assert "github.com/login/oauth/authorize" in response["headers"]["Location"]

    def test_callback_route_via_path_field(self, mock_secrets):
        """API Gateway v1 callback with cookies in headers."""
        import handler

        handler._github_oauth_creds = {"client_id": "test-client-id", "client_secret": "test-secret-123"}
        state = _make_valid_state("test-secret-123")

        event = {
            "path": "/api/auth/github/callback",
            "httpMethod": "GET",
            "requestContext": {
                "resourcePath": "/auth/github/{proxy+}",
                "httpMethod": "GET",
            },
            "headers": {
                "Cookie": f"gh_oauth_state={state}",
            },
            "queryStringParameters": {"code": "test-code", "state": state},
        }

        with (
            patch("handler.exchange_code_for_token") as mock_exchange,
            patch("handler.get_github_user") as mock_get_user,
            patch("handler.provision_and_authenticate") as mock_provision,
            patch("handler.check_org_membership", return_value="allowed"),
        ):
            mock_exchange.return_value = "gh-token"
            mock_get_user.return_value = {
                "id": 100,
                "login": "v1user",
                "email": "v1@example.com",
                "name": "V1 User",
                "avatar_url": "",
            }
            mock_provision.return_value = {
                "id_token": "idt",
                "access_token": "at",
                "refresh_token": "rt",
                "expires_in": 3600,
            }
            response = handler.handler(event, None)

        assert response["statusCode"] == 302
        assert "access_token=at" in response["headers"]["Location"]

    def test_unknown_path_v1_returns_404(self, mock_secrets):
        """Unknown path returns 404 for v1 event shape."""
        import handler

        event = {
            "path": "/api/auth/github/invalid",
            "httpMethod": "GET",
            "requestContext": {"resourcePath": "/auth/github/{proxy+}", "httpMethod": "GET"},
            "headers": {},
            "queryStringParameters": None,
        }
        response = handler.handler(event, None)
        assert response["statusCode"] == 404


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

    @patch("handler.check_org_membership", return_value="allowed")
    @patch("handler.exchange_code_for_token")
    @patch("handler.get_github_user")
    @patch("handler.provision_and_authenticate")
    def test_refresh_token_in_redirect(self, mock_provision, mock_get_user, mock_exchange, mock_check_org, mock_secrets):
        """Response redirect includes refresh_token parameter."""
        import handler

        handler._github_oauth_creds = {"client_id": "test-client-id", "client_secret": "test-secret-123"}
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


class TestClientIdResolution:
    """Issue #2708: client_id resolves from the OAuth secret, env is fallback."""

    def test_uses_client_id_from_secret(self, mock_secrets):
        """A real client_id in the OAuth secret is used over the env var."""
        import handler

        mock_secrets.get_secret_value.return_value = {"SecretString": json.dumps({"client_id": "Iv1.from_secret", "client_secret": "s"})}
        assert handler._get_github_client_id() == "Iv1.from_secret"

    def test_falls_back_to_env_when_secret_placeholder(self, mock_secrets):
        """Placeholder client_id in the secret falls back to GITHUB_CLIENT_ID env (embark1)."""
        import handler

        mock_secrets.get_secret_value.return_value = {"SecretString": json.dumps({"client_id": "PLACEHOLDER", "client_secret": "s"})}
        # GITHUB_CLIENT_ID env is "test-client-id" (module-level, set at import)
        assert handler._get_github_client_id() == "test-client-id"

    def test_falls_back_to_env_when_secret_empty(self, mock_secrets):
        """Empty client_id in the secret falls back to the env var."""
        import handler

        mock_secrets.get_secret_value.return_value = {"SecretString": json.dumps({"client_id": "", "client_secret": "s"})}
        assert handler._get_github_client_id() == "test-client-id"


class TestCallbackUrlDerivation:
    """Issue #2708: CALLBACK_URL is derived from requestContext; env var wins."""

    def test_env_var_wins_when_set(self):
        """When CALLBACK_URL env is set it is used verbatim."""
        import handler

        handler.CALLBACK_URL = "https://env.example.com/api/auth/github/callback"
        try:
            event = {"requestContext": {"domainName": "api.other.com", "stage": "prod"}}
            assert handler._derive_callback_url(event) == "https://env.example.com/api/auth/github/callback"
        finally:
            handler.CALLBACK_URL = ""

    def test_derives_from_request_context_with_stage(self):
        """Derived URL is https://<domain>/<stage>/auth/github/callback."""
        import handler

        handler.CALLBACK_URL = ""
        event = {"requestContext": {"domainName": "abc123.execute-api.us-east-1.amazonaws.com", "stage": "prod"}}
        assert handler._derive_callback_url(event) == "https://abc123.execute-api.us-east-1.amazonaws.com/prod/auth/github/callback"

    def test_derives_without_default_stage(self):
        """The $default stage is not part of the invoke path."""
        import handler

        handler.CALLBACK_URL = ""
        event = {"requestContext": {"domainName": "d.example.com", "stage": "$default"}}
        assert handler._derive_callback_url(event) == "https://d.example.com/auth/github/callback"

    def test_returns_empty_when_no_context(self):
        """No env var and no domainName yields an empty string (no broken redirect)."""
        import handler

        handler.CALLBACK_URL = ""
        assert handler._derive_callback_url({"requestContext": {}}) == ""
