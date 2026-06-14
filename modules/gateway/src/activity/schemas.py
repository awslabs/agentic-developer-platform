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
"""

from pydantic import BaseModel, Field


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
    error_message: str | None = None


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
