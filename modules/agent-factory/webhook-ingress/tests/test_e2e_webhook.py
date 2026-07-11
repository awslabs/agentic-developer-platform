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
import os
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


def _test_sender_id() -> int:
    """Return the synthetic sender ID that matches the seeded identity-index fixture."""
    return int(os.environ.get("WEBHOOK_TEST_SENDER_ID", "100001"))


def _issue_labeled_payload(installation_id: str, sender_type: str = "User") -> dict:
    """Build a minimal issues.labeled webhook payload.

    Uses label "developer" which maps to persona "developer" in LABEL_TO_PERSONA.
    """
    return {
        "action": "labeled",
        "issue": {
            "number": 42,
            "title": "Test issue for E2E webhook",
            "body": "This is an integration test issue.",
            "user": {"login": "test-user", "id": _test_sender_id(), "type": "User"},
            "labels": [{"name": "developer"}],
            "html_url": "https://github.com/test-org-e2e/test-repo/issues/42",
        },
        "label": {"name": "developer"},
        "repository": {
            "full_name": "test-org-e2e/test-repo",
            "name": "test-repo",
            "owner": {"login": "test-org-e2e"},
        },
        "installation": {"id": int(installation_id)},
        "sender": {"login": "test-user", "id": _test_sender_id(), "type": sender_type},
    }


def _pr_opened_payload(installation_id: str) -> dict:
    """Build a minimal pull_request.opened webhook payload.

    Uses head.ref="agent/issue-999" to match the agent/issue-* branch filter
    required by the intent parser for reviewer persona dispatch.
    """
    return {
        "action": "opened",
        "pull_request": {
            "number": 99,
            "title": "feat: add new feature",
            "body": "This PR adds a great feature.",
            "user": {"login": "test-user", "id": _test_sender_id(), "type": "User"},
            "html_url": "https://github.com/test-org-e2e/test-repo/pull/99",
            "head": {"ref": "agent/issue-999", "sha": "abc123"},
            "base": {"ref": "main", "sha": "def456"},
        },
        "repository": {
            "full_name": "test-org-e2e/test-repo",
            "name": "test-repo",
            "owner": {"login": "test-org-e2e"},
        },
        "installation": {"id": int(installation_id)},
        "sender": {"login": "test-user", "id": _test_sender_id(), "type": "User"},
    }


def _issue_comment_payload(installation_id: str, body: str = "Hello") -> dict:
    """Build a minimal issue_comment.created webhook payload."""
    return {
        "action": "created",
        "issue": {
            "number": 42,
            "title": "Test issue",
            "body": "Original issue body.",
            "user": {"login": "test-user", "id": _test_sender_id(), "type": "User"},
            "labels": [],
            "html_url": "https://github.com/test-org-e2e/test-repo/issues/42",
        },
        "comment": {
            "body": body,
            "user": {"login": "test-user", "id": _test_sender_id(), "type": "User"},
            "html_url": "https://github.com/test-org-e2e/test-repo/issues/42#issuecomment-1",
        },
        "repository": {
            "full_name": "test-org-e2e/test-repo",
            "name": "test-repo",
            "owner": {"login": "test-org-e2e"},
        },
        "installation": {"id": int(installation_id)},
        "sender": {"login": "test-user", "id": _test_sender_id(), "type": "User"},
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
        "sender": {"login": "test-user", "id": _test_sender_id(), "type": "User"},
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
        """Fire a properly signed issue.labeled webhook -> assert 202 accepted + SQS envelope.

        The SQS envelope uses a normalized schema (see spawn_persona.py::_build_envelope):
        top-level keys are version, channel, tenant_id, persona, actor, source_ref,
        intent, correlation, payload, arrived_at, message_id. There is NO delivery_id,
        event_type, or action at the top level. We match on the SQS MessageId returned
        in the 202 response (poll_sqs returns it as msg["message_id"]).
        """
        payload = _issue_labeled_payload(test_tenant["installation_id"])
        cleanup_ddb_events.track(unique_delivery_id)

        resp = _send_webhook(
            webhook_endpoint,
            webhook_secret,
            "issues",
            payload,
            delivery_id=unique_delivery_id,
        )

        # Successful dispatch returns 202 with message_id (handler.py:1290)
        assert resp.status_code == 202, f"Expected 202, got {resp.status_code}: {resp.text}"
        resp_body = resp.json()
        assert resp_body.get("status") == "accepted"
        assert "message_id" in resp_body, f"Response missing message_id: {resp_body}"

        sqs_message_id = resp_body["message_id"]

        # Poll SQS for the dispatched message
        messages = poll_sqs(
            sqs_client["client"],
            sqs_client["queue_url"],
            timeout=10,
        )

        assert len(messages) > 0, "No message received in SQS within timeout"

        # Match by SQS MessageId (returned in 202 response, stored as msg["message_id"])
        our_msg = None
        for msg in messages:
            if msg["message_id"] == sqs_message_id:
                our_msg = msg
                break

        assert our_msg is not None, (
            f"Could not find SQS message with MessageId={sqs_message_id}. "
            f"Got: {[m['message_id'] for m in messages]}"
        )

        envelope = our_msg["body"]
        # Envelope uses intent.trigger (not top-level event_type/action)
        assert envelope.get("intent", {}).get("trigger") == "issue_labeled"
        assert envelope.get("persona") == "developer"
        assert envelope.get("tenant_id") == test_tenant["tenant_id"]
        assert envelope.get("source_ref", {}).get("repo") == "test-org-e2e/test-repo"

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

        # The envelope has no top-level delivery_id — this filter is a safety net
        # verifying no message body accidentally references our delivery. Primary
        # assertion is the 401 status code above.
        matching = [m for m in messages if m["body"].get("delivery_id") == unique_delivery_id]
        assert len(matching) == 0, "Message should NOT be dispatched for invalid signature"

        # Cleanup any messages we received (from other tests)
        if messages:
            cleanup_sqs(
                sqs_client["client"],
                sqs_client["queue_url"],
                [m["receipt_handle"] for m in messages],
            )

    def test_unknown_installation_returns_403_no_dispatch(
        self,
        webhook_endpoint,
        webhook_secret,
        sqs_client,
        unique_delivery_id,
        cleanup_ddb_events,
    ):
        """Fire webhook from unregistered installation with no resolvable org -> 403, no SQS.

        The handler self-heals unknown installations when the payload carries a
        resolvable org login (handler.py:988-999). To exercise the 403 path, the
        payload must have NO owner.login so auto-registration cannot succeed.

        The installation_id must be random per run: a fixed id (e.g. 11111111)
        gets auto-registered into the live identity-index by other payload
        shapes and then resolves as a tenant forever after, turning the outcome
        into unknown_user instead of unknown_installation.
        """
        # Random unknown installation id — never registered, and this payload
        # has no org_login so the self-heal path cannot register it either.
        unknown_installation_id = 900_000_000 + (uuid.uuid4().int % 100_000_000)
        payload = {
            "action": "labeled",
            "issue": {
                "number": 42,
                "title": "Test issue for 403 path",
                "body": "This is an integration test issue.",
                "user": {"login": "ghost", "id": 0, "type": "User"},
                "labels": [{"name": "developer"}],
                "html_url": "https://github.com/unknown/unknown/issues/42",
            },
            "label": {"name": "developer"},
            "repository": {
                "full_name": "unknown/unknown",
                "name": "unknown",
                "owner": {},  # Empty owner — no login to resolve
            },
            "installation": {"id": unknown_installation_id},
            "sender": {"login": "ghost", "id": 0, "type": "User"},
        }
        cleanup_ddb_events.track(unique_delivery_id)

        resp = _send_webhook(
            webhook_endpoint,
            webhook_secret,
            "issues",
            payload,
            delivery_id=unique_delivery_id,
        )

        # Lambda returns 403 with unknown_identity error for unresolvable installations
        assert resp.status_code == 403, f"Expected 403, got {resp.status_code}: {resp.text}"
        body = resp.json()
        assert body.get("error") == "unknown_identity"
        assert body.get("outcome") == "unknown_installation"

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
        """Exceed rate limit -> assert 429 response.

        Uses issue_comment with @agent-developer mention so the handler resolves
        identity and reaches the rate limiter (handler.py:1083-1116). The
        rate_limited_tenant fixture pre-exhausts the rate-limit counters.
        Uses a dedicated sender_id that maps to a user in the rate-limited
        tenant's org (seeded by seed_identity_index fixture).
        """
        # Build payload with the rate-limited tenant's dedicated sender_id
        # so identity resolution succeeds and the handler reaches the rate limiter.
        rl_sender_id = rate_limited_tenant["sender_id"]
        payload = {
            "action": "created",
            "issue": {
                "number": 42,
                "title": "Test issue",
                "body": "Original issue body.",
                "user": {"login": "rl-user", "id": rl_sender_id, "type": "User"},
                "labels": [],
                "html_url": "https://github.com/test-org-e2e/test-repo/issues/42",
            },
            "comment": {
                "body": "@agent-developer please fix this bug",
                "user": {"login": "rl-user", "id": rl_sender_id, "type": "User"},
                "html_url": "https://github.com/test-org-e2e/test-repo/issues/42#issuecomment-1",
            },
            "repository": {
                "full_name": "test-org-e2e/test-repo",
                "name": "test-repo",
                "owner": {"login": "test-org-e2e"},
            },
            "installation": {"id": int(rate_limited_tenant["installation_id"])},
            "sender": {"login": "rl-user", "id": rl_sender_id, "type": "User"},
        }
        cleanup_ddb_events.track(unique_delivery_id)

        resp = _send_webhook(
            webhook_endpoint,
            webhook_secret,
            "issue_comment",
            payload,
            delivery_id=unique_delivery_id,
        )

        assert resp.status_code == 429, f"Expected 429, got {resp.status_code}: {resp.text}"
        resp_body = resp.json()
        assert resp_body.get("error") == "Rate limited"
        assert "retry_after" in resp_body

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
        """Fire pull_request.opened with agent/issue-* branch -> 202 + reviewer persona."""
        payload = _pr_opened_payload(test_tenant["installation_id"])
        cleanup_ddb_events.track(unique_delivery_id)

        resp = _send_webhook(
            webhook_endpoint,
            webhook_secret,
            "pull_request",
            payload,
            delivery_id=unique_delivery_id,
        )

        # Successful dispatch returns 202 accepted (handler.py:1290)
        assert resp.status_code == 202, f"Expected 202, got {resp.status_code}: {resp.text}"
        resp_body = resp.json()
        assert resp_body.get("status") == "accepted"
        assert "message_id" in resp_body

        sqs_message_id = resp_body["message_id"]

        # Poll SQS for the dispatched message
        messages = poll_sqs(
            sqs_client["client"],
            sqs_client["queue_url"],
            timeout=10,
        )

        # Match by SQS MessageId (returned in 202 response)
        our_msg = None
        for msg in messages:
            if msg["message_id"] == sqs_message_id:
                our_msg = msg
                break

        assert our_msg is not None, (
            f"No SQS message with MessageId={sqs_message_id}. "
            f"Got: {[m['message_id'] for m in messages]}"
        )

        envelope = our_msg["body"]
        assert envelope.get("intent", {}).get("trigger") == "pr_opened"
        # PR opened should dispatch with reviewer persona
        assert envelope.get("persona") == "reviewer", (
            f"Expected 'reviewer' persona, got '{envelope.get('persona')}'"
        )
        assert envelope.get("tenant_id") == test_tenant["tenant_id"]

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
        """Fire issue_comment with @agent-developer -> 202 accepted + developer persona."""
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

        # Successful dispatch returns 202 accepted (handler.py:1290)
        assert resp.status_code == 202, f"Expected 202, got {resp.status_code}: {resp.text}"
        resp_body = resp.json()
        assert resp_body.get("status") == "accepted"
        assert "message_id" in resp_body

        sqs_message_id = resp_body["message_id"]

        messages = poll_sqs(
            sqs_client["client"],
            sqs_client["queue_url"],
            timeout=10,
        )

        # Match by SQS MessageId (returned in 202 response)
        our_msg = None
        for msg in messages:
            if msg["message_id"] == sqs_message_id:
                our_msg = msg
                break

        assert our_msg is not None, (
            f"No SQS message with MessageId={sqs_message_id}. "
            f"Got: {[m['message_id'] for m in messages]}"
        )

        envelope = our_msg["body"]
        assert envelope.get("intent", {}).get("trigger") == "mentioned"
        assert envelope.get("persona") == "developer", (
            f"Expected 'developer' persona from @agent-developer mention, "
            f"got '{envelope.get('persona')}'"
        )
        assert envelope.get("tenant_id") == test_tenant["tenant_id"]

        cleanup_sqs(
            sqs_client["client"],
            sqs_client["queue_url"],
            [m["receipt_handle"] for m in messages],
        )

    def test_webhook_event_logged_in_ddb(
        self,
        webhook_endpoint,
        webhook_secret,
        sqs_client,
        ddb_client,
        test_tenant,
        unique_delivery_id,
        cleanup_ddb_events,
    ):
        """After a dispatched webhook, assert webhook-events DDB table has the record.

        The DDB table uses PK=event_id + SK=arrived_at, where event_id is the
        envelope's internal UUID (envelope["message_id"]), NOT the SQS MessageId.
        The 202 response's message_id is the SQS MessageId (AWS-generated).

        Strategy: poll SQS to get the envelope, extract envelope["message_id"]
        (the internal UUID written to DDB as event_id), then query DDB with it.
        This also proves the Lambda→SQS→DDB key contract is consistent.
        """
        # Use a mention-based comment to trigger a full dispatch (202 response)
        payload = _issue_comment_payload(
            test_tenant["installation_id"],
            body="@agent-developer check the DDB event log",
        )
        cleanup_ddb_events.track(unique_delivery_id)

        resp = _send_webhook(
            webhook_endpoint,
            webhook_secret,
            "issue_comment",
            payload,
            delivery_id=unique_delivery_id,
        )

        # Successful dispatch returns 202 with SQS MessageId
        assert resp.status_code == 202, f"Expected 202, got {resp.status_code}: {resp.text}"
        resp_body = resp.json()
        sqs_message_id = resp_body.get("message_id")
        assert sqs_message_id, f"Response missing message_id: {resp_body}"

        # Poll SQS to retrieve the envelope and extract the internal event_id
        messages = poll_sqs(
            sqs_client["client"],
            sqs_client["queue_url"],
            timeout=10,
        )

        our_msg = None
        for msg in messages:
            if msg["message_id"] == sqs_message_id:
                our_msg = msg
                break

        assert our_msg is not None, (
            f"No SQS message with MessageId={sqs_message_id}. "
            f"Got: {[m['message_id'] for m in messages]}"
        )

        # The envelope's internal message_id is what DDB uses as event_id (PK)
        envelope = our_msg["body"]
        event_id = envelope.get("message_id")
        assert event_id, f"Envelope missing message_id field: {list(envelope.keys())}"

        # Allow time for DDB eventual consistency (event is logged before SQS
        # publish in spawn_persona, so it should already be there)
        time.sleep(2)

        # Query DDB for the event log entry by event_id (PK).
        # SK is arrived_at — use query with ScanIndexForward=False to get latest.
        events_table = ddb_client["events_table"]
        from boto3.dynamodb.conditions import Key

        try:
            result = events_table.query(
                KeyConditionExpression=Key("event_id").eq(event_id),
                ScanIndexForward=False,
                Limit=1,
            )
        except Exception as e:
            pytest.fail(f"Failed to query webhook-events table: {e}")

        items = result.get("Items", [])
        assert len(items) > 0, f"No event log entry found for event_id={event_id}"

        item = items[0]
        # Validate the logged event has expected fields
        assert item.get("event_type") == "issue_comment"
        assert item.get("action") == "created"
        assert item.get("installation_id") == test_tenant["installation_id"]
        assert item.get("tenant_id") == test_tenant["tenant_id"]
        assert "arrived_at" in item

        # Cleanup SQS
        cleanup_sqs(
            sqs_client["client"],
            sqs_client["queue_url"],
            [m["receipt_handle"] for m in messages],
        )

    def test_mixed_case_headers_accepted(
        self,
        webhook_endpoint,
        webhook_secret,
        sqs_client,
        test_tenant,
        unique_delivery_id,
        cleanup_ddb_events,
    ):
        """Fire webhook with mixed-case headers (REST API style) -> 202 accepted.

        REST API v1 preserves original header case from GitHub (X-Hub-Signature-256,
        X-GitHub-Event). The Lambda must lowercase headers before accessing them.
        This test proves the normalization works end-to-end with a successful dispatch.
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

        # Successful dispatch with mixed-case headers returns 202 accepted
        assert resp.status_code == 202, f"Expected 202, got {resp.status_code}: {resp.text}"
        body = resp.json()
        assert body["status"] == "accepted", (
            f"Expected 'accepted' status with mixed-case headers, got: {body}"
        )
        assert "message_id" in body, f"Response missing message_id: {body}"

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
