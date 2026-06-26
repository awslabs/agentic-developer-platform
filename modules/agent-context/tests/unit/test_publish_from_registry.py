"""Unit tests for publish-ingestion.py --from-registry mode (Issue #2082 Phase 2).

Tests:
- publish_from_registry enqueues changed repos with correct scope + installation_id
- publish_from_registry skips unchanged repos (DynamoDB SHA match)
- publish_from_registry handles registry read failure gracefully
- publish_message includes registry_asset_id and installation_id when provided
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add the ingestion source to sys.path for direct imports
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "images" / "ingestion"))

_PUBLISH_INGESTION_PATH = str(
    Path(__file__).resolve().parents[2] / "images" / "ingestion" / "publish-ingestion.py"
)


def _load_publish_module(monkeypatch):
    """Load publish-ingestion.py as a module (handles the hyphenated filename)."""
    monkeypatch.setenv("SQS_QUEUE_URL", "https://sqs.example.com/queue")
    spec = importlib.util.spec_from_file_location("publish_ingestion", _PUBLISH_INGESTION_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _set_sqs_url(monkeypatch):
    """Ensure SQS_QUEUE_URL is set for all tests."""
    monkeypatch.setenv("SQS_QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/test-queue")


# ---------------------------------------------------------------------------
# publish_message tests (registry fields)
# ---------------------------------------------------------------------------


class TestPublishMessageRegistryFields:
    """Tests that publish_message includes registry_asset_id and installation_id."""

    def test_includes_registry_asset_id(self, monkeypatch):
        """registry_asset_id included in SQS message when provided."""
        mod = _load_publish_module(monkeypatch)

        mock_client = MagicMock()
        mock_client.send_message.return_value = {"MessageId": "abc"}

        with patch.object(mod, "sqs_client", return_value=mock_client):
            result = mod.publish_message(
                source="acme/svc",
                content_type="repo",
                tags={},
                registry_asset_id="uuid-123",
                installation_id=99999,
            )

        assert result is True
        call_args = mock_client.send_message.call_args
        body = json.loads(call_args.kwargs["MessageBody"])
        assert body["registry_asset_id"] == "uuid-123"
        assert body["installation_id"] == 99999

    def test_omits_registry_fields_when_none(self, monkeypatch):
        """registry_asset_id and installation_id omitted when not provided."""
        mod = _load_publish_module(monkeypatch)

        mock_client = MagicMock()
        mock_client.send_message.return_value = {"MessageId": "abc"}

        with patch.object(mod, "sqs_client", return_value=mock_client):
            result = mod.publish_message(
                source="acme/svc",
                content_type="repo",
                tags={},
            )

        assert result is True
        call_args = mock_client.send_message.call_args
        body = json.loads(call_args.kwargs["MessageBody"])
        assert "registry_asset_id" not in body
        assert "installation_id" not in body


# ---------------------------------------------------------------------------
# publish_from_registry tests
# ---------------------------------------------------------------------------


class TestPublishFromRegistry:
    """Tests for the publish_from_registry function."""

    @patch("registry_reader.read_registry_assets")
    def test_enqueues_changed_repos_with_scope(self, mock_read, monkeypatch):
        """Changed repos are enqueued with correct scope and installation_id."""
        from registry_reader import RegistryAsset

        mock_read.return_value = [
            RegistryAsset(
                asset_id="uuid-1",
                source_ref="https://github.com/acme/svc",
                asset_type="repo",
                tenant_id="tenant-acme",
                owner_sub=None,
                project_id=None,
                installation_id=12345,
            ),
        ]

        mod = _load_publish_module(monkeypatch)

        mock_client = MagicMock()
        mock_client.send_message.return_value = {"MessageId": "x"}

        with (
            patch.object(mod, "sqs_client", return_value=mock_client),
            patch.object(mod, "get_dynamo_state", return_value=None),
            patch.object(mod, "has_changed", return_value=True),
        ):
            stats = mod.publish_from_registry(force=False, triggered_by="daily_refresh")

        assert stats["total"] == 1
        assert stats["enqueued"] == 1
        assert stats["skipped"] == 0

        # Verify message body
        call_args = mock_client.send_message.call_args
        body = json.loads(call_args.kwargs["MessageBody"])
        assert body["source"] == "acme/svc"
        assert body["scope"]["tenant_id"] == "tenant-acme"
        assert body["scope"]["visibility"] == "tenant"
        assert body["registry_asset_id"] == "uuid-1"
        assert body["installation_id"] == 12345

    @patch("registry_reader.read_registry_assets")
    def test_skips_unchanged_repos(self, mock_read, monkeypatch):
        """Unchanged repos (DynamoDB SHA match) are skipped."""
        from registry_reader import RegistryAsset

        mock_read.return_value = [
            RegistryAsset(
                asset_id="uuid-2",
                source_ref="https://github.com/acme/stable",
                asset_type="repo",
                tenant_id="tenant-acme",
                owner_sub=None,
                project_id=None,
                installation_id=12345,
            ),
        ]

        mod = _load_publish_module(monkeypatch)

        with (
            patch.object(mod, "get_dynamo_state", return_value={"last_sha": "abc123"}),
            patch.object(mod, "has_changed", return_value=False),
        ):
            stats = mod.publish_from_registry(force=False, triggered_by="daily_refresh")

        assert stats["total"] == 1
        assert stats["enqueued"] == 0
        assert stats["skipped"] == 1

    @patch("registry_reader.read_registry_assets")
    def test_force_enqueues_all(self, mock_read, monkeypatch):
        """Force mode enqueues all repos regardless of state."""
        from registry_reader import RegistryAsset

        mock_read.return_value = [
            RegistryAsset(
                asset_id="uuid-3",
                source_ref="https://github.com/acme/stable",
                asset_type="repo",
                tenant_id=None,
                owner_sub=None,
                project_id=None,
                installation_id=None,
            ),
        ]

        mod = _load_publish_module(monkeypatch)

        mock_client = MagicMock()
        mock_client.send_message.return_value = {"MessageId": "x"}

        with patch.object(mod, "sqs_client", return_value=mock_client):
            stats = mod.publish_from_registry(force=True, triggered_by="daily_refresh")

        assert stats["enqueued"] == 1
        # Verify shared scope
        call_args = mock_client.send_message.call_args
        body = json.loads(call_args.kwargs["MessageBody"])
        assert body["scope"]["visibility"] == "shared"

    @patch("registry_reader.read_registry_assets")
    def test_handles_registry_read_failure(self, mock_read, monkeypatch):
        """Returns error stats when registry read fails."""
        mock_read.side_effect = RuntimeError("GATEWAY_DB_NAME not set")

        mod = _load_publish_module(monkeypatch)
        stats = mod.publish_from_registry(force=False, triggered_by="daily_refresh")

        assert stats["errors"] == 1
        assert stats["enqueued"] == 0
