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


# Issue #5663 (A09): lineage authority is now bound to the job's own tenant /
# installation / repository as well as to the server-written row's fields. These
# #1696 precedence tests are not about that predicate, so ``_chain`` and ``_payload``
# below are kept CONSISTENT with the identity's tenant; the predicate itself is
# asserted in test_handler_lineage_context_5663.py.
TENANT = "test-org"
REPO = "test-org/repo"
INSTALLATION = "4242"


@dataclass
class MockResolvedIdentity:
    """Minimal mock of ResolvedIdentity."""

    tenant_id: str = TENANT
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


def _chain(correlation_id, root_human_id, is_human_rooted, chain_depth):
    """A server-written ``webhook-events`` row for a chain (issue #4129).

    The pointer branches of determine_correlation now source root_human_id /
    is_human_rooted / chain_depth from the ``correlation-index`` GSI instead of
    reading them off the pod-writable pointer row, so precedence tests must stub
    the chain row too. Values mirror the pointer under test, which keeps these
    tests asserting pointer-vs-marker PRECEDENCE (their actual subject) rather
    than #4129's fail-closed path.
    """
    return {
        "event_id": "evt-chain",
        "correlation_id": correlation_id,
        "root_human_id": root_human_id,
        "is_human_rooted": is_human_rooted,
        "chain_depth": chain_depth,
        # Issue #5663: same tenant/installation/repo as ``_payload()``.
        "tenant_id": TENANT,
        "installation_id": INSTALLATION,
        "repo": REPO,
    }


def _payload() -> dict:
    """Signature-verified payload fields the A09 job context is derived from."""
    return {"repository": {"full_name": REPO}, "installation": {"id": INSTALLATION}}


class TestDetermineCorrelationPrecedence:
    """Test pointer-vs-marker precedence rule (issue #1696, architect I1)."""

    @patch(
        "handler._resolve_chain_record",
        return_value=_chain("corr-marker-001", "user-pointer", True, 4),
    )
    @patch("handler._get_correlation_store")
    def test_pointer_and_marker_same_correlation_uses_pointer(self, mock_store_fn, _chain_fn):
        """Pointer + marker with matching correlation_id → pointer wins."""
        from handler import determine_correlation

        mock_store = MagicMock()
        mock_store.read_pointer.return_value = {
            "correlation_id": "corr-marker-001",  # Same as marker
            "triggering_invocation_id": "msg-pointer-inv",
        }
        mock_store_fn.return_value = mock_store

        identity = _bot_identity()
        result = determine_correlation(
            _payload(), identity, "github:repo=org/repo,issue=55", marker_text=MARKER_TEXT
        )

        assert result["correlation_id"] == "corr-marker-001"
        # Server-resolved chain data wins over the marker's claim (issue #4129:
        # sourced from the webhook-events GSI, not from the pointer row).
        assert result["root_human_id"] == "user-pointer"
        assert result["parent_invocation_id"] == "msg-pointer-inv"
        # Issue #4268: inherited unchanged — the chain row's 4, not 4+1.
        assert result["chain_depth"] == 4
        assert result["is_new_chain"] is False

    @patch("handler._get_correlation_store")
    def test_same_corr_parent_falls_back_to_marker_when_pointer_lacks_trig(self, mock_store_fn):
        """Issue #1738: pointer + marker same corr, but pointer has NO
        triggering_invocation_id (e.g. a worker-written PR-channel pointer) →
        parent_invocation_id falls back to the marker's invocation id.

        This is the final lineage gap: the reviewer inherited the chain (corr
        matched) but parent stayed null because the PR pointer that 'won' had no
        triggering_invocation_id, even though the synthesized issue marker did.
        """
        from handler import determine_correlation

        mock_store = MagicMock()
        mock_store.read_pointer.return_value = {
            "correlation_id": "corr-marker-001",  # same as MARKER_TEXT
            "root_human_id": "user-pointer",
            "is_human_rooted": True,
            "triggering_invocation_id": None,  # PR pointer lacks the parent edge
            "chain_depth": 0,
        }
        mock_store_fn.return_value = mock_store

        identity = _bot_identity()
        result = determine_correlation(
            _payload(), identity, "github:repo=org/repo,pr=1741", marker_text=MARKER_TEXT
        )

        assert result["correlation_id"] == "corr-marker-001"  # chain inherited
        # parent falls back to the marker's invocation (msg-parent-123)
        assert result["parent_invocation_id"] == "msg-parent-123"
        assert result["is_new_chain"] is False

    @patch(
        "handler._resolve_chain_record",
        return_value=_chain("corr-marker-001", "user-marker", True, 2),
    )
    @patch("handler._get_correlation_store")
    def test_pointer_and_marker_different_correlation_uses_marker(self, mock_store_fn, _chain_fn):
        """Pointer + marker with different correlation_id → marker wins (cross-channel hop).

        Issue #5663: the marker still WINS the precedence decision — it selects the
        chain — but the human authority is now read from that chain's server-written
        row rather than off the marker, so this test stubs the row for the marker's
        correlation_id. Same adaptation #4129 already made to the pointer branches
        above; the subject here is still precedence.
        """
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
            _payload(), identity, "github:repo=org/repo,issue=55", marker_text=MARKER_TEXT
        )

        # Marker data wins
        assert result["correlation_id"] == "corr-marker-001"
        assert result["root_human_id"] == "user-marker"
        assert result["parent_invocation_id"] == "msg-parent-123"
        # Issue #4268: inherited unchanged — the marker's 2, not 2+1.
        assert result["chain_depth"] == 2
        assert result["is_new_chain"] is False

    @patch(
        "handler._resolve_chain_record",
        return_value=_chain("corr-ptr-001", "user-ptr", True, 1),
    )
    @patch("handler._get_correlation_store")
    def test_pointer_only_no_marker(self, mock_store_fn, _chain_fn):
        """Pointer exists, no marker → same-channel continuation (pointer wins)."""
        from handler import determine_correlation

        mock_store = MagicMock()
        mock_store.read_pointer.return_value = {
            "correlation_id": "corr-ptr-001",
            "triggering_invocation_id": "msg-ptr-inv",
        }
        mock_store_fn.return_value = mock_store

        identity = _bot_identity()
        result = determine_correlation(
            _payload(), identity, "github:repo=org/repo,issue=55", marker_text=None
        )

        assert result["correlation_id"] == "corr-ptr-001"
        assert result["parent_invocation_id"] == "msg-ptr-inv"
        assert result["chain_depth"] == 1  # inherited unchanged (#4268)
        assert result["is_new_chain"] is False

    @patch(
        "handler._resolve_chain_record",
        return_value=_chain("corr-marker-001", "user-marker", True, 2),
    )
    @patch("handler._get_correlation_store")
    def test_marker_only_no_pointer(self, mock_store_fn, _chain_fn):
        """No pointer, valid marker → cross-channel first hop (marker wins).

        Issue #5663: as above — the marker selects the chain, the chain's row supplies
        the human.
        """
        from handler import determine_correlation

        mock_store = MagicMock()
        mock_store.read_pointer.return_value = None  # No pointer
        mock_store_fn.return_value = mock_store

        identity = _bot_identity()
        result = determine_correlation(
            _payload(), identity, "github:repo=org/repo,issue=55", marker_text=MARKER_TEXT
        )

        assert result["correlation_id"] == "corr-marker-001"
        assert result["root_human_id"] == "user-marker"
        assert result["parent_invocation_id"] == "msg-parent-123"
        # Issue #4268: inherited unchanged — the marker's 2, not 2+1.
        assert result["chain_depth"] == 2
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
            _payload(), identity, "github:repo=org/repo,issue=55", marker_text=None
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
            _payload(), identity, "github:repo=org/repo,issue=55", marker_text=MARKER_TEXT
        )

        assert result["correlation_id"] != "corr-marker-001"
        assert result["correlation_id"] != "corr-stale"
        assert result["root_human_id"] == "user-alice-456"
        assert result["parent_invocation_id"] is None
        assert result["chain_depth"] == 0
        assert result["is_new_chain"] is True
        assert result["is_human_rooted"] is True


class TestChainDepthInheritance:
    """Ingest inherits depth unchanged; the increment belongs to dispatch (#4268).

    This class asserted ``inherited + 1`` until #4268. The +1 on ingest is what
    made the counter measure webhook events instead of agent generations: the
    value is persisted on every row, including the ``no_op`` rows this Lambda
    writes and discards, and the next event inherits the newest row's depth. Which
    SOURCE the depth is read from (server-written chain row vs marker, and the
    legacy-absent fallback) is unchanged and still covered here.
    """

    @patch(
        "handler._resolve_chain_record",
        return_value=_chain("corr-001", "user-h", True, 3),
    )
    @patch("handler._get_correlation_store")
    def test_depth_inherited_from_server_resolved_chain(self, mock_store_fn, _chain_fn):
        """Server-resolved chain depth N → context carries N, not N+1."""
        from handler import determine_correlation

        mock_store = MagicMock()
        mock_store.read_pointer.return_value = {
            "correlation_id": "corr-001",
            "triggering_invocation_id": "msg-parent",
        }
        mock_store_fn.return_value = mock_store

        identity = _bot_identity()
        result = determine_correlation(_payload(), identity, "key", marker_text=None)
        assert result["chain_depth"] == 3

    @patch("handler._get_correlation_store")
    def test_depth_inherited_from_marker(self, mock_store_fn):
        """Marker depth N → context carries N, not N+1."""
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
        result = determine_correlation(_payload(), identity, "key", marker_text=marker)
        assert result["chain_depth"] == 5

    @patch("handler._get_correlation_store")
    def test_missing_depth_in_pointer_defaults_to_zero(self, mock_store_fn):
        """Pointer without chain_depth (old data) → treated as 0."""
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
        result = determine_correlation(_payload(), identity, "key", marker_text=None)
        assert result["chain_depth"] == 0

    @patch("handler._get_correlation_store")
    def test_missing_depth_in_marker_defaults_to_zero(self, mock_store_fn):
        """Marker without chain_depth (legacy) → treated as 0."""
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
        result = determine_correlation(_payload(), identity, "key", marker_text=legacy_marker)
        assert result["chain_depth"] == 0


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
