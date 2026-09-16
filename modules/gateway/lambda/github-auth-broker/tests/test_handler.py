"""
Unit tests for the GitHub Auth Broker Lambda handler.

Issue #520: Lambda broker for GitHub sign-in.
"""

import base64
import hashlib
import hmac as hmac_module
import json
import logging
import os
import time
import urllib.parse
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

    def test_redirect_uri_mismatch_gets_a_distinct_error_code(self, mock_secrets):
        """Issue #4017: GitHub exposes no API to read an App's callback URL, so
        this error path is the ONLY signal that it has drifted. It must be
        distinguishable from a generic github_error so the login page can tell
        the operator what actually broke."""
        import handler

        event = {
            "rawPath": "/callback",
            "requestContext": {"http": {"method": "GET"}},
            "queryStringParameters": {
                "error": "redirect_uri_mismatch",
                "error_description": "The redirect_uri MUST match the registered callback URL",
            },
            "cookies": [],
        }
        response = handler.handler(event, None)
        assert response["statusCode"] == 302
        assert "error=redirect_uri_mismatch" in response["headers"]["Location"]

    def test_redirect_uri_mismatch_logs_the_callback_we_sent(self, mock_secrets, caplog):
        """Without the sent value in the logs, the operator has nothing to compare
        against the App's settings page."""
        import handler

        event = {
            "rawPath": "/callback",
            "requestContext": {
                "http": {"method": "GET"},
                "domainName": "abc123.execute-api.us-east-1.amazonaws.com",
                "stage": "dev",
            },
            "queryStringParameters": {"error": "redirect_uri_mismatch"},
            "cookies": [],
        }
        # Clear the env override so the runtime-derived value (#2708) is logged.
        previous = handler.CALLBACK_URL
        handler.CALLBACK_URL = ""
        try:
            with caplog.at_level(logging.ERROR):
                handler.handler(event, None)
        finally:
            handler.CALLBACK_URL = previous

        logged = "\n".join(r.getMessage() for r in caplog.records)
        assert "oauth_callback_drift" in logged
        assert "https://abc123.execute-api.us-east-1.amazonaws.com/dev/auth/github/callback" in logged

    def test_redirect_uri_mismatch_does_not_pin_the_callback_url(self, mock_secrets):
        """Issue #4017 / #2708: writing the derived value into CALLBACK_URL would
        reverse the runtime derivation and pin a value that goes stale silently."""
        import handler

        before = handler.CALLBACK_URL
        event = {
            "rawPath": "/callback",
            "requestContext": {
                "http": {"method": "GET"},
                "domainName": "abc123.execute-api.us-east-1.amazonaws.com",
                "stage": "dev",
            },
            "queryStringParameters": {"error": "redirect_uri_mismatch"},
            "cookies": [],
        }
        handler.handler(event, None)

        assert handler.CALLBACK_URL == before
        assert "CALLBACK_URL" not in os.environ or os.environ.get("CALLBACK_URL") == before

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


def _fake_membership_reader(verdict=None, exc=None):
    """Stand in for lambda/shared/membership_eligibility, imported lazily by the handler."""
    fake = MagicMock()
    fake.ELIGIBLE = "eligible"
    fake.NOT_ELIGIBLE = "not_eligible"
    fake.UNAVAILABLE = "unavailable"
    if exc is not None:
        fake.check_platform_membership.side_effect = exc
    else:
        fake.check_platform_membership.return_value = verdict
    return fake


class TestPlatformAllowlistMode:
    """Issue #4844: ALLOWLIST_MODE=platform decides from platform membership.

    Eligibility stops being "is this person in the allowed GitHub org?" and becomes
    "does this GitHub identity resolve to a user holding at least one platform org
    membership?". GitHub still proves *who* you are; it no longer decides whether
    you belong.

    Every failure mode denies. This is the broker — the sole enforcement point for
    GitHub sign-in (#3986) — so a fail-open bug here is a platform-wide
    authorization hole, and a spurious-deny bug is a platform-wide login outage.
    """

    _ID = "20402445"

    def _check(self, verdict=None, exc=None, github_id=None):
        import handler

        handler.ALLOWLIST_MODE = "platform"
        reader = _fake_membership_reader(verdict, exc)
        with patch.dict("sys.modules", {"membership_eligibility": reader}):
            result = handler._check_allowlist("octocat", "gh-token", self._ID if github_id is None else github_id)
        return result, reader

    def test_membership_holder_is_allowed(self):
        """The mode's whole purpose: a platform member signs in."""
        result, _ = self._check(verdict="eligible")
        assert result is None

    def test_no_membership_is_denied(self):
        result, _ = self._check(verdict="not_eligible")
        assert result == "not_authorized"

    def test_unavailable_source_denies_with_a_distinct_code(self):
        """Fail-CLOSED, and attributably so.

        An unavailable membership source must deny rather than fall through to
        another mode. The code differs from not_authorized so an operator can tell
        "the projection table is unreachable" from "this user is genuinely not a
        member" — collapsing them is the ambiguity #3986 was filed to fix.
        """
        result, _ = self._check(verdict="unavailable")
        assert result == "membership_check_unavailable"

    def test_raising_read_denies(self):
        """A raising read denies rather than being swallowed.

        #4849's shadow wrapper swallows exceptions on purpose — right while the
        verdict was inert, fail-OPEN once it is authoritative.
        """
        result, _ = self._check(exc=RuntimeError("ddb down"))
        assert result == "membership_check_unavailable"

    def test_missing_shared_reader_denies(self):
        """ImportError denies: the mode cannot be enforced, so it must not look enforced.

        Guards the packaging contract — deploy-broker.sh and the broker deploy
        workflow each copy lambda/shared/membership_eligibility.py in flat beside
        the handler. If that step is ever dropped, sign-in must fail closed rather
        than admit everyone.
        """
        import handler

        handler.ALLOWLIST_MODE = "platform"
        with patch.dict("sys.modules", {"membership_eligibility": None}):
            assert handler._check_allowlist("octocat", "gh-token", self._ID) == "membership_check_unavailable"

    def test_unrecognised_verdict_denies(self):
        """A verdict this code does not know is "not proven", i.e. denied."""
        result, _ = self._check(verdict="something_new")
        assert result == "membership_check_unavailable"

    def test_lookup_uses_the_github_id_not_the_login(self):
        """The projection is id-keyed: GitHub logins are renameable, ids are not."""
        _, reader = self._check(verdict="eligible")
        reader.check_platform_membership.assert_called_once_with(self._ID)

    def test_empty_github_id_is_passed_through_and_denied(self):
        """With no id there is nothing to look up, and the reader denies it.

        The handler does not silently substitute the login: that would look up the
        wrong user instead of failing.
        """
        reader = _fake_membership_reader("not_eligible")
        import handler

        handler.ALLOWLIST_MODE = "platform"
        with patch.dict("sys.modules", {"membership_eligibility": reader}):
            assert handler._check_allowlist("octocat", "gh-token", "") == "not_authorized"
        reader.check_platform_membership.assert_called_once_with("")

    @patch("handler.check_org_membership", return_value="denied")
    def test_github_org_membership_is_irrelevant_in_this_mode(self, mock_check_org):
        """A platform member with NO GitHub org relationship is allowed.

        This is the admin-created-org case the mode exists for, and the assertion
        that GitHub is no longer the authority: the org check must not even run.
        """
        result, _ = self._check(verdict="eligible")
        assert result is None
        mock_check_org.assert_not_called()

    def test_shadow_logging_is_skipped_in_this_mode(self):
        """The #4849 shadow read must not double the DDB call once it is the real one.

        Cognito's synchronous trigger budget is 5s and non-negotiable; a duplicate
        read inside it buys nothing but latency, and its "would_agree" line could
        only ever say True.
        """
        import handler

        handler.ALLOWLIST_MODE = "platform"
        reader = _fake_membership_reader("eligible")
        with patch.dict("sys.modules", {"membership_eligibility": reader}):
            handler._log_membership_eligibility_shadow(self._ID, "octocat", None)
        reader.check_platform_membership.assert_not_called()

    def test_shadow_logging_still_runs_in_org_mode(self):
        """...but the shadow read stays live for the modes it was built to measure."""
        import handler

        handler.ALLOWLIST_MODE = "org"
        reader = _fake_membership_reader("eligible")
        with patch.dict("sys.modules", {"membership_eligibility": reader}):
            handler._log_membership_eligibility_shadow(self._ID, "octocat", None)
        reader.check_platform_membership.assert_called_once_with(self._ID)


class TestPlatformModeBehaviouralGate:
    """Issue #4844: the gate test that matters, driven through the real callback.

    The issue is explicit that a parity matrix over the pre-signup trigger proves
    nothing about live sign-in, because that trigger never fires for this flow —
    ``admin_create_user`` does not raise ``PreSignUp_ExternalProvider``. So these
    tests drive ``_handle_callback`` end-to-end with only the GitHub/Cognito edges
    stubbed, and assert on what the user actually gets: a session, or a redirect
    carrying an error.

    The load-bearing assertion is ``provision_and_authenticate`` — a denied user
    must not get a Cognito account provisioned, not merely be redirected.
    """

    _USER = {
        "id": 20402445,
        "login": "octocat",
        "email": "octocat@example.com",
        "name": "Octo Cat",
        "avatar_url": "https://example.invalid/a.png",
    }

    def _run(self, verdict, mock_provision, mock_get_user, mock_exchange):
        import handler

        handler.ALLOWLIST_MODE = "platform"
        mock_exchange.return_value = "gh-token"
        mock_get_user.return_value = dict(self._USER)
        mock_provision.return_value = {
            "access_token": "access-tok",
            "id_token": "id-tok",
            "refresh_token": "refresh-tok",
            "expires_in": 3600,
        }
        handler._github_oauth_creds = {"client_id": "test-client-id", "client_secret": "test-secret-123"}
        state = _make_valid_state("test-secret-123")
        event = {
            "rawPath": "/callback",
            "requestContext": {"http": {"method": "GET"}},
            "queryStringParameters": {"code": "gh-code", "state": state},
            "cookies": [f"gh_oauth_state={state}"],
        }
        reader = _fake_membership_reader(verdict)
        with patch.dict("sys.modules", {"membership_eligibility": reader}):
            return handler.handler(event, None), reader

    @patch("handler.provision_and_authenticate")
    @patch("handler.get_github_user")
    @patch("handler.exchange_code_for_token")
    def test_membership_holder_signs_in(self, mock_exchange, mock_get_user, mock_provision, mock_secrets):
        """A user holding a platform membership gets a session, with no GitHub org check."""
        response, reader = self._run("eligible", mock_provision, mock_get_user, mock_exchange)

        assert response["statusCode"] == 302
        location = response["headers"]["Location"]
        assert "error" not in location
        mock_provision.assert_called_once()
        # Looked up by numeric id, as a string.
        reader.check_platform_membership.assert_called_once_with(str(self._USER["id"]))

    @patch("handler.provision_and_authenticate")
    @patch("handler.get_github_user")
    @patch("handler.exchange_code_for_token")
    def test_membershipless_user_is_refused_and_not_provisioned(self, mock_exchange, mock_get_user, mock_provision, mock_secrets):
        """A GitHub user with no platform membership is refused before provisioning.

        The provisioning assertion is the real one: a denial that still created the
        Cognito account would leave a usable account behind and make the gate
        cosmetic.
        """
        response, _ = self._run("not_eligible", mock_provision, mock_get_user, mock_exchange)

        assert response["statusCode"] == 302
        assert "error=not_authorized" in response["headers"]["Location"]
        mock_provision.assert_not_called()

    @patch("handler.provision_and_authenticate")
    @patch("handler.get_github_user")
    @patch("handler.exchange_code_for_token")
    def test_unavailable_source_refuses_and_does_not_provision(self, mock_exchange, mock_get_user, mock_provision, mock_secrets):
        """Fail-closed end to end: an unreadable projection denies the sign-in."""
        response, _ = self._run("unavailable", mock_provision, mock_get_user, mock_exchange)

        assert response["statusCode"] == 302
        assert "error=membership_check_unavailable" in response["headers"]["Location"]
        mock_provision.assert_not_called()


class TestOrgModeLoginCanary:
    """Issue #4844: the eternal login canary — org mode is untouched by this change.

    Adding a mode to the most outage-prone surface in this platform's history has
    exactly one hard requirement: every environment, all of which are on ``org`` or
    ``open`` today, must behave bit-identically. Driven through the real callback
    rather than the predicate, because that is what a user experiences.
    """

    _USER = {
        "id": 20402445,
        "login": "insider",
        "email": "insider@example.com",
        "name": "In Sider",
        "avatar_url": "https://example.invalid/a.png",
    }

    def _run(self, mode, mock_provision, mock_get_user, mock_exchange, *, allow_open=False):
        import handler

        handler.ALLOWLIST_MODE = mode
        handler.ALLOW_OPEN_SIGNUP = allow_open
        mock_exchange.return_value = "gh-token"
        mock_get_user.return_value = dict(self._USER)
        mock_provision.return_value = {"access_token": "a", "id_token": "i", "refresh_token": "r", "expires_in": 3600}
        handler._github_oauth_creds = {"client_id": "test-client-id", "client_secret": "test-secret-123"}
        state = _make_valid_state("test-secret-123")
        event = {
            "rawPath": "/callback",
            "requestContext": {"http": {"method": "GET"}},
            "queryStringParameters": {"code": "gh-code", "state": state},
            "cookies": [f"gh_oauth_state={state}"],
        }
        return handler.handler(event, None)

    @patch("handler.check_org_membership", return_value="allowed")
    @patch("handler.provision_and_authenticate")
    @patch("handler.get_github_user")
    @patch("handler.exchange_code_for_token")
    def test_org_member_still_signs_in(self, mock_exchange, mock_get_user, mock_provision, mock_check_org, mock_secrets):
        """The canary: an in-org user signs in exactly as before #4844."""
        response = self._run("org", mock_provision, mock_get_user, mock_exchange)

        assert response["statusCode"] == 302
        assert "error" not in response["headers"]["Location"]
        mock_provision.assert_called_once()
        mock_check_org.assert_called_once()

    @patch("handler.check_org_membership", return_value="denied")
    @patch("handler.provision_and_authenticate")
    @patch("handler.get_github_user")
    @patch("handler.exchange_code_for_token")
    def test_org_non_member_still_denied(self, mock_exchange, mock_get_user, mock_provision, mock_check_org, mock_secrets):
        response = self._run("org", mock_provision, mock_get_user, mock_exchange)

        assert "error=not_authorized" in response["headers"]["Location"]
        mock_provision.assert_not_called()

    @patch("handler.provision_and_authenticate")
    @patch("handler.get_github_user")
    @patch("handler.exchange_code_for_token")
    def test_open_mode_with_flag_still_signs_in(self, mock_exchange, mock_get_user, mock_provision, mock_secrets):
        """dev runs mode=open today; #4844 must not disturb it."""
        response = self._run("open", mock_provision, mock_get_user, mock_exchange, allow_open=True)

        assert "error" not in response["headers"]["Location"]
        mock_provision.assert_called_once()

    @patch("handler.provision_and_authenticate")
    @patch("handler.get_github_user")
    @patch("handler.exchange_code_for_token")
    def test_org_mode_does_not_consult_the_membership_projection(self, mock_exchange, mock_get_user, mock_provision, mock_secrets):
        """org mode's DECISION never depends on the projection.

        The #4849 shadow read still runs in org mode (asserted in
        TestPlatformAllowlistMode), so this pins the thing that matters: whatever
        the projection says, an org member is allowed and a non-member is denied.
        A projection that reads NOT_ELIGIBLE must not leak into an org-mode denial.
        """
        import handler

        reader = _fake_membership_reader("not_eligible")
        with patch.dict("sys.modules", {"membership_eligibility": reader}):
            with patch.object(handler, "check_org_membership", return_value="allowed"):
                response = self._run("org", mock_provision, mock_get_user, mock_exchange)

        assert "error" not in response["headers"]["Location"]
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


# =============================================================================
# Issue #4133 — session handoff carries no tokens in the URL and is state-bound
# =============================================================================


def _login_event(state: str, path: str = "/callback") -> dict:
    """A GitHub callback event with matching state param + cookie."""
    return {
        "rawPath": path,
        "requestContext": {"http": {"method": "GET"}},
        "queryStringParameters": {"code": "github-auth-code", "state": state},
        "cookies": [f"gh_oauth_state={state}"],
    }


@pytest.fixture
def broker_user_flow():
    """Patch the GitHub/Cognito calls a successful login makes."""
    with (
        patch("handler.check_org_membership", return_value="allowed"),
        patch("handler.exchange_code_for_token", return_value="gh-token"),
        patch("handler.get_github_user") as get_user,
        patch("handler.provision_and_authenticate") as provision,
    ):
        get_user.return_value = {
            "id": 12345,
            "login": "testuser",
            "email": "test@example.com",
            "name": "Test User",
            "avatar_url": "",
        }
        provision.return_value = {
            "id_token": "cognito-id-token",
            "access_token": "cognito-access-token",
            "refresh_token": "cognito-refresh-token",
            "expires_in": 3600,
        }
        yield provision


@pytest.fixture
def code_table(monkeypatch):
    """Enable the exchange-code transport with an in-memory DynamoDB stand-in.

    Models the two calls the handler makes — put_item, and delete_item with
    ReturnValues=ALL_OLD — including the single-use semantics that make the
    delete both the read and the invalidation.
    """
    import handler

    monkeypatch.setattr(handler, "AUTH_CODE_TABLE", "test-auth-codes")
    rows: dict[str, dict] = {}

    # N803: these argument names must match boto3's PascalCase DynamoDB kwargs
    # exactly — the handler calls them by keyword, so lowercase would not bind.
    def put_item(TableName, Item):  # noqa: N803
        rows[Item["code"]["S"]] = Item
        return {}

    def delete_item(TableName, Key, ReturnValues=None):  # noqa: N803
        item = rows.pop(Key["code"]["S"], None)
        return {"Attributes": item} if item else {}

    ddb = MagicMock()
    ddb.put_item.side_effect = put_item
    ddb.delete_item.side_effect = delete_item

    def fake_client(service, *args, **kwargs):
        if service == "dynamodb":
            return ddb
        sm = MagicMock()
        sm.get_secret_value.return_value = {"SecretString": json.dumps({"client_id": "test-client-id", "client_secret": "test-secret-123"})}
        return sm

    with patch("handler.boto3.client", side_effect=fake_client):
        yield {"rows": rows, "ddb": ddb}


def _seed_creds():
    """Prime the cached OAuth creds so state signing/verification works."""
    import handler

    handler._github_oauth_creds = {"client_id": "test-client-id", "client_secret": "test-secret-123"}
    handler._github_oauth_creds_ts = time.time()


class TestNoTokensInRedirect:
    """The core #4133 fix: session tokens must never reach the URL."""

    def test_redirect_carries_no_token_params(self, code_table, broker_user_flow):
        """Redirect has a code, and none of the three token params."""
        import handler

        _seed_creds()
        state = handler._generate_state("spa-nonce-abc")
        response = handler.handler(_login_event(state), None)

        assert response["statusCode"] == 302
        location = response["headers"]["Location"]
        assert "id_token=" not in location
        assert "access_token=" not in location
        assert "refresh_token=" not in location
        # The real token values must not appear under any parameter name either.
        assert "cognito-id-token" not in location
        assert "cognito-access-token" not in location
        assert "cognito-refresh-token" not in location
        assert "code=" in location
        assert "source=github_broker" in location

    def test_redirect_echoes_the_spa_nonce(self, code_table, broker_user_flow):
        """The SPA's nonce comes back as `state` so the SPA can verify it."""
        import handler

        _seed_creds()
        state = handler._generate_state("spa-nonce-abc")
        location = handler.handler(_login_event(state), None)["headers"]["Location"]

        assert "state=spa-nonce-abc" in location

    def test_redirect_sets_no_referrer_policy(self, code_table, broker_user_flow):
        """Referrer-Policy: no-referrer keeps the callback URL out of Referer."""
        import handler

        _seed_creds()
        state = handler._generate_state("spa-nonce-abc")
        response = handler.handler(_login_event(state), None)

        assert response["headers"]["Referrer-Policy"] == "no-referrer"

    def test_error_redirect_sets_no_referrer_policy(self, mock_secrets):
        """Error redirects are covered too."""
        import handler

        assert handler._redirect_with_error("boom")["headers"]["Referrer-Policy"] == "no-referrer"

    def test_stored_row_holds_only_a_nonce_digest(self, code_table, broker_user_flow):
        """The persisted row must not contain the raw nonce."""
        import handler

        _seed_creds()
        state = handler._generate_state("spa-nonce-abc")
        handler.handler(_login_event(state), None)

        (row,) = code_table["rows"].values()
        assert row["app_state_hash"]["S"] == hashlib.sha256(b"spa-nonce-abc").hexdigest()
        assert "spa-nonce-abc" not in json.dumps(row)

    def test_handoff_failure_does_not_fall_back_to_url_tokens(self, broker_user_flow, monkeypatch):
        """A DynamoDB fault must error out, never leak tokens into the URL."""
        import handler

        _seed_creds()
        monkeypatch.setattr(handler, "AUTH_CODE_TABLE", "test-auth-codes")

        ddb = MagicMock()
        ddb.put_item.side_effect = RuntimeError("throttled")

        with patch("handler.boto3.client", return_value=ddb):
            state = handler._generate_state("spa-nonce-abc")
            # State verification needs the cached creds, already seeded above.
            response = handler.handler(_login_event(state), None)

        location = response["headers"]["Location"]
        assert "error=handoff_failed" in location
        assert "cognito-access-token" not in location


class TestExchangeEndpoint:
    """POST /exchange swaps the single-use code for tokens in a body."""

    def _issue_code(self, handler_mod, app_state="spa-nonce-abc"):
        _seed_creds()
        state = handler_mod._generate_state(app_state)
        location = handler_mod.handler(_login_event(state), None)["headers"]["Location"]
        query = urllib.parse.parse_qs(urllib.parse.urlparse(location).query)
        return query["code"][0]

    def _exchange(self, handler_mod, code, app_state="spa-nonce-abc"):
        return handler_mod.handler(
            {
                "rawPath": "/exchange",
                "requestContext": {"http": {"method": "POST"}},
                "body": json.dumps({"code": code, "app_state": app_state}),
            },
            None,
        )

    def test_returns_tokens_in_body(self, code_table, broker_user_flow):
        """Happy path: the code yields the tokens as JSON."""
        import handler

        code = self._issue_code(handler)
        response = self._exchange(handler, code)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["id_token"] == "cognito-id-token"
        assert body["access_token"] == "cognito-access-token"
        assert body["refresh_token"] == "cognito-refresh-token"
        assert body["expires_in"] == 3600
        assert body["token_type"] == "Bearer"

    def test_code_is_single_use(self, code_table, broker_user_flow):
        """A second redemption of the same code fails."""
        import handler

        code = self._issue_code(handler)
        assert self._exchange(handler, code)["statusCode"] == 200

        replay = self._exchange(handler, code)
        assert replay["statusCode"] == 400
        assert json.loads(replay["body"])["error"] == "invalid_code"

    def test_consumes_via_atomic_delete(self, code_table, broker_user_flow):
        """The code is consumed by delete_item(ALL_OLD) — no read-then-delete race."""
        import handler

        code = self._issue_code(handler)
        self._exchange(handler, code)

        code_table["ddb"].delete_item.assert_called_once_with(
            TableName="test-auth-codes",
            Key={"code": {"S": code}},
            ReturnValues="ALL_OLD",
        )
        code_table["ddb"].get_item.assert_not_called()

    def test_rejects_mismatched_app_state(self, code_table, broker_user_flow):
        """A code lifted from history is useless without the SPA's nonce."""
        import handler

        code = self._issue_code(handler, "victim-nonce")
        response = self._exchange(handler, code, "attacker-nonce")

        assert response["statusCode"] == 400
        assert json.loads(response["body"])["error"] == "state_mismatch"
        assert "cognito-access-token" not in response["body"]

    def test_rejects_expired_code(self, code_table, broker_user_flow, monkeypatch):
        """expires_at is enforced on read, not left to DynamoDB's lazy TTL."""
        import handler

        code = self._issue_code(handler)
        code_table["rows"][code]["expires_at"] = {"N": str(int(time.time()) - 1)}

        response = self._exchange(handler, code)
        assert response["statusCode"] == 400
        assert json.loads(response["body"])["error"] == "expired_code"

    def test_rejects_unknown_code(self, code_table):
        """An invented code is rejected."""
        import handler

        response = self._exchange(handler, "not-a-real-code")
        assert response["statusCode"] == 400
        assert json.loads(response["body"])["error"] == "invalid_code"

    def test_rejects_missing_code(self, code_table):
        """A body with no code is a 400."""
        import handler

        response = self._exchange(handler, "")
        assert response["statusCode"] == 400
        assert json.loads(response["body"])["error"] == "missing_code"

    def test_rejects_malformed_body(self, code_table):
        """Non-JSON body is a 400, not a 500."""
        import handler

        response = handler.handler(
            {"rawPath": "/exchange", "requestContext": {"http": {"method": "POST"}}, "body": "not json"},
            None,
        )
        assert response["statusCode"] == 400
        assert json.loads(response["body"])["error"] == "invalid_body"

    def test_rejects_non_object_body(self, code_table):
        """Valid JSON that isn't an object must 400, not raise AttributeError."""
        import handler

        response = handler.handler(
            {"rawPath": "/exchange", "requestContext": {"http": {"method": "POST"}}, "body": "[]"},
            None,
        )
        assert response["statusCode"] == 400
        assert json.loads(response["body"])["error"] == "invalid_body"

    def test_accepts_base64_encoded_body(self, code_table, broker_user_flow):
        """API Gateway may deliver the body base64-encoded."""
        import handler

        code = self._issue_code(handler)
        response = handler.handler(
            {
                "rawPath": "/exchange",
                "requestContext": {"http": {"method": "POST"}},
                "body": base64.b64encode(json.dumps({"code": code, "app_state": "spa-nonce-abc"}).encode()).decode(),
                "isBase64Encoded": True,
            },
            None,
        )

        assert response["statusCode"] == 200
        assert json.loads(response["body"])["access_token"] == "cognito-access-token"

    def test_unavailable_without_table(self, mock_secrets):
        """With no table configured the exchange 503s rather than 500s."""
        import handler

        response = handler.handler(
            {"rawPath": "/exchange", "requestContext": {"http": {"method": "POST"}}, "body": json.dumps({"code": "x"})},
            None,
        )
        assert response["statusCode"] == 503
        assert json.loads(response["body"])["error"] == "exchange_unavailable"

    def test_response_is_not_cacheable(self, code_table, broker_user_flow):
        """Token responses must never be cached by a proxy or the browser."""
        import handler

        code = self._issue_code(handler)
        response = self._exchange(handler, code)

        assert response["headers"]["Cache-Control"] == "no-store"


class TestExchangeCors:
    """The SPA (CloudFront) and broker (API Gateway) are different origins."""

    def test_preflight_is_answered(self, mock_secrets):
        """OPTIONS returns 204 with the SPA origin allowed."""
        import handler

        response = handler.handler(
            {"rawPath": "/exchange", "requestContext": {"http": {"method": "OPTIONS"}}},
            None,
        )
        assert response["statusCode"] == 204
        assert response["headers"]["Access-Control-Allow-Origin"] == "https://example.com"
        assert "POST" in response["headers"]["Access-Control-Allow-Methods"]

    def test_origin_is_scoped_not_wildcard(self, mock_secrets):
        """Allow-Origin is the configured frontend, never '*'."""
        import handler

        headers = handler._cors_headers()
        assert headers["Access-Control-Allow-Origin"] == "https://example.com"

    def test_no_allow_credentials(self, mock_secrets):
        """The code travels in the body; no cookie needs to ride along."""
        import handler

        assert "Access-Control-Allow-Credentials" not in handler._cors_headers()


class TestAppStateBinding:
    """The signed-state changes that carry the SPA nonce through GitHub."""

    def test_app_state_round_trips_through_signed_state(self, mock_secrets):
        """A nonce put into state comes back out intact."""
        import handler

        _seed_creds()
        state = handler._generate_state("spa-nonce-abc")
        assert handler._verify_state(state) is True
        assert handler._extract_app_state(state) == "spa-nonce-abc"

    def test_state_without_app_state_still_verifies(self, mock_secrets):
        """3-field state (SPA build predating #4133) is still accepted."""
        import handler

        _seed_creds()
        state = handler._generate_state()
        assert handler._verify_state(state) is True
        assert handler._extract_app_state(state) == ""

    def test_tampered_app_state_fails_verification(self, mock_secrets):
        """Swapping the nonce breaks the signature — it is signed, not passed through."""
        import handler

        _seed_creds()
        nonce, timestamp, app_state, signature = handler._generate_state("victim-nonce").split(".")
        forged = f"{nonce}.{timestamp}.attacker-nonce.{signature}"

        assert handler._verify_state(forged) is False

    def test_start_binds_app_state_from_query(self, mock_secrets):
        """/start folds the SPA's nonce into the state it sends to GitHub."""
        import handler

        response = handler.handler(
            {
                "rawPath": "/start",
                "requestContext": {"http": {"method": "GET"}},
                "queryStringParameters": {"app_state": "spa-nonce-abc"},
            },
            None,
        )
        location = response["headers"]["Location"]
        state = urllib.parse.parse_qs(urllib.parse.urlparse(location).query)["state"][0]

        assert handler._extract_app_state(state) == "spa-nonce-abc"

    def test_start_without_app_state_still_works(self, mock_secrets):
        """An old SPA build sends no app_state; login must not break."""
        import handler

        response = handler.handler(
            {"rawPath": "/start", "requestContext": {"http": {"method": "GET"}}},
            None,
        )
        assert response["statusCode"] == 302
        assert "state=" in response["headers"]["Location"]

    def test_start_ignores_malformed_app_state(self, mock_secrets):
        """A nonce containing the '.' separator is dropped, not signed in."""
        import handler

        response = handler.handler(
            {
                "rawPath": "/start",
                "requestContext": {"http": {"method": "GET"}},
                "queryStringParameters": {"app_state": "evil.forged.fields"},
            },
            None,
        )
        location = response["headers"]["Location"]
        state = urllib.parse.parse_qs(urllib.parse.urlparse(location).query)["state"][0]

        assert handler._extract_app_state(state) == ""
        assert handler._verify_state(state) is True

    def test_app_state_validation_rules(self):
        """Separator, emptiness and length are all rejected."""
        import handler

        assert handler._is_valid_app_state("abc-123_XYZ~") is True
        assert handler._is_valid_app_state("has.dot") is False
        assert handler._is_valid_app_state("") is False
        assert handler._is_valid_app_state("x" * 129) is False


class TestLegacyTransportFallback:
    """Rollout safety: no table yet must degrade to main's behaviour, not a lockout."""

    def test_falls_back_to_url_tokens_without_table(self, mock_secrets, broker_user_flow):
        """AUTH_CODE_TABLE unset → legacy redirect, so login still works mid-deploy."""
        import handler

        _seed_creds()
        assert handler.AUTH_CODE_TABLE == ""

        state = handler._generate_state("spa-nonce-abc")
        location = handler.handler(_login_event(state), None)["headers"]["Location"]

        assert "id_token=cognito-id-token" in location
        assert "source=github_broker" in location


class TestExchangeUnderRestV1EventShape:
    """The broker runs behind the REST (v1) API, so /exchange must work on v1 events.

    Production fronts this Lambda with aws_api_gateway_rest_api and
    /auth/github/{proxy+} (x-amazon-apigateway-any-method + aws_proxy). Those
    events carry `path` and a top-level `httpMethod` and have NO
    `requestContext.http`. The rest of the suite is v2-shaped, which is exactly
    why a v2-only method read passed tests while breaking every real login.
    """

    @staticmethod
    def _v1_event(path: str, method: str, body: str | None = None) -> dict:
        """A REST v1 proxy event — top-level httpMethod, no requestContext.http."""
        event = {
            "path": path,
            "httpMethod": method,
            "requestContext": {
                "resourcePath": "/auth/github/{proxy+}",
                "httpMethod": method,
            },
            "headers": {"Content-Type": "application/json"},
            "queryStringParameters": None,
        }
        if body is not None:
            event["body"] = body
        return event

    def _issue_code(self, handler_mod, app_state="spa-nonce-abc"):
        _seed_creds()
        state = handler_mod._generate_state(app_state)
        location = handler_mod.handler(_login_event(state), None)["headers"]["Location"]
        query = urllib.parse.parse_qs(urllib.parse.urlparse(location).query)
        return query["code"][0]

    def test_preflight_is_answered_on_v1_event(self, mock_secrets):
        """OPTIONS on a v1 event → 204, so the browser lets the POST through."""
        import handler

        response = handler.handler(self._v1_event("/auth/github/exchange", "OPTIONS"), None)

        assert response["statusCode"] == 204
        assert response["headers"]["Access-Control-Allow-Origin"] == "https://example.com"
        assert "POST" in response["headers"]["Access-Control-Allow-Methods"]

    def test_preflight_is_not_routed_into_exchange(self, code_table):
        """Regression: the preflight must not fall through into _handle_exchange.

        The v2-only method read made http_method default to "GET" for every v1
        request, so OPTIONS reached the exchange handler and 400'd on a missing
        code. A non-2xx preflight blocks the POST → every GitHub login fails.
        """
        import handler

        response = handler.handler(self._v1_event("/auth/github/exchange", "OPTIONS"), None)

        assert response["statusCode"] == 204
        assert json.loads(response["body"] or "{}") == {}
        code_table["ddb"].delete_item.assert_not_called()

    def test_exchange_happy_path_on_v1_event(self, code_table, broker_user_flow):
        """POST /exchange on a v1 event returns the tokens in the body."""
        import handler

        code = self._issue_code(handler)
        response = handler.handler(
            self._v1_event(
                "/auth/github/exchange",
                "POST",
                json.dumps({"code": code, "app_state": "spa-nonce-abc"}),
            ),
            None,
        )

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["id_token"] == "cognito-id-token"
        assert body["access_token"] == "cognito-access-token"
        assert body["token_type"] == "Bearer"

    def test_lowercase_method_is_normalised(self, mock_secrets):
        """Method comparison is case-insensitive, so 'options' still preflights."""
        import handler

        response = handler.handler(self._v1_event("/auth/github/exchange", "options"), None)
        assert response["statusCode"] == 204


class TestExchangeRejectsUnboundCodes:
    """A code minted without a nonce has no binding, so it must not be redeemable."""

    def test_code_minted_without_app_state_is_refused(self, code_table, broker_user_flow):
        """sha256("") is public, so an empty-nonce code would be anyone's to redeem."""
        import handler

        _seed_creds()
        # A login that sent no app_state: _is_valid_app_state("") is False, so
        # nothing is signed into the state and the row stores sha256("").
        state = handler._generate_state("")
        location = handler.handler(_login_event(state), None)["headers"]["Location"]
        code = urllib.parse.parse_qs(urllib.parse.urlparse(location).query)["code"][0]

        assert code_table["rows"][code]["app_state_hash"]["S"] == hashlib.sha256(b"").hexdigest()

        response = handler.handler(
            {
                "rawPath": "/exchange",
                "requestContext": {"http": {"method": "POST"}},
                "body": json.dumps({"code": code, "app_state": ""}),
            },
            None,
        )

        assert response["statusCode"] == 400
        assert json.loads(response["body"])["error"] == "state_mismatch"
        assert "cognito-access-token" not in response["body"]
