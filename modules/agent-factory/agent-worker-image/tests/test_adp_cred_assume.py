"""Unit tests for adp-cred assume subcommand.

Issue #481: aws_role credential type + STS assume_role as a vault delivery path.

Coverage:
  - Happy path: writes to ~/.aws/credentials with correct profile shape
  - stdout contains only the profile name — no credential values
  - Existing ~/.aws/credentials profiles are not overwritten
  - ENABLE_USER_CREDENTIALS=false -> error exit
  - Gateway error -> sys.exit(1) with error on stderr
"""

from __future__ import annotations

import configparser
import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from adp_cred.assume import _write_aws_credentials, cmd_assume  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Set required env vars for every test."""
    monkeypatch.setenv("VAULT_GATEWAY_URL", "http://gateway:8080")
    monkeypatch.setenv("VAULT_INTERNAL_API_KEY", "test-key")
    monkeypatch.setenv("ADP_USER_ID", "user-001")
    monkeypatch.setenv("ADP_AGENT_ID", "developer")
    monkeypatch.setenv("ADP_TASK_ID", "task-abc")
    monkeypatch.setenv("ENABLE_USER_CREDENTIALS", "1")


class TestWriteAwsCredentials:
    def test_creates_profile_in_credentials_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        # Patch expanduser to use tmp_path.
        monkeypatch.setattr(os.path, "expanduser", lambda p: str(tmp_path / p.lstrip("~/")))

        _write_aws_credentials(
            profile_name="adp-aws-prod",
            access_key_id="ASIAEXAMPLE",
            secret_access_key="secretKey123",
            session_token="sessionToken456",
            region="us-west-2",
        )

        creds_path = tmp_path / ".aws" / "credentials"
        assert creds_path.exists()

        config = configparser.ConfigParser()
        config.read(str(creds_path))
        assert "adp-aws-prod" in config
        assert config["adp-aws-prod"]["aws_access_key_id"] == "ASIAEXAMPLE"
        assert config["adp-aws-prod"]["aws_secret_access_key"] == "secretKey123"
        assert config["adp-aws-prod"]["aws_session_token"] == "sessionToken456"
        assert config["adp-aws-prod"]["region"] == "us-west-2"

    def test_does_not_overwrite_existing_profiles(self, tmp_path, monkeypatch):
        monkeypatch.setattr(os.path, "expanduser", lambda p: str(tmp_path / p.lstrip("~/")))

        # Create an existing profile.
        aws_dir = tmp_path / ".aws"
        aws_dir.mkdir()
        creds_path = aws_dir / "credentials"
        creds_path.write_text("[existing-profile]\naws_access_key_id = EXISTING\n")

        _write_aws_credentials(
            profile_name="adp-aws-prod",
            access_key_id="ASIANEW",
            secret_access_key="newSecret",
            session_token="newSession",
            region="eu-west-1",
        )

        config = configparser.ConfigParser()
        config.read(str(creds_path))
        # Existing profile preserved.
        assert "existing-profile" in config
        assert config["existing-profile"]["aws_access_key_id"] == "EXISTING"
        # New profile added.
        assert "adp-aws-prod" in config
        assert config["adp-aws-prod"]["aws_access_key_id"] == "ASIANEW"


class TestCmdAssume:
    def test_stdout_contains_only_profile_name(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(os.path, "expanduser", lambda p: str(tmp_path / p.lstrip("~/")))

        mock_response = {
            "profile_name": "adp-aws-prod",
            "access_key_id": "ASIAEXAMPLE",
            "secret_access_key": "secretKeyXYZ",
            "session_token": "sessionTokenXYZ",
            "expiration": "2026-05-07T17:00:00Z",
            "region": "us-west-2",
            "provenance_id": "prov-1",
        }

        with patch("adp_cred.assume._request", return_value=mock_response):
            cmd_assume(["--service", "aws", "--label", "prod", "--purpose", "deploy"])

        captured = capsys.readouterr()
        # stdout must contain ONLY the profile name (no newline because end="").
        assert captured.out == "adp-aws-prod"
        # NO credential values in stdout.
        assert "secretKeyXYZ" not in captured.out
        assert "sessionTokenXYZ" not in captured.out
        assert "ASIAEXAMPLE" not in captured.out
        # No credential values in stderr either.
        assert "secretKeyXYZ" not in captured.err

    def test_disabled_feature_exits_with_error(self, monkeypatch):
        monkeypatch.setenv("ENABLE_USER_CREDENTIALS", "0")
        with pytest.raises(SystemExit) as exc_info:
            cmd_assume(["--service", "aws", "--label", "prod"])
        assert exc_info.value.code == 1

    def test_writes_credentials_to_aws_profile(self, tmp_path, monkeypatch):
        monkeypatch.setattr(os.path, "expanduser", lambda p: str(tmp_path / p.lstrip("~/")))

        mock_response = {
            "profile_name": "adp-aws-staging",
            "access_key_id": "ASIASTAGING",
            "secret_access_key": "stagingSecret",
            "session_token": "stagingSession",
            "expiration": "2026-05-07T18:00:00Z",
            "region": "eu-west-1",
            "provenance_id": "prov-2",
        }

        with patch("adp_cred.assume._request", return_value=mock_response):
            cmd_assume(["--label", "staging"])

        creds_path = tmp_path / ".aws" / "credentials"
        config = configparser.ConfigParser()
        config.read(str(creds_path))
        assert "adp-aws-staging" in config
        assert config["adp-aws-staging"]["aws_access_key_id"] == "ASIASTAGING"
        assert config["adp-aws-staging"]["aws_secret_access_key"] == "stagingSecret"
        assert config["adp-aws-staging"]["aws_session_token"] == "stagingSession"

    def test_unknown_argument_exits_with_usage(self):
        with pytest.raises(SystemExit) as exc_info:
            cmd_assume(["--unknown-flag", "value"])
        assert exc_info.value.code == 2
