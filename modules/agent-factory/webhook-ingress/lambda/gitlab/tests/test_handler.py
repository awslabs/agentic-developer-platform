"""Tests for GitLab webhook handler."""

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

# Add parent directories to path
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

# Set required env vars before importing handler
os.environ.setdefault("GITLAB_WEBHOOK_SECRET", "test-gitlab-secret")
os.environ.setdefault("GITLAB_WEBHOOK_SECRET_ARN", "")  # Empty = use env fallback
os.environ.setdefault(
    "SUBMIT_QUEUE_URL",
    "https://sqs.us-east-1.amazonaws.com/123456789/adp-dev-agent-submit.fifo",
)
os.environ.setdefault("ENVIRONMENT", "dev")
os.environ.setdefault("AWS_REGION", "us-east-1")

GITLAB_SECRET = "test-gitlab-secret"


def _make_event(payload: dict, token: str | None = GITLAB_SECRET) -> dict:
    """Build an API Gateway proxy event for a GitLab webhook."""
    body = json.dumps(payload)
    headers = {"content-type": "application/json"}
    if token is not None:
        headers["x-gitlab-token"] = token
    return {
        "headers": headers,
        "body": body,
        "isBase64Encoded": False,
    }


def _sample_note_payload(
    note: str = "@agent please help",
    project_id: int = 123,
    project_path: str = "group/repo",
    issue_iid: int = 42,
    note_id: int = 789,
    username: str = "alice",
) -> dict:
    """Build a sample GitLab note event payload."""
    return {
        "object_kind": "note",
        "project": {
            "id": project_id,
            "path_with_namespace": project_path,
            "web_url": f"https://gitlab.dev.adp.internal/{project_path}",
        },
        "issue": {"iid": issue_iid},
        "object_attributes": {
            "id": note_id,
            "note": note,
            "noteable_type": "Issue",
        },
        "user": {
            "username": username,
            "name": "Test User",
        },
    }


class TestTokenValidation:
    """Tests for X-Gitlab-Token validation."""

    def test_valid_token_accepted(self):
        """Request with valid token proceeds to processing."""
        # Reset cached secret
        import gitlab.handler as h

        h._webhook_secret = None

        event = _make_event(_sample_note_payload())

        with patch.object(h, "_get_sqs_publisher") as mock_sqs:
            mock_sqs.return_value.publish_envelope.return_value = "msg-123"
            result = h.handler(event, None)

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["status"] == "accepted"

    def test_invalid_token_returns_401(self):
        """Request with wrong token returns 401."""
        import gitlab.handler as h

        h._webhook_secret = None

        event = _make_event(_sample_note_payload(), token="wrong-token")
        result = h.handler(event, None)

        assert result["statusCode"] == 401
        body = json.loads(result["body"])
        assert "Invalid" in body["error"]

    def test_missing_token_returns_401(self):
        """Request without X-Gitlab-Token header returns 401."""
        import gitlab.handler as h

        h._webhook_secret = None

        event = _make_event(_sample_note_payload(), token=None)
        result = h.handler(event, None)

        assert result["statusCode"] == 401

    def test_empty_token_returns_401(self):
        """Request with empty token returns 401."""
        import gitlab.handler as h

        h._webhook_secret = None

        event = _make_event(_sample_note_payload(), token="")
        result = h.handler(event, None)

        assert result["statusCode"] == 401


class TestEventParsing:
    """Tests for event parsing and routing."""

    def test_note_event_with_mention_accepted(self):
        """Note event with @agent mention is accepted and queued."""
        import gitlab.handler as h

        h._webhook_secret = None

        payload = _sample_note_payload(note="@agent-developer implement this")
        event = _make_event(payload)

        with patch.object(h, "_get_sqs_publisher") as mock_sqs:
            mock_sqs.return_value.publish_envelope.return_value = "msg-456"
            result = h.handler(event, None)

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["status"] == "accepted"
        assert body["message_id"] == "msg-456"

    def test_note_event_without_mention_ignored(self):
        """Note event without @agent mention returns 200 with ignored status."""
        import gitlab.handler as h

        h._webhook_secret = None

        payload = _sample_note_payload(note="This is just a regular comment")
        event = _make_event(payload)

        result = h.handler(event, None)

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["status"] == "ignored"
        assert "no @agent mention" in body["reason"]

    def test_non_note_event_ignored(self):
        """Non-note events return 200 with ignored status."""
        import gitlab.handler as h

        h._webhook_secret = None

        payload = {"object_kind": "push", "project": {"id": 1, "path_with_namespace": "a/b", "web_url": ""}}
        event = _make_event(payload)

        result = h.handler(event, None)

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["status"] == "ignored"
        assert "unsupported" in body["reason"]

    def test_note_on_merge_request_ignored(self):
        """Note on MR is ignored even with @agent mention."""
        import gitlab.handler as h

        h._webhook_secret = None

        payload = {
            "object_kind": "note",
            "project": {"id": 1, "path_with_namespace": "a/b", "web_url": ""},
            "object_attributes": {
                "id": 1,
                "note": "@agent review this MR",
                "noteable_type": "MergeRequest",
            },
            "user": {"username": "dev", "name": "Dev"},
        }
        event = _make_event(payload)

        result = h.handler(event, None)

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["status"] == "ignored"


class TestSQSMessage:
    """Tests for SQS message construction."""

    def test_sqs_message_schema(self):
        """Verify the SQS message envelope matches expected schema."""
        import gitlab.handler as h

        h._webhook_secret = None

        payload = _sample_note_payload(
            note="@agent-developer please fix the login bug",
            project_id=123,
            project_path="group/repo",
            issue_iid=42,
            note_id=789,
            username="alice",
        )
        event = _make_event(payload)

        published_envelope = None

        def capture_envelope(envelope):
            nonlocal published_envelope
            published_envelope = envelope
            return "msg-789"

        with patch.object(h, "_get_sqs_publisher") as mock_sqs:
            mock_sqs.return_value.publish_envelope.side_effect = capture_envelope
            result = h.handler(event, None)

        assert result["statusCode"] == 200
        assert published_envelope is not None

        # Verify top-level envelope structure
        assert published_envelope["version"] == "1.0"
        assert published_envelope["channel"] == "gitlab"
        assert published_envelope["persona"] == "developer"

        # Verify actor
        assert published_envelope["actor"]["user_id"] == "alice"

        # Verify source_ref
        assert published_envelope["source_ref"]["repo"] == "group/repo"
        assert published_envelope["source_ref"]["issue"] == 42

        # Verify intent
        assert published_envelope["intent"]["trigger"] == "mention"
        assert published_envelope["intent"]["persona"] == "developer"

        # Verify correlation
        assert published_envelope["correlation"]["root_human_id"] == "alice"
        assert published_envelope["correlation"]["is_human_rooted"] is True
        assert published_envelope["correlation"]["correlation_id"] != ""

        # Verify payload (GitLab-specific nested data)
        pl = published_envelope["payload"]
        assert pl["provider"] == "gitlab"
        assert pl["event_type"] == "mention"
        assert pl["source"]["project_id"] == 123
        assert pl["source"]["project_path"] == "group/repo"
        assert pl["source"]["issue_iid"] == 42
        assert pl["source"]["note_id"] == 789
        assert pl["actor"]["username"] == "alice"
        assert pl["content"]["body"] == "@agent-developer please fix the login bug"
        assert pl["content"]["mention_target"] == "developer"

    def test_sqs_publish_failure_returns_500(self):
        """If SQS publish fails, handler returns 500."""
        import gitlab.handler as h

        h._webhook_secret = None

        payload = _sample_note_payload()
        event = _make_event(payload)

        with patch.object(h, "_get_sqs_publisher") as mock_sqs:
            mock_sqs.return_value.publish_envelope.return_value = None
            result = h.handler(event, None)

        assert result["statusCode"] == 500
        body = json.loads(result["body"])
        assert "Failed" in body["error"]


class TestMalformedInput:
    """Tests for malformed input handling."""

    def test_invalid_json_body_returns_400(self):
        """Invalid JSON in body returns 400."""
        import gitlab.handler as h

        h._webhook_secret = None

        event = {
            "headers": {
                "content-type": "application/json",
                "x-gitlab-token": GITLAB_SECRET,
            },
            "body": "not valid json {{{",
            "isBase64Encoded": False,
        }
        result = h.handler(event, None)

        assert result["statusCode"] == 400
        body = json.loads(result["body"])
        assert "Invalid JSON" in body["error"]

    def test_base64_encoded_body(self):
        """Base64 encoded body is properly decoded."""
        import base64

        import gitlab.handler as h

        h._webhook_secret = None

        payload = _sample_note_payload()
        encoded_body = base64.b64encode(json.dumps(payload).encode()).decode()
        event = {
            "headers": {
                "content-type": "application/json",
                "x-gitlab-token": GITLAB_SECRET,
            },
            "body": encoded_body,
            "isBase64Encoded": True,
        }

        with patch.object(h, "_get_sqs_publisher") as mock_sqs:
            mock_sqs.return_value.publish_envelope.return_value = "msg-b64"
            result = h.handler(event, None)

        assert result["statusCode"] == 200

    def test_case_insensitive_headers(self):
        """Headers are handled case-insensitively."""
        import gitlab.handler as h

        h._webhook_secret = None

        payload = _sample_note_payload()
        event = {
            "headers": {
                "Content-Type": "application/json",
                "X-Gitlab-Token": GITLAB_SECRET,
            },
            "body": json.dumps(payload),
            "isBase64Encoded": False,
        }

        with patch.object(h, "_get_sqs_publisher") as mock_sqs:
            mock_sqs.return_value.publish_envelope.return_value = "msg-case"
            result = h.handler(event, None)

        assert result["statusCode"] == 200
