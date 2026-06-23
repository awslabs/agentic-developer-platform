"""Tests for intent_parser.py — Issue #1696 changes.

Tests the depth-only loop guard, branch filter, synchronize gate,
marker-gated bot-guard relaxation for PR events, and fan-out.
"""

import sys
from dataclasses import dataclass
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from intent_parser import MAX_CHAIN_DEPTH, extract_intent


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


# Marker that has_valid_marker will recognize
VALID_MARKER = "<!-- adp-correlation:corr-123 adp-root-human:user-456 adp-is-human-rooted:true adp-invocation:msg-789 adp-chain-depth:2 -->"


# --- Depth-only guard tests ---


class TestDepthOnlyGuard:
    """Test the depth-only loop guard (replaces is_new_chain)."""

    def _make_comment_payload(self, mention="@agent-developer", sender=None):
        return {
            "action": "created",
            "comment": {"body": f"Hey {mention} please fix this"},
            "issue": {"number": 55},
            "sender": sender or _bot_sender(),
            "installation": {"id": 123},
        }

    def test_bot_allowed_when_depth_below_max(self):
        """Bot mention at depth < MAX_CHAIN_DEPTH should be allowed."""
        payload = self._make_comment_payload()
        ctx = {
            "correlation_id": "corr-001",
            "root_human_id": "user-alice",
            "is_new_chain": False,
            "chain_depth": 3,
        }
        identity = _bot_identity("operations")
        result = extract_intent(
            "issue_comment", payload, correlation_ctx=ctx, resolved_identity=identity
        )
        assert result is not None
        assert result.persona == "developer"

    def test_bot_blocked_when_depth_at_max(self):
        """Bot mention at depth == MAX_CHAIN_DEPTH should be blocked."""
        payload = self._make_comment_payload()
        ctx = {
            "correlation_id": "corr-001",
            "root_human_id": "user-alice",
            "is_new_chain": False,
            "chain_depth": MAX_CHAIN_DEPTH,
        }
        identity = _bot_identity("operations")
        result = extract_intent(
            "issue_comment", payload, correlation_ctx=ctx, resolved_identity=identity
        )
        assert result is None

    def test_bot_blocked_when_depth_above_max(self):
        """Bot mention at depth > MAX_CHAIN_DEPTH should be blocked."""
        payload = self._make_comment_payload()
        ctx = {
            "correlation_id": "corr-001",
            "root_human_id": "user-alice",
            "is_new_chain": False,
            "chain_depth": MAX_CHAIN_DEPTH + 5,
        }
        identity = _bot_identity("operations")
        result = extract_intent(
            "issue_comment", payload, correlation_ctx=ctx, resolved_identity=identity
        )
        assert result is None

    def test_bot_allowed_at_depth_zero(self):
        """Bot at depth 0 (first hop) should be allowed."""
        payload = self._make_comment_payload()
        ctx = {
            "correlation_id": "corr-001",
            "root_human_id": "user-alice",
            "is_new_chain": True,
            "chain_depth": 0,
        }
        identity = _bot_identity("operations")
        result = extract_intent(
            "issue_comment", payload, correlation_ctx=ctx, resolved_identity=identity
        )
        assert result is not None
        assert result.persona == "developer"

    def test_bot_allowed_at_depth_seven(self):
        """Bot at depth 7 (one below max) should be allowed."""
        payload = self._make_comment_payload()
        ctx = {
            "correlation_id": "corr-001",
            "root_human_id": "user-alice",
            "is_new_chain": False,
            "chain_depth": 7,
        }
        identity = _bot_identity("operations")
        result = extract_intent(
            "issue_comment", payload, correlation_ctx=ctx, resolved_identity=identity
        )
        assert result is not None

    def test_human_sender_ignores_depth(self):
        """Human senders are never gated by depth."""
        payload = self._make_comment_payload(sender=_human_sender())
        ctx = {
            "correlation_id": "corr-001",
            "root_human_id": "user-alice",
            "is_new_chain": False,
            "chain_depth": MAX_CHAIN_DEPTH + 100,
        }
        identity = _human_identity()
        result = extract_intent(
            "issue_comment", payload, correlation_ctx=ctx, resolved_identity=identity
        )
        assert result is not None
        assert result.persona == "developer"

    def test_missing_chain_depth_defaults_to_zero(self):
        """Missing chain_depth in correlation_ctx defaults to 0 (allowed)."""
        payload = self._make_comment_payload()
        ctx = {
            "correlation_id": "corr-001",
            "root_human_id": "user-alice",
            "is_new_chain": True,
            # no chain_depth key
        }
        identity = _bot_identity("operations")
        result = extract_intent(
            "issue_comment", payload, correlation_ctx=ctx, resolved_identity=identity
        )
        assert result is not None


class TestSelfMentionGuard:
    """Self-mention guard evaluated BEFORE depth check."""

    def test_self_mention_blocked(self):
        """Bot mentioning its own persona is blocked regardless of depth."""
        payload = {
            "action": "created",
            "comment": {"body": "@agent-operations let me re-run"},
            "issue": {"number": 55},
            "sender": _bot_sender(),
            "installation": {"id": 123},
        }
        ctx = {
            "correlation_id": "corr-001",
            "root_human_id": "user-alice",
            "is_new_chain": True,
            "chain_depth": 0,  # Even at depth 0
        }
        identity = _bot_identity("operations")  # Same persona as mention
        result = extract_intent(
            "issue_comment", payload, correlation_ctx=ctx, resolved_identity=identity
        )
        assert result is None

    def test_cross_mention_allowed(self):
        """Bot mentioning a DIFFERENT persona is allowed (not self-mention)."""
        payload = {
            "action": "created",
            "comment": {"body": "@agent-developer please fix this"},
            "issue": {"number": 55},
            "sender": _bot_sender(),
            "installation": {"id": 123},
        }
        ctx = {
            "correlation_id": "corr-001",
            "root_human_id": "user-alice",
            "is_new_chain": False,
            "chain_depth": 1,
        }
        identity = _bot_identity("operations")  # Different from "developer"
        result = extract_intent(
            "issue_comment", payload, correlation_ctx=ctx, resolved_identity=identity
        )
        assert result is not None
        assert result.persona == "developer"


# --- PR branch filter tests ---


class TestPRBranchFilter:
    """Test agent/issue-* branch filter for PR events."""

    def test_agent_branch_triggers_reviewer(self):
        """PR on agent/issue-123 branch triggers reviewer."""
        payload = {
            "action": "opened",
            "pull_request": {
                "number": 15,
                "body": "Some PR body",
                "head": {"ref": "agent/issue-123", "sha": "abc"},
            },
            "sender": _human_sender(),
            "installation": {"id": 123},
        }
        result = extract_intent("pull_request", payload)
        assert result is not None
        assert result.persona == "reviewer"
        assert result.trigger == "pr_opened"

    def test_agent_branch_with_suffix(self):
        """PR on agent/issue-123-followup branch triggers reviewer."""
        payload = {
            "action": "opened",
            "pull_request": {
                "number": 15,
                "body": "Some PR body",
                "head": {"ref": "agent/issue-123-followup", "sha": "abc"},
            },
            "sender": _human_sender(),
            "installation": {"id": 123},
        }
        result = extract_intent("pull_request", payload)
        assert result is not None
        assert result.persona == "reviewer"

    def test_human_branch_does_not_trigger(self):
        """PR on feature/foo branch does NOT trigger reviewer."""
        payload = {
            "action": "opened",
            "pull_request": {
                "number": 15,
                "body": "Normal PR",
                "head": {"ref": "feature/foo", "sha": "abc"},
            },
            "sender": _human_sender(),
            "installation": {"id": 123},
        }
        result = extract_intent("pull_request", payload)
        assert result is None

    def test_main_branch_does_not_trigger(self):
        """PR on main does NOT trigger reviewer."""
        payload = {
            "action": "opened",
            "pull_request": {
                "number": 15,
                "body": "Hotfix",
                "head": {"ref": "main", "sha": "abc"},
            },
            "sender": _human_sender(),
            "installation": {"id": 123},
        }
        result = extract_intent("pull_request", payload)
        assert result is None

    def test_non_agent_prefix_blocked(self):
        """PR on fix/agent/issue-123 does NOT match (must START with agent/issue-)."""
        payload = {
            "action": "opened",
            "pull_request": {
                "number": 15,
                "body": "Some body",
                "head": {"ref": "fix/agent/issue-123", "sha": "abc"},
            },
            "sender": _human_sender(),
            "installation": {"id": 123},
        }
        result = extract_intent("pull_request", payload)
        assert result is None


# --- Synchronize gate tests ---


class TestSynchronizeGate:
    """Bot pull_request.synchronize should NOT trigger reviewer."""

    def test_bot_synchronize_blocked(self):
        """Bot PR synchronize event returns None (no re-trigger)."""
        payload = {
            "action": "synchronize",
            "pull_request": {
                "number": 15,
                "body": VALID_MARKER + "\nPR body",
                "head": {"ref": "agent/issue-123", "sha": "def456"},
            },
            "sender": _bot_sender(),
            "installation": {"id": 123},
        }
        result = extract_intent("pull_request", payload)
        assert result is None

    def test_human_synchronize_allowed(self):
        """Human PR synchronize event on agent branch triggers reviewer."""
        payload = {
            "action": "synchronize",
            "pull_request": {
                "number": 15,
                "body": "PR body",
                "head": {"ref": "agent/issue-123", "sha": "def456"},
            },
            "sender": _human_sender(),
            "installation": {"id": 123},
        }
        result = extract_intent("pull_request", payload)
        assert result is not None
        assert result.persona == "reviewer"
        assert result.trigger == "pr_synchronize"

    def test_bot_opened_with_marker_allowed(self):
        """Bot PR opened event WITH valid marker on agent branch → allowed."""
        payload = {
            "action": "opened",
            "pull_request": {
                "number": 15,
                "body": VALID_MARKER + "\nPR body",
                "head": {"ref": "agent/issue-123", "sha": "abc123"},
            },
            "sender": _bot_sender(),
            "installation": {"id": 123},
        }
        result = extract_intent("pull_request", payload)
        assert result is not None
        assert result.persona == "reviewer"
        assert result.trigger == "pr_opened"


# --- Bot-guard marker-gated relaxation ---


class TestBotGuardMarkerGated:
    """Bot PR events: only allowed with valid adp-* marker."""

    def test_bot_pr_without_marker_blocked(self):
        """Bot-opened PR without adp-* marker stays blocked."""
        payload = {
            "action": "opened",
            "pull_request": {
                "number": 15,
                "body": "Just a regular PR body with no marker",
                "head": {"ref": "agent/issue-123", "sha": "abc"},
            },
            "sender": _bot_sender(),
            "installation": {"id": 123},
        }
        result = extract_intent("pull_request", payload)
        assert result is None

    def test_bot_pr_with_marker_allowed(self):
        """Bot-opened PR with valid adp-* marker is allowed through."""
        payload = {
            "action": "opened",
            "pull_request": {
                "number": 15,
                "body": VALID_MARKER + "\nAutomated PR body",
                "head": {"ref": "agent/issue-123", "sha": "abc"},
            },
            "sender": _bot_sender(),
            "installation": {"id": 123},
        }
        result = extract_intent("pull_request", payload)
        assert result is not None
        assert result.persona == "reviewer"

    def test_bot_pr_with_empty_body_blocked(self):
        """Bot PR with empty body (no marker possible) → blocked."""
        payload = {
            "action": "opened",
            "pull_request": {
                "number": 15,
                "body": "",
                "head": {"ref": "agent/issue-123", "sha": "abc"},
            },
            "sender": _bot_sender(),
            "installation": {"id": 123},
        }
        result = extract_intent("pull_request", payload)
        assert result is None

    def test_bot_pr_with_null_body_blocked(self):
        """Bot PR with null body → blocked."""
        payload = {
            "action": "opened",
            "pull_request": {
                "number": 15,
                "body": None,
                "head": {"ref": "agent/issue-123", "sha": "abc"},
            },
            "sender": _bot_sender(),
            "installation": {"id": 123},
        }
        result = extract_intent("pull_request", payload)
        assert result is None


# --- Fan-out test ---


class TestFanOut:
    """A comment mentioning multiple personas should yield an intent for the first."""

    def test_multiple_mentions_yields_first_match(self):
        """Comment with two mentions produces intent for the first matched persona."""
        payload = {
            "action": "created",
            "comment": {"body": "@agent-developer and @agent-reviewer please look"},
            "issue": {"number": 55},
            "sender": _human_sender(),
            "installation": {"id": 123},
        }
        result = extract_intent("issue_comment", payload)
        assert result is not None
        # First match wins (order depends on MENTION_TO_PERSONA iteration)
        assert result.persona in ("developer", "reviewer")


# --- Depth across PR hop ---


class TestDepthAcrossPRHop:
    """Verify depth is correctly used when coming from PR-body marker."""

    def test_pr_marker_depth_used_in_reviewer_trigger(self):
        """When bot opens PR with marker depth N, reviewer gets depth N+1 in ctx.

        This test verifies intent_parser allows the trigger — the actual depth
        increment happens in handler.determine_correlation, not intent_parser.
        Intent_parser just needs to not block it.
        """
        # depth 2 in marker < MAX_CHAIN_DEPTH → should be allowed
        payload = {
            "action": "opened",
            "pull_request": {
                "number": 15,
                "body": VALID_MARKER + "\nPR body",
                "head": {"ref": "agent/issue-123", "sha": "abc"},
            },
            "sender": _bot_sender(),
            "installation": {"id": 123},
        }
        result = extract_intent("pull_request", payload)
        assert result is not None
        assert result.persona == "reviewer"


# --- MAX_CHAIN_DEPTH env override ---


class TestMaxChainDepthConfig:
    """MAX_CHAIN_DEPTH should be configurable via env."""

    def test_default_is_8(self):
        assert MAX_CHAIN_DEPTH == 8
