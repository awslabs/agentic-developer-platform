"""Pydantic request/response models for the knowledge-assets registry CRUD API.

Issue #1791 (Story B of E10 #1736): Register/list/detail/delete/reindex endpoints.
Contracts per design docs/agent-context/design-1736-knowledge-asset-registry.md S8.7.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


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
    scope: str = Field("personal", pattern=r"^(personal|tenant)$")


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
