"""Invocation logger for non-GitHub channels (Slack + WebChat).

Best-effort DDB write mirroring Phase 1's key contract:
  PK: event_id  = message.message_id (UUID)
  SK: arrived_at = ISO 8601 timestamp

The worker uses the same (event_id, arrived_at) pair from the SQS envelope
to advance status via UpdateItem. This module is an intentional duplication
of the webhook-ingress logger (~50 lines) — the two Lambdas are separate
deploy packages and a shared pip dependency would be over-engineering.

Table: adp-<env>-webhook-events (same as Phase 1).
"""

from __future__ import annotations

import logging
import time
from typing import Any

import boto3

logger = logging.getLogger(__name__)

# TTL: 30 days in seconds (matches Phase 1)
EVENT_TTL_SECONDS = 30 * 24 * 60 * 60

_dynamodb_resource = None
_table = None


def _get_table(table_name: str, region: str = "us-east-1"):
    """Lazy-init the DynamoDB Table resource."""
    global _dynamodb_resource, _table
    if _table is None or _table.table_name != table_name:
        _dynamodb_resource = boto3.resource("dynamodb", region_name=region)
        _table = _dynamodb_resource.Table(table_name)
    return _table


def log_invocation(
    table_name: str,
    *,
    event_id: str,
    arrived_at: str,
    user_id: str,
    channel: str,
    topic: str | None = None,
    persona: str | None = None,
    status: str = "webhook_received",
    tenant_id: str = "",
    region: str = "us-east-1",
) -> dict[str, Any] | None:
    """Write an invocation row to the webhook-events table.

    Args:
        table_name: DynamoDB table name.
        event_id: Stable key shared with worker (message.message_id).
        arrived_at: ISO timestamp shared with worker.
        user_id: Resolved user ID or "unattributed".
        channel: "slack" or "webchat".
        topic: Conversation subject (truncated to 120 chars).
        persona: Agent persona from classifier.
        status: Initial status (always "webhook_received" at capture).
        tenant_id: Tenant identifier (from identity claims).
        region: AWS region.

    Returns:
        The written item dict, or None on failure.
    """
    if not table_name:
        return None

    expires_at = int(time.time()) + EVENT_TTL_SECONDS
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    item: dict[str, Any] = {
        "event_id": event_id,
        "arrived_at": arrived_at,
        "tenant_id": tenant_id,
        "GSI1PK": tenant_id,
        "GSI1SK": arrived_at,
        "user_id": user_id or "unattributed",
        "channel": channel,
        "event_type": "chat_message",
        "action": "invoke",
        "status": status,
        "status_updated_at": now_iso,
        "expires_at": expires_at,
    }

    if persona:
        item["persona"] = persona
    if topic:
        item["topic"] = topic[:120]

    try:
        table = _get_table(table_name, region)
        table.put_item(Item=item)
        logger.info(
            "Logged invocation: event_id=%s channel=%s user=%s",
            event_id,
            channel,
            user_id,
        )
        return item
    except Exception as e:
        logger.warning("Failed to log invocation event_id=%s: %s", event_id, e)
        return None
