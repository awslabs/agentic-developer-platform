"""Private native-human transport. Never mounted through generic CLI/MCP tools."""

import re

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from src.shared.database import get_session_factory

from .native_credential_authority import NativeConfig, NativeProviders, authenticated, canonical_human, public_configuration, refused, require
from .native_credential_service import NativeCredentialService
from .native_credential_wire import MAX_WIRE_BYTES, CreateRequest, parse

router = APIRouter(prefix="/auth/native-credentials", tags=["native-credentials"])


def service():
    return NativeCredentialService(get_session_factory(), NativeConfig.configured(), NativeProviders())


async def body(request):
    require(request.headers.get("content-type", "").split(";")[0] == "application/json", "native_request_invalid", 400)
    raw = bytearray()
    async for part in request.stream():
        require(len(raw) + len(part) <= MAX_WIRE_BYTES, "native_request_invalid", 400)
        raw.extend(part)
    return parse(raw)


async def dispatch(request, operation, *, browser=False):
    try:
        issuer = service()
        actor = await authenticated(request, issuer.config, issuer.providers, browser=browser)
        result = await operation(issuer, actor)
        return JSONResponse(result, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})
    except HTTPException as exc:
        detail = exc.detail
        if isinstance(detail, dict) and set(detail) == {"error"} and re.fullmatch(r"native_[a-z_]+", str(detail["error"])):
            raise refused(detail["error"], exc.status_code) from None
        raise refused("native_credential_authority_changed") from None
    except (ValueError, TypeError, KeyError):
        raise refused("native_request_invalid", 400) from None
    except Exception:
        raise refused("native_credentials_unavailable", 503) from None


@router.get("/config")
async def configuration(request: Request):
    async def operation(issuer, actor):
        async with issuer.factory() as db:
            await canonical_human(db, actor)
        return await public_configuration(issuer.config, issuer.providers)

    return await dispatch(request, operation)


@router.post("/requests")
async def create(request: Request):
    async def operation(issuer, actor):
        return await issuer.create(actor, CreateRequest.model_validate(await body(request)))

    return await dispatch(request, operation)


@router.get("/requests/by-request/{request_id}")
async def by_request(request: Request, request_id: str):
    return await dispatch(request, lambda issuer, actor: issuer.status(actor, request_id=request_id))


@router.get("/requests/{receipt_id}")
async def status(request: Request, receipt_id: str):
    return await dispatch(request, lambda issuer, actor: issuer.status(actor, receipt_id=receipt_id))


@router.post("/requests/{receipt_id}/offer")
async def offer(request: Request, receipt_id: str):
    async def operation(issuer, actor):
        value = await body(request)
        require(isinstance(value, dict) and set(value) == {"offer"}, "native_request_invalid", 400)
        return await issuer.offer(actor, receipt_id, value["offer"])

    return await dispatch(request, operation)


@router.get("/approvals/{receipt_id}")
async def preview(request: Request, receipt_id: str):
    return await dispatch(request, lambda issuer, actor: issuer.preview(actor, receipt_id), browser=True)


@router.post("/approvals/{receipt_id}")
async def approve(request: Request, receipt_id: str):
    async def operation(issuer, actor):
        origins = request.headers.getlist("origin")
        require(len(origins) == 1, "native_browser_origin_required")
        return await issuer.approve(actor, receipt_id, await body(request), origins[0])

    return await dispatch(request, operation, browser=True)


@router.post("/requests/{receipt_id}/challenge")
async def challenge(request: Request, receipt_id: str):
    async def operation(issuer, actor):
        require(await body(request) == {}, "native_request_invalid", 400)
        return await issuer.challenge(actor, receipt_id)

    return await dispatch(request, operation)


@router.post("/requests/{receipt_id}/issue")
async def issue(request: Request, receipt_id: str):
    async def operation(issuer, actor):
        return await issuer.issue(actor, receipt_id, await body(request))

    return await dispatch(request, operation)


@router.post("/requests/{receipt_id}/ack")
async def acknowledge(request: Request, receipt_id: str):
    async def operation(issuer, actor):
        return await issuer.acknowledge(actor, receipt_id, await body(request))

    return await dispatch(request, operation)
