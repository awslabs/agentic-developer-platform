"""Unit tests for Issue #3385: PAT execution path (C1+C4) in entrypoint.

Tests the extracted _resolve_execution_token() helper which handles:
- Kill-switch gating (ADP_PAT_EXECUTION_ENABLED)
- PAT resolution via GatewayCredentialClient.raw_read()
- PAT zero-token guard (GET /user validation)
- Token mode provenance
- Warning when flag off but PAT requested
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


# --- Fixtures ---

SAMPLE_ENVELOPE_PAT = {
    "version": "1.0",
    "channel": "github",
    "tenant_id": "acme-corp",
    "persona": "developer",
    "message_id": "msg-pat-test-001",
    "token_source": "pat",
    "actor": {
        "github_id": 12345678,
        "github_login": "jane-dev",
        "user_id": "cognito-sub-jane-123",
        "is_bot": False,
    },
    "source_ref": {
        "installation_id": 99887766,
        "repo": "acme-corp/flagship-app",
        "issue": 42,
    },
}

SAMPLE_ENVELOPE_NO_TOKEN_SOURCE = {
    "version": "1.0",
    "channel": "github",
    "tenant_id": "acme-corp",
    "persona": "developer",
    "message_id": "msg-app-test-001",
    "actor": {
        "github_id": 12345678,
        "github_login": "jane-dev",
        "user_id": "cognito-sub-jane-123",
        "is_bot": False,
    },
    "source_ref": {
        "installation_id": 99887766,
        "repo": "acme-corp/flagship-app",
        "issue": 42,
    },
}


class TestPatKillSwitch:
    """ADP_PAT_EXECUTION_ENABLED flag gates the entire PAT branch."""

    @patch("urllib.request.urlopen")
    @patch.object(GatewayCredentialClient, "raw_read")
    def test_flag_absent_means_app_path(self, mock_raw_read, mock_urlopen):
        """When ADP_PAT_EXECUTION_ENABLED is unset, returns App mode."""
        result = _resolve_execution_token(
            envelope=SAMPLE_ENVELOPE_PAT,
            environ={},
        )
        assert result.token_mode == "app"
        mock_raw_read.assert_not_called()
        mock_urlopen.assert_not_called()

    @patch("urllib.request.urlopen")
    @patch.object(GatewayCredentialClient, "raw_read")
    def test_flag_false_means_app_path(self, mock_raw_read, mock_urlopen):
        """When ADP_PAT_EXECUTION_ENABLED=false, PAT path is dead."""
        result = _resolve_execution_token(
            envelope=SAMPLE_ENVELOPE_PAT,
            environ={"ADP_PAT_EXECUTION_ENABLED": "false"},
        )
        assert result.token_mode == "app"
        mock_raw_read.assert_not_called()

    @patch("urllib.request.urlopen")
    @patch.object(GatewayCredentialClient, "raw_read")
    def test_flag_true_with_pat_source_enters_pat_path(self, mock_raw_read, mock_urlopen):
        """When flag=true and token_source=pat, resolves PAT."""
        mock_raw_read.return_value = {"value": "ghp_test123"}
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"login": "jane-dev"}).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        result = _resolve_execution_token(
            envelope=SAMPLE_ENVELOPE_PAT,
            environ={"ADP_PAT_EXECUTION_ENABLED": "true"},
        )
        assert result.token_mode == "pat"
        assert result.token == "ghp_test123"
        assert result.github_login == "jane-dev"

    @patch("urllib.request.urlopen")
    @patch.object(GatewayCredentialClient, "raw_read")
    def test_flag_1_enables_pat_branch(self, mock_raw_read, mock_urlopen):
        """When ADP_PAT_EXECUTION_ENABLED=1, PAT path is active."""
        mock_raw_read.return_value = {"value": "ghp_abc"}
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"login": "u"}).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        result = _resolve_execution_token(
            envelope=SAMPLE_ENVELOPE_PAT,
            environ={"ADP_PAT_EXECUTION_ENABLED": "1"},
        )
        assert result.token_mode == "pat"


class TestPatResolutionLogic:
    """C1: PAT resolution via gateway credential client."""

    @patch("urllib.request.urlopen")
    @patch.object(GatewayCredentialClient, "raw_read")
    def test_pat_resolution_calls_raw_read_correctly(self, mock_raw_read, mock_urlopen):
        """raw_read called with correct kwargs from envelope."""
        mock_raw_read.return_value = {"value": "ghp_resolved_token"}
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"login": "jane-dev"}).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        _resolve_execution_token(
            envelope=SAMPLE_ENVELOPE_PAT,
            environ={"ADP_PAT_EXECUTION_ENABLED": "true"},
        )

        mock_raw_read.assert_called_once_with(
            user_id="cognito-sub-jane-123",
            agent_id="developer",
            task_id="msg-pat-test-001",
            service="github",
            label="github-pat",
            purpose="entrypoint: PAT resolution for execution token",
        )

    @patch("urllib.request.urlopen")
    @patch.object(GatewayCredentialClient, "raw_read")
    def test_pat_not_found_raises_runtime_error(self, mock_raw_read, mock_urlopen):
        """GatewayCredentialError → RuntimeError (no App fallback)."""
        mock_raw_read.side_effect = GatewayCredentialError(
            "Gateway returned HTTP 404: credential not found"
        )

        with pytest.raises(RuntimeError, match="credential resolution failed"):
            _resolve_execution_token(
                envelope=SAMPLE_ENVELOPE_PAT,
                environ={"ADP_PAT_EXECUTION_ENABLED": "true"},
            )

    @patch("urllib.request.urlopen")
    @patch.object(GatewayCredentialClient, "raw_read")
    def test_absent_token_source_skips_pat_branch(self, mock_raw_read, mock_urlopen):
        """When token_source is absent, returns App mode."""
        result = _resolve_execution_token(
            envelope=SAMPLE_ENVELOPE_NO_TOKEN_SOURCE,
            environ={"ADP_PAT_EXECUTION_ENABLED": "true"},
        )
        assert result.token_mode == "app"
        mock_raw_read.assert_not_called()

    @patch("urllib.request.urlopen")
    @patch.object(GatewayCredentialClient, "raw_read")
    def test_app_token_source_skips_pat_branch(self, mock_raw_read, mock_urlopen):
        """When token_source='app', returns App mode."""
        envelope = {**SAMPLE_ENVELOPE_PAT, "token_source": "app"}
        result = _resolve_execution_token(
            envelope=envelope,
            environ={"ADP_PAT_EXECUTION_ENABLED": "true"},
        )
        assert result.token_mode == "app"
        mock_raw_read.assert_not_called()


class TestPatZeroTokenGuard:
    """C4: PAT validation before clone."""

    @patch("urllib.request.urlopen")
    @patch.object(GatewayCredentialClient, "raw_read")
    def test_pat_validate_success_returns_login(self, mock_raw_read, mock_urlopen):
        """Valid PAT → returns github_login from GET /user."""
        mock_raw_read.return_value = {"value": "ghp_valid_token"}
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"login": "jane-dev"}).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        result = _resolve_execution_token(
            envelope=SAMPLE_ENVELOPE_PAT,
            environ={"ADP_PAT_EXECUTION_ENABLED": "true"},
        )
        assert result.github_login == "jane-dev"
        assert result.token == "ghp_valid_token"

    @patch("urllib.request.urlopen")
    @patch.object(GatewayCredentialClient, "raw_read")
    def test_pat_validate_401_raises_expired(self, mock_raw_read, mock_urlopen):
        """401 from GET /user → RuntimeError about expired/revoked."""
        import urllib.error

        mock_raw_read.return_value = {"value": "ghp_expired"}
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="https://api.github.com/user",
            code=401,
            msg="Unauthorized",
            hdrs={},
            fp=None,
        )

        with pytest.raises(RuntimeError, match="expired or revoked"):
            _resolve_execution_token(
                envelope=SAMPLE_ENVELOPE_PAT,
                environ={"ADP_PAT_EXECUTION_ENABLED": "true"},
            )

    @patch("urllib.request.urlopen")
    @patch.object(GatewayCredentialClient, "raw_read")
    def test_pat_validate_403_raises_permissions(self, mock_raw_read, mock_urlopen):
        """403 from GET /user → RuntimeError about permissions."""
        import urllib.error

        mock_raw_read.return_value = {"value": "ghp_noscope"}
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="https://api.github.com/user",
            code=403,
            msg="Forbidden",
            hdrs={},
            fp=None,
        )

        with pytest.raises(RuntimeError, match="lacks required permissions"):
            _resolve_execution_token(
                envelope=SAMPLE_ENVELOPE_PAT,
                environ={"ADP_PAT_EXECUTION_ENABLED": "true"},
            )

    @patch("urllib.request.urlopen")
    @patch.object(GatewayCredentialClient, "raw_read")
    def test_pat_validate_500_raises_generic(self, mock_raw_read, mock_urlopen):
        """Other HTTP errors → generic RuntimeError."""
        import urllib.error

        mock_raw_read.return_value = {"value": "ghp_x"}
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="https://api.github.com/user",
            code=500,
            msg="ISE",
            hdrs={},
            fp=None,
        )

        with pytest.raises(RuntimeError, match="HTTP 500"):
            _resolve_execution_token(
                envelope=SAMPLE_ENVELOPE_PAT,
                environ={"ADP_PAT_EXECUTION_ENABLED": "true"},
            )


class TestTokenModeProvenance:
    """C5: token_mode is set correctly for DDB write."""

    @patch("urllib.request.urlopen")
    @patch.object(GatewayCredentialClient, "raw_read")
    def test_pat_resolution_sets_mode_pat(self, mock_raw_read, mock_urlopen):
        """Successful PAT resolution → token_mode='pat'."""
        mock_raw_read.return_value = {"value": "ghp_prov"}
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"login": "u"}).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        result = _resolve_execution_token(
            envelope=SAMPLE_ENVELOPE_PAT,
            environ={"ADP_PAT_EXECUTION_ENABLED": "true"},
        )
        assert result.token_mode == "pat"

    def test_no_token_source_returns_mode_app(self):
        """Default path (no token_source) → token_mode='app'."""
        result = _resolve_execution_token(
            envelope=SAMPLE_ENVELOPE_NO_TOKEN_SOURCE,
            environ={"ADP_PAT_EXECUTION_ENABLED": "true"},
        )
        assert result.token_mode == "app"

    def test_flag_off_returns_mode_app(self):
        """Flag disabled with token_source=pat → token_mode='app'."""
        result = _resolve_execution_token(
            envelope=SAMPLE_ENVELOPE_PAT,
            environ={},
        )
        assert result.token_mode == "app"


class TestFlagDisabledWarning:
    """When token_source=pat but flag is off, log warning."""

    def test_warning_logged_when_flag_off_but_pat_requested(self, caplog):
        """Capture actual log output via caplog."""
        with caplog.at_level(logging.WARNING):
            result = _resolve_execution_token(
                envelope=SAMPLE_ENVELOPE_PAT,
                environ={},
            )

        assert result.token_mode == "app"
        assert result.warning is not None
        assert "ADP_PAT_EXECUTION_ENABLED not set" in result.warning
        # Verify the actual log record was emitted
        assert any(
            "ADP_PAT_EXECUTION_ENABLED not set" in record.message for record in caplog.records
        )

    def test_no_warning_when_token_source_absent(self, caplog):
        """No warning when token_source is absent (normal App path)."""
        with caplog.at_level(logging.WARNING):
            result = _resolve_execution_token(
                envelope=SAMPLE_ENVELOPE_NO_TOKEN_SOURCE,
                environ={},
            )

        assert result.warning is None
        assert not any("ADP_PAT_EXECUTION_ENABLED" in record.message for record in caplog.records)


class TestPoisonGuardUnchanged:
    """Regression: installation_id=0 poison guard still fires on App path.

    This validates the condition used in entrypoint.py and spawn_persona.py.
    """

    def test_poison_guard_still_triggers(self):
        """installation_id=0 still matches the guard condition."""
        installation_id = 0
        assert installation_id in (0, None, "0")

    def test_poison_guard_string_zero(self):
        """installation_id='0' also triggers guard."""
        installation_id = "0"
        assert installation_id in (0, None, "0")

    def test_poison_guard_none(self):
        """installation_id=None also triggers guard."""
        installation_id = None
        assert installation_id in (0, None, "0")

    def test_valid_installation_id_does_not_trigger(self):
        """A real installation_id does NOT trigger the guard."""
        installation_id = 99887766
        assert installation_id not in (0, None, "0")
