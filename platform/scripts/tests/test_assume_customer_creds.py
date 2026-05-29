"""Tests for assume-customer-creds.py retry logic."""

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

import pytest

# Load the script as a module (it doesn't live in a package)
_SCRIPT_PATH = Path(__file__).resolve().parent.parent / "assume-customer-creds.py"
_spec = importlib.util.spec_from_file_location("assume_customer_creds", _SCRIPT_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

_request_with_retry = _mod._request_with_retry
Request = _mod.Request


def _make_request():
    """Build a dummy Request object for testing."""
    return Request(
        "http://gateway.local/internal/v1/credential-assume-role",
        data=json.dumps({"user_id": "test"}).encode(),
        headers={"X-Internal-Api-Key": "key", "Content-Type": "application/json"},
        method="POST",
    )


class TestRetryOnServerError:
    """Retry behavior for transient 5xx errors."""

    @patch("time.sleep")
    def test_retries_on_500_then_succeeds(self, mock_sleep):
        """Should retry on HTTP 500 and succeed on subsequent attempt."""
        success_body = json.dumps({
            "access_key_id": "AKIA...",
            "secret_access_key": "secret",
            "session_token": "token",
        }).encode()

        mock_resp = MagicMock()
        mock_resp.read.return_value = success_body
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        http_500 = HTTPError(
            "http://gateway.local", 500, "Internal Server Error", {}, None
        )
        http_500.fp = None

        call_count = [0]

        def side_effect(req, timeout=30):
            call_count[0] += 1
            if call_count[0] == 1:
                raise http_500
            return mock_resp

        with patch.object(_mod, "MAX_RETRIES", 3):
            with patch.object(_mod, "BACKOFF_BASE_SECONDS", 0.01):
                with patch.object(_mod, "urlopen", side_effect=side_effect):
                    result = _request_with_retry(
                        "http://gateway.local/internal/v1/credential-assume-role",
                        _make_request(),
                    )

        assert result["access_key_id"] == "AKIA..."
        assert call_count[0] == 2
        mock_sleep.assert_called_once()

    @patch("time.sleep")
    def test_fails_after_max_retries_exhausted(self, mock_sleep):
        """Should exit with code 2 after all retries fail."""
        http_500 = HTTPError(
            "http://gateway.local", 500, "Internal Server Error", {}, None
        )
        http_500.fp = None

        with patch.object(_mod, "MAX_RETRIES", 2):
            with patch.object(_mod, "BACKOFF_BASE_SECONDS", 0.01):
                with patch.object(_mod, "urlopen", side_effect=http_500):
                    with pytest.raises(SystemExit) as exc_info:
                        _request_with_retry(
                            "http://gateway.local/endpoint",
                            _make_request(),
                        )
        assert exc_info.value.code == 2

    @patch("time.sleep")
    def test_no_retry_on_4xx(self, mock_sleep):
        """Should NOT retry on client errors (4xx)."""
        http_403 = HTTPError(
            "http://gateway.local", 403, "Forbidden", {}, None
        )
        http_403.fp = None

        with patch.object(_mod, "MAX_RETRIES", 3):
            with patch.object(_mod, "urlopen", side_effect=http_403):
                with pytest.raises(SystemExit) as exc_info:
                    _request_with_retry(
                        "http://gateway.local/endpoint",
                        _make_request(),
                    )
        assert exc_info.value.code == 2
        mock_sleep.assert_not_called()


class TestRetryOnConnectionError:
    """Retry behavior for connection/timeout errors."""

    @patch("time.sleep")
    def test_retries_on_url_error_then_succeeds(self, mock_sleep):
        """Should retry on URLError and succeed on next attempt."""
        success_body = json.dumps({
            "access_key_id": "AKIA...",
            "secret_access_key": "secret",
            "session_token": "token",
        }).encode()

        mock_resp = MagicMock()
        mock_resp.read.return_value = success_body
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        call_count = [0]

        def side_effect(req, timeout=30):
            call_count[0] += 1
            if call_count[0] == 1:
                raise URLError("Connection refused")
            return mock_resp

        with patch.object(_mod, "MAX_RETRIES", 3):
            with patch.object(_mod, "BACKOFF_BASE_SECONDS", 0.01):
                with patch.object(_mod, "urlopen", side_effect=side_effect):
                    result = _request_with_retry(
                        "http://gateway.local/endpoint",
                        _make_request(),
                    )

        assert result["access_key_id"] == "AKIA..."
        assert call_count[0] == 2

    @patch("time.sleep")
    def test_retries_on_timeout_then_succeeds(self, mock_sleep):
        """Should retry on TimeoutError."""
        success_body = json.dumps({
            "access_key_id": "AKIA...",
            "secret_access_key": "secret",
            "session_token": "token",
        }).encode()

        mock_resp = MagicMock()
        mock_resp.read.return_value = success_body
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        call_count = [0]

        def side_effect(req, timeout=30):
            call_count[0] += 1
            if call_count[0] == 1:
                raise TimeoutError("timed out")
            return mock_resp

        with patch.object(_mod, "MAX_RETRIES", 3):
            with patch.object(_mod, "BACKOFF_BASE_SECONDS", 0.01):
                with patch.object(_mod, "urlopen", side_effect=side_effect):
                    result = _request_with_retry(
                        "http://gateway.local/endpoint",
                        _make_request(),
                    )

        assert result["access_key_id"] == "AKIA..."
        assert call_count[0] == 2


class TestBackoffTiming:
    """Exponential backoff timing validation."""

    @patch("time.sleep")
    def test_exponential_backoff_intervals(self, mock_sleep):
        """Backoff should double each attempt: base, base*2, base*4."""
        http_500 = HTTPError(
            "http://gateway.local", 500, "Internal Server Error", {}, None
        )
        http_500.fp = None

        with patch.object(_mod, "MAX_RETRIES", 3):
            with patch.object(_mod, "BACKOFF_BASE_SECONDS", 1.0):
                with patch.object(_mod, "urlopen", side_effect=http_500):
                    with pytest.raises(SystemExit):
                        _request_with_retry(
                            "http://gateway.local/endpoint",
                            _make_request(),
                        )

        # 3 attempts means 2 sleeps (between attempt 1→2 and 2→3)
        assert mock_sleep.call_count == 2
        calls = [c[0][0] for c in mock_sleep.call_args_list]
        assert calls[0] == 1.0  # base * 2^0
        assert calls[1] == 2.0  # base * 2^1
