"""Bounded L5 producer forwarding and non-data current-authority callback."""

import json
import os
from copy import deepcopy
from urllib.parse import quote, urlsplit

import httpx
from adp_platform_common.l5_contributions.assertions import HEADER, sign, verify
from adp_platform_common.l5_contributions.contract import DESTINATIONS, MAX_BYTES, SUFFIXES, canonical, kind_of
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from fastapi import APIRouter, Depends, HTTPException, Request
from l5_services.publication.contributions import contribution_pages
from starlette.responses import JSONResponse

from src.agentauth.envelope import SIGNING_KEY_ID_ENV, _signing_key
from src.internal.auth_deps import verify_internal_or_irsa
from src.internal.l5_product_routes import service_actor
from src.l5_consumer_authority import authorize_assertion, claims_for, current, denied
from src.l5_consumer_config import consumer_targets
from src.l5_service_identity import contribution_worker_id
from src.shared.database import get_session_factory
from src.shared.models.vault import CredentialServiceDelegation

router = APIRouter()


def verification_keys():
    key = _signing_key(os.environ).public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    return {os.environ[SIGNING_KEY_ID_ENV]: key}


def consumer_url(destination):
    try:
        configured = consumer_targets(os.environ.get("L5_CONSUMER_TARGETS", "{}"))
    except (ValueError, TypeError):
        raise HTTPException(503, detail={"code": "consumer_not_installed"}) from None
    value = configured.get(destination, "")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise HTTPException(503, detail={"code": "consumer_not_installed"})
    return value


async def send_to_consumer(claims, *, method, path, body, sessions):
    token = sign(
        claims,
        key=_signing_key(os.environ),
        key_id=os.environ[SIGNING_KEY_ID_ENV],
        method=method,
        path=path,
        body=body,
        destination=claims["destination"],
    )
    if not await authorize_assertion(claims, sessions):
        raise denied()
    async with httpx.AsyncClient(base_url=consumer_url(claims["destination"]), trust_env=False, follow_redirects=False, timeout=30) as transport:
        response = await transport.request(method, path, content=body, headers={HEADER: token, "Content-Type": "application/json"})
    if not await authorize_assertion(claims, sessions):
        raise denied()
    # Never forward a peer's redirect, arbitrary HTML, headers, or exception body.
    if response.status_code not in {200, 202}:
        status = response.status_code if response.status_code in {404, 409, 422} else 503
        raise HTTPException(status, detail={"code": "consumer_unavailable", "retryable": status == 503})
    if len(response.content) > MAX_BYTES * 8:
        raise HTTPException(502, detail={"code": "invalid_consumer_response"})
    try:
        result = response.json()
    except ValueError:
        raise HTTPException(502, detail={"code": "invalid_consumer_response"}) from None
    return response.status_code, result


@router.post("/internal/v1/l5/contribution-authority", include_in_schema=False)
async def contribution_authority(request: Request):
    raw = await bounded_body(request)
    try:
        body = json.loads(raw)
        if not isinstance(body, dict) or set(body) != {"assertion"} or len(raw) > 40000:
            raise ValueError()
        claims = None
        # Destination is selected by verified audience, not caller attribution.
        for destination in DESTINATIONS.values():
            try:
                claims = verify(body["assertion"], verification_keys(), destination=destination)
                break
            except (ValueError, KeyError, TypeError, InvalidSignature):
                continue
        if claims is None:
            raise ValueError()
        authorized = await authorize_assertion(claims, get_session_factory())
    except (ValueError, KeyError, TypeError, InvalidSignature):
        authorized = False
    return JSONResponse({"authorized": authorized}, headers={"Cache-Control": "no-store"})


async def bounded_body(request):
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > MAX_BYTES:
            raise HTTPException(413, detail={"code": "request_too_large"})
    return bytes(data)


async def producer_request(request: Request, actor=Depends(service_actor)):
    if request.url.query or len(request.headers.getlist("X-L5-Delegation-ID")) != 1:
        raise denied()
    delegation_id = request.headers["X-L5-Delegation-ID"]
    parts = request.url.path.split("/")
    consumer, suffix = parts[3:5]
    destination = DESTINATIONS[consumer]
    kind = next((key for key, value in SUFFIXES.items() if value == suffix), None)
    raw = await bounded_body(request)
    try:
        body = json.loads(raw) if raw else None
    except ValueError:
        raise HTTPException(422, detail={"code": "invalid_contribution"}) from None
    action = (
        "admit"
        if request.method == "POST" and len(parts) == 5
        else ("invalidate" if request.method == "POST" and parts[-1] == "invalidate" else "read")
    )
    contribution_id = body.get("contribution_id") if action == "admit" and isinstance(body, dict) else (parts[5] if len(parts) > 5 else None)
    sessions = get_session_factory()
    async with sessions() as db:
        grant = await db.get(CredentialServiceDelegation, delegation_id)
        if grant is None or grant.service_id != contribution_worker_id(actor):
            raise denied()
        run, publication = await current(
            db, actor, tenant_id=grant.tenant_id, contribution_id=contribution_id, destination=destination, action=action, delegation_id=delegation_id
        )
        if kind != kind_of(publication.payload):
            raise denied()
        if action == "admit":
            allowed = contribution_pages(publication) if kind == "security_result" else [publication.payload]
            if body not in allowed:
                raise denied()
        elif action == "invalidate":
            if body != {**publication.invalidation, "invalidation_watermark": publication.invalidation_watermark}:
                raise denied()
        elif request.method != "GET" or len(parts) not in {6, 7} or (len(parts) == 7 and parts[-1] != "projection"):
            raise denied()
        claims = claims_for(actor, run, publication, action=action, delegation_id=delegation_id)
    status, result = await send_to_consumer(claims, method=request.method, path=request.url.path, body=raw, sessions=sessions)
    return JSONResponse(result, status_code=status, headers={"Cache-Control": "no-store"})


for consumer in DESTINATIONS:
    for kind, suffix in SUFFIXES.items():
        if consumer == "insights" and kind == "security_result":
            continue
        base = "/internal/v1/" + consumer + "/" + suffix
        for path, methods in (
            (base, ["POST"]),
            (base + "/{contribution_id}", ["GET"]),
            (base + "/{contribution_id}/projection", ["GET"]),
            (base + "/{contribution_id}/invalidate", ["POST"]),
        ):
            router.add_api_route(path, producer_request, methods=methods, dependencies=[Depends(verify_internal_or_irsa)], include_in_schema=False)


async def human_consumer_request(actor, publication, *, operation="projection", task_id=None, read_only=True, sessions=None):
    sessions = sessions or get_session_factory()
    destination = publication.destination
    async with sessions() as db:
        run, stored = await current(
            db,
            actor,
            tenant_id=actor.org_id,
            contribution_id=publication.publication_id,
            destination=destination,
            action="use" if task_id else "read",
            task_id=task_id,
        )
        kind = kind_of(stored.payload)
        consumer = "context" if destination == "product-context" else "insights"
        base = "/internal/v1/" + consumer + "/" + SUFFIXES[kind] + "/" + quote(stored.publication_id, safe="")
        usage = None
        if task_id:
            usage = {
                "scope": deepcopy(stored.payload["scope"]),
                "result_ref": deepcopy(stored.payload["result_ref"]),
                "contribution_id": stored.publication_id,
                "task_id": task_id,
                "actor_id": actor.user_id,
                "citation": f"{kind}:{stored.result_id}@{stored.result_version}",
            }
            if destination == "engineering-insights":
                from l5_services.models.publication import Publication
                from sqlalchemy import select

                context = await db.scalar(
                    select(Publication).where(
                        Publication.tenant_id == actor.org_id,
                        Publication.result_id == stored.result_id,
                        Publication.result_version == stored.result_version,
                        Publication.destination == "product-context",
                    )
                )
                if context is None:
                    raise denied()
                retrieval = await human_consumer_request(actor, context, task_id=task_id, read_only=True, sessions=sessions)
                usage["retrieval_receipt_id"] = retrieval["receipt_id"]
            path = base + "/tasks/" + quote(task_id, safe="")
        elif operation in {"receipt", "projection"}:
            path = base + ("/projection" if operation == "projection" else "")
        else:
            raise denied()
        claims = claims_for(actor, run, stored, action="use" if task_id else "read", usage=usage)
    method = "GET" if read_only or not task_id else "POST"
    _, result = await send_to_consumer(claims, method=method, path=path, body=canonical(usage) if method == "POST" else b"", sessions=sessions)
    return result
