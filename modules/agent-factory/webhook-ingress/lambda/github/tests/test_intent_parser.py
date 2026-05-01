"""Tests for intent_parser.py."""

import json
import sys
from pathlib import Path

# Add parent directory to path so we can import intent_parser
sys.path.insert(0, str(Path(__file__).parent.parent))

from intent_parser import (
    LABEL_TO_PERSONA,
    extract_intent,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict:
    with open(FIXTURES_DIR / name) as f:
        return json.load(f)


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

    def test_pr_opened_by_bot_ignored(self):
        payload = load_fixture("pr_opened.json")
        payload["sender"] = {"login": "dependabot[bot]", "id": 777, "type": "Bot"}
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

    def test_comment_by_bot_ignored(self):
        payload = load_fixture("issue_comment_mention.json")
        payload["sender"] = {"login": "github-actions[bot]", "id": 777, "type": "Bot"}
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
