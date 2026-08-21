"""
Unit tests for the GitHub Auth Broker org-membership allowlist.

Issue #3986: an empty allowed_orgs used to return True (allow everyone),
inverting the pre-signup helper's behaviour for the same input.
"""

import urllib.error
from unittest.mock import MagicMock, patch

from allowlist import ALLOWED, DENIED, UNVERIFIED, check_org_membership


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(url="https://api.github.com", code=code, msg="err", hdrs=None, fp=None)


class TestEmptyConfigFailsClosed:
    """The headline fix: no org config must never mean 'allow everyone'."""

    def test_empty_org_list_denies(self):
        assert check_org_membership("anyone", [], "gh-token") == UNVERIFIED

    def test_whitespace_only_orgs_deny(self):
        """A tfvars value of ' , ' parses to an empty list and must still deny."""
        assert check_org_membership("anyone", [" ", "", "  "], "gh-token") == UNVERIFIED

    def test_missing_token_denies(self):
        assert check_org_membership("anyone", ["my-org"], "") == UNVERIFIED


class TestMembership:
    """Membership outcomes for a well-formed config."""

    @patch("allowlist.urllib.request.urlopen")
    def test_member_is_allowed(self, mock_urlopen):
        mock_urlopen.return_value.__enter__.return_value = MagicMock(status=204)
        assert check_org_membership("insider", ["my-org"], "gh-token") == ALLOWED

    @patch("allowlist.urllib.request.urlopen")
    def test_non_member_is_denied(self, mock_urlopen):
        """404 is GitHub's authoritative 'not a member' — a real denial."""
        mock_urlopen.side_effect = _http_error(404)
        assert check_org_membership("outsider", ["my-org"], "gh-token") == DENIED

    @patch("allowlist.urllib.request.urlopen")
    def test_membership_in_any_org_allows(self, mock_urlopen):
        mock_urlopen.side_effect = [_http_error(404), MagicMock(__enter__=MagicMock(return_value=MagicMock(status=204)), __exit__=MagicMock())]
        assert check_org_membership("insider", ["org-a", "org-b"], "gh-token") == ALLOWED

    @patch("allowlist.urllib.request.urlopen")
    def test_orgs_are_stripped(self, mock_urlopen):
        """' my-org ' from a comma-separated tfvars value must be queried clean."""
        mock_urlopen.return_value.__enter__.return_value = MagicMock(status=204)
        check_org_membership("insider", [" my-org "], "gh-token")
        assert "orgs/my-org/members/insider" in mock_urlopen.call_args.args[0].full_url


class TestUnverifiableIsNotDenial:
    """A check we could not complete must be distinguishable from a real denial."""

    @patch("allowlist.urllib.request.urlopen")
    def test_302_is_unverified(self, mock_urlopen):
        """302 means the token lacks read:org — we learned nothing about membership."""
        mock_urlopen.side_effect = _http_error(302)
        assert check_org_membership("someone", ["my-org"], "gh-token") == UNVERIFIED

    @patch("allowlist.urllib.request.urlopen")
    def test_server_error_is_unverified(self, mock_urlopen):
        mock_urlopen.side_effect = _http_error(500)
        assert check_org_membership("someone", ["my-org"], "gh-token") == UNVERIFIED

    @patch("allowlist.urllib.request.urlopen")
    def test_network_error_is_unverified(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.URLError("connection refused")
        assert check_org_membership("someone", ["my-org"], "gh-token") == UNVERIFIED

    @patch("allowlist.urllib.request.urlopen")
    def test_definite_non_member_wins_over_nothing(self, mock_urlopen):
        """All orgs answered 404 → a real denial, not 'unverified'."""
        mock_urlopen.side_effect = [_http_error(404), _http_error(404)]
        assert check_org_membership("outsider", ["org-a", "org-b"], "gh-token") == DENIED

    @patch("allowlist.urllib.request.urlopen")
    def test_partial_failure_is_unverified(self, mock_urlopen):
        """One 404 + one error must not be reported as a confirmed denial."""
        mock_urlopen.side_effect = [_http_error(404), _http_error(500)]
        assert check_org_membership("someone", ["org-a", "org-b"], "gh-token") == UNVERIFIED
