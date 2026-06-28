"""GitHub event → persona + intent mapping.

Parses incoming GitHub webhook events into actionable intents that determine
which agent persona should handle the work.

Issue #1696: Replaced the binary is_new_chain bot guard with a depth-only loop
guard (MAX_CHAIN_DEPTH=8, env-configurable). The self-mention guard is evaluated
BEFORE the depth check. Bot pull_request events are allowed when the PR body
carries a valid adp-* marker (marker-gated relaxation). The agent/issue-* branch
filter and synchronize gate prevent double-triggering.

Issue #2149: Bot comments now require an explicit `adp-dispatch:<persona>` marker
to trigger a run. Bare `@agent-X` in bot-authored prose (status headers, plans)
no longer triggers. Human comments still trigger from bare `@agent-X` as before.
Additionally, a windowed cross-persona loop guard catches A→B→A→B alternation
that the scalar last_triggered_persona guard cannot detect.

Issue #2151: Loop guards (self-mention, self-re-trigger, cross-persona loop,
depth cap) moved to common/spawn_persona.py. This module now returns Intent
for any valid mention/dispatch without making spawn/block decisions. The guards
are applied once in spawn_persona() — the single enforcement point.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass

from common.personas import LABEL_TO_PERSONA, MENTION_TO_PERSONA

logger = logging.getLogger(__name__)

# Re-export for backward compatibility with existing test imports.
# These constants are now defined in common/personas.py.
__all__ = [
    "LABEL_TO_PERSONA",
    "MENTION_TO_PERSONA",
    "MAX_CHAIN_DEPTH",
    "CROSS_PERSONA_LOOP_THRESHOLD",
    "Intent",
    "extract_intent",
]

# Maximum chain depth before blocking bot-to-bot triggers (issue #1696).
# Env-configurable for operational flexibility. Default 8 bounds all loop
# topologies (proven in architect review). Worst case: 8 runs ≈ $4-$16.
MAX_CHAIN_DEPTH = int(os.environ.get("MAX_CHAIN_DEPTH", "8"))

# Issue #2149: Cross-persona loop threshold. A bot dispatch is blocked when the
# target persona is already in the recent_triggered_personas set AND the total
# trigger count meets or exceeds this threshold. Default 4 allows legitimate
# human→dev→reviewer→dev (3 bot dispatches) while blocking the 4th (loop).
CROSS_PERSONA_LOOP_THRESHOLD = int(os.environ.get("CROSS_PERSONA_LOOP_THRESHOLD", "4"))


@dataclass
class Intent:
    """Parsed intent from a GitHub webhook event."""

    persona: str
    trigger: str
    label: str | None = None
    model: str | None = None


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

    # Bot pull_request events: allowed through to _handle_pr_event, which gates
    # on the agent/issue-* branch filter + the synchronize dedup (issue #1716).
    #
    # Issue #1731: We deliberately DO NOT require an adp-* marker in the PR body
    # to TRIGGER the reviewer. The earlier marker-gate (#1696) created a race:
    # the agent self-opens the PR (fires pull_request.opened) BEFORE the
    # entrypoint backfills the marker (#1727), so the opened event always saw an
    # unmarked body and was blocked. Triggering is a separate decision from
    # lineage:
    #   - SPAWN gate  = bot identity (already 403-gated by identity resolution
    #     in handler.py BEFORE this runs) + agent/issue-* branch + opened.
    #   - LINEAGE      = best-effort: the PR-body marker (if present by now) OR
    #     the issue's correlation pointer (issue number is in the branch name),
    #     resolved in determine_correlation. Marker ABSENCE no longer blocks.
    # Non-ADP bots (dependabot, etc.) never reach here — they 403 upstream.

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


# Issue #2279: Line-anchored regex for /model directive. Must start at
# beginning of a line to avoid matching inline code or blockquotes.
# First match wins when multiple /model lines are present.
_MODEL_DIRECTIVE_RE = re.compile(r"^/model\s+(\S+)\s*$", re.MULTILINE)


def _extract_model_directive(body: str) -> str | None:
    """Extract the /model alias from the comment body (issue #2279).

    Uses a line-anchored regex so it won't match `/model x` in inline code
    (backtick lines), blockquotes (`> /model x`), or embedded prose.
    Returns the raw alias string (e.g. "opus") or None if no directive found.
    First match wins if multiple /model lines are present.
    """
    if not body:
        return None
    match = _MODEL_DIRECTIVE_RE.search(body)
    if match:
        return match.group(1)
    return None


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
    """Handle issue_comment.created — dispatch-marker gate for bot comments.

    Issue #2149: Bot comments require an explicit `adp-dispatch:<persona>` marker.
    Bare `@agent-X` in bot prose is treated as text.
    Human comments still trigger from bare `@agent-X` as before.

    Issue #2151: Loop guards (self-mention, self-re-trigger, cross-persona loop,
    depth cap) have been moved to common/spawn_persona.py. This function now
    returns Intent for any valid mention/dispatch WITHOUT making block decisions.
    The guards are enforced once in spawn_persona() — the single point.

    For human senders: parse @-mention, always produces intent.
    For bot senders: require adp-dispatch marker + correlation_ctx to produce
    intent. Guards are applied downstream by spawn_persona().
    """
    sender = payload.get("sender", {})
    body = payload.get("comment", {}).get("body", "")

    # Human sender: parse @-mention, always allow (no chain-aware gating needed)
    if not _is_bot_sender(sender):
        persona = _extract_mention_persona(body)
        if not persona:
            return None
        # Issue #2279: Parse /model directive (human path only)
        model = _extract_model_directive(body)
        return Intent(persona=persona, trigger="mentioned", label=None, model=model)

    # --- Bot sender path (issue #2149) ---
    # Bot comments require an explicit adp-dispatch:<persona> marker to trigger.
    # This separates deliberate dispatch from incidental @agent-X prose in
    # status comments, plans, and boilerplate.
    persona = _extract_dispatch_persona_from_marker(body)
    if not persona:
        # No dispatch marker found. As a fallback, check if the body contains
        # a bare @agent-X mention — but log it as blocked (the comment is prose,
        # not a dispatch). This logging helps diagnose when a legitimate emit
        # site forgets to include the dispatch marker.
        bare_persona = _extract_mention_persona(body)
        if bare_persona:
            logger.info(
                "Bot %s comment contains @agent-%s but no adp-dispatch marker "
                "— treating as prose, not dispatch (issue #2149)",
                sender.get("login", "unknown"),
                bare_persona,
            )
            _emit_metric("BotMentionWithoutDispatchMarker", {"persona": bare_persona})
        return None

    # Bot sender: require correlation context (safe default blocks without it)
    if correlation_ctx is None:
        logger.info(
            "Bot %s mentioned %s but no correlation context available — blocking (safe default)",
            sender.get("login", "unknown"),
            persona,
        )
        return None

    # Issue #2151: Guards removed — spawn_persona() enforces them.
    # Return Intent so handler can call spawn_persona() with full context.
    return Intent(persona=persona, trigger="mentioned", label=None)


def _extract_dispatch_persona_from_marker(body: str) -> str | None:
    """Extract the dispatch persona from an adp-dispatch marker in the body.

    Issue #2149: Only parses the first 1000 bytes (markers are always prepended).
    Returns the persona string if `adp-dispatch:<persona>` is found, else None.
    """
    if not body:
        return None

    from common.marker_parse import parse_marker

    marker_data = parse_marker(body)
    if marker_data is None:
        return None

    dispatch_persona = marker_data.get("dispatch_persona")
    if not dispatch_persona:
        return None

    # Validate the dispatch persona against known personas
    if dispatch_persona not in {p for p in MENTION_TO_PERSONA.values()}:
        logger.warning(
            "adp-dispatch marker contains unknown persona %r — ignoring",
            dispatch_persona,
        )
        return None

    return dispatch_persona


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
