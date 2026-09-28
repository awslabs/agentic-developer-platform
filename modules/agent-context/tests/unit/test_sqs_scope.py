"""Unit tests for SQS ingestion scope envelope (Story 7, #1776).

Tests cover:
- Scoped message round-trips scope through producer → consumer
- Legacy no-scope message defaults to shared (backward-compat)
- Invalid visibility normalizes to shared
- parse_scope handles None / empty / partial dicts
- publish_message includes scope in the message body
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add ingestion scripts to path for imports
_INGESTION_DIR = str(Path(__file__).parent.parent.parent / "images" / "ingestion")
if _INGESTION_DIR not in sys.path:
    sys.path.insert(0, _INGESTION_DIR)

from scope import DEFAULT_SCOPE, IngestionScope, parse_scope


def _load_publish_ingestion():
    """Import publish-ingestion.py (hyphenated filename requires importlib)."""
    spec = importlib.util.spec_from_file_location(
        "publish_ingestion",
        Path(__file__).parent.parent.parent / "images" / "ingestion" / "publish-ingestion.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Tests for scope.py — model + parse_scope
# ---------------------------------------------------------------------------


class TestIngestionScope:
    """Tests for the IngestionScope dataclass."""

    def test_default_scope_values(self):
        """DEFAULT_SCOPE has shared visibility and null IDs."""
        assert DEFAULT_SCOPE.tenant_id is None
        assert DEFAULT_SCOPE.owner_sub is None
        assert DEFAULT_SCOPE.project_id is None
        assert DEFAULT_SCOPE.visibility == "shared"

    def test_to_dict_roundtrip(self):
        """to_dict produces a JSON-serializable dict that parse_scope can read back."""
        scope = IngestionScope(
            tenant_id="aws-e",
            owner_sub="us-east-1:abc-123",
            project_id="proj-456",
            visibility="tenant",
        )
        d = scope.to_dict()
        assert d == {
            "tenant_id": "aws-e",
            "owner_sub": "us-east-1:abc-123",
            "project_id": "proj-456",
            "visibility": "tenant",
        }
        # Round-trip
        parsed = parse_scope(d)
        assert parsed == scope

    def test_scope_is_frozen(self):
        """IngestionScope is immutable."""
        scope = IngestionScope(tenant_id="t1")
        with pytest.raises(Exception):
            scope.tenant_id = "t2"  # type: ignore[misc]


class TestParseScope:
    """Tests for parse_scope backward compatibility."""

    def test_none_returns_default(self):
        """None input returns DEFAULT_SCOPE (backward-compat)."""
        assert parse_scope(None) == DEFAULT_SCOPE

    def test_empty_dict_returns_default(self):
        """Empty dict returns DEFAULT_SCOPE (backward-compat)."""
        assert parse_scope({}) == DEFAULT_SCOPE

    def test_partial_dict_fills_defaults(self):
        """A partial scope dict fills missing fields with defaults."""
        scope = parse_scope({"tenant_id": "acme"})
        assert scope.tenant_id == "acme"
        assert scope.owner_sub is None
        assert scope.project_id is None
        assert scope.visibility == "shared"

    def test_full_scope_parsed(self):
        """A complete scope dict is fully parsed."""
        raw = {
            "tenant_id": "aws-e",
            "owner_sub": "us-east-1:user-abc",
            "project_id": "proj-789",
            "visibility": "personal",
        }
        scope = parse_scope(raw)
        assert scope.tenant_id == "aws-e"
        assert scope.owner_sub == "us-east-1:user-abc"
        assert scope.project_id == "proj-789"
        assert scope.visibility == "personal"

    def test_invalid_visibility_defaults_to_shared(self):
        """Unknown visibility value normalizes to 'shared' for safety."""
        scope = parse_scope({"visibility": "bogus"})
        assert scope.visibility == "shared"

    def test_tenant_visibility(self):
        """Tenant visibility is valid."""
        scope = parse_scope({"tenant_id": "acme", "visibility": "tenant"})
        assert scope.visibility == "tenant"


# ---------------------------------------------------------------------------
# Tests for producer (publish_message) — scope in message body
# ---------------------------------------------------------------------------


class TestPublishMessageScope:
    """Tests that publish_message includes scope in the SQS message body."""

    @pytest.fixture(autouse=True)
    def _patch_sqs(self):
        """Patch the SQS client so publish_message doesn't call AWS."""
        self.sent_messages: list[str] = []
        self._mod = _load_publish_ingestion()

        mock_sqs = MagicMock()

        def capture_send(**kwargs):
            self.sent_messages.append(kwargs.get("MessageBody", ""))

        mock_sqs.send_message.side_effect = capture_send

        with patch.object(self._mod, "_sqs", mock_sqs):
            with patch.object(self._mod, "sqs_client", return_value=mock_sqs):
                yield

    def test_default_scope_included_when_none_provided(self):
        """When no scope is passed, DEFAULT_SCOPE is serialized into the message."""
        self._mod.publish_message(
            source="org/repo",
            content_type="repo",
            tags={},
        )
        assert len(self.sent_messages) == 1
        body = json.loads(self.sent_messages[0])
        assert "scope" in body
        assert body["scope"] == DEFAULT_SCOPE.to_dict()

    def test_explicit_scope_included(self):
        """When a scope is passed, it is serialized into the message."""
        scope = IngestionScope(
            tenant_id="acme",
            owner_sub="us-east-1:user-1",
            project_id="p-123",
            visibility="tenant",
        )
        self._mod.publish_message(
            source="acme/private-repo",
            content_type="repo",
            tags={"team": "platform"},
            scope=scope,
        )
        assert len(self.sent_messages) == 1
        body = json.loads(self.sent_messages[0])
        assert body["scope"] == {
            "tenant_id": "acme",
            "owner_sub": "us-east-1:user-1",
            "project_id": "p-123",
            "visibility": "tenant",
        }


# ---------------------------------------------------------------------------
# Tests for consumer (sqs-worker) — scope extraction + backward compat
# ---------------------------------------------------------------------------


class TestConsumerScopeExtraction:
    """Tests that the consumer correctly extracts scope from messages."""

    def test_scoped_message_roundtrip(self):
        """A message with scope has its fields correctly extracted by parse_scope."""
        # Simulate what the producer writes
        scope = IngestionScope(
            tenant_id="aws-e",
            owner_sub="us-east-1:abc-def",
            project_id="proj-42",
            visibility="personal",
        )
        message_body = {
            "source": "aws-e/internal-tool",
            "content_type": "repo",
            "steps": ["s3_upload", "cgc", "deepwiki", "graphrag"],
            "force": False,
            "tags": {},
            "triggered_by": "manual",
            "enqueued_at": "2026-06-24T10:00:00+00:00",
            "scope": scope.to_dict(),
        }

        # Consumer side: parse scope from message
        parsed = parse_scope(message_body.get("scope"))
        assert parsed.tenant_id == "aws-e"
        assert parsed.owner_sub == "us-east-1:abc-def"
        assert parsed.project_id == "proj-42"
        assert parsed.visibility == "personal"

    def test_legacy_message_no_scope_defaults_to_shared(self):
        """A legacy message without scope field defaults to shared (backward-compat)."""
        # Legacy message format (pre-Story 7)
        message_body = {
            "source": "oss/public-lib",
            "content_type": "repo",
            "steps": ["s3_upload", "cgc", "deepwiki", "graphrag"],
            "force": False,
            "tags": {},
            "triggered_by": "daily_refresh",
            "enqueued_at": "2026-06-24T06:00:00+00:00",
        }

        # Consumer side: message.get("scope") returns None
        parsed = parse_scope(message_body.get("scope"))
        assert parsed == DEFAULT_SCOPE
        assert parsed.visibility == "shared"
        assert parsed.tenant_id is None
        assert parsed.owner_sub is None
        assert parsed.project_id is None

    def test_scope_json_serialization_roundtrip(self):
        """Scope survives JSON encode/decode (as happens in SQS)."""
        scope = IngestionScope(
            tenant_id="corp",
            owner_sub=None,
            project_id="p-1",
            visibility="tenant",
        )
        # Simulate SQS JSON roundtrip
        encoded = json.dumps({"scope": scope.to_dict()})
        decoded = json.loads(encoded)
        parsed = parse_scope(decoded.get("scope"))
        assert parsed.tenant_id == "corp"
        assert parsed.owner_sub is None
        assert parsed.project_id == "p-1"
        assert parsed.visibility == "tenant"
