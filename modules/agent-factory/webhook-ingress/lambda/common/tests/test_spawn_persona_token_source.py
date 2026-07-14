"""Tests for Issue #3385 (C3): token_source producer in spawn_persona.

Verifies that:
- token_source_override="pat" on the tenant identity-index row results in
  token_source="pat" on the published envelope.
- Absent token_source_override results in token_source absent from envelope.
- token_source is passed through _build_envelope correctly.
"""

import sys
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from common.spawn_persona import _build_envelope, spawn_persona


@dataclass
class MockResolvedIdentity:
    """Minimal mock of ResolvedIdentity for test purposes."""

    tenant_id: str = "test-org"
    org_id: str = "test-org"
    user_id: str = "user-123"
    user_provisioning_mode: str = "strict"
    user_kind: str = "human"
    bot_kind: str = ""


def _human_sender():
    return {"login": "alice", "id": 100, "type": "User"}


def _base_correlation_ctx():
    return {
        "correlation_id": "corr-test-001",
        "root_human_id": "user-alice",
        "triggered_by": None,
        "is_human_rooted": True,
        "is_new_chain": False,
        "parent_invocation_id": None,
        "chain_depth": 0,
        "last_triggered_persona": None,
        "recent_triggered_personas": set(),
        "recent_trigger_count": 0,
    }


def _base_payload():
    return {
        "action": "created",
        "comment": {"body": "@agent-developer please fix"},
        "issue": {
            "number": 55,
            "title": "Test issue",
            "html_url": "https://github.com/org/repo/issues/55",
        },
        "repository": {"full_name": "org/repo"},
        "sender": _human_sender(),
        "installation": {"id": 123},
    }


class TestBuildEnvelopeTokenSource:
    """_build_envelope includes token_source only when set."""

    def test_token_source_pat_included_in_envelope(self):
        """token_source='pat' → key present in envelope dict."""
        envelope = _build_envelope(
            persona="developer",
            tenant_id="test-org",
            cognito_sub="user-123",
            actor_user_id="user-123",
            actor_org_id="test-org",
            sender=_human_sender(),
            installation_id=123,
            repo="org/repo",
            payload=_base_payload(),
            correlation_ctx=_base_correlation_ctx(),
            intent_trigger="mentioned",
            intent_label=None,
            token_source="pat",
        )
        assert envelope["token_source"] == "pat"

    def test_token_source_none_omitted_from_envelope(self):
        """token_source=None → key absent from envelope dict."""
        envelope = _build_envelope(
            persona="developer",
            tenant_id="test-org",
            cognito_sub="user-123",
            actor_user_id="user-123",
            actor_org_id="test-org",
            sender=_human_sender(),
            installation_id=123,
            repo="org/repo",
            payload=_base_payload(),
            correlation_ctx=_base_correlation_ctx(),
            intent_trigger="mentioned",
            intent_label=None,
            token_source=None,
        )
        assert "token_source" not in envelope

    def test_token_source_app_included_in_envelope(self):
        """token_source='app' → key present (explicit App mode)."""
        envelope = _build_envelope(
            persona="developer",
            tenant_id="test-org",
            cognito_sub="user-123",
            actor_user_id="user-123",
            actor_org_id="test-org",
            sender=_human_sender(),
            installation_id=123,
            repo="org/repo",
            payload=_base_payload(),
            correlation_ctx=_base_correlation_ctx(),
            intent_trigger="mentioned",
            intent_label=None,
            token_source="app",
        )
        assert envelope["token_source"] == "app"


class TestSpawnPersonaTokenSource:
    """spawn_persona() passes token_source to envelope."""

    @patch("common.sqs_publisher.publish_envelope")
    @patch("common.spawn_persona._capture_invocation_event")
    @patch("common.spawn_persona._write_pointer_and_provenance")
    @patch("common.spawn_persona._emit_metric")
    def test_spawn_with_token_source_pat(
        self,
        mock_metric,
        mock_write,
        mock_capture,
        mock_publish,
    ):
        """token_source='pat' flows through to the published envelope."""
        mock_publish.return_value = "sqs-msg-id-001"

        result = spawn_persona(
            persona="developer",
            correlation_ctx=_base_correlation_ctx(),
            channel_key="github:repo=org/repo,issue=55",
            resolved_identity=MockResolvedIdentity(),
            tenant_id="test-org",
            actor_user_id="user-123",
            actor_org_id="test-org",
            sender=_human_sender(),
            event_type="issue_comment",
            action="created",
            installation_id=123,
            repo="org/repo",
            payload=_base_payload(),
            intent_trigger="mentioned",
            intent_label=None,
            token_source="pat",
        )

        assert result.success is True
        # Verify the envelope passed to publish_envelope has token_source
        published_envelope = mock_publish.call_args[0][0]
        assert published_envelope["token_source"] == "pat"

    @patch("common.sqs_publisher.publish_envelope")
    @patch("common.spawn_persona._capture_invocation_event")
    @patch("common.spawn_persona._write_pointer_and_provenance")
    @patch("common.spawn_persona._emit_metric")
    def test_spawn_without_token_source(
        self,
        mock_metric,
        mock_write,
        mock_capture,
        mock_publish,
    ):
        """token_source=None → key absent from the published envelope."""
        mock_publish.return_value = "sqs-msg-id-002"

        result = spawn_persona(
            persona="developer",
            correlation_ctx=_base_correlation_ctx(),
            channel_key="github:repo=org/repo,issue=55",
            resolved_identity=MockResolvedIdentity(),
            tenant_id="test-org",
            actor_user_id="user-123",
            actor_org_id="test-org",
            sender=_human_sender(),
            event_type="issue_comment",
            action="created",
            installation_id=123,
            repo="org/repo",
            payload=_base_payload(),
            intent_trigger="mentioned",
            intent_label=None,
            token_source=None,
        )

        assert result.success is True
        published_envelope = mock_publish.call_args[0][0]
        assert "token_source" not in published_envelope
