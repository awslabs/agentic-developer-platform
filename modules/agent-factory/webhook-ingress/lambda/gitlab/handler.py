"""GitLab webhook ingress Lambda handler.

Validates X-Gitlab-Token, parses GitLab note events for @agent mentions,
and publishes qualifying events to the shared agent-submit SQS FIFO queue.

Completely separate from the GitHub webhook handler — no shared code paths
except the common SQS publisher utility.

Target execution time: <300ms.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import uuid
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Environment variables
GITLAB_WEBHOOK_SECRET_ARN = os.environ.get("GITLAB_WEBHOOK_SECRET_ARN", "")
SUBMIT_QUEUE_URL = os.environ.get("SUBMIT_QUEUE_URL", "")
ENVIRONMENT = os.environ.get("ENVIRONMENT", "dev")
PLACEHOLDER_WEBHOOK_SECRET = "PLACEHOLDER_REPLACE_WITH_ACTUAL_SECRET"

# Cached webhook secret
_webhook_secret: str | None = None

# Lazy imports for fast cold start
_secrets_mod = None
_sqs_mod = None


def _get_secrets():
    global _secrets_mod
    if _secrets_mod is None:
        from common import secrets

        _secrets_mod = secrets
    return _secrets_mod


def _get_sqs_publisher():
    global _sqs_mod
    if _sqs_mod is None:
        from common import sqs_publisher

        _sqs_mod = sqs_publisher
    return _sqs_mod


def _resolve_webhook_secret() -> str:
    """Resolve the GitLab webhook secret from Secrets Manager (cached).

    Falls back to GITLAB_WEBHOOK_SECRET env var for local dev/testing.
    """
    global _webhook_secret
    if _webhook_secret is not None:
        return _webhook_secret

    if GITLAB_WEBHOOK_SECRET_ARN:
        _webhook_secret = _get_secrets().get_secret(GITLAB_WEBHOOK_SECRET_ARN)
    else:
        _webhook_secret = os.environ.get("GITLAB_WEBHOOK_SECRET", "")

    return _webhook_secret


def _response(status_code: int, body: dict[str, Any]) -> dict[str, Any]:
    """Build an API Gateway proxy response."""
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


def _validate_token(headers: dict[str, str]) -> bool:
    """Validate the X-Gitlab-Token header against the stored secret.

    GitLab sends the configured secret token in the X-Gitlab-Token header.
    This is a simple string comparison (not HMAC).
    """
    # GitLab headers are case-insensitive; API Gateway lowercases them
    token = headers.get("x-gitlab-token", "")
    if not token:
        logger.warning("Missing X-Gitlab-Token header")
        return False

    secret = _resolve_webhook_secret()
    if not secret:
        logger.error("No GitLab webhook secret configured")
        return False

    # A fresh Terraform deployment historically installed this public value.
    # Never treat it as authentication, even before webhook setup is complete.
    if hmac.compare_digest(secret, PLACEHOLDER_WEBHOOK_SECRET):
        logger.error("GitLab webhook secret is still the insecure placeholder")
        return False

    # Constant-time comparison to prevent timing attacks
    return hmac.compare_digest(token, secret)


def _registered_project(headers: dict) -> dict | None:
    """A secret is scoped to one immutable project on one trusted instance.

    Protected mode stores a JSON list in the existing webhook secret. Reusing
    the global token across projects cannot authenticate their tenant roots.
    """
    try:
        rows = json.loads(_resolve_webhook_secret())
        token = headers.get("x-gitlab-token", "")
        if not token or not isinstance(rows, list) or len(rows) > 100:
            return None
        matches = []
        tokens = []
        for row in rows:
            secret = row["token"]
            if (
                not isinstance(secret, str)
                or len(secret) < 32
                or secret == PLACEHOLDER_WEBHOOK_SECRET
            ):
                return None
            if secret in tokens:
                return None
            tokens.append(secret)
            if hmac.compare_digest(token, secret):
                if (
                    not isinstance(row["project_id"], int)
                    or row["project_id"] < 1
                    or not row["instance"].startswith("https://")
                ):
                    return None
                matches.append(row)
        return matches[0] if len(matches) == 1 else None
    except (ValueError, KeyError, TypeError, AttributeError):
        return None


def _build_sqs_message(parsed_event) -> dict[str, Any]:
    """Build the SQS message envelope compatible with the agent-worker consumer.

    Uses the same WebhookEnvelope-compatible schema so the downstream
    agent-worker SQS consumer can process GitLab events identically to
    GitHub events.
    """
    now = datetime.now(UTC).isoformat()
    webhook_id = str(uuid.uuid4())

    return {
        "version": "1.0",
        "channel": "gitlab",
        "tenant_id": "",  # GitLab events don't go through tenant resolution yet
        "persona": parsed_event.mention_target,
        "actor": {
            "user_id": parsed_event.author_username,
            "org_id": "",
            "github_id": 0,
            "github_login": "",
            "is_bot": False,
        },
        "source_ref": {
            "installation_id": 0,
            "repo": parsed_event.project_path,
            "issue": parsed_event.issue_iid,
            "pr": None,
            "sha": None,
        },
        "intent": {
            "trigger": "mention",
            "label": None,
            "persona": parsed_event.mention_target,
        },
        "correlation": {
            "correlation_id": webhook_id,
            "root_human_id": parsed_event.author_username,
            "is_human_rooted": True,
        },
        "payload": {
            "provider": "gitlab",
            "event_type": "mention",
            "source": {
                "project_id": parsed_event.project_id,
                "project_path": parsed_event.project_path,
                "issue_iid": parsed_event.issue_iid,
                "note_id": parsed_event.note_id,
            },
            "actor": {
                "username": parsed_event.author_username,
                "display_name": parsed_event.author_name,
            },
            "content": {
                "body": parsed_event.body,
                "mention_target": parsed_event.mention_target,
            },
            "metadata": {
                "timestamp": now,
                "webhook_id": webhook_id,
            },
        },
        "arrived_at": now,
        "model_requested": None,
        "model_resolved": None,
    }


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Lambda entry point for GitLab webhook events.

    Expects API Gateway proxy integration events with:
    - headers["x-gitlab-token"]: shared secret for validation
    - body: JSON-encoded GitLab webhook payload
    """
    # Extract headers (API Gateway lowercases them)
    headers = event.get("headers") or {}
    headers = {k.lower(): v for k, v in headers.items()}

    # Validate token
    protected = os.environ.get("ADP_GITLAB_MODEL_POLICY_ENABLED", "false").lower() == "true"
    registration = _registered_project(headers) if protected else None
    if (protected and registration is None) or (not protected and not _validate_token(headers)):
        logger.warning("GitLab webhook token validation failed")
        return _response(401, {"error": "Invalid or missing token"})

    registration = registration or {}

    # Parse body
    body = event.get("body", "")
    if event.get("isBase64Encoded", False):
        import base64

        body = base64.b64decode(body).decode("utf-8")

    try:
        payload = json.loads(body) if isinstance(body, str) else body
    except (json.JSONDecodeError, TypeError) as e:
        logger.error("Failed to parse request body: %s", e)
        return _response(400, {"error": "Invalid JSON body"})

    if not isinstance(payload, dict):
        return _response(400, {"error": "Invalid webhook body"})
    if protected and payload.get("project", {}).get("id") != registration["project_id"]:
        return _response(403, {"error": "Webhook project does not match registration"})

    # Parse the event
    from event_parser import parse_event

    parsed = parse_event(payload)

    logger.info(
        "GitLab webhook: object_kind=%s actionable=%s project=%s reason=%s",
        payload.get("object_kind", ""),
        parsed.is_actionable,
        parsed.project_path,
        parsed.reason,
    )

    # If not actionable, acknowledge but don't queue
    if not parsed.is_actionable:
        return _response(
            200,
            {
                "status": "ignored",
                "reason": parsed.reason,
            },
        )

    # Build and publish SQS message
    envelope = _build_sqs_message(parsed)
    sqs_publisher = _get_sqs_publisher()
    if protected:
        user_id = payload.get("user", {}).get("id")
        if type(user_id) is not int or user_id < 1 or payload.get("user", {}).get("bot") is True:
            return _response(403, {"error": "Canonical human identity required"})
        from common.personas import MENTION_TO_PERSONA

        requested = envelope["persona"]
        envelope["persona"] = (
            "developer"
            if requested == "agent"
            else MENTION_TO_PERSONA.get(f"@agent-{requested}", requested)
        )
        envelope["intent"]["persona"] = envelope["persona"]
        envelope["message_id"] = envelope["correlation"]["correlation_id"]
        message_id = sqs_publisher.publish_envelope(
            envelope,
            model_root={
                "source": "gitlab",
                "subject": str(user_id),
                "instance": registration["instance"],
                "project_id": registration["project_id"],
            },
        )
    else:
        message_id = sqs_publisher.publish_envelope(envelope)

    if message_id:
        logger.info(
            "GitLab event queued: project=%s issue=%d note=%d sqs_id=%s",
            parsed.project_path,
            parsed.issue_iid or 0,
            parsed.note_id or 0,
            message_id,
        )
        return _response(
            200,
            {
                "status": "accepted",
                "message_id": message_id,
            },
        )
    else:
        logger.error(
            "Failed to queue GitLab event: project=%s issue=%d",
            parsed.project_path,
            parsed.issue_iid or 0,
        )
        return _response(500, {"error": "Failed to queue event"})
