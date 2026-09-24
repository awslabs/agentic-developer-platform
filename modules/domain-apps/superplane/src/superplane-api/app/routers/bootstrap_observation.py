"""Operation-scoped bootstrap readiness; never returns registry credentials."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.models.organization import Organization
from app.services.bootstrap_tokens import authenticate_bootstrap_token
from app.services.bootstrap_observation import (
    manager_snapshot,
    operation_connect,
    provisional_targets,
    verified_observation,
)

router = APIRouter(tags=["bootstrap-observation"])


@router.get("/api/v1/workspaces/{workspace_id}/bootstrap-observation")
async def bootstrap_observation(
    workspace_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_session),
):
    token = await authenticate_bootstrap_token(request, db)
    if token.workspace_id != str(workspace_id):
        raise HTTPException(403, "Bootstrap read credential names another workspace")
    org_id, operation_id, claim = (
        token.org_id,
        token.operation_id,
        token.registration_claim,
    )
    try:
        org_uuid = uuid.UUID(org_id)
    except ValueError as exc:
        raise HTTPException(403, "Invalid bootstrap organization scope") from exc
    org = await db.get(Organization, org_uuid)
    if org is None or not org.adp_org_id:
        raise HTTPException(403, "Bound bootstrap organization required")
    connect = operation_connect(request)

    async def current_target():
        targets = await provisional_targets(db, org=org, connect=connect)
        matching = [
            target
            for target in targets
            if target["workspace_id"] == str(workspace_id)
            and target["bootstrap_operation_id"] == operation_id
            and target["registration_claim"] == claim
        ]
        if len(matching) != 1:
            raise HTTPException(
                409, "Bootstrap operation or registration claim is stale"
            )
        return matching[0]

    before = await current_target()
    snapshot = await manager_snapshot()
    # Provider I/O occurs outside a reservation lock. Re-read the real claim and
    # operation after I/O so an expired/replaced bootstrap cannot consume its reply.
    after = await current_target()
    await authenticate_bootstrap_token(request, db)
    if before != after:
        raise HTTPException(409, "Bootstrap observation target changed")
    return await verified_observation(db, org=org, target=after, snapshot=snapshot)
