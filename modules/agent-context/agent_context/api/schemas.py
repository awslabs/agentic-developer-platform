"""Pydantic response models for the indexing admin API.

Issue #1424: Knowledge-layer indexing status page (per-repo, per-stage).
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class IndexRunStageResponse(BaseModel):
    """Single stage row from index_run_stages."""

    id: str
    run_id: str
    repo: str
    stage: str
    status: str
    artifact_ref: str | None = None
    verified_at: datetime | None = None
    attempts: int = 0
    error: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None


class IndexRunSummary(BaseModel):
    """Level 1 — one row per index run in the runs table."""

    id: str
    repo_id: str
    started_at: datetime
    completed_at: datetime | None = None
    duration_ms: int | None = None
    status: str
    commit_sha: str | None = None
    error: str | None = None
    # Derived stats
    total_repos: int = 0
    repos_verified: int = 0
    repos_failed: int = 0
    repos_partial: int = 0


class IndexRunListResponse(BaseModel):
    """Paginated list of index runs."""

    items: list[IndexRunSummary]
    total: int
    page: int
    page_size: int
    has_more: bool
    # Top-level summary stats for the latest run
    summary: IndexingSummaryStats | None = None


class IndexingSummaryStats(BaseModel):
    """Top-level stats for StatCards (based on latest run)."""

    total_repos: int = 0
    fully_verified_pct: float = 0.0
    failed_stages: int = 0
    drift_count: int = 0


class IndexRunDetailResponse(BaseModel):
    """Level 2 — expand a run to see per-repo, per-stage detail."""

    run_id: str
    started_at: datetime
    completed_at: datetime | None = None
    status: str
    commit_sha: str | None = None
    stages: list[IndexRunStageResponse]
