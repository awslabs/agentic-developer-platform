"""Shared spawn_persona() — the ONE place loop guards + publish live.

Issue #2151: Extracted from handler.py steps 12-15 and intent_parser.py guard
logic. Every trigger adapter (GitHub comment, agent HTTP, EventBridge) calls
this single function. Guards exist in ONE place so no spawn path can drift.

The function performs, in order:
  1. Persona validation (against VALID_PERSONAS)
  2. Self-mention guard (bot dispatching to its own persona)
  3. Self-re-trigger guard (same as last_triggered_persona on channel)
  4. Cross-persona loop guard (A->B->A->B alternation detection)
  5. Depth guard (MAX_CHAIN_DEPTH)
  6. Pointer write (with recent_triggered_personas merge)
  7. Provenance write
  8. Envelope build
  9. Webhook-events capture (DDB row BEFORE SQS)
  10. SQS publish

Returns SpawnResult indicating success (with message_id) or block (with reason).
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from dataclasses import dataclass

from common.personas import VALID_PERSONAS

logger = logging.getLogger(__name__)

# Maximum chain depth before blocking bot-to-bot triggers (issue #1696).
MAX_CHAIN_DEPTH = int(os.environ.get("MAX_CHAIN_DEPTH", "8"))

# Issue #2149: Cross-persona loop threshold.
CROSS_PERSONA_LOOP_THRESHOLD = int(os.environ.get("CROSS_PERSONA_LOOP_THRESHOLD", "4"))


@dataclass
class SpawnResult:
    """Outcome of a spawn_persona() call."""

    success: bool
    message_id: str | None = None
    block_reason: str | None = None


def spawn_persona(
    *,
    persona: str,
    correlation_ctx: dict,
    channel_key: str,
    resolved_identity,
    tenant_id: str,
    actor_user_id: str,
    actor_org_id: str,
    sender: dict,
    event_type: str,
    action: str,
    installation_id: int,
    repo: str,
    payload: dict,
    intent_trigger: str,
    intent_label: str | None = None,
    model_requested: str | None = None,
    model_resolved: str | None = None,
) -> SpawnResult:
    """Validate guards, write lineage, build envelope, publish to SQS.

    This is the ONLY place spawn logic lives. All trigger adapters call this.

    Args:
        persona: Target persona to spawn (must be in VALID_PERSONAS).
        correlation_ctx: From determine_correlation() — contains chain_depth,
            last_triggered_persona, recent_triggered_personas, etc.
        channel_key: Canonical channel key for pointer writes.
        resolved_identity: ResolvedIdentity from identity resolver.
        tenant_id: Resolved tenant ID.
        actor_user_id: Platform user_id of the sender.
        actor_org_id: Platform org_id.
        sender: Raw sender dict from payload (github_id, login, type).
        event_type: Webhook event type (e.g. "issue_comment").
        action: Webhook action (e.g. "created").
        installation_id: GitHub App installation ID.
        repo: Full repo name (e.g. "org/repo").
        payload: Full webhook payload dict.
        intent_trigger: Trigger string (e.g. "mentioned", "issue_labeled").
        intent_label: Optional label that triggered this (for issues.labeled).
        model_requested: Raw alias the user typed in /model directive (issue #2279).
        model_resolved: Validated Bedrock model ID, or None if rejected/absent.

    Returns:
        SpawnResult with success=True and message_id, or success=False and
        block_reason explaining why the spawn was blocked.
    """
    # --- Guard 1: Persona validation ---
    if persona not in VALID_PERSONAS:
        logger.warning("spawn_persona: unknown persona %r — blocking", persona)
        _emit_metric("UnknownPersonaBlocked", {"persona": persona})
        return SpawnResult(success=False, block_reason="unknown_persona")

    # --- Guards 2-5: Only apply to bot senders ---
    is_bot = _is_bot_sender(sender)
    if is_bot:
        block = _apply_bot_guards(
            persona=persona,
            correlation_ctx=correlation_ctx,
            resolved_identity=resolved_identity,
            sender=sender,
        )
        if block is not None:
            return block

    # --- Step 6: Write pointer + provenance (fail-soft) ---
    _write_pointer_and_provenance(
        persona=persona,
        correlation_ctx=correlation_ctx,
        channel_key=channel_key,
        resolved_identity=resolved_identity,
        actor_user_id=actor_user_id,
        event_type=event_type,
        action=action,
        repo=repo,
        payload=payload,
    )

    # --- Step 7: Build envelope ---
    cognito_sub = actor_user_id if resolved_identity.user_kind == "human" else ""
    envelope = _build_envelope(
        persona=persona,
        tenant_id=tenant_id,
        cognito_sub=cognito_sub,
        actor_user_id=actor_user_id,
        actor_org_id=actor_org_id,
        sender=sender,
        installation_id=installation_id,
        repo=repo,
        payload=payload,
        correlation_ctx=correlation_ctx,
        intent_trigger=intent_trigger,
        intent_label=intent_label,
        model_requested=model_requested,
        model_resolved=model_resolved,
    )

    # --- Step 8: Capture invocation event to DDB BEFORE SQS ---
    _capture_invocation_event(
        envelope=envelope,
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        sender=sender,
        event_type=event_type,
        action=action,
        installation_id=installation_id,
        repo=repo,
        persona=persona,
        payload=payload,
        correlation_ctx=correlation_ctx,
    )

    # --- Step 9: Publish to SQS ---
    from common.sqs_publisher import publish_envelope

    message_id = publish_envelope(envelope)
    if not message_id:
        logger.error("spawn_persona: SQS publish failed for persona=%s", persona)
        return SpawnResult(success=False, block_reason="sqs_publish_failed")

    logger.info(
        "spawn_persona: published persona=%s sqs_message_id=%s",
        persona,
        message_id,
    )
    return SpawnResult(success=True, message_id=message_id)


def _is_bot_sender(sender: dict) -> bool:
    """Check if the sender is a bot (GitHub App or bot user)."""
    if sender.get("type") == "Bot":
        return True
    login = sender.get("login", "")
    if login.endswith("[bot]"):
        return True
    return False


def _apply_bot_guards(
    *,
    persona: str,
    correlation_ctx: dict,
    resolved_identity,
    sender: dict,
) -> SpawnResult | None:
    """Apply bot loop guards. Returns SpawnResult if blocked, else None."""
    sender_login = sender.get("login", "unknown")

    # Guard 2: Self-mention — bot dispatching to its own persona
    if resolved_identity is not None and hasattr(resolved_identity, "bot_kind"):
        if persona == resolved_identity.bot_kind:
            logger.info(
                "spawn_persona: self-mention blocked — %s to %s",
                sender_login,
                persona,
            )
            _emit_metric("SelfMentionBlocked", {"persona": persona})
            return SpawnResult(success=False, block_reason="self_mention")

    # Guard 3: Self-re-trigger — same as last_triggered_persona on channel
    last_persona = correlation_ctx.get("last_triggered_persona")
    if last_persona and persona == last_persona:
        logger.info(
            "spawn_persona: self-re-trigger blocked — %s targeting %s "
            "(last triggered in chain %s)",
            sender_login,
            persona,
            correlation_ctx.get("correlation_id", "unknown"),
        )
        _emit_metric("SelfReTriggerBlocked", {"persona": persona})
        return SpawnResult(success=False, block_reason="self_re_trigger")

    # Guard 4: Cross-persona loop — A->B->A->B alternation detection
    recent_personas = correlation_ctx.get("recent_triggered_personas", set())
    recent_count = correlation_ctx.get("recent_trigger_count", 0)
    if persona in recent_personas and recent_count >= CROSS_PERSONA_LOOP_THRESHOLD:
        logger.info(
            "spawn_persona: cross-persona loop blocked — %s already in recent set %s "
            "(count=%d, threshold=%d) in chain %s",
            persona,
            recent_personas,
            recent_count,
            CROSS_PERSONA_LOOP_THRESHOLD,
            correlation_ctx.get("correlation_id", "unknown"),
        )
        _emit_metric("CrossPersonaLoopBlocked", {"persona": persona})
        return SpawnResult(success=False, block_reason="cross_persona_loop")

    # Guard 5: Depth cap
    chain_depth = correlation_ctx.get("chain_depth", 0)
    if chain_depth >= MAX_CHAIN_DEPTH:
        logger.info(
            "spawn_persona: depth guard blocked — %s at depth %d >= max %d",
            persona,
            chain_depth,
            MAX_CHAIN_DEPTH,
        )
        _emit_metric(
            "ChainDepthExceeded", {"persona": persona, "depth": str(chain_depth)}
        )
        return SpawnResult(success=False, block_reason="chain_depth_exceeded")

    # All guards passed
    source_bot = ""
    if resolved_identity is not None and hasattr(resolved_identity, "bot_kind"):
        source_bot = resolved_identity.bot_kind
    _emit_metric(
        "BotToBotTrigger",
        {"source_bot": source_bot, "target_persona": persona},
    )
    return None


def _write_pointer_and_provenance(
    *,
    persona: str,
    correlation_ctx: dict,
    channel_key: str,
    resolved_identity,
    actor_user_id: str,
    event_type: str,
    action: str,
    repo: str,
    payload: dict,
) -> None:
    """Write correlation pointer + provenance record (fail-soft)."""
    if not channel_key:
        return

    # Provenance write
    try:
        from common.gateway_client import post_provenance

        post_provenance(
            actor_user_id=actor_user_id,
            triggered_by=correlation_ctx.get("triggered_by"),
            root_human_id=correlation_ctx["root_human_id"],
            is_human_rooted=correlation_ctx["is_human_rooted"],
            action_kind="webhook_trigger",
            source_event={
                "event_type": event_type,
                "action": action,
                "repo": repo,
                "issue": payload.get("issue", {}).get("number"),
            },
            correlation_id=correlation_ctx["correlation_id"],
            org_id=resolved_identity.org_id,
            parent_invocation_id=correlation_ctx.get("parent_invocation_id"),
        )
    except Exception as e:
        logger.warning("spawn_persona: post_provenance failed (fail-soft): %s", e)

    # Pointer write — merge persona into recent set
    try:
        from common.correlation_store import write_pointer

        existing_recent = correlation_ctx.get("recent_triggered_personas", set())
        if not isinstance(existing_recent, set):
            existing_recent = set(existing_recent) if existing_recent else set()
        updated_recent = existing_recent | {persona}
        updated_count = correlation_ctx.get("recent_trigger_count", 0) + 1

        write_pointer(
            key=channel_key,
            correlation_id=correlation_ctx["correlation_id"],
            root_human_id=correlation_ctx["root_human_id"],
            is_human_rooted=correlation_ctx["is_human_rooted"],
            last_triggered_persona=persona,
            recent_triggered_personas=updated_recent,
            recent_trigger_count=updated_count,
        )
    except Exception as e:
        logger.warning("spawn_persona: write_pointer failed (fail-soft): %s", e)


def _build_envelope(
    *,
    persona: str,
    tenant_id: str,
    cognito_sub: str,
    actor_user_id: str,
    actor_org_id: str,
    sender: dict,
    installation_id: int,
    repo: str,
    payload: dict,
    correlation_ctx: dict,
    intent_trigger: str,
    intent_label: str | None,
    model_requested: str | None = None,
    model_resolved: str | None = None,
) -> dict:
    """Build the normalized webhook envelope for SQS."""
    envelope = {
        "version": "1.0",
        "channel": "github",
        "tenant_id": tenant_id,
        "cognito_sub": cognito_sub,
        "persona": persona,
        "actor": {
            "user_id": actor_user_id,
            "org_id": actor_org_id,
            "github_id": sender.get("id", 0),
            "github_login": sender.get("login", ""),
            "is_bot": sender.get("type") == "Bot",  # Deprecated
        },
        "source_ref": {
            "installation_id": installation_id,
            "repo": repo,
            "issue": payload.get("issue", {}).get("number")
            if "issue" in payload
            else payload.get("pull_request", {}).get("number"),
            "pr": payload.get("pull_request", {}).get("number")
            if "pull_request" in payload
            else None,
            "sha": payload.get("pull_request", {}).get("head", {}).get("sha")
            if "pull_request" in payload
            else None,
        },
        "intent": {
            "trigger": intent_trigger,
            "label": intent_label,
            "persona": persona,
        },
        "correlation": {
            "correlation_id": correlation_ctx.get("correlation_id", ""),
            "root_human_id": correlation_ctx.get("root_human_id", ""),
            "is_human_rooted": correlation_ctx.get("is_human_rooted", True),
            "parent_invocation_id": correlation_ctx.get("parent_invocation_id"),
            "chain_depth": correlation_ctx.get("chain_depth", 0),
        },
        "payload": payload,
        "arrived_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    # Issue #2279: Thread caller-chosen model through the envelope.
    # model_requested = raw alias the user typed; model_resolved = validated
    # Bedrock model ID (or None if rejected). Worker reads model_resolved to
    # override ANTHROPIC_MODEL; uses model_requested for the warning message.
    if model_requested is not None:
        envelope["model_requested"] = model_requested
    if model_resolved is not None:
        envelope["model_resolved"] = model_resolved
    envelope["message_id"] = str(uuid.uuid4())
    return envelope


def _capture_invocation_event(
    *,
    envelope: dict,
    tenant_id: str,
    actor_user_id: str,
    sender: dict,
    event_type: str,
    action: str,
    installation_id: int,
    repo: str,
    persona: str,
    payload: dict,
    correlation_ctx: dict,
) -> None:
    """Write enriched invocation row to DynamoDB (best-effort).

    Issue #2042: attribute the run to the chain's HUMAN ROOT for human-rooted
    chains so it appears in the originating human's Activity view.
    """
    try:
        from common.webhook_events import WebhookEventLogger

        table_name = os.environ.get("EVENTS_TABLE", "")
        if not table_name:
            return

        region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
        event_logger = WebhookEventLogger(table_name=table_name, region=region)

        # Issue #2042: attribute to human root for human-rooted chains
        root_human = correlation_ctx.get("root_human_id")
        is_human_rooted = correlation_ctx.get("is_human_rooted")
        effective_user_id = (
            root_human if (is_human_rooted and root_human) else actor_user_id
        )

        # Derive topic from issue/PR title
        issue_title = payload.get("issue", {}).get("title", "")
        pr_title = payload.get("pull_request", {}).get("title", "")
        topic = (issue_title or pr_title or "(untitled)")[:120]

        # Derive source_url
        issue_url = payload.get("issue", {}).get("html_url", "")
        pr_url = payload.get("pull_request", {}).get("html_url", "")
        source_url = issue_url or pr_url or None

        # Derive issue_number
        issue_number = payload.get("issue", {}).get("number")
        if issue_number is None:
            issue_number = payload.get("pull_request", {}).get("number")

        event_logger.log_event(
            event_id=envelope["message_id"],
            arrived_at=envelope["arrived_at"],
            tenant_id=tenant_id,
            channel="github",
            event_type=event_type,
            action=action,
            installation_id=str(installation_id),
            repo=repo,
            status="webhook_received",
            user_id=effective_user_id or "unattributed",
            github_login=sender.get("login", "") or None,
            persona=persona,
            topic=topic,
            source_url=source_url,
            issue_number=issue_number,
            correlation_id=correlation_ctx.get("correlation_id"),
            parent_invocation_id=correlation_ctx.get("parent_invocation_id"),
            chain_depth=correlation_ctx.get("chain_depth"),
            root_human_id=root_human,
            is_human_rooted=is_human_rooted,
        )
    except Exception as e:
        logger.warning("spawn_persona: capture_invocation_event failed: %s", e)


def _emit_metric(metric_name: str, dimensions: dict[str, str]) -> None:
    """Emit a CloudWatch metric under WebhookIngress namespace (fail-soft)."""
    try:
        import boto3

        cw = boto3.client("cloudwatch", region_name="us-east-1")
        cw.put_metric_data(
            Namespace="WebhookIngress",
            MetricData=[
                {
                    "MetricName": metric_name,
                    "Dimensions": [
                        {"Name": k, "Value": v} for k, v in dimensions.items()
                    ],
                    "Value": 1,
                    "Unit": "Count",
                }
            ],
        )
    except Exception as e:
        logger.debug("Failed to emit metric %s: %s", metric_name, e)
