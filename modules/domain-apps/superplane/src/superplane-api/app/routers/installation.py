"""Authenticated installation observations; never mounted by the public proxy."""

import os
import uuid

from fastapi import APIRouter, Depends, Request
from superplane_contracts import Submitter

from app.config import settings
from app.database import get_session
from app.middleware.auth import get_current_org
from app.installation import capabilities_async
from app.management import management_only
from app.routers.heartbeat import _authenticated_submitter

router = APIRouter(prefix="/internal")


@router.get("/installation")
async def installation_readiness(
    request: Request, submitter: Submitter = Depends(_authenticated_submitter)
):
    return {
        "release_id": os.environ.get("SUPERPLANE_RELEASE_ID"),
        "source_revision": os.environ.get("SUPERPLANE_SOURCE_REVISION"),
        "mode": "management" if management_only() else "full",
        "operation_dispatch_enabled": settings.superplane_operation_dispatch_enabled,
        "paid_admission_enabled": settings.superplane_operation_dispatch_enabled,
        "domain_auth_enforced": getattr(request.app.state, "domain_policy", None)
        is not None,
        # Exactly the four booleans, unchanged: the post-rollout recheck asserts
        # `len(capabilities) == 4` (installation/runner.py:1188). The probe detail is
        # deliberately not added here — this is an authenticated but tenant-facing
        # response, and an adapter's refusal message is the one place a provider
        # error or another tenant's identifier could have been interpolated. The
        # image-local CLI is where that detail belongs.
        "capabilities": await capabilities_async(),
        "observations": {
            key: value
            for key, value in getattr(
                request.app.state, "observation_delivery", {}
            ).items()
            if value["workspace"] in submitter.workspaces
        },
    }


@router.get(
    "/installation/workspaces/{workspace_id}/credential-evidence/{connection_id}"
)
async def installation_credential_evidence(
    request: Request,
    workspace_id: uuid.UUID,
    connection_id: uuid.UUID,
    org_id=Depends(get_current_org),
    db=Depends(get_session),
):
    """Private metadata-only positive control under the real caller's live grant."""
    from fastapi import HTTPException
    from superplane_auth.policy import Permission

    from app.auth import authorize_workspace_operation
    from app.operation_activation import dispatch_enabled
    from app.routers.provider_connections import _authorize, _load_or_404, _state

    if not management_only() or dispatch_enabled():
        raise HTTPException(
            409, "credential control requires the disabled adapter stage"
        )
    caller = getattr(request.state, "caller", None)
    if caller is None:
        raise HTTPException(403, "verified workspace identity is required")
    await authorize_workspace_operation(
        db, caller, workspace_id, Permission.RENEW_CREDENTIAL
    )
    connection, binding = await _load_or_404(db, org_id, connection_id)
    evidence = await _authorize(
        request=request,
        workspace_id=workspace_id,
        connection=connection,
        binding=binding,
        org_id=org_id,
        require_renewal=True,
    )
    state = _state(connection, binding)
    return {
        "control_version": 1,
        "org_id": str(org_id),
        "workspace_id": str(workspace_id),
        "connection_id": str(connection_id),
        "credential_id": evidence.reference.credential_id,
        "service": evidence.reference.service,
        "label": evidence.reference.label,
        "evidence_expires_at": evidence.expires_at.isoformat(),
        "connection_state": state.status.value,
        "credential_version_verified": False,
        "raw_material_returned": False,
    }
