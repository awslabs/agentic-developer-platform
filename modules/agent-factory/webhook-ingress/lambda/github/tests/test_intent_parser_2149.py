"""Tests for intent_parser.py — Issue #2149 changes.

Tests the dispatch-marker gate for bot comments and the cross-persona loop
guard that catches A->B->A->B alternation.

Issue #2149: Agents self-trigger runs by mentioning @agent-X in comment prose.
Fix: bot comments require adp-dispatch:<persona> marker; windowed cross-persona
loop guard catches alternation patterns.
"""

import sys
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from intent_parser import CROSS_PERSONA_LOOP_THRESHOLD, extract_intent


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
    return {"login": "aws-e-adp-agent-dev[bot]", "id": 900, "type": "Bot"}


def _human_sender():
    return {"login": "alice", "id": 100, "type": "User"}


def _bot_identity(bot_kind="operations"):
    return MockResolvedIdentity(user_kind="bot", bot_kind=bot_kind, user_id="bot-ops-123")


def _human_identity():
    return MockResolvedIdentity(user_kind="human", bot_kind="", user_id="user-alice-456")


# Standard correlation marker WITH dispatch persona
DISPATCH_MARKER = (
    "<!-- adp-correlation:corr-abc-123 adp-root-human:user-456 "
    "adp-is-human-rooted:true adp-invocation:msg-789 "
    "adp-chain-depth:2 adp-dispatch:developer -->"
)

# Standard correlation marker WITHOUT dispatch persona (status/boilerplate)
NO_DISPATCH_MARKER = (
    "<!-- adp-correlation:corr-abc-123 adp-root-human:user-456 "
    "adp-is-human-rooted:true adp-invocation:msg-789 "
    "adp-chain-depth:2 -->"
)


def _base_ctx(chain_depth=1, last_triggered_persona=None):
    """Build a base correlation context for tests."""
    return {
        "correlation_id": "corr-test-001",
        "root_human_id": "user-alice",
        "is_human_rooted": True,
        "is_new_chain": False,
        "chain_depth": chain_depth,
        "last_triggered_persona": last_triggered_persona,
        "recent_triggered_personas": set(),
        "recent_trigger_count": 0,
    }


# --- Dispatch Marker Gate (Story 2) ---


class TestDispatchMarkerGate:
    """Bot comments now require adp-dispatch marker to trigger a run.

    This is THE primary fix for #2149: bare @agent-X in bot-authored prose
    (status headers, plans, implementation summaries) no longer self-triggers.
    """

    @patch("intent_parser._emit_metric")
    def test_bot_comment_with_dispatch_marker_triggers(self, mock_metric):
        """Bot comment with adp-dispatch:developer marker -> Intent(developer)."""
        body = DISPATCH_MARKER + "\n@agent-developer please implement this"
        payload = {
            "action": "created",
            "comment": {"body": body},
            "issue": {"number": 55},
            "sender": _bot_sender(),
            "installation": {"id": 123},
        }
        ctx = _base_ctx(chain_depth=1)
        identity = _bot_identity(bot_kind="operations")
        result = extract_intent(
            "issue_comment", payload, correlation_ctx=ctx, resolved_identity=identity
        )
        assert result is not None
        assert result.persona == "developer"
        assert result.trigger == "mentioned"

    @patch("intent_parser._emit_metric")
    def test_bot_status_comment_without_dispatch_marker_blocked(self, mock_metric):
        """THE BUG: Bot status comment with ## @agent-developer Started -> None.

        This is the exact pattern that caused the #2082 loop.
        """
        body = (
            NO_DISPATCH_MARKER + "\n## @agent-developer Started\n"
            "**Task**: #2082\n**Status**: In Progress"
        )
        payload = {
            "action": "created",
            "comment": {"body": body},
            "issue": {"number": 2082},
            "sender": _bot_sender(),
            "installation": {"id": 123},
        }
        ctx = _base_ctx(chain_depth=1)
        identity = _bot_identity(bot_kind="")
        result = extract_intent(
            "issue_comment", payload, correlation_ctx=ctx, resolved_identity=identity
        )
        assert result is None

    @patch("intent_parser._emit_metric")
    def test_bot_plan_comment_without_dispatch_marker_blocked(self, mock_metric):
        """Bot implementation plan mentioning **Agent**: @agent-developer -> None."""
        body = (
            NO_DISPATCH_MARKER + "\n## Implementation Plan\n"
            "**Agent**: @agent-developer\n"
            "Working on task..."
        )
        payload = {
            "action": "created",
            "comment": {"body": body},
            "issue": {"number": 100},
            "sender": _bot_sender(),
            "installation": {"id": 123},
        }
        ctx = _base_ctx(chain_depth=2)
        identity = _bot_identity(bot_kind="")
        result = extract_intent(
            "issue_comment", payload, correlation_ctx=ctx, resolved_identity=identity
        )
        assert result is None

    @patch("intent_parser._emit_metric")
    def test_bot_comment_bare_mention_no_marker_at_all_blocked(self, mock_metric):
        """Bot comment with bare @agent-X but NO marker at all -> None."""
        body = "Hey @agent-developer can you fix this?"
        payload = {
            "action": "created",
            "comment": {"body": body},
            "issue": {"number": 55},
            "sender": _bot_sender(),
            "installation": {"id": 123},
        }
        ctx = _base_ctx(chain_depth=1)
        identity = _bot_identity(bot_kind="operations")
        result = extract_intent(
            "issue_comment", payload, correlation_ctx=ctx, resolved_identity=identity
        )
        assert result is None

    @patch("intent_parser._emit_metric")
    def test_bot_comment_emits_metric_when_mention_without_marker(self, mock_metric):
        """When bot has @agent-X but no dispatch marker, emit tracking metric."""
        body = NO_DISPATCH_MARKER + "\n## @agent-developer Completed\nDone."
        payload = {
            "action": "created",
            "comment": {"body": body},
            "issue": {"number": 55},
            "sender": _bot_sender(),
            "installation": {"id": 123},
        }
        ctx = _base_ctx()
        identity = _bot_identity(bot_kind="")
        extract_intent("issue_comment", payload, correlation_ctx=ctx, resolved_identity=identity)
        mock_metric.assert_called_with("BotMentionWithoutDispatchMarker", {"persona": "developer"})

    def test_human_comment_still_triggers_from_bare_mention(self):
        """Human @agent-developer -> Intent (unchanged behavior)."""
        body = "Hey @agent-developer can you fix this?"
        payload = {
            "action": "created",
            "comment": {"body": body},
            "issue": {"number": 55},
            "sender": _human_sender(),
            "installation": {"id": 123},
        }
        result = extract_intent("issue_comment", payload)
        assert result is not None
        assert result.persona == "developer"
        assert result.trigger == "mentioned"

    def test_human_comment_no_mention_returns_none(self):
        """Human comment without mention -> None (unchanged)."""
        body = "This looks good, let's merge it."
        payload = {
            "action": "created",
            "comment": {"body": body},
            "issue": {"number": 55},
            "sender": _human_sender(),
            "installation": {"id": 123},
        }
        result = extract_intent("issue_comment", payload)
        assert result is None

    @patch("intent_parser._emit_metric")
    def test_dispatch_marker_reviewer_persona(self, mock_metric):
        """adp-dispatch:reviewer triggers reviewer persona."""
        marker = (
            "<!-- adp-correlation:corr-abc adp-root-human:user-x "
            "adp-is-human-rooted:true adp-dispatch:reviewer -->"
        )
        body = marker + "\n@agent-reviewer please review"
        payload = {
            "action": "created",
            "comment": {"body": body},
            "issue": {"number": 55},
            "sender": _bot_sender(),
            "installation": {"id": 123},
        }
        ctx = _base_ctx(chain_depth=1, last_triggered_persona="developer")
        identity = _bot_identity(bot_kind="developer")
        result = extract_intent(
            "issue_comment", payload, correlation_ctx=ctx, resolved_identity=identity
        )
        assert result is not None
        assert result.persona == "reviewer"

    @patch("intent_parser._emit_metric")
    def test_dispatch_marker_unknown_persona_blocked(self, mock_metric):
        """adp-dispatch with unknown persona -> None."""
        marker = (
            "<!-- adp-correlation:corr-abc adp-root-human:user-x "
            "adp-is-human-rooted:true adp-dispatch:nonexistent -->"
        )
        body = marker + "\n@agent-developer do something"
        payload = {
            "action": "created",
            "comment": {"body": body},
            "issue": {"number": 55},
            "sender": _bot_sender(),
            "installation": {"id": 123},
        }
        ctx = _base_ctx()
        identity = _bot_identity(bot_kind="operations")
        result = extract_intent(
            "issue_comment", payload, correlation_ctx=ctx, resolved_identity=identity
        )
        assert result is None


# --- Cross-persona Loop Guard (Story 4) ---


class TestCrossPersonaLoopGuard:
    """Windowed cross-persona loop guard catches A->B->A->B alternation.

    The scalar last_triggered_persona guard only catches A->A. The new guard
    tracks the SET of all personas triggered in a chain + total count, blocking
    when the target is already in the set AND count >= threshold.
    """

    @patch("intent_parser._emit_metric")
    def test_cross_persona_loop_blocked_at_threshold(self, mock_metric):
        """dev->reviewer->dev at count=4 (threshold) -> blocked."""
        marker = (
            "<!-- adp-correlation:corr-loop adp-root-human:user-x "
            "adp-is-human-rooted:true adp-dispatch:developer -->"
        )
        body = marker + "\n@agent-developer fix the review comments"
        payload = {
            "action": "created",
            "comment": {"body": body},
            "issue": {"number": 55},
            "sender": _bot_sender(),
            "installation": {"id": 123},
        }
        ctx = {
            "correlation_id": "corr-loop",
            "root_human_id": "user-alice",
            "is_human_rooted": True,
            "is_new_chain": False,
            "chain_depth": 5,
            "last_triggered_persona": "reviewer",  # Different from target
            "recent_triggered_personas": {"developer", "reviewer"},
            "recent_trigger_count": CROSS_PERSONA_LOOP_THRESHOLD,  # At threshold
        }
        identity = _bot_identity(bot_kind="reviewer")
        result = extract_intent(
            "issue_comment", payload, correlation_ctx=ctx, resolved_identity=identity
        )
        assert result is None
        mock_metric.assert_called_with("CrossPersonaLoopBlocked", {"persona": "developer"})

    @patch("intent_parser._emit_metric")
    def test_cross_persona_loop_blocked_above_threshold(self, mock_metric):
        """Trigger count above threshold -> blocked."""
        marker = (
            "<!-- adp-correlation:corr-loop adp-root-human:user-x "
            "adp-is-human-rooted:true adp-dispatch:developer -->"
        )
        body = marker + "\n@agent-developer fix this"
        payload = {
            "action": "created",
            "comment": {"body": body},
            "issue": {"number": 55},
            "sender": _bot_sender(),
            "installation": {"id": 123},
        }
        ctx = {
            "correlation_id": "corr-loop",
            "root_human_id": "user-alice",
            "is_human_rooted": True,
            "is_new_chain": False,
            "chain_depth": 7,
            "last_triggered_persona": "reviewer",
            "recent_triggered_personas": {"developer", "reviewer", "architect"},
            "recent_trigger_count": 6,
        }
        identity = _bot_identity(bot_kind="reviewer")
        result = extract_intent(
            "issue_comment", payload, correlation_ctx=ctx, resolved_identity=identity
        )
        assert result is None

    @patch("intent_parser._emit_metric")
    def test_legitimate_chain_below_threshold_allowed(self, mock_metric):
        """human->dev->reviewer (count=2, below threshold) -> allowed."""
        marker = (
            "<!-- adp-correlation:corr-ok adp-root-human:user-x "
            "adp-is-human-rooted:true adp-dispatch:reviewer -->"
        )
        body = marker + "\n@agent-reviewer please review"
        payload = {
            "action": "created",
            "comment": {"body": body},
            "issue": {"number": 55},
            "sender": _bot_sender(),
            "installation": {"id": 123},
        }
        ctx = {
            "correlation_id": "corr-ok",
            "root_human_id": "user-alice",
            "is_human_rooted": True,
            "is_new_chain": False,
            "chain_depth": 2,
            "last_triggered_persona": "developer",
            "recent_triggered_personas": {"developer"},
            "recent_trigger_count": 1,
        }
        identity = _bot_identity(bot_kind="developer")
        result = extract_intent(
            "issue_comment", payload, correlation_ctx=ctx, resolved_identity=identity
        )
        assert result is not None
        assert result.persona == "reviewer"

    @patch("intent_parser._emit_metric")
    def test_new_persona_at_threshold_allowed(self, mock_metric):
        """New persona not in set, even at threshold -> allowed."""
        marker = (
            "<!-- adp-correlation:corr-ok adp-root-human:user-x "
            "adp-is-human-rooted:true adp-dispatch:architect -->"
        )
        body = marker + "\n@agent-architect review design"
        payload = {
            "action": "created",
            "comment": {"body": body},
            "issue": {"number": 55},
            "sender": _bot_sender(),
            "installation": {"id": 123},
        }
        ctx = {
            "correlation_id": "corr-ok",
            "root_human_id": "user-alice",
            "is_human_rooted": True,
            "is_new_chain": False,
            "chain_depth": 5,
            "last_triggered_persona": "reviewer",
            "recent_triggered_personas": {"developer", "reviewer"},
            "recent_trigger_count": CROSS_PERSONA_LOOP_THRESHOLD,
        }
        identity = _bot_identity(bot_kind="reviewer")
        result = extract_intent(
            "issue_comment", payload, correlation_ctx=ctx, resolved_identity=identity
        )
        assert result is not None
        assert result.persona == "architect"

    @patch("intent_parser._emit_metric")
    def test_empty_recent_set_allows_any_dispatch(self, mock_metric):
        """Fresh chain with empty recent set -> any dispatch allowed."""
        marker = (
            "<!-- adp-correlation:corr-fresh adp-root-human:user-x "
            "adp-is-human-rooted:true adp-dispatch:developer -->"
        )
        body = marker + "\n@agent-developer implement this"
        payload = {
            "action": "created",
            "comment": {"body": body},
            "issue": {"number": 55},
            "sender": _bot_sender(),
            "installation": {"id": 123},
        }
        ctx = {
            "correlation_id": "corr-fresh",
            "root_human_id": "user-alice",
            "is_human_rooted": True,
            "is_new_chain": True,
            "chain_depth": 0,
            "last_triggered_persona": None,
            "recent_triggered_personas": set(),
            "recent_trigger_count": 0,
        }
        identity = _bot_identity(bot_kind="pm")
        result = extract_intent(
            "issue_comment", payload, correlation_ctx=ctx, resolved_identity=identity
        )
        assert result is not None
        assert result.persona == "developer"

    @patch("intent_parser._emit_metric")
    def test_three_hop_chain_allowed(self, mock_metric):
        """human->dev->reviewer->dev (count=3, below threshold=4) -> allowed.

        This is the deepest legitimate pattern and must NOT be blocked.
        """
        marker = (
            "<!-- adp-correlation:corr-3hop adp-root-human:user-x "
            "adp-is-human-rooted:true adp-dispatch:developer -->"
        )
        body = marker + "\n@agent-developer fix review comments"
        payload = {
            "action": "created",
            "comment": {"body": body},
            "issue": {"number": 55},
            "sender": _bot_sender(),
            "installation": {"id": 123},
        }
        ctx = {
            "correlation_id": "corr-3hop",
            "root_human_id": "user-alice",
            "is_human_rooted": True,
            "is_new_chain": False,
            "chain_depth": 3,
            "last_triggered_persona": "reviewer",
            "recent_triggered_personas": {"developer", "reviewer"},
            "recent_trigger_count": 3,  # Below threshold (4)
        }
        identity = _bot_identity(bot_kind="reviewer")
        result = extract_intent(
            "issue_comment", payload, correlation_ctx=ctx, resolved_identity=identity
        )
        assert result is not None
        assert result.persona == "developer"


# --- Existing guards still work with dispatch marker ---


class TestExistingGuardsWithDispatchMarker:
    """Verify existing guards (self-mention, self-re-trigger, depth) still fire
    even when a dispatch marker is present.
    """

    @patch("intent_parser._emit_metric")
    def test_self_mention_still_blocked_with_dispatch_marker(self, mock_metric):
        """Bot dispatching to its own persona -> blocked."""
        marker = (
            "<!-- adp-correlation:corr-self adp-root-human:user-x "
            "adp-is-human-rooted:true adp-dispatch:operations -->"
        )
        body = marker + "\n@agent-operations re-run"
        payload = {
            "action": "created",
            "comment": {"body": body},
            "issue": {"number": 55},
            "sender": _bot_sender(),
            "installation": {"id": 123},
        }
        ctx = _base_ctx(chain_depth=0)
        identity = _bot_identity(bot_kind="operations")
        result = extract_intent(
            "issue_comment", payload, correlation_ctx=ctx, resolved_identity=identity
        )
        assert result is None

    @patch("intent_parser._emit_metric")
    def test_self_retrigger_still_blocked_with_dispatch_marker(self, mock_metric):
        """Dispatch targeting last_triggered_persona -> blocked."""
        marker = (
            "<!-- adp-correlation:corr-retrig adp-root-human:user-x "
            "adp-is-human-rooted:true adp-dispatch:developer -->"
        )
        body = marker + "\n@agent-developer fix"
        payload = {
            "action": "created",
            "comment": {"body": body},
            "issue": {"number": 55},
            "sender": _bot_sender(),
            "installation": {"id": 123},
        }
        ctx = _base_ctx(chain_depth=2, last_triggered_persona="developer")
        identity = _bot_identity(bot_kind="")
        result = extract_intent(
            "issue_comment", payload, correlation_ctx=ctx, resolved_identity=identity
        )
        assert result is None

    @patch("intent_parser._emit_metric")
    def test_depth_guard_still_blocks_with_dispatch_marker(self, mock_metric):
        """Dispatch at depth >= MAX -> blocked."""
        marker = (
            "<!-- adp-correlation:corr-deep adp-root-human:user-x "
            "adp-is-human-rooted:true adp-dispatch:developer -->"
        )
        body = marker + "\n@agent-developer implement"
        payload = {
            "action": "created",
            "comment": {"body": body},
            "issue": {"number": 55},
            "sender": _bot_sender(),
            "installation": {"id": 123},
        }
        ctx = _base_ctx(chain_depth=8)  # MAX_CHAIN_DEPTH default
        identity = _bot_identity(bot_kind="operations")
        result = extract_intent(
            "issue_comment", payload, correlation_ctx=ctx, resolved_identity=identity
        )
        assert result is None

    @patch("intent_parser._emit_metric")
    def test_no_correlation_ctx_blocks_with_dispatch_marker(self, mock_metric):
        """Bot with dispatch marker but no correlation ctx -> blocked (safe default)."""
        body = DISPATCH_MARKER + "\n@agent-developer do this"
        payload = {
            "action": "created",
            "comment": {"body": body},
            "issue": {"number": 55},
            "sender": _bot_sender(),
            "installation": {"id": 123},
        }
        result = extract_intent(
            "issue_comment", payload, correlation_ctx=None, resolved_identity=_bot_identity()
        )
        assert result is None


# --- Regression: the self-trigger loop scenario ---


class TestSelfTriggerLoopScenario:
    """End-to-end scenario from the #2082 bug report.

    The loop was: developer posts status comment with `## @agent-developer Started`
    -> webhook parses the mention -> spawns developer -> developer posts another
    status -> repeat.
    """

    @patch("intent_parser._emit_metric")
    def test_developer_started_comment_no_dispatch(self, mock_metric):
        """Exact pattern from the bug: developer's 'Started' status comment."""
        body = (
            "<!-- adp-correlation:7c382ffb adp-root-human:650f093f "
            "adp-is-human-rooted:true -->\n"
            "## @agent-developer Started\n\n"
            "**Task**: #2082 - EPIC description\n"
            "**Status**: In Progress\n"
            "**Started**: 2026-06-26T17:48:22.594Z\n\n"
            "Working on this task..."
        )
        payload = {
            "action": "created",
            "comment": {"body": body},
            "issue": {"number": 2082},
            "sender": _bot_sender(),
            "installation": {"id": 123},
        }
        ctx = {
            "correlation_id": "7c382ffb",
            "root_human_id": "650f093f",
            "is_human_rooted": True,
            "is_new_chain": False,
            "chain_depth": 1,
            "last_triggered_persona": "developer",
            "recent_triggered_personas": {"developer"},
            "recent_trigger_count": 1,
        }
        identity = _bot_identity(bot_kind="")
        result = extract_intent(
            "issue_comment", payload, correlation_ctx=ctx, resolved_identity=identity
        )
        assert result is None, "Status comment must NOT self-trigger"

    @patch("intent_parser._emit_metric")
    def test_architect_completed_comment_no_dispatch(self, mock_metric):
        """Architect's 'Completed' comment mentioning @agent-architect."""
        body = (
            "<!-- adp-correlation:abc123 adp-root-human:user-x "
            "adp-is-human-rooted:true -->\n"
            "## @agent-architect Completed\n\n"
            "**Task**: #2149\n**Status**: Done"
        )
        payload = {
            "action": "created",
            "comment": {"body": body},
            "issue": {"number": 2149},
            "sender": _bot_sender(),
            "installation": {"id": 123},
        }
        ctx = _base_ctx(chain_depth=1)
        identity = _bot_identity(bot_kind="architect")
        result = extract_intent(
            "issue_comment", payload, correlation_ctx=ctx, resolved_identity=identity
        )
        assert result is None

    @patch("intent_parser._emit_metric")
    def test_cross_persona_alternation_scenario(self, mock_metric):
        """Simulated dev->reviewer->dev->reviewer loop (the #2082 pattern).

        After 4 bot dispatches (threshold), the 5th attempt to trigger a persona
        already in the set is blocked.
        """
        marker = (
            "<!-- adp-correlation:corr-epic adp-root-human:user-x "
            "adp-is-human-rooted:true adp-dispatch:developer -->"
        )
        body = marker + "\n@agent-developer fix the review comments"
        payload = {
            "action": "created",
            "comment": {"body": body},
            "issue": {"number": 2082},
            "sender": _bot_sender(),
            "installation": {"id": 123},
        }
        # Simulates: human->dev->reviewer->dev->reviewer (4 bot dispatches)
        # Now reviewer tries to dispatch developer again (5th)
        ctx = {
            "correlation_id": "corr-epic",
            "root_human_id": "user-alice",
            "is_human_rooted": True,
            "is_new_chain": False,
            "chain_depth": 5,
            "last_triggered_persona": "reviewer",  # Different, so #1716 passes
            "recent_triggered_personas": {"developer", "reviewer"},
            "recent_trigger_count": 4,  # At threshold
        }
        identity = _bot_identity(bot_kind="reviewer")
        result = extract_intent(
            "issue_comment", payload, correlation_ctx=ctx, resolved_identity=identity
        )
        assert result is None, "Cross-persona loop must be caught at threshold"


# --- Configuration ---


class TestConfiguration:
    """Verify env-configurable threshold."""

    def test_cross_persona_threshold_default_is_4(self):
        """Default threshold is 4 (allows 3 legit hops)."""
        assert CROSS_PERSONA_LOOP_THRESHOLD == 4
