"""DynamoDB correlation-pointer client for provenance chain tracking.

Issue #785: Phase 2-b — read/write helpers for the correlation-pointers
DDB table created in Phase 2-a (#784).

The correlation pointer maps a channel (e.g. a GitHub issue) to its active
correlation_id + root_human_id. This allows the webhook handler (Phase 2-c)
to propagate provenance through multi-event chains without parsing HTML
markers as the primary path.

Fail-soft semantics: on DDB errors, read returns None (caller falls back
to HTML marker), write logs a warning + emits a metric but does not raise.
This ensures the webhook handler never crashes due to provenance plumbing.

# TODO: Once Phase 2-d ships and consumers rely on provenance rows,
# evaluate fail-hard or circuit-breaker for write failures. Currently
# silent metric emission is acceptable because nothing reads provenance yet.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

_TABLE_NAME = os.environ.get("CORRELATION_POINTERS_TABLE", "")
_REGION = os.environ.get("AWS_REGION", "us-east-1")

# Lazy-init DDB resource
_ddb_table: Any = None


def _get_table():
    """Lazily initialize the DynamoDB Table resource."""
    global _ddb_table
    if _ddb_table is None:
        table_name = os.environ.get("CORRELATION_POINTERS_TABLE", _TABLE_NAME)
        if not table_name:
            return None
        region = os.environ.get("AWS_REGION", _REGION)
        dynamodb = boto3.resource("dynamodb", region_name=region)
        _ddb_table = dynamodb.Table(table_name)
    return _ddb_table


def channel_key(provider: str, repo: str, kind: str, number: int) -> str:
    """Build canonical channel key.

    Format follows Phase 2-a spec: 'github:repo=aws-e/adp,issue=783'
    """
    return f"{provider}:repo={repo},{kind}={number}"


def read_pointer(key: str) -> dict | None:
    """Read the correlation pointer for a channel.

    Returns {correlation_id, root_human_id, is_human_rooted,
    triggering_invocation_id, chain_depth, last_triggered_persona} or None
    if no pointer exists or on DDB error.

    Uses ConsistentRead=True to close the bot-race window where two
    near-simultaneous bot events could both miss the pointer.
    """
    table = _get_table()
    if table is None:
        logger.warning("CORRELATION_POINTERS_TABLE not configured — returning None")
        return None

    try:
        resp = table.get_item(
            Key={"channel_key": key},
            ConsistentRead=True,
        )
        item = resp.get("Item")
        if item is None:
            return None
        # chain_depth may be absent on old pointers — treat as None (unknown)
        chain_depth = item.get("chain_depth")
        if chain_depth is not None:
            try:
                chain_depth = int(chain_depth)
            except (ValueError, TypeError):
                chain_depth = None
        # Issue #2149: recent_trigger_count for cross-persona loop detection
        recent_trigger_count = item.get("recent_trigger_count")
        if recent_trigger_count is not None:
            try:
                recent_trigger_count = int(recent_trigger_count)
            except (ValueError, TypeError):
                recent_trigger_count = 0
        else:
            recent_trigger_count = 0
        return {
            "correlation_id": item["correlation_id"],
            "root_human_id": item["root_human_id"],
            "is_human_rooted": item.get("is_human_rooted", True),
            "triggering_invocation_id": item.get("triggering_invocation_id"),
            "chain_depth": chain_depth,
            # Persona most recently triggered in this chain on this channel.
            # Used by the immediate-self-re-trigger guard (issue #1716): a bot
            # mention is blocked when the mentioned persona == this value, which
            # stops an agent's own status comments (e.g. "**Agent**: @agent-X")
            # from re-spawning the same persona, WITHOUT blocking legitimate
            # cross-persona cycles like reviewer→developer→reviewer.
            "last_triggered_persona": item.get("last_triggered_persona"),
            # Issue #2149: Set of all personas triggered in this chain on this
            # channel + total count. Used by the cross-persona loop guard to
            # catch A→B→A→B alternation that the scalar last_triggered_persona
            # guard cannot detect.
            "recent_triggered_personas": set(
                item.get("recent_triggered_personas", []) or []
            ),
            "recent_trigger_count": recent_trigger_count,
        }
    except ClientError as e:
        logger.warning(
            "read_pointer DDB error for key=%s: %s",
            key,
            e.response["Error"]["Message"],
        )
        return None
    except Exception as e:
        logger.warning("read_pointer unexpected error for key=%s: %s", key, e)
        return None


def write_pointer(
    key: str,
    correlation_id: str,
    root_human_id: str,
    is_human_rooted: bool,
    ttl_days: int = 7,
    triggering_invocation_id: str | None = None,
    last_triggered_persona: str | None = None,
    recent_triggered_personas: set | None = None,
    recent_trigger_count: int | None = None,
) -> None:
    """Idempotently upsert a correlation pointer with TTL.

    Refreshes TTL on every call so active chains don't expire mid-flight.
    Fail-soft: logs warning + emits metric on error, does not raise.

    Args:
        triggering_invocation_id: The message_id/invocation_id of the run
            that produced this outbound action. Used by the next inbound
            webhook to set parent_invocation_id on the child run's provenance.
        last_triggered_persona: The persona this webhook is about to spawn on
            this channel/chain. Recorded so the next bot mention can be blocked
            if it targets the SAME persona (immediate self-re-trigger guard,
            issue #1716). NOT preserved when None (so we never clobber a real
            value with a missing one on a continuation write).
        recent_triggered_personas: Issue #2149 — set of all personas triggered
            in this chain. Used by the cross-persona loop guard to detect
            A→B→A→B alternation. NOT preserved when None.
        recent_trigger_count: Issue #2149 — total number of bot-triggered
            dispatches in this chain. NOT preserved when None.
    """
    table = _get_table()
    if table is None:
        logger.warning("CORRELATION_POINTERS_TABLE not configured — skipping write")
        return

    expires_at = int(time.time()) + (ttl_days * 86400)

    item: dict[str, Any] = {
        "channel_key": key,
        "correlation_id": correlation_id,
        "root_human_id": root_human_id,
        "is_human_rooted": is_human_rooted,
        "expires_at": expires_at,
    }
    if triggering_invocation_id:
        item["triggering_invocation_id"] = triggering_invocation_id
    if last_triggered_persona:
        item["last_triggered_persona"] = last_triggered_persona
    # Issue #2149: persist cross-persona loop tracking fields
    if recent_triggered_personas is not None:
        # DynamoDB StringSet — must be non-empty; omit if empty set
        if recent_triggered_personas:
            item["recent_triggered_personas"] = recent_triggered_personas
    if recent_trigger_count is not None:
        item["recent_trigger_count"] = recent_trigger_count

    try:
        table.put_item(Item=item)
    except ClientError as e:
        logger.warning(
            "write_pointer DDB error for key=%s: %s",
            key,
            e.response["Error"]["Message"],
        )
        _emit_write_failed_metric()
    except Exception as e:
        logger.warning("write_pointer unexpected error for key=%s: %s", key, e)
        _emit_write_failed_metric()


def _emit_write_failed_metric() -> None:
    """Emit CorrelationPointerWriteFailed metric to CloudWatch.

    Uses the ADP/WebhookIngress namespace (aligned with existing WebhookMetrics).
    """
    try:
        region = os.environ.get("AWS_REGION", "us-east-1")
        cw = boto3.client("cloudwatch", region_name=region)
        cw.put_metric_data(
            Namespace="WebhookIngress",
            MetricData=[
                {
                    "MetricName": "CorrelationPointerWriteFailed",
                    "Value": 1,
                    "Unit": "Count",
                }
            ],
        )
    except Exception as e:
        # Best-effort — don't let metric emission crash anything
        logger.debug("Failed to emit CorrelationPointerWriteFailed metric: %s", e)
