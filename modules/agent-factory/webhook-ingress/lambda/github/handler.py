"""GitHub webhook Lambda — full handler.

Entry point for the GitHub channel webhook Lambda. Validates signatures,
parses events into agent intents, and publishes normalized envelopes to SQS.

Target execution time: <300ms.
This Lambda does NOT clone repos, call LLMs, or do heavy computation.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Environment variables
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
SUBMIT_QUEUE_URL = os.environ.get("SUBMIT_QUEUE_URL", "")

# Lazy imports to keep cold start fast — these modules import boto3
_signature_mod = None
_tenant_mod = None
_rate_limit_mod = None
_sqs_mod = None
_events_log_mod = None


def _get_signature():
    global _signature_mod
    if _signature_mod is None:
        from common import signature

        _signature_mod = signature
    return _signature_mod


def _get_tenant_resolver():
    global _tenant_mod
    if _tenant_mod is None:
        from common import tenant_resolver

        _tenant_mod = tenant_resolver
    return _tenant_mod


def _get_rate_limit():
    global _rate_limit_mod
    if _rate_limit_mod is None:
        from common import rate_limit

        _rate_limit_mod = rate_limit
    return _rate_limit_mod


def _get_sqs_publisher():
    global _sqs_mod
    if _sqs_mod is None:
        from common import sqs_publisher

        _sqs_mod = sqs_publisher
    return _sqs_mod


def _get_events_log():
    global _events_log_mod
    if _events_log_mod is None:
        from common import webhook_events_log

        _events_log_mod = webhook_events_log
    return _events_log_mod


def handler(event: dict, context) -> dict:
    """Lambda entry point for GitHub webhook processing.

    Args:
        event: API Gateway v2 HTTP event.
        context: Lambda context object.

    Returns:
        API Gateway v2 response dict.
    """
    start_time = time.time()

    # 1. Extract headers + body from API Gateway v2 event
    headers = event.get("headers", {})
    raw_body = event.get("body", "")
    is_base64 = event.get("isBase64Encoded", False)

    if is_base64:
        body_bytes = base64.b64decode(raw_body)
    else:
        body_bytes = raw_body.encode("utf-8") if isinstance(raw_body, str) else raw_body

    # 2. Verify HMAC signature
    signature_header = headers.get("x-hub-signature-256", "")
    if not _get_signature().verify_github_signature(body_bytes, signature_header, WEBHOOK_SECRET):
        _log_outcome(
            event_type="unknown",
            action="",
            installation_id=0,
            tenant_id=None,
            repo="",
            persona=None,
            outcome="invalid_signature",
            start_time=start_time,
        )
        return _response(401, {"error": "Invalid signature"})

    # 3. Parse event type from X-GitHub-Event header
    event_type = headers.get("x-github-event", "")
    if not event_type:
        return _response(400, {"error": "Missing X-GitHub-Event header"})

    # Parse body
    try:
        payload = json.loads(body_bytes)
    except (json.JSONDecodeError, ValueError):
        return _response(400, {"error": "Invalid JSON body"})

    action = payload.get("action", "")

    # 4. Extract installation_id from payload
    installation_id = payload.get("installation", {}).get("id", 0)
    repo = payload.get("repository", {}).get("full_name", "")

    # 5. Resolve tenant
    tenant = None
    if installation_id:
        tenant = _get_tenant_resolver().resolve_tenant(installation_id)

    # 6. If unknown installation → log + return 200 (don't error)
    if not tenant:
        _log_outcome(
            event_type=event_type,
            action=action,
            installation_id=installation_id,
            tenant_id=None,
            repo=repo,
            persona=None,
            outcome="unknown_tenant",
            start_time=start_time,
        )
        return _response(200, {"status": "ignored", "reason": "unknown_installation"})

    tenant_id = tenant["tenant_id"]

    # 7. Check rate limit
    allowed, retry_after = _get_rate_limit().check_rate_limit(tenant_id)

    # 8. If rate-limited → return 429 with Retry-After
    if not allowed:
        _log_outcome(
            event_type=event_type,
            action=action,
            installation_id=installation_id,
            tenant_id=tenant_id,
            repo=repo,
            persona=None,
            outcome="rate_limited",
            start_time=start_time,
        )
        return _response(
            429, {"error": "Rate limited", "retry_after": retry_after}, retry_after=retry_after
        )

    # 9. Parse intent
    from intent_parser import extract_intent

    intent = extract_intent(event_type, payload)

    # 10. If no actionable intent → log + return 200 (no-op)
    if intent is None:
        _log_outcome(
            event_type=event_type,
            action=action,
            installation_id=installation_id,
            tenant_id=tenant_id,
            repo=repo,
            persona=None,
            outcome="no_op",
            start_time=start_time,
        )
        return _response(200, {"status": "no_op"})

    # 11. Build envelope + publish to SQS
    sender = payload.get("sender", {})
    envelope = {
        "version": "1.0",
        "channel": "github",
        "tenant_id": tenant_id,
        "persona": intent.persona,
        "actor": {
            "github_id": sender.get("id", 0),
            "github_login": sender.get("login", ""),
            "is_bot": sender.get("type") == "Bot",
        },
        "source_ref": {
            "installation_id": installation_id,
            "repo": repo,
            "issue": payload.get("issue", {}).get("number") if "issue" in payload else None,
            "pr": payload.get("pull_request", {}).get("number")
            if "pull_request" in payload
            else None,
            "sha": payload.get("pull_request", {}).get("head", {}).get("sha")
            if "pull_request" in payload
            else None,
        },
        "intent": {
            "trigger": intent.trigger,
            "label": intent.label,
            "persona": intent.persona,
        },
        "payload": payload,
        "arrived_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    message_id = _get_sqs_publisher().publish_envelope(envelope)
    if not message_id:
        _log_outcome(
            event_type=event_type,
            action=action,
            installation_id=installation_id,
            tenant_id=tenant_id,
            repo=repo,
            persona=intent.persona,
            outcome="error",
            start_time=start_time,
            error="SQS publish failed",
        )
        return _response(500, {"error": "Failed to enqueue"})

    # 12. Log event
    _log_outcome(
        event_type=event_type,
        action=action,
        installation_id=installation_id,
        tenant_id=tenant_id,
        repo=repo,
        persona=intent.persona,
        outcome="published",
        start_time=start_time,
    )

    # 13. Return 200
    return _response(200, {"status": "accepted", "message_id": message_id})


def _response(status_code: int, body: dict, *, retry_after: int = 0) -> dict:
    """Build API Gateway v2 response."""
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if retry_after:
        headers["Retry-After"] = str(retry_after)
    return {
        "statusCode": status_code,
        "body": json.dumps(body),
        "headers": headers,
    }


def _log_outcome(
    *,
    event_type: str,
    action: str,
    installation_id: int,
    tenant_id: str | None,
    repo: str,
    persona: str | None,
    outcome: str,
    start_time: float,
    error: str | None = None,
) -> None:
    """Log webhook processing outcome."""
    latency_ms = (time.time() - start_time) * 1000
    try:
        _get_events_log().log_event(
            channel="github",
            event_type=event_type,
            action=action,
            installation_id=installation_id,
            tenant_id=tenant_id,
            repo=repo,
            intent_persona=persona,
            outcome=outcome,
            latency_ms=latency_ms,
            error=error,
        )
    except Exception as e:
        logger.warning("Failed to log event: %s", e)
