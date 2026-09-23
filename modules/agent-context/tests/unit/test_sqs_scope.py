"""Unit tests for SQS ingestion scope envelope (Story 7, #1776).

Tests cover:
- Scoped message round-trips scope through producer → consumer
- Missing, invalid and contradictory ownership is refused
- Trusted producers continue to emit an explicit shared scope
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

from scope import DEFAULT_SCOPE, IngestionScope, ScopeValidationError, parse_scope


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

    def test_none_is_refused(self):
        with pytest.raises(ScopeValidationError):
            parse_scope(None)

    def test_empty_dict_is_refused(self):
        with pytest.raises(ScopeValidationError):
            parse_scope({})

    def test_partial_dict_cannot_become_shared(self):
        with pytest.raises(ScopeValidationError):
            parse_scope({"tenant_id": "acme"})

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

    def test_invalid_visibility_is_rejected(self):
        """Unknown visibility value is fatal, not normalized to shared (#5658).

        This test previously asserted the opposite, on the stated rationale that
        shared was the "safe" default. It is the unsafe one: an unrecognised
        visibility means producer and consumer disagree about the vocabulary, and
        resolving that disagreement by publishing to the prefix every tenant can
        read is the disclosure. Missing scope is likewise refused; trusted
        producers must provide an explicit shared scope.
        """
        with pytest.raises(ScopeValidationError):
            parse_scope({"visibility": "bogus"})

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

    def test_legacy_message_needs_authoritative_scope_before_replay(self):
        message_body = {"source": "oss/public-lib", "content_type": "repo"}
        with pytest.raises(ScopeValidationError):
            parse_scope(message_body.get("scope"))

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


# ---------------------------------------------------------------------------
# Scope downgrade is not a safe default (#5658)
# ---------------------------------------------------------------------------


class TestScopeDowngradeIsRefused:
    """Missing ownership and invalid restrictions both deny; explicit sharing works."""

    def test_only_explicit_shared_scope_is_allowed(self):
        for missing in (None, {}):
            with pytest.raises(ScopeValidationError):
                parse_scope(missing)
        assert parse_scope({"visibility": "shared"}) == DEFAULT_SCOPE

    def test_tenant_scope_without_tenant_id_raises(self):
        with pytest.raises(ScopeValidationError) as excinfo:
            parse_scope({"visibility": "tenant"})
        # The message names the missing field: an operator reading the DLQ needs
        # to know what the producer omitted.
        assert "tenant_id" in str(excinfo.value)

    def test_personal_scope_without_owner_sub_raises(self):
        with pytest.raises(ScopeValidationError) as excinfo:
            parse_scope({"visibility": "personal"})
        assert "owner_sub" in str(excinfo.value)

    def test_a_valid_restriction_is_honoured_unchanged(self):
        """The guard must not break scoped ingestion, or it will be reverted."""
        scope = parse_scope(
            {"visibility": "personal", "owner_sub": "us-east-1:user-abc", "tenant_id": "acme"}
        )
        assert scope.is_personal
        assert scope.owner_sub == "us-east-1:user-abc"

    def test_no_restricted_visibility_can_silently_become_shared(self):
        """Exhaustive over the restricted visibilities, so a new one is not missed.

        If a fourth visibility is added with a required identifier, this test does
        not automatically cover it — but it does document the invariant that every
        restricted visibility either validates or raises, never downgrades.
        """
        from scope import VALID_VISIBILITIES

        restricted = [v for v in VALID_VISIBILITIES if v != "shared"]
        assert restricted, "expected at least one restricted visibility"

        for visibility in restricted:
            # Stated with none of its required identifiers present.
            with pytest.raises(ScopeValidationError):
                parse_scope({"visibility": visibility})
