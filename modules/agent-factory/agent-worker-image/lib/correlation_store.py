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

import boto3

logger = logging.getLogger(__name__)

_table_name = os.environ.get("CORRELATION_POINTERS_TABLE", "")
_ddb: "boto3.client" | None = None


def _get_client():
    global _ddb
    if _ddb is None:
        _ddb = boto3.client("dynamodb", region_name=os.environ.get("AWS_REGION", "us-east-1"))
    return _ddb


def write_pointer(
    channel_key: str,
    correlation_id: str,
    root_human_id: str,
    is_human_rooted: bool,
    ttl_days: int = 7,
    triggering_invocation_id: str | None = None,
) -> None:
    """Write a correlation pointer to DynamoDB. Fail-soft: logs and returns on error.

    Args:
        channel_key: Channel identifier (e.g. "github:org/repo:issue:123").
        correlation_id: Active correlation ID for this channel.
        root_human_id: The originating human's user ID.
        is_human_rooted: Whether the chain traces back to a human action.
        ttl_days: TTL in days for the pointer record.
        triggering_invocation_id: The message_id/invocation_id of the producing
            run. Propagated to the next inbound event as parent_invocation_id.
    """
    table = _table_name or os.environ.get("CORRELATION_POINTERS_TABLE", "")
    if not table:
        logger.debug("CORRELATION_POINTERS_TABLE not set; skipping pointer write")
        return

    try:
        now = int(time.time())
        item = {
            "channel_key": {"S": channel_key},
            "latest_correlation_id": {"S": correlation_id},
            "latest_root_human_id": {"S": root_human_id},
            "latest_is_human_rooted": {"BOOL": is_human_rooted},
            "updated_at": {"N": str(now)},
            "expires_at": {"N": str(now + ttl_days * 86400)},
        }
        if triggering_invocation_id:
            item["triggering_invocation_id"] = {"S": triggering_invocation_id}
        _get_client().put_item(
            TableName=table,
            Item=item,
        )
        logger.info("Wrote correlation pointer: channel=%s corr=%s", channel_key, correlation_id)
    except Exception as exc:
        logger.warning("Failed to write correlation pointer (non-fatal): %s", exc)
