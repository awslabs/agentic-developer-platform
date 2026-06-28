"""Tests for /model directive parsing — Issue #2279.

Tests the /model directive extraction, line-anchored regex behavior,
and coexistence with existing @agent-X matching, dispatch markers,
and loop guards.
"""

import sys
from pathlib import Path

# Add parent directory to path so we can import intent_parser
sys.path.insert(0, str(Path(__file__).parent.parent))

from intent_parser import (
    _extract_model_directive,
    extract_intent,
)


class TestModelDirectiveExtraction:
    """Tests for _extract_model_directive() — the /model regex."""

    def test_basic_model_directive(self):
        """/model opus on its own line is parsed correctly."""
        body = "@agent-developer refactor the auth module\n/model opus"
        result = _extract_model_directive(body)
        assert result == "opus"

    def test_model_directive_sonnet(self):
        """/model sonnet is parsed."""
        body = "@agent-reviewer review this PR\n/model sonnet"
        result = _extract_model_directive(body)
        assert result == "sonnet"

    def test_model_directive_haiku(self):
        """/model haiku is parsed."""
        body = "@agent-developer quick fix\n/model haiku"
        result = _extract_model_directive(body)
        assert result == "haiku"

    def test_model_directive_full_alias(self):
        """/model with a longer alias like claude-opus-4 is parsed."""
        body = "@agent-developer work on this\n/model claude-opus-4"
        result = _extract_model_directive(body)
        assert result == "claude-opus-4"

    def test_model_directive_first_match_wins(self):
        """When multiple /model lines exist, first match wins."""
        body = "@agent-developer do stuff\n/model opus\n/model haiku"
        result = _extract_model_directive(body)
        assert result == "opus"

    def test_no_model_directive_returns_none(self):
        """Comment without /model returns None."""
        body = "@agent-developer just do the thing"
        result = _extract_model_directive(body)
        assert result is None

    def test_empty_body_returns_none(self):
        """Empty body returns None."""
        result = _extract_model_directive("")
        assert result is None

    def test_none_body_returns_none(self):
        """None body returns None (edge case from missing comment body)."""
        result = _extract_model_directive(None)
        assert result is None

    def test_model_in_inline_code_not_matched(self):
        """``/model opus`` in inline code is NOT matched (doesn't start at line beginning)."""
        body = "@agent-developer use `/model opus` to choose the model"
        result = _extract_model_directive(body)
        assert result is None

    def test_model_in_blockquote_not_matched(self):
        """> /model opus in a blockquote is NOT matched (starts with >)."""
        body = "@agent-developer\n> /model opus\ndo the thing"
        result = _extract_model_directive(body)
        assert result is None

    def test_model_in_code_block_not_matched(self):
        """/model inside a fenced code block starts with spaces/indent, not matched."""
        body = "@agent-developer\n```\n  /model opus\n```"
        result = _extract_model_directive(body)
        assert result is None

    def test_model_embedded_in_sentence_not_matched(self):
        """/model embedded in prose (not line-start) is NOT matched."""
        body = "@agent-developer you can use /model opus to select"
        result = _extract_model_directive(body)
        assert result is None

    def test_model_with_trailing_whitespace(self):
        """/model with trailing spaces is still parsed."""
        body = "@agent-developer do work\n/model opus   "
        result = _extract_model_directive(body)
        assert result == "opus"

    def test_model_at_start_of_body(self):
        """/model can be the first line of the body."""
        body = "/model opus\n@agent-developer do work"
        result = _extract_model_directive(body)
        assert result == "opus"

    def test_model_with_extra_spaces_between(self):
        """/model with multiple spaces before alias is parsed."""
        body = "@agent-developer\n/model   haiku"
        result = _extract_model_directive(body)
        assert result == "haiku"


class TestModelDirectiveInIntent:
    """Tests that /model directive is correctly threaded into Intent."""

    def test_human_comment_with_model_sets_intent_model(self):
        """Human comment with /model → Intent.model is set."""
        payload = {
            "action": "created",
            "comment": {
                "id": 12345,
                "body": "@agent-developer refactor this\n/model opus",
                "user": {"login": "alice", "id": 100},
            },
            "issue": {"number": 42, "title": "Refactor auth"},
            "repository": {"full_name": "org/repo"},
            "sender": {"login": "alice", "id": 100, "type": "User"},
            "installation": {"id": 555},
        }
        result = extract_intent("issue_comment", payload)
        assert result is not None
        assert result.persona == "developer"
        assert result.trigger == "mentioned"
        assert result.model == "opus"

    def test_human_comment_without_model_has_none(self):
        """Human comment without /model → Intent.model is None (back-compat)."""
        payload = {
            "action": "created",
            "comment": {
                "id": 12345,
                "body": "@agent-developer fix the bug",
                "user": {"login": "alice", "id": 100},
            },
            "issue": {"number": 42, "title": "Bug fix"},
            "repository": {"full_name": "org/repo"},
            "sender": {"login": "alice", "id": 100, "type": "User"},
            "installation": {"id": 555},
        }
        result = extract_intent("issue_comment", payload)
        assert result is not None
        assert result.persona == "developer"
        assert result.model is None

    def test_bot_comment_ignores_model_directive(self):
        """Bot comments don't parse /model (they use dispatch markers)."""
        payload = {
            "action": "created",
            "comment": {
                "id": 12345,
                "body": "<!-- adp-dispatch:developer -->\n/model opus\ndo work",
                "user": {"login": "bot[bot]", "id": 900},
            },
            "issue": {"number": 42, "title": "Task"},
            "repository": {"full_name": "org/repo"},
            "sender": {"login": "bot[bot]", "id": 900, "type": "Bot"},
            "installation": {"id": 555},
        }
        # Bot path requires correlation_ctx and marker; without marker,
        # the bot path returns None (no dispatch). Model is not relevant here.
        result = extract_intent("issue_comment", payload)
        # With a valid dispatch marker but no correlation_ctx, intent is None
        # (safe default for bot senders without correlation context)
        assert result is None

    def test_model_does_not_interfere_with_mention_matching(self):
        """Presence of /model does not prevent @agent-X from being matched."""
        payload = {
            "action": "created",
            "comment": {
                "id": 12345,
                "body": "/model haiku\n@agent-architect review this design",
                "user": {"login": "bob", "id": 200},
            },
            "issue": {"number": 99, "title": "Design review"},
            "repository": {"full_name": "org/repo"},
            "sender": {"login": "bob", "id": 200, "type": "User"},
            "installation": {"id": 555},
        }
        result = extract_intent("issue_comment", payload)
        assert result is not None
        assert result.persona == "architect"
        assert result.model == "haiku"

    def test_issue_labeled_does_not_have_model(self):
        """Issues.labeled events don't parse /model (label-triggered)."""
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
        assert result.model is None

    def test_pr_event_does_not_have_model(self):
        """PR events don't parse /model (trigger is branch-based, not comment)."""
        payload = {
            "action": "opened",
            "pull_request": {
                "number": 5,
                "head": {"ref": "agent/issue-42", "sha": "abc123"},
                "body": "/model opus",
            },
            "repository": {"full_name": "org/repo"},
            "sender": {"login": "user", "id": 100, "type": "User"},
            "installation": {"id": 123},
        }
        result = extract_intent("pull_request", payload)
        assert result is not None
        assert result.persona == "reviewer"
        assert result.model is None


class TestModelDirectiveRegression:
    """Regression tests: /model must NOT break existing dispatch/loop logic."""

    def test_no_mention_with_model_only_returns_none(self):
        """A comment with only /model and no @agent-X mention → no intent."""
        payload = {
            "action": "created",
            "comment": {
                "id": 12345,
                "body": "/model opus\nplease do the thing",
                "user": {"login": "alice", "id": 100},
            },
            "issue": {"number": 42, "title": "Task"},
            "repository": {"full_name": "org/repo"},
            "sender": {"login": "alice", "id": 100, "type": "User"},
            "installation": {"id": 555},
        }
        result = extract_intent("issue_comment", payload)
        # No @agent-X mention → no intent, regardless of /model
        assert result is None

    def test_model_directive_does_not_match_model_word_in_prose(self):
        """The word 'model' in normal prose is NOT matched as a directive."""
        body = "@agent-developer the model needs updating"
        result = _extract_model_directive(body)
        assert result is None
