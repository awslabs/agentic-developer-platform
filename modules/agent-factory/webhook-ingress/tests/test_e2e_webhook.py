"""
End-to-end integration tests for the hosted webhook ingress pipeline.

Tests the full flow: signed webhook → API Gateway → Lambda → signature validation,
tenant resolution, event logging, rate limit check, SQS message publish.

Dispatch verification uses the webhook-events DDB table (tenant-index GSI),
NOT the submit SQS queue. The queue has a live KEDA-scaled consumer, so
polling it races the agent-worker (messages vanish in seconds → flaky), and
receiving+deleting from it consumes REAL production dispatches (the #3530
dead-letter incident). spawn_persona writes the webhook_received row — with
persona, event_type, action, tenant_id — BEFORE the SQS publish, so a DDB row
with our unique issue number proves the dispatch path end-to-end without ever
touching the queue.

Run with:
    cd modules/agent-factory/webhook-ingress
    TEST_ENV=dev make test-integration-webhook

Requires:
    - WEBHOOK_ENDPOINT: API Gateway URL for the webhook ingress
    - WEBHOOK_SECRET: Shared HMAC secret for signing payloads
    - WEBHOOK_EVENTS_TABLE: DynamoDB webhook-events table for dispatch assertions
    - DynamoDB tables deployed (webhook-events, tenant-registry)
"""

from __future__ import annotations

import json
import os
import time
import uuid

import pytest
import requests
from boto3.dynamodb.conditions import Attr, Key

from .helpers import (
    build_github_webhook_headers,
    sign_payload,
)

# All tests in this file require a live environment
pytestmark = [pytest.mark.integration]


def _test_sender_id() -> int:
    """Return the synthetic sender ID that matches the seeded identity-index fixture."""
    return int(os.environ.get("WEBHOOK_TEST_SENDER_ID", "100001"))


def _unique_issue_number() -> int:
    """Random issue/PR number so each test's DDB event row is unambiguous."""
    return 100_000 + (uuid.uuid4().int % 900_000)


def _wait_for_ddb_event(
    events_table,
    tenant_id: str,
    issue_number: int,
    timeout: float = 15.0,
    interval: float = 1.0,
) -> dict | None:
    """Poll the webhook-events tenant-index GSI for the event row.

    Matches on our unique issue_number within the tenant's most recent events.
    Returns the item or None on timeout.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = events_table.query(
            IndexName="tenant-index",
            KeyConditionExpression=Key("tenant_id").eq(tenant_id),
            FilterExpression=Attr("issue_number").eq(issue_number),
            ScanIndexForward=False,
            Limit=50,
        )
        items = resp.get("Items", [])
        if items:
            return items[0]
        # nosemgrep: arbitrary-sleep — polling interval in deadline-bounded loop
        time.sleep(interval)
    return None


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
        ddb_client,
        test_tenant,
        unique_delivery_id,
        cleanup_ddb_events,
    ):
        """Fire a properly signed issue.labeled webhook -> 202 accepted + DDB event row.

        Dispatch is verified via the webhook-events row spawn_persona writes
        BEFORE the SQS publish — never by polling the live submit queue (see
        module docstring). A row with our unique issue number carrying the
        resolved persona proves the full dispatch path ran.
        """
        issue_number = _unique_issue_number()
        payload = _issue_labeled_payload(test_tenant["installation_id"])
        payload["issue"]["number"] = issue_number
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

        item = _wait_for_ddb_event(
            ddb_client["events_table"],
            test_tenant["tenant_id"],
            issue_number,
        )
        assert item is not None, (
            f"No webhook-events row for tenant={test_tenant['tenant_id']} "
            f"issue={issue_number} — dispatch row was never written"
        )
        assert item.get("persona") == "developer"
        assert item.get("event_type") == "issues"
        assert item.get("action") == "labeled"
        assert item.get("installation_id") == test_tenant["installation_id"]

    def test_invalid_signature_returns_401(
        self,
        webhook_endpoint,
        webhook_secret,
        ddb_client,
        test_tenant,
        unique_delivery_id,
        cleanup_ddb_events,
    ):
        """Fire webhook with wrong HMAC -> assert 401 and no dispatch row."""
        issue_number = _unique_issue_number()
        payload = _issue_labeled_payload(test_tenant["installation_id"])
        payload["issue"]["number"] = issue_number
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

        # Signature is rejected before any processing — no event row may exist
        # for our unique issue number (short timeout: absence check).
        item = _wait_for_ddb_event(
            ddb_client["events_table"],
            test_tenant["tenant_id"],
            issue_number,
            timeout=5,
        )
        assert item is None, f"No event should be logged for invalid signature, got: {item}"

    def test_unknown_installation_returns_403_no_dispatch(
        self,
        webhook_endpoint,
        webhook_secret,
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

        # Lambda returns 403 with unknown_identity error for unresolvable
        # installations. The 403 fires before spawn_persona, so no dispatch
        # row can exist — the status code is the complete assertion.
        assert resp.status_code == 403, f"Expected 403, got {resp.status_code}: {resp.text}"
        body = resp.json()
        assert body.get("error") == "unknown_identity"
        assert body.get("outcome") == "unknown_installation"

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
        test_tenant,
        unique_delivery_id,
        cleanup_ddb_events,
    ):
        """Fire webhook for event type we don't handle -> 200 no_op.

        Push events never reach spawn_persona (intent parser returns None),
        so the 200 no_op response IS the no-dispatch proof.
        """
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
        assert resp.json().get("status") == "no_op"

    def test_pr_opened_dispatches_reviewer(
        self,
        webhook_endpoint,
        webhook_secret,
        ddb_client,
        test_tenant,
        unique_delivery_id,
        cleanup_ddb_events,
    ):
        """Fire pull_request.opened with agent/issue-* branch -> 202 + reviewer persona."""
        pr_number = _unique_issue_number()
        payload = _pr_opened_payload(test_tenant["installation_id"])
        payload["pull_request"]["number"] = pr_number
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

        item = _wait_for_ddb_event(
            ddb_client["events_table"],
            test_tenant["tenant_id"],
            pr_number,
        )
        assert item is not None, (
            f"No webhook-events row for tenant={test_tenant['tenant_id']} "
            f"pr={pr_number} — dispatch row was never written"
        )
        # PR opened on an agent/issue-* branch dispatches the reviewer persona
        assert item.get("persona") == "reviewer", (
            f"Expected 'reviewer' persona, got '{item.get('persona')}'"
        )
        assert item.get("event_type") == "pull_request"
        assert item.get("action") == "opened"

    def test_mention_in_comment_dispatches_persona(
        self,
        webhook_endpoint,
        webhook_secret,
        ddb_client,
        test_tenant,
        unique_delivery_id,
        cleanup_ddb_events,
    ):
        """Fire issue_comment with @agent-developer -> 202 accepted + developer persona."""
        issue_number = _unique_issue_number()
        payload = _issue_comment_payload(
            test_tenant["installation_id"],
            body="@agent-developer please review this code and suggest improvements",
        )
        payload["issue"]["number"] = issue_number
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

        item = _wait_for_ddb_event(
            ddb_client["events_table"],
            test_tenant["tenant_id"],
            issue_number,
        )
        assert item is not None, (
            f"No webhook-events row for tenant={test_tenant['tenant_id']} "
            f"issue={issue_number} — dispatch row was never written"
        )
        assert item.get("persona") == "developer", (
            f"Expected 'developer' persona from @agent-developer mention, "
            f"got '{item.get('persona')}'"
        )
        assert item.get("event_type") == "issue_comment"
        assert item.get("action") == "created"

    def test_webhook_event_logged_in_ddb(
        self,
        webhook_endpoint,
        webhook_secret,
        ddb_client,
        test_tenant,
        unique_delivery_id,
        cleanup_ddb_events,
    ):
        """After a dispatched webhook, assert the full webhook-events record shape.

        The table uses PK=event_id (envelope's internal UUID) + SK=arrived_at.
        We locate the row via the tenant-index GSI using our unique issue
        number, then verify the primary-key fields and lifecycle attributes
        are present and correct.
        """
        issue_number = _unique_issue_number()
        # Use a mention-based comment to trigger a full dispatch (202 response)
        payload = _issue_comment_payload(
            test_tenant["installation_id"],
            body="@agent-developer check the DDB event log",
        )
        payload["issue"]["number"] = issue_number
        cleanup_ddb_events.track(unique_delivery_id)

        resp = _send_webhook(
            webhook_endpoint,
            webhook_secret,
            "issue_comment",
            payload,
            delivery_id=unique_delivery_id,
        )

        assert resp.status_code == 202, f"Expected 202, got {resp.status_code}: {resp.text}"

        item = _wait_for_ddb_event(
            ddb_client["events_table"],
            test_tenant["tenant_id"],
            issue_number,
        )
        assert item is not None, f"No event log entry found for issue={issue_number}"

        # Validate the logged event has the expected schema
        assert item.get("event_type") == "issue_comment"
        assert item.get("action") == "created"
        assert item.get("installation_id") == test_tenant["installation_id"]
        assert item.get("tenant_id") == test_tenant["tenant_id"]
        assert "arrived_at" in item
        assert item.get("event_id"), "Row missing event_id PK"
        # Status starts as webhook_received; the agent-worker may UpdateItem it
        # to a later lifecycle value before we read — only assert presence.
        assert item.get("status"), "Row missing status attribute"

    def test_mixed_case_headers_accepted(
        self,
        webhook_endpoint,
        webhook_secret,
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
        payload["issue"]["number"] = _unique_issue_number()
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

    def test_bot_sender_ignored(
        self,
        webhook_endpoint,
        webhook_secret,
        ddb_client,
        test_tenant,
        unique_delivery_id,
        cleanup_ddb_events,
    ):
        """Fire webhook with sender.type=Bot -> 200, no dispatch row."""
        issue_number = _unique_issue_number()
        payload = _issue_labeled_payload(
            test_tenant["installation_id"],
            sender_type="Bot",
        )
        payload["issue"]["number"] = issue_number
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

        # Bot-blocked events must not produce a webhook_received dispatch row
        item = _wait_for_ddb_event(
            ddb_client["events_table"],
            test_tenant["tenant_id"],
            issue_number,
            timeout=5,
        )
        assert item is None or item.get("status") != "webhook_received", (
            f"Bot sender should NOT dispatch, got row: {item}"
        )
