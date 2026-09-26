"""Public preview, adoption and durable operation recovery contracts."""

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.operation_authority_source import GrantBackedAuthority
from app.database import async_session_factory, get_session
from app.middleware.auth import get_current_org
from app.models.workspace import Workspace
from app.schemas.workspace import CreateWorkspaceRequest, WorkspaceResponse
from app.services.onboarding import policy_for, preview
from app.services.provisioning import ProvisioningRefused, ProvisioningUnavailable

router = APIRouter(tags=["workspace-onboarding"])


class PreviewWorkspaceRequest(CreateWorkspaceRequest):
    operation_id: uuid.UUID


class ContinuationPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: uuid.UUID


class ContinuationRequest(ContinuationPreviewRequest):
    approval_id: uuid.UUID


def _composition(request):
    composition = getattr(request.app.state, "trust_composition", None)
    if composition is None:
        raise HTTPException(503, "operation authority is unavailable")
    return composition


@router.get("/workspaces/{workspace_id}/lifecycle-proposals")
async def lifecycle_proposals(
    workspace_id: uuid.UUID,
    request: Request,
    org_id: uuid.UUID = Depends(get_current_org),
    db: AsyncSession = Depends(get_session),
):
    from app.services.lifecycle_proposals import verified_proposal, workspace_scope
    from workspace_provisioning.artifacts import proposal
    from workspace_provisioning.runtime_config import LifecycleRefused

    composition = _composition(request)
    try:
        workspace, _ = await workspace_scope(db, org_id, workspace_id)
        async with composition.operation_connect() as connection:
            rows = await connection.fetch(
                "SELECT artifact_id FROM workspace_lifecycle_artifacts WHERE org_id=$1 AND workspace_id=$2 "
                "AND source_operation_id=$3 ORDER BY created_at DESC LIMIT 20",
                str(org_id),
                str(workspace_id),
                workspace.provisioning_operation_id,
            )
        proposals = []
        for row in rows:
            try:
                result = proposal(
                    await verified_proposal(
                        composition, org_id, workspace_id, row["artifact_id"]
                    )
                )
                # Completed bootstrap artifacts remain durable recovery evidence,
                # but are not plans a caller can approve or continue.
                if result["status"] == "awaiting_plan_approval":
                    proposals.append(result)
            except LifecycleRefused:
                continue
        return {"workspace_id": str(workspace_id), "proposals": proposals}
    except ProvisioningRefused:
        raise HTTPException(403, "workspace lifecycle access refused") from None
    except Exception:
        raise HTTPException(503, "workspace lifecycle proposals unavailable") from None


@router.post("/workspaces/{workspace_id}/lifecycle-proposals/{artifact_id}/preview")
async def preview_lifecycle_continuation(
    workspace_id: uuid.UUID,
    artifact_id: str,
    body: ContinuationPreviewRequest,
    request: Request,
    org_id: uuid.UUID = Depends(get_current_org),
    db: AsyncSession = Depends(get_session),
):
    from app.services.lifecycle_proposals import preview_continuation
    from workspace_provisioning.runtime_config import LifecycleRefused

    try:
        *_, result = await preview_continuation(
            _composition(request),
            db,
            org_id,
            workspace_id,
            artifact_id,
            body.operation_id,
        )
        return result
    except (ProvisioningRefused, LifecycleRefused) as error:
        raise HTTPException(409, str(error)) from None
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503, "workspace lifecycle preview unavailable") from None


@router.post("/workspaces/{workspace_id}/lifecycle-proposals/{artifact_id}/continue")
async def continue_workspace_lifecycle(
    workspace_id: uuid.UUID,
    artifact_id: str,
    body: ContinuationRequest,
    request: Request,
    org_id: uuid.UUID = Depends(get_current_org),
    db: AsyncSession = Depends(get_session),
):
    from app.services.lifecycle_proposals import continue_lifecycle
    from workspace_provisioning.runtime_config import LifecycleRefused

    try:
        return await continue_lifecycle(
            _composition(request),
            db,
            org_id,
            workspace_id,
            artifact_id,
            body.operation_id,
            body.approval_id,
        )
    except (ProvisioningRefused, LifecycleRefused) as error:
        raise HTTPException(409, str(error)) from None
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(
            503, "workspace lifecycle admission unavailable; retain request identity"
        ) from None


@router.get("/capabilities")
async def onboarding_capabilities(
    request: Request, org_id: uuid.UUID = Depends(get_current_org)
):
    from workspace_provisioning.runtime_config import (
        LifecycleRefused,
        supported_runtime_modes,
        validate_runtime_config,
    )

    composition = getattr(request.app.state, "trust_composition", None)
    dispatcher = getattr(composition, "dispatcher", None)
    try:
        policy = policy_for(org_id)
        modes = set(policy.permitted_modes) & supported_runtime_modes(
            validate_runtime_config(policy.runtime)
        )
        ready = dispatcher is not None and await dispatcher.ready(str(org_id))
        ready = ready and bool(modes)
    except (ProvisioningRefused, ProvisioningUnavailable, LifecycleRefused):
        policy, modes, ready = None, set(), False
    return {
        "version": 1,
        "features": ["provider-connection-operation-id-v1"]
        + (["create-operation-id-v1", "adopt-operation-id-v1"] if ready else []),
        "modes": sorted(modes) if ready else [],
        "providers": ["aws"] if ready else [],
        "isolation_modes": sorted(policy.isolation_modes) if ready else [],
        "ready": bool(ready),
    }


@router.post("/workspaces/preview")
async def preview_workspace(
    body: PreviewWorkspaceRequest,
    org_id: uuid.UUID = Depends(get_current_org),
    db: AsyncSession = Depends(get_session),
):
    from account_factory.modes import ModeError

    try:
        return await preview(db, org_id, body)
    except ProvisioningRefused as error:
        raise HTTPException(403, str(error)) from None
    except ProvisioningUnavailable as error:
        raise HTTPException(503, str(error)) from None
    except (ModeError, ValueError, TypeError):
        raise HTTPException(422, "workspace preview inputs are invalid") from None


@router.post("/workspaces/adopt", response_model=WorkspaceResponse, status_code=201)
async def adopt_workspace(
    body: CreateWorkspaceRequest,
    org_id: uuid.UUID = Depends(get_current_org),
    db: AsyncSession = Depends(get_session),
):
    from app.routers.workspaces import create_workspace

    if body.mode != "adopt" or not body.cluster_reference:
        raise HTTPException(422, "adoption requires an existing cluster and adopt mode")
    return await create_workspace(body, org_id, db)


async def operation_response(request, db, org_id, identity, *, by_request):
    from harness_jobs.identity import decode_payload, payload_digest

    composition = getattr(request.app.state, "trust_composition", None)
    if composition is None:
        raise HTTPException(503, "operation authority is unavailable")
    field = "idempotency_key" if by_request else "operation_id"
    try:
        async with composition.operation_connect() as connection:
            rows = await connection.fetch(
                f"SELECT * FROM harness_operations WHERE org_id=$1 AND {field}=$2 LIMIT 2",
                str(org_id),
                identity,
            )
            if len(rows) != 1:
                raise HTTPException(404, "operation not found")
            row = rows[0]
            principal = await GrantBackedAuthority(async_session_factory).resolve(
                org_id=str(org_id),
                workspace_id=row["workspace_id"],
                permission="workspace:provision",
            )
            if principal is None:
                raise HTTPException(403, "operation access refused")
            admitted = decode_payload(row["request_payload"])
            if payload_digest(admitted) != row["plan_digest"]:
                raise HTTPException(
                    503, "stored operation request could not be verified"
                )
            workspace = await db.scalar(
                select(Workspace).where(
                    Workspace.id == uuid.UUID(row["workspace_id"]),
                    Workspace.org_id == org_id,
                )
            )
            return {
                "request_id": row["idempotency_key"],
                "provisioning_operation_id": row["operation_id"],
                "workspace_id": str(workspace.id) if workspace is not None else None,
                "state": row["state"],
                "phase": "execution"
                if workspace is not None
                else "workspace_registration",
                "reason": row.get("detail"),
                "observed_at": datetime.now(UTC),
                "retryable": False,
            }
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503, "operation authority is unavailable") from None


@router.get("/operations/by-idempotency/{idempotency_key}")
async def recover_operation(
    idempotency_key: uuid.UUID,
    request: Request,
    org_id: uuid.UUID = Depends(get_current_org),
    db: AsyncSession = Depends(get_session),
):
    return await operation_response(
        request, db, org_id, str(idempotency_key), by_request=True
    )


@router.get("/operations/{operation_id}")
async def get_operation(
    operation_id: str,
    request: Request,
    org_id: uuid.UUID = Depends(get_current_org),
    db: AsyncSession = Depends(get_session),
):
    return await operation_response(request, db, org_id, operation_id, by_request=False)
