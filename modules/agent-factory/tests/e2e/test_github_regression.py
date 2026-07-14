"""
GitHub webhook regression tests — confirm the GitHub path is unaffected by GitLab additions.

Validates that the existing GitHub webhook ingress still processes events correctly
after the GitLab Lambda and API Gateway routes were added. This prevents accidental
regressions introduced by shared infrastructure changes (API Gateway, IAM, SQS).

Run with:
    cd modules/agent-factory
    TEST_ENV=dev python3 -m pytest tests/e2e/test_github_regression.py -v

Requires:
    - WEBHOOK_ENDPOINT: API Gateway URL for the GitHub webhook ingress
    - WEBHOOK_SECRET: HMAC secret for signing GitHub webhook payloads
    - WEBHOOK_SQS_QUEUE_URL: SQS queue URL for dispatch assertions
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import uuid

import boto3
import pytest
import requests

# All tests require a live environment
pytestmark = [pytest.mark.integration, pytest.mark.github_regression]


# ---------------------------------------------------------------------------
# Environment helpers
# ---------------------------------------------------------------------------


def _require_env(name: str) -> str:
    """Return env var or skip the test."""
    val = os.environ.get(name)
    if not val:
        pytest.skip(f"Missing required env var: {name}")
    return val


def _sign_github_payload(payload: str, secret: str) -> str:
    """Generate GitHub HMAC-SHA256 signature."""
    mac = hmac.HMAC(
        key=secret.encode("utf-8"),
        msg=payload.encode("utf-8"),
        digestmod=hashlib.sha256,
    )
    return f"sha256={mac.hexdigest()}"


def _poll_sqs_for_github_message(
    sqs_client,
    queue_url: str,
    delivery_id: str,
    timeout: float = 10.0,
) -> dict | None:
    """Poll SQS for a GitHub message matching the delivery ID.

    Returns the parsed message body or None if not found.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = sqs_client.receive_message(
            QueueUrl=queue_url,
            MaxNumberOfMessages=10,
            WaitTimeSeconds=0,
            MessageAttributeNames=["All"],
        )
        for msg in resp.get("Messages", []):
            try:
                body = json.loads(msg.get("Body", "{}"))
            except (json.JSONDecodeError, TypeError):
                continue
            if body.get("delivery_id") == delivery_id:
                sqs_client.delete_message(QueueUrl=queue_url, ReceiptHandle=msg["ReceiptHandle"])
                return body
        # nosemgrep: arbitrary-sleep — polling interval in deadline-bounded loop
        time.sleep(1.0)
    return None


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestGitHubWebhookRegression:
    """Regression tests confirming the GitHub webhook path still works after GitLab additions."""

    @pytest.fixture(autouse=True)
    def setup_env(self):
        """Resolve required environment variables."""
        self.webhook_endpoint = _require_env("WEBHOOK_ENDPOINT")
        self.webhook_secret = _require_env("WEBHOOK_SECRET")
        self.sqs_queue_url = _require_env("WEBHOOK_SQS_QUEUE_URL")
        self.sqs_client = boto3.client("sqs", region_name=os.environ.get("AWS_REGION", "us-east-1"))
        self.installation_id = os.environ.get("WEBHOOK_TEST_INSTALLATION_ID", "12345678")
        self.sender_id = int(os.environ.get("WEBHOOK_TEST_SENDER_ID", "100001"))

    def test_github_webhook_still_processes_events(self):
        """Existing GitHub webhook path processes issue_comment events correctly.

        This confirms that adding the GitLab Lambda + route did not break
        the GitHub handler, API Gateway routing, or shared SQS queue.
        """
        delivery_id = f"regression-{uuid.uuid4().hex[:12]}"
        payload = {
            "action": "created",
            "issue": {
                "number": 9999,
                "title": "GitHub regression test issue",
                "body": "Verifying GitHub path after GitLab additions.",
                "user": {
                    "login": "regression-tester",
                    "id": self.sender_id,
                    "type": "User",
                },
                "labels": [{"name": "agent-developer"}],
                "html_url": "https://github.com/test-org/test-repo/issues/9999",
            },
            "comment": {
                "body": "@agent-developer confirm GitHub path works",
                "user": {
                    "login": "regression-tester",
                    "id": self.sender_id,
                    "type": "User",
                },
                "html_url": "https://github.com/test-org/test-repo/issues/9999#issuecomment-1",
            },
            "repository": {
                "full_name": "test-org/test-repo",
                "name": "test-repo",
                "owner": {"login": "test-org"},
            },
            "installation": {"id": int(self.installation_id)},
            "sender": {
                "login": "regression-tester",
                "id": self.sender_id,
                "type": "User",
            },
        }

        body_str = json.dumps(payload, separators=(",", ":"))
        signature = _sign_github_payload(body_str, self.webhook_secret)

        headers = {
            "Content-Type": "application/json",
            "X-GitHub-Event": "issue_comment",
            "X-GitHub-Delivery": delivery_id,
            "X-Hub-Signature-256": signature,
            "User-Agent": "GitHub-Hookshot/regression-test",
        }

        resp = requests.post(
            self.webhook_endpoint,
            data=body_str,
            headers=headers,
            timeout=30,
        )

        # The handler validates HMAC first, then checks installation identity.
        # With a synthetic/unregistered installation_id, we expect either:
        #   200 — event processed (if test installation is registered)
        #   403 — "unknown_installation" (HMAC passed, installation not found)
        # Both prove the GitHub path is alive and processing. A broken path
        # would return 401 (HMAC fail) or 5xx.
        assert resp.status_code in (200, 403), (
            f"GitHub webhook path broken (expected 200 or 403): {resp.status_code} {resp.text}"
        )

        if resp.status_code == 200:
            # If accepted, verify SQS message dispatched with correct provider
            sqs_msg = _poll_sqs_for_github_message(
                self.sqs_client, self.sqs_queue_url, delivery_id, timeout=10
            )
            assert sqs_msg is not None, (
                f"No SQS message for GitHub delivery {delivery_id} within 10s — "
                "GitHub webhook path may be broken"
            )
            # Verify it's a GitHub-originated message
            assert sqs_msg.get("event_type") == "issue_comment", (
                f"Expected event_type 'issue_comment', got '{sqs_msg.get('event_type')}'"
            )
        else:
            # 403 with unknown_installation proves HMAC validation passed
            # and the handler is correctly rejecting unregistered installations
            assert "unknown_installation" in resp.text or "unknown_identity" in resp.text, (
                f"Expected unknown_installation rejection, got: {resp.text}"
            )

    def test_github_invalid_signature_still_rejected(self):
        """GitHub webhook with wrong HMAC still returns 401 after GitLab additions.

        Confirms that signature validation wasn't accidentally weakened
        when adding the GitLab token-based authentication path.
        """
        delivery_id = f"regression-sig-{uuid.uuid4().hex[:12]}"
        payload = {
            "action": "labeled",
            "issue": {
                "number": 9999,
                "title": "Signature regression test",
                "body": "Testing signature validation.",
                "user": {
                    "login": "regression-tester",
                    "id": self.sender_id,
                    "type": "User",
                },
                "labels": [{"name": "agent-developer"}],
                "html_url": "https://github.com/test-org/test-repo/issues/9999",
            },
            "label": {"name": "agent-developer"},
            "repository": {
                "full_name": "test-org/test-repo",
                "name": "test-repo",
                "owner": {"login": "test-org"},
            },
            "installation": {"id": int(self.installation_id)},
            "sender": {
                "login": "regression-tester",
                "id": self.sender_id,
                "type": "User",
            },
        }

        body_str = json.dumps(payload, separators=(",", ":"))
        # Deliberately wrong signature
        fake_signature = "sha256=0000000000000000000000000000000000000000000000000000000000000000"

        headers = {
            "Content-Type": "application/json",
            "X-GitHub-Event": "issues",
            "X-GitHub-Delivery": delivery_id,
            "X-Hub-Signature-256": fake_signature,
            "User-Agent": "GitHub-Hookshot/regression-test",
        }

        resp = requests.post(
            self.webhook_endpoint,
            data=body_str,
            headers=headers,
            timeout=30,
        )

        assert resp.status_code == 401, (
            f"Expected 401 for invalid GitHub signature, got {resp.status_code}. "
            "Signature validation may be broken after GitLab additions."
        )
