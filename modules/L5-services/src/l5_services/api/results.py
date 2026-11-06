"""Public result readback; the host authorizes runs before constructing the store."""

import re
from datetime import UTC, timedelta
from urllib.parse import quote

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response

from l5_services.api.availability import reject_unqualified_transform
from l5_services.triage import append_triage, triage_history


def create_results_router(backend_dependency):
    router = APIRouter(tags=["l5-security"])

    async def scoped(backend, product_id, project_id, service_id, *, cost_read=False,
                     artifact_read=False, contribution_read=False):
        if service_id == "well_architected":
            store = await backend.result_store(product_id, project_id, service_id)
            runs = getattr(store, "runs", {})
            if (getattr(store, "wa_reader_ready", False) is not True or not runs
                    or getattr(store, "service_id", None) != service_id
                    or len({run.tenant_id for run in runs.values()}) != 1
                    or any((run.product_id, run.project_id, run.service_id) != (
                        product_id, project_id, service_id) for run in runs.values())):
                raise HTTPException(404, detail={"code": "not_found"})
            return store
        if service_id == "transform":
            if contribution_read:
                store = await backend.result_store(product_id, project_id, service_id)
                if getattr(store, "transform_contribution_reader_ready", False) is not True:
                    raise HTTPException(404, detail={"code": "not_found"})
                return store
            if not (cost_read or artifact_read) or getattr(backend, "transform_result_reader_ready", False) is not True:
                if callable(getattr(backend, "current", None)):
                    await backend.current(product_id, project_id, service_id)
                reject_unqualified_transform(service_id)
            store = await backend.result_store(product_id, project_id, service_id)
            if getattr(store, "transform_reader_ready", False) is not True:
                reject_unqualified_transform(service_id)
            return store
        if service_id not in ({"security", "devops", "finops"} if cost_read else {"security", "devops"}):
            raise HTTPException(404, detail={"code": "not_found"})
        return await backend.result_store(product_id, project_id, service_id)

    @router.get("/results", operation_id="listResults")
    async def list_results(product_id: str, project_id: str, service_id: str, response: Response,
                           limit: int = Query(25, ge=1, le=100),
                           cursor: str | None = Query(None, min_length=1, max_length=2048),
                           backend=Depends(backend_dependency)):
        store = await scoped(backend, product_id, project_id, service_id, cost_read=True)
        if service_id == "finops" and cursor is not None:
            await store.get(cursor)
        records = await store.list(cursor)
        page = records[:limit]
        response.headers["Cache-Control"] = "no-store"
        output = {"items": page}
        if len(records) > limit:
            output["next_cursor"] = page[-1]["result_id"]
        return output

    @router.get("/results/{result_id}", operation_id="getResult")
    async def get_result(product_id: str, project_id: str, service_id: str, result_id: str,
                         response: Response, backend=Depends(backend_dependency)):
        store = await scoped(backend, product_id, project_id, service_id, cost_read=True)
        result = await store.get(result_id)
        response.headers["ETag"] = f'"{result["revision"]}"'
        response.headers["Cache-Control"] = "no-store"
        return result

    @router.get("/results/{result_id}/native-answer", operation_id="getFinOpsNativeAnswer")
    async def native_answer(product_id: str, project_id: str, service_id: str, result_id: str,
                            response: Response, revision: int = Query(..., ge=1),
                            backend=Depends(backend_dependency)):
        if service_id != "finops":
            raise HTTPException(404, detail={"code": "not_found"})
        store = await scoped(backend, product_id, project_id, service_id, cost_read=True)
        answer = await store.finops_answer(result_id, revision)
        response.headers["Cache-Control"] = "no-store"
        return answer

    @router.post("/results/{result_id}/design-usage", operation_id="useWellArchitectedResult")
    async def use_well_architected_result(product_id: str, project_id: str, service_id: str,
                                          result_id: str, body: dict, response: Response,
                                          backend=Depends(backend_dependency)):
        if service_id != "well_architected":
            raise HTTPException(404, detail={"code": "not_found"})
        if (set(body) != {"task_id"} or not isinstance(body["task_id"], str)
                or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", body["task_id"]) is None):
            raise HTTPException(422, detail={"code": "validation_error"})
        store = await scoped(backend, product_id, project_id, service_id)
        result = await store.use_wa_result(result_id, body["task_id"])
        response.headers["Cache-Control"] = "no-store"
        return result

    @router.get("/results/{result_id}/publication-status", operation_id="getResultPublicationStatus")
    async def get_result_publication_status(product_id: str, project_id: str, service_id: str,
                                            result_id: str, response: Response,
                                            backend=Depends(backend_dependency)):
        if service_id not in {"finops", "well_architected"}:
            raise HTTPException(404, detail={"code": "not_found"})
        store = await scoped(backend, product_id, project_id, service_id, cost_read=True)
        status = (await store.well_architected_publication_status(result_id)
                  if service_id == "well_architected" else await store.finops_publication_status(result_id))
        response.headers["Cache-Control"] = "no-store"
        return status

    @router.get("/results/{result_id}/design-usage/{task_id}", operation_id="getWellArchitectedResultUsage")
    async def read_well_architected_result_usage(product_id: str, project_id: str, service_id: str,
                                                result_id: str, task_id: str, response: Response,
                                                backend=Depends(backend_dependency)):
        if service_id != "well_architected" or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", task_id) is None:
            raise HTTPException(404, detail={"code": "not_found"})
        store = await scoped(backend, product_id, project_id, service_id)
        result = await store.use_wa_result(result_id, task_id, read_only=True)
        response.headers["Cache-Control"] = "no-store"
        return result

    @router.get("/results/{result_id}/devops-contributions/{destination}", operation_id="getDevopsContribution")
    async def get_devops_contribution(product_id: str, project_id: str, service_id: str, result_id: str,
                                      destination: str, response: Response, backend=Depends(backend_dependency)):
        if service_id != "devops":
            raise HTTPException(404, detail={"code": "not_found"})
        store = await scoped(backend, product_id, project_id, service_id)
        contribution = await store.devops_contribution(result_id, destination)
        response.headers["Cache-Control"] = "no-store"
        return contribution

    @router.get("/results/{result_id}/cost-contributions/{destination}",
                operation_id="getCostContribution")
    async def get_cost_contribution(product_id: str, project_id: str, service_id: str, result_id: str,
                                    destination: str, response: Response, backend=Depends(backend_dependency)):
        if service_id != "finops":
            raise HTTPException(404, detail={"code": "not_found"})
        store = await scoped(backend, product_id, project_id, service_id, cost_read=True)
        contribution = await store.cost_contribution(result_id, destination)
        response.headers["Cache-Control"] = "no-store"
        return contribution

    @router.get("/results/{result_id}/transform-contributions/{destination}",
                operation_id="getTransformContribution")
    async def get_transform_contribution(product_id: str, project_id: str, service_id: str,
                                         result_id: str, destination: str, response: Response,
                                         backend=Depends(backend_dependency)):
        if service_id != "transform":
            raise HTTPException(404, detail={"code": "not_found"})
        store = await scoped(backend, product_id, project_id, service_id, contribution_read=True)
        contribution = await store.transform_contribution(result_id, destination)
        response.headers["Cache-Control"] = "no-store"
        return contribution

    @router.get("/results/{result_id}/findings", operation_id="listFindings")
    async def list_findings(product_id: str, project_id: str, service_id: str, result_id: str,
                            response: Response, limit: int = Query(25, ge=1, le=100),
                            cursor: str | None = Query(None, min_length=1, max_length=2048),
                            backend=Depends(backend_dependency)):
        store = await scoped(backend, product_id, project_id, service_id)
        records = await store.findings(result_id, cursor)
        page = records[:limit]
        response.headers["Cache-Control"] = "no-store"
        output = {"items": page}
        if len(records) > limit:
            output["next_cursor"] = page[-1]["finding_id"]
        return output

    @router.post("/comparisons", operation_id="compareResults")
    async def compare_results(product_id: str, project_id: str, service_id: str, body: dict,
                              response: Response, backend=Depends(backend_dependency)):
        if (set(body) != {"baseline_result_id", "candidate_result_id"} or
                any(not isinstance(value, str) or not value for value in body.values())):
            raise HTTPException(422, detail={"code": "validation_error"})
        store = await scoped(backend, product_id, project_id, service_id)
        output = await store.compare(body["baseline_result_id"], body["candidate_result_id"])
        response.headers["Cache-Control"] = "no-store"
        return output

    @router.get("/results/{result_id}/findings/{finding_id}/triage", operation_id="listTriage")
    async def list_triage(product_id: str, project_id: str, service_id: str, result_id: str,
                          finding_id: str, response: Response, backend=Depends(backend_dependency)):
        store = await scoped(backend, product_id, project_id, service_id)
        items, revision = await triage_history(store, result_id, finding_id)
        response.headers["ETag"] = f'"{revision}"'
        response.headers["Cache-Control"] = "no-store"
        return {"items": items}

    @router.put("/results/{result_id}/findings/{finding_id}/triage", operation_id="setTriage")
    async def set_triage(product_id: str, project_id: str, service_id: str, result_id: str,
                         finding_id: str, decision: dict, response: Response,
                         if_match: str | None = Header(None, alias="If-Match"),
                         backend=Depends(backend_dependency)):
        if if_match is None:
            raise HTTPException(428, detail={"code": "precondition_required"})
        store = await scoped(backend, product_id, project_id, service_id)
        item = await append_triage(store, result_id, finding_id, decision, if_match)
        response.headers["ETag"] = f'"{item["revision"]}"'
        response.headers["Cache-Control"] = "no-store"
        return item

    @router.get("/artifacts/{artifact_id}", operation_id="getArtifactAccess")
    async def artifact_access(product_id: str, project_id: str, service_id: str, artifact_id: str,
                              response: Response, backend=Depends(backend_dependency)):
        store = await scoped(backend, product_id, project_id, service_id, artifact_read=True)
        reference, _ = await store.artifact(artifact_id)
        prefix = (f"/v1/products/{quote(product_id, safe='')}/projects/{quote(project_id, safe='')}"
                  f"/services/{quote(service_id, safe='')}")
        response.headers["Cache-Control"] = "no-store"
        access_expires = store.clock().astimezone(UTC) + timedelta(minutes=5)
        if service_id == "transform":
            from datetime import datetime

            access_expires = min(access_expires, datetime.fromisoformat(
                reference["expires_at"].replace("Z", "+00:00")))
        return {"artifact": reference, "download_path": f"{prefix}/artifacts/{quote(artifact_id, safe='')}/content",
                "expires_at": access_expires.isoformat().replace("+00:00", "Z")}

    @router.get("/artifacts/{artifact_id}/content", operation_id="downloadArtifact")
    async def download_artifact(product_id: str, project_id: str, service_id: str, artifact_id: str,
                                backend=Depends(backend_dependency)):
        store = await scoped(backend, product_id, project_id, service_id, artifact_read=True)
        reference, contents = await store.artifact(artifact_id)
        return Response(content=contents, media_type=(reference["media_type"] if service_id == "transform"
                                                      else "application/json"), headers={
            "Cache-Control": "no-store", "Content-Disposition": "attachment",
            "Content-Security-Policy": "sandbox; default-src 'none'",
            "X-Content-Type-Options": "nosniff", "X-Adp-Content-Sha256": reference["sha256"],
        })

    @router.get("/publications/{publication_id}", operation_id="getPublication")
    async def get_publication(product_id: str, project_id: str, service_id: str, publication_id: str,
                              response: Response, backend=Depends(backend_dependency)):
        store = await scoped(backend, product_id, project_id, service_id, cost_read=True)
        publication, result = await store.publication(publication_id)
        response.headers["ETag"] = f'"{publication.revision}"'
        response.headers["Cache-Control"] = "no-store"
        return result

    @router.post("/publications/{publication_id}/retry", operation_id="retryPublication", status_code=202)
    async def retry_publication(product_id: str, project_id: str, service_id: str, publication_id: str,
                                body: dict, response: Response,
                                idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
                                backend=Depends(backend_dependency)):
        if not idempotency_key:
            raise HTTPException(428, detail={"code": "precondition_required"})
        if (re.fullmatch(r"[A-Za-z0-9_-]{16,128}", idempotency_key) is None
                or set(body) != {"reason"} or not isinstance(body["reason"], str)
                or not 1 <= len(body["reason"].strip()) <= 1000):
            raise HTTPException(422, detail={"code": "validation_error"})
        store = await scoped(backend, product_id, project_id, service_id, cost_read=True)
        result = await store.retry_publication(publication_id, body, idempotency_key)
        response.headers["Cache-Control"] = "no-store"
        return result

    return router
