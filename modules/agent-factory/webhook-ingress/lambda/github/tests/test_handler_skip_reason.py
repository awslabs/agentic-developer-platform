"""Issue #4020 — the handler must persist the no-op reason, end to end.

``extract_intent_with_reason`` producing a reason is only half the fix; the
reason has to reach the DynamoDB row that Agent Activity reads. This module
drives the real ``handler()`` entry point and asserts on what
``_capture_invocation_event`` was asked to write, so the wiring between the two
is covered rather than each side in isolation.

Also pins that a no-op still returns HTTP 200. The reason now appears in the
response body too, but the status code must not move: GitHub retries 5xx, and
"nobody mentioned an agent" is a completely normal delivery.
"""

import hashlib
import hmac
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

os.environ.setdefault("WEBHOOK_SECRET", "test-secret-123")
os.environ.setdefault("WEBHOOK_SECRET_ARN", "")
os.environ.setdefault(
    "SUBMIT_QUEUE_URL",
    "https://sqs.us-east-1.amazonaws.com/123456789/adp-dev-agent-submit.fifo",
)
os.environ.setdefault("IDENTITY_INDEX_TABLE", "adp-dev-identity-index")
os.environ.setdefault("RATE_LIMITS_TABLE", "adp-dev-rate-limits")
os.environ.setdefault("AWS_REGION", "us-east-1")

WEBHOOK_SECRET = "test-secret-123"


def _make_event(event_type: str, payload: dict) -> dict:
    body = json.dumps(payload)
    sig = hmac.new(WEBHOOK_SECRET.encode("utf-8"), body.encode("utf-8"), hashlib.sha256).hexdigest()
    return {
        "headers": {
            "x-github-event": event_type,
            "content-type": "application/json",
            "x-hub-signature-256": f"sha256={sig}",
        },
        "body": body,
        "isBase64Encoded": False,
    }


def _mock_resolved_identity(tenant_id="acme", user_id="u_test1"):
    from common.identity_resolver import ResolvedIdentity

    return ResolvedIdentity(
        tenant_id=tenant_id,
        org_id=tenant_id,
        user_id=user_id,
        user_provisioning_mode="strict",
    )


def _mock_rate_result():
    mock = MagicMock()
    mock.allowed = True
    mock.retry_after_seconds = 0
    return mock


def _no_mention_comment_payload():
    return {
        "action": "created",
        "comment": {"body": "thanks, looks good"},
        "issue": {
            "number": 1,
            "title": "Something broke",
            "html_url": "https://github.com/acme/repo/issues/1",
        },
        "repository": {"full_name": "acme/repo"},
        "sender": {"login": "user", "id": 1, "type": "User"},
        "installation": {"id": 123},
    }


class _HandlerHarness:
    """Runs handler() with the external dependencies stubbed out.

    Everything patched here is I/O the reason-plumbing does not depend on
    (signature verification, identity resolution, rate limiting, the audit log).
    ``_capture_invocation_event`` is patched so the test can inspect the intended
    DDB write without needing a table.
    """

    @staticmethod
    def run(event_type: str, payload: dict):
        with (
            patch("handler._get_events_log") as mock_log,
            patch("handler._get_rate_limiter") as mock_rate,
            patch("handler._get_identity_resolver") as mock_resolver,
            patch("handler._get_signature") as mock_sig,
            patch("handler._capture_invocation_event") as mock_capture,
        ):
            mock_sig.return_value.verify_github_signature.return_value = True
            mock_resolver.return_value.resolve.return_value = (
                _mock_resolved_identity(),
                "ok",
            )
            mock_rate.return_value.check_and_increment.return_value = _mock_rate_result()
            mock_log.return_value.log_event = MagicMock()

            from handler import handler

            result = handler(_make_event(event_type, payload), None)
            return result, mock_capture


class TestNoOpReasonPersisted:
    def test_draft_pr_records_reason_without_publishing_work(self):
        payload = {
            "action": "opened",
            "pull_request": {
                "number": 3,
                "title": "Work in progress",
                "draft": True,
                "head": {"ref": "agent/issue-3"},
            },
            "repository": {"full_name": "acme/repo"},
            "sender": {"login": "user", "id": 1, "type": "User"},
            "installation": {"id": 123},
        }
        with patch("common.sqs_publisher.publish_envelope") as publish:
            result, capture = _HandlerHarness.run("pull_request", payload)

        assert result["statusCode"] == 200
        assert json.loads(result["body"])["reason"] == "pr_draft"
        assert capture.call_args.kwargs["skip_reason"] == "pr_draft"
        publish.assert_not_called()

    def test_comment_without_mention_writes_no_mention(self):
        result, mock_capture = _HandlerHarness.run("issue_comment", _no_mention_comment_payload())

        assert result["statusCode"] == 200
        mock_capture.assert_called_once()
        kwargs = mock_capture.call_args.kwargs
        assert kwargs["status"] == "no_op"
        assert kwargs["skip_reason"] == "no_mention"

    def test_unmapped_label_writes_label_unmapped(self):
        payload = {
            "action": "labeled",
            "label": {"name": "wontfix"},
            "issue": {
                "number": 2,
                "title": "Nope",
                "html_url": "https://github.com/acme/repo/issues/2",
            },
            "repository": {"full_name": "acme/repo"},
            "sender": {"login": "user", "id": 1, "type": "User"},
            "installation": {"id": 123},
        }
        result, mock_capture = _HandlerHarness.run("issues", payload)

        assert result["statusCode"] == 200
        assert mock_capture.call_args.kwargs["skip_reason"] == "label_unmapped"

    def test_non_agent_pr_branch_writes_pr_branch_not_agent(self):
        payload = {
            "action": "opened",
            "pull_request": {
                "number": 3,
                "title": "My feature",
                "html_url": "https://github.com/acme/repo/pull/3",
                "head": {"ref": "feature/whatever"},
            },
            "repository": {"full_name": "acme/repo"},
            "sender": {"login": "user", "id": 1, "type": "User"},
            "installation": {"id": 123},
        }
        result, mock_capture = _HandlerHarness.run("pull_request", payload)

        assert result["statusCode"] == 200
        assert mock_capture.call_args.kwargs["skip_reason"] == "pr_branch_not_agent"


class TestNoOpResponseBody:
    def test_reason_echoed_in_response(self):
        """Parity with the guard-block response, which always included a reason.

        The response is what a webhook-delivery replay in the GitHub UI shows, so
        it is the one place an operator can already see the reason — it should not
        be the ONLY place, but it should stay consistent with the row.
        """
        result, _ = _HandlerHarness.run("issue_comment", _no_mention_comment_payload())

        body = json.loads(result["body"])
        assert body["status"] == "no_op"
        assert body["reason"] == "no_mention"

    def test_still_returns_200_not_an_error_code(self):
        """A no-op is a successful delivery. A 4xx/5xx here would make GitHub
        mark the webhook unhealthy (and retry, on 5xx)."""
        result, _ = _HandlerHarness.run("issue_comment", _no_mention_comment_payload())
        assert result["statusCode"] == 200


class TestTriggeringPathUnaffected:
    @pytest.mark.parametrize("event_type", ["issues", "pull_request"])
    @patch.dict("os.environ", {"GITHUB_AUTO_PR_REVIEW_ENABLED": "true"})
    def test_dispatched_run_writes_no_skip_reason(self, event_type):
        """Regression: a real dispatch must not acquire a skip_reason.

        The row would otherwise show "Complete" next to an explanation of why
        nothing ran. This drives the mapped-label path, which produces an intent
        and therefore never reaches the no-op branch.
        """
        payload = {
            "action": "labeled",
            "label": {"name": "developer"},
            "issue": {
                "number": 4,
                "title": "Real work",
                "html_url": "https://github.com/acme/repo/issues/4",
            },
            "repository": {"full_name": "acme/repo"},
            "sender": {"login": "user", "id": 1, "type": "User"},
            "installation": {"id": 123},
        }

        if event_type == "pull_request":
            payload["action"] = "ready_for_review"
            payload["pull_request"] = {
                **payload.pop("issue"),
                "draft": False,
                "head": {"ref": "agent/issue-4"},
            }

        with (
            patch("handler._get_events_log") as mock_log,
            patch("handler._get_rate_limiter") as mock_rate,
            patch("handler._get_identity_resolver") as mock_resolver,
            patch("handler._get_signature") as mock_sig,
            patch("handler._capture_invocation_event") as mock_noop_capture,
            patch("common.spawn_persona._capture_invocation_event") as mock_spawn_capture,
            patch("common.spawn_persona._write_pointer_and_provenance"),
            patch("common.sqs_publisher.publish_envelope", return_value="msg-1") as publish,
        ):
            mock_sig.return_value.verify_github_signature.return_value = True
            mock_resolver.return_value.resolve.return_value = (
                _mock_resolved_identity(),
                "ok",
            )
            mock_rate.return_value.check_and_increment.return_value = _mock_rate_result()
            mock_log.return_value.log_event = MagicMock()

            from handler import handler

            result = handler(_make_event(event_type, payload), None)

        # 202 Accepted — queued for the worker. Distinct from the no-op 200,
        # which is what makes the two paths distinguishable in delivery replays.
        assert result["statusCode"] == 202
        publish.assert_called_once()
        assert publish.call_args.args[0]["persona"] == (
            "agent-codex-reviewer" if event_type == "pull_request" else "developer"
        )
        # The no-op capture (the only path that sets skip_reason) never ran.
        mock_noop_capture.assert_not_called()
        # The dispatch capture ran, and carries no skip_reason kwarg.
        mock_spawn_capture.assert_called_once()
        assert "skip_reason" not in mock_spawn_capture.call_args.kwargs
