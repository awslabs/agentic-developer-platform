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

    # Issue #1653: Rich detail fields
    error_message: str | None = Field(
        default=None,
        description="Error message for failed invocations. Written to DDB by webhook-ingress.",
    )
    completed_at: str | None = Field(
        default=None,
        description="ISO 8601 completion timestamp (= status_updated_at when status is terminal). Null for in-progress runs.",
    )
    run_log_url: str | None = Field(
        default=None,
        description="URL to the agent run log (GitHub check-run link). Null until Tier 2 worker persists it.",
    )

    # Issue #3069: S3 transcript key (set by worker write-back after S3 upload)
    transcript_key: str | None = Field(
        default=None,
        description="S3 object key for the full run transcript. Null for runs before #3061 or if upload failed.",
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

    # Issue #3069: S3 transcript key
    transcript_key: str | None = None

    # Issue #1653: Per-node cost (enriched from Postgres usage_logs)
    total_cost_usd: float | None = None
    total_tokens: int | None = None
    call_count: int | None = None


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

    # Issue #1653: Chain-wide cost totals
    chain_total_cost_usd: float | None = Field(
        default=None,
        description="Sum of cost across all nodes in the chain.",
    )
    chain_total_tokens: int | None = Field(
        default=None,
        description="Sum of tokens across all nodes in the chain.",
    )
    chain_total_call_count: int | None = Field(
        default=None,
        description="Sum of Bedrock calls across all nodes in the chain.",
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


# ---------------------------------------------------------------------------
# Issue #1662: Chain-grouped view schemas
# ---------------------------------------------------------------------------


class ChainSummary(BaseModel):
    """One chain = root run + optional descendants, with chain-level aggregates.

    Issue #1662: Used in the chain-grouped board view. The root is the
    human-initiated invocation (the issue that started the chain); descendants
    are the agent runs it spawned.
    """

    chain_id: str = Field(description="The correlation_id grouping this chain.")
    root: InvocationItem = Field(description="The root invocation (the human-triggered run that started the chain).")
    descendant_count: int = Field(description="Number of other runs in the chain (0 = singleton).")
    descendants: list[InvocationChainItem] = Field(
        default_factory=list,
        description="Chain members other than the root (time-ordered). Empty for singletons.",
    )
    # Chain-level cost aggregates
    chain_total_cost_usd: float | None = Field(
        default=None,
        description="Sum of cost across all runs in the chain (root + descendants).",
    )
    chain_total_tokens: int | None = Field(
        default=None,
        description="Sum of tokens across all runs in the chain.",
    )
    chain_total_call_count: int | None = Field(
        default=None,
        description="Sum of Bedrock calls across all runs in the chain.",
    )


class ChainListResponse(BaseModel):
    """Paginated list of chains for the chain-grouped board view.

    Issue #1662: Pagination is over chains (by root arrived_at desc), not
    individual runs. Each chain contains the root + its descendants inline
    (eager loading — data already fetched for cost totals).
    """

    chains: list[ChainSummary] = Field(description="Page of chains, newest-root-first.")
    count: int = Field(description="Number of chains in this page.")
    last_key: str | None = Field(
        default=None,
        description=("Opaque cursor for the next page of chains. Null means no more pages."),
    )
