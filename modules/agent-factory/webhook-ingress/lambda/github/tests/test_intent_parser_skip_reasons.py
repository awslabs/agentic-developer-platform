"""Issue #4020 — every intent-parser no-op must name its reason.

The parser had ten separate ``return None`` paths that were indistinguishable to
anything downstream. An operator asking "why didn't the reviewer run on my PR?"
got the same bare "✗ No-op" whether the branch wasn't an agent branch, the label
wasn't mapped, or a bot push had been correctly deduplicated.

One test per reason enum, so a future refactor that collapses two paths onto the
same reason (or drops one) fails here rather than silently degrading the UI back
to an unexplained badge.

Also pins the compatibility property that made this change cheap: ``extract_intent``
still returns a bare ``Intent | None``. It is called from ~150 places; changing
its signature would have meant touching every one of them, so the reason-aware
entry point is additive and the old name delegates to it.
"""

import sys
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from common import skip_reasons
from intent_parser import extract_intent, extract_intent_with_reason


@dataclass
class MockResolvedIdentity:
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


def _ctx():
    return {
        "correlation_id": "corr-test-001",
        "root_human_id": "user-alice",
        "is_human_rooted": True,
        "is_new_chain": False,
        "chain_depth": 1,
        "last_triggered_persona": None,
        "recent_triggered_personas": set(),
        "recent_trigger_count": 0,
    }


DISPATCH_MARKER = (
    "<!-- adp-correlation:corr-abc-123 adp-root-human:user-456 "
    "adp-is-human-rooted:true adp-invocation:msg-789 "
    "adp-chain-depth:2 adp-dispatch:developer -->"
)


class TestCommentReasons:
    def test_human_comment_without_mention(self):
        payload = {
            "action": "created",
            "comment": {"body": "looks good to me, merging"},
            "issue": {"number": 7},
            "sender": _human_sender(),
        }
        intent, reason = extract_intent_with_reason("issue_comment", payload)
        assert intent is None
        assert reason == skip_reasons.NO_MENTION

    @patch("intent_parser._emit_metric")
    def test_bot_mention_without_dispatch_marker(self, _mock_metric):
        """The #2149 case: bot prose that happens to name an agent.

        This is the reason most worth distinguishing — it means an emit site
        forgot the dispatch marker, i.e. a real bug in our own code, whereas
        `no_mention` is normal traffic.
        """
        payload = {
            "action": "created",
            "comment": {"body": "## @agent-developer Started\n**Status**: In Progress"},
            "issue": {"number": 2082},
            "sender": _bot_sender(),
        }
        intent, reason = extract_intent_with_reason(
            "issue_comment", payload, correlation_ctx=_ctx()
        )
        assert intent is None
        assert reason == skip_reasons.BOT_MENTION_NO_DISPATCH_MARKER

    def test_bot_comment_with_no_mention_at_all_is_plain_no_mention(self):
        """A bot comment with neither marker nor mention is ordinary traffic.

        It must NOT report BOT_MENTION_NO_DISPATCH_MARKER — that reason is the
        "our emit site is broken" signal, and diluting it with every routine bot
        comment would make it useless for diagnosis.
        """
        payload = {
            "action": "created",
            "comment": {"body": "Build succeeded in 4m12s."},
            "issue": {"number": 9},
            "sender": _bot_sender(),
        }
        intent, reason = extract_intent_with_reason(
            "issue_comment", payload, correlation_ctx=_ctx()
        )
        assert intent is None
        assert reason == skip_reasons.NO_MENTION

    def test_bot_dispatch_without_correlation_context(self):
        payload = {
            "action": "created",
            "comment": {"body": DISPATCH_MARKER + "\n@agent-developer go"},
            "issue": {"number": 11},
            "sender": _bot_sender(),
        }
        intent, reason = extract_intent_with_reason("issue_comment", payload, correlation_ctx=None)
        assert intent is None
        assert reason == skip_reasons.BOT_DISPATCH_NO_CORRELATION

    def test_bot_edited_own_comment_is_bot_comment_action_unhandled(self):
        """The agent editing its own status comment in place (not a new comment).

        Must be distinguishable from EVENT_TYPE_UNHANDLED — otherwise this looks
        like an unexplained gap in coverage every time it shows up in webhook
        delivery logs, when it's actually routine self-authored activity.
        """
        payload = {
            "action": "edited",
            "comment": {"body": "**Status**: In Progress"},
            "issue": {"number": 12},
            "sender": _bot_sender(),
        }
        intent, reason = extract_intent_with_reason("issue_comment", payload)
        assert intent is None
        assert reason == skip_reasons.BOT_COMMENT_ACTION_UNHANDLED

    def test_human_edited_comment_is_still_event_type_unhandled(self):
        """A human editing their comment has no bot-specific reason to claim —
        it stays on the generic catch-all, not BOT_COMMENT_ACTION_UNHANDLED."""
        payload = {
            "action": "edited",
            "comment": {"body": "typo fix"},
            "issue": {"number": 13},
            "sender": _human_sender(),
        }
        intent, reason = extract_intent_with_reason("issue_comment", payload)
        assert intent is None
        assert reason == skip_reasons.EVENT_TYPE_UNHANDLED


class TestIssueReasons:
    def test_issue_opened_without_aidlc_label(self):
        payload = {
            "action": "opened",
            "issue": {"number": 3, "labels": [{"name": "bug"}]},
            "sender": _human_sender(),
        }
        intent, reason = extract_intent_with_reason("issues", payload)
        assert intent is None
        assert reason == skip_reasons.NO_AIDLC_LABEL

    def test_bot_issue_opened_without_aidlc_label(self):
        """Bot path to the same conclusion (#3245) — also NO_AIDLC_LABEL.

        Both paths mean "the authorizing label is missing", so they share a
        reason; the operator's next action is identical.
        """
        payload = {
            "action": "opened",
            "issue": {"number": 4, "labels": [{"name": "chore"}]},
            "sender": _bot_sender(),
        }
        intent, reason = extract_intent_with_reason("issues", payload)
        assert intent is None
        assert reason == skip_reasons.NO_AIDLC_LABEL

    def test_label_without_persona_mapping(self):
        payload = {
            "action": "labeled",
            "label": {"name": "wontfix"},
            "issue": {"number": 5},
            "sender": _human_sender(),
        }
        intent, reason = extract_intent_with_reason("issues", payload)
        assert intent is None
        assert reason == skip_reasons.LABEL_UNMAPPED

    def test_bot_non_comment_event_ignored(self):
        payload = {
            "action": "closed",
            "issue": {"number": 6, "labels": []},
            "sender": _bot_sender(),
        }
        intent, reason = extract_intent_with_reason("issues", payload)
        assert intent is None
        assert reason == skip_reasons.BOT_EVENT_IGNORED


class TestPullRequestReasons:
    def test_pr_on_non_agent_branch(self):
        payload = {
            "action": "opened",
            "pull_request": {"number": 12, "head": {"ref": "feature/my-work"}},
            "sender": _human_sender(),
        }
        intent, reason = extract_intent_with_reason("pull_request", payload)
        assert intent is None
        assert reason == skip_reasons.PR_BRANCH_NOT_AGENT

    def test_bot_synchronize_is_deduped(self):
        payload = {
            "action": "synchronize",
            "pull_request": {"number": 13, "head": {"ref": "agent/issue-4020"}},
            "sender": _bot_sender(),
        }
        intent, reason = extract_intent_with_reason("pull_request", payload)
        assert intent is None
        assert reason == skip_reasons.BOT_SYNCHRONIZE_DEDUP


class TestCatchAllReasons:
    def test_installation_event(self):
        payload = {
            "action": "created",
            "installation": {"id": 42, "account": {"login": "acme"}},
            "sender": _human_sender(),
        }
        intent, reason = extract_intent_with_reason("installation", payload)
        assert intent is None
        assert reason == skip_reasons.INSTALLATION_EVENT

    def test_unhandled_event_type(self):
        payload = {"action": "created", "sender": _human_sender()}
        intent, reason = extract_intent_with_reason("star", payload)
        assert intent is None
        assert reason == skip_reasons.EVENT_TYPE_UNHANDLED

    def test_unhandled_action_on_handled_event_type(self):
        """`issues.assigned` is a handled event TYPE with an unhandled action —
        it falls through to the same catch-all, not to a label reason."""
        payload = {
            "action": "assigned",
            "issue": {"number": 8, "labels": []},
            "sender": _human_sender(),
        }
        intent, reason = extract_intent_with_reason("issues", payload)
        assert intent is None
        assert reason == skip_reasons.EVENT_TYPE_UNHANDLED


class TestTriggeringEventsCarryNoReason:
    """A dispatching event must return reason=None.

    Without this, a reason could leak onto a successful run's row and the UI
    would show "Complete" alongside an explanation of why nothing ran.
    """

    def test_human_mention_returns_intent_and_no_reason(self):
        payload = {
            "action": "created",
            "comment": {"body": "@agent-developer please fix this"},
            "issue": {"number": 20},
            "sender": _human_sender(),
        }
        intent, reason = extract_intent_with_reason("issue_comment", payload)
        assert intent is not None
        assert intent.persona == "developer"
        assert reason is None

    @patch.dict("os.environ", {"GITHUB_AUTO_PR_REVIEW_ENABLED": "true"})
    def test_agent_pr_opened_returns_intent_and_no_reason(self):
        payload = {
            "action": "opened",
            "pull_request": {"number": 21, "head": {"ref": "agent/issue-4020"}},
            "sender": _human_sender(),
        }
        intent, reason = extract_intent_with_reason("pull_request", payload)
        assert intent is not None
        assert intent.persona == "agent-codex-reviewer"
        assert reason is None

    def test_mapped_label_returns_intent_and_no_reason(self):
        payload = {
            "action": "labeled",
            "label": {"name": "developer"},
            "issue": {"number": 22},
            "sender": _human_sender(),
        }
        intent, reason = extract_intent_with_reason("issues", payload)
        assert intent is not None
        assert reason is None


class TestExtractIntentBackCompat:
    """`extract_intent` keeps its original single-value signature.

    ~150 existing call sites (and every other test module here) depend on it.
    The reason-aware variant is additive precisely so none of them had to change.
    """

    def test_returns_bare_none_not_a_tuple(self):
        payload = {
            "action": "created",
            "comment": {"body": "no mention here"},
            "issue": {"number": 30},
            "sender": _human_sender(),
        }
        assert extract_intent("issue_comment", payload) is None

    def test_returns_bare_intent_not_a_tuple(self):
        payload = {
            "action": "created",
            "comment": {"body": "@agent-developer hi"},
            "issue": {"number": 31},
            "sender": _human_sender(),
        }
        result = extract_intent("issue_comment", payload)
        assert result is not None
        assert not isinstance(result, tuple)
        assert result.persona == "developer"

    def test_agrees_with_the_reason_aware_variant(self):
        """The wrapper must not diverge — same inputs, same intent decision."""
        payloads = [
            (
                "issue_comment",
                {
                    "action": "created",
                    "comment": {"body": "nothing"},
                    "issue": {"number": 1},
                    "sender": _human_sender(),
                },
            ),
            (
                "issues",
                {
                    "action": "labeled",
                    "label": {"name": "developer"},
                    "issue": {"number": 2},
                    "sender": _human_sender(),
                },
            ),
            (
                "pull_request",
                {
                    "action": "opened",
                    "pull_request": {"number": 3, "head": {"ref": "main"}},
                    "sender": _human_sender(),
                },
            ),
        ]
        for event_type, payload in payloads:
            wrapped = extract_intent(event_type, payload)
            direct, _reason = extract_intent_with_reason(event_type, payload)
            assert (wrapped is None) == (direct is None), event_type
            if wrapped is not None:
                assert wrapped.persona == direct.persona, event_type
