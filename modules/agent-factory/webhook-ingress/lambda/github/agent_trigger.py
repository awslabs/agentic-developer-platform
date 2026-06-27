"""POST /agent/trigger handler — IAM-authenticated agent-to-agent spawn.

Issue #2152: Agents spawn other personas via authenticated HTTP call with
mandatory server-resolved lineage. The body provides correlation_id +
parent_invocation_id; the handler resolves the chain from the webhook-events
table's correlation-index GSI and cross-checks trust properties. An agent
CANNOT forge root_human_id or tenant — those come from the chain record.

Reject rules:
  - Missing correlation_id        -> 400 missing_lineage
  - Unknown/expired chain         -> 422 unknown_chain (NEVER mint new root)
  - Cross-tenant mismatch         -> 403 cross_tenant
  - Guard rejections              -> 422 guard_rejected
  - Missing required fields       -> 400 invalid_body
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

logger = logging.getLogger(__name__)

# GSI propagation retry: a single retry after 200ms handles the common case
# where the parent just wrote the event row and the GSI hasn't replicated yet.
_GSI_RETRY_DELAY_S = 0.2
_GSI_MAX_ATTEMPTS = 2

# Required body fields
_REQUIRED_FIELDS = ("correlation_id", "parent_invocation_id", "persona", "target")


def handle_agent_trigger(event: dict, context) -> dict:
    """Handle POST /agent/trigger — IAM-authenticated agent spawn.

    The caller is authenticated by API Gateway AWS_IAM authorization. The
    Lambda receives the caller's IAM identity in event.requestContext.identity.

    Body schema:
        {
            "correlation_id": "<chain id>",
            "parent_invocation_id": "<caller's invocation id>",
            "persona": "<target persona>",
            "target": {"repo": "org/repo", "issue": 123},
            "reason": "<optional spawn reason>"
        }

    Returns:
        API Gateway response dict (202 on success, 4xx on reject).
    """
    start_time = time.time()

    # Parse body
    raw_body = event.get("body", "")
    if event.get("isBase64Encoded", False):
        import base64

        raw_body = base64.b64decode(raw_body).decode("utf-8")

    try:
        body = json.loads(raw_body) if raw_body else {}
    except (json.JSONDecodeError, ValueError):
        return _response(400, {"error": "invalid_json"})

    # Validate required fields
    missing = [f for f in _REQUIRED_FIELDS if not body.get(f)]
    if missing:
        if "correlation_id" in missing:
            return _response(
                400, {"error": "missing_lineage", "detail": "correlation_id is required"}
            )
        return _response(400, {"error": "invalid_body", "detail": f"missing fields: {missing}"})

    correlation_id = body["correlation_id"]
    parent_invocation_id = body["parent_invocation_id"]
    persona = body["persona"]
    target = body["target"]
    reason = body.get("reason", "")

    # Validate target shape
    if not isinstance(target, dict) or not target.get("repo") or not target.get("issue"):
        return _response(
            400, {"error": "invalid_body", "detail": "target must have repo and issue"}
        )

    target_repo = target["repo"]
    target_issue = int(target["issue"])

    # Resolve chain from correlation-index GSI (mandatory retry for GSI propagation)
    chain_record = _resolve_chain(correlation_id)
    if chain_record is None:
        return _response(
            422, {"error": "unknown_chain", "detail": "correlation_id not found in chain index"}
        )

    # Extract trust properties from chain record (server-resolved, not from body)
    chain_tenant_id = chain_record.get("tenant_id")
    chain_root_human_id = chain_record.get("root_human_id")
    chain_is_human_rooted = chain_record.get("is_human_rooted", True)
    chain_depth = chain_record.get("chain_depth")
    if chain_depth is not None:
        try:
            chain_depth = int(chain_depth)
        except (ValueError, TypeError):
            chain_depth = 0
    else:
        chain_depth = 0

    # Cross-tenant check: if body provides tenant_id, it must match chain
    body_tenant = body.get("tenant_id")
    if body_tenant and body_tenant != chain_tenant_id:
        logger.warning(
            "agent_trigger: cross-tenant rejected body_tenant=%s chain_tenant=%s",
            body_tenant,
            chain_tenant_id,
        )
        return _response(
            403, {"error": "cross_tenant", "detail": "body tenant does not match chain"}
        )

    # Resolve caller identity from IAM context
    request_context = event.get("requestContext", {})
    caller_arn = request_context.get("identity", {}).get("userArn", "")

    # Build correlation context for spawn_persona
    correlation_ctx = {
        "correlation_id": correlation_id,
        "root_human_id": chain_root_human_id or "",
        "triggered_by": caller_arn,
        "is_human_rooted": chain_is_human_rooted,
        "is_new_chain": False,
        "parent_invocation_id": parent_invocation_id,
        "chain_depth": chain_depth + 1,
        # Carry forward loop-tracking from pointer if available
        "last_triggered_persona": chain_record.get("last_triggered_persona"),
        "recent_triggered_personas": set(chain_record.get("recent_triggered_personas") or []),
        "recent_trigger_count": _safe_int(chain_record.get("recent_trigger_count"), 0),
    }

    # Build channel key for pointer writes
    from common.correlation_store import channel_key

    channel_key_str = channel_key("github", target_repo, "issue", target_issue)

    # Build a synthetic resolved_identity for spawn_persona
    resolved_identity = _SyntheticIdentity(
        tenant_id=chain_tenant_id or "",
        org_id=chain_tenant_id or "",
        user_id=caller_arn,
        user_kind="bot",
        bot_kind=_extract_bot_kind_from_arn(caller_arn),
    )

    # Build minimal sender dict
    sender = {
        "login": f"iam:{caller_arn}",
        "id": 0,
        "type": "Bot",
    }

    # Build minimal payload for envelope (target context)
    payload = {
        "action": "agent_trigger",
        "issue": {
            "number": target_issue,
            "title": reason or "(agent-triggered)",
            "html_url": f"https://github.com/{target_repo}/issues/{target_issue}",
        },
        "repository": {"full_name": target_repo},
        "sender": sender,
        "installation": {"id": 0},
    }

    # Call spawn_persona (the single enforcement point)
    from common.spawn_persona import spawn_persona

    spawn_result = spawn_persona(
        persona=persona,
        correlation_ctx=correlation_ctx,
        channel_key=channel_key_str,
        resolved_identity=resolved_identity,
        tenant_id=chain_tenant_id or "",
        actor_user_id=caller_arn,
        actor_org_id=chain_tenant_id or "",
        sender=sender,
        event_type="agent_trigger",
        action="trigger",
        installation_id=0,
        repo=target_repo,
        payload=payload,
        intent_trigger="agent_trigger",
        intent_label=None,
    )

    if not spawn_result.success:
        block_reason = spawn_result.block_reason or "error"
        logger.info(
            "agent_trigger: spawn blocked reason=%s persona=%s correlation=%s",
            block_reason,
            persona,
            correlation_id,
        )
        if block_reason == "sqs_publish_failed":
            return _response(500, {"error": "enqueue_failed"})
        return _response(422, {"error": "guard_rejected", "detail": block_reason})

    latency_ms = (time.time() - start_time) * 1000
    logger.info(
        "agent_trigger: spawned persona=%s correlation=%s message_id=%s latency_ms=%.1f",
        persona,
        correlation_id,
        spawn_result.message_id,
        latency_ms,
    )
    return _response(
        202,
        {
            "status": "accepted",
            "message_id": spawn_result.message_id,
            "correlation_id": correlation_id,
        },
    )


def _resolve_chain(correlation_id: str) -> dict[str, Any] | None:
    """Query webhook-events correlation-index GSI to find the chain record.

    Uses mandatory single retry with 200ms backoff to handle GSI eventual
    consistency (the parent may have just written the row).

    Returns the most recent event row for this correlation_id, or None if
    the chain is unknown/expired.
    """
    import boto3
    from boto3.dynamodb.conditions import Key

    table_name = os.environ.get("EVENTS_TABLE", "")
    if not table_name:
        logger.error("agent_trigger: EVENTS_TABLE not configured")
        return None

    region = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))
    dynamodb = boto3.resource("dynamodb", region_name=region)
    table = dynamodb.Table(table_name)

    for attempt in range(_GSI_MAX_ATTEMPTS):
        try:
            resp = table.query(
                IndexName="correlation-index",
                KeyConditionExpression=Key("correlation_id").eq(correlation_id),
                ScanIndexForward=False,  # Most recent first
                Limit=1,
            )
            items = resp.get("Items", [])
            if items:
                return items[0]
        except Exception as e:
            logger.warning(
                "agent_trigger: GSI query failed attempt=%d error=%s",
                attempt + 1,
                e,
            )

        # Retry with backoff (only if not last attempt)
        if attempt < _GSI_MAX_ATTEMPTS - 1:
            time.sleep(_GSI_RETRY_DELAY_S)

    return None


class _SyntheticIdentity:
    """Minimal identity object for spawn_persona compatibility."""

    def __init__(self, *, tenant_id: str, org_id: str, user_id: str, user_kind: str, bot_kind: str):
        self.tenant_id = tenant_id
        self.org_id = org_id
        self.user_id = user_id
        self.user_kind = user_kind
        self.bot_kind = bot_kind


def _extract_bot_kind_from_arn(caller_arn: str) -> str:
    """Extract a bot_kind hint from the caller's IAM role ARN.

    Example: arn:aws:sts::123:assumed-role/adp-dev-agent-worker-role/session
    -> returns "" (no persona inference from ARN — guards use the target persona).
    """
    # We don't infer bot_kind from ARN — it would be unreliable.
    # The self-mention guard compares persona vs bot_kind; returning ""
    # means the guard won't fire for IAM callers (correct — the caller is
    # authenticated, not a bot mentioning itself).
    return ""


def _safe_int(value: Any, default: int) -> int:
    """Safely convert to int with fallback."""
    if value is None:
        return default
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def _response(status_code: int, body: dict) -> dict:
    """Build API Gateway response."""
    return {
        "statusCode": status_code,
        "body": json.dumps(body),
        "headers": {"Content-Type": "application/json"},
    }
