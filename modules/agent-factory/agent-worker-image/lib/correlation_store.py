"""DynamoDB correlation pointer writer for worker pods.

Worker-only: writes pointers after successful outbound GitHub actions so that
the next inbound webhook on the same channel can look up the active correlation.

Phase 2-d of EPIC #779. Intentional duplication of ~30 lines from the Lambda's
correlation_store — worker runs on EKS (IRSA), not in the Lambda runtime.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import boto3

logger = logging.getLogger(__name__)

_table_name = os.environ.get("CORRELATION_POINTERS_TABLE", "")
_ddb: "boto3.client" | None = None


def _get_client():
    global _ddb
    if _ddb is None:
        _ddb = boto3.client("dynamodb", region_name=os.environ.get("AWS_REGION", "us-east-1"))
    return _ddb


def channel_key(provider: str, repo: str, kind: str, number: int) -> str:
    """Build canonical channel key.

    Format follows Phase 2-a spec: 'github:repo=aws-e/adp,issue=783'

    Must produce the EXACT same string as the webhook-ingress Lambda's
    correlation_store.channel_key() — these are separate deploy bundles with
    a pinned-string test on both sides ensuring parity (issue #1661).
    """
    return f"{provider}:repo={repo},{kind}={number}"


def write_pointer(
    channel_key: str,
    correlation_id: str,
    root_human_id: str,
    is_human_rooted: bool,
    ttl_days: int = 7,
    triggering_invocation_id: str | None = None,
    chain_depth: int | None = None,
    last_triggered_persona: str | None = None,
) -> None:
    """Write a correlation pointer to DynamoDB. Fail-soft: logs and returns on error.

    Args:
        channel_key: Channel identifier (e.g. "github:repo=aws-e/adp,issue=783").
        correlation_id: Active correlation ID for this channel.
        root_human_id: The originating human's user ID.
        is_human_rooted: Whether the chain traces back to a human action.
        ttl_days: TTL in days for the pointer record.
        triggering_invocation_id: The message_id/invocation_id of the producing
            run. Propagated to the next inbound event as parent_invocation_id.
        chain_depth: Current chain depth of the producing run (issue #1696).
            Used by the webhook to enforce the depth-only loop guard.
        last_triggered_persona: The persona being triggered on this channel
            (issue #2149). Pre-seeds the self-re-trigger guard so the webhook
            can block immediate re-dispatch of the same persona.
    """
    table = _table_name or os.environ.get("CORRELATION_POINTERS_TABLE", "")
    if not table:
        logger.debug("CORRELATION_POINTERS_TABLE not set; skipping pointer write")
        return

    try:
        now = int(time.time())
        # Use update_item (not put_item) so we only SET the attributes this
        # producing run owns — and never erase webhook-managed fields like
        # last_triggered_persona (issue #1716). A full put_item here would wipe
        # the self-re-trigger guard value the webhook wrote when it spawned this
        # run, reopening the self-loop the moment the agent posts its first
        # comment mid-run.
        set_parts = [
            "correlation_id = :cid",
            "root_human_id = :rh",
            "is_human_rooted = :hr",
            "updated_at = :ua",
            "expires_at = :ea",
        ]
        expr_vals: dict[str, Any] = {
            ":cid": {"S": correlation_id},
            ":rh": {"S": root_human_id},
            ":hr": {"BOOL": is_human_rooted},
            ":ua": {"N": str(now)},
            ":ea": {"N": str(now + ttl_days * 86400)},
        }
        if triggering_invocation_id:
            set_parts.append("triggering_invocation_id = :tii")
            expr_vals[":tii"] = {"S": triggering_invocation_id}
        if chain_depth is not None:
            set_parts.append("chain_depth = :cd")
            expr_vals[":cd"] = {"N": str(chain_depth)}
        # Issue #2149: pre-seed the self-re-trigger guard for cross-issue dispatch.
        if last_triggered_persona:
            set_parts.append("last_triggered_persona = :ltp")
            expr_vals[":ltp"] = {"S": last_triggered_persona}
        _get_client().update_item(
            TableName=table,
            Key={"channel_key": {"S": channel_key}},
            UpdateExpression="SET " + ", ".join(set_parts),
            ExpressionAttributeValues=expr_vals,
        )
        logger.info("Wrote correlation pointer: channel=%s corr=%s", channel_key, correlation_id)
    except Exception as exc:
        logger.warning("Failed to write correlation pointer (non-fatal): %s", exc)
