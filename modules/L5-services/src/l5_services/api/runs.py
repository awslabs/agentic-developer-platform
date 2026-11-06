"""Public run submission contract; admission is delegated to a scoped backend."""

import re

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response

from l5_services.api.availability import reject_unqualified_transform
from l5_services.models.run import view_run
from l5_services.run_input import validate_run_input


def validate_mutation(service_id, body, idempotency_key, *, allow_devops=False, allow_finops=False,
                      allow_wa=False, backend=None):
    if service_id == "transform" and getattr(backend, "transform_submission_ready", False) is not True:
        reject_unqualified_transform(service_id)
    if (service_id != "security" and not (allow_devops and service_id == "devops")
            and not (allow_finops and service_id == "finops")
            and not (allow_wa and service_id == "well_architected")
            and not (backend is not None and service_id == "transform")):
        raise HTTPException(404, detail={"code": "not_found"})
    if idempotency_key is None:
        raise HTTPException(428, detail={"code": "precondition_required"})
    if re.fullmatch(r"[A-Za-z0-9_-]{16,128}", idempotency_key) is None:
        raise HTTPException(422, detail={"code": "validation_error"})
    reason = body.get("reason")
    if set(body) != {"reason"} or not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
        raise HTTPException(422, detail={"code": "validation_error"})
    return reason


def mutation_response(run, response):
    result = view_run(run)
    response.headers["ETag"] = f'"{result["revision"]}"'
    response.headers["Cache-Control"] = "no-store"
    return result


def create_runs_router(backend_dependency):
    router = APIRouter(tags=["l5-security"])

    @router.get("/qualification-prerequisites", operation_id="getWellArchitectedQualificationPrerequisites")
    async def qualification_prerequisites(product_id: str, project_id: str, service_id: str,
                                         response: Response, backend=Depends(backend_dependency)):
        if service_id != "well_architected":
            raise HTTPException(404, detail={"code": "not_found"})
        response.headers["Cache-Control"] = "no-store"
        return await backend.wa_qualification_prerequisites(product_id, project_id)

    @router.post("/runs", operation_id="submitRun", status_code=202)
    async def submit_run(
        product_id: str, project_id: str, service_id: str, run_input: dict, response: Response,
        idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
        backend=Depends(backend_dependency),
    ):
        if service_id == "transform" and getattr(backend, "transform_submission_ready", False) is not True:
            if callable(getattr(backend, "current", None)):
                await backend.current(product_id, project_id, service_id)
            reject_unqualified_transform(service_id)
        if service_id not in {"security", "devops", "finops", "well_architected", "transform"}:
            raise HTTPException(404, detail={"code": "not_found"})
        if idempotency_key is None:
            raise HTTPException(428, detail={"code": "precondition_required"})
        if re.fullmatch(r"[A-Za-z0-9_-]{16,128}", idempotency_key) is None:
            raise HTTPException(422, detail={"code": "validation_error"})
        # Syntax only: the backend permits the old shape solely for an exact
        # existing receipt. New admission always applies the current contract.
        validate_run_input(run_input, allow_historical=True)
        if run_input.get("service_id", "security") != service_id:
            raise HTTPException(422, detail={"code": "validation_error"})
        run = await backend.submit(product_id, project_id, service_id, run_input, idempotency_key)
        response.headers["ETag"] = f'"{run.revision}"'
        response.headers["Cache-Control"] = "no-store"
        return view_run(run)

    @router.get("/runs", operation_id="listRuns")
    async def list_runs(
        product_id: str, project_id: str, service_id: str, response: Response,
        limit: int = Query(25, ge=1, le=100), cursor: str | None = Query(None, min_length=1, max_length=2048),
        backend=Depends(backend_dependency),
    ):
        if service_id not in {"security", "devops", "finops", "well_architected", "transform"}:
            raise HTTPException(404, detail={"code": "not_found"})
        if service_id == "finops" and cursor is not None:
            await backend.get_run(product_id, project_id, service_id, cursor)
        runs = await backend.list_runs(product_id, project_id, service_id, cursor)
        page = runs[:limit]
        response.headers["Cache-Control"] = "no-store"
        result = {"items": [view_run(run) for run in page]}
        if len(runs) > limit:
            result["next_cursor"] = page[-1].run_id
        return result

    @router.get("/runs/{run_id}", operation_id="getRun")
    async def get_run(product_id: str, project_id: str, service_id: str, run_id: str,
                      response: Response, backend=Depends(backend_dependency)):
        if service_id not in {"security", "devops", "finops", "well_architected", "transform"}:
            raise HTTPException(404, detail={"code": "not_found"})
        run = await backend.get_run(product_id, project_id, service_id, run_id)
        response.headers["ETag"] = f'"{run.revision}"'
        response.headers["Cache-Control"] = "no-store"
        return view_run(run)

    @router.get("/runs/{run_id}/native-observations", operation_id="getWellArchitectedNativeObservations")
    async def native_observations(product_id: str, project_id: str, service_id: str, run_id: str,
                                  response: Response, backend=Depends(backend_dependency)):
        if service_id != "well_architected":
            raise HTTPException(404, detail={"code": "not_found"})
        response.headers["Cache-Control"] = "no-store"
        return await backend.wa_native_observations(product_id, project_id, run_id)

    @router.get("/runs/{run_id}/cleanup-observations", operation_id="getWellArchitectedCleanupObservations")
    async def cleanup_observations(product_id: str, project_id: str, service_id: str, run_id: str,
                                  response: Response, backend=Depends(backend_dependency)):
        if service_id != "well_architected":
            raise HTTPException(404, detail={"code": "not_found"})
        response.headers["Cache-Control"] = "no-store"
        return await backend.wa_cleanup_observations(product_id, project_id, run_id)

    @router.get("/runs/{run_id}/source-manifest", operation_id="getRunSourceManifest")
    async def source_manifest(product_id: str, project_id: str, service_id: str, run_id: str,
                              response: Response, limit: int | None = Query(None, ge=1, le=1000),
                              cursor: str | None = Query(None, min_length=1, max_length=128),
                              backend=Depends(backend_dependency)):
        if service_id not in {"security", "devops", "finops", "well_architected", "transform"}:
            raise HTTPException(404, detail={"code": "not_found"})
        response.headers["Cache-Control"] = "no-store"
        if limit is None and cursor is None:
            return await backend.source_manifest(product_id, project_id, service_id, run_id)
        return await backend.source_manifest(product_id, project_id, service_id, run_id, limit=limit, cursor=cursor)

    @router.get("/runs/{run_id}/source-snapshots/{snapshot_id}", operation_id="getRunSourceSnapshot")
    async def source_snapshot(product_id: str, project_id: str, service_id: str, run_id: str, snapshot_id: str,
                              response: Response, backend=Depends(backend_dependency)):
        if service_id not in {"security", "devops", "finops", "well_architected", "transform"}:
            raise HTTPException(404, detail={"code": "not_found"})
        response.headers["Cache-Control"] = "no-store"
        return await backend.source_snapshot(product_id, project_id, service_id, run_id, snapshot_id, "receipt")

    @router.get("/runs/{run_id}/source-snapshots/{snapshot_id}/{part}", operation_id="readRunSourceSnapshot")
    async def source_snapshot_bytes(product_id: str, project_id: str, service_id: str, run_id: str,
                                    snapshot_id: str, part: str, request: Request, backend=Depends(backend_dependency)):
        if service_id not in {"security", "devops", "finops", "well_architected", "transform"} or part not in {"content", "manifest"}:
            raise HTTPException(404, detail={"code": "not_found"})
        values = request.headers.getlist("range")
        if not values:
            return await backend.source_snapshot(product_id, project_id, service_id, run_id, snapshot_id, part)
        # Joining duplicates makes the backend reject them after checking access.
        return await backend.source_snapshot(product_id, project_id, service_id, run_id, snapshot_id, part, range_header=",".join(values))

    @router.get("/runs/{run_id}/native-readback/{action}", operation_id="readSecurityNativeJob")
    async def native_readback(product_id: str, project_id: str, service_id: str, run_id: str,
                              action: str, response: Response, backend=Depends(backend_dependency)):
        if service_id != "security" or action not in {"job", "inventory"}:
            raise HTTPException(404, detail={"code": "not_found"})
        response.headers["Cache-Control"] = "no-store"
        return await backend.native_security_readback(product_id, project_id, run_id, action)

    @router.get("/runs/{run_id}/native-readback/spaces/{owned_binding_id}",
                operation_id="readSecurityOwnedSpaceCleanup")
    async def native_spaces_readback(product_id: str, project_id: str, service_id: str, run_id: str,
                                     owned_binding_id: str, response: Response,
                                     backend=Depends(backend_dependency)):
        if service_id != "security":
            raise HTTPException(404, detail={"code": "not_found"})
        response.headers["Cache-Control"] = "no-store"
        return await backend.native_security_readback(product_id, project_id, run_id, "spaces",
                                                     owned_binding_id=owned_binding_id)

    @router.get("/runs/receipts/{receipt_id}", operation_id="readSecuritySubmissionReceipt")
    async def submission_receipt(product_id: str, project_id: str, service_id: str, receipt_id: str,
                                 response: Response, backend=Depends(backend_dependency)):
        if service_id != "security" or re.fullmatch(r"[A-Za-z0-9_-]{16,128}", receipt_id) is None:
            raise HTTPException(404, detail={"code": "not_found"})
        response.headers["Cache-Control"] = "no-store"
        return await backend.security_submission_receipt(product_id, project_id, receipt_id)

    @router.get("/runs/{run_id}/recovery-state", operation_id="readSecurityRecoveryState")
    async def recovery_state(product_id: str, project_id: str, service_id: str, run_id: str,
                             response: Response, backend=Depends(backend_dependency)):
        if service_id not in {"security", "devops"}:
            raise HTTPException(404, detail={"code": "not_found"})
        response.headers["Cache-Control"] = "no-store"
        if service_id == "devops":
            return await backend.devops_recovery_state(product_id, project_id, run_id)
        return await backend.security_recovery_state(product_id, project_id, run_id)

    @router.get("/runs/{run_id}/publications/{publication_id}", operation_id="readSecurityPublicationEvidence")
    async def publication_evidence(product_id: str, project_id: str, service_id: str, run_id: str,
                                   publication_id: str, response: Response,
                                   backend=Depends(backend_dependency)):
        if service_id != "security":
            raise HTTPException(404, detail={"code": "not_found"})
        response.headers["Cache-Control"] = "no-store"
        return await backend.security_publication(product_id, project_id, run_id, publication_id)

    @router.post("/runs/{run_id}/cancel", operation_id="cancelRun", status_code=202)
    async def cancel_run(
        product_id: str, project_id: str, service_id: str, run_id: str, body: dict, response: Response,
        idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
        backend=Depends(backend_dependency),
    ):
        if service_id == "transform" and getattr(backend, "transform_submission_ready", False) is not True:
            if callable(getattr(backend, "current", None)):
                await backend.current(product_id, project_id, service_id)
        reason = validate_mutation(service_id, body, idempotency_key, allow_devops=True, allow_finops=True,
                                   allow_wa=True, backend=backend)
        run = await backend.cancel_run(product_id, project_id, service_id, run_id, reason, idempotency_key)
        return mutation_response(run, response)

    @router.post("/runs/{run_id}/reconcile", operation_id="reconcileRun", status_code=202)
    async def reconcile_run(
        product_id: str, project_id: str, service_id: str, run_id: str, body: dict, response: Response,
        idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
        backend=Depends(backend_dependency),
    ):
        if service_id == "devops" and body.get("authorize_readback") is True:
            reason = validate_mutation(service_id, {"reason": body.get("reason")}, idempotency_key, allow_devops=True)
            if (set(body) != {"reason", "authorize_readback", "expected_revision", "native_job_ref"}
                    or type(body.get("expected_revision")) is not int or body["expected_revision"] < 1
                    or not isinstance(body.get("native_job_ref"), dict) or not body["native_job_ref"]):
                raise HTTPException(422, detail={"code": "validation_error"})
            run = await backend.recover_devops_report(product_id, project_id, run_id, {**body, "reason": reason}, idempotency_key)
            return mutation_response(run, response)
        if service_id == "transform" and getattr(backend, "transform_submission_ready", False) is not True:
            if callable(getattr(backend, "current", None)):
                await backend.current(product_id, project_id, service_id)
        reason = validate_mutation(service_id, body, idempotency_key, allow_devops=True, allow_finops=True,
                                   allow_wa=True, backend=backend)
        run = await backend.reconcile_run(product_id, project_id, service_id, run_id, reason, idempotency_key)
        return mutation_response(run, response)

    @router.post("/runs/{run_id}/retry", operation_id="retryRun", status_code=202)
    async def retry_run(
        product_id: str, project_id: str, service_id: str, run_id: str, body: dict, response: Response,
        idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
        backend=Depends(backend_dependency),
    ):
        reason = validate_mutation(service_id, body, idempotency_key)
        run = await backend.retry_run(product_id, project_id, service_id, run_id, reason, idempotency_key)
        return mutation_response(run, response)

    return router
