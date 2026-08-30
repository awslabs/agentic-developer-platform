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

  engine-command-index (sparse, #4527):
    PK: engine_command_status  SK: arrived_at

Query patterns:
  - All events for a tenant in the last 24h via tenant-index
  - All events for a user via user-index
  - Single event lookup by event_id + arrived_at
  - Outstanding `@agent-engine` commands, oldest first, via engine-command-index
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key

logger = logging.getLogger(__name__)

# TTL: 30 days in seconds
EVENT_TTL_SECONDS = 30 * 24 * 60 * 60

# --- Engine-command bridge (issue #4527) -------------------------------------
# An `@agent-engine` comment produces no agent pod and no SQS message. The ROW is
# the delivery mechanism: this Lambda marks it pending, and the gateway-side
# orchestration tick queries the sparse `engine-command-index` on its next wake,
# applies the command and flips the marker to consumed.
#
# `engine_command_status` is the index's hash key, which makes the index sparse:
# only rows carrying this attribute are projected, so the tick's Query scans the
# handful of outstanding commands rather than every webhook of the last 30 days.
#
# The tick lives in the gateway container and CANNOT import this module (separate
# deploy units — same constraint that forces `_emit_row_write_dropped` to
# re-implement the gateway's metric helper). It re-declares these two values; the
# pair is asserted equal by a test on each side.
ENGINE_COMMAND_STATUS_PENDING = "pending"
ENGINE_COMMAND_STATUS_CONSUMED = "consumed"

#: Cap on the stored comment body. Commands are one line; a `replan:` directive
#: may be a paragraph. GitHub allows 65536-char comments, and this row is written
#: from unauthenticated-until-verified webhook input, so it is bounded here rather
#: than trusted to be small. Generous enough that no realistic command is cut, far
#: enough under DynamoDB's 400 KB item limit that a body can never be what makes a
#: write fail.
ENGINE_COMMAND_BODY_MAX_CHARS = 4000

# Issue #4347: namespace/metric for a dropped row write. Namespace matches the
# existing WebhookIngress metrics (metrics.py, correlation_store.py) so the
# drop lands on the same dashboard as the rest of ingress observability.
METRICS_NAMESPACE = "WebhookIngress"
ROW_WRITE_DROPPED_METRIC = "WebhookEventRowWriteDropped"

_cloudwatch = None


def _get_cloudwatch():
    """Return a lazily-created, module-cached CloudWatch client."""
    global _cloudwatch
    if _cloudwatch is None:
        region = (
            os.environ.get("AWS_REGION")
            or os.environ.get("AWS_DEFAULT_REGION")
            or "us-east-1"
        )
        _cloudwatch = boto3.client("cloudwatch", region_name=region)
    return _cloudwatch


def _emit_row_write_dropped(status: str, error_kind: str) -> None:
    """Emit ``WebhookEventRowWriteDropped`` when a row write is swallowed (#4347).

    The row write below is best-effort by design so audit logging never blocks a
    webhook response. That is correct while the row is only an audit record — but
    under #4187 enforce the same row becomes the run's AUTHORIZATION record
    (``run_binding.verify_row_matches_caller`` reads it on the model-call path).
    A silently dropped write then means the run dispatches fine and is denied on
    every model call for its whole lifetime, with no retry path: ``unknown_run``
    is deliberately not negative-cached, but the row never appears either, so
    re-lookup never succeeds.

    This metric removes the silence. It does NOT change the best-effort
    semantics — making the write authoritative (fail the spawn) is the separate,
    deliberately deferred option 1.

    Modelled on the gateway's ``emit_run_binding_drift`` (dimension per cause, so
    the drop is alertable) but implemented with the in-module
    ``put_metric_data`` pattern from ``correlation_store``: webhook-ingress is a
    Lambda deploy unit and cannot import gateway container code.

    Emitted ONLY on a drop — the happy path emits nothing, so there are no false
    positives and no per-webhook metric cost.

    Args:
        status: The row's lifecycle status, so an operator can tell an
            authorization-bearing ``webhook_received`` drop (the #4187 hazard)
            from a terminal-at-ingress ``blocked``/``no_op`` drop (audit only).
        error_kind: Exception class name of the underlying failure.
    """
    try:
        _get_cloudwatch().put_metric_data(
            Namespace=METRICS_NAMESPACE,
            MetricData=[
                {
                    "MetricName": ROW_WRITE_DROPPED_METRIC,
                    "Dimensions": [
                        {"Name": "Status", "Value": status or "unknown"},
                        {"Name": "ErrorKind", "Value": error_kind},
                    ],
                    "Value": 1,
                    "Unit": "Count",
                }
            ],
        )
    except Exception as e:
        # Best-effort — a metric failure must never crash the caller, or the
        # observability fix would become a worse outage than the silence it fixes.
        logger.debug("Failed to emit %s metric: %s", ROW_WRITE_DROPPED_METRIC, e)


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
        skip_reason: str | None = None,
        user_id: str = "unattributed",
        github_login: str | None = None,
        persona: str | None = None,
        topic: str | None = None,
        summary: str | None = None,
        source_url: str | None = None,
        issue_number: int | None = None,
        correlation_id: str | None = None,
        parent_invocation_id: str | None = None,
        chain_depth: int | None = None,
        root_human_id: str | None = None,
        is_human_rooted: bool | None = None,
        authorized_user_id: str = "",
        engine_command: bool = False,
        comment_body: str | None = None,
        sender_github_id: str | None = None,
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
            skip_reason: Issue #4020 — why this delivery produced no agent run.
                A static enum from common/skip_reasons.py, or a SpawnResult
                block_reason. Read by the Activity UI to explain a no_op /
                blocked / skipped row instead of showing a bare "✗ No-op" badge.
                MUST NOT contain webhook payload content (see skip_reasons.py).
            user_id: Platform user ID from identity resolver.
                "unattributed" if resolution failed (never dropped).
            github_login: GitHub sender login (display only).
            persona: Agent persona (e.g. developer, architect).
            topic: Issue/PR title (truncated to 120 chars).
            summary: Run outcome summary (set by worker at terminal).
            source_url: Link to the triggering issue/PR.
            issue_number: Issue or PR number.
            correlation_id: Correlation chain ID.
            parent_invocation_id: The invocation_id (event_id) of the run that
                spawned this one — the cross-agent lineage edge (#1696). Read by
                the Activity chain view. Computed by determine_correlation but
                previously never persisted to the row (issue #1750).
            chain_depth: This run's depth in the chain (#1696).
            authorized_user_id: Canonical user whose credentials this run
                may access (#3174). Set at spawn from chain policy; empty
                string means no vault access. Written but unread until S2.
            engine_command: Issue #4527 — this delivery is an ``@agent-engine``
                comment. Marks the row ``engine_command_status=pending`` so the
                orchestration tick picks it up. Nothing else about the row
                changes: no queue message, no gateway call.
            comment_body: The raw comment text, stored ONLY when
                ``engine_command`` is true and truncated to
                ``ENGINE_COMMAND_BODY_MAX_CHARS``. The tick parses the command
                from it — this Lambda deliberately does not, because parsing needs
                the graph and the tenant, which it cannot see (#4303).
            sender_github_id: The commenter's NUMERIC GitHub id as a string, again
                only on the engine path. Numeric rather than the login because
                logins are renameable, so a login would let a renamed account
                inherit another user's approvals. The tick resolves it to a
                platform identity server-side and never trusts it as authority.

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
        # Issue #4020: the reason a delivery produced no run. Optional — rows
        # written before this change simply lack the attribute, and the API
        # serializes the absence as null.
        if skip_reason:
            item["skip_reason"] = skip_reason
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
        if parent_invocation_id:
            item["parent_invocation_id"] = parent_invocation_id
        if chain_depth is not None:
            item["chain_depth"] = chain_depth
        # Issue #2042: persist the chain's human root so the Activity layer can
        # attribute agent-spawned runs to the originating human (not the bot
        # sender) — otherwise cross-issue/agent-triggered runs never appear under
        # the human's /me view.
        if root_human_id:
            item["root_human_id"] = root_human_id
        if is_human_rooted is not None:
            item["is_human_rooted"] = is_human_rooted
        # Issue #3174: credential-authorization binding — write the canonical
        # user whose credentials this run may access (ships dark until S2).
        if authorized_user_id:
            item["authorized_user_id"] = authorized_user_id
        # Issue #4527: mark the row for the orchestration tick. The three
        # attributes are written together or not at all — a pending marker with no
        # body would make the tick wake up to a command it cannot parse, and a body
        # with no marker would never be found (the index is sparse on the marker).
        if engine_command:
            item["engine_command_status"] = ENGINE_COMMAND_STATUS_PENDING
            item["engine_command_body"] = (comment_body or "")[
                :ENGINE_COMMAND_BODY_MAX_CHARS
            ]
            item["engine_command_sender_github_id"] = sender_github_id or ""

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
            # Best-effort logging — never block the webhook response.
            # Issue #4347: but no longer SILENTLY. Under #4187 enforce this row is
            # the run's authorization record, so a dropped write means the run
            # dispatches and is then denied on every model call for its whole
            # life, with no retry. Emit an alertable metric so the drop is caught
            # before it becomes a wave of 402s.
            logger.error(
                "Failed to log webhook event %s (status=%s): %s — row dropped; "
                "under #4187 enforce this row is the run's authorization record, "
                "so this run may be denied on every model call",
                event_id,
                status,
                e,
            )
            _emit_row_write_dropped(status=status, error_kind=type(e).__name__)

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
