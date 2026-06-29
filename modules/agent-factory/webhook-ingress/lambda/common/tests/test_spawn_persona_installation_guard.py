"""Tests for spawn_persona() installation_id guard — Issue #2336.

Tests that spawn_persona rejects dispatches with installation_id=0/None
before they reach SQS, preventing guaranteed-to-crash messages from
entering the FIFO queue.
"""

import sys
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

# Add lambda root to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from common.spawn_persona import spawn_persona


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
        "chain_depth": 1,
        "last_triggered_persona": None,
        "recent_triggered_personas": set(),
        "recent_trigger_count": 0,
    }


def _base_payload():
    return {
        "action": "created",
        "issue": {
            "number": 55,
            "title": "Test issue",
            "html_url": "https://github.com/org/repo/issues/55",
        },
        "repository": {"full_name": "org/repo"},
        "sender": _human_sender(),
        "installation": {"id": 123},
    }


def _spawn_kwargs(**overrides):
    """Build default spawn_persona kwargs, overridable."""
    defaults = {
        "persona": "developer",
        "correlation_ctx": _base_correlation_ctx(),
        "channel_key": "github:repo=org/repo,issue=55",
        "resolved_identity": MockResolvedIdentity(),
        "tenant_id": "test-org",
        "actor_user_id": "user-alice-456",
        "actor_org_id": "test-org",
        "sender": _human_sender(),
        "event_type": "issue_comment",
        "action": "created",
        "installation_id": 123,
        "repo": "org/repo",
        "payload": _base_payload(),
        "intent_trigger": "mentioned",
        "intent_label": None,
    }
    defaults.update(overrides)
    return defaults


class TestInstallationIdGuard:
    """spawn_persona rejects installation_id=0/None (Issue #2336)."""

    @patch("common.spawn_persona._emit_metric")
    def test_installation_id_zero_blocked(self, mock_metric):
        """installation_id=0 is rejected with block_reason=invalid_installation_id."""
        result = spawn_persona(**_spawn_kwargs(installation_id=0))
        assert result.success is False
        assert result.block_reason == "invalid_installation_id"
        mock_metric.assert_called_once_with(
            "InvalidInstallationIdBlocked", {"persona": "developer"}
        )

    @patch("common.spawn_persona._emit_metric")
    def test_installation_id_none_blocked(self, mock_metric):
        """installation_id=None is rejected pre-publish."""
        result = spawn_persona(**_spawn_kwargs(installation_id=None))
        assert result.success is False
        assert result.block_reason == "invalid_installation_id"

    @patch("common.spawn_persona._emit_metric")
    @patch("common.spawn_persona._write_pointer_and_provenance")
    @patch("common.spawn_persona._capture_invocation_event")
    @patch("common.sqs_publisher.publish_envelope", return_value="msg-123")
    def test_valid_installation_id_passes(
        self, mock_sqs, mock_capture, mock_write, mock_metric
    ):
        """A valid non-zero installation_id passes the guard."""
        result = spawn_persona(**_spawn_kwargs(installation_id=124731131))
        assert result.success is True
        assert result.message_id == "msg-123"

    @patch("common.spawn_persona._emit_metric")
    @patch("common.spawn_persona._write_pointer_and_provenance")
    @patch("common.spawn_persona._capture_invocation_event")
    @patch("common.sqs_publisher.publish_envelope", return_value="msg-123")
    def test_event_type_test_bypasses_guard(
        self, mock_sqs, mock_capture, mock_write, mock_metric
    ):
        """event_type='test' bypasses the installation_id guard."""
        result = spawn_persona(**_spawn_kwargs(installation_id=0, event_type="test"))
        assert result.success is True
        assert result.message_id == "msg-123"

    @patch("common.spawn_persona._emit_metric")
    def test_eventbridge_event_type_with_zero_blocked(self, mock_metric):
        """EventBridge dispatches with installation_id=0 are blocked."""
        result = spawn_persona(
            **_spawn_kwargs(installation_id=0, event_type="eventbridge")
        )
        assert result.success is False
        assert result.block_reason == "invalid_installation_id"

    @patch("common.spawn_persona._emit_metric")
    def test_agent_trigger_event_type_with_zero_blocked(self, mock_metric):
        """Agent-trigger dispatches with installation_id=0 are blocked."""
        result = spawn_persona(
            **_spawn_kwargs(installation_id=0, event_type="agent_trigger")
        )
        assert result.success is False
        assert result.block_reason == "invalid_installation_id"
