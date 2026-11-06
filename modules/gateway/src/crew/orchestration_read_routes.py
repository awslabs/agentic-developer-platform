"""Read-only owner projections for the Dev Box's verified host run."""

import os
import time
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.access_control import AccessControl
from src.admin.connections.service import list_connections
from src.auth.vault_routes import get_secrets_manager
from src.crew.host_routes import host_run_route
from src.crew.host_tools import current_host_target
from src.crew.model_token import verify_model_token
from src.orchestration import controls as flow_controls
from src.orchestration import draft_routes
from src.orchestration import routes as flow_routes
from src.products import budget_routes, source_routes
from src.products import routes as product_routes
from src.proxy.routes import get_token_context, set_agent_run_id_from_header
from src.shared.database import get_db
from src.shared.identity.workflow_access import is_workflow_member
from src.shared.models.audit import AuditLog
from src.shared.schemas.auth import TokenContext

router = APIRouter(prefix="/crew/orchestration", tags=["crew"])


def read_route(path: str) -> str:
    host_run_route(router.prefix + path)
    return path


async def owner_context(
    context: Annotated[TokenContext, Depends(get_token_context)],
    run_id: Annotated[str | None, Depends(set_agent_run_id_from_header)],
    token: Annotated[str | None, Header(alias="X-Adp-Crew-Model-Token")] = None,
) -> TokenContext:
    now = int(time.time())
    owner, target = await current_host_target(context, run_id, now=now)
    claims = await verify_model_token(token or "", target, now=now)
    if claims["run"] != run_id:
        raise HTTPException(403, detail={"error": "crew_model_credential_denied"})
    return TokenContext(
        user_id=owner.user_id,
        org_id=owner.tenant_id,
        team_id="",
        department_id="",
        account_type="human",
        expires_at=context.expires_at,
    )


async def draft_actor(
    actor: Annotated[TokenContext, Depends(owner_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TokenContext:
    if not await is_workflow_member(db, user_id=actor.user_id, org_id=actor.org_id):
        raise HTTPException(403, detail={"error": "workflow_membership_denied"})
    return actor.model_copy(update={"account_type": "service", "auth_source": "iam"})


OWNER_SESSION_MINUTES = 60


def public_api_url() -> str:
    """The URL the box CLI uses as the owner would: the deployment's public API base (#9640)."""
    explicit = os.environ.get("ADP_PUBLIC_API_URL", "").strip().rstrip("/")
    if explicit:
        return explicit
    for origin in os.environ.get("CORS_ALLOWED_ORIGINS", "").split(","):
        origin = origin.strip().rstrip("/")
        if origin.startswith("https://") and "*" not in origin:
            return f"{origin}/api"
    raise HTTPException(503, detail={"error": "public_api_url_unavailable"})


@router.post(read_route("/owner-session"), status_code=201)
async def owner_session(
    response: Response,
    context: Annotated[TokenContext, Depends(get_token_context)],
    run_id: Annotated[str | None, Depends(set_agent_run_id_from_header)],
    db: Annotated[AsyncSession, Depends(get_db)],
    token: Annotated[str | None, Header(alias="X-Adp-Crew-Model-Token")] = None,
):
    """Exchange a verified Dev Box host run for a short-lived owner session (#9640).

    The owner decided that code on their own Dev Box acts with the same authority as
    their laptop CLI. The session is the owner's login-equivalent context: their
    workspace user, their team, their platform-admin standing (read from Cognito
    groups, exactly as a login's claims carry it). It is a gateway-minted token in the
    revocable token table, expires in ``OWNER_SESSION_MINUTES`` and carries
    ``delegation = dev-box:<box>:<host_run>`` so every request made with it is logged as
    the owner acting through that run. Only the host run that ``owner_context``
    verified can mint; a fenced or foreign run gets no session.
    """
    from starlette.concurrency import run_in_threadpool

    from src.admin.platform_authority import current_cognito_admin
    from src.auth.auth_service import AuthService
    from src.auth.schemas import TenantInfo
    from src.shared.identity.workspaces import login_subject_for_user, workspace_user

    # Same proof as owner_context: the box's signed instance identity, its current host
    # lease and the gateway-minted model token for this very run.
    now = int(time.time())
    owner, target = await current_host_target(context, run_id, now=now)
    claims = await verify_model_token(token or "", target, now=now)
    if claims["run"] != run_id:
        raise HTTPException(403, detail={"error": "crew_model_credential_denied"})
    member = await workspace_user(db, owner.user_id, owner.tenant_id, refresh=True)
    if member is None:
        raise HTTPException(403, detail={"error": "workspace_membership_denied"})
    # The session's subject is the owner's verified login subject, exactly what their
    # Cognito login carries, so membership and authority lookups resolve the same person.
    # Linked workspace accounts keep that subject in UserIdentity (users.cognito_sub is
    # null); login_subject_for_user is the platform's resolver for both shapes. A member
    # with no verified Cognito link has no Cognito standing to carry: the session then
    # names the workspace row itself and current_cognito_admin answers False.
    try:
        subject = await login_subject_for_user(db, member) or member.id
    except ValueError:
        raise HTTPException(403, detail={"error": "ambiguous_identity"})
    is_admin = await run_in_threadpool(current_cognito_admin, subject, member.cognito_username or "")
    delegation = f"dev-box:{target.lease.target_ref}:{target.host_run_id or run_id}"
    gateway_url = public_api_url()
    token_manager = AuthService().token_manager
    session_identity = TenantInfo(
        org_id=owner.tenant_id,
        org_name=owner.tenant_id,
        department_id="",
        team_id=member.team_id,
        account_type="human",
        entity_id=subject,
        is_admin=bool(is_admin),
    )
    token, expires_at = token_manager.generate_token(session_identity, duration_minutes=OWNER_SESSION_MINUTES, delegation=delegation)
    await token_manager.store_token(token, session_identity, expires_at, db)
    db.add(
        AuditLog(
            org_id=owner.tenant_id,
            actor_id=member.id,
            event_type="dev_box_owner_session_minted",
            details={"delegation": delegation, "expires_at": expires_at.isoformat(), "is_admin": bool(is_admin), "team_id": member.team_id},
        )
    )
    await db.commit()
    response.headers["Cache-Control"] = "no-store"
    return {
        "access_token": token,
        "expires_at": expires_at.isoformat(),
        "gateway_url": gateway_url,
        "delegation": delegation,
        "user_id": subject,
        "is_admin": bool(is_admin),
    }


@router.get(read_route("/products"))
async def products(
    response: Response,
    actor: Annotated[TokenContext, Depends(owner_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    cursor: str | None = None,
    limit: int = Query(100, ge=1, le=100),
):
    return await product_routes.list_products(response=response, cursor=cursor, limit=limit, db=db, actor=actor)


@router.get(read_route("/products/{product_id}/projects/{project_id}/sources"))
async def sources(
    product_id: str,
    project_id: str,
    response: Response,
    actor: Annotated[TokenContext, Depends(owner_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = Query(50, ge=1, le=100),
    cursor: str | None = None,
    connection_id: str | None = None,
    connection_kind: Literal["github_installation", "credential"] | None = None,
    resource_cursor: str | None = None,
    secrets_manager=Depends(get_secrets_manager),
):
    return await source_routes.list_sources(
        product_id=product_id,
        project_id=project_id,
        response=response,
        limit=limit,
        cursor=cursor,
        connection_id=connection_id,
        connection_kind=connection_kind,
        resource_cursor=resource_cursor,
        pipeline_repository_id=None,
        secrets_manager=secrets_manager,
        db=db,
        actor=actor,
    )


@router.get(read_route("/products/{product_id}/projects/{project_id}"))
async def profile(
    product_id: str,
    project_id: str,
    response: Response,
    actor: Annotated[TokenContext, Depends(owner_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """The Product profile (delivery home, revision) the adp-flow preflight reads on a Dev Box (#9522)."""
    return await product_routes.get_profile(
        product_id=product_id, project_id=project_id, response=response, db=db, actor=actor, x_l5_delegation_id=None
    )


@router.get(read_route("/projects/{project_id}/budget"))
async def project_budget(
    project_id: str,
    response: Response,
    actor: Annotated[TokenContext, Depends(owner_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Read-only Project budget for the adp-flow preflight on a Dev Box (#9522); writes stay laptop-only."""
    return await budget_routes.get_project_budget(project_id=project_id, response=response, db=db, actor=actor, run_id=None)


@router.get(read_route("/github/status"))
async def github_status(
    response: Response,
    actor: Annotated[TokenContext, Depends(owner_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    response.headers["Cache-Control"] = "no-store"
    return await list_connections(
        caller_org_id=actor.org_id,
        caller_user_id=actor.user_id,
        db=db,
        member_tenant_ids=[actor.org_id],
        caller_is_admin=False,
    )


@router.get(read_route("/flows"))
async def flows(
    actor: Annotated[TokenContext, Depends(owner_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0),
    status: flow_routes.FlowStatus | None = None,
    include_archived: bool = False,
    needs_me: bool = False,
):
    return await flow_routes.list_flows_route(
        current_user=actor,
        access=AccessControl(db),
        db=db,
        limit=limit,
        offset=offset,
        q=None,
        status=status,
        include_archived=include_archived,
        needs_me=needs_me,
        sort="created",
        product_id=None,
    )


@router.get(read_route("/flows/{flow_id}"))
async def flow(
    flow_id: str,
    actor: Annotated[TokenContext, Depends(owner_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    return await flow_routes.get_flow_graph(
        flow_id=flow_id,
        current_user=actor,
        access=AccessControl(db),
        db=db,
        include_activity=False,
    )


@router.get(read_route("/flows/{flow_id}/plans"))
async def flow_plans(
    flow_id: str,
    actor: Annotated[TokenContext, Depends(owner_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    return await flow_routes.list_plans(flow_id=flow_id, current_user=actor, access=AccessControl(db), db=db)


@router.get(read_route("/flows/{flow_id}/decisions"))
async def flow_decisions(
    flow_id: str,
    actor: Annotated[TokenContext, Depends(owner_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    return await flow_controls.list_flow_decisions(flow_id=flow_id, current_user=actor, access=AccessControl(db), db=db)


@router.post(read_route("/flows/drafts/preview"))
async def preview_draft(
    proposal: draft_routes.LoopProposal,
    actor: Annotated[TokenContext, Depends(draft_actor)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    return await draft_routes.preview_draft(proposal=proposal, current_user=actor, access=AccessControl(db))


@router.post(read_route("/flows/drafts"), status_code=201)
async def register_draft(
    proposal: draft_routes.LoopProposal,
    response: Response,
    actor: Annotated[TokenContext, Depends(draft_actor)],
    db: Annotated[AsyncSession, Depends(get_db)],
    reason: str | None = Query(None, max_length=2000),
):
    return await draft_routes.register_draft(
        proposal=proposal,
        response=response,
        current_user=actor,
        access=AccessControl(db),
        db=db,
        run_bindings=None,
        reason=reason,
        x_agent_run_id=None,
    )
