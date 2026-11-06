"""Owner-scoped WebSocket relay to the active Crew dashboard."""

import asyncio
import os
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

from fastapi import HTTPException, WebSocket
from fastapi.responses import JSONResponse
from starlette.websockets import WebSocketState
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake

from src.crew.connections import (
    RENEW_SECONDS,
    CrewConnectionLimitError,
    CrewConnectionUnavailableError,
    crew_connection_registry,
)
from src.crew.handoff import crew_host_context
from src.crew.keepalive import keepalive_seconds
from src.crew.targets import ResolvedCrewTarget


def crew_websocket_connect(uri: str, **kwargs):
    return connect(uri, **kwargs)


async def forward_crew_websocket(websocket: WebSocket, target: ResolvedCrewTarget, on_activity: Callable[[], Awaitable[None]] | None = None) -> None:
    origins = websocket.headers.getlist("origin")
    if len(origins) > 1 or (origins and origins[0] != os.environ["CREW_DASHBOARD_ORIGIN"].rstrip("/")):
        await websocket.send_denial_response(JSONResponse({"error": "crew_origin_denied"}, status_code=403))
        return
    try:
        registry = crew_connection_registry()
        lease = await registry.acquire(target.lease.tenant_id, target.lease.user_id)
    except CrewConnectionLimitError:
        await websocket.send_denial_response(JSONResponse({"error": "crew_connection_limit"}, status_code=429))
        return
    except CrewConnectionUnavailableError:
        await websocket.send_denial_response(JSONResponse({"error": "crew_admission_unavailable"}, status_code=503))
        return

    try:
        await _relay_websocket(websocket, target, lease, on_activity)
    finally:
        await lease.release()


async def _relay_websocket(websocket: WebSocket, target: ResolvedCrewTarget, lease, on_activity: Callable[[], Awaitable[None]] | None = None) -> None:
    endpoint = urlsplit(target.endpoint)
    dashboard = urlsplit(os.environ["CREW_DASHBOARD_ORIGIN"])
    query = websocket.scope.get("query_string", b"")
    path = websocket.scope.get("raw_path", websocket.url.path.encode()).decode("ascii")
    uri = f"{'wss' if endpoint.scheme == 'https' else 'ws'}://{dashboard.netloc}{path}"
    if query:
        uri += f"?{query.decode('ascii')}"
    cookies = [
        part.strip() for part in websocket.headers.get("cookie", "").split(";") if part.strip() and part.partition("=")[0].strip() != "adp_crew"
    ]
    headers = {
        "X-Forwarded-Host": dashboard.netloc,
        "X-Forwarded-Proto": "https",
        "X-Forwarded-For": websocket.client.host if websocket.client else "",
    }
    if cookies:
        headers["Cookie"] = "; ".join(cookies)
    # Pod-hop keepalive: the websockets client pings the pod every interval and
    # answers its pings itself; pings are control frames, so data ordering is
    # untouched. The browser hop is pinged by uvicorn (see src/crew/keepalive.py).
    keepalive = keepalive_seconds()
    try:
        async with crew_websocket_connect(
            uri,
            host=endpoint.hostname,
            port=endpoint.port or (443 if endpoint.scheme == "https" else 80),
            origin=websocket.headers.get("origin"),
            additional_headers=headers,
            subprotocols=websocket.scope.get("subprotocols") or None,
            proxy=None,
            ping_interval=keepalive or None,
            **({"ssl": await crew_host_context(target), "server_hostname": endpoint.hostname} if target.lease.target_kind == "ec2-instance" else {}),
        ) as upstream:
            await websocket.accept(subprotocol=upstream.subprotocol)

            async def to_pod():
                while True:
                    message = await websocket.receive()
                    if message["type"] == "websocket.disconnect":
                        return
                    if on_activity is not None and (message.get("text") is not None or message.get("bytes") is not None):
                        await on_activity()
                    if message.get("text") is not None:
                        await upstream.send(message["text"])
                    elif message.get("bytes") is not None:
                        await upstream.send(message["bytes"])

            async def to_browser():
                async for message in upstream:
                    if isinstance(message, str):
                        await websocket.send_text(message)
                    else:
                        await websocket.send_bytes(message)

            async def maintain_lease():
                while True:
                    await asyncio.sleep(min(RENEW_SECONDS, lease.remaining()))
                    if not await lease.renew():
                        return

            lease_task = asyncio.create_task(maintain_lease())
            browser_task = asyncio.create_task(to_browser())
            tasks = {asyncio.create_task(to_pod()), browser_task, lease_task}
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                upstream_code = upstream.close_code
                if upstream_code not in range(1000, 5000) or upstream_code in {1004, 1005, 1006, 1015}:
                    upstream_code = 1011
                close_code = 1013 if lease_task in done else upstream_code if browser_task in done else 1000
                for task in done:
                    if not task.cancelled() and task.exception() is not None:
                        if isinstance(task.exception(), ConnectionClosed):
                            close_code = upstream_code
                        else:
                            close_code = 1011
                if websocket.application_state == WebSocketState.CONNECTED and websocket.client_state == WebSocketState.CONNECTED:
                    await websocket.close(code=close_code)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
    except HTTPException:
        if websocket.application_state == WebSocketState.CONNECTING:
            await websocket.send_denial_response(JSONResponse({"error": "crew_host_trust_unavailable"}, status_code=503))
        elif websocket.client_state == WebSocketState.CONNECTED:
            await websocket.close(code=1013)
    except (OSError, TimeoutError, InvalidHandshake, ValueError):
        if websocket.application_state == WebSocketState.CONNECTING:
            await websocket.send_denial_response(JSONResponse({"error": "crew_target_unavailable"}, status_code=502))
        elif websocket.client_state == WebSocketState.CONNECTED:
            await websocket.close(code=1013)
