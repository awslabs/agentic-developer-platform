"""Review and decide a bound daily-budget amendment for initial planning."""

import uuid
from decimal import Decimal
from fastapi import APIRouter, Request
from pydantic import Field
from app.database import async_session_factory
from app.routers.dispatch_recovery import Review, Decision, call
from app.routers.onboarding import _composition
from app.services.planning_budget import PlanningBudgetService

router = APIRouter(
    prefix="/workspaces/{workspace_id}/planning-budget", tags=["workspace-recovery"]
)


class BudgetReview(Review):
    daily_usd: Decimal = Field(gt=0, max_digits=10, decimal_places=2)


def service(request):
    return PlanningBudgetService(async_session_factory, _composition(request))


@router.post("/review")
async def review(workspace_id: uuid.UUID, body: BudgetReview, request: Request):
    return await call(
        service(request).request(
            str(workspace_id),
            expected_version=body.expected_version,
            idempotency_key=str(body.idempotency_key),
            daily_usd=body.daily_usd,
        )
    )


@router.post("/decision")
async def decide(workspace_id: uuid.UUID, body: Decision, request: Request):
    return await call(
        service(request).decide(
            str(workspace_id), str(body.reauthorization_id), body.result
        )
    )
