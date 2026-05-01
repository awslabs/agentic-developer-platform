"""Normalized webhook envelope schema.

Defines the structure published to SQS regardless of source channel.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class Actor:
    """The user/bot that triggered the event."""

    github_id: int = 0
    github_login: str = ""
    is_bot: bool = False


@dataclass
class SourceRef:
    """Reference back to the source event location."""

    installation_id: int = 0
    repo: str = ""
    issue: int | None = None
    pr: int | None = None
    sha: str | None = None


@dataclass
class Intent:
    """Parsed intent from the webhook event."""

    trigger: str = ""
    label: str | None = None
    persona: str = ""


@dataclass
class WebhookEnvelope:
    """Normalized envelope published to SQS."""

    version: str = "1.0"
    channel: str = "github"
    tenant_id: str = ""
    persona: str = ""
    actor: Actor = field(default_factory=Actor)
    source_ref: SourceRef = field(default_factory=SourceRef)
    intent: Intent = field(default_factory=Intent)
    payload: dict[str, Any] = field(default_factory=dict)
    arrived_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict for JSON/SQS publishing."""
        return {
            "version": self.version,
            "channel": self.channel,
            "tenant_id": self.tenant_id,
            "persona": self.persona,
            "actor": {
                "github_id": self.actor.github_id,
                "github_login": self.actor.github_login,
                "is_bot": self.actor.is_bot,
            },
            "source_ref": {
                "installation_id": self.source_ref.installation_id,
                "repo": self.source_ref.repo,
                "issue": self.source_ref.issue,
                "pr": self.source_ref.pr,
                "sha": self.source_ref.sha,
            },
            "intent": {
                "trigger": self.intent.trigger,
                "label": self.intent.label,
                "persona": self.intent.persona,
            },
            "payload": self.payload,
            "arrived_at": self.arrived_at,
        }
