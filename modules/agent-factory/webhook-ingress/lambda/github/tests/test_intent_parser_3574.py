"""Tests for /aws-label directive parsing — Issue #3574.

Tests the /aws-label directive extraction, charset validation,
coexistence with /model directive, and correct threading into Intent.
"""

import sys
from pathlib import Path

# Add parent directory to path so we can import intent_parser
sys.path.insert(0, str(Path(__file__).parent.parent))

from intent_parser import (
    _extract_aws_label_directive,
    extract_intent,
)


class TestAwsLabelDirectiveExtraction:
    """Tests for _extract_aws_label_directive() — the /aws-label regex + charset."""

    def test_basic_aws_label(self):
        """/aws-label foo on its own line is parsed correctly."""
        body = "@agent-operations run this.\n/aws-label adp-integration-test"
        result = _extract_aws_label_directive(body)
        assert result == "adp-integration-test"

    def test_aws_label_with_underscores(self):
        """/aws-label with underscores is valid."""
        body = "@agent-operations do deploy\n/aws-label my_account_label"
        result = _extract_aws_label_directive(body)
        assert result == "my_account_label"

    def test_aws_label_alphanumeric(self):
        """/aws-label with digits is valid."""
        body = "/aws-label account123"
        result = _extract_aws_label_directive(body)
        assert result == "account123"

    def test_aws_label_max_length(self):
        """/aws-label with exactly 64 chars is valid."""
        label = "a" * 64
        body = f"/aws-label {label}"
        result = _extract_aws_label_directive(body)
        assert result == label

    def test_aws_label_too_long_rejected(self):
        """/aws-label with >64 chars is rejected (charset validation)."""
        label = "a" * 65
        body = f"/aws-label {label}"
        result = _extract_aws_label_directive(body)
        assert result is None

    def test_aws_label_invalid_chars_rejected(self):
        """/aws-label with invalid chars (dots, spaces) is rejected."""
        body = "/aws-label my.account.label"
        result = _extract_aws_label_directive(body)
        assert result is None

    def test_aws_label_special_chars_rejected(self):
        """/aws-label with special chars (@, !) is rejected."""
        body = "/aws-label bad@label!"
        result = _extract_aws_label_directive(body)
        assert result is None

    def test_aws_label_empty_value_not_matched(self):
        """/aws-label with no value (just the directive) is not matched."""
        body = "/aws-label\n@agent-operations"
        result = _extract_aws_label_directive(body)
        assert result is None

    def test_no_aws_label_returns_none(self):
        """Comment without /aws-label returns None."""
        body = "@agent-operations just do the thing"
        result = _extract_aws_label_directive(body)
        assert result is None

    def test_empty_body_returns_none(self):
        """Empty body returns None."""
        result = _extract_aws_label_directive("")
        assert result is None

    def test_none_body_returns_none(self):
        """None body returns None."""
        result = _extract_aws_label_directive(None)
        assert result is None

    def test_aws_label_in_inline_code_not_matched(self):
        """`/aws-label foo` in inline code is NOT matched."""
        body = "@agent-operations use `/aws-label foo` to select"
        result = _extract_aws_label_directive(body)
        assert result is None

    def test_aws_label_in_blockquote_not_matched(self):
        """> /aws-label foo in a blockquote is NOT matched."""
        body = "@agent-operations\n> /aws-label foo\ndo the thing"
        result = _extract_aws_label_directive(body)
        assert result is None

    def test_aws_label_embedded_in_sentence_not_matched(self):
        """/aws-label in middle of a line is NOT matched."""
        body = "@agent-operations you can use /aws-label foo to select"
        result = _extract_aws_label_directive(body)
        assert result is None

    def test_aws_label_with_trailing_whitespace(self):
        """/aws-label with trailing spaces is still parsed."""
        body = "@agent-operations\n/aws-label my-label   "
        result = _extract_aws_label_directive(body)
        assert result == "my-label"

    def test_aws_label_at_start_of_body(self):
        """/aws-label can be the first line."""
        body = "/aws-label prod-account\n@agent-operations deploy"
        result = _extract_aws_label_directive(body)
        assert result == "prod-account"

    def test_aws_label_first_match_wins(self):
        """When multiple /aws-label lines exist, first match wins."""
        body = "/aws-label first\n/aws-label second"
        result = _extract_aws_label_directive(body)
        assert result == "first"


class TestAwsLabelInIntent:
    """Tests that /aws-label is correctly threaded into Intent."""

    def test_human_comment_with_aws_label_sets_intent(self):
        """Human comment with /aws-label → Intent.aws_label is set."""
        payload = {
            "action": "created",
            "comment": {
                "id": 12345,
                "body": "@agent-operations run this\n/aws-label adp-integration-test",
                "user": {"login": "alice", "id": 100},
            },
            "issue": {"number": 42, "title": "Deploy"},
            "repository": {"full_name": "org/repo"},
            "sender": {"login": "alice", "id": 100, "type": "User"},
            "installation": {"id": 555},
        }
        result = extract_intent("issue_comment", payload)
        assert result is not None
        assert result.persona == "operations"
        assert result.aws_label == "adp-integration-test"

    def test_human_comment_without_aws_label_has_none(self):
        """Human comment without /aws-label → Intent.aws_label is None."""
        payload = {
            "action": "created",
            "comment": {
                "id": 12345,
                "body": "@agent-operations deploy the thing",
                "user": {"login": "alice", "id": 100},
            },
            "issue": {"number": 42, "title": "Deploy"},
            "repository": {"full_name": "org/repo"},
            "sender": {"login": "alice", "id": 100, "type": "User"},
            "installation": {"id": 555},
        }
        result = extract_intent("issue_comment", payload)
        assert result is not None
        assert result.persona == "operations"
        assert result.aws_label is None

    def test_aws_label_coexists_with_model(self):
        """/aws-label and /model in the same comment both parse correctly."""
        payload = {
            "action": "created",
            "comment": {
                "id": 12345,
                "body": "@agent-operations deploy\n/model opus\n/aws-label my-account",
                "user": {"login": "alice", "id": 100},
            },
            "issue": {"number": 42, "title": "Deploy"},
            "repository": {"full_name": "org/repo"},
            "sender": {"login": "alice", "id": 100, "type": "User"},
            "installation": {"id": 555},
        }
        result = extract_intent("issue_comment", payload)
        assert result is not None
        assert result.model == "opus"
        assert result.aws_label == "my-account"

    def test_invalid_aws_label_yields_none_on_intent(self):
        """Invalid charset /aws-label → Intent.aws_label is None (lenient)."""
        payload = {
            "action": "created",
            "comment": {
                "id": 12345,
                "body": "@agent-operations deploy\n/aws-label bad.label!",
                "user": {"login": "alice", "id": 100},
            },
            "issue": {"number": 42, "title": "Deploy"},
            "repository": {"full_name": "org/repo"},
            "sender": {"login": "alice", "id": 100, "type": "User"},
            "installation": {"id": 555},
        }
        result = extract_intent("issue_comment", payload)
        assert result is not None
        assert result.persona == "operations"
        assert result.aws_label is None

    def test_issue_labeled_does_not_have_aws_label(self):
        """Issues.labeled events don't parse /aws-label."""
        payload = {
            "action": "labeled",
            "label": {"name": "developer"},
            "issue": {"number": 1},
            "repository": {"full_name": "org/repo"},
            "sender": {"login": "user", "id": 100, "type": "User"},
            "installation": {"id": 123},
        }
        result = extract_intent("issues", payload)
        assert result is not None
        assert result.persona == "developer"
        assert result.aws_label is None

    def test_pr_event_does_not_have_aws_label(self):
        """PR events don't parse /aws-label."""
        payload = {
            "action": "opened",
            "pull_request": {
                "number": 5,
                "head": {"ref": "agent/issue-42", "sha": "abc123"},
                "body": "/aws-label something",
            },
            "repository": {"full_name": "org/repo"},
            "sender": {"login": "user", "id": 100, "type": "User"},
            "installation": {"id": 123},
        }
        result = extract_intent("pull_request", payload)
        assert result is not None
        assert result.persona == "reviewer"
        assert result.aws_label is None


class TestAwsLabelRegression:
    """Regression tests: /aws-label must NOT break existing dispatch logic."""

    def test_no_mention_with_aws_label_only_returns_none(self):
        """Comment with only /aws-label and no @agent-X mention → no intent."""
        payload = {
            "action": "created",
            "comment": {
                "id": 12345,
                "body": "/aws-label my-account\nplease do the thing",
                "user": {"login": "alice", "id": 100},
            },
            "issue": {"number": 42, "title": "Task"},
            "repository": {"full_name": "org/repo"},
            "sender": {"login": "alice", "id": 100, "type": "User"},
            "installation": {"id": 555},
        }
        result = extract_intent("issue_comment", payload)
        assert result is None

    def test_model_directive_unaffected_by_aws_label(self):
        """/model continues to work correctly with or without /aws-label."""
        payload = {
            "action": "created",
            "comment": {
                "id": 12345,
                "body": "@agent-developer fix the bug\n/model haiku",
                "user": {"login": "alice", "id": 100},
            },
            "issue": {"number": 42, "title": "Bug"},
            "repository": {"full_name": "org/repo"},
            "sender": {"login": "alice", "id": 100, "type": "User"},
            "installation": {"id": 555},
        }
        result = extract_intent("issue_comment", payload)
        assert result is not None
        assert result.model == "haiku"
        assert result.aws_label is None
