"""Research findings + proposals API endpoints (US-G2, US-G3).

Provides endpoints to:
- List and filter research findings
- Get finding details
- Trigger manual scans
- View scanner statistics
- Create, list, and manage research proposals
- Auto-generate proposals from findings
- Approve or reject proposals (human-in-the-loop)

AUTHENTICATION (issue #5682, A02)
---------------------------------
Every route here reads or writes tenant data, so every route depends on
``get_current_org`` and is therefore unreachable without a credential. That
dependency is the load-bearing change: this router was the ONLY group of
tenant-data routes wired to neither of the service's two identity mechanisms.
The strict global guard in ``app/domain_guard.py`` closes these routes when
``domain_auth_enforced`` is on, but that setting is off by default — and with it
off the guard returns early, no other gate existed here, and all twelve routes
answered anonymous callers with every organization's rows merged together.
Verified against the shipping default before the fix.

``get_current_org`` resolves the tenant from the guard's verified caller when
strict mode is enforcing and from the legacy org-scoped token otherwise, so this
router needs no mode-specific branch: in both modes the organization is
server-derived, and in neither is it absent. The tenant scoping helpers below
consequently take a required ``UUID`` rather than an optional one, so there is no
argument value that turns the filter off.
"""

import logging
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.middleware.auth import get_current_org, get_current_user_context
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


def _recorded_actor(http_request: Request, user_context: dict) -> str:
    """The identity to record for an approval or rejection.

    Issue #5682 (A02). NEVER the request body. Approving a proposal commits the
    organization to work, so ``approved_by`` is the audit trail for that
    commitment — and a body field is a claim the caller typed, not an identity.
    Taking it meant the record could name anyone, which makes the audit trail
    worse than absent: it reads as attribution while being caller-authored.

    Three sources, in descending strength, and every one of them is
    server-derived:

    1. The VERIFIED principal's subject, when strict domain authorization is
       enforcing. The guard has already admitted the caller and published it on
       ``request.state.caller``; that subject comes entirely from
       signature-verified token claims.
    2. The legacy org-scoped token's ``user_id`` claim, when it carries one. Still
       a signed claim this server minted, so it is attribution rather than
       assertion.
    3. Failing both, the authenticated organization itself, recorded as
       ``org:<uuid>``. Deliberately NOT the body value: the honest record is
       "some holder of this organization's credential", which is exactly what the
       legacy token proves and no more. An org-scoped credential carries no user
       identity, and inventing one from the body is the defect this closes.

    Unauthenticated callers never reach here — every route that calls this depends
    on :func:`get_current_org`, which refuses a request with no credential.
    """
    caller = getattr(http_request.state, "caller", None)
    if caller is not None:
        return caller.principal.subject
    user_id = user_context.get("user_id")
    if user_id is not None:
        return str(user_id)
    return f"org:{user_context['org_id']}"


def _scope_to_tenant(query, model, org_id: UUID):
    """Inner-join research rows to their server-held workspace tenant.

    An inner join intentionally excludes null, dangling, and otherwise unowned
    legacy rows.  Those rows cannot be exposed merely because strict auth has
    now been enabled.

    Issue #5682 (A02): ``org_id`` is now non-optional. It previously accepted
    ``None`` and returned the query UNFILTERED, which is how the unenforced
    default served every organization's rows to any caller — the filter read as
    present at every call site while matching nothing. The tenant is now always a
    verified organization resolved by :func:`get_current_org`, so there is no
    value of this argument that disables the join.
    """
    return query.join(Workspace, Workspace.id == model.workspace_id).where(
        Workspace.org_id == org_id
    )


async def _require_owned_workspace(
    session: AsyncSession, org_id: UUID, workspace_id: UUID | None
) -> None:
    """Refuse a write whose target workspace this tenant does not own.

    Issue #5682 (A02): no ``org_id is None`` early return any more. That branch
    made every write unscoped in the default configuration, so a caller could
    name any workspace in any organization and have the row created against it.
    """
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
    org_id: UUID = Depends(get_current_org),
    session: AsyncSession = Depends(get_session),
) -> ResearchFindingsList:
    """List research findings with filtering and pagination.

    By default, low-relevance findings (<30) are excluded unless
    explicitly requested via min_relevance=0.

    Scoped to the caller's organization (issue #5682, A02). The ``workspace_id``
    query filter below NARROWS within that organization; it can never widen past
    it, because the tenant join is applied first and is not caller-supplied.
    """
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
    org_id: UUID = Depends(get_current_org),
    session: AsyncSession = Depends(get_session),
) -> ResearchFindingDetail:
    """Get a single research finding with full details including raw content.

    A finding belonging to another organization answers 404, not 403: the
    response must not confirm that an id exists in a tenant the caller cannot
    read.
    """
    query = _scope_to_tenant(select(ResearchFinding), ResearchFinding, org_id).where(
        ResearchFinding.id == finding_id
    )
    result = await session.execute(query)
    finding = result.scalar_one_or_none()

    if not finding:
        raise HTTPException(status_code=404, detail="Finding not found")

    return ResearchFindingDetail.model_validate(finding)


@router.post("/scan", response_model=ScanResponse)
async def trigger_scan(
    request: ScanRequest,
    org_id: UUID = Depends(get_current_org),
    session: AsyncSession = Depends(get_session),
) -> ScanResponse:
    """Trigger a manual scan of external data sources.

    If sources are not specified, all sources are scanned.
    This endpoint is also called by the scheduled cron/CloudWatch trigger, which
    authenticates as an organization like any other caller.

    A scan spends provider budget, so the target workspace must belong to the
    caller's organization (issue #5682, A02).
    """
    await _require_owned_workspace(session, org_id, request.workspace_id)

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
    org_id: UUID = Depends(get_current_org),
    session: AsyncSession = Depends(get_session),
) -> ScannerStatsResponse:
    """Get aggregate statistics about scanner findings, for this tenant only.

    Counts are as disclosive as rows: an unscoped total tells a caller how much
    research every other organization is doing.
    """
    stats = await get_scanner_stats(session, org_id)
    return ScannerStatsResponse(**stats)


@router.get("/sources")
async def list_sources(
    org_id: UUID = Depends(get_current_org),
) -> dict:
    """List all available scanner sources and their configurations.

    The response is the same for every tenant — a static description of the
    scanner's capabilities, holding no tenant data. It still requires a
    credential (issue #5682, A02) because it describes this deployment's
    configured integrations, and because leaving one route of twelve open is how
    the family stops being reviewable as a whole. It is NOT classified public:
    the inventory's public class is for routes that cannot require a credential.
    """
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
    status: str | None = Query(None, description="Filter by status"),
    workspace_id: UUID | None = Query(None, description="Filter by workspace"),
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(20, ge=1, le=100, description="Items per page"),
    org_id: UUID = Depends(get_current_org),
    session: AsyncSession = Depends(get_session),
) -> ResearchProposalsList:
    """List research proposals for the caller's organization."""
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
    org_id: UUID = Depends(get_current_org),
    session: AsyncSession = Depends(get_session),
) -> ProposalStatsResponse:
    """Get aggregate statistics about this tenant's research proposals."""
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
    org_id: UUID = Depends(get_current_org),
    session: AsyncSession = Depends(get_session),
) -> ResearchProposalResponse:
    """Get a single research proposal with full details.

    Another tenant's proposal answers 404 rather than 403, so the response does
    not confirm the id exists.
    """
    query = _scope_to_tenant(select(ResearchProposal), ResearchProposal, org_id).where(
        ResearchProposal.id == proposal_id
    )
    result = await session.execute(query)
    proposal = result.scalar_one_or_none()

    if not proposal:
        raise HTTPException(status_code=404, detail="Proposal not found")

    return ResearchProposalResponse.model_validate(proposal)


@router.post("/proposals", response_model=ResearchProposalResponse, status_code=201)
async def create_proposal(
    request: ProposalCreateRequest,
    org_id: UUID = Depends(get_current_org),
    session: AsyncSession = Depends(get_session),
) -> ResearchProposalResponse:
    """Create a research proposal manually.

    Proposals start in 'proposed' status and require human approval
    before experiments begin.

    The row's tenant comes from its workspace, so the named workspace must belong
    to the caller's organization (issue #5682, A02) — otherwise a caller could
    plant rows inside another tenant.
    """
    import uuid

    await _require_owned_workspace(session, org_id, request.workspace_id)

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
    org_id: UUID = Depends(get_current_org),
    session: AsyncSession = Depends(get_session),
) -> ProposalGenerateResponse:
    """Auto-generate research proposals from scanner findings.

    The analysis agent reviews findings with relevance_score >= min_relevance,
    groups them into themes, and generates actionable proposals with cost
    estimates and experiment plans.

    Generation reads findings and writes proposals inside one workspace, so that
    workspace must belong to the caller's organization (issue #5682, A02).
    """
    await _require_owned_workspace(session, org_id, request.workspace_id)

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
    user_context: dict = Depends(get_current_user_context),
    session: AsyncSession = Depends(get_session),
) -> ResearchProposalResponse:
    """Approve a research proposal for execution.

    Only proposals in 'proposed' status can be approved.
    Approved proposals move to the experiment queue (US-G4).

    The recorded approver is always server-derived, never the ``approved_by``
    field in the request body (issues #5055 and #5682). This previously wrote the
    body value straight to ``proposal.approved_by`` whenever strict enforcement
    was off — which was the default — so the audit record said whatever the caller
    typed, on a route that also required no credential at all. Body fields never
    carry authority; see ``_recorded_actor`` for the three server-derived sources
    it uses instead.

    ``user_context`` rather than a bare org id because approval is the one action
    here worth attributing to a user when the credential names one.
    """
    org_id = user_context["org_id"]
    query = _scope_to_tenant(select(ResearchProposal), ResearchProposal, org_id).where(
        ResearchProposal.id == proposal_id
    )
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

    approver = _recorded_actor(http_request, user_context)

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
    user_context: dict = Depends(get_current_user_context),
    session: AsyncSession = Depends(get_session),
) -> ResearchProposalResponse:
    """Reject a research proposal.

    Only proposals in 'proposed' or 'approved' status can be rejected.

    The logged rejector is server-derived rather than the body's ``rejected_by``,
    for the same reason as approval — a body field is a claim, not an identity.
    """
    query = _scope_to_tenant(
        select(ResearchProposal), ResearchProposal, user_context["org_id"]
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
        _recorded_actor(http_request, user_context),
        request.reason or "no reason",
    )

    return ResearchProposalResponse.model_validate(proposal)
