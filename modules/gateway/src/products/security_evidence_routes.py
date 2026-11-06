"""Local Product consent and evidence routes; no orchestration row is read."""

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from src.orchestration.l5_observation_scope import ObservationError

from . import security_evidence_local as local
from .security_evidence_contract import Consent, LocalExport, Observation, Preview, Registration

NEXT_ACTION = {
    "security_evidence_request_expired": (
        "Ask the same running release for an exact renewal within its original lease; completed saves must be reconciled, never repeated."
    ),
    "security_evidence_grant_expired": (
        "The Product owner must endorse a fresh exact renewal before another export. Retained save and observation history remains available."
    ),
    "security_evidence_original_lease_expired": (
        "The original release lease ended. Preserve receipts for review; this request cannot authorize another save, submission or transfer."
    ),
    "security_evidence_challenge_expired": (
        "The human observation window ended. Read retained receipts and obtain a fresh exact request before further human action."
    ),
    "security_evidence_fixture_registry_unavailable": (
        "Restore the protected fixture identity registry before selecting a human. Do not use an automation credential as a fallback."
    ),
    "security_evidence_reader_changed": (
        "Use the exact API fixture credential registered and endorsed for this request, with current Product membership and session."
    ),
    "security_evidence_owner_changed": (
        "Ownership changed. The current legitimate Product owner must establish new configuration/source authority "
        "and explicitly endorse a fresh request."
    ),
    "security_evidence_pending": (
        "The selected owner or observer has not completed this request. Open its request ID in Security configuration to see the outstanding step."
    ),
    "security_evidence_interactive_session_required": (
        "Sign in separately to ADP with the selected human account. Fixture credentials cannot record human actions."
    ),
    "security_evidence_already_attempted": "Read the retained before and after receipts. This save may have committed; do not repeat it.",
    "security_evidence_scope_changed": (
        "Refresh the exact configuration, binding and run. Ask the current release job for a new request and endorse its fresh snapshot."
    ),
    "security_evidence_grant_revoked": "The Product owner revoked this request. Its retained history remains readable but cannot be exported.",
}


def create_router(dependency):
    router = APIRouter(prefix="/v1/products/{product_id}/projects/{project_id}/services/{service_id}/release-evidence", tags=["products"])

    @router.get("/identity")
    async def identity(product_id: str, project_id: str, service_id: str, request: Request, response: Response, backend=Depends(dependency)):
        if service_id != "security":
            raise HTTPException(404, detail={"code": "not_found"})
        product, _ = await local.member_access(backend.db, backend.actor.org_id, product_id, project_id, backend.actor.user_id)
        response.headers["Cache-Control"] = "no-store"
        from src.auth.security_fixture_credential import require_human_subject

        eligibility = "available"
        try:
            await require_human_subject(request, product.owner_user_id)
        except HTTPException as error:
            if error.status_code != 403:
                raise
            eligibility = "designated_automation_account"
        return {
            "tenant_id": backend.actor.org_id,
            "user_id": backend.actor.user_id,
            "owner_id": product.owner_user_id,
            "human_owner_eligibility": eligibility,
        }

    @router.get("/edits/{receipt_id}")
    async def edit_receipt(product_id: str, project_id: str, service_id: str, receipt_id: str, response: Response, backend=Depends(dependency)):
        from l5_services.api.configuration import view
        from l5_services.models.configuration import ConfigurationReceipt, ConfigurationVersion

        if service_id != "security":
            raise HTTPException(404, detail={"code": "not_found"})
        config, _ = await backend.current(product_id, project_id, service_id)
        receipt = await backend.db.get(
            ConfigurationReceipt, (backend.actor.org_id, product_id, project_id, service_id, backend.actor.user_id, "edit", receipt_id)
        )
        if receipt is None or receipt.configuration_id != config.configuration_id:
            raise HTTPException(404, detail={"code": "security_edit_receipt_unavailable"})
        version = await backend.db.get(ConfigurationVersion, (backend.actor.org_id, config.configuration_id, receipt.revision))
        before = await backend.db.get(ConfigurationVersion, (backend.actor.org_id, config.configuration_id, receipt.revision - 1))
        if version is None or before is None:
            raise HTTPException(409, detail={"code": "security_edit_receipt_unverifiable"})
        await backend.authorize_sources(version, project_id)
        response.headers["Cache-Control"] = "no-store"
        return {"configuration": view(config, version), "request_sha256": receipt.request_hash, "before_hash": before.hash}

    @router.get("/provider/{binding_id}")
    async def provider(
        product_id: str, project_id: str, service_id: str, binding_id: str, request: Request, response: Response, backend=Depends(dependency)
    ):
        if service_id != "security":
            raise HTTPException(404, detail={"code": "not_found"})
        from .security_provider_authority import read

        response.headers["Cache-Control"] = "no-store"
        return await read(backend, product_id, project_id, binding_id, request)

    async def invoke(operation, backend, request, response, product_id, project_id, service_id, *args):
        response.headers["Cache-Control"] = "no-store"
        if service_id != "security":
            raise HTTPException(404, detail={"code": "not_found"})
        try:
            result = await operation(backend, request, product_id, project_id, *args)
            await backend.db.commit()
            return result
        except ObservationError as error:
            await backend.db.rollback()
            raise HTTPException(
                error.status,
                detail={
                    "code": error.code,
                    "next_action": NEXT_ACTION.get(
                        error.code,
                        "Read the selected Security request and retained receipts. Restore its current owner, reader and release "
                        "authority before continuing.",
                    ),
                },
            ) from None

    @router.get("/requests/{request_id}")
    async def status(
        product_id: str, project_id: str, service_id: str, request_id: str, request: Request, response: Response, backend=Depends(dependency)
    ):
        return await invoke(local.read_status, backend, request, response, product_id, project_id, service_id, request_id)

    @router.post("/requests/{request_id}/credentials")
    async def register(
        product_id: str,
        project_id: str,
        service_id: str,
        request_id: str,
        body: Registration,
        request: Request,
        response: Response,
        backend=Depends(dependency),
    ):
        return await invoke(local.register, backend, request, response, product_id, project_id, service_id, request_id, body.slot)

    @router.post("/requests/{request_id}/preview")
    async def preview(
        product_id: str,
        project_id: str,
        service_id: str,
        request_id: str,
        request: Request,
        response: Response,
        body: Preview | None = None,
        backend=Depends(dependency),
    ):
        return await invoke(
            local.preview, backend, request, response, product_id, project_id, service_id, request_id, body.observer_id if body else None
        )

    @router.post("/requests/{request_id}/endorse")
    async def endorse(
        product_id: str,
        project_id: str,
        service_id: str,
        request_id: str,
        body: Consent,
        request: Request,
        response: Response,
        backend=Depends(dependency),
    ):
        return await invoke(local.endorse, backend, request, response, product_id, project_id, service_id, request_id, body)

    @router.post("/requests/{request_id}/prepare")
    async def prepare(
        product_id: str, project_id: str, service_id: str, request_id: str, request: Request, response: Response, backend=Depends(dependency)
    ):
        return await invoke(local.prepare, backend, request, response, product_id, project_id, service_id, request_id)

    @router.post("/requests/{request_id}/observe")
    async def observe(
        product_id: str,
        project_id: str,
        service_id: str,
        request_id: str,
        body: Observation,
        request: Request,
        response: Response,
        backend=Depends(dependency),
    ):
        return await invoke(local.observe, backend, request, response, product_id, project_id, service_id, request_id, body)

    @router.post("/requests/{request_id}/revoke")
    async def revoke(
        product_id: str, project_id: str, service_id: str, request_id: str, request: Request, response: Response, backend=Depends(dependency)
    ):
        return await invoke(local.revoke, backend, request, response, product_id, project_id, service_id, request_id)

    @router.post("/requests/{request_id}/reconcile")
    async def reconcile(
        product_id: str, project_id: str, service_id: str, request_id: str, request: Request, response: Response, backend=Depends(dependency)
    ):
        from .security_evidence_effect import reconcile

        return await invoke(reconcile, backend, request, response, product_id, project_id, service_id, request_id)

    @router.post("/exports")
    async def export(
        product_id: str, project_id: str, service_id: str, body: LocalExport, request: Request, response: Response, backend=Depends(dependency)
    ):
        return await invoke(local.export_local, backend, request, response, product_id, project_id, service_id, body.ticket_id)

    return router
