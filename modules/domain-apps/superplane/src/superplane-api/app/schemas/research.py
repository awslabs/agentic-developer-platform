"""Pydantic schemas for external data scanner (US-G2) and research proposals (US-G3)."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Finding response schemas (US-G2)
# ---------------------------------------------------------------------------


class ResearchFindingResponse(BaseModel):
    """Single research finding returned by the API."""

    id: UUID
    workspace_id: UUID | None = None
    source: str
    source_url: str
    title: str
    summary: str | None = None
    relevance_score: int = Field(ge=0, le=100)
    tags: list[str] | None = None
    scanned_at: datetime
    created_at: datetime

    model_config = {"from_attributes": True}


class ResearchFindingDetail(ResearchFindingResponse):
    """Finding with raw content included (for detail view)."""

    raw_content_json: dict | None = None


class ResearchFindingsList(BaseModel):
    """Paginated list of research findings."""

    items: list[ResearchFindingResponse]
    total: int
    page: int
    page_size: int


# ---------------------------------------------------------------------------
# Scan request / filter schemas (US-G2)
# ---------------------------------------------------------------------------


class ScanRequest(BaseModel):
    """Request to trigger a manual scan."""

    sources: list[str] | None = Field(
        default=None,
        description=(
            "Sources to scan. If omitted, all sources are scanned. "
            "Valid: arxiv, huggingface, github, twitter, reddit, "
            "hackernews, aws_whatsnew, nvidia_blog, competitor_blog"
        ),
    )
    workspace_id: UUID | None = Field(
        default=None,
        description="Optional workspace to associate findings with.",
    )


class ScanResponse(BaseModel):
    """Response after triggering a scan."""

    status: str
    sources_scanned: list[str]
    findings_count: int
    high_relevance_count: int


class ScannerStatsResponse(BaseModel):
    """Aggregate scanner statistics."""

    total_findings: int
    high_relevance_count: int  # > 70
    medium_relevance_count: int  # 30-70
    low_relevance_count: int  # < 30
    findings_by_source: dict[str, int]
    last_scan_at: datetime | None = None


# ---------------------------------------------------------------------------
# Research Proposal schemas (US-G3)
# ---------------------------------------------------------------------------


class ExperimentStep(BaseModel):
    """A single step in an experiment plan."""

    step_number: int
    description: str
    expected_output: str | None = None


class ProposalCreateRequest(BaseModel):
    """Request to create a research proposal manually."""

    workspace_id: UUID | None = Field(
        default=None,
        description="Workspace to associate the proposal with.",
    )
    title: str = Field(
        ...,
        min_length=5,
        max_length=1024,
        description="Descriptive title for the proposal.",
    )
    objective: str = Field(
        ...,
        min_length=10,
        description="What this experiment aims to validate or achieve.",
    )
    hypothesis: str = Field(
        ...,
        min_length=10,
        description="The hypothesis being tested.",
    )
    source_findings: list[UUID] | None = Field(
        default=None,
        description="List of finding IDs that inspired this proposal.",
    )
    estimated_cost_usd: float | None = Field(
        default=None,
        ge=0,
        description="Estimated cost in USD.",
    )
    estimated_duration_hours: float | None = Field(
        default=None,
        ge=0,
        description="Estimated duration in hours.",
    )
    required_resources: str | None = Field(
        default=None,
        description='Resources needed, e.g. "1x L40S, 256GB disk".',
    )
    experiment_plan: list[ExperimentStep] | None = Field(
        default=None,
        description="Step-by-step experiment plan.",
    )


class ProposalGenerateRequest(BaseModel):
    """Request to auto-generate proposals from scanner findings."""

    workspace_id: UUID | None = Field(
        default=None,
        description="Workspace scope for finding analysis.",
    )
    min_relevance: int = Field(
        default=50,
        ge=0,
        le=100,
        description="Minimum relevance score for findings to consider.",
    )
    max_proposals: int = Field(
        default=5,
        ge=1,
        le=20,
        description="Maximum number of proposals to generate.",
    )


class ProposalApproveRequest(BaseModel):
    """Request to approve a research proposal."""

    approved_by: str = Field(
        ...,
        min_length=1,
        description="User ID or username of the approver.",
    )


class ProposalRejectRequest(BaseModel):
    """Request to reject a research proposal."""

    rejected_by: str = Field(
        ...,
        min_length=1,
        description="User ID or username of the rejector.",
    )
    reason: str | None = Field(
        default=None,
        description="Reason for rejection.",
    )


class ResearchProposalResponse(BaseModel):
    """Single research proposal returned by the API."""

    id: UUID
    workspace_id: UUID | None = None
    title: str
    objective: str
    hypothesis: str
    source_findings: list[str] | None = None
    estimated_cost_usd: float | None = None
    estimated_duration_hours: float | None = None
    required_resources: str | None = None
    experiment_plan: list[dict] | None = None
    status: str
    approved_by: str | None = None
    approved_at: datetime | None = None
    rejected_reason: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ResearchProposalsList(BaseModel):
    """Paginated list of research proposals."""

    items: list[ResearchProposalResponse]
    total: int
    page: int
    page_size: int


class ProposalGenerateResponse(BaseModel):
    """Response after auto-generating proposals."""

    status: str
    proposals_generated: int
    themes_identified: int
    findings_analyzed: int


class ProposalStatsResponse(BaseModel):
    """Aggregate proposal statistics."""

    total_proposals: int
    proposed_count: int
    approved_count: int
    rejected_count: int
    in_progress_count: int
    completed_count: int
    failed_count: int
    total_estimated_cost_usd: float | None = None
