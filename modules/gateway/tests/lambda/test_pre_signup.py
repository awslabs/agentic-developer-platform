"""
Unit tests for the Pre Sign-Up Lambda trigger.

Issue #314: GitHub-based authentication across ADP web UIs

Issue #4848: these tests lived next to the handler in
``modules/gateway/lambda/pre-signup/`` and were never collected -- pytest's
``testpaths = ["tests"]`` does not reach there -- while the copy Terraform
actually packaged lived in the cognito Terraform module and had no tests at
all. Moved here so the deployed artifact is the tested one. Loaded through
``_handler_loader`` rather than a bare ``import handler`` because several
lambdas ship a top-level module literally named ``handler``; the first one
imported would otherwise win ``sys.modules`` for the whole pytest process.
"""

from unittest.mock import MagicMock, patch

import pytest

from ._handler_loader import handler_module_name, load_handler

# Unique module name for the pre-signup handler -- used both to load it and as
# the patch target, so it never collides with other lambdas' ``handler``.
_PRE_SIGNUP = handler_module_name("pre-signup")


@pytest.fixture(autouse=True)
def reset_env(monkeypatch):
    """Reset environment and module-level state for each test."""
    monkeypatch.setenv("ALLOWLIST_MODE", "org")
    monkeypatch.setenv("ALLOWED_ORGS", "my-org")
    monkeypatch.setenv("ALLOWLIST_TABLE", "test-allowlist")
    monkeypatch.setenv("GITHUB_TOKEN_SECRET_ARN", "arn:aws:secretsmanager:us-east-1:123456789012:secret:test")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")

    # Reset module-level cached state
    handler = load_handler("pre-signup")

    handler._dynamodb = None
    handler._secrets_client = None
    handler._github_token = None
    # Issue #4844: tests mutate these module-level globals directly, so they must
    # be restored or one test's mode/flag leaks into the next.
    handler.ALLOWLIST_MODE = "org"
    handler.ALLOWED_ORGS = "my-org"
    handler.ALLOW_OPEN_SIGNUP = False


def _make_event(
    trigger_source="PreSignUp_ExternalProvider",
    username="GitHub_12345",
    email="testuser@example.com",
    preferred_username="testuser",
):
    """Create a minimal Cognito Pre Sign-Up event."""
    return {
        "version": "1",
        "triggerSource": trigger_source,
        "region": "us-east-1",
        "userPoolId": "us-east-1_testpool",
        "userName": username,
        "callerContext": {
            "awsSdkVersion": "aws-sdk-unknown-unknown",
            "clientId": "test-client-id",
        },
        "request": {
            "userAttributes": {
                "email": email,
                "preferred_username": preferred_username,
            }
        },
        "response": {
            "autoConfirmUser": False,
            "autoVerifyEmail": False,
            "autoVerifyPhone": False,
        },
    }


class TestOpenMode:
    """Tests for ALLOWLIST_MODE=open.

    Issue #4844 aligned this copy with the broker: 'open' now requires
    ``ALLOW_OPEN_SIGNUP=true``, which the broker has required since #3986. Before
    that, this copy auto-confirmed everyone on the mode alone — a verified
    divergence between two copies of one rule. Terraform passes the flag from the
    same root variable that feeds the broker, so no environment's behaviour moves.
    """

    def test_open_mode_allows_any_user_with_the_flag(self, monkeypatch):
        monkeypatch.setenv("ALLOWLIST_MODE", "open")
        handler = load_handler("pre-signup")

        handler.ALLOWLIST_MODE = "open"
        handler.ALLOW_OPEN_SIGNUP = True

        event = _make_event()
        result = handler.handler(event, None)
        assert result["response"]["autoConfirmUser"] is True

    def test_open_mode_allows_unknown_user_with_the_flag(self, monkeypatch):
        monkeypatch.setenv("ALLOWLIST_MODE", "open")
        handler = load_handler("pre-signup")

        handler.ALLOWLIST_MODE = "open"
        handler.ALLOW_OPEN_SIGNUP = True

        event = _make_event(username="GitHub_99999", preferred_username="stranger")
        result = handler.handler(event, None)
        assert result["response"]["autoConfirmUser"] is True

    def test_open_mode_denies_without_the_flag(self, monkeypatch):
        """#4844: the mode alone is now a misconfiguration, not an instruction.

        Matches the broker's behaviour exactly — an operator who sets 'open' but
        not the acknowledgement flag has not opted into unauthenticated signup.
        """
        monkeypatch.setenv("ALLOWLIST_MODE", "open")
        handler = load_handler("pre-signup")

        handler.ALLOWLIST_MODE = "open"
        handler.ALLOW_OPEN_SIGNUP = False

        event = _make_event()
        with pytest.raises(Exception, match="misconfiguration"):
            handler.handler(event, None)


class TestPlatformMode:
    """Issue #4844: ALLOWLIST_MODE=platform in the pre-signup copy.

    PARITY, not enforcement. This trigger is not the live gate for GitHub sign-in
    (``admin_create_user`` does not fire ``PreSignUp_ExternalProvider``, and
    ``PreSignUp_AdminCreateUser`` is passed through by design), so these tests
    guard against future drift rather than proving anything about live sign-in.
    The behavioural gate lives in the broker's suite.
    """

    @staticmethod
    def _reader(verdict=None, exc=None):
        fake = MagicMock()
        fake.ELIGIBLE = "eligible"
        fake.NOT_ELIGIBLE = "not_eligible"
        fake.UNAVAILABLE = "unavailable"
        if exc is not None:
            fake.check_platform_membership.side_effect = exc
        else:
            fake.check_platform_membership.return_value = verdict
        return fake

    def test_platform_mode_allows_membership_holder(self):
        handler = load_handler("pre-signup")
        handler.ALLOWLIST_MODE = "platform"

        reader = self._reader("eligible")
        with patch.dict("sys.modules", {"membership_eligibility": reader}):
            result = handler.handler(_make_event(), None)

        assert result["response"]["autoConfirmUser"] is True
        # Keyed on the numeric id from the userName, not the login.
        reader.check_platform_membership.assert_called_once_with("12345")

    def test_platform_mode_denies_without_membership(self):
        handler = load_handler("pre-signup")
        handler.ALLOWLIST_MODE = "platform"

        reader = self._reader("not_eligible")
        with patch.dict("sys.modules", {"membership_eligibility": reader}):
            with pytest.raises(Exception, match="not a member of any organization on this platform"):
                handler.handler(_make_event(), None)

    def test_platform_mode_denies_when_source_unavailable(self):
        """Fail-closed: "could not check" must not fall through to a grant."""
        handler = load_handler("pre-signup")
        handler.ALLOWLIST_MODE = "platform"

        reader = self._reader("unavailable")
        with patch.dict("sys.modules", {"membership_eligibility": reader}):
            with pytest.raises(Exception, match="not a member of any organization on this platform"):
                handler.handler(_make_event(), None)

    def test_platform_mode_denies_when_read_raises(self):
        handler = load_handler("pre-signup")
        handler.ALLOWLIST_MODE = "platform"

        reader = self._reader(exc=RuntimeError("ddb down"))
        with patch.dict("sys.modules", {"membership_eligibility": reader}):
            with pytest.raises(Exception, match="not a member of any organization on this platform"):
                handler.handler(_make_event(), None)

    def test_platform_mode_denies_when_username_has_no_numeric_id(self):
        """No id to look up ⇒ deny, never guess from the login."""
        handler = load_handler("pre-signup")
        handler.ALLOWLIST_MODE = "platform"

        reader = self._reader("eligible")
        event = _make_event(username="no-id-here")
        with patch.dict("sys.modules", {"membership_eligibility": reader}):
            with pytest.raises(Exception, match="not a member of any organization on this platform"):
                handler.handler(event, None)
        reader.check_platform_membership.assert_not_called()

    def test_platform_mode_does_not_gate_admin_create_user(self):
        """The pass-through this trigger must keep.

        ``PreSignUp_AdminCreateUser`` is how broker-provisioned users arrive; the
        broker has already decided. A mode that started gating it would deny every
        broker-provisioned login — the outage this story most risks causing.
        """
        handler = load_handler("pre-signup")
        handler.ALLOWLIST_MODE = "platform"

        reader = self._reader("not_eligible")
        event = _make_event(trigger_source="PreSignUp_AdminCreateUser")
        with patch.dict("sys.modules", {"membership_eligibility": reader}):
            result = handler.handler(event, None)

        assert result is event
        reader.check_platform_membership.assert_not_called()


class TestOrgMode:
    """Tests for ALLOWLIST_MODE=org."""

    @patch(f"{_PRE_SIGNUP}._is_org_member")
    @patch(f"{_PRE_SIGNUP}._get_github_token")
    def test_org_mode_allows_member(self, mock_token, mock_is_member, monkeypatch):
        handler = load_handler("pre-signup")

        handler.ALLOWLIST_MODE = "org"
        handler.ALLOWED_ORGS = "my-org"

        mock_token.return_value = "ghp_testtoken"
        mock_is_member.return_value = True

        event = _make_event()
        result = handler.handler(event, None)
        assert result["response"]["autoConfirmUser"] is True
        mock_is_member.assert_called_once_with("my-org", "testuser", "ghp_testtoken")

    @patch(f"{_PRE_SIGNUP}._is_org_member")
    @patch(f"{_PRE_SIGNUP}._get_github_token")
    def test_org_mode_denies_non_member(self, mock_token, mock_is_member, monkeypatch):
        handler = load_handler("pre-signup")

        handler.ALLOWLIST_MODE = "org"
        handler.ALLOWED_ORGS = "my-org"

        mock_token.return_value = "ghp_testtoken"
        mock_is_member.return_value = False

        event = _make_event()
        with pytest.raises(Exception, match="not a member of an allowed organization"):
            handler.handler(event, None)

    @patch(f"{_PRE_SIGNUP}._is_org_member")
    @patch(f"{_PRE_SIGNUP}._get_github_token")
    def test_org_mode_checks_multiple_orgs(self, mock_token, mock_is_member, monkeypatch):
        handler = load_handler("pre-signup")

        handler.ALLOWLIST_MODE = "org"
        handler.ALLOWED_ORGS = "org-a, org-b, org-c"

        mock_token.return_value = "ghp_testtoken"
        # Not member of org-a, but member of org-b
        mock_is_member.side_effect = [False, True]

        event = _make_event()
        result = handler.handler(event, None)
        assert result["response"]["autoConfirmUser"] is True
        assert mock_is_member.call_count == 2

    @patch(f"{_PRE_SIGNUP}._get_github_token")
    def test_org_mode_denies_when_no_token(self, mock_token, monkeypatch):
        handler = load_handler("pre-signup")

        handler.ALLOWLIST_MODE = "org"
        handler.ALLOWED_ORGS = "my-org"

        mock_token.return_value = ""

        event = _make_event()
        with pytest.raises(Exception, match="not a member"):
            handler.handler(event, None)

    def test_org_mode_denies_when_no_orgs_configured(self, monkeypatch):
        handler = load_handler("pre-signup")

        handler.ALLOWLIST_MODE = "org"
        handler.ALLOWED_ORGS = ""

        event = _make_event()
        with pytest.raises(Exception, match="not a member"):
            handler.handler(event, None)


class TestExplicitMode:
    """Tests for ALLOWLIST_MODE=explicit."""

    @patch(f"{_PRE_SIGNUP}._get_dynamodb")
    def test_explicit_mode_allows_listed_user(self, mock_ddb, monkeypatch):
        handler = load_handler("pre-signup")

        handler.ALLOWLIST_MODE = "explicit"
        handler.ALLOWLIST_TABLE = "test-allowlist"

        mock_table = MagicMock()
        mock_table.get_item.return_value = {"Item": {"username": "testuser", "active": True}}
        mock_ddb.return_value.Table.return_value = mock_table

        event = _make_event()
        result = handler.handler(event, None)
        assert result["response"]["autoConfirmUser"] is True

    @patch(f"{_PRE_SIGNUP}._get_dynamodb")
    def test_explicit_mode_denies_unlisted_user(self, mock_ddb, monkeypatch):
        handler = load_handler("pre-signup")

        handler.ALLOWLIST_MODE = "explicit"
        handler.ALLOWLIST_TABLE = "test-allowlist"

        mock_table = MagicMock()
        mock_table.get_item.return_value = {}  # No Item
        mock_ddb.return_value.Table.return_value = mock_table

        event = _make_event()
        with pytest.raises(Exception, match="not on the allowlist"):
            handler.handler(event, None)

    @patch(f"{_PRE_SIGNUP}._get_dynamodb")
    def test_explicit_mode_denies_inactive_user(self, mock_ddb, monkeypatch):
        handler = load_handler("pre-signup")

        handler.ALLOWLIST_MODE = "explicit"
        handler.ALLOWLIST_TABLE = "test-allowlist"

        mock_table = MagicMock()
        mock_table.get_item.side_effect = [
            {"Item": {"username": "testuser", "active": False}},  # username lookup
            {},  # email lookup
        ]
        mock_ddb.return_value.Table.return_value = mock_table

        event = _make_event()
        with pytest.raises(Exception, match="not on the allowlist"):
            handler.handler(event, None)

    @patch(f"{_PRE_SIGNUP}._get_dynamodb")
    def test_explicit_mode_allows_by_email(self, mock_ddb, monkeypatch):
        handler = load_handler("pre-signup")

        handler.ALLOWLIST_MODE = "explicit"
        handler.ALLOWLIST_TABLE = "test-allowlist"

        mock_table = MagicMock()
        mock_table.get_item.side_effect = [
            {},  # username lookup fails
            {"Item": {"username": "testuser@example.com", "active": True}},  # email lookup
        ]
        mock_ddb.return_value.Table.return_value = mock_table

        event = _make_event()
        result = handler.handler(event, None)
        assert result["response"]["autoConfirmUser"] is True


class TestNonExternalProvider:
    """Tests for non-external-provider triggers (should pass through)."""

    def test_admin_create_user_passes_through(self, monkeypatch):
        handler = load_handler("pre-signup")

        handler.ALLOWLIST_MODE = "org"

        event = _make_event(trigger_source="PreSignUp_AdminCreateUser")
        result = handler.handler(event, None)
        # Should not raise and should not set autoConfirmUser
        assert result["response"]["autoConfirmUser"] is False

    def test_sign_up_passes_through(self, monkeypatch):
        handler = load_handler("pre-signup")

        handler.ALLOWLIST_MODE = "org"

        event = _make_event(trigger_source="PreSignUp_SignUp")
        result = handler.handler(event, None)
        assert result["response"]["autoConfirmUser"] is False


class TestUsernameExtraction:
    """Tests for _extract_github_username helper."""

    def test_uses_preferred_username(self):
        handler = load_handler("pre-signup")

        result = handler._extract_github_username("GitHub_12345", {"preferred_username": "octocat", "email": "octo@test.com"})
        assert result == "octocat"

    def test_falls_back_to_email_prefix(self):
        handler = load_handler("pre-signup")

        result = handler._extract_github_username("GitHub_12345", {"email": "octocat@github.com"})
        assert result == "octocat"

    def test_falls_back_to_username_suffix(self):
        handler = load_handler("pre-signup")

        result = handler._extract_github_username("GitHub_12345", {})
        assert result == "12345"

    def test_handles_plain_username(self):
        handler = load_handler("pre-signup")

        result = handler._extract_github_username("plainuser", {})
        assert result == "plainuser"


class TestUnknownMode:
    """Tests for misconfigured allowlist mode."""

    def test_unknown_mode_denies(self, monkeypatch):
        handler = load_handler("pre-signup")

        handler.ALLOWLIST_MODE = "invalid"

        event = _make_event()
        with pytest.raises(Exception, match="misconfiguration"):
            handler.handler(event, None)
