"""GitHub event → persona + intent mapping.

Parses incoming GitHub webhook events into actionable intents that determine
which agent persona should handle the work.

Issue #1696: Replaced the binary is_new_chain bot guard with a depth-only loop
guard (MAX_CHAIN_DEPTH=8, env-configurable). The self-mention guard is evaluated
BEFORE the depth check. Bot pull_request events are allowed when the PR body
carries a valid adp-* marker (marker-gated relaxation). The agent/issue-* branch
filter and synchronize gate prevent double-triggering.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Maximum chain depth before blocking bot-to-bot triggers (issue #1696).
# Env-configurable for operational flexibility. Default 8 bounds all loop
# topologies (proven in architect review). Worst case: 8 runs ≈ $4-$16.
MAX_CHAIN_DEPTH = int(os.environ.get("MAX_CHAIN_DEPTH", "8"))

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

    # Non-comment, non-PR events: keep binary bot guard (no behavior change)
    if event_type not in ("issue_comment", "pull_request") and _is_bot_sender(sender):
        logger.info(
            "Ignoring bot-generated %s event from %s",
            event_type,
            sender.get("login", "unknown"),
        )
        return None

    # pull_request events from bots: marker-gated relaxation (issue #1696).
    # Allow ONLY when the PR body carries a valid adp-* marker AND branch
    # matches agent/issue-*. Without a marker, bot PRs stay blocked.
    if event_type == "pull_request" and _is_bot_sender(sender):
        pr_body = payload.get("pull_request", {}).get("body", "") or ""
        from common.marker_parse import has_valid_marker

        if not has_valid_marker(pr_body):
            logger.info(
                "Bot PR event from %s blocked — no valid adp-* marker in PR body",
                sender.get("login", "unknown"),
            )
            return None

    # issues + labeled → map label to persona
    if event_type == "issues" and action == "labeled":
        return _handle_issue_labeled(payload)

    # pull_request + opened|synchronize → reviewer persona (with guards)
    if event_type == "pull_request" and action in ("opened", "synchronize"):
        return _handle_pr_event(payload, action, sender)

    # issue_comment + created → chain-aware handler (Phase 2-c / issue #1696)
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


def _extract_all_mention_personas(body: str) -> list[str]:
    """Extract ALL @agent-X persona mentions from comment body.

    Returns a list of persona strings. Supports fan-out (one comment
    triggering multiple agents). Order follows MENTION_TO_PERSONA iteration.
    """
    if not body:
        return []
    personas = []
    for mention, persona in MENTION_TO_PERSONA.items():
        if mention in body:
            personas.append(persona)
    return personas


def _handle_issue_labeled(payload: dict) -> Intent | None:
    """Handle issues.labeled event — map the added label to a persona."""
    label = payload.get("label", {})
    label_name = label.get("name", "")

    persona = LABEL_TO_PERSONA.get(label_name)
    if not persona:
        logger.debug("Label '%s' has no persona mapping — no-op", label_name)
        return None

    return Intent(persona=persona, trigger="issue_labeled", label=label_name)


def _handle_pr_event(payload: dict, action: str, sender: dict) -> Intent | None:
    """Handle pull_request opened/synchronize — assign reviewer persona.

    Issue #1696 guards:
    - Branch filter: only trigger for agent/issue-* branches
    - Synchronize gate: bot senders only trigger on 'opened' (not synchronize)
      to prevent runaway reviewer spawning on every push
    """
    # Branch filter: only trigger reviewer for agent PR branches (issue #1696).
    # Reproduces the behavior of the removed pr-review-trigger.yml.
    head_ref = payload.get("pull_request", {}).get("head", {}).get("ref", "")
    if not head_ref.startswith("agent/issue-"):
        logger.debug(
            "PR branch '%s' does not match agent/issue-* pattern — no reviewer trigger",
            head_ref,
        )
        return None

    # Synchronize gate: bot senders only trigger on 'opened' (issue #1696).
    # Without this, every push to an agent PR branch (including the reviewer's
    # own fix commits) would spawn a NEW reviewer = runaway.
    if action == "synchronize" and _is_bot_sender(sender):
        logger.info(
            "Bot PR synchronize event from %s — blocking to prevent double-trigger",
            sender.get("login", "unknown"),
        )
        return None

    return Intent(persona="reviewer", trigger=f"pr_{action}", label=None)


def _handle_issue_comment(
    payload: dict,
    correlation_ctx: dict | None,
    resolved_identity,
) -> Intent | None:
    """Handle issue_comment.created — depth-only loop guard (issue #1696).

    For human senders: parse @-mention as before, always produces intent.
    For bot senders: apply depth-only guard (MAX_CHAIN_DEPTH) with self-mention
    guard evaluated first. Replaces the binary is_new_chain block.
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

    # Bot sender: depth-only loop guard (issue #1696)
    if correlation_ctx is None:
        # No correlation context available (e.g. Phase 2-b not configured).
        # Fall back to blocking all bot mentions (safe default).
        logger.info(
            "Bot %s mentioned %s but no correlation context available — blocking (safe default)",
            sender.get("login", "unknown"),
            persona,
        )
        return None

    # Self-mention guard: evaluated BEFORE depth check (catches depth-0 self-loops)
    if resolved_identity is not None and hasattr(resolved_identity, "bot_kind"):
        if persona == resolved_identity.bot_kind:
            logger.info(
                "Bot %s self-mention to persona %s — blocking",
                sender.get("login", "unknown"),
                persona,
            )
            _emit_metric("SelfMentionBlocked", {"persona": persona})
            return None

    # Immediate self-re-trigger guard (issue #1716): block when the mentioned
    # persona is the SAME as the persona most recently triggered in this chain.
    # This stops an agent's own status comments (which contain its persona token
    # in boilerplate, e.g. "**Agent**: @agent-developer") from re-spawning the
    # same persona — the self-loop the depth-only guard (issue #1696) failed to
    # catch because the shared bot identity doesn't resolve to a per-persona
    # bot_kind. Server-side (read from the correlation pointer), NOT spoofable
    # via the marker. Cross-persona cycles (reviewer→developer→reviewer) remain
    # allowed because each hop's persona differs from the previous.
    last_persona = correlation_ctx.get("last_triggered_persona")
    if last_persona and persona == last_persona:
        logger.info(
            "Bot %s mentioned %s but that persona was the last triggered in chain %s "
            "— blocking immediate self-re-trigger",
            sender.get("login", "unknown"),
            persona,
            correlation_ctx.get("correlation_id", "unknown"),
        )
        _emit_metric("SelfReTriggerBlocked", {"persona": persona})
        return None

    # Depth-only guard (issue #1696): block when chain depth >= MAX_CHAIN_DEPTH.
    # chain_depth in correlation_ctx is the CURRENT run's depth (already incremented
    # by determine_correlation). If it meets or exceeds the cap, block.
    chain_depth = correlation_ctx.get("chain_depth", 0)
    if chain_depth >= MAX_CHAIN_DEPTH:
        logger.info(
            "Bot %s mentioned %s but chain depth %d >= max %d — blocking (loop prevention)",
            sender.get("login", "unknown"),
            persona,
            chain_depth,
            MAX_CHAIN_DEPTH,
        )
        _emit_metric("ChainDepthExceeded", {"persona": persona, "depth": str(chain_depth)})
        return None

    # Depth within bounds, not self-mention → allow bot-to-bot trigger
    logger.info(
        "Bot %s triggering %s at chain depth %d — allowing",
        sender.get("login", "unknown"),
        persona,
        chain_depth,
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
