"""Browser-only native DevOps access on the shared binding API."""

import re
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field


class BrowserLaunchInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    configuration_revision: int = Field(ge=1)


class PreventionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    configuration_revision: int = Field(ge=1)
    action: Literal["enable", "pause", "resume"]


def create_devops_access_router(backend_dependency):
    router = APIRouter()

    @router.get("/bindings/{binding_id}/access-policy", operation_id="getDevopsAccessPolicy")
    async def get_access_policy(product_id: str, project_id: str, service_id: str, binding_id: str,
                                response: Response, backend=Depends(backend_dependency)):
        response.headers["Cache-Control"] = "no-store"
        if service_id != "devops":
            raise HTTPException(404, detail={"code": "not_found"})
        return await backend.devops_access_policy(product_id, project_id, binding_id)

    @router.get("/bindings/{binding_id}/prevention", operation_id="getDevopsPrevention")
    async def get_prevention(product_id: str, project_id: str, service_id: str, binding_id: str,
                             response: Response, backend=Depends(backend_dependency)):
        response.headers["Cache-Control"] = "no-store"
        if service_id != "devops":
            raise HTTPException(404, detail={"code": "not_found"})
        return await backend.prevention_status(product_id, project_id, binding_id)

    @router.post("/bindings/{binding_id}/prevention", operation_id="configureDevopsPrevention")
    async def configure_prevention(product_id: str, project_id: str, service_id: str, binding_id: str,
                                   body: PreventionInput, response: Response,
                                   idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
                                   if_match: str | None = Header(None, alias="If-Match"),
                                   backend=Depends(backend_dependency)):
        response.headers["Cache-Control"] = "no-store"
        if service_id != "devops":
            raise HTTPException(404, detail={"code": "not_found"})
        if not idempotency_key or if_match is None:
            raise HTTPException(428, detail={"code": "precondition_required"})
        if re.fullmatch(r"[A-Za-z0-9_-]{16,128}", idempotency_key) is None:
            raise HTTPException(422, detail={"code": "validation_error"})
        return await backend.configure_prevention(
            product_id, project_id, binding_id, body.configuration_revision,
            body.action, idempotency_key, if_match)

    async def browser_request(product_id, project_id, service_id, binding_id, request, response,
                              authorization, backend, *, check=False):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        if service_id != "devops":
            raise HTTPException(404, detail={"code": "not_found"})
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(401, detail={"code": "session_required"})
        payload = bytearray()
        async for chunk in request.stream():
            payload.extend(chunk)
            if len(payload) > 256:
                raise HTTPException(413, detail={"code": "request_too_large"})
        try:
            body = BrowserLaunchInput.model_validate_json(bytes(payload))
        except ValueError:
            raise HTTPException(422, detail={"code": "validation_error"}) from None
        action = backend.check_browser_launch if check else backend.launch_browser
        return await action(product_id, project_id, binding_id, body.configuration_revision, request)

    @router.post("/bindings/{binding_id}/browser-launch/check", operation_id="checkNativeBrowserLaunch")
    async def check_native_browser(product_id: str, project_id: str, service_id: str,
                                   binding_id: str, request: Request, response: Response,
                                   authorization: str | None = Header(None),
                                   backend=Depends(backend_dependency)):
        return await browser_request(product_id, project_id, service_id, binding_id, request, response,
                                     authorization, backend, check=True)

    @router.post("/bindings/{binding_id}/browser-launch", operation_id="launchNativeBrowser")
    async def launch_native_browser(product_id: str, project_id: str, service_id: str,
                                    binding_id: str, request: Request, response: Response,
                                    authorization: str | None = Header(None),
                                    backend=Depends(backend_dependency)):
        return await browser_request(product_id, project_id, service_id, binding_id, request, response,
                                     authorization, backend)

    @router.get("/bindings/{binding_id}/resources")
    async def resources(product_id: str, project_id: str, service_id: str, binding_id: str,
                        response: Response, backend=Depends(backend_dependency)):
        if service_id != "devops":
            raise HTTPException(404, detail={"code": "not_found"})
        response.headers["Cache-Control"] = "no-store"
        return await backend.devops_resources(product_id, project_id, binding_id)

    return router
