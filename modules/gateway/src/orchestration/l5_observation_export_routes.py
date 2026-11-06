"""Current workflow OIDC may retrieve only its own partial human observations."""

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from .l5_effect_export import ExportEffect, export_effect
from .l5_observation_discovery import DiscoverObservation, discover
from .l5_observation_export import ExportObservation, export_observation
from .l5_observation_routes import NEXT_ACTION
from .l5_observation_scope import ObservationError
from .operations_release_job import AUDIENCE, github_claims, require, service
from .review_cycle import CycleBlockedError

router = APIRouter(prefix="/orchestration/operations-release-job/l5-observations", tags=["orchestration"])


@router.post("/export")
async def export_human_observation(body: ExportObservation, request: Request, response: Response, guard=Depends(service)):
    response.headers["Cache-Control"] = "no-store"
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    require(scheme.lower() == "bearer" and 0 < len(token) <= 8192 and not any(character.isspace() for character in token))
    claims = await github_claims(token, audience=AUDIENCE)
    try:
        return await export_observation(guard, body, claims)
    except (ObservationError, CycleBlockedError) as error:
        code = error.code if isinstance(error, ObservationError) else error.reason
        raise HTTPException(
            error.status if isinstance(error, ObservationError) else 409,
            detail={
                "code": code,
                "next_action": NEXT_ACTION.get(
                    code, "Verify the original human receipt and this exact release run's current Product access; do not resubmit native work."
                ),
            },
        ) from None


@router.post("/effects/export")
async def export_request_effect(body: ExportEffect, request: Request, response: Response, guard=Depends(service)):
    response.headers["Cache-Control"] = "no-store"
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    require(scheme.lower() == "bearer" and 0 < len(token) <= 8192 and not any(character.isspace() for character in token))
    claims = await github_claims(token, audience=AUDIENCE)
    try:
        return await export_effect(guard, body, claims)
    except (ObservationError, CycleBlockedError) as error:
        code = error.code if isinstance(error, ObservationError) else error.reason
        raise HTTPException(
            error.status if isinstance(error, ObservationError) else 409,
            detail={
                "code": code,
                "next_action": "Read the retained before/after effect and the original selected scope. "
                "An unknown response may have committed; do not repeat the operation to create evidence.",
            },
        ) from None


@router.post("/discover")
async def discover_current_observation(body: DiscoverObservation, request: Request, response: Response, guard=Depends(service)):
    response.headers["Cache-Control"] = "no-store"
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    require(scheme.lower() == "bearer" and 0 < len(token) <= 8192 and not any(character.isspace() for character in token))
    claims = await github_claims(token, audience=AUDIENCE)
    try:
        return await discover(guard, body, claims)
    except (ObservationError, CycleBlockedError) as error:
        code = error.code if isinstance(error, ObservationError) else error.reason
        raise HTTPException(
            error.status if isinstance(error, ObservationError) else 409,
            detail={
                "code": code,
                "next_action": "Select the exact intended observer and runtime readback scope. "
                "Resolve ambiguous or changed receipts; do not select the newest run or reuse old evidence.",
            },
        ) from None
