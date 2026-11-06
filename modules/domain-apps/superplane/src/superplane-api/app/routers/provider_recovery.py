"""Review, decide and resume the original interrupted planning operation."""

import uuid

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from app.database import async_session_factory
from app.routers.dispatch_recovery import Decision, Resume, Review, call
from app.routers.onboarding import _composition
from app.services.provider_recovery import (
    ProviderRecoveryService,
    SettledBootstrapRecoveryService,
    UnstartedBootstrapRecoveryService,
    SavedPlanCompletionService,
    InterruptedPlanningRecoveryService,
    UnstartedPlanningRecoveryService,
)

router = APIRouter(
    prefix="/workspaces/{workspace_id}/provider-recovery", tags=["workspace-recovery"]
)


def service(request):
    return ProviderRecoveryService(async_session_factory, _composition(request))


@router.post("/review")
async def review(workspace_id: uuid.UUID, body: Review, request: Request):
    return await call(
        service(request).request(
            str(workspace_id),
            expected_version=body.expected_version,
            idempotency_key=str(body.idempotency_key),
        )
    )


@router.post("/decision")
async def decide(workspace_id: uuid.UUID, body: Decision, request: Request):
    return await call(
        service(request).decide(
            str(workspace_id), str(body.reauthorization_id), body.result
        )
    )


@router.post("/resume")
async def resume(workspace_id: uuid.UUID, body: Resume, request: Request):
    return await call(
        service(request).resume(str(workspace_id), str(body.reauthorization_id))
    )


planning_router = APIRouter(
    prefix="/workspaces/{workspace_id}/planning-recovery", tags=["workspace-recovery"]
)


def planning_service(request):
    return UnstartedPlanningRecoveryService(
        async_session_factory, _composition(request)
    )


@planning_router.post("/review")
async def planning_review(workspace_id: uuid.UUID, body: Review, request: Request):
    return await call(
        planning_service(request).request(
            str(workspace_id),
            expected_version=body.expected_version,
            idempotency_key=str(body.idempotency_key),
        )
    )


@planning_router.post("/decision")
async def planning_decide(workspace_id: uuid.UUID, body: Decision, request: Request):
    return await call(
        planning_service(request).decide(
            str(workspace_id),
            str(body.reauthorization_id),
            body.result,
        )
    )


@planning_router.post("/resume")
async def planning_resume(workspace_id: uuid.UUID, body: Resume, request: Request):
    return await call(
        planning_service(request).resume(
            str(workspace_id),
            str(body.reauthorization_id),
        )
    )


completion_router = APIRouter(
    prefix="/workspaces/{workspace_id}/saved-plan-completion",
    tags=["workspace-recovery"],
)


def completion_service(request):
    return SavedPlanCompletionService(async_session_factory, _composition(request))


@completion_router.post("/review")
async def completion_review(workspace_id: uuid.UUID, body: Review, request: Request):
    return await call(
        completion_service(request).request(
            str(workspace_id),
            expected_version=body.expected_version,
            idempotency_key=str(body.idempotency_key),
        )
    )


@completion_router.post("/decision")
async def completion_decide(workspace_id: uuid.UUID, body: Decision, request: Request):
    return await call(
        completion_service(request).decide(
            str(workspace_id),
            str(body.reauthorization_id),
            body.result,
        )
    )


@completion_router.post("/resume")
async def completion_resume(workspace_id: uuid.UUID, body: Resume, request: Request):
    return await call(
        completion_service(request).resume(
            str(workspace_id),
            str(body.reauthorization_id),
        )
    )


interrupted_router = APIRouter(
    prefix="/workspaces/{workspace_id}/interrupted-planning-recovery",
    tags=["workspace-recovery"],
)


def interrupted_service(request):
    return InterruptedPlanningRecoveryService(
        async_session_factory, _composition(request)
    )


@interrupted_router.post("/review")
async def interrupted_review(workspace_id: uuid.UUID, body: Review, request: Request):
    return await call(
        interrupted_service(request).request(
            str(workspace_id),
            expected_version=body.expected_version,
            idempotency_key=str(body.idempotency_key),
        )
    )


@interrupted_router.post("/decision")
async def interrupted_decide(workspace_id: uuid.UUID, body: Decision, request: Request):
    return await call(
        interrupted_service(request).decide(
            str(workspace_id),
            str(body.reauthorization_id),
            body.result,
        )
    )


@interrupted_router.post("/resume")
async def interrupted_resume(workspace_id: uuid.UUID, body: Resume, request: Request):
    return await call(
        interrupted_service(request).resume(
            str(workspace_id),
            str(body.reauthorization_id),
        )
    )


bootstrap_router = APIRouter(
    prefix="/workspaces/{workspace_id}/bootstrap-recovery", tags=["workspace-recovery"]
)


class BootstrapReview(Review):
    max_runtime_seconds: int | None = Field(default=None, strict=True, gt=900, le=10800)


class BootstrapObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(strict=True, ge=1)
    expected_fence: int = Field(strict=True, ge=1)


def bootstrap_service(request):
    return UnstartedBootstrapRecoveryService(
        async_session_factory, _composition(request)
    )


@bootstrap_router.post("/observe")
async def bootstrap_observe(
    workspace_id: uuid.UUID, body: BootstrapObservation, request: Request
):
    async def observe():
        from app.adapters.bootstrap_observer import publish

        service = bootstrap_service(request)
        _, original = await service.original(str(workspace_id))
        return await publish(
            service.composition.dispatcher,
            original,
            expected_version=body.expected_version,
            expected_fence=body.expected_fence,
        )

    return await call(observe())


@bootstrap_router.post("/review")
async def bootstrap_review(
    workspace_id: uuid.UUID, body: BootstrapReview, request: Request
):
    return await call(
        bootstrap_service(request).request(
            str(workspace_id),
            expected_version=body.expected_version,
            idempotency_key=str(body.idempotency_key),
            max_runtime_seconds=body.max_runtime_seconds,
        )
    )


@bootstrap_router.post("/decision")
async def bootstrap_decide(workspace_id: uuid.UUID, body: Decision, request: Request):
    return await call(
        bootstrap_service(request).decide(
            str(workspace_id), str(body.reauthorization_id), body.result
        )
    )


@bootstrap_router.post("/resume")
async def bootstrap_resume(workspace_id: uuid.UUID, body: Resume, request: Request):
    return await call(
        bootstrap_service(request).resume(
            str(workspace_id), str(body.reauthorization_id)
        )
    )


settled_bootstrap_router = APIRouter(
    prefix="/workspaces/{workspace_id}/settled-bootstrap-recovery",
    tags=["workspace-recovery"],
)


def settled_bootstrap_service(request):
    return SettledBootstrapRecoveryService(async_session_factory, _composition(request))


@settled_bootstrap_router.post("/review")
async def settled_bootstrap_review(
    workspace_id: uuid.UUID, body: BootstrapReview, request: Request
):
    return await call(
        settled_bootstrap_service(request).request(
            str(workspace_id),
            expected_version=body.expected_version,
            idempotency_key=str(body.idempotency_key),
            max_runtime_seconds=body.max_runtime_seconds,
        )
    )


@settled_bootstrap_router.post("/decision")
async def settled_bootstrap_decide(
    workspace_id: uuid.UUID, body: Decision, request: Request
):
    return await call(
        settled_bootstrap_service(request).decide(
            str(workspace_id), str(body.reauthorization_id), body.result
        )
    )


@settled_bootstrap_router.post("/resume")
async def settled_bootstrap_resume(
    workspace_id: uuid.UUID, body: Resume, request: Request
):
    return await call(
        settled_bootstrap_service(request).resume(
            str(workspace_id), str(body.reauthorization_id)
        )
    )
