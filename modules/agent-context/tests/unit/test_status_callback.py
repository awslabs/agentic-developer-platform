"""Unit tests for status_callback.py — worker → gateway status emitter.

Issue #2049: Minimal status-callback bridge (C1/Decision 5).

Tests cover:
  - emit_status_callback sends correct HTTP POST with asset_id, status, status_detail
  - Skips silently when asset_id is None/empty (legacy messages)
  - Skips silently when GATEWAY_CALLBACK_URL is not set
  - Fail-open: request exceptions are caught and logged (never raised)
  - Fail-open: timeout exceptions are caught (never raised)
  - Error message truncated to 1000 chars
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add ingestion scripts to path for imports
_INGESTION_DIR = str(Path(__file__).parent.parent.parent / "images" / "ingestion")
if _INGESTION_DIR not in sys.path:
    sys.path.insert(0, _INGESTION_DIR)


@pytest.fixture(autouse=True)
def _reset_module_env(monkeypatch):
    """Reset module-level env vars before each test."""
    monkeypatch.setenv("GATEWAY_CALLBACK_URL", "http://gateway:8080")
    monkeypatch.setenv("GATEWAY_INTERNAL_API_KEY", "test-key-123")
    # Reimport to pick up env changes
    if "status_callback" in sys.modules:
        del sys.modules["status_callback"]


def _import_status_callback():
    """Import status_callback module freshly (picks up env vars)."""
    import status_callback

    importlib.reload(status_callback)
    return status_callback


# ---------------------------------------------------------------------------
# Tests — Happy path
# ---------------------------------------------------------------------------


class TestEmitStatusCallback:
    """Test emit_status_callback sends correct HTTP POST."""

    @patch("status_callback.requests.post")
    def test_sends_indexing_status(self, mock_post, monkeypatch):
        """Sends POST with status=indexing and correct headers."""
        monkeypatch.setenv("GATEWAY_CALLBACK_URL", "http://gateway:8080")
        monkeypatch.setenv("GATEWAY_INTERNAL_API_KEY", "secret-key")
        mod = _import_status_callback()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_post.return_value = mock_resp

        mod.emit_status_callback("asset-uuid-123", "indexing")

        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args
        url = call_kwargs[0][0] if call_kwargs[0] else call_kwargs[1].get("url")
        assert url == "http://gateway:8080/internal/v1/knowledge-assets/status-callback"

        body = json.loads(call_kwargs[1]["data"])
        assert body["asset_id"] == "asset-uuid-123"
        assert body["status"] == "indexing"
        assert "status_detail" not in body

        headers = call_kwargs[1]["headers"]
        assert headers["X-Internal-Api-Key"] == "secret-key"

    @patch("status_callback.requests.post")
    def test_sends_complete_with_status_detail(self, mock_post, monkeypatch):
        """Sends POST with status=complete and compact status_detail."""
        monkeypatch.setenv("GATEWAY_CALLBACK_URL", "http://gateway:8080")
        monkeypatch.setenv("GATEWAY_INTERNAL_API_KEY", "key")
        mod = _import_status_callback()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_post.return_value = mock_resp

        detail = {"completed_at": "2026-06-26T00:00:00Z", "steps": {"s3": "ok"}}
        mod.emit_status_callback("asset-uuid-456", "complete", status_detail=detail)

        body = json.loads(mock_post.call_args[1]["data"])
        assert body["status"] == "complete"
        assert body["status_detail"] == detail

    @patch("status_callback.requests.post")
    def test_sends_failed_with_error(self, mock_post, monkeypatch):
        """Sends POST with status=failed and error field."""
        monkeypatch.setenv("GATEWAY_CALLBACK_URL", "http://gateway:8080")
        monkeypatch.setenv("GATEWAY_INTERNAL_API_KEY", "key")
        mod = _import_status_callback()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_post.return_value = mock_resp

        mod.emit_status_callback("asset-uuid-789", "failed", error="timeout after 900s")

        body = json.loads(mock_post.call_args[1]["data"])
        assert body["status"] == "failed"
        assert body["error"] == "timeout after 900s"


# ---------------------------------------------------------------------------
# Tests — Skip conditions
# ---------------------------------------------------------------------------


class TestSkipConditions:
    """Test conditions where callback is skipped silently."""

    @patch("status_callback.requests.post")
    def test_skips_when_asset_id_is_none(self, mock_post, monkeypatch):
        """Legacy messages without registry_asset_id → no HTTP call."""
        monkeypatch.setenv("GATEWAY_CALLBACK_URL", "http://gateway:8080")
        mod = _import_status_callback()

        mod.emit_status_callback(None, "indexing")
        mock_post.assert_not_called()

    @patch("status_callback.requests.post")
    def test_skips_when_asset_id_is_empty(self, mock_post, monkeypatch):
        """Empty string asset_id → no HTTP call."""
        monkeypatch.setenv("GATEWAY_CALLBACK_URL", "http://gateway:8080")
        mod = _import_status_callback()

        mod.emit_status_callback("", "indexing")
        mock_post.assert_not_called()

    @patch("status_callback.requests.post")
    def test_skips_when_callback_url_not_set(self, mock_post, monkeypatch):
        """GATEWAY_CALLBACK_URL not configured → no HTTP call."""
        monkeypatch.setenv("GATEWAY_CALLBACK_URL", "")
        mod = _import_status_callback()

        mod.emit_status_callback("asset-uuid-123", "indexing")
        mock_post.assert_not_called()


# ---------------------------------------------------------------------------
# Tests — Fail-open behavior
# ---------------------------------------------------------------------------


class TestFailOpen:
    """Test that callback failures never crash the worker."""

    @patch("status_callback.requests.post")
    def test_request_exception_is_swallowed(self, mock_post, monkeypatch):
        """Network error → logged warning, no exception raised."""
        monkeypatch.setenv("GATEWAY_CALLBACK_URL", "http://gateway:8080")
        monkeypatch.setenv("GATEWAY_INTERNAL_API_KEY", "key")
        mod = _import_status_callback()

        import requests

        mock_post.side_effect = requests.ConnectionError("Connection refused")

        # Should NOT raise
        mod.emit_status_callback("asset-uuid-123", "indexing")

    @patch("status_callback.requests.post")
    def test_timeout_exception_is_swallowed(self, mock_post, monkeypatch):
        """Timeout → logged warning, no exception raised."""
        monkeypatch.setenv("GATEWAY_CALLBACK_URL", "http://gateway:8080")
        monkeypatch.setenv("GATEWAY_INTERNAL_API_KEY", "key")
        mod = _import_status_callback()

        import requests

        mock_post.side_effect = requests.Timeout("timed out")

        # Should NOT raise
        mod.emit_status_callback("asset-uuid-123", "complete")

    @patch("status_callback.requests.post")
    def test_500_response_is_logged_not_raised(self, mock_post, monkeypatch):
        """Gateway returns 500 → logged warning, no exception raised."""
        monkeypatch.setenv("GATEWAY_CALLBACK_URL", "http://gateway:8080")
        monkeypatch.setenv("GATEWAY_INTERNAL_API_KEY", "key")
        mod = _import_status_callback()

        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "Internal Server Error"
        mock_post.return_value = mock_resp

        # Should NOT raise
        mod.emit_status_callback("asset-uuid-123", "indexing")


# ---------------------------------------------------------------------------
# Tests — Error truncation
# ---------------------------------------------------------------------------


class TestErrorTruncation:
    """Test that long error messages are truncated."""

    @patch("status_callback.requests.post")
    def test_error_truncated_to_1000_chars(self, mock_post, monkeypatch):
        """Error messages longer than 1000 chars are truncated."""
        monkeypatch.setenv("GATEWAY_CALLBACK_URL", "http://gateway:8080")
        monkeypatch.setenv("GATEWAY_INTERNAL_API_KEY", "key")
        mod = _import_status_callback()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_post.return_value = mock_resp

        long_error = "x" * 2000
        mod.emit_status_callback("asset-uuid-123", "failed", error=long_error)

        body = json.loads(mock_post.call_args[1]["data"])
        assert len(body["error"]) == 1000
