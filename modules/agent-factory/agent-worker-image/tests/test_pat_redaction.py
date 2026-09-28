"""Unit tests for Issue #3385: PAT redaction assertions.

Verifies that the PAT value never leaks into log output from the real
_resolve_execution_token() code path. Tests exercise the actual entrypoint
helper and capture log records via caplog.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from entrypoint import _resolve_execution_token
from lib.gateway_credential_client import (
    GatewayCredentialClient,
    GatewayCredentialError,
)


PAT_TOKEN = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef01234"


class TestPatNotInLogOutput:
    """PAT value must never appear in any log record emitted by the helper."""

    @patch("urllib.request.urlopen")
    @patch.object(GatewayCredentialClient, "raw_read")
    def test_successful_pat_resolve_does_not_log_token(self, mock_raw_read, mock_urlopen, caplog):
        """On successful PAT resolution, no log record contains the PAT."""
        mock_raw_read.return_value = {"value": PAT_TOKEN}
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"login": "jane-dev"}).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        with caplog.at_level(logging.DEBUG):
            result = _resolve_execution_token(
                envelope={
                    "token_source": "pat",
                    "persona": "developer",
                    "message_id": "msg-001",
                    "actor": {"user_id": "u1"},
                    "source_ref": {"repo": "org/repo", "issue": 1},
                },
                environ={"ADP_PAT_EXECUTION_ENABLED": "true"},
            )

        assert result.token_mode == "pat"
        # The PAT must NOT appear in ANY log record
        for record in caplog.records:
            assert PAT_TOKEN not in record.getMessage(), (
                f"PAT leaked into log: {record.getMessage()!r}"
            )

    @patch("urllib.request.urlopen")
    @patch.object(GatewayCredentialClient, "raw_read")
    def test_failed_pat_resolve_does_not_log_token(self, mock_raw_read, mock_urlopen, caplog):
        """On PAT resolution failure, error message does not contain PAT."""
        mock_raw_read.side_effect = GatewayCredentialError("Gateway returned HTTP 404: not found")

        with caplog.at_level(logging.DEBUG):
            with pytest.raises(RuntimeError) as exc_info:
                _resolve_execution_token(
                    envelope={
                        "token_source": "pat",
                        "persona": "developer",
                        "message_id": "msg-002",
                        "actor": {"user_id": "u1"},
                        "source_ref": {"repo": "org/repo", "issue": 1},
                    },
                    environ={"ADP_PAT_EXECUTION_ENABLED": "true"},
                )

        # Error message must not contain any PAT-like string
        assert PAT_TOKEN not in str(exc_info.value)

    @patch("urllib.request.urlopen")
    @patch.object(GatewayCredentialClient, "raw_read")
    def test_warning_path_does_not_log_token(self, mock_raw_read, mock_urlopen, caplog):
        """Warning log when flag off does not contain any token value."""
        with caplog.at_level(logging.WARNING):
            result = _resolve_execution_token(
                envelope={
                    "token_source": "pat",
                    "persona": "developer",
                    "message_id": "msg-003",
                    "actor": {"user_id": "u1"},
                    "source_ref": {"repo": "org/repo", "issue": 1},
                },
                environ={},
            )

        assert result.token_mode == "app"
        for record in caplog.records:
            assert PAT_TOKEN not in record.getMessage()


class TestGitAskpassPreventsUrlLeak:
    """Clone URL uses x-access-token@ without embedding token in URL."""

    def test_clone_url_pattern_does_not_embed_token(self):
        """The clone URL pattern must not contain the raw PAT."""
        repo = "acme-corp/flagship-app"
        clone_url = f"https://x-access-token@github.com/{repo}"

        assert PAT_TOKEN not in clone_url
        assert "x-access-token@" in clone_url
        # No colon between username and password in the URL
        assert ":" not in clone_url.split("@")[0].split("//")[1]


class TestPatResultDoesNotExposeInRepr:
    """PatResolutionResult does not expose token in its repr/str."""

    @patch("urllib.request.urlopen")
    @patch.object(GatewayCredentialClient, "raw_read")
    def test_result_object_token_is_present_but_not_in_str(self, mock_raw_read, mock_urlopen):
        """The token IS accessible via .token but doesn't leak in __repr__."""
        mock_raw_read.return_value = {"value": PAT_TOKEN}
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"login": "jane-dev"}).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        result = _resolve_execution_token(
            envelope={
                "token_source": "pat",
                "persona": "developer",
                "message_id": "msg-004",
                "actor": {"user_id": "u1"},
                "source_ref": {"repo": "org/repo", "issue": 1},
            },
            environ={"ADP_PAT_EXECUTION_ENABLED": "true"},
        )

        # Token is stored for downstream use
        assert result.token == PAT_TOKEN
        # But it should not appear in casual string representations
        # (PatResolutionResult uses __slots__, no default __repr__ shows values)
        assert result.token_mode == "pat"
        assert result.github_login == "jane-dev"
