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
    ttl_days: int = 7,
    triggering_invocation_id: str | None = None,
    last_triggered_persona: str | None = None,
) -> None:
    """Write a correlation pointer to DynamoDB. Fail-soft: logs and returns on error.

    Issue #4129: this no longer writes ``root_human_id``, ``is_human_rooted`` or
    ``chain_depth``, and does not accept them. Those three fields decide whose
    authority a run holds and how deep the chain has gone, and this code runs in
    the agent's own sandboxed pod — so a compromised run could set
    ``root_human_id=<victim> is_human_rooted=true chain_depth=0``, trigger the
    channel, and have the webhook persist the victim's id as a legitimate
    server-side ``authorized_user_id`` with the depth counter reset. The webhook
    now derives all three from the ``correlation-index`` GSI on ``webhook-events``
    (server-written), so nothing reads them from here.

    They are REMOVED rather than accepted-and-ignored on purpose: the pod must be
    structurally unable to send them, so a future caller cannot quietly
    reintroduce the write. The IAM ``dynamodb:Attributes`` Condition that makes
    this enforceable rather than merely conventional lands in a follow-up, after
    this image is confirmed live.

    What the pod legitimately owns is unchanged: the chain id, the parent edge,
    the TTL, and the self-re-trigger guard's persona.

    Args:
        channel_key: Channel identifier (e.g. "github:repo=aws-e/adp,issue=783").
        correlation_id: Active correlation ID for this channel.
        ttl_days: TTL in days for the pointer record.
        triggering_invocation_id: The message_id/invocation_id of the producing
            run. Propagated to the next inbound event as parent_invocation_id.
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
        # Issue #4129: root_human_id / is_human_rooted / chain_depth are
        # deliberately absent — see the docstring. Every attribute below is one
        # the pod legitimately owns.
        set_parts = [
            "correlation_id = :cid",
            "updated_at = :ua",
            "expires_at = :ea",
        ]
        expr_vals: dict[str, Any] = {
            ":cid": {"S": correlation_id},
            ":ua": {"N": str(now)},
            ":ea": {"N": str(now + ttl_days * 86400)},
        }
        if triggering_invocation_id:
            set_parts.append("triggering_invocation_id = :tii")
            expr_vals[":tii"] = {"S": triggering_invocation_id}
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
