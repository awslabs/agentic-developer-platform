"""Product-team activity reads anchored in server-recorded execution attribution.

No tenant-wide activity scan, client-side filtering, or correlation-based grant.
Every detail, transcript and chain member must have its own Project attribution.
"""

import asyncio
import base64
import json
from datetime import datetime
from typing import Annotated, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import PlainTextResponse
from sqlalchemy import and_, exists, func, or_, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from src.activity.schemas import ChainListResponse, ChainSummary, InvocationChainItem, InvocationChainResponse, InvocationItem, InvocationListResponse
from src.activity.service import NON_TRIGGERING_STATUSES, ActivityService
from src.auth.dependencies import get_current_user
from src.orchestration.models import OrchestrationFlow
from src.orchestration.run_reports import OrchestrationRunReport
from src.products.models import ProjectRun
from src.shared.database import get_db
from src.shared.schemas.auth import TokenContext

router = APIRouter(prefix="/v1/products/{product_id}/projects/{project_id}/agent-invocations", tags=["activity"])


def get_service():
    return ActivityService()


async def authorize(db, actor, product_id, project_id):
    from src.products.budget_routes import accessible_project

    product, _, _, _ = await accessible_project(db, actor, project_id)
    if product.id != product_id:
        raise HTTPException(404, "Project unavailable or you do not have access.")


def project_runs(tenant_id, product_id, project_id):
    """Current and historic dispatched runs, including failures before metering.

    The report assignment is written by dispatch, before a worker starts. The
    immutable metering attribution takes precedence if the two disagree.
    """
    metered = select(ProjectRun.run_id, ProjectRun.created_at).where(
        ProjectRun.tenant_id == tenant_id, ProjectRun.product_id == product_id, ProjectRun.project_id == project_id
    )
    report = OrchestrationRunReport
    dispatched = (
        select(report.run_id, report.created_at)
        .join(OrchestrationFlow, and_(OrchestrationFlow.org_id == report.org_id, OrchestrationFlow.id == report.flow_id))
        .where(
            report.org_id == tenant_id,
            OrchestrationFlow.product_id == product_id,
            OrchestrationFlow.project_id == project_id,
            ~exists().where(ProjectRun.tenant_id == tenant_id, ProjectRun.run_id == report.run_id),
        )
    )
    candidates = union_all(metered, dispatched).subquery()
    return select(candidates.c.run_id, func.min(candidates.c.created_at).label("created_at")).group_by(candidates.c.run_id).subquery()


async def allowed_ids(db, actor, product_id, project_id, ids):
    rows = project_runs(actor.org_id, product_id, project_id)
    return set(await db.scalars(select(rows.c.run_id).where(rows.c.run_id.in_(ids)))) if ids else set()


async def scoped_item(db, service, actor, product_id, project_id, invocation_id):
    await authorize(db, actor, product_id, project_id)
    if invocation_id not in await allowed_ids(db, actor, product_id, project_id, [invocation_id]):
        raise HTTPException(404, "Invocation not found in this Project")
    item = await run_in_threadpool(service.get_invocation, invocation_id, tenant_id=actor.org_id)
    if item is None:
        raise HTTPException(404, "Invocation not found in this Project")
    from src.shared.identity import resolve_canonical_user_id

    reader = await resolve_canonical_user_id(db, actor.user_id, org_id=actor.org_id)
    item.viewer_is_run_owner = item.root_human_id in {reader, actor.user_id}
    return item


def encode_cursor(scope, row):
    return base64.urlsafe_b64encode(json.dumps([scope, row.created_at.isoformat(), row.run_id]).encode()).decode()


def decode_cursor(value, scope):
    try:
        saved_scope, timestamp, run_id = json.loads(base64.b64decode(value, altchars=b"-_", validate=True))
        if saved_scope != scope or not isinstance(run_id, str) or not 0 < len(run_id) <= 255:
            raise ValueError
        return datetime.fromisoformat(timestamp), run_id
    except (ValueError, TypeError, UnicodeError):
        raise HTTPException(422, "Invalid activity cursor. Return to the first page.") from None


@router.get("", response_model=InvocationListResponse | ChainListResponse)
async def list_project_activity(
    product_id: str,
    project_id: str,
    response: Response,
    actor: Annotated[TokenContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[ActivityService, Depends(get_service)],
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    last_key: Annotated[str | None, Query(max_length=4096)] = None,
    status: str | None = None,
    channel: str | None = None,
    persona: str | None = None,
    since: str | None = None,
    until: str | None = None,
    include_non_triggering: bool = False,
    view: Literal["runs", "chains"] = "runs",
):
    from src.activity.routes import _enrich_with_cost, _expand_date_bound

    await authorize(db, actor, product_id, project_id)
    response.headers["Cache-Control"] = "no-store"
    # Bind a cursor to the authorized scope and filters; no page can widen access.
    scope = [actor.org_id, product_id, project_id, status, channel, persona, since, until, include_non_triggering]
    candidates = project_runs(actor.org_id, product_id, project_id)
    query = select(candidates).order_by(candidates.c.created_at.desc(), candidates.c.run_id.desc())
    if last_key:
        timestamp, run_id = decode_cursor(last_key, scope)
        query = query.where(or_(candidates.c.created_at < timestamp, and_(candidates.c.created_at == timestamp, candidates.c.run_id < run_id)))
    rows = (await db.execute(query.limit(101))).all()
    since, until = _expand_date_bound(since, end=False), _expand_date_bound(until, end=True)
    items, consumed = [], 0
    # Bound cross-store work even for a sparse filter. Empty pages retain a cursor.
    for start in range(0, min(len(rows), 100), 8):
        batch = rows[start : min(start + 8, 100)]
        found = await asyncio.gather(*(run_in_threadpool(service.get_invocation, row.run_id, tenant_id=actor.org_id) for row in batch))
        for row, item in zip(batch, found, strict=True):
            consumed += 1
            if (
                item
                and (not status or item.status == status)
                and (status or include_non_triggering or item.status not in NON_TRIGGERING_STATUSES)
                and (not channel or item.channel == channel)
                and (not persona or item.persona == persona)
                and (not since or item.invoked_at >= since)
                and (not until or item.invoked_at <= until)
            ):
                items.append(item)
            if len(items) == page_size:
                break
        if len(items) == page_size:
            break
    cursor = encode_cursor(scope, rows[consumed - 1]) if consumed and consumed < len(rows) else None
    from src.shared.identity import resolve_canonical_user_id

    reader = await resolve_canonical_user_id(db, actor.user_id, org_id=actor.org_id)
    for item in items:
        item.viewer_is_run_owner = item.root_human_id in {reader, actor.user_id}
    result = await _enrich_with_cost(db, InvocationListResponse(items=items, count=len(items), last_key=cursor), tenant_id=actor.org_id)
    if view == "runs":
        return result
    # Group this authorized page only. A chain can be opened to inspect all its
    # authorized members; neither grouping nor lineage grants new read access.
    grouped = {}
    for item in result.items:
        grouped.setdefault(item.correlation_id or item.invocation_id, []).append(item)
    chains = []
    for chain_id, members in grouped.items():
        members.sort(key=lambda item: (item.invoked_at, item.invocation_id))
        root, *descendants = members
        chains.append(
            ChainSummary(
                chain_id=chain_id,
                root=root,
                descendant_count=len(descendants),
                descendants=[InvocationChainItem(**item.model_dump(), parent_invocation_id=item.triggered_by_invocation_id) for item in descendants],
                chain_total_cost_usd=sum(item.total_cost_usd or 0 for item in members)
                if any(item.total_cost_usd is not None for item in members)
                else None,
            )
        )
    return ChainListResponse(chains=chains, count=len(chains), last_key=cursor)


@router.get("/chain/{correlation_id}", response_model=InvocationChainResponse)
async def project_chain(
    product_id: str,
    project_id: str,
    correlation_id: str,
    response: Response,
    actor: Annotated[TokenContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[ActivityService, Depends(get_service)],
    include_non_triggering: bool = False,
):
    from src.activity.routes import _enrich_chain_with_cost

    await authorize(db, actor, product_id, project_id)
    response.headers["Cache-Control"] = "no-store"
    chain = await run_in_threadpool(
        service.get_chain, correlation_id, tenant_id=actor.org_id, include_non_triggering=include_non_triggering, page_cap=20, preserve_errors=True
    )
    flat = []

    def flatten(nodes):
        for node in nodes:
            flat.append(node)
            flatten(node.children)

    flatten(chain.items)
    allowed = await allowed_ids(db, actor, product_id, project_id, [node.invocation_id for node in flat])
    visible = {node.invocation_id: node.model_copy(update={"children": []}) for node in flat if node.invocation_id in allowed}
    roots = []
    for node in visible.values():
        if node.parent_invocation_id in visible and node.parent_invocation_id != node.invocation_id:
            visible[node.parent_invocation_id].children.append(node)
        else:
            node.parent_invocation_id = None
            roots.append(node)
    if not visible:
        raise HTTPException(404, "Chain not found in this Project")
    return await _enrich_chain_with_cost(
        db,
        InvocationChainResponse(correlation_id=correlation_id, items=roots, total_count=len(visible), depth_capped=chain.depth_capped),
        tenant_id=actor.org_id,
    )


@router.get("/{invocation_id}", response_model=InvocationItem)
async def project_detail(
    product_id: str,
    project_id: str,
    invocation_id: str,
    response: Response,
    actor: Annotated[TokenContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[ActivityService, Depends(get_service)],
):
    from src.activity.budget_usage import enrich_budget_usage
    from src.activity.routes import _enrich_with_cost

    item = await scoped_item(db, service, actor, product_id, project_id, invocation_id)
    response.headers["Cache-Control"] = "no-store"
    await _enrich_with_cost(db, InvocationListResponse(items=[item], count=1), tenant_id=actor.org_id)
    return await enrich_budget_usage(db, item, tenant_id=actor.org_id)


@router.get("/{invocation_id}/transcript")
async def project_transcript(
    product_id: str,
    project_id: str,
    invocation_id: str,
    actor: Annotated[TokenContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[ActivityService, Depends(get_service)],
):
    from src.activity.routes import _fetch_transcript

    item = await scoped_item(db, service, actor, product_id, project_id, invocation_id)
    if not item.transcript_key:
        raise HTTPException(404, "Transcript not available for this invocation")
    return PlainTextResponse(await _fetch_transcript(item.transcript_key), media_type="text/markdown", headers={"Cache-Control": "no-store"})


@router.get("/{invocation_id}/events")
async def project_events(
    product_id: str,
    project_id: str,
    invocation_id: str,
    request: Request,
    actor: Annotated[TokenContext, Depends(get_current_user)],
    service: Annotated[ActivityService, Depends(get_service)],
):
    from src.activity.control_service import ControlError
    from src.activity.explanation_stream import open_explanation_stream
    from src.activity.routes import _raise_control_error, get_control_service
    from src.agentauth.bootstrap import BootstrapRefusedError
    from src.agentauth.human_control import authorize_human_session
    from src.shared.database import get_session_factory
    from src.shared.identity import resolve_canonical_user_id

    if request.query_params:
        raise HTTPException(400, "Event stream accepts no query parameters")

    async def reauthorize():
        async with get_session_factory()() as session_db:
            session = await authorize_human_session(actor, session_db)
            item = await scoped_item(session_db, service, actor, product_id, project_id, invocation_id)
            binding = await session_db.get(ProjectRun, (actor.org_id, invocation_id))
            owner = binding.owner_user_id if binding else item.root_human_id
            if not owner:
                raise BootstrapRefusedError("Run owner is unavailable")
            owner = await resolve_canonical_user_id(session_db, owner, org_id=actor.org_id)
            return session, owner

    try:
        session, owner = await reauthorize()

        async def recheck():
            _, current_owner = await reauthorize()
            if current_owner != owner:
                raise BootstrapRefusedError("Run owner changed")

        return await open_explanation_stream(
            get_control_service(),
            invocation_id,
            session=session,
            reauthorize=recheck,
            cursor=request.headers.get("last-event-id"),
            target_owner_id=owner,
        )
    except BootstrapRefusedError:
        raise HTTPException(404, "Run not found in this Project") from None
    except ControlError as exc:
        _raise_control_error(exc)
    except httpx.HTTPError:
        raise HTTPException(503, "Live explanations unavailable. Retry shortly.") from None
