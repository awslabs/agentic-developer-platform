"""Tests for intent_parser.py.

Phase 2-c (#786): Tests cover chain-aware bot logic and regression guards
for the split bot guard by event type.
"""

import json
import sys
from pathlib import Path
from dataclasses import dataclass
from unittest.mock import patch

# Add parent directory to path so we can import intent_parser
sys.path.insert(0, str(Path(__file__).parent.parent))

from intent_parser import (
    LABEL_TO_PERSONA,
    MENTION_TO_PERSONA,
    extract_intent,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict:
    with open(FIXTURES_DIR / name) as f:
        return json.load(f)


@dataclass
class MockResolvedIdentity:
    """Minimal mock of ResolvedIdentity for test purposes."""

    tenant_id: str = "test-org"
    org_id: str = "test-org"
    user_id: str = "user-123"
    user_provisioning_mode: str = "strict"
    user_kind: str = "human"
    bot_kind: str = ""


# --- Issue labeled tests ---


class TestIssueLabeledEvents:
    def test_developer_label(self):
        payload = load_fixture("issue_labeled.json")
        result = extract_intent("issues", payload)
        assert result is not None
        assert result.persona == "developer"
        assert result.trigger == "issue_labeled"
        assert result.label == "developer"

    def test_all_label_mappings(self):
        """Every label in LABEL_TO_PERSONA should produce a valid intent."""
        for label_name, expected_persona in LABEL_TO_PERSONA.items():
            payload = {
                "action": "labeled",
                "label": {"name": label_name},
                "issue": {"number": 1},
                "repository": {"full_name": "org/repo"},
                "sender": {"login": "user", "id": 100, "type": "User"},
                "installation": {"id": 123},
            }
            result = extract_intent("issues", payload)
            assert result is not None, f"Label '{label_name}' should produce intent"
            assert result.persona == expected_persona

    def test_unknown_label_returns_none(self):
        payload = {
            "action": "labeled",
            "label": {"name": "bug"},
            "issue": {"number": 1},
            "repository": {"full_name": "org/repo"},
            "sender": {"login": "user", "id": 100, "type": "User"},
            "installation": {"id": 123},
        }
        result = extract_intent("issues", payload)
        assert result is None

    def test_bot_sender_ignored(self):
        """Bot-generated label events should be ignored to prevent loops."""
        payload = load_fixture("issue_labeled.json")
        payload["sender"] = {"login": "adp-bot[bot]", "id": 888, "type": "Bot"}
        result = extract_intent("issues", payload)
        assert result is None

    def test_github_app_bot_ignored(self):
        """GitHub App bot (type=Bot) should be ignored."""
        payload = load_fixture("issue_labeled.json")
        payload["sender"]["type"] = "Bot"
        result = extract_intent("issues", payload)
        assert result is None


# --- Pull request tests ---


class TestPullRequestEvents:
    def test_pr_opened(self):
        payload = load_fixture("pr_opened.json")
        result = extract_intent("pull_request", payload)
        assert result is not None
        assert result.persona == "reviewer"
        assert result.trigger == "pr_opened"
        assert result.label is None

    def test_pr_synchronize(self):
        payload = load_fixture("pr_synchronize.json")
        result = extract_intent("pull_request", payload)
        assert result is not None
        assert result.persona == "reviewer"
        assert result.trigger == "pr_synchronize"

    def test_pr_closed_no_intent(self):
        payload = {
            "action": "closed",
            "pull_request": {"number": 1},
            "sender": {"login": "user", "id": 1, "type": "User"},
            "installation": {"id": 123},
        }
        result = extract_intent("pull_request", payload)
        assert result is None

    def test_pr_opened_by_bot_on_agent_branch_produces_intent(self):
        """Issue #1731: intent_parser no longer gates bot PRs by sender identity.

        A bot PR on an agent/issue-* branch produces a reviewer intent at THIS
        layer. Protection against non-ADP bots (dependabot, etc.) lives upstream
        in handler.py identity resolution, which 403s any sender not in the
        identity-index BEFORE extract_intent is ever called. This unit test
        calls extract_intent directly, bypassing that gate — so it asserts the
        post-identity-gate behavior: branch filter is the only block here.
        """
        payload = load_fixture("pr_opened.json")  # head ref: agent/issue-258
        payload["sender"] = {"login": "dependabot[bot]", "id": 777, "type": "Bot"}
        result = extract_intent("pull_request", payload)
        assert result is not None
        assert result.persona == "reviewer"

    def test_pr_opened_by_bot_on_non_agent_branch_ignored(self):
        """Bot PR on a non-agent branch is still blocked by the branch filter."""
        payload = load_fixture("pr_opened.json")
        payload["sender"] = {"login": "dependabot[bot]", "id": 777, "type": "Bot"}
        payload["pull_request"]["head"]["ref"] = "dependabot/npm/foo-1.2.3"
        result = extract_intent("pull_request", payload)
        assert result is None


# --- Issue comment mention tests ---


class TestIssueCommentEvents:
    def test_mention_developer(self):
        payload = load_fixture("issue_comment_mention.json")
        result = extract_intent("issue_comment", payload)
        assert result is not None
        assert result.persona == "developer"
        assert result.trigger == "mentioned"

    def test_mention_operations(self):
        payload = {
            "action": "created",
            "comment": {"body": "Hey @agent-operations please deploy this"},
            "issue": {"number": 10},
            "sender": {"login": "user", "id": 1, "type": "User"},
            "installation": {"id": 123},
        }
        result = extract_intent("issue_comment", payload)
        assert result is not None
        assert result.persona == "operations"

    def test_mention_architect(self):
        payload = {
            "action": "created",
            "comment": {"body": "@agent-architect please review this design"},
            "issue": {"number": 42},
            "sender": {"login": "user", "id": 1, "type": "User"},
            "installation": {"id": 123},
        }
        result = extract_intent("issue_comment", payload)
        assert result is not None
        assert result.persona == "architect"
        assert result.trigger == "mentioned"

    def test_mention_product(self):
        """@agent-product mention resolves to product persona."""
        payload = {
            "action": "created",
            "comment": {"body": "@agent-product please groom this epic"},
            "issue": {"number": 42},
            "sender": {"login": "user", "id": 1, "type": "User"},
            "installation": {"id": 123},
        }
        result = extract_intent("issue_comment", payload)
        assert result is not None
        assert result.persona == "product"
        assert result.trigger == "mentioned"

    def test_mention_product_no_collision_with_pm(self):
        """@agent-product and @agent-pm are distinct — no substring collision."""
        payload_product = {
            "action": "created",
            "comment": {"body": "Hey @agent-product can you write acceptance criteria?"},
            "issue": {"number": 10},
            "sender": {"login": "user", "id": 1, "type": "User"},
            "installation": {"id": 123},
        }
        payload_pm = {
            "action": "created",
            "comment": {"body": "Hey @agent-pm can you triage this?"},
            "issue": {"number": 10},
            "sender": {"login": "user", "id": 1, "type": "User"},
            "installation": {"id": 123},
        }
        result_product = extract_intent("issue_comment", payload_product)
        result_pm = extract_intent("issue_comment", payload_pm)
        assert result_product is not None
        assert result_product.persona == "product"
        assert result_pm is not None
        assert result_pm.persona == "pm"

    def test_no_mention_returns_none(self):
        payload = {
            "action": "created",
            "comment": {"body": "This looks good, let's merge it."},
            "issue": {"number": 10},
            "sender": {"login": "user", "id": 1, "type": "User"},
            "installation": {"id": 123},
        }
        result = extract_intent("issue_comment", payload)
        assert result is None

    def test_comment_by_bot_no_correlation_blocked(self):
        """Bot comment with mention but no correlation context → blocked (safe default)."""
        payload = load_fixture("issue_comment_mention.json")
        payload["sender"] = {"login": "github-actions[bot]", "id": 777, "type": "Bot"}
        # No correlation_ctx passed → safe default blocks
        result = extract_intent("issue_comment", payload)
        assert result is None

    def test_empty_comment_body(self):
        payload = {
            "action": "created",
            "comment": {"body": ""},
            "issue": {"number": 10},
            "sender": {"login": "user", "id": 1, "type": "User"},
            "installation": {"id": 123},
        }
        result = extract_intent("issue_comment", payload)
        assert result is None

    def test_all_mention_mappings(self):
        """Every mention in MENTION_TO_PERSONA should produce the expected persona."""
        for mention, expected_persona in MENTION_TO_PERSONA.items():
            payload = {
                "action": "created",
                "comment": {"body": f"{mention} please handle this"},
                "issue": {"number": 10},
                "sender": {"login": "user", "id": 1, "type": "User"},
                "installation": {"id": 123},
            }
            result = extract_intent("issue_comment", payload)
            assert result is not None, f"Mention '{mention}' should produce intent"
            assert result.persona == expected_persona, (
                f"Mention '{mention}' should resolve to '{expected_persona}', got '{result.persona}'"
            )

    def test_first_mention_wins(self):
        """When multiple mentions exist, first match should win."""
        payload = {
            "action": "created",
            "comment": {"body": "Hey @agent-pm and @agent-developer can you both look?"},
            "issue": {"number": 10},
            "sender": {"login": "user", "id": 1, "type": "User"},
            "installation": {"id": 123},
        }
        result = extract_intent("issue_comment", payload)
        assert result is not None
        # The order depends on MENTION_TO_PERSONA dict iteration — both are valid
        assert result.persona in ("pm", "developer")


# --- Installation events ---


class TestInstallationEvents:
    def test_installation_created_returns_none(self):
        """Installation events are logged but don't trigger agent work."""
        payload = load_fixture("installation_created.json")
        result = extract_intent("installation", payload)
        assert result is None


# --- Edge cases ---


class TestEdgeCases:
    def test_unknown_event_type(self):
        payload = {"action": "completed", "sender": {"login": "u", "id": 1, "type": "User"}}
        result = extract_intent("check_run", payload)
        assert result is None

    def test_issues_event_wrong_action(self):
        """issues event with action != labeled should be no-op."""
        payload = {
            "action": "opened",
            "issue": {"number": 1},
            "sender": {"login": "user", "id": 1, "type": "User"},
            "installation": {"id": 123},
        }
        result = extract_intent("issues", payload)
        assert result is None

    def test_issue_comment_edited_ignored(self):
        """Only 'created' action triggers mention parsing."""
        payload = {
            "action": "edited",
            "comment": {"body": "@agent-developer fix this"},
            "issue": {"number": 10},
            "sender": {"login": "user", "id": 1, "type": "User"},
            "installation": {"id": 123},
        }
        result = extract_intent("issue_comment", payload)
        assert result is None


# --- Phase 2-c: Chain-aware bot logic (Issue #786 test matrix) ---


class TestChainAwareBotLogic:
    """Test matrix from Issue #786 — all 10 cases for the chain-aware bot guard."""

    def _bot_sender(self):
        return {"login": "aws-e-adp-agent-ops[bot]", "id": 900, "type": "Bot"}

    def _human_sender(self):
        return {"login": "alice", "id": 100, "type": "User"}

    def _bot_identity(self, bot_kind="operations"):
        return MockResolvedIdentity(
            user_kind="bot",
            bot_kind=bot_kind,
            user_id="bot-ops-123",
        )

    def _human_identity(self):
        return MockResolvedIdentity(
            user_kind="human",
            bot_kind="",
            user_id="user-alice-456",
        )

    def _new_chain_ctx(self):
        return {
            "correlation_id": "corr-new-001",
            "root_human_id": "user-alice-456",
            "triggered_by": None,
            "is_human_rooted": True,
            "is_new_chain": True,
        }

    def _active_chain_ctx(self):
        return {
            "correlation_id": "corr-active-002",
            "root_human_id": "user-alice-456",
            "triggered_by": "bot-ops-123",
            "is_human_rooted": True,
            "is_new_chain": False,
        }

    # 1. Human mentions bot → NEW chain → Intent
    def test_human_mentions_bot_starts_chain(self):
        payload = {
            "action": "created",
            "comment": {"body": "@agent-developer please fix this"},
            "issue": {"number": 55},
            "sender": self._human_sender(),
            "installation": {"id": 123},
        }
        ctx = self._new_chain_ctx()
        identity = self._human_identity()
        result = extract_intent(
            "issue_comment", payload, correlation_ctx=ctx, resolved_identity=identity
        )
        assert result is not None
        assert result.persona == "developer"
        assert result.trigger == "mentioned"

    # 2. Human, no mention → None
    def test_human_no_mention(self):
        payload = {
            "action": "created",
            "comment": {"body": "Looks good to me, let's ship it."},
            "issue": {"number": 55},
            "sender": self._human_sender(),
            "installation": {"id": 123},
        }
        ctx = self._new_chain_ctx()
        identity = self._human_identity()
        result = extract_intent(
            "issue_comment", payload, correlation_ctx=ctx, resolved_identity=identity
        )
        assert result is None

    # 3. Human mentions bot, stale pointer exists → NEW chain (overrides pointer)
    def test_human_starts_new_overrides_pointer(self):
        """Human comment always starts a new chain even if a pointer exists."""
        payload = {
            "action": "created",
            "comment": {"body": "@agent-developer implement this"},
            "issue": {"number": 55},
            "sender": self._human_sender(),
            "installation": {"id": 123},
        }
        # Even with an "active" chain ctx, human sender always creates intent
        # (because determine_correlation for humans always returns is_new_chain=True)
        # Here we test that the intent parser itself doesn't block humans
        ctx = self._new_chain_ctx()  # Human always gets new chain from determine_correlation
        identity = self._human_identity()
        result = extract_intent(
            "issue_comment", payload, correlation_ctx=ctx, resolved_identity=identity
        )
        assert result is not None
        assert result.persona == "developer"

    # 4. Bot mentions another bot, NO pointer → NEW chain → Intent
    @patch("intent_parser._emit_metric")
    def test_bot_starts_new_subchain(self, mock_metric):
        payload = {
            "action": "created",
            "comment": {"body": "@agent-developer please implement this fix"},
            "issue": {"number": 55},
            "sender": self._bot_sender(),
            "installation": {"id": 123},
        }
        ctx = self._new_chain_ctx()
        identity = self._bot_identity(bot_kind="operations")
        result = extract_intent(
            "issue_comment", payload, correlation_ctx=ctx, resolved_identity=identity
        )
        assert result is not None
        assert result.persona == "developer"
        assert result.trigger == "mentioned"
        # Verify BotToBotTrigger metric emitted
        mock_metric.assert_called_with(
            "BotToBotTrigger",
            {"source_bot": "operations", "target_persona": "developer"},
        )

    # 5. Bot mentions another bot, chain depth >= MAX → blocked (depth guard)
    @patch("intent_parser._emit_metric")
    def test_bot_in_active_chain_blocked(self, mock_metric):
        """Bot mention at depth >= MAX_CHAIN_DEPTH is blocked (issue #1696)."""
        payload = {
            "action": "created",
            "comment": {"body": "@agent-developer please implement this fix"},
            "issue": {"number": 55},
            "sender": self._bot_sender(),
            "installation": {"id": 123},
        }
        # Issue #1696: is_new_chain no longer matters; depth guard decides.
        # Set chain_depth >= MAX_CHAIN_DEPTH to trigger blocking.
        ctx = self._active_chain_ctx()
        ctx["chain_depth"] = 8  # MAX_CHAIN_DEPTH default
        identity = self._bot_identity(bot_kind="operations")
        result = extract_intent(
            "issue_comment", payload, correlation_ctx=ctx, resolved_identity=identity
        )
        assert result is None
        # Verify ChainDepthExceeded metric emitted (replaces BotChainContinuationBlocked)
        mock_metric.assert_called_with(
            "ChainDepthExceeded",
            {"persona": "developer", "depth": "8"},
        )

    # 6. Bot self-mention → blocked
    def test_bot_self_mention_blocked(self):
        payload = {
            "action": "created",
            "comment": {"body": "@agent-operations let me think about this more"},
            "issue": {"number": 55},
            "sender": self._bot_sender(),
            "installation": {"id": 123},
        }
        ctx = self._new_chain_ctx()
        # Bot kind matches the persona it's mentioning
        identity = self._bot_identity(bot_kind="operations")
        result = extract_intent(
            "issue_comment", payload, correlation_ctx=ctx, resolved_identity=identity
        )
        assert result is None

    # 7. Bot, no mention → None
    def test_bot_no_mention(self):
        payload = {
            "action": "created",
            "comment": {"body": "PR Ready for Review — all checks passing."},
            "issue": {"number": 55},
            "sender": self._bot_sender(),
            "installation": {"id": 123},
        }
        ctx = self._new_chain_ctx()
        identity = self._bot_identity(bot_kind="operations")
        result = extract_intent(
            "issue_comment", payload, correlation_ctx=ctx, resolved_identity=identity
        )
        assert result is None

    # 8. REGRESSION GUARD: Bot applies label → still blocked (top-level guard preserved)
    def test_bot_label_event_still_blocked(self):
        """Bot-applied labels must STILL be blocked by the top-level guard."""
        payload = {
            "action": "labeled",
            "label": {"name": "developer"},
            "issue": {"number": 1},
            "repository": {"full_name": "org/repo"},
            "sender": {"login": "github-actions[bot]", "id": 777, "type": "Bot"},
            "installation": {"id": 123},
        }
        result = extract_intent("issues", payload)
        assert result is None

    # 9. REGRESSION GUARD: Bot opens PR → still blocked (top-level guard preserved)
    def test_bot_pr_event_still_blocked(self):
        """Bot-opened PRs (e.g. Dependabot) must STILL be blocked."""
        payload = {
            "action": "opened",
            "pull_request": {"number": 42, "head": {"sha": "abc123"}},
            "repository": {"full_name": "org/repo"},
            "sender": {"login": "dependabot[bot]", "id": 777, "type": "Bot"},
            "installation": {"id": 123},
        }
        result = extract_intent("pull_request", payload)
        assert result is None

    # 10. REGRESSION GUARD: Bot no-op does NOT poison channel
    def test_bot_noop_does_not_poison_channel(self):
        """A bot comment with no mention returns None — handler must NOT write pointer.

        This test verifies intent is None, which means the handler's
        order-of-operations logic (pointer write AFTER intent parse) will
        not write a pointer for this event.
        """
        payload = {
            "action": "created",
            "comment": {"body": "Build succeeded. No issues found."},
            "issue": {"number": 55},
            "sender": self._bot_sender(),
            "installation": {"id": 123},
        }
        ctx = self._new_chain_ctx()
        identity = self._bot_identity(bot_kind="operations")
        result = extract_intent(
            "issue_comment", payload, correlation_ctx=ctx, resolved_identity=identity
        )
        # No mention in body → None. Handler will NOT write pointer.
        assert result is None
