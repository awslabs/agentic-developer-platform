"""Structured event logging for webhook processing.

Emits structured JSON logs for observability (CloudWatch Logs Insights queries).
"""

import json
import logging
import time

logger = logging.getLogger(__name__)


def log_event(
    *,
    channel: str,
    event_type: str,
    action: str,
    installation_id: int,
    tenant_id: str | None,
    repo: str,
    intent_persona: str | None,
    outcome: str,
    latency_ms: float = 0,
    error: str | None = None,
) -> None:
    """Log a structured webhook processing event.

    Args:
        channel: Source channel (github, slack, etc.)
        event_type: Raw event type header value
        action: Parsed action from payload
        installation_id: Channel-specific installation ID
        tenant_id: Resolved tenant or None
        repo: Repository full name
        intent_persona: Resolved persona or None
        outcome: One of: published, no_op, rate_limited, unknown_tenant, invalid_signature, error
        latency_ms: Processing time in milliseconds
        error: Error message if outcome is error
    """
    record = {
        "ts": time.time(),
        "channel": channel,
        "event_type": event_type,
        "action": action,
        "installation_id": installation_id,
        "tenant_id": tenant_id,
        "repo": repo,
        "intent_persona": intent_persona,
        "outcome": outcome,
        "latency_ms": round(latency_ms, 1),
    }
    if error:
        record["error"] = error

    logger.info(json.dumps(record))
