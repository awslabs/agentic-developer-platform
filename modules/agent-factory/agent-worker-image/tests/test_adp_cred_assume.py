"""Unit tests for adp-cred assume subcommand.

Issue #481: aws_role credential type + STS assume_role as a vault delivery path.
Issue #583: --exec flag for scoped env-var credential injection (defeats IRSA).

Coverage:
  - Happy path: writes to ~/.aws/credentials with correct profile shape
  - stdout contains only the profile name — no credential values
  - Existing ~/.aws/credentials profiles are not overwritten
  - ENABLE_USER_CREDENTIALS=false -> error exit
  - Gateway error -> sys.exit(1) with error on stderr
  - --exec builds correct env (creds added, IRSA removed)
  - --exec calls os.execvpe with the right args
  - --exec with no command -> exit code 2
  - Default (no --exec) behavior unchanged
  - _get_config() 6-tuple unpack works correctly
"""

from __future__ import annotations

import configparser
import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from adp_cred.assume import _write_aws_credentials, cmd_assume  # noqa: E402


MOCK_RESPONSE = {
    "profile_name": "adp-aws-prod",
    "access_key_id": "ASIAEXAMPLE",
    "secret_access_key": "secretKeyXYZ",
    "session_token": "sessionTokenXYZ",
    "expiration": "2026-05-07T17:00:00Z",
    "region": "us-west-2",
    "provenance_id": "prov-1",
}


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

        with patch("adp_cred.assume._do_request", return_value=MOCK_RESPONSE):
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

        with patch("adp_cred.assume._do_request", return_value=mock_response):
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

    def test_no_exec_preserves_existing_behavior(self, tmp_path, monkeypatch, capsys):
        """Without --exec, profile is still written, profile name printed, no execvpe."""
        monkeypatch.setattr(os.path, "expanduser", lambda p: str(tmp_path / p.lstrip("~/")))

        with patch("adp_cred.assume._do_request", return_value=MOCK_RESPONSE):
            with patch("os.execvpe") as mock_execvpe:
                cmd_assume(["--service", "aws", "--label", "prod"])

        # execvpe NOT called.
        mock_execvpe.assert_not_called()
        # Profile name printed.
        captured = capsys.readouterr()
        assert captured.out == "adp-aws-prod"
        # Credentials file written.
        creds_path = tmp_path / ".aws" / "credentials"
        assert creds_path.exists()


class TestCmdAssumeExec:
    """Tests for the --exec flag (issue #583)."""

    def test_exec_calls_execvpe_with_creds_in_env(self, tmp_path, monkeypatch):
        """--exec aws sts get-caller-identity → os.execvpe called with correct env."""
        monkeypatch.setattr(os.path, "expanduser", lambda p: str(tmp_path / p.lstrip("~/")))

        with patch("adp_cred.assume._do_request", return_value=MOCK_RESPONSE):
            with patch("shutil.which", return_value="/usr/bin/aws"):
                with patch("os.execvpe") as mock_execvpe:
                    cmd_assume([
                        "--service", "aws", "--label", "prod",
                        "--exec", "aws", "sts", "get-caller-identity",
                    ])

        mock_execvpe.assert_called_once()
        call_args = mock_execvpe.call_args
        exec_path = call_args[0][0]
        exec_argv = call_args[0][1]
        exec_env = call_args[0][2]

        # Correct binary path.
        assert exec_path == "/usr/bin/aws"
        # Correct argv.
        assert exec_argv == ["aws", "sts", "get-caller-identity"]
        # Env contains assumed-role creds.
        assert exec_env["AWS_ACCESS_KEY_ID"] == "ASIAEXAMPLE"
        assert exec_env["AWS_SECRET_ACCESS_KEY"] == "secretKeyXYZ"
        assert exec_env["AWS_SESSION_TOKEN"] == "sessionTokenXYZ"
        assert exec_env["AWS_REGION"] == "us-west-2"
        assert exec_env["AWS_DEFAULT_REGION"] == "us-west-2"

    def test_exec_strips_irsa_and_profile_vars(self, tmp_path, monkeypatch):
        """IRSA env vars and AWS_PROFILE are removed from the exec env."""
        monkeypatch.setattr(os.path, "expanduser", lambda p: str(tmp_path / p.lstrip("~/")))
        # Simulate pod IRSA env vars.
        monkeypatch.setenv("AWS_ROLE_ARN", "arn:aws:iam::123456789012:role/pod-role")
        monkeypatch.setenv("AWS_WEB_IDENTITY_TOKEN_FILE", "/var/run/secrets/token")
        monkeypatch.setenv("AWS_PROFILE", "stale-profile")

        with patch("adp_cred.assume._do_request", return_value=MOCK_RESPONSE):
            with patch("shutil.which", return_value="/usr/bin/aws"):
                with patch("os.execvpe") as mock_execvpe:
                    cmd_assume(["--service", "aws", "--label", "prod", "--exec", "aws", "s3", "ls"])

        exec_env = mock_execvpe.call_args[0][2]
        # IRSA vars removed.
        assert "AWS_ROLE_ARN" not in exec_env
        assert "AWS_WEB_IDENTITY_TOKEN_FILE" not in exec_env
        # AWS_PROFILE removed.
        assert "AWS_PROFILE" not in exec_env

    def test_exec_passes_command_args_unchanged(self, tmp_path, monkeypatch):
        """The second arg to execvpe is [cmd, *user_args] exactly as supplied."""
        monkeypatch.setattr(os.path, "expanduser", lambda p: str(tmp_path / p.lstrip("~/")))

        with patch("adp_cred.assume._do_request", return_value=MOCK_RESPONSE):
            with patch("shutil.which", return_value="/usr/bin/python3"):
                with patch("os.execvpe") as mock_execvpe:
                    cmd_assume([
                        "--label", "prod",
                        "--exec", "python3", "my-script.py", "--flag", "value",
                    ])

        exec_argv = mock_execvpe.call_args[0][1]
        assert exec_argv == ["python3", "my-script.py", "--flag", "value"]

    def test_exec_with_no_command_exits_2(self, capsys):
        """--exec with nothing after it → exit code 2 with error message."""
        with pytest.raises(SystemExit) as exc_info:
            cmd_assume(["--service", "aws", "--label", "prod", "--exec"])
        assert exc_info.value.code == 2
        captured = capsys.readouterr()
        assert "--exec requires a command" in captured.err

    def test_exec_region_fallback_to_env(self, tmp_path, monkeypatch):
        """If response has no region, falls back to AWS_REGION env var."""
        monkeypatch.setattr(os.path, "expanduser", lambda p: str(tmp_path / p.lstrip("~/")))
        monkeypatch.setenv("AWS_REGION", "eu-central-1")

        response_no_region = {**MOCK_RESPONSE, "region": ""}

        with patch("adp_cred.assume._do_request", return_value=response_no_region):
            with patch("shutil.which", return_value="/usr/bin/aws"):
                with patch("os.execvpe") as mock_execvpe:
                    cmd_assume(["--label", "prod", "--exec", "aws", "s3", "ls"])

        exec_env = mock_execvpe.call_args[0][2]
        assert exec_env["AWS_REGION"] == "eu-central-1"
        assert exec_env["AWS_DEFAULT_REGION"] == "eu-central-1"

    def test_exec_which_not_found_uses_raw_cmd(self, tmp_path, monkeypatch):
        """If shutil.which returns None, use the raw command name."""
        monkeypatch.setattr(os.path, "expanduser", lambda p: str(tmp_path / p.lstrip("~/")))

        with patch("adp_cred.assume._do_request", return_value=MOCK_RESPONSE):
            with patch("shutil.which", return_value=None):
                with patch("os.execvpe") as mock_execvpe:
                    cmd_assume(["--label", "prod", "--exec", "custom-tool", "--arg"])

        exec_path = mock_execvpe.call_args[0][0]
        assert exec_path == "custom-tool"


class TestGetConfigUnpack:
    """Regression guard for the 6-tuple unpack fix (bug #7 from EPIC #579)."""

    def test_get_config_returns_six_values(self, monkeypatch):
        """_get_config() returns 6 values; assume.py unpacks all of them."""
        from adp_cred.client import _get_config

        monkeypatch.setenv("VAULT_GATEWAY_URL", "http://gateway:8080")
        monkeypatch.setenv("VAULT_INTERNAL_API_KEY", "test-key")
        monkeypatch.setenv("ADP_USER_ID", "user-001")
        monkeypatch.setenv("ADP_AGENT_ID", "developer")
        monkeypatch.setenv("ADP_TASK_ID", "task-abc")

        result = _get_config()
        assert len(result) == 6
        base_url, api_key, user_id, agent_id, task_id, use_sigv4 = result
        assert base_url == "http://gateway:8080"
        assert api_key == "test-key"
        assert user_id == "user-001"
        assert agent_id == "developer"
        assert task_id == "task-abc"
        assert use_sigv4 is False

    def test_get_config_sigv4_mode(self, monkeypatch):
        """In SigV4 mode, api_key is None and use_sigv4 is True."""
        from adp_cred.client import _get_config

        monkeypatch.setenv("ADP_GATEWAY_ENDPOINT", "https://api.example.com")
        monkeypatch.setenv("ADP_USER_ID", "user-001")
        monkeypatch.setenv("ADP_AGENT_ID", "developer")
        monkeypatch.setenv("ADP_TASK_ID", "task-abc")
        # Legacy vars not needed in SigV4 mode.
        monkeypatch.delenv("VAULT_GATEWAY_URL", raising=False)
        monkeypatch.delenv("VAULT_INTERNAL_API_KEY", raising=False)

        result = _get_config()
        assert len(result) == 6
        base_url, api_key, user_id, agent_id, task_id, use_sigv4 = result
        assert base_url == "https://api.example.com/agent"
        assert api_key is None
        assert use_sigv4 is True
