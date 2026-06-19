"""Activity module Pydantic schemas for agent invocation API request/response.

Contract rules (baked into this module — Phase 3 client must follow):

1. **Graceful missing-GSI -> empty, never 500.**
   If the `user-index` or `tenant-index` GSI does not yet exist (deploy-order
   gap), the service layer catches the DynamoDB ValidationException /
   ResourceNotFoundException and returns `{items: [], last_key: null, count: 0}`.
   A fresh deploy shows "no activity yet" — correct behavior.

2. **Filtered pages may be short/empty with a non-null `last_key`.**
   `status`/`channel`/`persona` are DynamoDB FilterExpressions that run *after*
   the page_size read. A page can return fewer than page_size items — or zero —
   while more matches exist. The client MUST keep following `last_key` until it
   is null; do not stop on a short/empty page.

Phase 6 additions (issue #1461):
- `trigger_kind` (human|agent|bot) — derived from provenance is_human_rooted +
  parent_invocation_id presence.
- `triggered_by_invocation_id` / `triggered_by_topic` — parent run link.
- `root_human_id` / `is_human_rooted` — chain root info.
- `InvocationChainResponse` — tree/flat chain for the chain view.
"""

from typing import Literal

from pydantic import BaseModel, Field

# Trigger kind: human (user-initiated), agent (spawned by another run), bot (cron/automated, not human-rooted)
TriggerKind = Literal["human", "agent", "bot"]


class InvocationItem(BaseModel):
    """A single agent invocation record."""

    invocation_id: str
    invoked_at: str
    channel: str | None = None
    status: str | None = None
    status_updated_at: str | None = None
    topic: str | None = None
    persona: str | None = None
    summary: str | None = None
    source_url: str | None = None
    repo: str | None = None
    issue_number: int | None = None
    correlation_id: str | None = None
    run_id: str | None = None

    # Phase 6 lineage fields (#1461)
    trigger_kind: TriggerKind = Field(
        default="human",
        description="How this invocation was triggered: human (user request), agent (spawned by another run), bot (cron/automated).",
    )
    triggered_by_invocation_id: str | None = Field(
        default=None,
        description="The parent invocation ID that triggered this run (null for root/human-initiated).",
    )
    triggered_by_topic: str | None = Field(
        default=None,
        description="Topic of the parent invocation (for display: 'triggered by {topic}').",
    )
    root_human_id: str | None = Field(
        default=None,
        description="The originating human user who started this chain.",
    )
    is_human_rooted: bool = Field(
        default=True,
        description="Whether this chain traces back to a human request (false = bot/cron-initiated).",
    )

    # Issue #1616: Per-run cost fields (enriched from Postgres usage_logs)
    total_cost_usd: float | None = Field(
        default=None,
        description="Total cost in USD for this run's Bedrock calls. Null if not metered (non-gateway mode) or cost not yet backfilled.",
    )
    total_tokens: int | None = Field(
        default=None,
        description="Total tokens (input + output) for this run's Bedrock calls.",
    )
    call_count: int | None = Field(
        default=None,
        description="Number of Bedrock API calls made during this run.",
    )


class InvocationChainItem(BaseModel):
    """A node in the invocation chain tree."""

    invocation_id: str
    invoked_at: str
    channel: str | None = None
    status: str | None = None
    topic: str | None = None
    persona: str | None = None
    parent_invocation_id: str | None = None
    children: list["InvocationChainItem"] = Field(default_factory=list)


class InvocationChainResponse(BaseModel):
    """Chain view: all invocations sharing a correlation_id, rendered as a tree.

    Falls back to flat date-ordered list when parent edges are null (pre-feature rows).
    """

    correlation_id: str
    root_human_id: str | None = None
    is_human_rooted: bool = True
    items: list[InvocationChainItem] = Field(
        description="Tree of invocations. Root nodes have parent_invocation_id=null; children nested.",
    )
    total_count: int = Field(description="Total invocations in this chain (may be capped at depth limit).")
    depth_capped: bool = Field(
        default=False,
        description="True if the chain exceeded the depth cap and was truncated.",
    )


class InvocationListResponse(BaseModel):
    """Paginated list of agent invocations.

    Note on pagination: `last_key` being non-null does NOT mean the current page
    is "full." Because DynamoDB FilterExpressions are applied after reading
    `page_size` items, a page may be short or empty while `last_key` is still
    set. Clients MUST follow `last_key` until it is null.
    """

    items: list[InvocationItem]
    count: int = Field(description="Number of items in this page (may be < page_size due to filters).")
    last_key: str | None = Field(
        default=None,
        description=(
            "Opaque cursor (base64-encoded DynamoDB LastEvaluatedKey). "
            "Null means no more pages. Non-null with zero items means "
            "more pages exist — keep paginating."
        ),
    )
