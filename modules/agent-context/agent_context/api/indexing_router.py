"""Admin indexing status API router.

Issue #1424: Knowledge-layer indexing status page (per-repo, per-stage).

Route-ownership: this router is DEFINED in agent-context, MOUNTED by the gateway
via conditional include_router behind AGENT_CONTEXT_ENABLED. The gateway provides
Cognito JWT auth + admin-role guard; this module provides the query logic and routes.

Database dependency: the gateway overrides `get_indexing_db` at mount time to inject
its own session factory (same Postgres instance, agent_context database).
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from agent_context.api.schemas import (
    IndexingSummaryStats,
    IndexRunDetailResponse,
    IndexRunListResponse,
    IndexRunStageResponse,
    IndexRunSummary,
)

logger = logging.getLogger("agent_context.api.indexing")

router = APIRouter(prefix="/admin/indexing", tags=["admin-indexing"])


async def get_indexing_db() -> AsyncSession:  # type: ignore[return]
    """Placeholder DB dependency — overridden by the gateway at mount time.

    The gateway calls `app.dependency_overrides[get_indexing_db] = get_db`
    so that this router receives the gateway's async DB session.
    """
    raise HTTPException(
        status_code=503,
        detail="Agent-context database dependency not configured",
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/runs", response_model=IndexRunListResponse)
async def list_indexing_runs(
    db: Annotated[AsyncSession, Depends(get_indexing_db)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> IndexRunListResponse:
    """List index runs with derived per-run stats.

    Level 1 of the two-level drill-down: shows one row per index run
    with aggregated stats (repos verified / failed / partial).
    """
    offset = (page - 1) * page_size

    # Get total count
    count_result = await db.execute(text("SELECT COUNT(*) FROM index_runs"))
    total = count_result.scalar() or 0

    # Get paginated runs ordered by most recent first
    runs_result = await db.execute(
        text("""
            SELECT id, repo_id, started_at, completed_at, duration_ms,
                   status, commit_sha, error
            FROM index_runs
            ORDER BY started_at DESC
            LIMIT :limit OFFSET :offset
        """),
        {"limit": page_size, "offset": offset},
    )
    runs_rows = runs_result.fetchall()

    items: list[IndexRunSummary] = []
    for row in runs_rows:
        run_id = str(row.id)

        # Get per-run stage stats
        stats = await _get_run_stats(db, run_id)

        items.append(
            IndexRunSummary(
                id=run_id,
                repo_id=str(row.repo_id),
                started_at=row.started_at,
                completed_at=row.completed_at,
                duration_ms=row.duration_ms,
                status=row.status,
                commit_sha=row.commit_sha,
                error=row.error,
                total_repos=stats["total_repos"],
                repos_verified=stats["repos_verified"],
                repos_failed=stats["repos_failed"],
                repos_partial=stats["repos_partial"],
            )
        )

    # Summary stats from latest run (if any)
    summary = None
    if items:
        latest = items[0]
        total_repos = latest.total_repos or 1  # avoid division by zero
        summary = IndexingSummaryStats(
            total_repos=latest.total_repos,
            fully_verified_pct=round((latest.repos_verified / total_repos) * 100, 1)
            if total_repos > 0
            else 0.0,
            failed_stages=await _count_failed_stages(db, latest.id),
            drift_count=await _count_drift(db, latest.id),
        )

    return IndexRunListResponse(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        has_more=(offset + page_size) < total,
        summary=summary,
    )


@router.get("/runs/{run_id}", response_model=IndexRunDetailResponse)
async def get_indexing_run_detail(
    run_id: str,
    db: Annotated[AsyncSession, Depends(get_indexing_db)],
) -> IndexRunDetailResponse:
    """Get per-repo, per-stage detail for a specific index run.

    Level 2 of the two-level drill-down: shows all stage rows for a run.
    """
    # Verify run exists
    run_result = await db.execute(
        text("""
            SELECT id, started_at, completed_at, status, commit_sha
            FROM index_runs
            WHERE id = :run_id
        """),
        {"run_id": run_id},
    )
    run_row = run_result.fetchone()

    if not run_row:
        raise HTTPException(status_code=404, detail="Index run not found")

    # Get all stages for this run
    stages_result = await db.execute(
        text("""
            SELECT id, run_id, repo, stage, status, artifact_ref,
                   verified_at, attempts, error, started_at, completed_at
            FROM index_run_stages
            WHERE run_id = :run_id
            ORDER BY repo, stage
        """),
        {"run_id": run_id},
    )
    stage_rows = stages_result.fetchall()

    stages = [
        IndexRunStageResponse(
            id=str(row.id),
            run_id=str(row.run_id),
            repo=row.repo,
            stage=row.stage,
            status=row.status,
            artifact_ref=row.artifact_ref,
            verified_at=row.verified_at,
            attempts=row.attempts,
            error=row.error,
            started_at=row.started_at,
            completed_at=row.completed_at,
        )
        for row in stage_rows
    ]

    return IndexRunDetailResponse(
        run_id=str(run_row.id),
        started_at=run_row.started_at,
        completed_at=run_row.completed_at,
        status=run_row.status,
        commit_sha=run_row.commit_sha,
        stages=stages,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _get_run_stats(db: AsyncSession, run_id: str) -> dict[str, int]:
    """Compute per-repo stats for a run from its stage rows."""
    result = await db.execute(
        text("""
            SELECT repo,
                   COUNT(*) AS total_stages,
                   COUNT(*) FILTER (WHERE status = 'verified') AS verified,
                   COUNT(*) FILTER (WHERE status = 'failed') AS failed
            FROM index_run_stages
            WHERE run_id = :run_id
            GROUP BY repo
        """),
        {"run_id": run_id},
    )
    rows = result.fetchall()

    total_repos = len(rows)
    repos_verified = 0
    repos_failed = 0
    repos_partial = 0

    for row in rows:
        if row.failed > 0:
            repos_failed += 1
        elif row.verified == row.total_stages:
            repos_verified += 1
        else:
            repos_partial += 1

    return {
        "total_repos": total_repos,
        "repos_verified": repos_verified,
        "repos_failed": repos_failed,
        "repos_partial": repos_partial,
    }


async def _count_failed_stages(db: AsyncSession, run_id: str) -> int:
    """Count total failed stages in a run."""
    result = await db.execute(
        text("""
            SELECT COUNT(*) FROM index_run_stages
            WHERE run_id = :run_id AND status = 'failed'
        """),
        {"run_id": run_id},
    )
    return result.scalar() or 0


async def _count_drift(db: AsyncSession, run_id: str) -> int:
    """Count repos with drift (verified but artifact_ref is null — indicates missing artifact)."""
    result = await db.execute(
        text("""
            SELECT COUNT(DISTINCT repo) FROM index_run_stages
            WHERE run_id = :run_id
              AND status = 'verified'
              AND artifact_ref IS NULL
        """),
        {"run_id": run_id},
    )
    return result.scalar() or 0
