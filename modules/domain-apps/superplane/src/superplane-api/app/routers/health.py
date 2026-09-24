"""Health check endpoint."""

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_session
from app.management import management_only
from app.schemas.health import HealthResponse
from app.services.provisioning import get_operation_facade

router = APIRouter()


@router.get("/capabilities")
async def capabilities():
    return {
        "version": 1,
        "features": ["create-operation-id-v1"]
        if get_operation_facade() is not None
        else [],
    }


@router.get("/readyz")
async def readiness(request: Request, db: AsyncSession = Depends(get_session)):
    try:
        await db.execute(text("SELECT 1"))
        if (
            management_only()
            and getattr(request.app.state, "domain_policy", None) is None
        ):
            raise ValueError("strict authorization unavailable")
    except Exception:
        raise HTTPException(503, "Management service is not ready") from None
    return {"status": "ready", "mode": "management" if management_only() else "full"}


@router.get("/health", response_model=HealthResponse)
async def health_check(request: Request) -> HealthResponse:
    """Return application health status, version and identity posture.

    The two identity flags are reported so the environment's state can be
    asserted rather than inferred from this repository's defaults (issue #5055,
    R5 acceptance 5).

    `domain_auth_enforced` reads the policy OBJECT THE APP ACTUALLY LOADED, not
    the setting. The two can disagree — a truthy setting whose policy failed to
    build must never report as "enforcing" — and the loaded object is the one
    that decides real requests. It is read from app state rather than rebuilt
    here because rebuilding would re-raise a misconfiguration and turn the
    liveness probe into a 500, which would take the pod down for a reason that
    has nothing to do with liveness.
    """
    return HealthResponse(
        status="healthy",
        version=settings.app_version,
        cognito_enabled=settings.cognito_enabled,
        domain_auth_enforced=getattr(request.app.state, "domain_policy", None)
        is not None,
    )
