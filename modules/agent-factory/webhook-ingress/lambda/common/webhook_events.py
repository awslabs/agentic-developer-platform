"""Webhook event logging to DynamoDB.

Every incoming webhook (regardless of outcome) is recorded in the
`adp-<env>-webhook-events` table for audit and observability.

Table schema:
  PK: event_id (envelope message_id — stable across Lambda + worker)
  SK: arrived_at (ISO timestamp from envelope)
  GSI1PK: tenant_id
  GSI1SK: arrived_at
  GSI2PK: user_id
  GSI2SK: arrived_at
  TTL: expires_at (arrived_at + 30 days)

Query patterns:
  - All events for a tenant in the last 24h via tenant-index
  - All events for a user via user-index
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
            arrived_at="2026-06-13T22:00:00Z",
            tenant_id="acme-corp",
            channel="github",
            event_type="issues",
            action="labeled",
            installation_id="99887766",
            repo="acme-corp/flagship-app",
            status="webhook_received",
            user_id="usr-42",
            persona="developer",
            topic="Fix login bug",
            source_url="https://github.com/acme-corp/app/issues/99",
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
        arrived_at: str | None = None,
        tenant_id: str,
        channel: str,
        event_type: str,
        action: str,
        installation_id: str = "",
        repo: str = "",
        status: str = "webhook_received",
        processing_time_ms: int | None = None,
        error_message: str | None = None,
        user_id: str = "unattributed",
        github_login: str | None = None,
        persona: str | None = None,
        topic: str | None = None,
        summary: str | None = None,
        source_url: str | None = None,
        issue_number: int | None = None,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        """Record a webhook event in DynamoDB.

        Args:
            event_id: Envelope message_id (stable key shared with worker).
                Falls back to auto-generated UUID if not provided.
            arrived_at: ISO timestamp from the envelope. Falls back to
                current time if not provided. MUST match what the worker
                will use for UpdateItem.
            tenant_id: Resolved tenant identifier.
            channel: Webhook channel (github, slack, whatsapp, etc.).
            event_type: GitHub event type (issues, pull_request, etc.).
            action: Event action (labeled, opened, etc.).
            installation_id: GitHub App installation ID.
            repo: Full repo name (owner/name).
            status: Processing status lifecycle value.
            processing_time_ms: Lambda processing time in milliseconds.
            error_message: Error details if status is 'error'.
            user_id: Platform user ID from identity resolver.
                "unattributed" if resolution failed (never dropped).
            github_login: GitHub sender login (display only).
            persona: Agent persona (e.g. developer, architect).
            topic: Issue/PR title (truncated to 120 chars).
            summary: Run outcome summary (set by worker at terminal).
            source_url: Link to the triggering issue/PR.
            issue_number: Issue or PR number.
            correlation_id: Correlation chain ID.

        Returns:
            The DDB item that was written.
        """
        if not event_id:
            event_id = str(uuid.uuid4())

        if not arrived_at:
            arrived_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        expires_at = int(time.time()) + EVENT_TTL_SECONDS
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        item: dict[str, Any] = {
            "event_id": event_id,
            "arrived_at": arrived_at,
            "GSI1PK": tenant_id,
            "GSI1SK": arrived_at,
            "tenant_id": tenant_id,
            "user_id": user_id,
            "channel": channel,
            "event_type": event_type,
            "action": action,
            "status": status,
            "status_updated_at": now_iso,
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
        if github_login:
            item["github_login"] = github_login
        if persona:
            item["persona"] = persona
        if topic:
            item["topic"] = topic[:120]
        if summary:
            item["summary"] = summary
        if source_url:
            item["source_url"] = source_url
        if issue_number is not None:
            item["issue_number"] = issue_number
        if correlation_id:
            item["correlation_id"] = correlation_id

        try:
            self._table.put_item(Item=item)
            logger.info(
                "Logged webhook event: event_id=%s tenant=%s user=%s status=%s",
                event_id,
                tenant_id,
                user_id,
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
