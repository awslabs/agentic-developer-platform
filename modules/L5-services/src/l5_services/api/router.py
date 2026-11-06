import re

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from l5_services.api.bindings import create_bindings_router
from l5_services.api.devops_access import create_devops_access_router
from l5_services.api.results import create_results_router
from l5_services.api.runs import create_runs_router


def _check_service(service_id: str):
    if service_id not in {"security", "devops", "finops", "well_architected", "transform"}:
        raise HTTPException(404, detail={"code": "not_found"})


def create_router(backend_dependency):
    router = APIRouter(prefix="/v1/products/{product_id}/projects/{project_id}/services/{service_id}", tags=["l5-security"])

    @router.get("/configuration/options", operation_id="getOptions")
    async def get_options(product_id: str, project_id: str, service_id: str, response: Response, backend=Depends(backend_dependency)):
        _check_service(service_id)
        options = await backend.options(product_id, project_id, service_id)
        response.headers["Cache-Control"] = "no-store"
        return options

    @router.get("/configuration", operation_id="getConfiguration")
    async def get_configuration(product_id: str, project_id: str, service_id: str, response: Response, backend=Depends(backend_dependency)):
        _check_service(service_id)
        configuration = await backend.get(product_id, project_id, service_id)
        response.headers["ETag"] = f'"{configuration["hash"]}"'
        response.headers["Cache-Control"] = "no-store"
        return configuration

    @router.get("/configuration/versions", operation_id="listConfigurationVersions")
    async def list_configuration_versions(product_id: str, project_id: str, service_id: str,
                                          response: Response, backend=Depends(backend_dependency)):
        _check_service(service_id)
        response.headers["Cache-Control"] = "no-store"
        return {"items": await backend.versions(product_id, project_id, service_id)}

    @router.get("/configuration/versions/{revision}", operation_id="getConfigurationVersion")
    async def get_configuration_version(
        product_id: str, project_id: str, service_id: str, revision: int,
        response: Response, backend=Depends(backend_dependency),
    ):
        _check_service(service_id)
        if revision < 1:
            raise HTTPException(404, detail={"code": "not_found"})
        configuration = await backend.version(product_id, project_id, service_id, revision)
        response.headers["ETag"] = f'"{configuration["hash"]}"'
        response.headers["Cache-Control"] = "no-store"
        return configuration

    @router.put("/configuration", operation_id="saveConfiguration")
    async def save_configuration(
        product_id: str, project_id: str, service_id: str, settings: dict, response: Response,
        idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
        if_none_match: str | None = Header(None, alias="If-None-Match"),
        if_match: str | None = Header(None, alias="If-Match"),
        backend=Depends(backend_dependency),
    ):
        _check_service(service_id)
        if if_none_match is not None and if_match is not None:
            raise HTTPException(422, detail={"code": "conflicting_preconditions"})
        if not idempotency_key or (if_none_match != "*" and if_match is None):
            raise HTTPException(428, detail={"code": "precondition_required"})
        if re.fullmatch(r"[A-Za-z0-9_-]{16,128}", idempotency_key) is None:
            raise HTTPException(422, detail={"code": "validation_error"})
        configuration = await backend.save(product_id, project_id, service_id, settings, idempotency_key, if_none_match, if_match)
        response.headers["ETag"] = f'"{configuration["hash"]}"'
        response.headers["Cache-Control"] = "no-store"
        response.status_code = 201 if if_none_match == "*" else 200
        return configuration

    @router.post("/configuration/check", operation_id="checkConfiguration")
    async def check_configuration(product_id: str, project_id: str, service_id: str, backend=Depends(backend_dependency)):
        _check_service(service_id)
        return await backend.check(product_id, project_id, service_id)

    @router.post("/configuration/refresh", operation_id="refreshConfiguration")
    async def refresh_configuration(
        product_id: str, project_id: str, service_id: str, refresh_input: dict, response: Response,
        idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
        if_match: str | None = Header(None, alias="If-Match"),
        backend=Depends(backend_dependency),
    ):
        _check_service(service_id)
        if not idempotency_key or if_match is None:
            raise HTTPException(428, detail={"code": "precondition_required"})
        if (re.fullmatch(r"[A-Za-z0-9_-]{16,128}", idempotency_key) is None
                or "profile_ref" not in refresh_input or set(refresh_input) - {"profile_ref", "choices"}
                or ("choices" in refresh_input and not isinstance(refresh_input["choices"], dict))):
            raise HTTPException(422, detail={"code": "validation_error"})
        configuration = await backend.refresh(
            product_id, project_id, service_id, refresh_input["profile_ref"], idempotency_key, if_match,
            **({"choices": refresh_input["choices"]} if "choices" in refresh_input else {}),
        )
        response.headers["ETag"] = f'"{configuration["hash"]}"'
        response.headers["Cache-Control"] = "no-store"
        return configuration

    @router.post("/configuration/handoff", operation_id="retryConfigurationHandoff")
    async def retry_handoff(
        product_id: str, project_id: str, service_id: str, response: Response,
        idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
        if_match: str | None = Header(None, alias="If-Match"),
        backend=Depends(backend_dependency),
    ):
        _check_service(service_id)
        if not idempotency_key or if_match is None:
            raise HTTPException(428, detail={"code": "precondition_required"})
        if re.fullmatch(r"[A-Za-z0-9_-]{16,128}", idempotency_key) is None:
            raise HTTPException(422, detail={"code": "validation_error"})
        configuration = await backend.retry_handoff(product_id, project_id, service_id, idempotency_key, if_match)
        response.headers["ETag"] = f'"{configuration["hash"]}"'
        response.headers["Cache-Control"] = "no-store"
        return configuration

    router.include_router(create_runs_router(backend_dependency))
    router.include_router(create_results_router(backend_dependency))
    router.include_router(create_bindings_router(backend_dependency))
    router.include_router(create_devops_access_router(backend_dependency))

    return router
