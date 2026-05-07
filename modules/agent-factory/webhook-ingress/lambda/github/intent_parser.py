"""GitHub event → persona + intent mapping.

Parses incoming GitHub webhook events into actionable intents that determine
which agent persona should handle the work.
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
    "@agent-malware-analysis-agent": "malware-analysis-agent",
    "@agent-superpower": "pt-superpower",
}


@dataclass
class Intent:
    """Parsed intent from a GitHub webhook event."""

    persona: str
    trigger: str
    label: str | None = None


def extract_intent(event_type: str, payload: dict) -> Intent | None:
    """Parse a GitHub webhook event into an actionable intent.

    Args:
        event_type: Value of X-GitHub-Event header (e.g. "issues", "pull_request").
        payload: Parsed JSON body of the webhook.

    Returns:
        Intent if the event should trigger agent work, None for no-op events.
    """
    # Guard: ignore bot-generated events to prevent feedback loops
    sender = payload.get("sender", {})
    if _is_bot_sender(sender):
        logger.info("Ignoring bot-generated event from %s", sender.get("login", "unknown"))
        return None

    action = payload.get("action", "")

    # issues + labeled → map label to persona
    if event_type == "issues" and action == "labeled":
        return _handle_issue_labeled(payload)

    # pull_request + opened|synchronize → reviewer persona
    if event_type == "pull_request" and action in ("opened", "synchronize"):
        return _handle_pr_event(payload, action)

    # issue_comment + created → parse @-mentions
    if event_type == "issue_comment" and action == "created":
        return _handle_issue_comment(payload)

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


def _handle_issue_comment(payload: dict) -> Intent | None:
    """Handle issue_comment.created — look for @-mentions of agent personas."""
    comment = payload.get("comment", {})
    body = comment.get("body", "")

    if not body:
        return None

    # Find first matching mention (process first match only)
    for mention, persona in MENTION_TO_PERSONA.items():
        if mention in body:
            return Intent(persona=persona, trigger="mentioned", label=None)

    return None
