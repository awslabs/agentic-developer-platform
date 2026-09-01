"""Issue #4599 — author-kind reaches the engine-command row, end to end.

`webhook_events.log_event` writing the attribute when told to is only half the
contract; the handler has to actually tell it. This module drives the real
`handler()` entry point with a genuine `issue_comment` payload and asserts on what
`_capture_invocation_event` was asked to write — the same style as
`test_handler_skip_reason.py`, and for the same reason: the two sides passing in
isolation is exactly how a field ends up never being populated in production.

**Why the flag has to travel at all.** The tick reads only what is on the row, and
author-kind was never on it — only the body and the numeric sender id. So the tick
could not tell a human's `accept` from an agent narrating one, and answered both
with "this command cannot be applied by this account" on the issue thread (#4589).

**This is a noise filter, not an authorization boundary.** Bot commands are already
refused on authority: bot identities seed with `role="agent"`, which resolves to
MEMBER and therefore lacks `PLAN_APPROVE`. Nothing here should be read as the thing
that stops a bot approving a plan.
"""

import hashlib
import hmac
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

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


def _engine_comment_payload(sender: dict, body: str = "@agent-engine accept") -> dict:
    return {
        "action": "created",
        "comment": {"body": body},
        "issue": {
            "number": 4599,
            "title": "Engine bridge noise",
            "html_url": "https://github.com/acme/repo/issues/4599",
        },
        "repository": {"full_name": "acme/repo"},
        "sender": sender,
        "installation": {"id": 123},
    }


HUMAN = {"login": "operator", "id": 1, "type": "User"}
# The platform's own agent account. `[bot]` suffix AND type=Bot, which is why no
# `aws-e-adp-agent-*` login-prefix rule is needed — a prefix match would silently
# stop working the day the account is renamed.
AGENT_BOT = {"login": "aws-e-adp-agent-dev[bot]", "id": 200, "type": "Bot"}
# A bot that does NOT carry the `[bot]` login suffix, to pin that `type` alone is
# sufficient. GitHub App senders are not guaranteed to have the suffix.
TYPE_ONLY_BOT = {"login": "some-app", "id": 201, "type": "Bot"}


def _run(payload: dict):
    with (
        patch("handler._get_events_log") as mock_log,
        patch("handler._get_rate_limiter") as mock_rate,
        patch("handler._get_identity_resolver") as mock_resolver,
        patch("handler._get_signature") as mock_sig,
        patch("handler._capture_invocation_event") as mock_capture,
    ):
        mock_sig.return_value.verify_github_signature.return_value = True
        mock_resolver.return_value.resolve.return_value = (_mock_resolved_identity(), "ok")
        rate = MagicMock()
        rate.allowed = True
        rate.retry_after_seconds = 0
        mock_rate.return_value.check_and_increment.return_value = rate
        mock_log.return_value.log_event = MagicMock()

        from handler import handler

        result = handler(_make_event("issue_comment", payload), None)
        return result, mock_capture


class TestTheFlagIsWrittenOnTheEnginePath:
    def test_a_bot_authored_command_is_marked_as_bot(self):
        result, mock_capture = _run(_engine_comment_payload(AGENT_BOT))

        assert result["statusCode"] == 200
        kwargs = mock_capture.call_args.kwargs
        # Still marked as an engine command — the Lambda does not decide anything
        # about it, it only records who wrote it. Suppression is the tick's call.
        assert kwargs["engine_command"] is True
        assert kwargs["sender_is_bot"] is True

    def test_a_bot_without_the_login_suffix_is_still_a_bot(self):
        """`type == "Bot"` alone is sufficient, so a GitHub App sender is caught."""
        _, mock_capture = _run(_engine_comment_payload(TYPE_ONLY_BOT))

        assert mock_capture.call_args.kwargs["sender_is_bot"] is True

    def test_a_human_command_is_not_marked_as_bot(self):
        """The positive control. If this ever flips, every real command is dropped."""
        _, mock_capture = _run(_engine_comment_payload(HUMAN))

        kwargs = mock_capture.call_args.kwargs
        assert kwargs["engine_command"] is True
        assert kwargs["sender_is_bot"] is False

    def test_the_body_and_sender_id_still_travel(self):
        """The new field must not disturb the attributes the tick already needs."""
        _, mock_capture = _run(_engine_comment_payload(HUMAN, body="@agent-engine halt"))

        kwargs = mock_capture.call_args.kwargs
        assert kwargs["comment_body"] == "@agent-engine halt"
        assert kwargs["sender_github_id"] == "1"


class TestOrdinaryDeliveriesAreUnaffected:
    def test_a_non_engine_comment_is_not_marked_as_bot(self):
        """`sender_is_bot` is engine-path-only, like the body and the sender id.

        A bot comment that is not an engine command must not start carrying engine
        attributes — the index is sparse on purpose and the tick must never see it.
        """
        _, mock_capture = _run(_engine_comment_payload(AGENT_BOT, body="thanks, looks good"))

        kwargs = mock_capture.call_args.kwargs
        assert kwargs["engine_command"] is False
        assert kwargs["sender_is_bot"] is False
