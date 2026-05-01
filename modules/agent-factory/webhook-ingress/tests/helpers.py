"""
Helper utilities for webhook ingress integration tests.

Provides:
- sign_payload: Generate GitHub-style HMAC-SHA256 signature
- poll_sqs: Poll SQS queue until messages arrive or timeout
- cleanup_sqs: Delete test messages after assertions
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any


def sign_payload(payload: str, secret: str) -> str:
    """Generate a GitHub webhook HMAC-SHA256 signature.

    Args:
        payload: The raw request body string.
        secret: The webhook secret (shared between GitHub and the endpoint).

    Returns:
        Signature in the format "sha256=<hex_digest>" matching GitHub's
        X-Hub-Signature-256 header format.
    """
    mac = hmac.HMAC(
        key=secret.encode("utf-8"),
        msg=payload.encode("utf-8"),
        digestmod=hashlib.sha256,
    )
    return f"sha256={mac.hexdigest()}"


def poll_sqs(
    sqs_client,
    queue_url: str,
    timeout: float = 10.0,
    interval: float = 1.0,
    max_messages: int = 10,
) -> list[dict[str, Any]]:
    """Poll an SQS queue until messages arrive or timeout.

    Uses short-polling with repeated ReceiveMessage calls to avoid
    long-poll blocking in test environments.

    Args:
        sqs_client: boto3 SQS client.
        queue_url: The SQS queue URL to poll.
        timeout: Maximum seconds to wait for messages.
        interval: Seconds between poll attempts.
        max_messages: Max messages per receive call (1-10).

    Returns:
        List of dicts with keys: 'body' (parsed JSON), 'receipt_handle',
        'message_id', 'raw' (full SQS message dict).
    """
    messages: list[dict[str, Any]] = []
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        resp = sqs_client.receive_message(
            QueueUrl=queue_url,
            MaxNumberOfMessages=min(max_messages, 10),
            WaitTimeSeconds=0,  # Short poll
            MessageAttributeNames=["All"],
        )
        for msg in resp.get("Messages", []):
            body_str = msg.get("Body", "{}")
            try:
                body = json.loads(body_str)
            except (json.JSONDecodeError, TypeError):
                body = {"_raw": body_str}
            messages.append(
                {
                    "body": body,
                    "receipt_handle": msg["ReceiptHandle"],
                    "message_id": msg["MessageId"],
                    "raw": msg,
                }
            )

        if messages:
            return messages

        time.sleep(interval)

    return messages


def cleanup_sqs(sqs_client, queue_url: str, receipt_handles: list[str]) -> None:
    """Delete messages from SQS after test assertions.

    Args:
        sqs_client: boto3 SQS client.
        queue_url: The SQS queue URL.
        receipt_handles: List of receipt handles to delete.
    """
    for handle in receipt_handles:
        try:
            sqs_client.delete_message(
                QueueUrl=queue_url,
                ReceiptHandle=handle,
            )
        except Exception:
            pass  # Best-effort cleanup


def build_github_webhook_headers(
    event_type: str,
    signature: str,
    delivery_id: str = "test-delivery-001",
) -> dict[str, str]:
    """Build standard GitHub webhook HTTP headers.

    Args:
        event_type: GitHub event type (e.g., "issues", "pull_request").
        signature: The HMAC signature (from sign_payload).
        delivery_id: Unique delivery ID for the webhook.

    Returns:
        Dict of HTTP headers matching GitHub's webhook format.
    """
    return {
        "Content-Type": "application/json",
        "X-GitHub-Event": event_type,
        "X-GitHub-Delivery": delivery_id,
        "X-Hub-Signature-256": signature,
        "User-Agent": "GitHub-Hookshot/test",
    }
