"""SQS publisher for normalized webhook envelopes.

Publishes to a FIFO queue with MessageGroupId scoped to a single run, not a
tenant. Agent runs are independent — there's no ordering requirement between
two runs in the same tenant, and using tenant_id as the group means a single
stuck message blocks the entire tenant (head-of-line blocking) until the
visibility timeout expires. Using a per-run group ID keeps dedup working
(via MessageDeduplicationId) without the blocking.
"""

import json
import logging
import os

import boto3

logger = logging.getLogger(__name__)

SUBMIT_QUEUE_URL = os.environ.get("SUBMIT_QUEUE_URL", "")
REGION = os.environ.get("AWS_REGION", "us-east-1")
# SQS message size limit
MAX_SQS_MESSAGE_BYTES = 256 * 1024
MODEL_POLICY_UNAVAILABLE_CHANNEL = "snapshot_unavailable_channel"

_sqs = None


def _get_sqs():
    global _sqs
    if _sqs is None:
        _sqs = boto3.client("sqs", region_name=REGION)
    return _sqs


def publish_envelope(envelope: dict) -> str | None:
    """Publish a normalized envelope to the agent submit queue.

    Args:
        envelope: The normalized webhook envelope dict.

    Returns:
        SQS MessageId on success, None on failure.
    """
    queue_url = SUBMIT_QUEUE_URL
    if not queue_url:
        logger.error("SUBMIT_QUEUE_URL not configured")
        return None

    tenant_id = envelope.get("tenant_id", "unknown")
    try:
        envelope = prepare_envelope(envelope)
    except ValueError:
        return None
    if os.environ.get("ADP_WORK_CLAIMS_ENABLED", "false").lower() == "true":
        if envelope.get("channel") == "gitlab":
            # GitLab does not yet have a trusted admission seam or canonical
            # tenant/root identity. In report-only it remains behaviour-neutral,
            # but the unavailable policy path is explicit evidence rather than a
            # silent default. PMM-09 must not enforce until PMM-07 routes this
            # channel through gateway admission.
            logger.warning(
                "Model-policy snapshot unavailable run_id=%s reason=%s "
                "posture=report_only",
                envelope.get("message_id"),
                MODEL_POLICY_UNAVAILABLE_CHANNEL,
            )
        else:
            from common.gateway_client import admit_issue_work

            if os.environ.get(
                "AGENT_AUTHORITY_ENABLED", "false"
            ).lower() != "true" or not admit_issue_work(envelope):
                logger.warning(
                    "Work ownership admission refused run_id=%s; nothing published",
                    envelope.get("message_id"),
                )
                return None
    message_body = json.dumps(envelope, default=str)

    send_kwargs = {
        "QueueUrl": queue_url,
        "MessageBody": message_body,
    }
    if queue_url.endswith(".fifo"):
        # MessageGroupId scoped per-run (tenant#repo#issue) — not per-tenant.
        # Agent runs are independent; FIFO ordering inside a tenant buys
        # nothing and costs us head-of-line blocking when a message gets
        # stuck. Scoping to (tenant, repo, issue) still lets two triggers
        # on the same issue serialize (usually what the user wants), while
        # letting unrelated issues progress in parallel.
        source_ref = envelope.get("source_ref", {})
        repo = source_ref.get("repo", "")
        issue = source_ref.get("issue", "")
        send_kwargs["MessageGroupId"] = f"{tenant_id}#{repo}#{issue}"[:128]

        # Dedup by arrived_at + source to prevent double-processing
        dedup_key = f"{envelope.get('arrived_at', '')}_{repo}_{issue}"
        send_kwargs["MessageDeduplicationId"] = (
            envelope["message_id"]
            if os.environ.get("AGENT_AUTHORITY_ENABLED", "false").lower() == "true"
            else dedup_key[:128]
        )

    try:
        sqs = _get_sqs()
        resp = sqs.send_message(**send_kwargs)
        msg_id = resp.get("MessageId", "")
        logger.info(
            "Published envelope tenant=%s sqs_message_id=%s run_id=%s",
            tenant_id,
            msg_id,
            envelope.get("message_id", ""),
        )
        return msg_id
    except Exception as e:
        logger.error("Failed to publish envelope for tenant=%s: %s", tenant_id, e)
        return None


def prepare_envelope(envelope: dict) -> dict:
    """Finalize the exact queue body before its protected digest is written."""
    if len(json.dumps(envelope, default=str).encode("utf-8")) > MAX_SQS_MESSAGE_BYTES:
        envelope = _truncate_payload(envelope)
    if len(json.dumps(envelope, default=str).encode("utf-8")) > MAX_SQS_MESSAGE_BYTES:
        raise ValueError("envelope exceeds the queue size limit")
    return envelope


def _truncate_payload(envelope: dict) -> dict:
    """Truncate the raw payload field to fit within SQS size limit."""
    truncated = envelope.copy()
    truncated["payload"] = {"_truncated": True, "reason": "exceeded 256KB SQS limit"}
    return truncated
