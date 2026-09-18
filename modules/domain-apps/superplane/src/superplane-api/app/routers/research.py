"""Research findings + proposals API endpoints (US-G2, US-G3).

Provides endpoints to:
- List and filter research findings
- Get finding details
- Trigger manual scans
- View scanner statistics
- Create, list, and manage research proposals
- Auto-generate proposals from findings
- Approve or reject proposals (human-in-the-loop)
"""

import logging
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.models.research_finding import VALID_SOURCES, ResearchFinding
from app.models.research_proposal import VALID_STATUSES, ResearchProposal
from app.models.workspace import Workspace
from app.schemas.research import (
    ProposalApproveRequest,
    ProposalCreateRequest,
    ProposalGenerateRequest,
    ProposalGenerateResponse,
    ProposalRejectRequest,
    ProposalStatsResponse,
    ResearchFindingDetail,
    ResearchFindingResponse,
    ResearchFindingsList,
    ResearchProposalResponse,
    ResearchProposalsList,
    ScannerStatsResponse,
    ScanRequest,
    ScanResponse,
)
from app.services.analysis import generate_proposals, validate_status_transition
from app.services.scanner import get_scanner_stats, run_scan

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/research", tags=["research"])


def _recorded_actor(http_request: Request, body_value: str) -> str:
    """The identity to record for an approval or rejection.

    Issue #5055 (U14). Returns the VERIFIED principal's subject when domain
    authorization is enforcing — the guard has already admitted the caller and
    put it on ``request.state.caller``, and that subject comes entirely from
    signature-verified token claims.

    The body value is used only when enforcement is off, where there is no
    verified caller to name and the legacy self-signed token carries no user
    identity at all. That is a legacy-compatibility fallback for an unenforcing
    deployment, NOT an authorization decision: it records who *claimed* to act,
    and the route is unreachable without organization authority once enforcement
    is on. Retiring that path is U21's conditional story.
    """
    caller = getattr(http_request.state, "caller", None)
    if caller is not None:
        return caller.principal.subject
    return body_value


def _tenant_org_id(http_request: Request) -> UUID | None:
    """Return the verified tenant when strict domain auth is enforcing.

    The global domain guard publishes only a signature-verified caller.  The
    legacy unenforced mode deliberately keeps its existing behaviour until U21;
    strict mode must never infer tenant ownership from a query or request body.
    """
    caller = getattr(http_request.state, "caller", None)
    if caller is None:
        return None
    return UUID(caller.principal.org_id)


def _scope_to_tenant(query, model, org_id: UUID | None):
    """Inner-join research rows to their server-held workspace tenant.

    An inner join intentionally excludes null, dangling, and otherwise unowned
    legacy rows.  Those rows cannot be exposed merely because strict auth has
    now been enabled.
    """
    if org_id is None:
        return query
    return query.join(Workspace, Workspace.id == model.workspace_id).where(
        Workspace.org_id == org_id
    )


async def _require_owned_workspace(
    session: AsyncSession, org_id: UUID | None, workspace_id: UUID | None
) -> None:
    """Fail closed when a strict-mode write lacks tenant-owned workspace state."""
    if org_id is None:
        return
    if workspace_id is None:
        raise HTTPException(status_code=403, detail="workspace is not authorized")
    owned = await session.scalar(
        select(Workspace.id).where(
            Workspace.id == workspace_id,
            Workspace.org_id == org_id,
        )
    )
    if owned is None:
        raise HTTPException(status_code=403, detail="workspace is not authorized")


@router.get("/findings", response_model=ResearchFindingsList)
async def list_findings(
    http_request: Request,
    source: str | None = Query(None, description="Filter by source"),
    min_relevance: int = Query(0, ge=0, le=100, description="Minimum relevance score"),
    max_relevance: int = Query(
        100, ge=0, le=100, description="Maximum relevance score"
    ),
    tag: str | None = Query(None, description="Filter by tag"),
    workspace_id: UUID | None = Query(None, description="Filter by workspace"),
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(20, ge=1, le=100, description="Items per page"),
    session: AsyncSession = Depends(get_session),
) -> ResearchFindingsList:
    """List research findings with filtering and pagination.

    By default, low-relevance findings (<30) are excluded unless
    explicitly requested via min_relevance=0.
    """
    org_id = _tenant_org_id(http_request)
    query = _scope_to_tenant(select(ResearchFinding), ResearchFinding, org_id)

    # Apply filters
    if source:
        if source not in VALID_SOURCES:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid source. Valid: {', '.join(VALID_SOURCES)}",
            )
        query = query.where(ResearchFinding.source == source)

    query = query.where(
        ResearchFinding.relevance_score >= min_relevance,
        ResearchFinding.relevance_score <= max_relevance,
    )

    if workspace_id:
        query = query.where(ResearchFinding.workspace_id == workspace_id)

    if tag:
        # JSONB contains operator for tag filtering
        query = query.where(ResearchFinding.tags.contains([tag]))

    # Count total matching
    count_query = select(func.count()).select_from(query.subquery())
    total_result = await session.execute(count_query)
    total = total_result.scalar() or 0

    # Apply pagination and ordering
    query = (
        query.order_by(ResearchFinding.scanned_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )

    result = await session.execute(query)
    findings = result.scalars().all()

    return ResearchFindingsList(
        items=[ResearchFindingResponse.model_validate(f) for f in findings],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/findings/{finding_id}", response_model=ResearchFindingDetail)
async def get_finding(
    finding_id: UUID,
    http_request: Request,
    session: AsyncSession = Depends(get_session),
) -> ResearchFindingDetail:
    """Get a single research finding with full details including raw content."""
    query = _scope_to_tenant(
        select(ResearchFinding), ResearchFinding, _tenant_org_id(http_request)
    ).where(ResearchFinding.id == finding_id)
    result = await session.execute(query)
    finding = result.scalar_one_or_none()

    if not finding:
        raise HTTPException(status_code=404, detail="Finding not found")

    return ResearchFindingDetail.model_validate(finding)


@router.post("/scan", response_model=ScanResponse)
async def trigger_scan(
    request: ScanRequest,
    http_request: Request,
    session: AsyncSession = Depends(get_session),
) -> ScanResponse:
    """Trigger a manual scan of external data sources.

    If sources are not specified, all sources are scanned.
    This endpoint is also called by the scheduled cron/CloudWatch trigger.
    """
    await _require_owned_workspace(
        session, _tenant_org_id(http_request), request.workspace_id
    )

    # Validate sources if provided
    if request.sources:
        invalid = [s for s in request.sources if s not in VALID_SOURCES]
        if invalid:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid sources: {', '.join(invalid)}. Valid: {', '.join(VALID_SOURCES)}",
            )

    logger.info(
        "Manual scan triggered for sources: %s",
        request.sources or "all",
    )

    result = await run_scan(
        session=session,
        sources=request.sources,
        workspace_id=request.workspace_id,
    )

    return ScanResponse(
        status=result["status"],
        sources_scanned=result["sources_scanned"],
        findings_count=result["findings_count"],
        high_relevance_count=result["high_relevance_count"],
    )


@router.get("/stats", response_model=ScannerStatsResponse)
async def scanner_stats(
    http_request: Request,
    session: AsyncSession = Depends(get_session),
) -> ScannerStatsResponse:
    """Get aggregate statistics about scanner findings."""
    stats = await get_scanner_stats(session, _tenant_org_id(http_request))
    return ScannerStatsResponse(**stats)


@router.get("/sources")
async def list_sources() -> dict:
    """List all available scanner sources and their configurations."""
    from app.services.scanner_sources import ALL_SOURCES

    return {
        "sources": [
            {
                "name": cfg.name,
                "display_name": cfg.display_name,
                "frequency": cfg.frequency,
                "categories": cfg.categories,
            }
            for cfg in ALL_SOURCES.values()
        ]
    }


# ---------------------------------------------------------------------------
# Research Proposal endpoints (US-G3)
# ---------------------------------------------------------------------------


@router.get("/proposals", response_model=ResearchProposalsList)
async def list_proposals(
    http_request: Request,
    status: str | None = Query(None, description="Filter by status"),
    workspace_id: UUID | None = Query(None, description="Filter by workspace"),
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(20, ge=1, le=100, description="Items per page"),
    session: AsyncSession = Depends(get_session),
) -> ResearchProposalsList:
    """List research proposals with filtering and pagination."""
    org_id = _tenant_org_id(http_request)
    query = _scope_to_tenant(select(ResearchProposal), ResearchProposal, org_id)

    if status:
        if status not in VALID_STATUSES:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid status. Valid: {', '.join(VALID_STATUSES)}",
            )
        query = query.where(ResearchProposal.status == status)

    if workspace_id:
        query = query.where(ResearchProposal.workspace_id == workspace_id)

    # Count total matching
    count_query = select(func.count()).select_from(query.subquery())
    total_result = await session.execute(count_query)
    total = total_result.scalar() or 0

    # Apply pagination and ordering
    query = (
        query.order_by(ResearchProposal.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )

    result = await session.execute(query)
    proposals = result.scalars().all()

    return ResearchProposalsList(
        items=[ResearchProposalResponse.model_validate(p) for p in proposals],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/proposals/stats", response_model=ProposalStatsResponse)
async def proposal_stats(
    http_request: Request,
    session: AsyncSession = Depends(get_session),
) -> ProposalStatsResponse:
    """Get aggregate statistics about research proposals."""
    org_id = _tenant_org_id(http_request)
    total_q = await session.execute(
        _scope_to_tenant(
            select(func.count(ResearchProposal.id)), ResearchProposal, org_id
        )
    )
    total = total_q.scalar() or 0

    # Count by status
    status_counts = {}
    for status_name in VALID_STATUSES:
        q = await session.execute(
            _scope_to_tenant(
                select(func.count(ResearchProposal.id)), ResearchProposal, org_id
            ).where(ResearchProposal.status == status_name)
        )
        status_counts[status_name] = q.scalar() or 0

    # Total estimated cost for approved + in_progress proposals
    cost_q = await session.execute(
        _scope_to_tenant(
            select(func.sum(ResearchProposal.estimated_cost_usd)),
            ResearchProposal,
            org_id,
        ).where(ResearchProposal.status.in_(["approved", "in_progress"]))
    )
    total_cost = cost_q.scalar()

    return ProposalStatsResponse(
        total_proposals=total,
        proposed_count=status_counts.get("proposed", 0),
        approved_count=status_counts.get("approved", 0),
        rejected_count=status_counts.get("rejected", 0),
        in_progress_count=status_counts.get("in_progress", 0),
        completed_count=status_counts.get("completed", 0),
        failed_count=status_counts.get("failed", 0),
        total_estimated_cost_usd=float(total_cost) if total_cost else None,
    )


@router.get("/proposals/{proposal_id}", response_model=ResearchProposalResponse)
async def get_proposal(
    proposal_id: UUID,
    http_request: Request,
    session: AsyncSession = Depends(get_session),
) -> ResearchProposalResponse:
    """Get a single research proposal with full details."""
    query = _scope_to_tenant(
        select(ResearchProposal), ResearchProposal, _tenant_org_id(http_request)
    ).where(ResearchProposal.id == proposal_id)
    result = await session.execute(query)
    proposal = result.scalar_one_or_none()

    if not proposal:
        raise HTTPException(status_code=404, detail="Proposal not found")

    return ResearchProposalResponse.model_validate(proposal)


@router.post("/proposals", response_model=ResearchProposalResponse, status_code=201)
async def create_proposal(
    request: ProposalCreateRequest,
    http_request: Request,
    session: AsyncSession = Depends(get_session),
) -> ResearchProposalResponse:
    """Create a research proposal manually.

    Proposals start in 'proposed' status and require human approval
    before experiments begin.
    """
    import uuid

    await _require_owned_workspace(
        session, _tenant_org_id(http_request), request.workspace_id
    )

    # Convert source_findings UUIDs to strings for JSONB storage
    source_finding_ids = (
        [str(f) for f in request.source_findings] if request.source_findings else None
    )

    # Convert experiment plan to dicts for JSONB storage
    experiment_plan = (
        [step.model_dump() for step in request.experiment_plan]
        if request.experiment_plan
        else None
    )

    proposal = ResearchProposal(
        id=uuid.uuid4(),
        workspace_id=request.workspace_id,
        title=request.title,
        objective=request.objective,
        hypothesis=request.hypothesis,
        source_findings=source_finding_ids,
        estimated_cost_usd=request.estimated_cost_usd,
        estimated_duration_hours=request.estimated_duration_hours,
        required_resources=request.required_resources,
        experiment_plan=experiment_plan,
        status="proposed",
    )
    session.add(proposal)
    await session.commit()
    await session.refresh(proposal)

    logger.info("Created proposal: %s (%s)", proposal.title, proposal.id)

    return ResearchProposalResponse.model_validate(proposal)


@router.post("/proposals/generate", response_model=ProposalGenerateResponse)
async def generate_proposals_endpoint(
    request: ProposalGenerateRequest,
    http_request: Request,
    session: AsyncSession = Depends(get_session),
) -> ProposalGenerateResponse:
    """Auto-generate research proposals from scanner findings.

    The analysis agent reviews findings with relevance_score >= min_relevance,
    groups them into themes, and generates actionable proposals with cost
    estimates and experiment plans.
    """
    await _require_owned_workspace(
        session, _tenant_org_id(http_request), request.workspace_id
    )

    logger.info(
        "Generating proposals: min_relevance=%d, max_proposals=%d, workspace=%s",
        request.min_relevance,
        request.max_proposals,
        request.workspace_id,
    )

    result = await generate_proposals(
        session=session,
        workspace_id=request.workspace_id,
        min_relevance=request.min_relevance,
        max_proposals=request.max_proposals,
    )

    return ProposalGenerateResponse(
        status=result["status"],
        proposals_generated=result["proposals_generated"],
        themes_identified=result["themes_identified"],
        findings_analyzed=result["findings_analyzed"],
    )


@router.patch(
    "/proposals/{proposal_id}/approve",
    response_model=ResearchProposalResponse,
)
async def approve_proposal(
    proposal_id: UUID,
    request: ProposalApproveRequest,
    http_request: Request,
    session: AsyncSession = Depends(get_session),
) -> ResearchProposalResponse:
    """Approve a research proposal for execution.

    Only proposals in 'proposed' status can be approved.
    Approved proposals move to the experiment queue (US-G4).

    Issue #5055 (U14): the recorded approver is the VERIFIED principal, not the
    ``approved_by`` field in the request body. Previously this wrote the body
    value straight to ``proposal.approved_by``, so the audit record said whatever
    the caller typed — on a route that also had no authentication at all. Body
    fields never carry authority; see ``_recorded_actor``.
    """
    query = _scope_to_tenant(
        select(ResearchProposal), ResearchProposal, _tenant_org_id(http_request)
    ).where(ResearchProposal.id == proposal_id)
    result = await session.execute(query)
    proposal = result.scalar_one_or_none()

    if not proposal:
        raise HTTPException(status_code=404, detail="Proposal not found")

    if not validate_status_transition(proposal.status, "approved"):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot approve proposal in '{proposal.status}' status. "
            f"Only proposals in 'proposed' status can be approved.",
        )

    approver = _recorded_actor(http_request, request.approved_by)

    proposal.status = "approved"
    proposal.approved_by = approver
    proposal.approved_at = datetime.now(timezone.utc)
    proposal.updated_at = datetime.now(timezone.utc)

    await session.commit()
    await session.refresh(proposal)

    logger.info(
        "Proposal approved: %s (%s) by %s",
        proposal.title,
        proposal.id,
        approver,
    )

    return ResearchProposalResponse.model_validate(proposal)


@router.patch(
    "/proposals/{proposal_id}/reject",
    response_model=ResearchProposalResponse,
)
async def reject_proposal(
    proposal_id: UUID,
    request: ProposalRejectRequest,
    http_request: Request,
    session: AsyncSession = Depends(get_session),
) -> ResearchProposalResponse:
    """Reject a research proposal.

    Only proposals in 'proposed' or 'approved' status can be rejected.

    Issue #5055 (U14): the logged rejector is the verified principal rather than
    the body's ``rejected_by``, for the same reason as approval — a body field
    is a claim, not an identity.
    """
    query = _scope_to_tenant(
        select(ResearchProposal), ResearchProposal, _tenant_org_id(http_request)
    ).where(ResearchProposal.id == proposal_id)
    result = await session.execute(query)
    proposal = result.scalar_one_or_none()

    if not proposal:
        raise HTTPException(status_code=404, detail="Proposal not found")

    if not validate_status_transition(proposal.status, "rejected"):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot reject proposal in '{proposal.status}' status.",
        )

    proposal.status = "rejected"
    proposal.rejected_reason = request.reason
    proposal.updated_at = datetime.now(timezone.utc)

    await session.commit()
    await session.refresh(proposal)

    logger.info(
        "Proposal rejected: %s (%s) by %s — %s",
        proposal.title,
        proposal.id,
        _recorded_actor(http_request, request.rejected_by),
        request.reason or "no reason",
    )

    return ResearchProposalResponse.model_validate(proposal)
