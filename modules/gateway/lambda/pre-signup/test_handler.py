"""
Unit tests for the Pre Sign-Up Lambda trigger.

Issue #314: GitHub-based authentication across ADP web UIs
"""

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def reset_env(monkeypatch):
    """Reset environment and module-level state for each test."""
    monkeypatch.setenv("ALLOWLIST_MODE", "org")
    monkeypatch.setenv("ALLOWED_ORGS", "my-org")
    monkeypatch.setenv("ALLOWLIST_TABLE", "test-allowlist")
    monkeypatch.setenv("GITHUB_TOKEN_SECRET_ARN", "arn:aws:secretsmanager:us-east-1:123456789012:secret:test")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")

    # Reset module-level cached state
    import handler

    handler._dynamodb = None
    handler._secrets_client = None
    handler._github_token = None


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
    """Tests for ALLOWLIST_MODE=open."""

    def test_open_mode_allows_any_user(self, monkeypatch):
        monkeypatch.setenv("ALLOWLIST_MODE", "open")
        import handler

        handler.ALLOWLIST_MODE = "open"

        event = _make_event()
        result = handler.handler(event, None)
        assert result["response"]["autoConfirmUser"] is True

    def test_open_mode_allows_unknown_user(self, monkeypatch):
        monkeypatch.setenv("ALLOWLIST_MODE", "open")
        import handler

        handler.ALLOWLIST_MODE = "open"

        event = _make_event(username="GitHub_99999", preferred_username="stranger")
        result = handler.handler(event, None)
        assert result["response"]["autoConfirmUser"] is True


class TestOrgMode:
    """Tests for ALLOWLIST_MODE=org."""

    @patch("handler._is_org_member")
    @patch("handler._get_github_token")
    def test_org_mode_allows_member(self, mock_token, mock_is_member, monkeypatch):
        import handler

        handler.ALLOWLIST_MODE = "org"
        handler.ALLOWED_ORGS = "my-org"

        mock_token.return_value = "ghp_testtoken"
        mock_is_member.return_value = True

        event = _make_event()
        result = handler.handler(event, None)
        assert result["response"]["autoConfirmUser"] is True
        mock_is_member.assert_called_once_with("my-org", "testuser", "ghp_testtoken")

    @patch("handler._is_org_member")
    @patch("handler._get_github_token")
    def test_org_mode_denies_non_member(self, mock_token, mock_is_member, monkeypatch):
        import handler

        handler.ALLOWLIST_MODE = "org"
        handler.ALLOWED_ORGS = "my-org"

        mock_token.return_value = "ghp_testtoken"
        mock_is_member.return_value = False

        event = _make_event()
        with pytest.raises(Exception, match="not a member of an allowed organization"):
            handler.handler(event, None)

    @patch("handler._is_org_member")
    @patch("handler._get_github_token")
    def test_org_mode_checks_multiple_orgs(self, mock_token, mock_is_member, monkeypatch):
        import handler

        handler.ALLOWLIST_MODE = "org"
        handler.ALLOWED_ORGS = "org-a, org-b, org-c"

        mock_token.return_value = "ghp_testtoken"
        # Not member of org-a, but member of org-b
        mock_is_member.side_effect = [False, True]

        event = _make_event()
        result = handler.handler(event, None)
        assert result["response"]["autoConfirmUser"] is True
        assert mock_is_member.call_count == 2

    @patch("handler._get_github_token")
    def test_org_mode_denies_when_no_token(self, mock_token, monkeypatch):
        import handler

        handler.ALLOWLIST_MODE = "org"
        handler.ALLOWED_ORGS = "my-org"

        mock_token.return_value = ""

        event = _make_event()
        with pytest.raises(Exception, match="not a member"):
            handler.handler(event, None)

    def test_org_mode_denies_when_no_orgs_configured(self, monkeypatch):
        import handler

        handler.ALLOWLIST_MODE = "org"
        handler.ALLOWED_ORGS = ""

        event = _make_event()
        with pytest.raises(Exception, match="not a member"):
            handler.handler(event, None)


class TestExplicitMode:
    """Tests for ALLOWLIST_MODE=explicit."""

    @patch("handler._get_dynamodb")
    def test_explicit_mode_allows_listed_user(self, mock_ddb, monkeypatch):
        import handler

        handler.ALLOWLIST_MODE = "explicit"
        handler.ALLOWLIST_TABLE = "test-allowlist"

        mock_table = MagicMock()
        mock_table.get_item.return_value = {"Item": {"username": "testuser", "active": True}}
        mock_ddb.return_value.Table.return_value = mock_table

        event = _make_event()
        result = handler.handler(event, None)
        assert result["response"]["autoConfirmUser"] is True

    @patch("handler._get_dynamodb")
    def test_explicit_mode_denies_unlisted_user(self, mock_ddb, monkeypatch):
        import handler

        handler.ALLOWLIST_MODE = "explicit"
        handler.ALLOWLIST_TABLE = "test-allowlist"

        mock_table = MagicMock()
        mock_table.get_item.return_value = {}  # No Item
        mock_ddb.return_value.Table.return_value = mock_table

        event = _make_event()
        with pytest.raises(Exception, match="not on the allowlist"):
            handler.handler(event, None)

    @patch("handler._get_dynamodb")
    def test_explicit_mode_denies_inactive_user(self, mock_ddb, monkeypatch):
        import handler

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

    @patch("handler._get_dynamodb")
    def test_explicit_mode_allows_by_email(self, mock_ddb, monkeypatch):
        import handler

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
        import handler

        handler.ALLOWLIST_MODE = "org"

        event = _make_event(trigger_source="PreSignUp_AdminCreateUser")
        result = handler.handler(event, None)
        # Should not raise and should not set autoConfirmUser
        assert result["response"]["autoConfirmUser"] is False

    def test_sign_up_passes_through(self, monkeypatch):
        import handler

        handler.ALLOWLIST_MODE = "org"

        event = _make_event(trigger_source="PreSignUp_SignUp")
        result = handler.handler(event, None)
        assert result["response"]["autoConfirmUser"] is False


class TestUsernameExtraction:
    """Tests for _extract_github_username helper."""

    def test_uses_preferred_username(self):
        import handler

        result = handler._extract_github_username("GitHub_12345", {"preferred_username": "octocat", "email": "octo@test.com"})
        assert result == "octocat"

    def test_falls_back_to_email_prefix(self):
        import handler

        result = handler._extract_github_username("GitHub_12345", {"email": "octocat@github.com"})
        assert result == "octocat"

    def test_falls_back_to_username_suffix(self):
        import handler

        result = handler._extract_github_username("GitHub_12345", {})
        assert result == "12345"

    def test_handles_plain_username(self):
        import handler

        result = handler._extract_github_username("plainuser", {})
        assert result == "plainuser"


class TestUnknownMode:
    """Tests for misconfigured allowlist mode."""

    def test_unknown_mode_denies(self, monkeypatch):
        import handler

        handler.ALLOWLIST_MODE = "invalid"

        event = _make_event()
        with pytest.raises(Exception, match="misconfiguration"):
            handler.handler(event, None)
