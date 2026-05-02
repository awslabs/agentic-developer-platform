"""
End-to-end integration tests for the hosted webhook ingress pipeline.

Tests the full flow: signed webhook → API Gateway → Lambda → signature validation,
tenant resolution, event logging, rate limit check, SQS message publish.

Run with:
    cd modules/agent-factory/webhook-ingress
    TEST_ENV=dev make test-integration-webhook

Requires:
    - WEBHOOK_ENDPOINT: API Gateway URL for the webhook ingress
    - WEBHOOK_SECRET: Shared HMAC secret for signing payloads
    - WEBHOOK_SQS_QUEUE_URL: SQS queue URL for dispatch assertions
    - DynamoDB tables deployed (webhook-events, tenant-registry)
"""

from __future__ import annotations

import json
import time
import uuid

import pytest
import requests

from .helpers import (
    build_github_webhook_headers,
    cleanup_sqs,
    poll_sqs,
    sign_payload,
)

# All tests in this file require a live environment
pytestmark = [pytest.mark.integration]


def _issue_labeled_payload(installation_id: str, sender_type: str = "User") -> dict:
    """Build a minimal issues.labeled webhook payload."""
    return {
        "action": "labeled",
        "issue": {
            "number": 42,
            "title": "Test issue for E2E webhook",
            "body": "This is an integration test issue.",
            "user": {"login": "test-user", "type": "User"},
            "labels": [{"name": "agent-developer"}],
            "html_url": "https://github.com/test-org-e2e/test-repo/issues/42",
        },
        "label": {"name": "agent-developer"},
        "repository": {
            "full_name": "test-org-e2e/test-repo",
            "name": "test-repo",
            "owner": {"login": "test-org-e2e"},
        },
        "installation": {"id": int(installation_id)},
        "sender": {"login": "test-user", "type": sender_type},
    }


def _pr_opened_payload(installation_id: str) -> dict:
    """Build a minimal pull_request.opened webhook payload."""
    return {
        "action": "opened",
        "pull_request": {
            "number": 99,
            "title": "feat: add new feature",
            "body": "This PR adds a great feature.",
            "user": {"login": "test-user", "type": "User"},
            "html_url": "https://github.com/test-org-e2e/test-repo/pull/99",
            "head": {"ref": "feature/new-thing", "sha": "abc123"},
            "base": {"ref": "main", "sha": "def456"},
        },
        "repository": {
            "full_name": "test-org-e2e/test-repo",
            "name": "test-repo",
            "owner": {"login": "test-org-e2e"},
        },
        "installation": {"id": int(installation_id)},
        "sender": {"login": "test-user", "type": "User"},
    }


def _issue_comment_payload(installation_id: str, body: str = "Hello") -> dict:
    """Build a minimal issue_comment.created webhook payload."""
    return {
        "action": "created",
        "issue": {
            "number": 42,
            "title": "Test issue",
            "body": "Original issue body.",
            "user": {"login": "test-user", "type": "User"},
            "labels": [],
            "html_url": "https://github.com/test-org-e2e/test-repo/issues/42",
        },
        "comment": {
            "body": body,
            "user": {"login": "test-user", "type": "User"},
            "html_url": "https://github.com/test-org-e2e/test-repo/issues/42#issuecomment-1",
        },
        "repository": {
            "full_name": "test-org-e2e/test-repo",
            "name": "test-repo",
            "owner": {"login": "test-org-e2e"},
        },
        "installation": {"id": int(installation_id)},
        "sender": {"login": "test-user", "type": "User"},
    }


def _push_payload(installation_id: str) -> dict:
    """Build a minimal push webhook payload (non-actionable event)."""
    return {
        "ref": "refs/heads/main",
        "before": "0000000000000000000000000000000000000000",
        "after": "abc123def456",
        "repository": {
            "full_name": "test-org-e2e/test-repo",
            "name": "test-repo",
            "owner": {"login": "test-org-e2e"},
        },
        "installation": {"id": int(installation_id)},
        "sender": {"login": "test-user", "type": "User"},
        "pusher": {"name": "test-user"},
    }


def _send_webhook(
    endpoint: str,
    secret: str,
    event_type: str,
    payload: dict,
    delivery_id: str | None = None,
    override_signature: str | None = None,
) -> requests.Response:
    """Send a webhook request to the endpoint.

    Args:
        endpoint: The webhook URL.
        secret: HMAC secret for signing.
        event_type: GitHub event type header value.
        payload: JSON payload dict.
        delivery_id: Unique delivery ID (auto-generated if None).
        override_signature: If set, use this instead of computing HMAC.

    Returns:
        requests.Response object.
    """
    if delivery_id is None:
        delivery_id = f"test-{uuid.uuid4().hex[:12]}"

    body_str = json.dumps(payload, separators=(",", ":"))

    if override_signature is not None:
        signature = override_signature
    else:
        signature = sign_payload(body_str, secret)

    headers = build_github_webhook_headers(event_type, signature, delivery_id)

    return requests.post(
        endpoint,
        data=body_str,
        headers=headers,
        timeout=30,
    )


class TestWebhookE2E:
    """End-to-end integration tests for the webhook ingress pipeline."""

    def test_valid_issue_labeled_webhook_dispatches_to_sqs(
        self,
        webhook_endpoint,
        webhook_secret,
        sqs_client,
        test_tenant,
        unique_delivery_id,
        cleanup_ddb_events,
    ):
        """Fire a properly signed issue.labeled webhook -> assert SQS receives envelope."""
        payload = _issue_labeled_payload(test_tenant["installation_id"])
        cleanup_ddb_events.track(unique_delivery_id)

        resp = _send_webhook(
            webhook_endpoint,
            webhook_secret,
            "issues",
            payload,
            delivery_id=unique_delivery_id,
        )

        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"

        # Poll SQS for the dispatched message
        messages = poll_sqs(
            sqs_client["client"],
            sqs_client["queue_url"],
            timeout=10,
        )

        assert len(messages) > 0, "No message received in SQS within timeout"

        # Find our message by delivery_id or installation_id
        our_msg = None
        for msg in messages:
            body = msg["body"]
            if (
                body.get("delivery_id") == unique_delivery_id
                or body.get("installation_id") == test_tenant["installation_id"]
            ):
                our_msg = msg
                break

        assert our_msg is not None, (
            f"Could not find message for delivery {unique_delivery_id} in SQS. "
            f"Got: {[m['body'].get('delivery_id') for m in messages]}"
        )

        envelope = our_msg["body"]
        assert envelope.get("event_type") == "issues"
        assert envelope.get("action") == "labeled"
        assert envelope.get("tenant_id") == test_tenant["tenant_id"]

        # Cleanup SQS
        cleanup_sqs(
            sqs_client["client"],
            sqs_client["queue_url"],
            [m["receipt_handle"] for m in messages],
        )

    def test_invalid_signature_returns_401(
        self,
        webhook_endpoint,
        webhook_secret,
        sqs_client,
        test_tenant,
        unique_delivery_id,
        cleanup_ddb_events,
    ):
        """Fire webhook with wrong HMAC -> assert 401 and no SQS message."""
        payload = _issue_labeled_payload(test_tenant["installation_id"])
        cleanup_ddb_events.track(unique_delivery_id)

        resp = _send_webhook(
            webhook_endpoint,
            webhook_secret,
            "issues",
            payload,
            delivery_id=unique_delivery_id,
            override_signature="sha256=0000000000000000000000000000000000000000000000000000000000000000",
        )

        assert resp.status_code == 401, f"Expected 401, got {resp.status_code}: {resp.text}"

        # Verify no SQS message was dispatched
        messages = poll_sqs(
            sqs_client["client"],
            sqs_client["queue_url"],
            timeout=3,
        )

        matching = [m for m in messages if m["body"].get("delivery_id") == unique_delivery_id]
        assert len(matching) == 0, "Message should NOT be dispatched for invalid signature"

        # Cleanup any messages we received (from other tests)
        if messages:
            cleanup_sqs(
                sqs_client["client"],
                sqs_client["queue_url"],
                [m["receipt_handle"] for m in messages],
            )

    def test_unknown_installation_returns_200_no_dispatch(
        self,
        webhook_endpoint,
        webhook_secret,
        sqs_client,
        unique_delivery_id,
        cleanup_ddb_events,
    ):
        """Fire webhook from unregistered installation -> 200 but no SQS."""
        # Use an installation_id that doesn't exist in tenant-registry
        payload = _issue_labeled_payload("11111111")
        cleanup_ddb_events.track(unique_delivery_id)

        resp = _send_webhook(
            webhook_endpoint,
            webhook_secret,
            "issues",
            payload,
            delivery_id=unique_delivery_id,
        )

        # Should return 200 (ack the webhook) but not dispatch
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"

        # Verify no SQS message for this delivery
        messages = poll_sqs(
            sqs_client["client"],
            sqs_client["queue_url"],
            timeout=3,
        )

        matching = [m for m in messages if m["body"].get("delivery_id") == unique_delivery_id]
        assert len(matching) == 0, "Message should NOT be dispatched for unknown installation"

        if messages:
            cleanup_sqs(
                sqs_client["client"],
                sqs_client["queue_url"],
                [m["receipt_handle"] for m in messages],
            )

    def test_rate_limited_tenant_returns_429(
        self,
        webhook_endpoint,
        webhook_secret,
        rate_limited_tenant,
        unique_delivery_id,
        cleanup_ddb_events,
    ):
        """Exceed rate limit -> assert 429 response."""
        payload = _issue_labeled_payload(rate_limited_tenant["installation_id"])
        cleanup_ddb_events.track(unique_delivery_id)

        resp = _send_webhook(
            webhook_endpoint,
            webhook_secret,
            "issues",
            payload,
            delivery_id=unique_delivery_id,
        )

        assert resp.status_code == 429, f"Expected 429, got {resp.status_code}: {resp.text}"

    def test_no_actionable_intent_returns_200_no_dispatch(
        self,
        webhook_endpoint,
        webhook_secret,
        sqs_client,
        test_tenant,
        unique_delivery_id,
        cleanup_ddb_events,
    ):
        """Fire webhook for event type we don't handle -> 200, no SQS."""
        # Push events are not actionable for agent dispatch
        payload = _push_payload(test_tenant["installation_id"])
        cleanup_ddb_events.track(unique_delivery_id)

        resp = _send_webhook(
            webhook_endpoint,
            webhook_secret,
            "push",
            payload,
            delivery_id=unique_delivery_id,
        )

        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"

        # Verify no SQS dispatch
        messages = poll_sqs(
            sqs_client["client"],
            sqs_client["queue_url"],
            timeout=3,
        )

        matching = [m for m in messages if m["body"].get("delivery_id") == unique_delivery_id]
        assert len(matching) == 0, "Push events should NOT dispatch to SQS"

        if messages:
            cleanup_sqs(
                sqs_client["client"],
                sqs_client["queue_url"],
                [m["receipt_handle"] for m in messages],
            )

    def test_pr_opened_dispatches_reviewer(
        self,
        webhook_endpoint,
        webhook_secret,
        sqs_client,
        test_tenant,
        unique_delivery_id,
        cleanup_ddb_events,
    ):
        """Fire pull_request.opened -> assert reviewer persona in SQS envelope."""
        payload = _pr_opened_payload(test_tenant["installation_id"])
        cleanup_ddb_events.track(unique_delivery_id)

        resp = _send_webhook(
            webhook_endpoint,
            webhook_secret,
            "pull_request",
            payload,
            delivery_id=unique_delivery_id,
        )

        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"

        # Poll SQS for the dispatched message
        messages = poll_sqs(
            sqs_client["client"],
            sqs_client["queue_url"],
            timeout=10,
        )

        our_msg = None
        for msg in messages:
            body = msg["body"]
            if body.get("delivery_id") == unique_delivery_id:
                our_msg = msg
                break

        assert our_msg is not None, f"No SQS message for delivery {unique_delivery_id}"

        envelope = our_msg["body"]
        assert envelope.get("event_type") == "pull_request"
        assert envelope.get("action") == "opened"
        # PR opened should dispatch with reviewer persona
        assert envelope.get("persona") == "reviewer", (
            f"Expected 'reviewer' persona, got '{envelope.get('persona')}'"
        )

        cleanup_sqs(
            sqs_client["client"],
            sqs_client["queue_url"],
            [m["receipt_handle"] for m in messages],
        )

    def test_mention_in_comment_dispatches_persona(
        self,
        webhook_endpoint,
        webhook_secret,
        sqs_client,
        test_tenant,
        unique_delivery_id,
        cleanup_ddb_events,
    ):
        """Fire issue_comment with @agent-developer -> assert developer persona."""
        payload = _issue_comment_payload(
            test_tenant["installation_id"],
            body="@agent-developer please review this code and suggest improvements",
        )
        cleanup_ddb_events.track(unique_delivery_id)

        resp = _send_webhook(
            webhook_endpoint,
            webhook_secret,
            "issue_comment",
            payload,
            delivery_id=unique_delivery_id,
        )

        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"

        messages = poll_sqs(
            sqs_client["client"],
            sqs_client["queue_url"],
            timeout=10,
        )

        our_msg = None
        for msg in messages:
            body = msg["body"]
            if body.get("delivery_id") == unique_delivery_id:
                our_msg = msg
                break

        assert our_msg is not None, f"No SQS message for delivery {unique_delivery_id}"

        envelope = our_msg["body"]
        assert envelope.get("event_type") == "issue_comment"
        assert envelope.get("persona") == "developer", (
            f"Expected 'developer' persona from @agent-developer mention, "
            f"got '{envelope.get('persona')}'"
        )

        cleanup_sqs(
            sqs_client["client"],
            sqs_client["queue_url"],
            [m["receipt_handle"] for m in messages],
        )

    def test_webhook_event_logged_in_ddb(
        self,
        webhook_endpoint,
        webhook_secret,
        ddb_client,
        test_tenant,
        unique_delivery_id,
        cleanup_ddb_events,
    ):
        """After any webhook, assert webhook-events DDB table has the record."""
        payload = _issue_labeled_payload(test_tenant["installation_id"])
        cleanup_ddb_events.track(unique_delivery_id)

        resp = _send_webhook(
            webhook_endpoint,
            webhook_secret,
            "issues",
            payload,
            delivery_id=unique_delivery_id,
        )

        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"

        # Allow time for async DDB write
        time.sleep(2)

        # Query DDB for the event log entry
        events_table = ddb_client["events_table"]
        try:
            result = events_table.get_item(Key={"delivery_id": unique_delivery_id})
        except Exception as e:
            pytest.fail(f"Failed to query webhook-events table: {e}")

        item = result.get("Item")
        assert item is not None, f"No event log entry found for delivery_id={unique_delivery_id}"

        # Validate the logged event has expected fields
        assert item.get("event_type") == "issues"
        assert item.get("action") == "labeled"
        assert item.get("installation_id") == test_tenant["installation_id"]
        assert item.get("tenant_id") == test_tenant["tenant_id"]
        assert "received_at" in item

    def test_mixed_case_headers_accepted(
        self,
        webhook_endpoint,
        webhook_secret,
        sqs_client,
        test_tenant,
        unique_delivery_id,
        cleanup_ddb_events,
    ):
        """Fire webhook with mixed-case headers (REST API style) -> 200 accepted.

        REST API v1 preserves original header case from GitHub (X-Hub-Signature-256,
        X-GitHub-Event). The Lambda must lowercase headers before accessing them.
        This test proves the normalization works end-to-end.
        """
        payload = _issue_labeled_payload(test_tenant["installation_id"])
        cleanup_ddb_events.track(unique_delivery_id)

        delivery_id = unique_delivery_id
        body_str = json.dumps(payload, separators=(",", ":"))
        signature = sign_payload(body_str, webhook_secret)

        # Send with mixed-case headers (as REST API v1 preserves from GitHub)
        headers = {
            "Content-Type": "application/json",
            "X-GitHub-Event": "issues",
            "X-GitHub-Delivery": delivery_id,
            "X-Hub-Signature-256": signature,
            "User-Agent": "GitHub-Hookshot/test",
        }

        resp = requests.post(
            webhook_endpoint,
            data=body_str,
            headers=headers,
            timeout=30,
        )

        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        body = resp.json()
        assert body["status"] == "accepted", (
            f"Expected 'accepted' status with mixed-case headers, got: {body}"
        )

        # Cleanup SQS
        messages = poll_sqs(
            sqs_client["client"],
            sqs_client["queue_url"],
            timeout=5,
        )
        if messages:
            cleanup_sqs(
                sqs_client["client"],
                sqs_client["queue_url"],
                [m["receipt_handle"] for m in messages],
            )

    def test_bot_sender_ignored(
        self,
        webhook_endpoint,
        webhook_secret,
        sqs_client,
        test_tenant,
        unique_delivery_id,
        cleanup_ddb_events,
    ):
        """Fire webhook with sender.type=Bot -> 200, no SQS dispatch."""
        payload = _issue_labeled_payload(
            test_tenant["installation_id"],
            sender_type="Bot",
        )
        cleanup_ddb_events.track(unique_delivery_id)

        resp = _send_webhook(
            webhook_endpoint,
            webhook_secret,
            "issues",
            payload,
            delivery_id=unique_delivery_id,
        )

        # Should acknowledge but not dispatch
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"

        # Verify no SQS message
        messages = poll_sqs(
            sqs_client["client"],
            sqs_client["queue_url"],
            timeout=3,
        )

        matching = [m for m in messages if m["body"].get("delivery_id") == unique_delivery_id]
        assert len(matching) == 0, "Bot sender should NOT dispatch to SQS"

        if messages:
            cleanup_sqs(
                sqs_client["client"],
                sqs_client["queue_url"],
                [m["receipt_handle"] for m in messages],
            )
