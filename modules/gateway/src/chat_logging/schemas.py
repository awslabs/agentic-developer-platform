"""Pydantic schemas for chat logging.

Issue #143: Defines the structure for chat logs stored in S3.
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class ChatLogRequest(BaseModel):
    """Request portion of the chat log.

    Captures the full request body sent to the model.
    """

    messages: list[dict[str, Any]] = Field(default_factory=list, description="Chat messages array")
    system: str | list[dict[str, Any]] | None = Field(default=None, description="System prompt")
    tools: list[dict[str, Any]] | None = Field(default=None, description="Tool definitions")
    max_tokens: int | None = Field(default=None, description="Max tokens requested")
    temperature: float | None = Field(default=None, description="Temperature setting")
    top_p: float | None = Field(default=None, description="Top-p sampling setting")
    top_k: int | None = Field(default=None, description="Top-k sampling setting")
    stop_sequences: list[str] | None = Field(default=None, description="Stop sequences")


class UsageInfo(BaseModel):
    """Token usage information.

    Issue #1486: Includes prompt-cache token fields so the budget-usage-tracker
    Lambda can price cached traffic correctly.
    """

    input_tokens: int | None = Field(default=None, description="Number of input tokens")
    output_tokens: int | None = Field(default=None, description="Number of output tokens")
    cache_read_input_tokens: int | None = Field(default=None, description="Tokens served from prompt cache")
    cache_creation_input_tokens: int | None = Field(default=None, description="Tokens written to prompt cache")

    cache_creation: dict[str, Any] | None = Field(default=None, description="Raw cache-write usage by duration")


class ChatLogResponse(BaseModel):
    """Response portion of the chat log.

    Captures the full response from the model.
    """

    content: list[dict[str, Any]] | str | None = Field(default=None, description="Response content blocks or text")
    stop_reason: str | None = Field(default=None, description="Reason for stopping generation")
    usage: UsageInfo | None = Field(default=None, description="Token usage information")
    model: str | None = Field(default=None, description="Model that generated the response")


class ScrubbingMetadata(BaseModel):
    """Metadata about the scrubbing process.

    Tracks what was redacted for audit purposes.
    """

    level: str = Field(description="Scrubbing level applied: off|basic|standard")
    redactions_count: int = Field(default=0, description="Total number of redactions made")
    pii_types_found: list[str] = Field(default_factory=list, description="Types of PII detected and redacted")
    regex_patterns_matched: list[str] = Field(default_factory=list, description="Regex patterns that matched")
    headers_scrubbed: list[str] = Field(default_factory=list, description="Headers that were scrubbed")


class ChatLog(BaseModel):
    """Complete chat log record stored in S3.

    Contains all metadata and the scrubbed request/response.
    """

    # Identifiers
    request_id: str = Field(description="Unique request identifier (UUID)")
    timestamp: datetime = Field(description="Request timestamp in ISO8601 format")

    # Context
    org_id: str = Field(description="Organization ID from auth context")
    user_id: str | None = Field(default=None, description="User ID from auth context")
    team_id: str | None = Field(default=None, description="Team ID from auth context")
    # Issue #4300: the human who set this agent chain in motion, as a canonical
    # `users.id`. Resolved server-side from the run's registry row, never from a
    # header. The budget-usage-tracker Lambda reads this to write the cumulative
    # `root_user` settled ledger row, so a chain's spend debits the initiating
    # person's budget rather than only the agent service account's.
    #
    # Empty for non-human-rooted requests, which is the common case. The Lambda
    # writes NO row when it is empty — never a $0 row keyed on "".
    root_human_id: str = Field(default="", description="Root human (users.id) who initiated this agent chain; empty if not human-rooted")
    account_type: Literal["human", "service"] = Field(description="Account type: human or service")

    # Request metadata
    model: str = Field(description="Model ID requested")
    api_format: Literal["bedrock", "anthropic", "openai"] = Field(description="API format used")
    latency_ms: float = Field(description="Total request latency in milliseconds")

    # Scrubbed content
    request: ChatLogRequest = Field(description="Scrubbed request body")
    response: ChatLogResponse = Field(description="Scrubbed response body")

    # Set only by the gateway completion path, never extracted from client JSON.
    pricing_decision: dict[str, Any] | None = Field(default=None, description="Server-produced durable pricing decision")

    # Scrubbing info
    scrubbing: ScrubbingMetadata = Field(description="Information about scrubbing applied")
