"""Refresh expired dynamic metadata through the existing authenticated facade."""

import httpx
from fastapi import Request
from starlette.concurrency import run_in_threadpool

FACADE_DISCOVERY_URL = "http://tools-facade.adp-gateway.svc/agent/tools"


async def refreshed_tool_catalogue(request: Request, loader):
    """Keep protected provenance as the source of schemas after one metadata read.

    Callers authenticate and authorize the user before entering here. The facade
    independently checks that user's authority and refreshes the private target's
    listing. Neither its response body nor the caller can supply trusted schemas.
    A failed refresh leaves unrelated static tools available.
    """
    catalogue = await run_in_threadpool(loader)
    if not any(connector.get("dynamic") and connector.get("discovery_status") != "ready" for connector in catalogue.get("connectors", [])):
        return catalogue
    authorization = request.headers.getlist("authorization")
    if len(authorization) != 1 or not authorization[0].startswith("Bearer "):
        return catalogue
    try:
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=15) as client:
            response = await client.get(FACADE_DISCOVERY_URL, headers={"Authorization": authorization[0]})
        response.raise_for_status()
    except httpx.HTTPError:
        return catalogue
    # Recheck current target/source bindings and the short-lived producer record.
    # Do not substitute a user policy snapshot or schemas returned by the facade.
    return await run_in_threadpool(loader)
