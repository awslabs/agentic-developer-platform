"""Unit tests for github_oauth.py — especially the /user/emails fallback."""

from unittest.mock import MagicMock, patch

import pytest

from github_oauth import GITHUB_USER_EMAILS_URL, get_github_user


def _user_resp(email):
    """Helper: shape a /user response with a given email value."""
    r = MagicMock()
    r.status_code = 200
    r.json.return_value = {
        "id": 20402445,
        "login": "PranavSharma1000",
        "email": email,
        "name": "Pranav",
        "avatar_url": "https://avatars.githubusercontent.com/u/20402445",
    }
    return r


def _emails_resp(payload, status=200):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = payload
    return r


class TestGetGitHubUser:
    def test_email_present_in_user_endpoint_does_not_call_emails(self):
        """Happy path: /user returns email → /user/emails is NOT called."""
        with patch("github_oauth.requests.get") as mock_get:
            mock_get.return_value = _user_resp("public@example.com")
            user = get_github_user("token")
        assert user["email"] == "public@example.com"
        # Exactly one HTTP call (to /user), no /user/emails fallback
        assert mock_get.call_count == 1

    def test_null_email_falls_back_to_user_emails_primary(self):
        """When /user returns email=null, broker queries /user/emails and picks primary verified."""
        with patch("github_oauth.requests.get") as mock_get:
            mock_get.side_effect = [
                _user_resp(None),
                _emails_resp([
                    {"email": "secondary@example.com", "primary": False, "verified": True},
                    {"email": "primary@example.com", "primary": True, "verified": True},
                ]),
            ]
            user = get_github_user("token")
        assert user["email"] == "primary@example.com"
        assert mock_get.call_count == 2
        assert mock_get.call_args_list[1][0][0] == GITHUB_USER_EMAILS_URL

    def test_empty_string_email_falls_back(self):
        """email='' in /user should also trigger the fallback (not just None)."""
        with patch("github_oauth.requests.get") as mock_get:
            mock_get.side_effect = [
                _user_resp(""),
                _emails_resp([{"email": "v@example.com", "primary": True, "verified": True}]),
            ]
            user = get_github_user("token")
        assert user["email"] == "v@example.com"

    def test_no_primary_falls_back_to_any_verified(self):
        """If no email is marked primary, use any verified one."""
        with patch("github_oauth.requests.get") as mock_get:
            mock_get.side_effect = [
                _user_resp(None),
                _emails_resp([
                    {"email": "a@example.com", "primary": False, "verified": False},
                    {"email": "b@example.com", "primary": False, "verified": True},
                ]),
            ]
            user = get_github_user("token")
        assert user["email"] == "b@example.com"

    def test_emails_endpoint_403_returns_empty_email(self):
        """Missing user:email scope → /user/emails returns 403 → graceful empty string."""
        with patch("github_oauth.requests.get") as mock_get:
            mock_get.side_effect = [
                _user_resp(None),
                _emails_resp({"message": "Forbidden"}, status=403),
            ]
            user = get_github_user("token")
        assert user["email"] == ""
        assert user["id"] == 20402445  # Other fields still populated

    def test_emails_endpoint_raises_returns_empty_email(self):
        """Network error during /user/emails is swallowed and treated as no email."""
        class _Boom(Exception):
            pass

        with patch("github_oauth.requests.get") as mock_get:
            mock_get.side_effect = [_user_resp(None), _Boom("network blip")]
            user = get_github_user("token")
        assert user["email"] == ""

    def test_missing_id_raises(self):
        """A /user response without id is still rejected, unchanged from before."""
        r = MagicMock()
        r.status_code = 200
        r.json.return_value = {"login": "x"}
        with patch("github_oauth.requests.get", return_value=r):
            with pytest.raises(ValueError, match="missing 'id'"):
                get_github_user("token")
