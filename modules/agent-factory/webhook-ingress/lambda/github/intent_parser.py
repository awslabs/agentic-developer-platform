"""GitHub event → persona + intent mapping.

Parses incoming GitHub webhook events into actionable intents that determine
which agent persona should handle the work.

Phase 2-c (#786): Bot guard split by event type. For issue_comment events,
bot senders are routed through chain-aware logic that allows bot-to-bot
triggering when starting a new correlation chain, while blocking continuation
mentions within an active chain (loop prevention). For all other event types
(issues.labeled, pull_request), the binary bot guard is preserved unchanged.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Label names that trigger specific agent personas.
LABEL_TO_PERSONA: dict[str, str] = {
    "developer": "developer",
    "pm": "pm",
    "agent-operations": "operations",
    "agent-reviewer": "reviewer",
    "agent-architect": "architect",
    "malware-analysis-agent": "malware-analysis-agent",
    "superpower": "pt-superpower",
}

# @-mention patterns in issue/PR comments that trigger personas.
MENTION_TO_PERSONA: dict[str, str] = {
    "@agent-developer": "developer",
    "@agent-pm": "pm",
    "@agent-operations": "operations",
    "@agent-reviewer": "reviewer",
    "@agent-architect": "architect",
    "@agent-product": "product",
    "@agent-malware-analysis-agent": "malware-analysis-agent",
    "@agent-superpower": "pt-superpower",
}


@dataclass
class Intent:
    """Parsed intent from a GitHub webhook event."""

    persona: str
    trigger: str
    label: str | None = None


def extract_intent(
    event_type: str,
    payload: dict,
    *,
    correlation_ctx: dict | None = None,
    resolved_identity=None,
) -> Intent | None:
    """Parse a GitHub webhook event into an actionable intent.

    Args:
        event_type: Value of X-GitHub-Event header (e.g. "issues", "pull_request").
        payload: Parsed JSON body of the webhook.
        correlation_ctx: Correlation context from determine_correlation() (Phase 2-c).
        resolved_identity: ResolvedIdentity from identity resolver (Phase 2-c).

    Returns:
        Intent if the event should trigger agent work, None for no-op events.
    """
    sender = payload.get("sender", {})
    action = payload.get("action", "")

    # Non-comment events: keep binary bot guard (no behavior change from pre-2c)
    if event_type != "issue_comment" and _is_bot_sender(sender):
        logger.info(
            "Ignoring bot-generated %s event from %s",
            event_type,
            sender.get("login", "unknown"),
        )
        return None

    # issues + labeled → map label to persona
    if event_type == "issues" and action == "labeled":
        return _handle_issue_labeled(payload)

    # pull_request + opened|synchronize → reviewer persona
    if event_type == "pull_request" and action in ("opened", "synchronize"):
        return _handle_pr_event(payload, action)

    # issue_comment + created → chain-aware handler (Phase 2-c)
    if event_type == "issue_comment" and action == "created":
        return _handle_issue_comment(payload, correlation_ctx, resolved_identity)

    # installation + created → log only, no agent dispatch
    if event_type == "installation" and action == "created":
        logger.info(
            "New GitHub App installation: id=%d account=%s",
            payload.get("installation", {}).get("id", 0),
            payload.get("installation", {}).get("account", {}).get("login", "unknown"),
        )
        return None

    return None


def _is_bot_sender(sender: dict) -> bool:
    """Check if the sender is a bot (GitHub App or bot user)."""
    if sender.get("type") == "Bot":
        return True
    login = sender.get("login", "")
    # GitHub App bots have [bot] suffix in their login
    if login.endswith("[bot]"):
        return True
    return False


def _extract_mention_persona(body: str) -> str | None:
    """Extract the first @agent-X persona mention from comment body.

    Returns the persona string (e.g. "developer") or None if no mention found.
    """
    if not body:
        return None
    for mention, persona in MENTION_TO_PERSONA.items():
        if mention in body:
            return persona
    return None


def _handle_issue_labeled(payload: dict) -> Intent | None:
    """Handle issues.labeled event — map the added label to a persona."""
    label = payload.get("label", {})
    label_name = label.get("name", "")

    persona = LABEL_TO_PERSONA.get(label_name)
    if not persona:
        logger.debug("Label '%s' has no persona mapping — no-op", label_name)
        return None

    return Intent(persona=persona, trigger="issue_labeled", label=label_name)


def _handle_pr_event(payload: dict, action: str) -> Intent | None:
    """Handle pull_request opened/synchronize — assign reviewer persona."""
    return Intent(persona="reviewer", trigger=f"pr_{action}", label=None)


def _handle_issue_comment(
    payload: dict,
    correlation_ctx: dict | None,
    resolved_identity,
) -> Intent | None:
    """Handle issue_comment.created — chain-aware bot-to-bot logic.

    For human senders: parse @-mention as before, always produces intent.
    For bot senders: only allow if this starts a NEW correlation chain
    (no existing pointer on the channel). Blocks continuation mentions
    within an active chain to prevent feedback loops.
    """
    sender = payload.get("sender", {})
    body = payload.get("comment", {}).get("body", "")

    # Parse @-mention regardless of sender kind
    persona = _extract_mention_persona(body)
    if not persona:
        return None

    # Human sender: always allow (no chain-aware gating needed)
    if not _is_bot_sender(sender):
        return Intent(persona=persona, trigger="mentioned", label=None)

    # Bot sender: chain-aware logic
    if correlation_ctx is None:
        # No correlation context available (e.g. Phase 2-b not configured).
        # Fall back to blocking all bot mentions (safe default).
        logger.info(
            "Bot %s mentioned %s but no correlation context available — blocking (safe default)",
            sender.get("login", "unknown"),
            persona,
        )
        return None

    # Bot-to-bot: only allow if this is a NEW sub-chain
    if not correlation_ctx.get("is_new_chain", False):
        logger.info(
            "Bot %s mentioned %s within existing chain %s — blocking to prevent loop",
            sender.get("login", "unknown"),
            persona,
            correlation_ctx.get("correlation_id", "unknown"),
        )
        _emit_metric("BotChainContinuationBlocked", {"persona": persona})
        return None

    # Self-mention guard (bot mentions own persona)
    if resolved_identity is not None and hasattr(resolved_identity, "bot_kind"):
        if persona == resolved_identity.bot_kind:
            logger.info(
                "Bot %s self-mention to persona %s — blocking",
                sender.get("login", "unknown"),
                persona,
            )
            return None

    # New chain, not self-mention → allow bot-to-bot trigger
    logger.info(
        "Bot %s starting new chain by mentioning %s — allowing",
        sender.get("login", "unknown"),
        persona,
    )
    source_bot = ""
    if resolved_identity is not None and hasattr(resolved_identity, "bot_kind"):
        source_bot = resolved_identity.bot_kind
    _emit_metric(
        "BotToBotTrigger",
        {"source_bot": source_bot, "target_persona": persona},
    )

    return Intent(persona=persona, trigger="mentioned", label=None)


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
                    "Dimensions": [{"Name": k, "Value": v} for k, v in dimensions.items()],
                    "Value": 1,
                    "Unit": "Count",
                }
            ],
        )
    except Exception as e:
        logger.debug("Failed to emit metric %s: %s", metric_name, e)
