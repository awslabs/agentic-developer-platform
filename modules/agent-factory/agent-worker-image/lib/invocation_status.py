"""DynamoDB invocation status updater for worker pods.

Worker-only: updates the webhook-events row (written by the Lambda at trigger
time) with status transitions as the agent run progresses.

Phase 1 of Agent Activity (issue #1455). Mirrors the fail-soft pattern from
correlation_store.py — never blocks or fails the run.
"""

from __future__ import annotations

import logging
import os
import time

import boto3

logger = logging.getLogger(__name__)

_table_name = os.environ.get("WEBHOOK_EVENTS_TABLE", "")
_ddb: "boto3.client" | None = None


def _get_client():
    global _ddb
    if _ddb is None:
        _ddb = boto3.client("dynamodb", region_name=os.environ.get("AWS_REGION", "us-east-1"))
    return _ddb


def update_status(
    event_id: str,
    arrived_at: str,
    status: str,
    *,
    run_id: str | None = None,
    summary: str | None = None,
    transcript_key: str | None = None,
) -> None:
    """Update the invocation row's status. Fail-soft: logs and returns on error.

    Args:
        event_id: The envelope message_id (PK of the webhook-events row).
        arrived_at: The envelope arrived_at timestamp (SK of the row).
        status: New status value (in_progress, complete, failed).
        run_id: KEDA job/pod name (set at in_progress).
        summary: Outcome summary (set at terminal status).
        transcript_key: S3 object key for the full run transcript (set at terminal status).
    """
    table = _table_name or os.environ.get("WEBHOOK_EVENTS_TABLE", "")
    if not table:
        logger.debug("WEBHOOK_EVENTS_TABLE not set; skipping status update")
        return

    if not event_id or not arrived_at:
        logger.debug("Missing event_id or arrived_at; skipping status update")
        return

    try:
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        # Build update expression
        expr_parts = ["#st = :status", "#su = :status_updated_at"]
        expr_names = {"#st": "status", "#su": "status_updated_at"}
        expr_values = {
            ":status": {"S": status},
            ":status_updated_at": {"S": now_iso},
        }

        if run_id:
            expr_parts.append("#rid = :run_id")
            expr_names["#rid"] = "run_id"
            expr_values[":run_id"] = {"S": run_id}

        if summary:
            expr_parts.append("#sm = :summary")
            expr_names["#sm"] = "summary"
            expr_values[":summary"] = {"S": summary}

        if transcript_key:
            expr_parts.append("#tk = :transcript_key")
            expr_names["#tk"] = "transcript_key"
            expr_values[":transcript_key"] = {"S": transcript_key}

        update_expr = "SET " + ", ".join(expr_parts)

        # Retry once on ConditionalCheckFailedException for the "in_progress"
        # transition. The Lambda now writes DDB before SQS publish (handler.py
        # reorder, #1463), so this should rarely fire. The retry is defense-in-
        # depth for transient DDB eventual-consistency windows.
        max_attempts = 2 if status == "in_progress" else 1
        for attempt in range(max_attempts):
            try:
                _get_client().update_item(
                    TableName=table,
                    Key={
                        "event_id": {"S": event_id},
                        "arrived_at": {"S": arrived_at},
                    },
                    UpdateExpression=update_expr,
                    ExpressionAttributeNames=expr_names,
                    ExpressionAttributeValues=expr_values,
                    # Only update if row exists — prevents orphan creates
                    ConditionExpression="attribute_exists(event_id)",
                )
                logger.info("Updated invocation status: event_id=%s status=%s", event_id, status)
                return
            except _get_client().exceptions.ConditionalCheckFailedException:
                if attempt < max_attempts - 1:
                    logger.info(
                        "Row not yet visible for event_id=%s, retrying in 2s (attempt %d/%d)",
                        event_id,
                        attempt + 1,
                        max_attempts,
                    )
                    time.sleep(2)  # nosemgrep: arbitrary-sleep
                else:
                    # Row doesn't exist after retries — capture write failed or was skipped
                    logger.warning(
                        "Invocation row not found for status update (event_id=%s) after %d attempts — skipping",
                        event_id,
                        max_attempts,
                    )
    except Exception as exc:
        logger.warning("Failed to update invocation status (non-fatal): %s", exc)
