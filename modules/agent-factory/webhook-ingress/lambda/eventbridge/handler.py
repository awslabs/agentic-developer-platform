"""EventBridge handler for machine/root-triggered agent spawns.

Issue #2154: Handles EventBridge events (CloudWatch alarms, scheduled rules, CI
events) that target the Lambda natively. These create ROOT triggers with
is_human_rooted=false, rooted at a registered service identity.

EventBridge events are identified by shape: they have `source`, `detail-type`,
and `detail` at the top level (no `requestContext` or `headers`).

The InputTransformer on each EventBridge rule maps the raw event to a standard
shape with an `adp_trigger` object containing:
  - persona: target persona to spawn
  - service_identity: registered service account key
  - reason: human-readable reason for the trigger
  - target: optional dict with issue creation/resolution info
  - dedup_key: optional dedup key (defaults to alarm-name or rule-name)

Guards:
  - adp_trigger flag must be present (InputTransformer must set it)
  - service_identity must be registered in identity-index
  - persona must be in VALID_PERSONAS
  - persona must be in allowed_personas (if restriction is set)
  - per-service rate limit (reuses rate-limits table with service identity as key)
"""

from __future__ import annotations

import logging
import os
import time
import uuid

logger = logging.getLogger(__name__)

# Lazy imports to keep cold-start fast
_service_identity_mod = None
_rate_limit_mod = None


def _get_service_identity_mod():
    global _service_identity_mod
    if _service_identity_mod is None:
        from common import service_identity

        _service_identity_mod = service_identity
    return _service_identity_mod


def _get_rate_limiter():
    global _rate_limit_mod
    if _rate_limit_mod is None:
        from common.rate_limit import RateLimiter

        table_name = os.environ.get("RATE_LIMITS_TABLE", "")
        region = os.environ.get("AWS_REGION", "us-east-1")
        _rate_limit_mod = RateLimiter(table_name=table_name, region=region)
    return _rate_limit_mod


def handle_eventbridge(event: dict, context) -> dict:
    """Handle an EventBridge event targeting this Lambda.

    Args:
        event: Raw EventBridge event (with source, detail-type, detail).
        context: Lambda context object.

    Returns:
        Response dict with statusCode and body (for consistency with API GW handler).
    """
    start_time = time.time()

    source = event.get("source", "")
    detail_type = event.get("detail-type", "")
    detail = event.get("detail", {})

    logger.info(
        "EventBridge event: source=%s detail-type=%s",
        source,
        detail_type,
    )

    # 1. Validate adp_trigger flag (set by InputTransformer)
    adp_trigger = detail.get("adp_trigger")
    if not adp_trigger or not isinstance(adp_trigger, dict):
        logger.info(
            "EventBridge event missing adp_trigger — ignoring (source=%s, detail-type=%s)",
            source,
            detail_type,
        )
        return _response(200, {"status": "ignored", "reason": "no_adp_trigger"})

    # 2. Extract fields from adp_trigger
    persona = adp_trigger.get("persona", "")
    service_identity_key = adp_trigger.get("service_identity", "")
    reason = adp_trigger.get("reason", "")
    target = adp_trigger.get("target", {})
    dedup_key = adp_trigger.get("dedup_key", "")

    if not persona:
        logger.warning("EventBridge adp_trigger missing persona")
        return _response(400, {"error": "adp_trigger.persona is required"})

    if not service_identity_key:
        logger.warning("EventBridge adp_trigger missing service_identity")
        return _response(400, {"error": "adp_trigger.service_identity is required"})

    # 3. Validate persona is known
    from common.personas import VALID_PERSONAS

    if persona not in VALID_PERSONAS:
        logger.warning(
            "EventBridge: unknown persona %r from service_identity=%r",
            persona,
            service_identity_key,
        )
        return _response(400, {"error": f"Unknown persona: {persona}"})

    # 4. Resolve service identity -> tenant
    svc_mod = _get_service_identity_mod()
    identity_result, outcome = svc_mod.resolve_service_identity(service_identity_key)

    if identity_result is None:
        logger.warning(
            "EventBridge: unknown service_identity=%r outcome=%s",
            service_identity_key,
            outcome,
        )
        return _response(403, {"error": "unknown_service_identity"})

    # 5. Enforce allowed_personas restriction
    if identity_result.allowed_personas and persona not in identity_result.allowed_personas:
        logger.warning(
            "EventBridge: persona %r not in allowed_personas %r for service=%r",
            persona,
            identity_result.allowed_personas,
            service_identity_key,
        )
        return _response(
            403,
            {"error": "persona_not_allowed", "allowed": identity_result.allowed_personas},
        )

    # 6. Per-service rate limit (uses service_identity as the tenant key)
    rate_limiter = _get_rate_limiter()
    rate_result = rate_limiter.check_and_increment(service_identity_key)
    if not rate_result.allowed:
        logger.warning(
            "EventBridge: rate limited service_identity=%r count=%d limit=%d",
            service_identity_key,
            rate_result.current_count,
            rate_result.limit,
        )
        return _response(
            429,
            {"error": "rate_limited", "retry_after": rate_result.retry_after_seconds},
        )

    # 7. Build ROOT lineage context (new chain, machine-rooted)
    correlation_id = str(uuid.uuid4())
    correlation_ctx = {
        "correlation_id": correlation_id,
        "root_human_id": service_identity_key,
        "is_human_rooted": False,
        "is_new_chain": True,
        "parent_invocation_id": None,
        "chain_depth": 0,
        "last_triggered_persona": None,
        "recent_triggered_personas": set(),
        "recent_trigger_count": 0,
    }

    # 8. Build a minimal channel key for pointer writes
    # Use the dedup_key or fallback to rule/source identity
    effective_dedup = dedup_key or f"{source}:{detail_type}:{service_identity_key}"
    channel_key = f"eventbridge:{effective_dedup}"

    # 9. Build sender and payload for spawn_persona compatibility
    sender = {
        "login": service_identity_key,
        "id": 0,
        "type": "Service",
    }

    # Build a payload that spawn_persona can work with
    # Target may contain issue info for envelope's source_ref
    issue_number = target.get("issue_number") if target else None
    repo = target.get("repo", "") if target else ""

    payload = {
        "eventbridge_source": source,
        "detail_type": detail_type,
        "detail": detail,
        "reason": reason,
        "target": target or {},
    }
    if issue_number:
        payload["issue"] = {"number": issue_number, "title": reason[:120]}

    # 10. Build a mock ResolvedIdentity for spawn_persona
    from common.identity_resolver import ResolvedIdentity

    resolved_identity = ResolvedIdentity(
        tenant_id=identity_result.tenant_id,
        org_id=identity_result.org_id,
        user_id=service_identity_key,
        user_provisioning_mode="strict",
        user_kind="service",
        bot_kind="",
    )

    # 11. Call spawn_persona (single enforcement point)
    from common.spawn_persona import spawn_persona

    spawn_result = spawn_persona(
        persona=persona,
        correlation_ctx=correlation_ctx,
        channel_key=channel_key,
        resolved_identity=resolved_identity,
        tenant_id=identity_result.tenant_id,
        actor_user_id=service_identity_key,
        actor_org_id=identity_result.org_id,
        sender=sender,
        event_type="eventbridge",
        action=detail_type,
        installation_id=0,
        repo=repo,
        payload=payload,
        intent_trigger="eventbridge_rule",
        intent_label=None,
    )

    latency_ms = (time.time() - start_time) * 1000
    logger.info(
        "EventBridge handler complete: persona=%s service=%s success=%s latency_ms=%.1f",
        persona,
        service_identity_key,
        spawn_result.success,
        latency_ms,
    )

    if not spawn_result.success:
        if spawn_result.block_reason == "sqs_publish_failed":
            return _response(500, {"error": "Failed to enqueue"})
        return _response(200, {"status": "no_op", "reason": spawn_result.block_reason})

    return _response(
        202,
        {
            "status": "accepted",
            "message_id": spawn_result.message_id,
            "correlation_id": correlation_id,
            "is_human_rooted": False,
        },
    )


def _response(status_code: int, body: dict) -> dict:
    """Build response dict (consistent shape with API GW handler)."""
    import json

    return {
        "statusCode": status_code,
        "body": json.dumps(body),
    }
