"""Pydantic request/response models for the knowledge-assets registry CRUD API.

Issue #2045: Relocated from agent_context.api.assets_schemas into the gateway.
Original: Issue #1791 (Story B of E10 #1736).
Contracts per design docs/agent-context/design-1736-knowledge-asset-registry.md S8.7.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, model_validator

# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------


class AssetCreateRequest(BaseModel):
    """POST /api/agent-context/assets — register one asset."""

    asset_type: str = Field(..., max_length=32, description="e.g. repo, url, doc")
    source_ref: str = Field(..., max_length=2048, description="Git URL / web URL / S3 path")
    display_name: str | None = Field(None, max_length=512)
    tags: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    scope: str = Field("personal", pattern=r"^(personal|tenant|product)$")
    product_id: str | None = Field(None, min_length=1, max_length=36, pattern=r"^[A-Za-z0-9-]+$")
    source_binding_id: str | None = Field(None, min_length=1, max_length=36, pattern=r"^[A-Za-z0-9-]+$")
    keep_updated: bool = False
    source_upload_id: str | None = Field(None, min_length=36, max_length=36)

    @model_validator(mode="after")
    def validate_product_scope(self):
        if (self.scope == "product") != bool(self.product_id):
            raise ValueError("product_id is required only for Product scope")
        if self.source_binding_id and (self.scope != "product" or self.asset_type != "repo"):
            raise ValueError("A saved source binding requires a Product repository")
        if self.keep_updated and not self.source_binding_id:
            raise ValueError("Automatic refresh requires a saved Product source")
        if self.source_upload_id and (self.scope != "product" or self.asset_type != "doc"):
            raise ValueError("A document upload requires Product scope")
        return self


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class AssetResponse(BaseModel):
    """Single asset in create/list/detail responses."""

    id: str
    asset_type: str
    source_ref: str
    display_name: str | None = None
    tags: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    tenant_id: str | None = None
    owner_sub: str | None = None
    project_id: str | None = None
    product_id: str | None = None
    can_manage: bool = False
    can_download: bool = False
    status: str
    last_error: str | None = None
    retry_count: int = 0
    registered_by: str | None = None
    created_at: datetime
    updated_at: datetime | None = None


class QuotaDetail(BaseModel):
    """Per-type quota usage."""

    used: int
    limit: int


class QuotaInfo(BaseModel):
    """Aggregated quota info returned in list responses."""

    repos: QuotaDetail | None = None
    urls: QuotaDetail | None = None
    docs: QuotaDetail | None = None


class AssetListResponse(BaseModel):
    """GET /api/agent-context/assets — paginated list."""

    items: list[AssetResponse]
    total: int
    page: int
    page_size: int
    has_more: bool
    quota: QuotaInfo | None = None


class AssetDetailResponse(AssetResponse):
    """GET /api/agent-context/assets/{id} — full detail (extends AssetResponse)."""

    pass


# ---------------------------------------------------------------------------
# Bulk upload schemas (Story C — S5, S8.3, S8.7)
# ---------------------------------------------------------------------------


class BulkPreviewItem(BaseModel):
    """A single valid item in the bulk preview response."""

    line: int
    source_ref: str
    asset_type: str
    display_name: str | None = None
    tags: dict[str, Any] = Field(default_factory=dict)


class BulkRejectedItem(BaseModel):
    """A rejected line in the bulk preview response."""

    line: int
    source_ref: str
    reason: str


class BulkDuplicateItem(BaseModel):
    """A duplicate item found during bulk preview."""

    line: int
    source_ref: str
    existing_id: str


class BulkPreviewResponse(BaseModel):
    """POST /api/agent-context/assets/bulk — preview response (no DB writes)."""

    total_lines: int
    parsed: int
    skipped_comments: int
    valid: list[BulkPreviewItem]
    rejected: list[BulkRejectedItem]
    duplicates: list[BulkDuplicateItem]
    quota_ok: bool
    quota_after: dict[str, QuotaDetail] = Field(default_factory=dict)


class BulkCommitItem(BaseModel):
    """A single item in the bulk commit request."""

    source_ref: str = Field(..., max_length=2048)
    asset_type: str = Field(..., max_length=32)
    display_name: str | None = Field(None, max_length=512)
    tags: dict[str, Any] = Field(default_factory=dict)


class BulkCommitRequest(BaseModel):
    """POST /api/agent-context/assets/bulk/commit — commit request."""

    items: list[BulkCommitItem]
    scope: str = Field("tenant", pattern=r"^(personal|tenant|product)$")
    product_id: str | None = Field(None, min_length=1, max_length=36, pattern=r"^[A-Za-z0-9-]+$")

    @model_validator(mode="after")
    def validate_product_scope(self):
        if (self.scope == "product") != bool(self.product_id):
            raise ValueError("product_id is required only for Product scope")
        return self


class BulkCommitResponse(BaseModel):
    """POST /api/agent-context/assets/bulk/commit — commit response."""

    created: int
    skipped_duplicates: int
    assets: list[AssetResponse]


# ---------------------------------------------------------------------------
# Asset status schema — per-stage detail (#2213 follow-up)
# ---------------------------------------------------------------------------
#
# Post-#2188 the knowledge_assets registry and the index_run_stages table BOTH
# live in the agent_context DB, so the status endpoint can join them (the
# earlier "cross-DB join forbidden" #2048 caveat no longer applies). The
# frontend (AssetStatusChips) has always expected the rich shape below; this
# restores the endpoint to populate it.


class AssetIndexStage(BaseModel):
    """One stage of an index run (clone, cgc_structural, deepwiki, ...)."""

    stage: str
    status: str
    artifact_ref: str | None = None
    error: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    metrics: dict[str, Any] | None = None
    worker_pod: str | None = None


class AssetStatusResponse(BaseModel):
    """GET /api/agent-context/assets/{id}/status — registry row + per-stage detail.

    ``repo_found`` is True once an index run exists for the asset (the frontend
    shows "Not yet indexed" when False). ``stages`` lists each pipeline stage
    with its status/artifact/error so the UI can render per-tool progress.
    """

    asset_id: str
    source_ref: str
    status: str
    status_detail: dict[str, Any] | None = None
    repo_found: bool = False
    run_id: str | None = None
    run_status: str | None = None
    run_started_at: str | None = None
    stages: list[AssetIndexStage] = Field(default_factory=list)
