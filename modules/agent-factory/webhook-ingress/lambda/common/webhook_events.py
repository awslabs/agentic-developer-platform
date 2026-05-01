"""Webhook event logging to DynamoDB.

Every incoming webhook (regardless of outcome) is recorded in the
`adp-<env>-webhook-events` table for audit and observability.

Table schema:
  PK: event_id (X-GitHub-Delivery header or generated UUID)
  SK: arrived_at (ISO timestamp)
  GSI1PK: tenant_id
  GSI1SK: arrived_at
  TTL: expires_at (arrived_at + 30 days)

Query patterns:
  - All events for a tenant in the last 24h via GSI1
  - Single event lookup by event_id + arrived_at
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key

logger = logging.getLogger(__name__)

# TTL: 30 days in seconds
EVENT_TTL_SECONDS = 30 * 24 * 60 * 60


class WebhookEventLogger:
    """Logs webhook events to DynamoDB for audit trail.

    Usage:
        logger = WebhookEventLogger(table_name="adp-dev-webhook-events")
        logger.log_event(
            event_id="abc-123",
            tenant_id="acme-corp",
            channel="github",
            event_type="issues",
            action="labeled",
            installation_id="99887766",
            repo="acme-corp/flagship-app",
            status="accepted",
        )
    """

    def __init__(self, table_name: str, region: str = "us-east-1"):
        self._table_name = table_name
        self._dynamodb = boto3.resource("dynamodb", region_name=region)
        self._table = self._dynamodb.Table(table_name)

    def log_event(
        self,
        *,
        event_id: str | None = None,
        tenant_id: str,
        channel: str,
        event_type: str,
        action: str,
        installation_id: str = "",
        repo: str = "",
        status: str = "accepted",
        processing_time_ms: int | None = None,
        error_message: str | None = None,
    ) -> dict[str, Any]:
        """Record a webhook event in DynamoDB.

        Args:
            event_id: X-GitHub-Delivery header value, or auto-generated UUID.
            tenant_id: Resolved tenant identifier.
            channel: Webhook channel (github, slack, whatsapp, etc.).
            event_type: GitHub event type (issues, pull_request, etc.).
            action: Event action (labeled, opened, etc.).
            installation_id: GitHub App installation ID.
            repo: Full repo name (owner/name).
            status: Processing outcome (accepted|rejected|rate_limited|no_op|error).
            processing_time_ms: Lambda processing time in milliseconds.
            error_message: Error details if status is 'error'.

        Returns:
            The DDB item that was written.
        """
        if not event_id:
            event_id = str(uuid.uuid4())

        arrived_at = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
        expires_at = int(time.time()) + EVENT_TTL_SECONDS

        item: dict[str, Any] = {
            "event_id": event_id,
            "arrived_at": arrived_at,
            "GSI1PK": tenant_id,
            "GSI1SK": arrived_at,
            "tenant_id": tenant_id,
            "channel": channel,
            "event_type": event_type,
            "action": action,
            "status": status,
            "expires_at": expires_at,
        }

        if installation_id:
            item["installation_id"] = installation_id
        if repo:
            item["repo"] = repo
        if processing_time_ms is not None:
            item["processing_time_ms"] = processing_time_ms
        if error_message:
            item["error_message"] = error_message

        try:
            self._table.put_item(Item=item)
            logger.info(
                "Logged webhook event: event_id=%s tenant=%s status=%s",
                event_id,
                tenant_id,
                status,
            )
        except Exception as e:
            # Best-effort logging — never block the webhook response
            logger.error("Failed to log webhook event %s: %s", event_id, e)

        return item

    def query_by_tenant(
        self,
        tenant_id: str,
        since: str,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Query webhook events for a tenant since a given timestamp.

        Args:
            tenant_id: The tenant to query.
            since: ISO timestamp lower bound (e.g. "2026-05-01T19:00:00Z").
            limit: Max items to return.

        Returns:
            List of event items, newest first.
        """
        try:
            response = self._table.query(
                IndexName="gsi1",
                KeyConditionExpression=(
                    Key("GSI1PK").eq(tenant_id) & Key("GSI1SK").gte(since)
                ),
                Limit=limit,
                ScanIndexForward=False,
            )
            return response.get("Items", [])
        except Exception as e:
            logger.error("Failed to query events for tenant %s: %s", tenant_id, e)
            return []
