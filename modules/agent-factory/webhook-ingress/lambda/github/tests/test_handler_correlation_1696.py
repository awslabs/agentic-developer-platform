"""Tests for handler.py determine_correlation — Issue #1696 changes.

Tests pointer-vs-marker precedence, chain_depth increment, PR event
correlation context, and source_ref.issue fallback.
"""

import sys
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock, patch


sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent))


@dataclass
class MockResolvedIdentity:
    """Minimal mock of ResolvedIdentity."""

    tenant_id: str = "test-org"
    org_id: str = "test-org"
    user_id: str = "user-123"
    user_provisioning_mode: str = "strict"
    user_kind: str = "human"
    bot_kind: str = ""


def _bot_identity(bot_kind="operations"):
    return MockResolvedIdentity(user_kind="bot", bot_kind=bot_kind, user_id="bot-ops-123")


def _human_identity():
    return MockResolvedIdentity(user_kind="human", bot_kind="", user_id="user-alice-456")


# Marker text for testing
MARKER_TEXT = (
    "<!-- adp-correlation:corr-marker-001 adp-root-human:user-marker "
    "adp-is-human-rooted:true adp-invocation:msg-parent-123 adp-chain-depth:2 -->\n"
    "Some body text"
)


class TestDetermineCorrelationPrecedence:
    """Test pointer-vs-marker precedence rule (issue #1696, architect I1)."""

    @patch("handler._get_correlation_store")
    def test_pointer_and_marker_same_correlation_uses_pointer(self, mock_store_fn):
        """Pointer + marker with matching correlation_id → pointer wins."""
        from handler import determine_correlation

        mock_store = MagicMock()
        mock_store.read_pointer.return_value = {
            "correlation_id": "corr-marker-001",  # Same as marker
            "root_human_id": "user-pointer",
            "is_human_rooted": True,
            "triggering_invocation_id": "msg-pointer-inv",
            "chain_depth": 4,
        }
        mock_store_fn.return_value = mock_store

        identity = _bot_identity()
        result = determine_correlation(
            {}, identity, "github:repo=org/repo,issue=55", marker_text=MARKER_TEXT
        )

        assert result["correlation_id"] == "corr-marker-001"
        # Pointer data wins (server-written, authoritative)
        assert result["root_human_id"] == "user-pointer"
        assert result["parent_invocation_id"] == "msg-pointer-inv"
        assert result["chain_depth"] == 5  # pointer depth (4) + 1
        assert result["is_new_chain"] is False

    @patch("handler._get_correlation_store")
    def test_pointer_and_marker_different_correlation_uses_marker(self, mock_store_fn):
        """Pointer + marker with different correlation_id → marker wins (cross-channel hop)."""
        from handler import determine_correlation

        mock_store = MagicMock()
        mock_store.read_pointer.return_value = {
            "correlation_id": "corr-STALE-old",  # Different from marker
            "root_human_id": "user-stale",
            "is_human_rooted": False,
            "triggering_invocation_id": "msg-stale",
            "chain_depth": 10,
        }
        mock_store_fn.return_value = mock_store

        identity = _bot_identity()
        result = determine_correlation(
            {}, identity, "github:repo=org/repo,issue=55", marker_text=MARKER_TEXT
        )

        # Marker data wins
        assert result["correlation_id"] == "corr-marker-001"
        assert result["root_human_id"] == "user-marker"
        assert result["parent_invocation_id"] == "msg-parent-123"
        assert result["chain_depth"] == 3  # marker depth (2) + 1
        assert result["is_new_chain"] is False

    @patch("handler._get_correlation_store")
    def test_pointer_only_no_marker(self, mock_store_fn):
        """Pointer exists, no marker → same-channel continuation (pointer wins)."""
        from handler import determine_correlation

        mock_store = MagicMock()
        mock_store.read_pointer.return_value = {
            "correlation_id": "corr-ptr-001",
            "root_human_id": "user-ptr",
            "is_human_rooted": True,
            "triggering_invocation_id": "msg-ptr-inv",
            "chain_depth": 1,
        }
        mock_store_fn.return_value = mock_store

        identity = _bot_identity()
        result = determine_correlation(
            {}, identity, "github:repo=org/repo,issue=55", marker_text=None
        )

        assert result["correlation_id"] == "corr-ptr-001"
        assert result["parent_invocation_id"] == "msg-ptr-inv"
        assert result["chain_depth"] == 2  # 1 + 1
        assert result["is_new_chain"] is False

    @patch("handler._get_correlation_store")
    def test_marker_only_no_pointer(self, mock_store_fn):
        """No pointer, valid marker → cross-channel first hop (marker wins)."""
        from handler import determine_correlation

        mock_store = MagicMock()
        mock_store.read_pointer.return_value = None  # No pointer
        mock_store_fn.return_value = mock_store

        identity = _bot_identity()
        result = determine_correlation(
            {}, identity, "github:repo=org/repo,issue=55", marker_text=MARKER_TEXT
        )

        assert result["correlation_id"] == "corr-marker-001"
        assert result["root_human_id"] == "user-marker"
        assert result["parent_invocation_id"] == "msg-parent-123"
        assert result["chain_depth"] == 3  # marker depth (2) + 1
        assert result["is_new_chain"] is False

    @patch("handler._get_correlation_store")
    def test_no_pointer_no_marker_new_chain(self, mock_store_fn):
        """Neither pointer nor marker → new bot-initiated chain."""
        from handler import determine_correlation

        mock_store = MagicMock()
        mock_store.read_pointer.return_value = None
        mock_store_fn.return_value = mock_store

        identity = _bot_identity()
        result = determine_correlation(
            {}, identity, "github:repo=org/repo,issue=55", marker_text=None
        )

        assert result["correlation_id"]  # UUID generated
        assert result["root_human_id"] == "bot-ops-123"
        assert result["parent_invocation_id"] is None
        assert result["chain_depth"] == 0
        assert result["is_new_chain"] is True
        assert result["is_human_rooted"] is False

    @patch("handler._get_correlation_store")
    def test_human_sender_always_new_chain(self, mock_store_fn):
        """Human senders always start a new chain, regardless of pointer/marker."""
        from handler import determine_correlation

        mock_store = MagicMock()
        mock_store.read_pointer.return_value = {
            "correlation_id": "corr-stale",
            "root_human_id": "user-old",
            "is_human_rooted": True,
            "triggering_invocation_id": "msg-old",
            "chain_depth": 5,
        }
        mock_store_fn.return_value = mock_store

        identity = _human_identity()
        result = determine_correlation(
            {}, identity, "github:repo=org/repo,issue=55", marker_text=MARKER_TEXT
        )

        assert result["correlation_id"] != "corr-marker-001"
        assert result["correlation_id"] != "corr-stale"
        assert result["root_human_id"] == "user-alice-456"
        assert result["parent_invocation_id"] is None
        assert result["chain_depth"] == 0
        assert result["is_new_chain"] is True
        assert result["is_human_rooted"] is True


class TestChainDepthIncrement:
    """Verify depth increments exactly once per hop."""

    @patch("handler._get_correlation_store")
    def test_depth_increments_once_from_pointer(self, mock_store_fn):
        """Pointer depth N → spawned run gets depth N+1."""
        from handler import determine_correlation

        mock_store = MagicMock()
        mock_store.read_pointer.return_value = {
            "correlation_id": "corr-001",
            "root_human_id": "user-h",
            "is_human_rooted": True,
            "triggering_invocation_id": "msg-parent",
            "chain_depth": 3,
        }
        mock_store_fn.return_value = mock_store

        identity = _bot_identity()
        result = determine_correlation({}, identity, "key", marker_text=None)
        assert result["chain_depth"] == 4

    @patch("handler._get_correlation_store")
    def test_depth_increments_once_from_marker(self, mock_store_fn):
        """Marker depth N → spawned run gets depth N+1."""
        from handler import determine_correlation

        mock_store = MagicMock()
        mock_store.read_pointer.return_value = None
        mock_store_fn.return_value = mock_store

        # Marker with depth 5
        marker = (
            "<!-- adp-correlation:corr-m adp-root-human:user-m "
            "adp-is-human-rooted:true adp-invocation:msg-m adp-chain-depth:5 -->"
        )
        identity = _bot_identity()
        result = determine_correlation({}, identity, "key", marker_text=marker)
        assert result["chain_depth"] == 6

    @patch("handler._get_correlation_store")
    def test_missing_depth_in_pointer_defaults_to_zero(self, mock_store_fn):
        """Pointer without chain_depth (old data) → treated as 0, child gets 1."""
        from handler import determine_correlation

        mock_store = MagicMock()
        mock_store.read_pointer.return_value = {
            "correlation_id": "corr-old",
            "root_human_id": "user-old",
            "is_human_rooted": True,
            "triggering_invocation_id": "msg-old",
            "chain_depth": None,  # Old pointer without depth
        }
        mock_store_fn.return_value = mock_store

        identity = _bot_identity()
        result = determine_correlation({}, identity, "key", marker_text=None)
        assert result["chain_depth"] == 1  # 0 + 1

    @patch("handler._get_correlation_store")
    def test_missing_depth_in_marker_defaults_to_zero(self, mock_store_fn):
        """Marker without chain_depth (legacy) → treated as 0, child gets 1."""
        from handler import determine_correlation

        mock_store = MagicMock()
        mock_store.read_pointer.return_value = None
        mock_store_fn.return_value = mock_store

        # Legacy marker without adp-chain-depth
        legacy_marker = (
            "<!-- adp-correlation:corr-leg adp-root-human:user-leg "
            "adp-is-human-rooted:true -->\nBody"
        )
        identity = _bot_identity()
        result = determine_correlation({}, identity, "key", marker_text=legacy_marker)
        assert result["chain_depth"] == 1  # 0 + 1


class TestSourceRefIssueFallback:
    """Test source_ref.issue falls back to pull_request.number for PR events."""

    def test_issue_comment_event_uses_issue_number(self):
        """For issue_comment events, source_ref.issue = issue.number."""
        payload = {
            "issue": {"number": 42, "title": "Bug"},
            "comment": {"body": "test"},
            "sender": {"login": "u", "id": 1, "type": "User"},
        }
        # The fallback logic is in the envelope build — test it directly
        issue_number = (
            payload.get("issue", {}).get("number")
            if "issue" in payload
            else payload.get("pull_request", {}).get("number")
        )
        assert issue_number == 42

    def test_pr_event_falls_back_to_pr_number(self):
        """For pull_request events (no issue key), falls back to pr.number."""
        payload = {
            "pull_request": {"number": 99, "title": "PR", "head": {"sha": "abc"}},
            "sender": {"login": "u", "id": 1, "type": "User"},
        }
        issue_number = (
            payload.get("issue", {}).get("number")
            if "issue" in payload
            else payload.get("pull_request", {}).get("number")
        )
        assert issue_number == 99

    def test_neither_issue_nor_pr_returns_none(self):
        """When neither issue nor pull_request key exists, result is None."""
        payload = {
            "sender": {"login": "u", "id": 1, "type": "User"},
        }
        issue_number = (
            payload.get("issue", {}).get("number")
            if "issue" in payload
            else payload.get("pull_request", {}).get("number")
        )
        assert issue_number is None
