"""First-party Crew handoff: a signed single-use ticket redeemed on a top-level navigation (#7902)."""

import io
import json
import logging
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import fakeredis.aioredis
import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from starlette.testclient import WebSocketDenialResponse

from src.crew import dashboard as dashboard_module
from src.crew import handoff_tickets, proxy
from src.crew import websocket as websocket_module
from src.crew.dashboard import crew_identity
from src.crew.handoff_tickets import (
    HANDOFF_TICKET_LIFETIME_SECONDS,
    HandoffTicketRegistry,
    HandoffTicketUnavailableError,
    mint_handoff_ticket,
    verify_handoff_ticket,
)
from src.crew.proxy import current_crew_owner, get_target_resolver
from src.crew.targets import CrewLease, CrewLeaseFencedError, CrewOwner, CrewTargetMissingError, ResolvedCrewTarget
from src.crew.tickets import verify_crew_ticket
from src.shared.database import get_db
from src.shared.models.audit import AuditLog
from tests.crew.test_dashboard import FakeCrewLease, FakeCrewWebSocket, build_dashboard, identity, signed_cookie

CREW = "https://crew.example.test"
NOW = 1_800_000_000


def owner(user_id="user-a"):
    return CrewOwner("tenant-a", user_id)


def target(user_id="user-a", *, generation=1, target_ref="pod-a", target_kind="eks-pod"):
    endpoint = f"http://pod-{user_id[-1]}.test"
    return ResolvedCrewTarget(
        endpoint,
        CrewLease(
            tenant_id="tenant-a",
            user_id=user_id,
            target_ref=target_ref,
            target_kind=target_kind,
            generation=generation,
            expires_at=4_000_000_000,
            endpoint=endpoint,
        ),
        "ready",
    )


def ticket_for(user_id="user-a", *, now=None, **lease):
    return mint_handoff_ticket(owner(user_id), target(user_id, **lease), now=int(time.time()) if now is None else now)


@pytest.fixture
def dashboard(monkeypatch):
    return build_dashboard(monkeypatch)


@pytest.fixture
def registry(monkeypatch):
    server = fakeredis.FakeServer()
    monkeypatch.setattr(
        handoff_tickets, "handoff_ticket_registry", lambda: HandoffTicketRegistry(fakeredis.aioredis.FakeRedis(server=server), "crew-test")
    )
    return server


@pytest.fixture
def link_mint(monkeypatch):
    mint = AsyncMock(side_effect=lambda owner, target, *, now: f"link-token-{target.lease.target_ref}-g{target.lease.generation}")
    monkeypatch.setattr(dashboard_module, "mint_crew_link", mint)
    return mint


# --- ticket primitive -------------------------------------------------------------------


def test_ticket_is_owner_and_lease_bound_short_lived_and_tamper_evident():
    ticket = ticket_for(now=NOW)
    claims = verify_handoff_ticket(ticket, now=NOW + 1)
    assert (claims["tenant"], claims["user"], claims["target_kind"], claims["target_ref"], claims["generation"]) == (
        "tenant-a",
        "user-a",
        "eks-pod",
        "pod-a",
        1,
    )
    assert claims["expires"] - claims["issued"] == HANDOFF_TICKET_LIFETIME_SECONDS <= 60
    assert ticket_for(now=NOW) != ticket  # fresh nonce on every mint
    with pytest.raises(ValueError):
        verify_handoff_ticket(ticket, now=NOW + HANDOFF_TICKET_LIFETIME_SECONDS)  # expired
    with pytest.raises(ValueError):
        verify_handoff_ticket(ticket, now=NOW - 10)  # issued in the future
    payload, signature = ticket.split(".")
    with pytest.raises(ValueError):
        verify_handoff_ticket(f"{payload}.{signature[:-2]}AA", now=NOW)
    forged = handoff_tickets._encode(json.dumps({**claims, "user": "user-b"}, sort_keys=True, separators=(",", ":")).encode())
    with pytest.raises(ValueError):
        verify_handoff_ticket(f"{forged}.{signature}", now=NOW)
    for junk in ("", "x", "a.b.c", "." * 10, "A" * 3000):
        with pytest.raises(ValueError):
            verify_handoff_ticket(junk, now=NOW)
    with pytest.raises(ValueError):
        verify_crew_ticket(ticket)  # a handoff ticket is never a dashboard cookie
    with pytest.raises(ValueError):
        mint_handoff_ticket(owner("user-b"), target("user-a"), now=NOW)  # foreign owner cannot mint


async def test_registry_burns_each_nonce_once_and_fails_closed(registry):
    claims = verify_handoff_ticket(ticket_for(now=NOW), now=NOW)
    assert await handoff_tickets.consume_handoff_ticket(claims, now=NOW) is True
    assert await handoff_tickets.consume_handoff_ticket(claims, now=NOW) is False
    other = verify_handoff_ticket(ticket_for(now=NOW), now=NOW)
    assert await handoff_tickets.consume_handoff_ticket(other, now=NOW) is True

    class Broken:
        async def set(self, *args, **kwargs):
            raise ConnectionError("redis down")

        async def aclose(self):
            pass

    with pytest.raises(HandoffTicketUnavailableError):
        await HandoffTicketRegistry(Broken(), "crew-test").consume(claims, now=NOW)


# --- ADP-origin mint route ----------------------------------------------------------------


@pytest.fixture
async def adp(monkeypatch):
    monkeypatch.setenv("CREW_PROXY_ENABLED", "true")
    monkeypatch.setenv("CREW_DASHBOARD_ORIGIN", CREW)
    app = FastAPI()
    app.include_router(proxy.router)
    state = {"owner": owner(), "target": target()}
    app.dependency_overrides[current_crew_owner] = lambda: state["owner"]

    class Resolver:
        async def resolve(self, who, *, now):
            if isinstance(state["target"], Exception):
                raise state["target"]
            return state["target"]

    app.dependency_overrides[get_target_resolver] = lambda: Resolver()
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://adp.example.test") as http:
        yield http, state


async def test_mint_route_returns_a_crew_host_navigation_url_bound_to_the_active_lease(adp):
    http, _ = adp
    before = int(time.time())
    response = await http.post("/crew/handoff-ticket")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["expires_in"] == HANDOFF_TICKET_LIFETIME_SECONDS
    assert body["handoff_url"].startswith(f"{CREW}/crew/handoff?ticket=")
    ticket = body["handoff_url"].split("ticket=", 1)[1]
    assert "&" not in ticket and "%" not in ticket
    claims = verify_handoff_ticket(ticket, now=before)
    assert (claims["tenant"], claims["user"], claims["target_ref"], claims["generation"]) == ("tenant-a", "user-a", "pod-a", 1)
    assert before <= claims["issued"] <= before + 5


@pytest.mark.parametrize(
    ("failure", "status"),
    [(CrewTargetMissingError("missing"), 404), (CrewLeaseFencedError("fenced"), 409)],
)
async def test_mint_route_refuses_without_an_active_lease(adp, failure, status):
    http, state = adp
    state["target"] = failure
    response = await http.post("/crew/handoff-ticket")
    assert response.status_code == status
    assert "handoff_url" not in response.text


async def test_mint_route_is_hidden_when_the_crew_proxy_is_off_or_misconfigured(adp, monkeypatch):
    http, _ = adp
    monkeypatch.setenv("CREW_PROXY_ENABLED", "false")
    assert (await http.post("/crew/handoff-ticket")).status_code == 404
    monkeypatch.setenv("CREW_PROXY_ENABLED", "true")
    for origin in ("http://crew.example.test", "https://crew.example.test:8443", "https://crew.example.test/path", ""):
        monkeypatch.setenv("CREW_DASHBOARD_ORIGIN", origin)
        assert (await http.post("/crew/handoff-ticket")).status_code == 404


# --- Crew-host redemption -----------------------------------------------------------------


def no_cookie(response):
    assert "set-cookie" not in response.headers
    return response


async def test_valid_ticket_sets_first_party_cookie_and_redirects_to_the_login_link(dashboard, registry, link_mint):
    app, host_app, _, _ = dashboard
    ticket = ticket_for()
    async with AsyncClient(transport=ASGITransport(app=app), base_url=CREW) as client:
        response = await client.get(f"/crew/handoff?ticket={ticket}", headers={"Accept": "text/html"}, follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == f"{CREW}/?token=link-token-pod-a-g1"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    cookie = response.headers["set-cookie"]
    attributes = {part.strip().lower() for part in cookie.split(";")[1:]}
    assert cookie.startswith("adp_crew=")
    assert {"secure", "httponly", "samesite=lax", "path=/"} <= attributes
    assert not any(attribute.startswith("domain=") for attribute in attributes)
    claims = verify_crew_ticket(cookie.split(";")[0].split("=", 1)[1])
    assert (claims["tenant"], claims["user"], claims["target_ref"], claims["generation"]) == ("tenant-a", "user-a", "pod-a", 1)
    link_mint.assert_awaited_once()
    audit_db = host_app.dependency_overrides[get_db]()
    assert len(audit_db.rows) == 1 and isinstance(audit_db.rows[0], AuditLog)
    assert audit_db.rows[0].event_type == "crew_dashboard_handoff"
    assert ticket not in json.dumps(audit_db.rows[0].details)


async def test_ticket_path_never_answers_json_or_needs_a_bearer(dashboard, registry, link_mint, monkeypatch):
    app, _, _, _ = dashboard

    async def no_bearer(request, authorization):
        pytest.fail("ticket redemption must not consult the ADP bearer path")

    monkeypatch.setattr(dashboard_module, "get_current_user", no_bearer)
    async with AsyncClient(transport=ASGITransport(app=app), base_url=CREW) as client:
        response = await client.get(
            f"/crew/handoff?ticket={ticket_for()}",
            headers={"Accept": "application/json", "Authorization": "Bearer stale"},
            follow_redirects=False,
        )
    assert response.status_code == 302
    assert "redirect_url" not in response.text


async def test_replayed_ticket_is_refused_without_a_cookie_or_a_second_mint(dashboard, registry, link_mint):
    app, _, _, _ = dashboard
    ticket = ticket_for()
    async with AsyncClient(transport=ASGITransport(app=app), base_url=CREW) as client:
        first = await client.get(f"/crew/handoff?ticket={ticket}", follow_redirects=False)
        assert first.status_code == 302
        client.cookies.clear()
        replay = no_cookie(await client.get(f"/crew/handoff?ticket={ticket}", follow_redirects=False))
    assert replay.status_code == 401
    assert replay.json() == {"detail": {"error": "crew_handoff_refused"}}
    assert replay.headers["cache-control"] == "no-store"
    link_mint.assert_awaited_once()


@pytest.mark.parametrize(
    "bad_ticket",
    [
        pytest.param(lambda: ticket_for(now=int(time.time()) - HANDOFF_TICKET_LIFETIME_SECONDS - 1), id="expired"),
        pytest.param(lambda: ticket_for()[:-3] + "xyz", id="tampered-signature"),
        pytest.param(lambda: "not-a-ticket", id="garbage"),
        pytest.param(lambda: signed_cookie().split("=", 1)[1], id="dashboard-cookie-as-ticket"),
    ],
)
async def test_invalid_tickets_are_refused_before_touching_redis_or_the_lease(dashboard, registry, link_mint, bad_ticket):
    app, _, resolved, _ = dashboard
    async with AsyncClient(transport=ASGITransport(app=app), base_url=CREW) as client:
        response = no_cookie(await client.get(f"/crew/handoff?ticket={bad_ticket()}", follow_redirects=False))
    assert response.status_code == 401
    assert response.json() == {"detail": {"error": "crew_handoff_refused"}}
    assert resolved == []
    link_mint.assert_not_awaited()
    assert fakeredis.FakeStrictRedis(server=registry).dbsize() == 0  # nonce never burned for a refused ticket


async def test_duplicate_or_extra_query_parameters_are_refused(dashboard, registry, link_mint):
    app, _, _, _ = dashboard
    ticket = ticket_for()
    async with AsyncClient(transport=ASGITransport(app=app), base_url=CREW) as client:
        for query in (f"ticket={ticket}&ticket={ticket}", f"ticket={ticket}&next=/evil"):
            response = no_cookie(await client.get(f"/crew/handoff?{query}", follow_redirects=False))
            assert response.status_code == 401
    link_mint.assert_not_awaited()


async def test_foreign_owner_ticket_cannot_open_another_users_crew(dashboard, registry, link_mint):
    app, _, _, _ = dashboard
    # user-b has no Crew target in this fixture; a ticket naming user-b is honestly signed
    # but resolves nothing, and must never produce a cookie for user-a's pod.
    async with AsyncClient(transport=ASGITransport(app=app), base_url=CREW) as client:
        response = no_cookie(await client.get(f"/crew/handoff?ticket={ticket_for('user-b', target_ref='pod-a')}", follow_redirects=False))
    assert response.status_code == 404
    link_mint.assert_not_awaited()


@pytest.mark.parametrize(
    "lease",
    [
        pytest.param({"generation": 2}, id="older-generation"),
        pytest.param({"target_ref": "pod-z"}, id="other-target"),
        pytest.param({"target_kind": "ec2-instance"}, id="other-kind"),
    ],
)
async def test_ticket_for_a_different_lease_is_refused(dashboard, registry, link_mint, lease):
    app, _, _, _ = dashboard
    async with AsyncClient(transport=ASGITransport(app=app), base_url=CREW) as client:
        response = no_cookie(await client.get(f"/crew/handoff?ticket={ticket_for(**lease)}", follow_redirects=False))
    assert response.status_code == 409
    assert response.json() == {"detail": {"error": "lease_fenced"}}
    link_mint.assert_not_awaited()


@pytest.mark.parametrize("state", ["fenced", "expired", "not-ready", "missing"])
async def test_inactive_lease_refuses_the_ticket(dashboard, registry, link_mint, state):
    app, host_app, _, _ = dashboard

    class Resolver:
        async def resolve(self, who, *, now):
            if state == "fenced":
                raise CrewLeaseFencedError("fenced")
            if state == "missing":
                raise CrewTargetMissingError("missing")
            current = target()
            if state == "expired":
                return ResolvedCrewTarget(current.endpoint, current.lease.model_copy(update={"expires_at": now}), "ready")
            return ResolvedCrewTarget(current.endpoint, current.lease, "not-ready")

    host_app.dependency_overrides[get_target_resolver] = lambda: Resolver()
    async with AsyncClient(transport=ASGITransport(app=app), base_url=CREW) as client:
        response = no_cookie(await client.get(f"/crew/handoff?ticket={ticket_for()}", follow_redirects=False))
    assert response.status_code == (404 if state == "missing" else 409)
    link_mint.assert_not_awaited()


async def test_registry_outage_fails_closed(dashboard, link_mint, monkeypatch):
    app, _, _, _ = dashboard

    def unavailable():
        raise HandoffTicketUnavailableError

    monkeypatch.setattr(handoff_tickets, "handoff_ticket_registry", unavailable)
    async with AsyncClient(transport=ASGITransport(app=app), base_url=CREW) as client:
        response = no_cookie(await client.get(f"/crew/handoff?ticket={ticket_for()}", follow_redirects=False))
    assert response.status_code == 503
    assert response.json() == {"detail": {"error": "crew_handoff_unavailable"}}
    link_mint.assert_not_awaited()


async def test_ticket_is_not_an_identity_anywhere_except_the_handoff_route(dashboard, registry, monkeypatch):
    app, _, resolved, _ = dashboard
    monkeypatch.setattr(dashboard_module, "crew_http_client", lambda: pytest.fail("pod contacted"))
    async with AsyncClient(transport=ASGITransport(app=app), base_url=CREW) as client:
        for path in ("/", "/api/chat/slots", "/crew/handoff/", "/CREW/handoff"):
            response = no_cookie(await client.get(f"{path}?ticket={ticket_for()}"))
            assert response.status_code in (401, 403), path
    monkeypatch.setattr(websocket_module, "crew_websocket_connect", lambda *args, **kwargs: pytest.fail("pod contacted"))
    with TestClient(app, base_url=CREW) as client:
        with pytest.raises(WebSocketDenialResponse) as upgrade:
            with client.websocket_connect(f"ws://crew.example.test/crew/handoff?ticket={ticket_for()}"):
                pass
    assert upgrade.value.status_code == 401
    assert resolved == []


async def test_bearer_handoff_keeps_working_for_same_site_edges(dashboard, registry, link_mint, monkeypatch):
    app, _, _, _ = dashboard

    async def authenticated_user(request, authorization):
        if authorization != "Bearer valid":
            raise HTTPException(401, detail={"error": "invalid_adp_session"})
        return identity("user-a")

    monkeypatch.setattr(dashboard_module, "get_current_user", authenticated_user)
    async with AsyncClient(transport=ASGITransport(app=app), base_url=CREW) as client:
        as_json = await client.get("/crew/handoff", headers={"Authorization": "Bearer valid", "Accept": "application/json"}, follow_redirects=False)
        assert as_json.status_code == 200
        assert as_json.json() == {"redirect_url": f"{CREW}/?token=link-token-pod-a-g1"}
        assert as_json.headers["set-cookie"].startswith("adp_crew=")
        client.cookies.clear()
        as_navigation = await client.get("/crew/handoff", headers={"Authorization": "Bearer valid"}, follow_redirects=False)
        assert as_navigation.status_code == 302
        client.cookies.clear()
        anonymous = no_cookie(await client.get("/crew/handoff", follow_redirects=False))
        assert anonymous.status_code == 401


async def test_ticket_never_reaches_request_logs(dashboard, registry, link_mint, monkeypatch):
    from src.admin.middleware import RequestLoggingMiddleware
    from src.auth.middleware import TokenContextMiddleware
    from src.shared.logging import StructuredJsonFormatter
    from src.shared.middleware.logging_middleware import LoggingMiddleware

    app, _, _, _ = dashboard
    recorded = AsyncMock()
    monkeypatch.setattr(RequestLoggingMiddleware, "_log_request", recorded)
    app.add_middleware(LoggingMiddleware)
    app.add_middleware(RequestLoggingMiddleware)
    app.add_middleware(TokenContextMiddleware)
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    handler.setFormatter(StructuredJsonFormatter())
    logger = logging.getLogger("src.shared.middleware.logging_middleware")
    monkeypatch.setattr(logger, "level", logging.INFO)
    logger.addHandler(handler)
    monkeypatch.setattr(
        dashboard_module,
        "crew_http_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"kiro")), trust_env=False),
    )
    ticket = ticket_for()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url=CREW) as client:
            response = await client.get(f"/crew/handoff?ticket={ticket}", follow_redirects=False)
            assert response.status_code == 302
            login = await client.get(response.headers["location"], headers={"Cookie": signed_cookie()}, follow_redirects=False)
            assert login.status_code == 200
    finally:
        logger.removeHandler(handler)
    lines = output.getvalue()
    assert "Request started" in lines
    assert ticket not in lines and "link-token-pod-a-g1" not in lines
    assert "ticket=[redacted]" in lines
    assert recorded.await_count >= 1
    for call in recorded.await_args_list:
        row = json.dumps(call.kwargs, default=str)
        assert ticket not in row and "link-token-pod-a-g1" not in row
    assert recorded.await_args_list[0].kwargs["query_params"] == {"ticket": "[redacted]"}


# --- WebSocket Origin check on a CloudFront Crew origin -------------------------------------


@pytest.mark.parametrize(
    ("origin", "accepted"),
    [
        ("https://d111crew.cloudfront.net", True),
        (None, True),
        ("https://d222frontend.cloudfront.net", False),
        ("https://crew.example.test", False),
        ("http://d111crew.cloudfront.net", False),
        ("null", False),
    ],
)
def test_websocket_origin_must_equal_the_cloudfront_crew_origin(monkeypatch, origin, accepted):
    app, host_app, _, _ = build_dashboard(monkeypatch, "https://d111crew.cloudfront.net")
    host_app.dependency_overrides[crew_identity] = lambda: identity("user-a")
    connections = []
    monkeypatch.setattr(websocket_module, "crew_connection_registry", lambda: SimpleNamespace(acquire=AsyncMock(return_value=FakeCrewLease())))
    monkeypatch.setattr(websocket_module, "crew_websocket_connect", lambda uri, **kwargs: connections.append((uri, kwargs)) or FakeCrewWebSocket())
    headers = {"Cookie": signed_cookie(), **({"Origin": origin} if origin is not None else {})}
    with TestClient(app, base_url="https://d111crew.cloudfront.net") as client:
        if accepted:
            with client.websocket_connect("ws://d111crew.cloudfront.net/api/live", headers=headers) as browser:
                browser.send_text("hello")
                assert browser.receive_text() == "hello"
                browser.send_text("close")
                # Observe the relay's close before TestClient cancels its app task.
                # Exiting immediately races the fake upstream's end-of-stream.
                assert browser.receive() == {"type": "websocket.close", "code": 1000, "reason": ""}
            assert connections[0][0] == "ws://d111crew.cloudfront.net/api/live"
            assert connections[0][1]["origin"] == origin
        else:
            with pytest.raises(WebSocketDenialResponse) as refused:
                with client.websocket_connect("ws://d111crew.cloudfront.net/api/live", headers=headers):
                    pass
            assert refused.value.status_code == 403
            assert connections == []


def test_cloudfront_crew_host_redeems_tickets_minted_for_it(monkeypatch, registry, link_mint):
    app, _, _, _ = build_dashboard(monkeypatch, "https://d111crew.cloudfront.net")
    with TestClient(app, base_url="https://d111crew.cloudfront.net") as client:
        response = client.get(f"/crew/handoff?ticket={ticket_for()}", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "https://d111crew.cloudfront.net/?token=link-token-pod-a-g1"
    assert response.headers["set-cookie"].startswith("adp_crew=")
    assert "samesite=lax" in response.headers["set-cookie"].lower()


# --- #9619: the handoff waits while the host's Crew is still starting -------------------


def _still_starting():
    return HTTPException(503, detail={"error": "crew_mint_unavailable"})


@pytest.fixture
def no_sleep(monkeypatch):
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    clock = {"now": 1000.0}
    monkeypatch.setattr(dashboard_module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(dashboard_module, "_monotonic", lambda: clock["now"])
    return sleeps, clock


async def test_handoff_retries_the_mint_while_the_host_crew_is_starting(dashboard, registry, link_mint, no_sleep):
    sleeps, _ = no_sleep
    link_mint.side_effect = [_still_starting(), _still_starting(), "link-token-pod-a-g1"]
    app, host_app, _, _ = dashboard
    async with AsyncClient(transport=ASGITransport(app=app), base_url=CREW) as client:
        response = await client.get(f"/crew/handoff?ticket={ticket_for()}", headers={"Accept": "text/html"}, follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == f"{CREW}/?token=link-token-pod-a-g1"
    assert link_mint.await_count == 3
    assert sleeps == [dashboard_module.CREW_HANDOFF_RETRY_INTERVAL_SECONDS] * 2
    audit_db = host_app.dependency_overrides[get_db]()
    assert [row.event_type for row in audit_db.rows] == ["crew_dashboard_handoff"]


async def test_handoff_gives_up_with_a_readable_page_inside_the_edge_timeout(dashboard, registry, link_mint, no_sleep, monkeypatch):
    sleeps, clock = no_sleep
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "https://app.example.test, https://other.example.test")

    async def never_ready(owner, target, *, now):
        clock["now"] += 6.0  # each refused mint costs the ingress' own upstream wait
        raise _still_starting()

    link_mint.side_effect = never_ready
    app, host_app, _, _ = dashboard
    async with AsyncClient(transport=ASGITransport(app=app), base_url=CREW) as client:
        response = await client.get(f"/crew/handoff?ticket={ticket_for()}", headers={"Accept": "text/html"}, follow_redirects=False)
    assert response.status_code == 503
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["cache-control"] == "no-store"
    assert "still starting" in response.text and "https://app.example.test/settings/dev-box/open" in response.text
    assert "set-cookie" not in response.headers
    # 20 s budget, 6 s per attempt, 2 s pause: three attempts, then stop with time to answer.
    assert link_mint.await_count == 3 and len(sleeps) == 2
    audit_db = host_app.dependency_overrides[get_db]()
    assert audit_db.rows == []


async def test_handoff_does_not_retry_a_fenced_lease_or_a_json_caller(dashboard, registry, link_mint, no_sleep, monkeypatch):
    sleeps, _ = no_sleep
    link_mint.side_effect = HTTPException(409, detail={"error": "lease_fenced"})
    app, _, _, _ = dashboard
    async with AsyncClient(transport=ASGITransport(app=app), base_url=CREW) as client:
        response = await client.get(f"/crew/handoff?ticket={ticket_for()}", headers={"Accept": "text/html"}, follow_redirects=False)
    assert response.status_code == 409 and response.json() == {"detail": {"error": "lease_fenced"}}
    assert link_mint.await_count == 1 and sleeps == []

    # A bearer caller asking for JSON (no ticket) keeps the machine-readable 503 after the wait.
    async def authenticated_user(request, authorization):
        return identity("user-a")

    monkeypatch.setattr(dashboard_module, "get_current_user", authenticated_user)
    link_mint.reset_mock()
    link_mint.side_effect = _still_starting()
    # A budget shorter than one retry interval means a single attempt, then the error as-is.
    monkeypatch.setattr(dashboard_module, "CREW_HANDOFF_WAIT_BUDGET_SECONDS", 1.0)
    async with AsyncClient(transport=ASGITransport(app=app), base_url=CREW) as client:
        response = await client.get("/crew/handoff", headers={"Accept": "application/json", "Authorization": "Bearer valid"}, follow_redirects=False)
    assert response.status_code == 503 and response.json() == {"detail": {"error": "crew_mint_unavailable"}}
