"""Unit tests for adp-trigger CLI.

Issue #2153: agent-to-agent dispatch via POST /agent/trigger.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest

# Ensure the adp_trigger package is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from adp_trigger import client as trigger_client  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Set required env vars for every test."""
    monkeypatch.setenv("ADP_CORRELATION_ID", "corr-abc-123")
    monkeypatch.setenv("ADP_MESSAGE_ID", "msg-xyz-789")
    monkeypatch.setenv("ADP_CHAIN_DEPTH", "1")
    monkeypatch.setenv(
        "ADP_TRIGGER_ENDPOINT",
        "https://api123.execute-api.us-east-1.amazonaws.com/dev/agent/trigger",
    )
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("GITHUB_REPOSITORY", "aws-e/adp")


# ---------------------------------------------------------------------------
# get_config tests
# ---------------------------------------------------------------------------


class TestGetConfig:
    def test_returns_all_values(self):
        config = trigger_client.get_config()
        assert config["correlation_id"] == "corr-abc-123"
        assert config["message_id"] == "msg-xyz-789"
        assert config["chain_depth"] == "1"
        assert (
            config["trigger_endpoint"]
            == "https://api123.execute-api.us-east-1.amazonaws.com/dev/agent/trigger"
        )

    def test_exits_code_2_when_correlation_id_missing(self, monkeypatch):
        monkeypatch.delenv("ADP_CORRELATION_ID")
        with pytest.raises(SystemExit) as exc_info:
            trigger_client.get_config()
        assert exc_info.value.code == 2

    def test_exits_code_2_when_message_id_missing(self, monkeypatch):
        monkeypatch.delenv("ADP_MESSAGE_ID")
        with pytest.raises(SystemExit) as exc_info:
            trigger_client.get_config()
        assert exc_info.value.code == 2

    def test_exits_code_2_when_chain_depth_missing(self, monkeypatch):
        monkeypatch.delenv("ADP_CHAIN_DEPTH")
        with pytest.raises(SystemExit) as exc_info:
            trigger_client.get_config()
        assert exc_info.value.code == 2

    def test_exits_code_2_when_trigger_endpoint_missing(self, monkeypatch):
        monkeypatch.delenv("ADP_TRIGGER_ENDPOINT")
        with pytest.raises(SystemExit) as exc_info:
            trigger_client.get_config()
        assert exc_info.value.code == 2

    def test_error_message_mentions_agent_pod(self, monkeypatch, capsys):
        monkeypatch.delenv("ADP_CORRELATION_ID")
        with pytest.raises(SystemExit):
            trigger_client.get_config()
        captured = capsys.readouterr()
        assert "must run inside an agent pod" in captured.err


# ---------------------------------------------------------------------------
# build_body tests
# ---------------------------------------------------------------------------


class TestBuildBody:
    def test_basic_body_construction(self):
        body = trigger_client.build_body(
            persona="reviewer",
            issue=42,
            repo="aws-e/adp",
        )
        assert body == {
            "correlation_id": "corr-abc-123",
            "parent_invocation_id": "msg-xyz-789",
            "persona": "reviewer",
            "target": {
                "repo": "aws-e/adp",
                "issue": 42,
            },
        }

    def test_body_with_reason(self):
        body = trigger_client.build_body(
            persona="operations",
            issue=100,
            repo="aws-e/adp",
            reason="deploy fix needed",
        )
        assert body["reason"] == "deploy fix needed"
        assert body["persona"] == "operations"
        assert body["target"]["issue"] == 100

    def test_body_without_reason_omits_key(self):
        body = trigger_client.build_body(
            persona="developer",
            issue=1,
            repo="org/repo",
        )
        assert "reason" not in body

    def test_parent_invocation_id_comes_from_message_id(self):
        """parent_invocation_id should map from ADP_MESSAGE_ID."""
        body = trigger_client.build_body(
            persona="reviewer",
            issue=10,
            repo="org/repo",
        )
        assert body["parent_invocation_id"] == "msg-xyz-789"

    def test_correlation_id_comes_from_env(self):
        body = trigger_client.build_body(
            persona="pm",
            issue=5,
            repo="org/repo",
        )
        assert body["correlation_id"] == "corr-abc-123"


# ---------------------------------------------------------------------------
# send_trigger tests
# ---------------------------------------------------------------------------


class TestSendTrigger:
    @patch("botocore.session.get_session")
    @patch("adp_trigger.client.urlopen")
    def test_success_returns_parsed_json(self, mock_urlopen, mock_session):
        """Successful 202 returns parsed response body."""
        # Mock credentials
        mock_creds = MagicMock()
        mock_creds.access_key = "AKIAIOSFODNN7EXAMPLE"
        mock_creds.secret_key = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
        mock_creds.token = None
        mock_frozen = MagicMock()
        mock_frozen.access_key = "AKIAIOSFODNN7EXAMPLE"
        mock_frozen.secret_key = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
        mock_frozen.token = None
        mock_creds.get_frozen_credentials.return_value = mock_frozen
        mock_session.return_value.get_credentials.return_value = mock_creds

        # Mock HTTP response
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(
            {
                "status": "accepted",
                "message_id": "sqs-msg-001",
                "correlation_id": "corr-abc-123",
            }
        ).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        body = {
            "correlation_id": "corr-abc-123",
            "parent_invocation_id": "msg-xyz-789",
            "persona": "reviewer",
            "target": {"repo": "aws-e/adp", "issue": 42},
        }
        result = trigger_client.send_trigger(body)

        assert result["status"] == "accepted"
        assert result["message_id"] == "sqs-msg-001"

    @patch("botocore.session.get_session")
    @patch("adp_trigger.client.urlopen")
    def test_sigv4_headers_present(self, mock_urlopen, mock_session):
        """Request must include SigV4 Authorization header."""
        mock_creds = MagicMock()
        mock_creds.access_key = "AKIAIOSFODNN7EXAMPLE"
        mock_creds.secret_key = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
        mock_creds.token = None
        mock_frozen = MagicMock()
        mock_frozen.access_key = "AKIAIOSFODNN7EXAMPLE"
        mock_frozen.secret_key = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
        mock_frozen.token = None
        mock_creds.get_frozen_credentials.return_value = mock_frozen
        mock_session.return_value.get_credentials.return_value = mock_creds

        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"status": "accepted"}'
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        body = {
            "correlation_id": "c",
            "parent_invocation_id": "p",
            "persona": "x",
            "target": {"repo": "o/r", "issue": 1},
        }
        trigger_client.send_trigger(body)

        # Verify the Request object has Authorization header with AWS4 signature
        call_args = mock_urlopen.call_args
        req = call_args[0][0]
        auth_header = req.get_header("Authorization")
        assert auth_header is not None
        assert "AWS4-HMAC-SHA256" in auth_header

    @patch("botocore.session.get_session")
    def test_exits_when_no_credentials(self, mock_session):
        """Exits with code 1 when no AWS credentials available."""
        mock_session.return_value.get_credentials.return_value = None

        body = {
            "correlation_id": "c",
            "parent_invocation_id": "p",
            "persona": "x",
            "target": {"repo": "o/r", "issue": 1},
        }
        with pytest.raises(SystemExit) as exc_info:
            trigger_client.send_trigger(body)
        assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# CLI entry point tests (subprocess)
# ---------------------------------------------------------------------------


class TestCLIEntryPoint:
    """Test the __main__.py entry point via subprocess."""

    def _run_cli(
        self, args: list[str], env_override: dict | None = None
    ) -> subprocess.CompletedProcess:
        env = {
            **os.environ,
            "ADP_CORRELATION_ID": "corr-test",
            "ADP_MESSAGE_ID": "msg-test",
            "ADP_CHAIN_DEPTH": "2",
            "ADP_TRIGGER_ENDPOINT": "https://api.example.com/dev/agent/trigger",
            "GITHUB_REPOSITORY": "aws-e/adp",
            "AWS_REGION": "us-east-1",
            "PYTHONPATH": os.path.join(os.path.dirname(__file__), ".."),
        }
        if env_override:
            env.update(env_override)
        return subprocess.run(
            [sys.executable, "-m", "adp_trigger"] + args,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def test_missing_env_exits_2(self):
        """When required env vars are missing, exits with code 2."""
        env = {k: v for k, v in os.environ.items() if not k.startswith("ADP_")}
        env["PYTHONPATH"] = os.path.join(os.path.dirname(__file__), "..")
        env["GITHUB_REPOSITORY"] = "aws-e/adp"
        result = subprocess.run(
            [sys.executable, "-m", "adp_trigger", "--persona", "reviewer", "--issue", "42"],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 2
        assert "must run inside an agent pod" in result.stderr

    def test_missing_persona_exits_2(self):
        """When --persona is not provided, exits with code 2."""
        result = self._run_cli(["--issue", "42"])
        assert result.returncode == 2
        assert "--persona is required" in result.stderr

    def test_missing_issue_exits_2(self):
        """When --issue is not provided, exits with code 2."""
        result = self._run_cli(["--persona", "reviewer"])
        assert result.returncode == 2
        assert "--issue is required" in result.stderr

    def test_invalid_issue_exits_2(self):
        """When --issue is not a number, exits with code 2."""
        result = self._run_cli(["--persona", "reviewer", "--issue", "abc"])
        assert result.returncode == 2
        assert "must be a number" in result.stderr

    def test_help_flag_shows_usage(self):
        """--help shows usage and exits with code 2."""
        result = self._run_cli(["--help"])
        assert result.returncode == 2
        assert "Usage:" in result.stderr

    def test_unknown_arg_shows_usage(self):
        """Unknown arguments show usage."""
        result = self._run_cli(["--persona", "reviewer", "--issue", "1", "--unknown", "x"])
        assert result.returncode == 2
        assert "unknown argument" in result.stderr

    def test_missing_repo_without_env_exits_2(self):
        """When --repo not provided and GITHUB_REPOSITORY unset, exits 2."""
        result = self._run_cli(
            ["--persona", "reviewer", "--issue", "42"],
            env_override={"GITHUB_REPOSITORY": ""},
        )
        assert result.returncode == 2
        assert "--repo is required" in result.stderr

    def test_valid_args_attempts_connection(self):
        """With valid args, CLI attempts the trigger (fails on connection, not env)."""
        result = self._run_cli(["--persona", "reviewer", "--issue", "42"])
        # Should fail with connection error (exit 1), not env/arg error (exit 2)
        assert result.returncode != 2
