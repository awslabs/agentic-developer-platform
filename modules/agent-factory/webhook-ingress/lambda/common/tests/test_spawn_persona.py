"""Tests for common/spawn_persona.py — Issue #2151.

Tests the shared spawn_persona() function's guard logic, envelope shape,
persona validation, and the SpawnResult contract.
"""

import sys
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

# Add lambda root to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from common.personas import VALID_PERSONAS
from common.spawn_persona import (
    CROSS_PERSONA_LOOP_THRESHOLD,
    MAX_CHAIN_DEPTH,
    spawn_persona,
)


@dataclass
class MockResolvedIdentity:
    """Minimal mock of ResolvedIdentity for test purposes."""

    tenant_id: str = "test-org"
    org_id: str = "test-org"
    user_id: str = "user-123"
    user_provisioning_mode: str = "strict"
    user_kind: str = "human"
    bot_kind: str = ""


def _bot_sender():
    return {"login": "aws-e-adp-agent-ops[bot]", "id": 900, "type": "Bot"}


def _human_sender():
    return {"login": "alice", "id": 100, "type": "User"}


def _bot_identity(bot_kind="operations"):
    return MockResolvedIdentity(
        user_kind="bot", bot_kind=bot_kind, user_id="bot-ops-123"
    )


def _human_identity():
    return MockResolvedIdentity(
        user_kind="human", bot_kind="", user_id="user-alice-456"
    )


def _base_correlation_ctx(chain_depth=1, last_triggered_persona=None):
    return {
        "correlation_id": "corr-test-001",
        "root_human_id": "user-alice",
        "triggered_by": None,
        "is_human_rooted": True,
        "is_new_chain": False,
        "parent_invocation_id": None,
        "chain_depth": chain_depth,
        "last_triggered_persona": last_triggered_persona,
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


def _spawn_kwargs(**overrides):
    """Build default spawn_persona kwargs, overridable."""
    defaults = {
        "persona": "developer",
        "correlation_ctx": _base_correlation_ctx(),
        "channel_key": "github:repo=org/repo,issue=55",
        "resolved_identity": _human_identity(),
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


# --- Persona Validation ---


class TestPersonaValidation:
    """spawn_persona rejects unknown personas."""

    @patch("common.spawn_persona._emit_metric")
    @patch("common.spawn_persona._write_pointer_and_provenance")
    @patch("common.spawn_persona._capture_invocation_event")
    @patch("common.sqs_publisher.publish_envelope", return_value="msg-123")
    def test_valid_persona_accepted(
        self, mock_sqs, mock_capture, mock_write, mock_metric
    ):
        result = spawn_persona(**_spawn_kwargs(persona="developer"))
        assert result.success is True
        assert result.message_id is not None

    @patch("common.spawn_persona._emit_metric")
    def test_unknown_persona_blocked(self, mock_metric):
        result = spawn_persona(**_spawn_kwargs(persona="nonexistent"))
        assert result.success is False
        assert result.block_reason == "unknown_persona"
        mock_metric.assert_called_with(
            "UnknownPersonaBlocked", {"persona": "nonexistent"}
        )

    def test_valid_personas_set_covers_all_mappings(self):
        """VALID_PERSONAS includes all mention and label persona targets."""
        from common.personas import LABEL_TO_PERSONA, MENTION_TO_PERSONA

        for persona in MENTION_TO_PERSONA.values():
            assert persona in VALID_PERSONAS
        for persona in LABEL_TO_PERSONA.values():
            assert persona in VALID_PERSONAS


# --- Self-Mention Guard ---


class TestSelfMentionGuard:
    """Bot dispatching to its own persona is blocked."""

    @patch("common.spawn_persona._emit_metric")
    def test_self_mention_blocked(self, mock_metric):
        """Bot with bot_kind='operations' dispatching to 'operations' -> blocked."""
        result = spawn_persona(
            **_spawn_kwargs(
                persona="operations",
                sender=_bot_sender(),
                resolved_identity=_bot_identity(bot_kind="operations"),
            )
        )
        assert result.success is False
        assert result.block_reason == "self_mention"
        mock_metric.assert_called_with("SelfMentionBlocked", {"persona": "operations"})

    @patch("common.spawn_persona._emit_metric")
    @patch("common.spawn_persona._write_pointer_and_provenance")
    @patch("common.spawn_persona._capture_invocation_event")
    @patch("common.sqs_publisher.publish_envelope", return_value="msg-456")
    def test_cross_persona_allowed(
        self, mock_sqs, mock_capture, mock_write, mock_metric
    ):
        """Bot with bot_kind='operations' dispatching to 'developer' -> allowed."""
        result = spawn_persona(
            **_spawn_kwargs(
                persona="developer",
                sender=_bot_sender(),
                resolved_identity=_bot_identity(bot_kind="operations"),
            )
        )
        assert result.success is True


# --- Self-Re-Trigger Guard ---


class TestSelfReTriggerGuard:
    """Block when persona == last_triggered_persona on channel."""

    @patch("common.spawn_persona._emit_metric")
    def test_self_retrigger_blocked(self, mock_metric):
        """Targeting 'developer' when last_triggered was 'developer' -> blocked."""
        ctx = _base_correlation_ctx(chain_depth=1, last_triggered_persona="developer")
        result = spawn_persona(
            **_spawn_kwargs(
                persona="developer",
                sender=_bot_sender(),
                resolved_identity=_bot_identity(bot_kind=""),
                correlation_ctx=ctx,
            )
        )
        assert result.success is False
        assert result.block_reason == "self_re_trigger"
        mock_metric.assert_called_with("SelfReTriggerBlocked", {"persona": "developer"})

    @patch("common.spawn_persona._emit_metric")
    @patch("common.spawn_persona._write_pointer_and_provenance")
    @patch("common.spawn_persona._capture_invocation_event")
    @patch("common.sqs_publisher.publish_envelope", return_value="msg-789")
    def test_different_persona_allowed(
        self, mock_sqs, mock_capture, mock_write, mock_metric
    ):
        """Targeting 'developer' when last_triggered was 'reviewer' -> allowed."""
        ctx = _base_correlation_ctx(chain_depth=2, last_triggered_persona="reviewer")
        result = spawn_persona(
            **_spawn_kwargs(
                persona="developer",
                sender=_bot_sender(),
                resolved_identity=_bot_identity(bot_kind=""),
                correlation_ctx=ctx,
            )
        )
        assert result.success is True


# --- Cross-Persona Loop Guard ---


class TestCrossPersonaLoopGuard:
    """Catch A->B->A->B alternation at threshold."""

    @patch("common.spawn_persona._emit_metric")
    def test_cross_persona_loop_blocked_at_threshold(self, mock_metric):
        """dev->reviewer->dev at count=4 (threshold) -> blocked."""
        ctx = _base_correlation_ctx(chain_depth=5, last_triggered_persona="reviewer")
        ctx["recent_triggered_personas"] = {"developer", "reviewer"}
        ctx["recent_trigger_count"] = CROSS_PERSONA_LOOP_THRESHOLD
        result = spawn_persona(
            **_spawn_kwargs(
                persona="developer",
                sender=_bot_sender(),
                resolved_identity=_bot_identity(bot_kind="reviewer"),
                correlation_ctx=ctx,
            )
        )
        assert result.success is False
        assert result.block_reason == "cross_persona_loop"

    @patch("common.spawn_persona._emit_metric")
    @patch("common.spawn_persona._write_pointer_and_provenance")
    @patch("common.spawn_persona._capture_invocation_event")
    @patch("common.sqs_publisher.publish_envelope", return_value="msg-loop")
    def test_below_threshold_allowed(
        self, mock_sqs, mock_capture, mock_write, mock_metric
    ):
        """count=3 (below threshold=4) -> allowed."""
        ctx = _base_correlation_ctx(chain_depth=3, last_triggered_persona="reviewer")
        ctx["recent_triggered_personas"] = {"developer", "reviewer"}
        ctx["recent_trigger_count"] = 3
        result = spawn_persona(
            **_spawn_kwargs(
                persona="developer",
                sender=_bot_sender(),
                resolved_identity=_bot_identity(bot_kind="reviewer"),
                correlation_ctx=ctx,
            )
        )
        assert result.success is True

    @patch("common.spawn_persona._emit_metric")
    @patch("common.spawn_persona._write_pointer_and_provenance")
    @patch("common.spawn_persona._capture_invocation_event")
    @patch("common.sqs_publisher.publish_envelope", return_value="msg-new")
    def test_new_persona_at_threshold_allowed(
        self, mock_sqs, mock_capture, mock_write, mock_metric
    ):
        """New persona not in set, even at threshold -> allowed."""
        ctx = _base_correlation_ctx(chain_depth=5, last_triggered_persona="reviewer")
        ctx["recent_triggered_personas"] = {"developer", "reviewer"}
        ctx["recent_trigger_count"] = CROSS_PERSONA_LOOP_THRESHOLD
        result = spawn_persona(
            **_spawn_kwargs(
                persona="architect",
                sender=_bot_sender(),
                resolved_identity=_bot_identity(bot_kind="reviewer"),
                correlation_ctx=ctx,
            )
        )
        assert result.success is True


# --- Depth Guard ---


class TestDepthGuard:
    """Block at MAX_CHAIN_DEPTH."""

    @patch("common.spawn_persona._emit_metric")
    def test_depth_at_max_blocked(self, mock_metric):
        """chain_depth == MAX_CHAIN_DEPTH -> blocked."""
        ctx = _base_correlation_ctx(chain_depth=MAX_CHAIN_DEPTH)
        result = spawn_persona(
            **_spawn_kwargs(
                persona="developer",
                sender=_bot_sender(),
                resolved_identity=_bot_identity(bot_kind="operations"),
                correlation_ctx=ctx,
            )
        )
        assert result.success is False
        assert result.block_reason == "chain_depth_exceeded"

    @patch("common.spawn_persona._emit_metric")
    @patch("common.spawn_persona._write_pointer_and_provenance")
    @patch("common.spawn_persona._capture_invocation_event")
    @patch("common.sqs_publisher.publish_envelope", return_value="msg-depth")
    def test_depth_below_max_allowed(
        self, mock_sqs, mock_capture, mock_write, mock_metric
    ):
        """chain_depth = 7 (one below max=8) -> allowed."""
        ctx = _base_correlation_ctx(chain_depth=7)
        result = spawn_persona(
            **_spawn_kwargs(
                persona="developer",
                sender=_bot_sender(),
                resolved_identity=_bot_identity(bot_kind="operations"),
                correlation_ctx=ctx,
            )
        )
        assert result.success is True

    @patch("common.spawn_persona._emit_metric")
    @patch("common.spawn_persona._write_pointer_and_provenance")
    @patch("common.spawn_persona._capture_invocation_event")
    @patch("common.sqs_publisher.publish_envelope", return_value="msg-human")
    def test_human_sender_ignores_depth(
        self, mock_sqs, mock_capture, mock_write, mock_metric
    ):
        """Human senders are never gated by depth."""
        ctx = _base_correlation_ctx(chain_depth=MAX_CHAIN_DEPTH + 100)
        result = spawn_persona(
            **_spawn_kwargs(
                persona="developer",
                sender=_human_sender(),
                resolved_identity=_human_identity(),
                correlation_ctx=ctx,
            )
        )
        assert result.success is True


# --- Envelope Shape ---


class TestEnvelopeShape:
    """Verify the envelope matches the pre-refactor golden shape."""

    @patch("common.spawn_persona._emit_metric")
    @patch("common.spawn_persona._write_pointer_and_provenance")
    @patch("common.spawn_persona._capture_invocation_event")
    @patch("common.sqs_publisher.publish_envelope")
    def test_envelope_has_required_keys(
        self, mock_sqs, mock_capture, mock_write, mock_metric
    ):
        """Envelope passed to SQS has all required top-level keys."""
        mock_sqs.return_value = "msg-envelope"
        spawn_persona(**_spawn_kwargs())

        assert mock_sqs.called
        envelope = mock_sqs.call_args[0][0]
        required_keys = {
            "version",
            "channel",
            "tenant_id",
            "cognito_sub",
            "persona",
            "actor",
            "source_ref",
            "intent",
            "correlation",
            "payload",
            "arrived_at",
            "message_id",
        }
        assert required_keys.issubset(envelope.keys())

    @patch("common.spawn_persona._emit_metric")
    @patch("common.spawn_persona._write_pointer_and_provenance")
    @patch("common.spawn_persona._capture_invocation_event")
    @patch("common.sqs_publisher.publish_envelope")
    def test_envelope_actor_shape(
        self, mock_sqs, mock_capture, mock_write, mock_metric
    ):
        """Actor sub-object has required fields."""
        mock_sqs.return_value = "msg-actor"
        spawn_persona(**_spawn_kwargs())

        envelope = mock_sqs.call_args[0][0]
        actor = envelope["actor"]
        assert "user_id" in actor
        assert "org_id" in actor
        assert "github_id" in actor
        assert "github_login" in actor
        assert "is_bot" in actor

    @patch("common.spawn_persona._emit_metric")
    @patch("common.spawn_persona._write_pointer_and_provenance")
    @patch("common.spawn_persona._capture_invocation_event")
    @patch("common.sqs_publisher.publish_envelope")
    def test_envelope_correlation_shape(
        self, mock_sqs, mock_capture, mock_write, mock_metric
    ):
        """Correlation sub-object has required fields."""
        mock_sqs.return_value = "msg-corr"
        ctx = _base_correlation_ctx(chain_depth=2)
        ctx["parent_invocation_id"] = "parent-abc"
        spawn_persona(**_spawn_kwargs(correlation_ctx=ctx))

        envelope = mock_sqs.call_args[0][0]
        corr = envelope["correlation"]
        assert corr["correlation_id"] == "corr-test-001"
        assert corr["root_human_id"] == "user-alice"
        assert corr["is_human_rooted"] is True
        assert corr["parent_invocation_id"] == "parent-abc"
        assert corr["chain_depth"] == 2

    @patch("common.spawn_persona._emit_metric")
    @patch("common.spawn_persona._write_pointer_and_provenance")
    @patch("common.spawn_persona._capture_invocation_event")
    @patch("common.sqs_publisher.publish_envelope")
    def test_envelope_persona_matches_intent(
        self, mock_sqs, mock_capture, mock_write, mock_metric
    ):
        """Top-level persona and intent.persona both match the requested persona."""
        mock_sqs.return_value = "msg-p"
        spawn_persona(**_spawn_kwargs(persona="reviewer", intent_trigger="pr_opened"))

        envelope = mock_sqs.call_args[0][0]
        assert envelope["persona"] == "reviewer"
        assert envelope["intent"]["persona"] == "reviewer"
        assert envelope["intent"]["trigger"] == "pr_opened"


# --- SQS Failure ---


class TestSQSFailure:
    """spawn_persona reports failure when SQS publish fails."""

    @patch("common.spawn_persona._emit_metric")
    @patch("common.spawn_persona._write_pointer_and_provenance")
    @patch("common.spawn_persona._capture_invocation_event")
    @patch("common.sqs_publisher.publish_envelope", return_value=None)
    def test_sqs_failure_returns_error(
        self, mock_sqs, mock_capture, mock_write, mock_metric
    ):
        result = spawn_persona(**_spawn_kwargs())
        assert result.success is False
        assert result.block_reason == "sqs_publish_failed"


# --- Human Sender Bypasses Bot Guards ---


class TestHumanBypass:
    """Human senders skip all bot guards."""

    @patch("common.spawn_persona._emit_metric")
    @patch("common.spawn_persona._write_pointer_and_provenance")
    @patch("common.spawn_persona._capture_invocation_event")
    @patch("common.sqs_publisher.publish_envelope", return_value="msg-human")
    def test_human_ignores_all_guards(
        self, mock_sqs, mock_capture, mock_write, mock_metric
    ):
        """Human at MAX depth + self-retrigger + in recent set -> still allowed."""
        ctx = _base_correlation_ctx(
            chain_depth=MAX_CHAIN_DEPTH + 5,
            last_triggered_persona="developer",
        )
        ctx["recent_triggered_personas"] = {"developer"}
        ctx["recent_trigger_count"] = 100
        result = spawn_persona(
            **_spawn_kwargs(
                persona="developer",
                sender=_human_sender(),
                resolved_identity=_human_identity(),
                correlation_ctx=ctx,
            )
        )
        assert result.success is True


# --- Configuration ---


class TestConfiguration:
    """Verify env-configurable thresholds."""

    def test_max_chain_depth_default_is_8(self):
        assert MAX_CHAIN_DEPTH == 8

    def test_cross_persona_threshold_default_is_4(self):
        assert CROSS_PERSONA_LOOP_THRESHOLD == 4
