"""Authenticated installation observations; never mounted by the public proxy."""

import os
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from superplane_contracts import Submitter

from app.config import settings
from app.database import get_session
from app.installation import (
    capabilities_async,
    prepared_lifecycle_binding,
    runtime_dependencies,
)
from app.management import management_only
from app.middleware.auth import get_current_org
from app.routers.heartbeat import _authenticated_submitter

router = APIRouter(prefix="/internal")


@router.get("/installation")
async def installation_readiness(
    request: Request,
    submitter: Submitter = Depends(_authenticated_submitter),
    org_id: uuid.UUID | None = None,
):
    """Distinguish locally prepared from installed proof for an authorized org."""
    if org_id is not None and not (
        "budget_monitor/global" in getattr(submitter, "lease_scopes", ())
        or f"controller_management/{org_id}" in getattr(submitter, "lease_scopes", ())
    ):
        raise HTTPException(403, "installation organization scope required")
    composition = getattr(request.app.state, "trust_composition", None)
    dependencies = await runtime_dependencies(composition)
    prepared = await prepared_lifecycle_binding(composition, dependencies)
    executable = False
    if prepared and org_id is not None:
        from app.operation_activation import expected_lifecycle_binding

        try:
            executable = await composition.dispatcher.binding_ready(
                str(org_id), expected_lifecycle_binding()
            ) and await prepared_lifecycle_binding(composition)
        except Exception:
            executable = False
    capabilities = await capabilities_async()
    for port, dependency in (
        ("provider_authority", "operation_store"),
        ("allocation_inventory", "operation_store"),
    ):
        capabilities[port] = capabilities[port] and dependencies[dependency]
    capabilities["operation_facade"] = capabilities["operation_facade"] and all(
        dependencies.values()
    )
    executable = executable and all(capabilities.values())
    return {
        "release_id": os.environ.get("SUPERPLANE_RELEASE_ID"),
        "source_revision": os.environ.get("SUPERPLANE_SOURCE_REVISION"),
        "mode": "management" if management_only() else "full",
        "operation_dispatch_enabled": settings.superplane_operation_dispatch_enabled,
        "paid_admission_enabled": executable,
        "paid_worker_binding": {"prepared": prepared, "executable": executable},
        "domain_auth_enforced": getattr(request.app.state, "domain_policy", None)
        is not None,
        # Exactly the four booleans, unchanged: the post-rollout recheck asserts
        # `len(capabilities) == 4` (installation/runner.py:1188). The probe detail is
        # deliberately not added here — this is an authenticated but tenant-facing
        # response, and an adapter's refusal message is the one place a provider
        # error or another tenant's identifier could have been interpolated. The
        # image-local CLI is where that detail belongs.
        "capabilities": capabilities,
        "dependencies": dependencies,
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
    from sqlalchemy import select
    from superplane_auth.policy import Permission

    from app.auth import authorize_workspace_operation
    from app.models.provider_connection import (
        ProviderConnection,
        ProviderConnectionBinding,
    )
    from app.operation_activation import dispatch_enabled
    from app.routers.provider_connections import (
        _authorize,
        _load_or_404,
        _mutation_authority,
        _require_binding,
        _state,
    )

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
    # Do not hold a connection lock across the external vault request. Capture
    # immutable input state, then re-read and lock all release authority after it.
    connection = await db.scalar(
        select(ProviderConnection)
        .where(
            ProviderConnection.id == connection_id,
            ProviderConnection.org_id == org_id,
        )
        .execution_options(populate_existing=True)
    )
    binding = await db.scalar(
        select(ProviderConnectionBinding)
        .where(
            ProviderConnectionBinding.connection_id == connection_id,
        )
        .execution_options(populate_existing=True)
    )
    if connection is None or binding is None:
        raise HTTPException(404, "provider connection not found")
    original_state = _state(connection, binding)
    original_binding_id = binding.id
    evidence = await _authorize(
        request=request,
        workspace_id=workspace_id,
        connection=connection,
        binding=binding,
        org_id=org_id,
        require_renewal=True,
    )
    # Roll back only the read-only lookup transaction, so no cached ORM grant
    # or binding can survive the await. The route performs no durable writes.
    await db.rollback()
    connection, binding = await _load_or_404(db, org_id, connection_id)
    await db.refresh(binding, with_for_update=True)
    state = _state(connection, binding)
    _require_binding(workspace_id=workspace_id, state=state, binding=binding)
    if binding.id != original_binding_id or state != original_state:
        raise HTTPException(403, "credential connection changed during verification")
    await _mutation_authority(
        db, request, org_id, workspace_id, connection.provider, evidence
    )
    await authorize_workspace_operation(
        db, caller, workspace_id, Permission.RENEW_CREDENTIAL
    )
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
