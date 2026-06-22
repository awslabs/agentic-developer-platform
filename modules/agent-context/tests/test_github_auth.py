"""
Unit tests for github_auth.py — the shared GitHub App token minting helper.

Covers:
- Successful token mint (subprocess returns 0)
- Failure modes: subprocess non-zero, timeout, missing secrets
- Environment variables set correctly on success
- Environment NOT modified on failure
"""

from __future__ import annotations

import os
import subprocess
import sys
from unittest.mock import MagicMock, patch

# We test github_auth in isolation by patching config.settings and subprocess.run.
# The module is at modules/agent-context/images/ingestion/github_auth.py.
# Add the ingestion directory to sys.path for import.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "images", "ingestion"))


class TestMintGithubToken:
    """Verify mint_github_token() behavior."""

    @patch("subprocess.run")
    @patch("github_auth.settings")
    def test_successful_mint_sets_git_askpass(self, mock_settings, mock_run):
        """On success, GIT_ASKPASS and GIT_TERMINAL_PROMPT are set in os.environ."""
        mock_settings.github_app_id_secret = "adp/gh-app-ops-id"
        mock_settings.github_app_key_secret = "adp/gh-app-ops-key"
        mock_settings.github_app_owner = "aws-e"
        mock_settings.aws_region = "us-east-1"

        mock_run.return_value = MagicMock(returncode=0, stdout=b"", stderr=b"")

        # Clear env before test
        env_backup = {k: os.environ.pop(k, None) for k in ("GIT_ASKPASS", "GIT_TERMINAL_PROMPT")}

        try:
            from github_auth import mint_github_token

            result = mint_github_token()

            assert result is True
            assert os.environ.get("GIT_ASKPASS") == "/app/git-credential-helper.sh"
            assert os.environ.get("GIT_TERMINAL_PROMPT") == "0"
        finally:
            # Restore env
            for k, v in env_backup.items():
                if v is not None:
                    os.environ[k] = v
                else:
                    os.environ.pop(k, None)

    @patch("subprocess.run")
    @patch("github_auth.settings")
    def test_successful_mint_calls_github_app_token_with_owner(self, mock_settings, mock_run):
        """When owner is configured, --owner flag is passed to github-app-token.py."""
        mock_settings.github_app_id_secret = "adp/gh-app-ops-id"
        mock_settings.github_app_key_secret = "adp/gh-app-ops-key"
        mock_settings.github_app_owner = "aws-e"
        mock_settings.aws_region = "us-east-1"

        mock_run.return_value = MagicMock(returncode=0, stdout=b"", stderr=b"")

        from github_auth import mint_github_token

        mint_github_token()

        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert "--owner" in cmd
        assert "aws-e" in cmd
        assert "--app-id-secret" in cmd
        assert "adp/gh-app-ops-id" in cmd
        assert "--app-key-secret" in cmd
        assert "adp/gh-app-ops-key" in cmd
        assert "--output-file" in cmd
        assert "/tmp/github-token" in cmd

    @patch("subprocess.run")
    @patch("github_auth.settings")
    def test_no_owner_omits_flag(self, mock_settings, mock_run):
        """When owner is empty, --owner flag is NOT passed."""
        mock_settings.github_app_id_secret = "adp/gh-app-ops-id"
        mock_settings.github_app_key_secret = "adp/gh-app-ops-key"
        mock_settings.github_app_owner = ""
        mock_settings.aws_region = "us-east-1"

        mock_run.return_value = MagicMock(returncode=0, stdout=b"", stderr=b"")

        from github_auth import mint_github_token

        mint_github_token()

        cmd = mock_run.call_args[0][0]
        assert "--owner" not in cmd

    @patch("subprocess.run")
    @patch("github_auth.settings")
    def test_returns_false_when_subprocess_fails(self, mock_settings, mock_run):
        """When github-app-token.py exits non-zero, return False."""
        mock_settings.github_app_id_secret = "adp/gh-app-ops-id"
        mock_settings.github_app_key_secret = "adp/gh-app-ops-key"
        mock_settings.github_app_owner = ""
        mock_settings.aws_region = "us-east-1"

        mock_run.return_value = MagicMock(returncode=1, stdout=b"", stderr=b"AccessDeniedException")

        from github_auth import mint_github_token

        result = mint_github_token()

        assert result is False

    @patch("subprocess.run")
    @patch("github_auth.settings")
    def test_returns_false_on_timeout(self, mock_settings, mock_run):
        """When github-app-token.py times out, return False."""
        mock_settings.github_app_id_secret = "adp/gh-app-ops-id"
        mock_settings.github_app_key_secret = "adp/gh-app-ops-key"
        mock_settings.github_app_owner = ""
        mock_settings.aws_region = "us-east-1"

        mock_run.side_effect = subprocess.TimeoutExpired(cmd="python", timeout=30)

        from github_auth import mint_github_token

        result = mint_github_token()

        assert result is False

    @patch("github_auth.settings")
    def test_returns_false_when_secrets_not_configured(self, mock_settings):
        """When GitHub App secrets are empty, return False without calling subprocess."""
        mock_settings.github_app_id_secret = ""
        mock_settings.github_app_key_secret = ""

        from github_auth import mint_github_token

        result = mint_github_token()

        assert result is False

    @patch("subprocess.run")
    @patch("github_auth.settings")
    def test_env_not_modified_on_failure(self, mock_settings, mock_run):
        """On failure, GIT_ASKPASS must NOT be set (don't leave stale state)."""
        mock_settings.github_app_id_secret = "adp/gh-app-ops-id"
        mock_settings.github_app_key_secret = "adp/gh-app-ops-key"
        mock_settings.github_app_owner = ""
        mock_settings.aws_region = "us-east-1"

        mock_run.return_value = MagicMock(returncode=1, stdout=b"", stderr=b"error")

        # Clear env
        os.environ.pop("GIT_ASKPASS", None)

        from github_auth import mint_github_token

        mint_github_token()

        assert "GIT_ASKPASS" not in os.environ
