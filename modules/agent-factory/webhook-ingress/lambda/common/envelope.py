"""Normalized webhook envelope schema.

Defines the structure published to SQS regardless of source channel.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class Actor:
    """The user/bot that triggered the event."""

    user_id: str = ""
    org_id: str = ""
    github_id: int = 0
    github_login: str = ""
    is_bot: bool = False  # Deprecated: use correlation fields instead


@dataclass
class Correlation:
    """Provenance chain context for agent-to-agent flows."""

    correlation_id: str = ""
    root_human_id: str = ""
    is_human_rooted: bool = True


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
    correlation: Correlation = field(default_factory=Correlation)
    payload: dict[str, Any] = field(default_factory=dict)
    arrived_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    # Issue #2279: Caller-chosen model fields (optional, human /model directive)
    model_requested: str | None = None  # Raw alias the user typed
    model_resolved: str | None = None  # Validated Bedrock model ID, or None
    # Issue #3574: Explicit AWS credential label (optional, human /aws-label directive)
    aws_label: str | None = None  # Validated label targeting a specific linked account

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict for JSON/SQS publishing."""
        return {
            "version": self.version,
            "channel": self.channel,
            "tenant_id": self.tenant_id,
            "persona": self.persona,
            "actor": {
                "user_id": self.actor.user_id,
                "org_id": self.actor.org_id,
                "github_id": self.actor.github_id,
                "github_login": self.actor.github_login,
                "is_bot": self.actor.is_bot,  # Deprecated
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
            "correlation": {
                "correlation_id": self.correlation.correlation_id,
                "root_human_id": self.correlation.root_human_id,
                "is_human_rooted": self.correlation.is_human_rooted,
            },
            "payload": self.payload,
            "arrived_at": self.arrived_at,
            "model_requested": self.model_requested,
            "model_resolved": self.model_resolved,
            "aws_label": self.aws_label,
        }
