"""Human-only knowledge observations; no UI automation or provider actions."""

from fastapi import Depends, Header, HTTPException, Query, Request, Response

from src import knowledge_evidence as evidence
from src.knowledge_evidence_contract import Complete, Observe, Prepare
from src.knowledge_evidence_inspection import operations
from src.orchestration.l5_observation_routes import human_session
from src.orchestration.l5_observation_scope import ObservationError

NEXT = {
    "knowledge_evidence_publication_not_ready": "Read the original publication. Resolve incomplete native readback before collecting knowledge use.",
    "knowledge_evidence_native_observation_required": (
        "Use the existing bounded human native UI observation for this Product and release. Only the selected person can record what they saw."
    ),
    "knowledge_evidence_source_access_revoked": (
        "Restore legitimate current access to the selected source before reading retained content. Do not substitute another source."
    ),
    "knowledge_evidence_source_expired": (
        "Retained source evidence expired. Preserve this operation and its effects for review; completion cannot replace its captured source."
    ),
    "knowledge_evidence_expired": (
        "Read retained evidence. Prepare a fresh bounded observation before a new manually initiated session; do not repeat publication."
    ),
    "knowledge_evidence_asset_changed": (
        "The before/after native Asset version or content differs from the original publication. "
        "Inspect the change; do not mark this operation qualified."
    ),
    "knowledge_evidence_inactive_requires_non_use": (
        "This selected skill is inactive. Keep its source and native readback for the separate revocation/non-use check; "
        "positive-use evidence cannot qualify an inactive skill."
    ),
    "knowledge_evidence_incomplete": "Review evidence for every selected publication before completing this original operation.",
    "knowledge_evidence_session_reused": (
        "This native conversation already belongs to retained evidence. Read that evidence instead of relabeling the session."
    ),
}


def add_routes(router, backend_dependency):
    path = "/binding-operations/{operation_id}/knowledge-evidence"

    async def invoke(operation, backend, request, response, *args, human=False):
        response.headers["Cache-Control"] = "no-store"
        try:
            actor = await human_session(request, backend.actor, backend.db)

            async def revalidate_human():
                # Server-owned callback; no request field can bypass the shared
                # logout, membership and automation-credential checks after I/O.
                current = await human_session(request, backend.actor, backend.db)
                if current != actor:
                    raise HTTPException(403, detail={"code": "l5_observation_human_required"})

            result = await operation(backend, *([actor] if human else []), *args, **({"revalidate_human": revalidate_human} if human else {}))
            if "revision" in result:
                response.headers["ETag"] = f'"{result["revision"]}"'
            return result
        except HTTPException:
            await backend.db.rollback()
            raise
        except ObservationError as error:
            await backend.db.rollback()
            raise HTTPException(
                error.status,
                detail={
                    "code": error.code,
                    "next_action": NEXT.get(
                        error.code,
                        "Read the original knowledge operation and immutable evidence. Resolve the exact missing or "
                        "changed fact before continuing; do not repeat native publication.",
                    ),
                },
            ) from None

    @router.get("/knowledge-operations")
    async def list_operations(
        product_id: str,
        project_id: str,
        service_id: str,
        request: Request,
        response: Response,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=50, ge=1, le=100),
        binding_id: str | None = Query(default=None, min_length=1, max_length=36),
        backend=Depends(backend_dependency),
    ):
        return await invoke(operations, backend, request, response, product_id, project_id, service_id, offset, limit, binding_id)

    @router.post(path + "/prepare")
    async def prepare(
        product_id: str,
        project_id: str,
        service_id: str,
        operation_id: str,
        body: Prepare,
        request: Request,
        response: Response,
        idempotency_key: str = Header(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_-]+$"),
        if_match: str = Header(pattern=r'^"[1-9][0-9]*"$'),
        backend=Depends(backend_dependency),
    ):
        return await invoke(
            evidence.prepare,
            backend,
            request,
            response,
            product_id,
            project_id,
            service_id,
            operation_id,
            body,
            idempotency_key,
            if_match,
            human=True,
        )

    @router.get(path)
    async def inspect(
        product_id: str,
        project_id: str,
        service_id: str,
        operation_id: str,
        request: Request,
        response: Response,
        backend=Depends(backend_dependency),
    ):
        return await invoke(evidence.inspect, backend, request, response, product_id, project_id, service_id, operation_id)

    @router.post(path + "/{evidence_id}/observe")
    async def observe(
        product_id: str,
        project_id: str,
        service_id: str,
        operation_id: str,
        evidence_id: str,
        body: Observe,
        request: Request,
        response: Response,
        backend=Depends(backend_dependency),
    ):
        return await invoke(
            evidence.observe, backend, request, response, product_id, project_id, service_id, operation_id, evidence_id, body, human=True
        )

    @router.get(path + "/{evidence_id}/artifacts/{kind}")
    async def artifact(
        product_id: str,
        project_id: str,
        service_id: str,
        operation_id: str,
        evidence_id: str,
        kind: str,
        request: Request,
        response: Response,
        backend=Depends(backend_dependency),
    ):
        return await invoke(evidence.read_artifact, backend, request, response, product_id, project_id, service_id, operation_id, evidence_id, kind)

    @router.post(path + "/complete")
    async def complete(
        product_id: str,
        project_id: str,
        service_id: str,
        operation_id: str,
        body: Complete,
        request: Request,
        response: Response,
        idempotency_key: str = Header(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_-]+$"),
        if_match: str = Header(pattern=r'^"[1-9][0-9]*"$'),
        backend=Depends(backend_dependency),
    ):
        return await invoke(
            evidence.complete,
            backend,
            request,
            response,
            product_id,
            project_id,
            service_id,
            operation_id,
            body,
            idempotency_key,
            if_match,
            human=True,
        )
