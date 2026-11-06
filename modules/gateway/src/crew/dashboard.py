"""Host-scoped entry points for the Crew dashboard."""

import asyncio
import os
import time
from datetime import UTC, datetime
from typing import Annotated
from urllib.parse import quote, urlsplit

import httpx
from fastapi import Depends, FastAPI, HTTPException, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.background import BackgroundTask
from starlette.requests import HTTPConnection, Request
from starlette.routing import Host

from src.auth.dependencies import get_current_user
from src.cloud_desktop.host_identity import current_host
from src.cloud_desktop.routes import ActivityRequest, accept_box_activity
from src.crew.connections import (
    RENEW_SECONDS,
    CrewConnectionLimitError,
    CrewConnectionUnavailableError,
    crew_connection_registry,
)
from src.crew.handoff import crew_host_client, mint_crew_link
from src.crew.handoff_tickets import HandoffTicketUnavailableError, consume_handoff_ticket, verify_handoff_ticket
from src.crew.keepalive import is_event_stream, keepalive_seconds, keepalive_stream
from src.crew.path_policy import require_dashboard_path
from src.crew.proxy import active_crew_target, current_crew_owner, get_target_resolver
from src.crew.targets import CrewOwner, CrewTargetResolver, ResolvedCrewTarget
from src.crew.tickets import CREW_TICKET_LIFETIME_SECONDS, mint_crew_ticket, verify_crew_ticket
from src.crew.websocket import forward_crew_websocket
from src.shared.database import get_db
from src.shared.models.audit import AuditLog
from src.shared.models.dev_box import DevBox
from src.shared.schemas.auth import TokenContext

# Issue #9619: after a cold start the controller activates the host run (and GET
# /crew/target answers "ready") a few seconds before Kiro Crew on the box accepts a
# mint, so the first "Open my Dev Box" landed on a raw 503 JSON page. The handoff is a
# top-level navigation behind a 30 s edge timeout, so the wait is bounded well below
# that; a mint refused for any other reason (lease fenced, bad response) is not retried.
CREW_HANDOFF_WAIT_BUDGET_SECONDS = float(os.environ.get("CREW_HANDOFF_WAIT_BUDGET_SECONDS", "20"))
CREW_HANDOFF_RETRY_INTERVAL_SECONDS = 2.0
_monotonic = time.monotonic


def _mint_still_starting(exc: HTTPException) -> bool:
    return exc.status_code == 503 and isinstance(exc.detail, dict) and exc.detail.get("error") == "crew_mint_unavailable"


async def mint_crew_link_when_ready(owner: CrewOwner, target: ResolvedCrewTarget, *, now: int) -> str:
    """Mint the Crew login link, retrying only while the host's Crew is still starting."""
    deadline = _monotonic() + CREW_HANDOFF_WAIT_BUDGET_SECONDS
    while True:
        try:
            return await mint_crew_link(owner, target, now=now)
        except HTTPException as exc:
            if not _mint_still_starting(exc) or _monotonic() + CREW_HANDOFF_RETRY_INTERVAL_SECONDS >= deadline:
                raise
        await asyncio.sleep(CREW_HANDOFF_RETRY_INTERVAL_SECONDS)


def _frontend_open_url() -> str | None:
    """The ADP page that re-runs the open flow, when this deployment names its frontend origin."""
    for origin in os.environ.get("CORS_ALLOWED_ORIGINS", "").split(","):
        origin = origin.strip().rstrip("/")
        if origin.startswith("https://") and "*" not in origin:
            return f"{origin}/settings/dev-box/open"
    return None


def _still_starting_page() -> HTMLResponse:
    link = _frontend_open_url()
    action = f'<p><a href="{link}">Open my Dev Box again</a></p>' if link else "<p>Go back to the Dev Box page and open it again.</p>"
    body = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Dev Box is still starting</title>'
        '<meta name="referrer" content="no-referrer"></head><body style="font-family:system-ui;margin:3rem;max-width:40rem">'
        "<h1>Your Dev Box is still starting</h1>"
        f"<p>The machine is running, but Kiro Crew on it did not answer within {int(CREW_HANDOFF_WAIT_BUDGET_SECONDS)} seconds. "
        "This usually clears within a minute of a cold start.</p>"
        f"{action}</body></html>"
    )
    return HTMLResponse(body, status_code=503, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


def crew_proxy_enabled() -> None:
    if os.environ.get("CREW_PROXY_ENABLED", "false").lower() != "true":
        raise HTTPException(404, detail={"error": "not_found"})


def _bind_identity(connection: HTTPConnection, identity: TokenContext) -> TokenContext:
    connection.state.token_context = identity
    connection.state.user_id = identity.user_id
    connection.state.org_id = identity.org_id
    connection.state.team_id = identity.team_id
    connection.state.department_id = identity.department_id
    return identity


def _refuse_handoff_ticket() -> HTTPException:
    return HTTPException(401, detail={"error": "crew_handoff_refused"}, headers={"Cache-Control": "no-store"})


def presented_handoff_ticket(connection: HTTPConnection) -> str | None:
    """The single `ticket` query value on a top-level `/crew/handoff` navigation, or None for the bearer path."""
    if connection.scope["type"] != "http" or connection.url.path != "/crew/handoff":
        return None
    tickets = connection.query_params.getlist("ticket")
    if not tickets:
        return None
    if len(tickets) != 1 or len(connection.query_params.multi_items()) != 1:
        raise _refuse_handoff_ticket()
    return tickets[0]


def handoff_ticket_claims(ticket: str, *, now: int) -> dict:
    try:
        return verify_handoff_ticket(ticket, now=now)
    except ValueError:
        raise _refuse_handoff_ticket() from None


async def crew_identity(connection: HTTPConnection) -> TokenContext:
    request_scope = connection.scope if connection.scope["type"] == "http" else {**connection.scope, "type": "http"}
    authorization = connection.headers.get("authorization")
    ticket = presented_handoff_ticket(connection)
    if ticket is not None:
        # First-party redemption (#7902): the ticket was minted by the ADP origin for an
        # authenticated session, so it is the identity for this one navigation. Neither a
        # bearer token nor the adp_crew cookie is consulted.
        claims = handoff_ticket_claims(ticket, now=int(time.time()))
        return _bind_identity(
            connection,
            TokenContext(
                user_id=claims["user"],
                org_id=claims["tenant"],
                team_id="",
                department_id="",
                account_type="human",
                auth_source="crew_handoff_ticket",
                expires_at=datetime.fromtimestamp(claims["expires"], UTC),
            ),
        )
    if authorization or connection.url.path == "/crew/handoff":
        return await get_current_user(Request(request_scope), authorization=authorization)
    ticket = connection.cookies.get("adp_crew")
    if not ticket:
        return await get_current_user(Request(request_scope), authorization=None)
    try:
        claims = verify_crew_ticket(ticket)
    except ValueError:
        raise HTTPException(401, detail={"error": "crew_session_refused"}, headers={"Cache-Control": "no-store"}) from None
    if not claims["tenant"] or not claims["user"] or claims["expires"] <= int(time.time()):
        raise HTTPException(401, detail={"error": "crew_session_refused"}, headers={"Cache-Control": "no-store"})
    return _bind_identity(
        connection,
        TokenContext(
            user_id=claims["user"],
            org_id=claims["tenant"],
            team_id="",
            department_id="",
            account_type="human",
            auth_source="crew_cookie",
            expires_at=datetime.fromtimestamp(claims["expires"], UTC),
        ),
    )


async def crew_access(
    connection: HTTPConnection,
    enabled: Annotated[None, Depends(crew_proxy_enabled)],
    identity: Annotated[TokenContext, Depends(crew_identity)],
    db: Annotated[object, Depends(get_db)],
    resolver: Annotated[CrewTargetResolver, Depends(get_target_resolver)],
) -> ResolvedCrewTarget:
    owner: CrewOwner = await current_crew_owner(identity, db)
    # End the read-only owner lookup here so its pooled connection returns before the
    # proxied stream runs. FastAPI >= 0.118 (0.136 live) runs yield-dependency teardown
    # after the response is sent, so without this every in-flight proxied request --
    # including long-lived SSE/websocket streams -- pins one of the 2+1 pooled
    # connections per worker; a 28-file SPA asset burst then times out at pool_timeout
    # (10 s) and the dashboard fails with 500s (dev, 2026-10-20 05:34 UTC, #8223).
    # The session stays usable: record_crew_developer_activity opens its own
    # transaction and commits.
    await db.rollback()
    require_dashboard_path(connection.scope)
    target = await active_crew_target(owner, resolver)
    cookie = connection.cookies.get("adp_crew")
    if not cookie:
        raise HTTPException(409, detail={"error": "crew_handoff_required"}, headers={"Cache-Control": "no-store"})
    try:
        claims = verify_crew_ticket(cookie)
    except ValueError:
        raise HTTPException(404, detail={"error": "crew_session_refused"}, headers={"Cache-Control": "no-store"}) from None
    if claims["tenant"] != owner.tenant_id or claims["user"] != owner.user_id:
        raise HTTPException(404, detail={"error": "crew_session_refused"}, headers={"Cache-Control": "no-store"})
    if (
        claims["expires"] <= int(time.time())
        or claims["generation"] != target.lease.generation
        or claims["target_kind"] != target.lease.target_kind
        or claims["target_ref"] != target.lease.target_ref
    ):
        raise HTTPException(409, detail={"error": "crew_handoff_required"}, headers={"Cache-Control": "no-store"})
    return target


def crew_http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(trust_env=False, timeout=httpx.Timeout(30, read=None))


_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


def _hop_headers(headers: httpx.Headers) -> set[str]:
    return _HOP_HEADERS | {part.strip().lower() for part in headers.get("connection", "").split(",")}


def _safe_location(location: str, target: ResolvedCrewTarget) -> str | None:
    if location.startswith("//") or "\\" in location:
        return None
    try:
        parsed = urlsplit(location)
        if parsed.scheme not in ("", "http", "https"):
            return None
        if parsed.scheme:
            upstream = urlsplit(target.endpoint)
            dashboard = urlsplit(os.environ["CREW_DASHBOARD_ORIGIN"])
            if (parsed.scheme, parsed.netloc) == (dashboard.scheme, dashboard.netloc):
                return location
            if (parsed.scheme, parsed.netloc) != (upstream.scheme, upstream.netloc):
                return None
            return (
                f"{dashboard.scheme}://{dashboard.netloc}{parsed.path or '/'}"
                + (f"?{parsed.query}" if parsed.query else "")
                + (f"#{parsed.fragment}" if parsed.fragment else "")
            )
        return location
    except ValueError:
        return None


async def forward_crew_http(request: Request, target: ResolvedCrewTarget) -> Response:
    upstream_url = (
        httpx.URL(target.endpoint)
        .copy_with(
            raw_path=request.scope.get("raw_path", quote(request.url.path, safe="/").encode("ascii")),
        )
        .copy_with(query=request.scope.get("query_string") or None)
    )
    require_dashboard_path({"path": upstream_url.path, "raw_path": upstream_url.raw_path.split(b"?", 1)[0]})
    excluded = _hop_headers(request.headers) | {"authorization", "host", "content-length"}
    headers = {name: value for name, value in request.headers.items() if name.lower() not in excluded and not name.lower().startswith("x-forwarded-")}
    cookies = [part.strip() for part in request.headers.get("cookie", "").split(";") if part.strip() and part.partition("=")[0].strip() != "adp_crew"]
    if cookies:
        headers["cookie"] = "; ".join(cookies)
    else:
        headers.pop("cookie", None)
    headers.update(
        {
            "host": request.headers["host"],
            "x-forwarded-host": request.headers["host"],
            "x-forwarded-proto": "https",
            "x-forwarded-for": request.client.host if request.client else "",
        }
    )
    client = await crew_host_client(target) if target.lease.target_kind == "ec2-instance" else crew_http_client()
    upstream = None

    async def close_upstream() -> None:
        if upstream is not None:
            await upstream.aclose()
        await client.aclose()

    try:
        upstream_request = client.build_request(
            request.method,
            upstream_url,
            headers=headers,
            content=request.stream() if request.method not in {"GET", "HEAD"} else None,
        )
        upstream = await client.send(upstream_request, stream=True, follow_redirects=False)
    except httpx.HTTPError:
        await close_upstream()
        return JSONResponse({"error": "crew_target_unavailable"}, status_code=502)
    except BaseException:
        await close_upstream()
        raise
    location = upstream.headers.get("location")
    safe_location = _safe_location(location, target) if location is not None else None
    if location is not None and safe_location is None:
        await close_upstream()
        return JSONResponse({"error": "crew_target_unavailable"}, status_code=502)

    excluded_response = _hop_headers(upstream.headers) | {"content-length", "location"}
    response_headers = [
        (name, value) for name, value in upstream.headers.raw if name.decode("ascii").lower() not in excluded_response | {"set-cookie"}
    ]
    if safe_location is not None:
        response_headers.append((b"location", safe_location.encode("latin-1")))
    for cookie in upstream.headers.get_list("set-cookie"):
        if cookie.partition("=")[0].strip().lower() == "adp_crew":
            continue
        if "secure" not in {part.strip().lower() for part in cookie.split(";")[1:]}:
            cookie += "; Secure"
        response_headers.append((b"set-cookie", cookie.encode("latin-1")))

    async def stream_body():
        try:
            if upstream.is_stream_consumed:
                yield upstream.content
            else:
                chunks = upstream.aiter_raw()
                if is_event_stream(upstream.headers.get("content-type")):
                    # Idle SSE streams get `: keepalive` comments so the edge's read
                    # timeout (60 s behind a CloudFront VPC origin) never fires (#7902).
                    chunks = keepalive_stream(chunks, keepalive_seconds())
                try:
                    async for chunk in chunks:
                        yield chunk
                finally:
                    await chunks.aclose()
        finally:
            await close_upstream()

    response = StreamingResponse(stream_body(), status_code=upstream.status_code, background=BackgroundTask(close_upstream))
    response.raw_headers = response_headers
    return response


class CrewLeasedResponse(Response):
    def __init__(self, request: Request, target: ResolvedCrewTarget, lease):
        super().__init__()
        self.request = request
        self.target = target
        self.inner = None
        self.lease = lease

    async def __call__(self, scope, receive, send) -> None:
        response_started = False

        async def send_response(message):
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        async def deliver():
            self.inner = await forward_crew_http(self.request, self.target)
            await self.inner(scope, receive, send_response)

        async def maintain_lease():
            while True:
                await asyncio.sleep(min(RENEW_SECONDS, self.lease.remaining()))
                if not await self.lease.renew():
                    return

        delivery = asyncio.create_task(deliver())
        heartbeat = asyncio.create_task(maintain_lease())
        try:
            done, _ = await asyncio.wait({delivery, heartbeat}, return_when=asyncio.FIRST_COMPLETED)
            if heartbeat in done and not delivery.done():
                delivery.cancel()
                await asyncio.gather(delivery, return_exceptions=True)
                if not response_started:
                    await JSONResponse({"error": "crew_admission_unavailable"}, status_code=503)(scope, receive, send)
            else:
                await delivery
        finally:
            delivery.cancel()
            heartbeat.cancel()
            await asyncio.gather(delivery, heartbeat, return_exceptions=True)
            try:
                if isinstance(self.inner, StreamingResponse) and self.inner.background is not None:
                    await self.inner.background()
            finally:
                await self.lease.release()


async def crew_rate_and_connection(request: Request, target: ResolvedCrewTarget, identity: TokenContext) -> Response:
    # The proxy applies no per-user request-rate admission (#8223 cause 24): the owner's
    # model RPM is enforced on the model route itself, and debiting it here refused 188
    # of a code-split SPA's ~213 first-load chunk requests with 429 (dev, 2026-10-20).
    # The per-user connection lease is the only proxy-level bound; it covers assets,
    # API calls, SSE and WebSocket upgrades alike (cause 22).
    try:
        registry = crew_connection_registry()
        lease = await registry.acquire(target.lease.tenant_id, target.lease.user_id)
    except (CrewConnectionLimitError, CrewConnectionUnavailableError) as exc:
        status = 429 if isinstance(exc, CrewConnectionLimitError) else 503
        return JSONResponse({"error": "crew_connection_limit" if status == 429 else "crew_admission_unavailable"}, status_code=status)
    return CrewLeasedResponse(request, target, lease)


async def record_crew_developer_activity(db: AsyncSession, target: ResolvedCrewTarget) -> None:
    if target.lease.target_kind != "ec2-instance":
        return
    box = await db.scalar(
        select(DevBox)
        .where(
            DevBox.id == target.lease.target_ref,
            DevBox.user_id == target.lease.user_id,
            DevBox.org_id == target.lease.tenant_id,
            DevBox.machine_generation == target.lease.generation,
        )
        .with_for_update()
    )
    if (
        box is None
        or await current_host(
            db,
            box_id=box.id,
            generation=box.machine_generation,
            provider_instance_id=target.host_instance_ref,
            user_id=box.user_id,
            org_id=box.org_id,
            deployment_id=box.deployment_id,
            run_id=target.host_run_id,
        )
        is None
    ):
        raise HTTPException(409, detail={"error": "lease_fenced"})
    await accept_box_activity(db, box, ActivityRequest(kind="host"), datetime.now(UTC))
    await db.commit()


def create_crew_dashboard() -> FastAPI:
    dashboard = FastAPI()

    @dashboard.get("/crew/handoff")
    async def crew_handoff(
        request: Request,
        enabled: Annotated[None, Depends(crew_proxy_enabled)],
        identity: Annotated[TokenContext, Depends(crew_identity)],
        db: Annotated[AsyncSession, Depends(get_db)],
        resolver: Annotated[CrewTargetResolver, Depends(get_target_resolver)],
    ):
        now = int(time.time())
        ticket = presented_handoff_ticket(request)
        claims = handoff_ticket_claims(ticket, now=now) if ticket is not None else None
        if claims is not None:
            if claims["tenant"] != identity.org_id or claims["user"] != identity.user_id:
                raise _refuse_handoff_ticket()
            # Burn the nonce before any further work so a replayed ticket never reaches the
            # custody mint, even when the first presentation is still in flight.
            try:
                if not await consume_handoff_ticket(claims, now=now):
                    raise _refuse_handoff_ticket()
            except HandoffTicketUnavailableError:
                raise HTTPException(503, detail={"error": "crew_handoff_unavailable"}, headers={"Cache-Control": "no-store"}) from None
        owner = await current_crew_owner(identity, db)
        target = await active_crew_target(owner, resolver)
        if claims is not None and (
            claims["target_kind"] != target.lease.target_kind
            or claims["target_ref"] != target.lease.target_ref
            or claims["generation"] != target.lease.generation
        ):
            raise HTTPException(409, detail={"error": "lease_fenced"}, headers={"Cache-Control": "no-store"})
        wants_json = claims is None and "application/json" in request.headers.get("accept", "").lower()
        try:
            link = await mint_crew_link_when_ready(owner, target, now=now)
        except HTTPException as exc:
            # The ticket is already burned by design; the page sends the owner back
            # through the Dev Box open flow, which mints a fresh one.
            if _mint_still_starting(exc) and not wants_json:
                return _still_starting_page()
            raise
        try:
            db.add(
                AuditLog(
                    org_id=owner.tenant_id,
                    actor_id=owner.user_id,
                    event_type="crew_dashboard_handoff",
                    details={"target_ref": target.lease.target_ref, "lease_generation": target.lease.generation},
                )
            )
            await db.commit()
        except SQLAlchemyError:
            await db.rollback()
            raise HTTPException(503, detail={"error": "crew_handoff_unavailable"}) from None
        destination = f"{os.environ['CREW_DASHBOARD_ORIGIN'].rstrip('/')}/?token={quote(link, safe='')}"
        if wants_json:
            response = JSONResponse({"redirect_url": destination}, headers={"Cache-Control": "no-store"})
        else:
            # A top-level navigation lands here, so this Set-Cookie is first-party even when the
            # ADP frontend is cross-site; the redirect then continues to the Kiro Crew login link.
            response = RedirectResponse(destination, status_code=302, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
        response.set_cookie(
            "adp_crew",
            mint_crew_ticket(owner, target, now=now),
            max_age=CREW_TICKET_LIFETIME_SECONDS,
            secure=True,
            httponly=True,
            samesite="lax",
            path="/",
        )
        return response

    @dashboard.get("/health")
    async def health():
        return {"status": "healthy"}

    @dashboard.api_route("/", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
    @dashboard.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
    async def crew_http(
        request: Request,
        target: Annotated[ResolvedCrewTarget, Depends(crew_access)],
        identity: Annotated[TokenContext, Depends(crew_identity)],
        db: Annotated[AsyncSession, Depends(get_db)],
    ):
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            await record_crew_developer_activity(db, target)
        return await crew_rate_and_connection(request, target, identity)

    @dashboard.websocket("/")
    @dashboard.websocket("/{path:path}")
    async def crew_websocket(
        websocket: WebSocket,
        target: Annotated[ResolvedCrewTarget, Depends(crew_access)],
        identity: Annotated[TokenContext, Depends(crew_identity)],
        db: Annotated[AsyncSession, Depends(get_db)],
    ):
        if target.lease.target_kind == "ec2-instance":

            async def on_activity():
                await record_crew_developer_activity(db, target)

            await forward_crew_websocket(websocket, target, on_activity=on_activity)
        else:
            await forward_crew_websocket(websocket, target)

    return dashboard


def install_crew_dashboard(app: FastAPI) -> None:
    origin = urlsplit(os.environ.get("CREW_DASHBOARD_ORIGIN", ""))
    if origin.scheme == "https" and origin.hostname and origin.port is None and origin.path in ("", "/") and not origin.query and not origin.fragment:
        dashboard = create_crew_dashboard()
        dashboard.state.ratelimit_service = getattr(app.state, "ratelimit_service", None)
        app.router.routes.insert(0, Host(origin.hostname, app=dashboard, name="crew-dashboard"))
