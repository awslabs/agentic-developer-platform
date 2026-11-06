"""Authenticated Crew target lookup for proxy consumers."""

import asyncio
import os
import time
from datetime import UTC, datetime
from typing import Annotated
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, Body, Depends, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.agentauth.chat_authority import principal_refusal_detail, tenant_member_principal
from src.agentauth.chat_capability import ChatAuthorizationRefusedError
from src.auth.dependencies import get_current_user
from src.cloud_desktop.host_identity import current_host
from src.cloud_desktop.routes import ActivityRequest, accept_box_activity
from src.crew.handoff_tickets import HANDOFF_TICKET_LIFETIME_SECONDS, mint_handoff_ticket
from src.crew.host_routes import host_run_route
from src.crew.inventory import CrewExecutorInventory
from src.crew.targets import (
    CrewLeaseFencedError,
    CrewOwner,
    CrewTargetMissingError,
    CrewTargetPolicy,
    CrewTargetResolver,
    OwnerTargetResolver,
    ResolvedCrewTarget,
)
from src.features.routes import _is_enabled, _is_enabled_strict
from src.orchestration.intake_wiring import _get_context_table
from src.proxy.routes import get_token_context, set_agent_run_id_from_header
from src.shared.database import get_db, get_session_factory
from src.shared.models.dev_box import DevBox
from src.shared.models.organization import Organization
from src.shared.schemas.auth import TokenContext

router = APIRouter(tags=["crew"])


class HostActivity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    busy: bool | None = None
    consolidation_complete: bool | None = None


@router.post(host_run_route("/crew/model-token/refresh"), status_code=204)
async def refresh_crew_model_token(
    context: Annotated[TokenContext, Depends(get_token_context)],
    run_id: Annotated[str | None, Depends(set_agent_run_id_from_header)],
    heartbeat: HostActivity = Body(default_factory=HostActivity),
    model_token: str | None = Header(default=None, alias="X-Adp-Crew-Model-Token"),
):
    from src.crew.handoff import deliver_model_token

    if context.auth_source != "iam" or context.account_type != "service" or not context._dev_box_instance_id or not run_id:
        raise HTTPException(403, detail={"error": "crew_model_credential_denied"})
    from src.agentauth.chat_crew_run import current_crew_store

    row = await asyncio.to_thread(current_crew_store().read_run, run_id)
    if row is None or row.get("target_kind") != "ec2-instance":
        raise HTTPException(403, detail={"error": "crew_model_credential_denied"})
    owner = CrewOwner(row["tenant_id"], row["user_id"])
    try:
        target = await get_target_resolver().resolve(owner, now=int(time.time()))
    except (CrewTargetMissingError, CrewLeaseFencedError):
        raise HTTPException(409, detail={"error": "lease_fenced"}) from None
    if target.host_run_id != run_id or target.host_instance_ref != context._dev_box_instance_id:
        raise HTTPException(403, detail={"error": "crew_model_credential_denied"})
    if heartbeat.busy is not None:
        if heartbeat.consolidation_complete is None or (heartbeat.busy and heartbeat.consolidation_complete):
            raise HTTPException(422, detail={"error": "invalid_crew_activity"})
        from src.crew.model_token import verify_model_token

        claims = await verify_model_token(model_token or "", target, now=int(time.time()))
        if claims["run"] != run_id:
            raise HTTPException(403, detail={"error": "crew_model_credential_denied"})
        async with get_session_factory()() as db:
            box = await db.scalar(
                select(DevBox)
                .where(
                    DevBox.id == row["session_id"],
                    DevBox.user_id == owner.user_id,
                    DevBox.org_id == owner.tenant_id,
                    DevBox.deployment_id == row.get("deployment_id"),
                    DevBox.machine_generation == row["lease_generation"],
                    DevBox.profile == "crew",
                    DevBox.auth_readiness == "ready",
                )
                .with_for_update()
            )
            if (
                box is None
                or await current_host(
                    db,
                    box_id=box.id,
                    generation=box.machine_generation,
                    provider_instance_id=context._dev_box_instance_id,
                    user_id=box.user_id,
                    org_id=box.org_id,
                    deployment_id=box.deployment_id,
                    run_id=run_id,
                )
                is None
            ):
                raise HTTPException(409, detail={"error": "lease_fenced"})
            await accept_box_activity(
                db,
                box,
                ActivityRequest(
                    kind="assistant",
                    busy=heartbeat.busy,
                    consolidation_complete=heartbeat.consolidation_complete,
                ),
                datetime.now(UTC),
            )
            await deliver_model_token(owner, target, now=int(time.time()))
            await db.commit()
        return
    elif heartbeat.consolidation_complete is not None:
        raise HTTPException(422, detail={"error": "invalid_crew_activity"})
    await deliver_model_token(owner, target, now=int(time.time()))


@router.get("/crew/model-usage/{run_id}")
async def crew_model_receipts(
    run_id: str,
    identity: Annotated[TokenContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    from src.agentauth.chat_crew_run import current_crew_store
    from src.crew.model_usage import CrewModelUsage

    return await CrewModelUsage(current_crew_store()).receipts(run_id, identity, db)


def get_target_resolver() -> CrewTargetResolver:
    table = _get_context_table()
    if table is None:
        raise HTTPException(503, detail={"error": "crew_target_unavailable"})
    return OwnerTargetResolver(
        CrewExecutorInventory(get_session_factory()),
        table,
        deployment_id=os.environ.get("BG_DEV_BOX_DEPLOYMENT_ID", ""),
    )


async def resolve_crew_owner(db: AsyncSession, context: TokenContext) -> CrewOwner:
    """The Crew owner is the caller's *tenant member row*, resolved like every other owner (#8108).

    ``context.user_id`` is the Cognito sub (or, for a redeemed handoff ticket, the
    ``users.id`` the ticket was minted for); it is never used as ``CrewOwner.user_id``
    directly. A GitHub-attested member row carries no ``cognito_sub``, so matching the
    sub against ``users.id`` refused the owner while ``/cloud-desktops/*`` (which fused
    through the signed username) let the same person in. Both now call
    ``tenant_member_principal`` so the Crew owner, the Dev Box owner and the chat
    principal are the same id -- the one ``CrewExecutor``/``DevBox`` rows are keyed on.
    """
    try:
        member = await tenant_member_principal(db, context.org_id, context.user_id, username=context.cognito_username)
    except ChatAuthorizationRefusedError as refusal:
        reason = str(refusal)
        raise HTTPException(
            404,
            detail={"error": "crew_scope_refused", "reason": reason, "detail": principal_refusal_detail(reason, context.org_id)},
            headers={"Cache-Control": "no-store"},
        ) from None
    return CrewOwner(context.org_id, member.id)


async def current_crew_owner(
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CrewOwner:
    return await resolve_crew_owner(db, current_user)


async def active_crew_target(
    owner: Annotated[CrewOwner, Depends(current_crew_owner)],
    resolver: Annotated[CrewTargetResolver, Depends(get_target_resolver)],
) -> ResolvedCrewTarget:
    now = int(time.time())
    try:
        target = await resolver.resolve(owner, now=now)
    except CrewTargetMissingError:
        raise HTTPException(404, detail={"error": "crew_target_missing"}, headers={"Cache-Control": "no-store"}) from None
    except CrewLeaseFencedError:
        raise HTTPException(409, detail={"error": "lease_fenced"}, headers={"Cache-Control": "no-store"}) from None
    if target.lease.tenant_id != owner.tenant_id or target.lease.user_id != owner.user_id:
        raise HTTPException(404, detail={"error": "crew_target_missing"}, headers={"Cache-Control": "no-store"})
    if target.health == "starting" and target.lease.expires_at > now:
        # #9619: the machine is running and enrolled; Kiro Crew on it is not answering yet.
        raise HTTPException(409, detail={"error": "crew_starting"}, headers={"Cache-Control": "no-store"})
    if target.health != "ready" or target.lease.expires_at <= now:
        raise HTTPException(409, detail={"error": "lease_fenced"}, headers={"Cache-Control": "no-store"})
    return target


@router.get("/crew/target", response_model=ResolvedCrewTarget)
async def resolve_crew_target(target: Annotated[ResolvedCrewTarget, Depends(active_crew_target)]) -> ResolvedCrewTarget:
    return target


def crew_handoff_origin() -> str:
    """The configured Crew dashboard origin, or 404 when the host proxy is not enabled."""
    origin = urlsplit(os.environ.get("CREW_DASHBOARD_ORIGIN", ""))
    if (
        os.environ.get("CREW_PROXY_ENABLED", "false").lower() != "true"
        or origin.scheme != "https"
        or not origin.hostname
        or origin.port is not None
        or origin.path not in ("", "/")
        or origin.query
        or origin.fragment
    ):
        raise HTTPException(404, detail={"error": "not_found"})
    return f"https://{origin.netloc}"


@router.post("/crew/handoff-ticket")
async def crew_handoff_ticket(
    origin: Annotated[str, Depends(crew_handoff_origin)],
    owner: Annotated[CrewOwner, Depends(current_crew_owner)],
    target: Annotated[ResolvedCrewTarget, Depends(active_crew_target)],
):
    """Mint the single-use ticket the frontend carries on its top-level navigation to the Crew host (#7902).

    The ADP origin is same-site to the frontend, so this authenticated call always works; the
    Crew host then sets `adp_crew` as a first-party cookie while redeeming the ticket.
    """
    now = int(time.time())
    ticket = mint_handoff_ticket(owner, target, now=now)
    return JSONResponse(
        {"handoff_url": f"{origin}/crew/handoff?ticket={quote(ticket, safe='')}", "expires_in": HANDOFF_TICKET_LIFETIME_SECONDS},
        headers={"Cache-Control": "no-store"},
    )


class CrewEnsureRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    runtime: str = "kirocrew"


def crew_dashboard_enabled() -> None:
    if not (
        _is_enabled_strict("FEATURE_CREW_DASHBOARD")
        and _is_enabled_strict("FEATURE_ASSISTANT_UI_ENABLED")
        and _is_enabled("FEATURE_CHAT_ENABLED")
        and _is_enabled_strict("CREW_PROXY_ENABLED")
        and os.environ.get("CREW_DASHBOARD_ORIGIN")
    ):
        raise HTTPException(404, detail={"error": "not_found"})


async def crew_allowed_targets(owner: CrewOwner, db: AsyncSession) -> frozenset[str]:
    settings = await db.scalar(select(Organization.settings).where(Organization.id == owner.tenant_id))
    if not isinstance(settings, dict):
        raise HTTPException(503, detail={"error": "crew_policy_unavailable"})
    try:
        return CrewTargetPolicy.from_tenant_settings(settings).allowed_targets
    except ValueError:
        raise HTTPException(503, detail={"error": "crew_policy_unavailable"}) from None


@router.get("/crew/runtime", dependencies=[Depends(crew_dashboard_enabled)])
async def crew_runtime(
    owner: Annotated[CrewOwner, Depends(current_crew_owner)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    allowed = await crew_allowed_targets(owner, db)
    return {"allowed_targets": sorted(allowed)}


@router.post("/crew/ensure", dependencies=[Depends(crew_dashboard_enabled)])
async def ensure_crew(
    body: CrewEnsureRequest,
    owner: Annotated[CrewOwner, Depends(current_crew_owner)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    if body.runtime != "kirocrew" or "eks-pod" not in await crew_allowed_targets(owner, db):
        raise HTTPException(404, detail={"error": "crew_runtime_unavailable"})
    raise HTTPException(503, detail={"error": "crew_admission_unavailable"})
