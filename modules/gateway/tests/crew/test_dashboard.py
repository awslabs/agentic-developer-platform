"""Dashboard host never bypasses the ADP owner and target gate."""

import asyncio
import io
import json
import logging
import time
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import fakeredis.aioredis
import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import SQLAlchemyError
from starlette.testclient import WebSocketDenialResponse
from starlette.websockets import WebSocketDisconnect

from src.agentauth.chat_capability import ChatAuthorizationRefusedError
from src.crew import connections as connections_module
from src.crew import dashboard as dashboard_module
from src.crew import handoff as handoff_module
from src.crew import proxy
from src.crew import websocket as websocket_module
from src.crew.connections import CrewConnectionLimitError, CrewConnectionRegistry
from src.crew.dashboard import crew_identity, install_crew_dashboard
from src.crew.proxy import get_target_resolver
from src.crew.targets import CrewLease, CrewLeaseFencedError, CrewOwner, CrewTargetMissingError, ResolvedCrewTarget
from src.crew.tickets import mint_crew_ticket
from src.shared.database import get_db
from src.shared.models.audit import AuditLog
from src.shared.schemas.auth import TokenContext


@pytest.fixture
def dashboard(monkeypatch):
    return build_dashboard(monkeypatch)


def build_dashboard(monkeypatch, origin="https://crew.example.test"):
    """Gateway app with the Crew host mounted for `origin`; `dashboard` uses the default same-site host."""
    monkeypatch.setenv("CREW_DASHBOARD_ORIGIN", origin)
    monkeypatch.setenv("CREW_PROXY_ENABLED", "true")
    resolved = []
    upstream = []
    app = FastAPI()

    class RateService:
        allowed = True
        calls = []

        async def check_rate_limit(self, context):
            self.calls.append(("check", context.user_id))
            return SimpleNamespace(allowed=self.allowed, retry_after_seconds=5)

        async def consume_rate_limit(self, context):
            self.calls.append(("consume", context.user_id))
            return SimpleNamespace(allowed=self.allowed, retry_after_seconds=5)

        async def release_concurrent(self, context):
            self.calls.append(("release", context.user_id))

    app.state.ratelimit_service = RateService()

    @app.get("/api/chat/slots")
    async def ordinary_gateway():
        upstream.append("ordinary")
        return {"gateway": True}

    @app.get("/health")
    async def health():
        return {"status": "healthy"}

    install_crew_dashboard(app)
    host_app = app.router.routes[0].app

    class Registry:
        async def acquire(self, tenant_id, user_id):
            return FakeCrewLease()

    monkeypatch.setattr(dashboard_module, "crew_connection_registry", Registry)

    class Resolver:
        async def resolve(self, owner: CrewOwner, *, now: int):
            resolved.append(owner)
            if owner.user_id != "user-a":
                raise CrewTargetMissingError("missing")
            return ResolvedCrewTarget(
                "http://pod-a.test",
                CrewLease(
                    tenant_id="tenant-a", user_id="user-a", target_ref="pod-a", generation=1, expires_at=4_000_000_000, endpoint="http://pod-a.test"
                ),
                "ready",
            )

    async def member(db, tenant_id, subject, *, username=""):
        if tenant_id == "tenant-a" and subject in ("user-a", "user-b"):
            return SimpleNamespace(id=subject)
        raise ChatAuthorizationRefusedError("chat_principal_not_tenant_member")

    class AuditDB:
        def __init__(self):
            self.rows = []
            self.pending = []
            self.fail_commit = False
            self.rollbacks = 0

        def add(self, row):
            self.pending.append(row)

        async def commit(self):
            if self.fail_commit:
                raise SQLAlchemyError("audit unavailable")
            self.rows.extend(self.pending)
            self.pending.clear()

        async def rollback(self):
            self.rollbacks += 1
            self.pending.clear()

    monkeypatch.setattr(proxy, "tenant_member_principal", member)
    audit_db = AuditDB()
    host_app.dependency_overrides[get_db] = lambda: audit_db
    host_app.dependency_overrides[get_target_resolver] = lambda: Resolver()
    return app, host_app, resolved, upstream


def identity(user_id):
    return TokenContext(user_id=user_id, org_id="tenant-a", team_id="", department_id="", account_type="human", expires_at=datetime.now(UTC))


def signed_cookie(user_id="user-a", *, generation=1, now=None, target_ref=None, target_kind="eks-pod"):
    owner = CrewOwner("tenant-a", user_id)
    endpoint = f"http://pod-{user_id[-1]}.test"
    target = ResolvedCrewTarget(
        endpoint,
        CrewLease(
            tenant_id="tenant-a",
            user_id=user_id,
            target_ref=target_ref or f"pod-{user_id[-1]}",
            target_kind=target_kind,
            generation=generation,
            expires_at=4_000_000_000,
            endpoint=endpoint,
        ),
        "ready",
    )
    return f"adp_crew={mint_crew_ticket(owner, target, now=int(time.time()) if now is None else now)}"


async def test_dashboard_host_gates_every_http_path_and_method(dashboard, monkeypatch):
    app, host_app, resolved, upstream = dashboard
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://crew.example.test") as client:
        no_identity = await client.get("/api/chat/slots", headers={"Cookie": "mc_token_5476=owner-a"})
        assert no_identity.status_code == 401
        assert resolved == [] and upstream == []

        host_app.dependency_overrides[crew_identity] = lambda: identity("user-b")
        for method, path in (("GET", "/api/chat/slots"), ("POST", "/api/chat/slots"), ("OPTIONS", "/")):
            response = await client.request(method, path, headers={"Cookie": "mc_token_5476=owner-a"})
            assert response.status_code == 404
            assert "pod-a.test" not in response.text
        assert len(resolved) == 3 and all(owner.user_id == "user-b" for owner in resolved)
        assert upstream == []

        host_app.dependency_overrides[crew_identity] = lambda: identity("user-a")
        posted = []

        async def fake_pod(request):
            posted.append((request.method, await request.aread()))
            return httpx.Response(200, json={"ok": True})

        monkeypatch.setattr(
            dashboard_module,
            "crew_http_client",
            lambda: httpx.AsyncClient(
                transport=httpx.MockTransport(fake_pod),
                trust_env=False,
            ),
        )
        admitted = await client.post("/api/chat/slots", content=b"dashboard-write", headers={"Cookie": signed_cookie()})
        assert admitted.status_code == 200
        assert admitted.json() == {"ok": True}
        assert posted == [("POST", b"dashboard-write")]
        assert resolved[-1].user_id == "user-a" and upstream == []
        healthy = await client.get("/health")
        assert healthy.status_code == 200

    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://gateway.example.test") as client:
        ordinary = await client.get("/api/chat/slots")
    assert ordinary.json() == {"gateway": True}
    assert upstream == ["ordinary"]


async def test_proxy_releases_the_owner_lookup_connection_before_streaming(dashboard, monkeypatch):
    """Each proxied request returns its pooled DB connection before the upstream stream starts.

    FastAPI >= 0.118 runs yield-dependency teardown after the response is sent, so a
    session left in its owner-lookup transaction pinned one of the 2+1 pooled
    connections per worker for the whole proxied stream; a 28-chunk SPA asset burst
    then timed out at pool_timeout and failed with 500s (dev, 2026-10-20, #8223).
    """
    app, host_app, resolved, upstream = dashboard
    audit_db = host_app.dependency_overrides[get_db]()
    host_app.dependency_overrides[crew_identity] = lambda: identity("user-a")
    rollbacks_at_upstream = []

    async def fake_pod(request):
        rollbacks_at_upstream.append(audit_db.rollbacks)
        return httpx.Response(200, content=b"console.log('chunk')", headers={"content-type": "text/javascript"})

    monkeypatch.setattr(
        dashboard_module,
        "crew_http_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(fake_pod), trust_env=False),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://crew.example.test") as client:
        for index, (method, path) in enumerate((("GET", "/assets/index-abc123.js"), ("POST", "/api/chat/slots")), start=1):
            response = await client.request(method, path, headers={"Cookie": signed_cookie()})
            assert response.status_code == 200, (method, path, response.text)
            # The lookup transaction ended before the pod was contacted, so the pooled
            # connection was free while the body streamed.
            assert rollbacks_at_upstream[-1] == index
            assert audit_db.rollbacks == index
    assert resolved and all(owner.user_id == "user-a" for owner in resolved)
    assert upstream == []


async def test_every_proxied_request_takes_a_connection_lease_and_no_rate_token(dashboard, monkeypatch):
    """Concurrency is leased per request; the request-rate service is never consulted.

    #8223 cause 24: the first dashboard load on dev requested ~213 ``/assets/*`` chunks and
    the model bucket (60 rpm -> 15-token burst) refused 188 of them with 429.
    """
    app, host_app, _, _ = dashboard
    host_app.dependency_overrides[crew_identity] = lambda: identity("user-a")
    leases = []

    class Registry:
        async def acquire(self, tenant_id, user_id):
            leases.append((tenant_id, user_id))
            return FakeCrewLease()

    monkeypatch.setattr(dashboard_module, "crew_connection_registry", Registry)
    monkeypatch.setattr(
        dashboard_module,
        "crew_http_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"ok")), trust_env=False),
    )
    app.state.ratelimit_service.calls.clear()
    requests = [
        ("GET", "/assets/index-abc123.js"),
        ("HEAD", "/assets/vendor-def456.css"),
        ("GET", "/favicon.ico"),
        ("GET", "/api/chat/slots"),
        ("POST", "/api/chat/slots"),
        ("GET", "/"),
    ]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://crew.example.test") as client:
        for method, path in requests:
            response = await client.request(method, path, headers={"Cookie": signed_cookie()})
            assert response.status_code == 200, (method, path, response.text)
    assert len(leases) == len(requests) and all(lease == ("tenant-a", "user-a") for lease in leases)
    assert app.state.ratelimit_service.calls == []


async def test_the_connection_cap_is_the_only_proxy_level_refusal(dashboard, monkeypatch):
    """With no rate admission, 429 from the proxy can only mean the per-user connection cap."""
    app, host_app, _, _ = dashboard
    host_app.dependency_overrides[crew_identity] = lambda: identity("user-a")

    class Registry:
        async def acquire(self, tenant_id, user_id):
            raise CrewConnectionLimitError

    monkeypatch.setattr(dashboard_module, "crew_connection_registry", Registry)
    monkeypatch.setattr(dashboard_module, "crew_http_client", lambda: pytest.fail("pod contacted"))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://crew.example.test") as client:
        response = await client.get("/assets/index-abc123.js", headers={"Cookie": signed_cookie()})
    assert response.status_code == 429 and response.json() == {"error": "crew_connection_limit"}


async def test_asset_cache_validators_and_304_are_relayed(dashboard, monkeypatch):
    """ETag / Cache-Control from the dashboard reach the browser, and a conditional reload stays a 304."""
    app, host_app, _, _ = dashboard
    host_app.dependency_overrides[crew_identity] = lambda: identity("user-a")
    seen = []

    def fake_pod(request):
        seen.append(request.headers.get("if-none-match"))
        if request.headers.get("if-none-match") == '"chunk-v1"':
            return httpx.Response(304, headers={"etag": '"chunk-v1"', "cache-control": "public, max-age=31536000, immutable"})
        headers = {"content-type": "text/javascript", "etag": '"chunk-v1"', "cache-control": "public, max-age=31536000, immutable"}
        return httpx.Response(200, content=b"console.log(1)", headers=headers)

    monkeypatch.setattr(dashboard_module, "crew_http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(fake_pod), trust_env=False))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://crew.example.test") as client:
        first = await client.get("/assets/index-abc123.js", headers={"Cookie": signed_cookie()})
        assert first.status_code == 200
        assert first.headers["etag"] == '"chunk-v1"' and first.headers["cache-control"] == "public, max-age=31536000, immutable"
        second = await client.get("/assets/index-abc123.js", headers={"Cookie": signed_cookie(), "If-None-Match": '"chunk-v1"'})
        assert second.status_code == 304 and second.headers["etag"] == '"chunk-v1"'
    assert seen == [None, '"chunk-v1"']


def test_dashboard_websocket_passes_through_same_owner_gate(dashboard):
    app, host_app, resolved, upstream = dashboard
    host_app.dependency_overrides[crew_identity] = lambda: identity("user-a")
    with TestClient(app, base_url="https://crew.example.test") as client:
        host_app.dependency_overrides.pop(crew_identity)
        with pytest.raises(WebSocketDenialResponse) as unauthenticated:
            with client.websocket_connect("ws://crew.example.test/api/live", headers={"Cookie": "mc_token_5476=owner-a"}):
                pass
        assert unauthenticated.value.status_code == 401
        assert resolved == []
        host_app.dependency_overrides[crew_identity] = lambda: identity("user-b")
        with pytest.raises(WebSocketDenialResponse) as other_user:
            with client.websocket_connect("ws://crew.example.test/api/live", headers={"Cookie": "mc_token_5476=owner-a"}):
                pass
        assert other_user.value.status_code == 404
        host_app.dependency_overrides[crew_identity] = lambda: identity("user-a")
        with pytest.raises(WebSocketDenialResponse) as result:
            with client.websocket_connect("ws://crew.example.test/api/live", headers={"Cookie": signed_cookie()}):
                pass
    assert result.value.status_code == 503
    assert resolved == [CrewOwner("tenant-a", "user-b"), CrewOwner("tenant-a", "user-a")]
    assert upstream == []


async def test_dashboard_flag_disabled_fails_closed(dashboard, monkeypatch):
    app, host_app, resolved, upstream = dashboard
    monkeypatch.setenv("CREW_PROXY_ENABLED", "false")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://crew.example.test") as client:
        response = await client.get("/api/chat/slots")
    assert response.status_code == 404
    assert resolved == upstream == []


async def test_fake_pods_receive_only_the_authenticated_owner_request(dashboard, monkeypatch):
    app, host_app, _, upstream = dashboard
    forwarded = []

    class OwnerResolver:
        async def resolve(self, owner, *, now):
            if owner.user_id == "user-c":
                raise CrewTargetMissingError("missing")
            endpoint = f"http://pod-{owner.user_id[-1]}.test"
            return ResolvedCrewTarget(
                endpoint,
                CrewLease(
                    tenant_id=owner.tenant_id,
                    user_id=owner.user_id,
                    target_ref=f"pod-{owner.user_id[-1]}",
                    generation=1,
                    expires_at=4_000_000_000,
                    endpoint=endpoint,
                ),
                "ready",
            )

    def fake_pod(request):
        forwarded.append(request)
        return httpx.Response(200, json={"pod": request.url.host})

    host_app.dependency_overrides[get_target_resolver] = lambda: OwnerResolver()
    monkeypatch.setattr(proxy, "tenant_member_principal", lambda db, tenant, user, **_: async_member(user))
    monkeypatch.setattr(
        dashboard_module,
        "crew_http_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(fake_pod), trust_env=False),
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://crew.example.test") as client:
        for user_id, pod in (("user-a", "pod-a.test"), ("user-b", "pod-b.test")):
            host_app.dependency_overrides[crew_identity] = lambda user_id=user_id: identity(user_id)
            response = await client.get(
                "/api/chat/slots?slot=1&slot=2",
                headers={
                    "Authorization": "Bearer local",
                    "X-Forwarded-For": "untrusted",
                    "Cookie": f"mc_token_5476=owner-{user_id[-1]}; {signed_cookie(user_id)}",
                },
            )
            assert response.status_code == 200
            assert response.json() == {"pod": pod}

        host_app.dependency_overrides[crew_identity] = lambda: identity("user-c")
        missing = await client.get("/api/chat/slots", headers={"Cookie": "mc_token_5476=owner-a"})
        assert missing.status_code == 404

    assert len(forwarded) == 2 and upstream == []
    for request in forwarded:
        assert request.url.path == "/api/chat/slots"
        assert request.url.query == b"slot=1&slot=2"
        assert "authorization" not in request.headers
        assert request.headers["host"] == "crew.example.test"
        assert request.headers["cookie"] == f"mc_token_5476=owner-{request.url.host.split('.')[0][-1]}"
        assert request.headers["x-forwarded-host"] == "crew.example.test"
        assert request.headers["x-forwarded-proto"] == "https"
        assert request.headers["x-forwarded-for"] != "untrusted"


async def async_member(user_id):
    if user_id in {"user-a", "user-b", "user-c"}:
        return SimpleNamespace(id=user_id)
    raise ChatAuthorizationRefusedError("chat_principal_not_tenant_member")


async def test_foreign_crew_cookie_never_reaches_its_owners_pod(dashboard, monkeypatch):
    app, host_app, _, _ = dashboard
    pod_requests = {"pod-a.test": [], "pod-b.test": []}

    class PairResolver:
        async def resolve(self, owner, *, now):
            pod = f"pod-{owner.user_id[-1]}.test"
            endpoint = f"http://{pod}"
            return ResolvedCrewTarget(
                endpoint,
                CrewLease(
                    tenant_id=owner.tenant_id, user_id=owner.user_id, target_ref=pod, generation=1, expires_at=4_000_000_000, endpoint=endpoint
                ),
                "ready",
            )

    def fake_pod(request):
        pod = request.url.host
        pod_requests[pod].append(request)
        expected_cookie = f"mc_token_5476=owner-{pod.split('.')[0][-1]}"
        if request.headers.get("cookie") != expected_cookie:
            return httpx.Response(404, json={"error": "no_owner_session"})
        return httpx.Response(200, json={"pod": pod})

    host_app.dependency_overrides[get_target_resolver] = lambda: PairResolver()
    monkeypatch.setattr(
        dashboard_module,
        "crew_http_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(fake_pod), trust_env=False),
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://crew.example.test") as client:
        host_app.dependency_overrides[crew_identity] = lambda: identity("user-a")
        owner_response = await client.get("/api/chat/slots", headers={"Cookie": f"mc_token_5476=owner-a; {signed_cookie(target_ref='pod-a.test')}"})
        assert owner_response.status_code == 200
        assert owner_response.json() == {"pod": "pod-a.test"}
        assert len(pod_requests["pod-a.test"]) == 1

        host_app.dependency_overrides[crew_identity] = lambda: identity("user-b")
        cross_response = await client.get(
            "/api/chat/slots", headers={"Cookie": f"mc_token_5476=owner-a; {signed_cookie('user-b', target_ref='pod-b.test')}"}
        )
        assert cross_response.status_code == 404
        assert cross_response.json() == {"error": "no_owner_session"}
        assert len(pod_requests["pod-a.test"]) == 1
        assert len(pod_requests["pod-b.test"]) == 1

        own_response = await client.get(
            "/api/chat/slots", headers={"Cookie": f"mc_token_5476=owner-b; {signed_cookie('user-b', target_ref='pod-b.test')}"}
        )
        assert own_response.status_code == 200
        assert own_response.json() == {"pod": "pod-b.test"}
        assert len(pod_requests["pod-a.test"]) == 1


async def test_replaced_lease_requires_fresh_signed_dashboard_ticket(dashboard, monkeypatch):
    app, host_app, _, _ = dashboard
    state = {"generation": 1}
    forwarded = []

    class ReplacementResolver:
        async def resolve(self, owner, *, now):
            generation = state["generation"]
            endpoint = f"http://pod-{generation}.test"
            return ResolvedCrewTarget(
                endpoint,
                CrewLease(
                    tenant_id=owner.tenant_id,
                    user_id=owner.user_id,
                    target_ref=f"pod-{generation}",
                    generation=generation,
                    expires_at=4_000_000_000,
                    endpoint=endpoint,
                ),
                "ready",
            )

    def fake_pod(request):
        forwarded.append(request.url.host)
        return httpx.Response(200, json={"pod": request.url.host})

    host_app.dependency_overrides[get_target_resolver] = lambda: ReplacementResolver()
    host_app.dependency_overrides[crew_identity] = lambda: identity("user-a")
    monkeypatch.setattr(
        dashboard_module,
        "crew_http_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(fake_pod), trust_env=False),
    )

    old_ticket = signed_cookie(target_ref="pod-1")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://crew.example.test") as client:
        first = await client.get("/api/chat/slots", headers={"Cookie": old_ticket})
        assert first.status_code == 200
        assert first.json() == {"pod": "pod-1.test"}

        state["generation"] = 2
        stale = await client.get("/api/chat/slots", headers={"Cookie": old_ticket})
        assert stale.status_code == 409
        assert forwarded == ["pod-1.test"]

        missing = await client.get("/api/chat/slots")
        assert missing.status_code == 409
        foreign = await client.get("/api/chat/slots", headers={"Cookie": signed_cookie("user-b", generation=2)})
        assert foreign.status_code == 404
        tampered = await client.get("/api/chat/slots", headers={"Cookie": "adp_crew=invalid.invalid"})
        assert tampered.status_code == 404
        expired = await client.get("/api/chat/slots", headers={"Cookie": signed_cookie(generation=2, now=0)})
        assert expired.status_code == 409
        assert forwarded == ["pod-1.test"]

        fresh = await client.get("/api/chat/slots", headers={"Cookie": signed_cookie(generation=2, target_ref="pod-2")})
        assert fresh.status_code == 200
        assert fresh.json() == {"pod": "pod-2.test"}
    assert forwarded == ["pod-1.test", "pod-2.test"]


async def test_fenced_target_never_forwards_http_or_websocket(dashboard, monkeypatch):
    app, host_app, _, _ = dashboard
    state = {"mode": "ready", "reads": 0}
    forwarded = []

    class CurrentLeaseResolver:
        async def resolve(self, owner, *, now):
            state["reads"] += 1
            if state["mode"] == "fenced":
                raise CrewLeaseFencedError("lease_fenced")
            endpoint = "http://pod-old.test"
            return ResolvedCrewTarget(
                endpoint,
                CrewLease(
                    tenant_id=owner.tenant_id,
                    user_id=owner.user_id,
                    target_ref="pod-old",
                    generation=1,
                    expires_at=now if state["mode"] == "expired" else now + 3600,
                    endpoint=endpoint,
                ),
                "not-ready" if state["mode"] == "not-ready" else "ready",
            )

    def fake_pod(request):
        forwarded.append(request.url.host)
        return httpx.Response(200, json={"pod": request.url.host})

    host_app.dependency_overrides[get_target_resolver] = lambda: CurrentLeaseResolver()
    host_app.dependency_overrides[crew_identity] = lambda: identity("user-a")
    monkeypatch.setattr(
        dashboard_module,
        "crew_http_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(fake_pod), trust_env=False),
    )

    with TestClient(app, base_url="https://crew.example.test") as client:
        allowed = client.get("/api/chat/slots", headers={"Cookie": signed_cookie(target_ref="pod-old")})
        assert allowed.status_code == 200
        assert forwarded == ["pod-old.test"]
        for mode in ("fenced", "not-ready", "expired"):
            state["mode"] = mode
            refused = client.get("/api/chat/slots", headers={"Cookie": signed_cookie()})
            assert refused.status_code == 409
            assert refused.headers["cache-control"] == "no-store"
            with pytest.raises(WebSocketDenialResponse) as handshake:
                with client.websocket_connect("ws://crew.example.test/api/live", headers={"Cookie": signed_cookie()}):
                    pass
            assert handshake.value.status_code == 409
            assert forwarded == ["pod-old.test"]
    assert state["reads"] == 7


async def test_new_handoff_after_pod_replacement_establishes_kiro_owner_session(dashboard, monkeypatch):
    from datetime import timedelta
    from types import SimpleNamespace

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from src.agentauth.envelope import verify_envelope
    from src.crew import handoff as handoff_module

    app, host_app, _, _ = dashboard
    generation = {"current": 1, "marker": 1}
    signed_mints = []
    link_tokens = {}
    redeemed = []
    received = []
    forwarded = []
    private_key = Ed25519PrivateKey.generate()
    monkeypatch.setenv("AGENT_CONTROL_ENVELOPE_KEY_ID", "test-crew-key")
    monkeypatch.setenv(
        "AGENT_CONTROL_ENVELOPE_SIGNING_KEY",
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode(),
    )

    def current_run():
        return f"00000000-0000-4000-8000-{generation['current']:012d}"

    class LeaseResolver:
        async def resolve(self, owner, *, now):
            current = generation["current"]
            return ResolvedCrewTarget(
                f"http://10.0.0.{current + 10}:5476",
                CrewLease(
                    tenant_id=owner.tenant_id,
                    user_id=owner.user_id,
                    target_ref=f"pod-{current}",
                    generation=current,
                    expires_at=4_000_000_000,
                    endpoint=f"http://10.0.0.{current + 10}:5476",
                ),
                "ready",
            )

    def get_owner_marker(**kwargs):
        assert kwargs["ConsistentRead"] is True
        assert kwargs["Key"] == {"event_id": "crew-owner#tenant-a#user-a", "arrived_at": "crew"}
        return {
            "Item": {
                "run_id": f"00000000-0000-4000-8000-{generation['marker']:012d}",
                "executor_scope": "user",
                "generation": generation["marker"],
                "target_kind": "eks-pod",
                "target_ref": f"pod-{generation['marker']}",
                "lease_expires_at": 4_000_000_000,
            }
        }

    def read_run(run_id):
        return {
            "run_id": run_id,
            "kind": "crew",
            "status": "active",
            "tenant_id": "tenant-a",
            "root_human_id": "user-a",
            "user_id": "user-a",
            "executor_scope": "user",
            "lease_generation": generation["current"],
            "target_kind": "eks-pod",
            "target_ref": f"pod-{generation['current']}",
            "lease_expires_at": 4_000_000_000,
        }

    def fake_custody(request):
        current = generation["current"]
        assert request.url == f"http://10.0.0.{current + 10}:5477/mint"
        assert request.headers["content-type"] == "application/json"
        body = json.loads(request.content)
        assert body["run_id"] == current_run()
        assert body["user_id"] == "user-a"
        assert body["lease_generation"] == current
        proof = verify_envelope(
            request.headers["x-adp-crew-signature"],
            public_keys={"test-crew-key": private_key.public_key()},
            expected_run_id=current_run(),
            expected_generation=current,
            expected_action="dashboard.mint",
            expected_command_id=body["request_id"],
            request_body=request.content,
            expected_audience="adp-crew-dashboard-mint",
        )
        assert proof.principal == "user-a" and proof.tenant_id == "tenant-a"
        assert proof.authority_kind == "human_session"
        signed_mints.append(body["request_id"])
        token = f"link-token-for-generation-{current}"
        link_tokens[token] = current
        return httpx.Response(
            200, json={"link_token": token, "lease_generation": current, "expires_at": (datetime.now(UTC) + timedelta(seconds=250)).isoformat()}
        )

    def fake_dashboard(request):
        received.append((request.url.host, request.url.path))
        assert request.headers["host"] == "crew.example.test"
        assert "authorization" not in request.headers
        assert "adp_crew" not in request.headers.get("cookie", "")
        current = int(request.url.host.rsplit(".", 1)[-1]) - 10
        if request.url.path == "/" and "token" in request.url.params:
            token = request.url.params["token"]
            if link_tokens.pop(token, None) != current:
                return httpx.Response(401)
            redeemed.append((current, token))
            return httpx.Response(
                302,
                headers={
                    "Location": "/",
                    "Set-Cookie": f"mc_token_5476=owner-user-a-pod-{current}; HttpOnly; SameSite=Lax",
                },
            )
        cookie = request.headers.get("cookie", "")
        if f"mc_token_5476=owner-user-a-pod-{current}" not in cookie.split("; "):
            return httpx.Response(401)
        if request.url.path == "/":
            return httpx.Response(200, text=f"Crew dashboard for user-a on pod {current}")
        if request.url.path == "/api/chat/slots":
            forwarded.append(request.url.host)
            return httpx.Response(200, json={"pod": request.url.host, "owner": "user-a"})
        return httpx.Response(404)

    async def authenticated_user(request, authorization):
        if authorization != "Bearer valid":
            raise HTTPException(401, detail={"error": "invalid_adp_session"})
        return identity("user-a")

    from fastapi import HTTPException

    monkeypatch.setattr(
        handoff_module,
        "current_crew_store",
        lambda: SimpleNamespace(
            table=SimpleNamespace(get_item=get_owner_marker),
            read_run=read_run,
        ),
    )
    monkeypatch.setattr(
        handoff_module,
        "custody_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(fake_custody), trust_env=False),
    )
    monkeypatch.setattr(
        dashboard_module,
        "crew_http_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(fake_dashboard), trust_env=False),
    )
    host_app.dependency_overrides[get_target_resolver] = lambda: LeaseResolver()
    monkeypatch.setattr(dashboard_module, "get_current_user", authenticated_user)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://crew.example.test") as client:
        first = await client.get("/crew/handoff", headers={"Authorization": "Bearer valid"}, follow_redirects=False)
        assert first.status_code == 302
        assert first.headers["location"] == "https://crew.example.test/?token=link-token-for-generation-1"
        assert "secure" in first.headers["set-cookie"].lower()
        assert "httponly" in first.headers["set-cookie"].lower()
        assert "samesite=lax" in first.headers["set-cookie"].lower()
        assert "domain=" not in first.headers["set-cookie"].lower()
        old_ticket = client.cookies["adp_crew"]
        without_kiro_session = await client.get("/api/chat/slots")
        assert without_kiro_session.status_code == 401
        assert not forwarded
        dashboard_page = await client.get(first.headers["location"], follow_redirects=True)
        assert dashboard_page.status_code == 200
        assert dashboard_page.text == "Crew dashboard for user-a on pod 1"
        assert len(dashboard_page.history) == 1
        assert "Secure" in dashboard_page.history[0].headers["set-cookie"]
        assert client.cookies["mc_token_5476"] == "owner-user-a-pod-1"
        replay = await client.get(first.headers["location"], follow_redirects=False)
        assert replay.status_code == 401
        assert "set-cookie" not in replay.headers
        assert redeemed == [(1, "link-token-for-generation-1")]
        old_pod = await client.get("/api/chat/slots")
        assert old_pod.status_code == 200
        assert old_pod.json() == {"pod": "10.0.0.11", "owner": "user-a"}

        generation["current"] = 2
        upstream_before_stale = len(received)
        stale = await client.get("/api/chat/slots")
        assert stale.status_code == 409
        assert len(received) == upstream_before_stale
        assert forwarded == ["10.0.0.11"]
        stale_marker = await client.get("/crew/handoff", headers={"Authorization": "Bearer valid"}, follow_redirects=False)
        assert stale_marker.status_code == 409
        assert len(signed_mints) == 1
        generation["marker"] = 2
        fresh = await client.get("/crew/handoff", headers={"Authorization": "Bearer valid", "Accept": "application/json"}, follow_redirects=False)
        assert fresh.status_code == 200
        assert fresh.json() == {"redirect_url": "https://crew.example.test/?token=link-token-for-generation-2"}
        assert client.cookies["adp_crew"] != old_ticket
        old_kiro_session = await client.get("/api/chat/slots")
        assert old_kiro_session.status_code == 401
        assert forwarded == ["10.0.0.11"]
        new_dashboard = await client.get(fresh.json()["redirect_url"], follow_redirects=True)
        assert new_dashboard.status_code == 200
        assert new_dashboard.text == "Crew dashboard for user-a on pod 2"
        assert client.cookies["mc_token_5476"] == "owner-user-a-pod-2"
        new_pod = await client.get("/api/chat/slots")
        assert new_pod.status_code == 200
        assert new_pod.json() == {"pod": "10.0.0.12", "owner": "user-a"}

    assert redeemed == [(1, "link-token-for-generation-1"), (2, "link-token-for-generation-2")]
    assert forwarded == ["10.0.0.11", "10.0.0.12"]
    assert len(signed_mints) == 2 and signed_mints[0] != signed_mints[1]
    audit_db = host_app.dependency_overrides[get_db]()
    assert len(audit_db.rows) == 2
    for generation, row in enumerate(audit_db.rows, start=1):
        assert isinstance(row, AuditLog)
        assert row.org_id == "tenant-a"
        assert row.actor_id == "user-a"
        assert row.event_type == "crew_dashboard_handoff"
        assert row.details == {"target_ref": f"pod-{generation}", "lease_generation": generation}
        assert "link-token" not in json.dumps(row.details)
    # Both handoff audits committed (nothing left pending, so no audit rollback). The
    # proxied dashboard requests above each rolled back their read-only owner lookup
    # by design, to free the pooled connection before streaming.
    assert audit_db.pending == []


async def test_privileged_routes_are_denied_but_dashboard_gets_reach_fake_pod(dashboard, monkeypatch):
    app, host_app, _, _ = dashboard
    received = []

    def fake_pod(request):
        received.append(request.url.path)
        return httpx.Response(200, json={"path": request.url.path})

    monkeypatch.setattr(
        dashboard_module,
        "crew_http_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(fake_pod), trust_env=False),
    )
    host_app.dependency_overrides[crew_identity] = lambda: identity("user-a")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://crew.example.test") as client:
        for path in ("/", "/_next/static/app.js", "/api/stream", "/api/chat/slots", "/api/tokenized"):
            allowed = await client.get(path, headers={"Cookie": signed_cookie()})
            assert allowed.status_code == 200
            assert allowed.json() == {"path": path}
        assert received == ["/", "/_next/static/app.js", "/api/stream", "/api/chat/slots", "/api/tokenized"]
        for path in ("/api/token/local", "/api/token", "/api/instances/x", "/api/instances", "/api/auth/mobile-link", "/crew/target"):
            for method in ("GET", "POST"):
                denied = await client.request(method, path, headers={"Cookie": signed_cookie()})
                assert denied.status_code == 403
                assert denied.headers["cache-control"] == "no-store"
        assert received == ["/", "/_next/static/app.js", "/api/stream", "/api/chat/slots", "/api/tokenized"]


@pytest.mark.parametrize(
    "path",
    (
        "/API/%74oken/local",
        "/api%2Ftoken%2Flocal",
        "/api/%2574oken/local",
        "/api//instances/x",
        "/public/../api/instances/x",
        "/api/auth/mobile-link%2Fextra",
        "/api/token;ignored/local",
        "/api/%5cinstances/x",
        "/api/token/local%23/%2e%2e/%2e%2e/safe",
        "/api/instances/x%23/%2e%2e/%2e%2e/safe",
        "/api/auth/mobile-link%23/%2e%2e/%2e%2e/safe",
        "/api/token/local%3f/%2e%2e/%2e%2e/safe",
        "/api/instances/x%3f/%2e%2e/%2e%2e/safe",
        "/api/auth/mobile-link%3f/%2e%2e/%2e%2e/safe",
        "/api/token/local%2523/%2e%2e/%2e%2e/safe",
    ),
)
async def test_encoded_privileged_route_never_reaches_fake_pod(dashboard, monkeypatch, path):
    app, host_app, resolved, _ = dashboard
    forwarded = []
    monkeypatch.setattr(
        dashboard_module,
        "crew_http_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(lambda request: forwarded.append(request) or httpx.Response(200)), trust_env=False),
    )
    host_app.dependency_overrides[crew_identity] = lambda: identity("user-a")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://crew.example.test") as client:
        response = await client.get(path, headers={"Cookie": signed_cookie()})
        assert response.status_code == 403
    assert resolved == [] and forwarded == []


@pytest.mark.parametrize(
    "path",
    [
        "/api/token/local",
        "/api/token/local%23/%2e%2e/%2e%2e/safe",
        "/api/instances/x%3f/%2e%2e/%2e%2e/safe",
        "/api/auth/mobile-link%23/%2e%2e/%2e%2e/safe",
    ],
)
def test_privileged_websocket_upgrade_is_denied_without_target_lookup(dashboard, monkeypatch, path):
    app, host_app, resolved, _ = dashboard
    host_app.dependency_overrides[crew_identity] = lambda: identity("user-a")
    monkeypatch.setattr(websocket_module, "crew_websocket_connect", lambda *args, **kwargs: pytest.fail("pod contacted"))
    with TestClient(app, base_url="https://crew.example.test") as client:
        with pytest.raises(WebSocketDenialResponse) as refused:
            with client.websocket_connect(f"ws://crew.example.test{path}", headers={"Cookie": signed_cookie()}):
                pass
    assert refused.value.status_code == 403
    assert resolved == []


@pytest.mark.parametrize("path", ["/api/chat/slots/a%2Fb", "/api/chat/slots/a%2Fb?slot=a%2Fb&slot=a+b&literal=%2523"])
async def test_http_relay_preserves_escaped_path_and_query_bytes(dashboard, monkeypatch, path):
    app, _, _, _ = dashboard
    received = []
    monkeypatch.setattr(
        dashboard_module,
        "crew_http_client",
        lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: received.append(request.url) or httpx.Response(200)), trust_env=False
        ),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://crew.example.test") as client:
        response = await client.get(path, headers={"Cookie": signed_cookie()})
    assert response.status_code == 200
    assert len(received) == 1
    assert received[0].host == "pod-a.test"
    assert received[0].raw_path == path.encode()


@pytest.mark.parametrize("origin", ["https://sibling.example.test", "https://untrusted.example.test", "null", "http://crew.example.test"])
def test_websocket_rejects_untrusted_origin_before_connecting(dashboard, monkeypatch, origin):
    app, _, _, _ = dashboard
    monkeypatch.setattr(
        websocket_module,
        "crew_connection_registry",
        lambda: SimpleNamespace(
            acquire=AsyncMock(return_value=FakeCrewLease()),
        ),
    )
    monkeypatch.setattr(websocket_module, "crew_websocket_connect", lambda *args, **kwargs: pytest.fail("pod contacted"))
    with TestClient(app, base_url="https://crew.example.test") as client:
        with pytest.raises(WebSocketDenialResponse) as refused:
            with client.websocket_connect(
                "ws://crew.example.test/api/live",
                headers={
                    "Cookie": f"{signed_cookie()}; mc_token_5476=owner-a",
                    "Origin": origin,
                },
            ):
                pass
    assert refused.value.status_code == 403


@pytest.mark.timeout(5)
async def test_http_connection_limit_renews_while_waiting_for_headers(dashboard, monkeypatch):
    app, _, _, _ = dashboard
    server = fakeredis.FakeServer()
    started = asyncio.Event()
    send_headers = asyncio.Event()
    received = []
    monkeypatch.setattr(connections_module, "LEASE_SECONDS", 1)
    monkeypatch.setattr(dashboard_module, "RENEW_SECONDS", 0.1)
    monkeypatch.setattr(
        dashboard_module,
        "crew_connection_registry",
        lambda: CrewConnectionRegistry(
            fakeredis.aioredis.FakeRedis(server=server),
            "delayed-headers",
            limit=1,
        ),
    )

    async def fake_pod(request):
        received.append(request)
        started.set()
        if len(received) == 1:
            await send_headers.wait()
        return httpx.Response(200, content=b"ready")

    monkeypatch.setattr(
        dashboard_module,
        "crew_http_client",
        lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(fake_pod),
            trust_env=False,
        ),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://crew.example.test") as client:
        first = asyncio.create_task(client.get("/api/stream", headers={"Cookie": signed_cookie()}))
        try:
            await asyncio.wait_for(started.wait(), 1)
            await asyncio.sleep(1.2)
            assert not first.done()
            second = await client.get("/api/stream", headers={"Cookie": signed_cookie()})
            assert second.status_code == 429
            assert len(received) == 1
        finally:
            send_headers.set()
            await asyncio.wait_for(first, 1)
        assert first.result().status_code == 200
        after_release = await client.get("/api/stream", headers={"Cookie": signed_cookie()})
        assert after_release.status_code == 200
    assert len(received) == 2


@pytest.mark.parametrize("stop", ["renewal_failure", "cancellation"])
@pytest.mark.timeout(5)
async def test_http_header_wait_cleans_up_when_admission_stops(dashboard, monkeypatch, stop):
    app, _, _, _ = dashboard
    started = asyncio.Event()
    cancelled = asyncio.Event()
    clients = []
    lease = FakeCrewLease()
    lease.renew = AsyncMock(return_value=False)
    registry = SimpleNamespace(acquire=AsyncMock(return_value=lease))
    monkeypatch.setattr(dashboard_module, "crew_connection_registry", lambda: registry)
    monkeypatch.setattr(dashboard_module, "RENEW_SECONDS", 0.01)

    async def fake_pod(request):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    def upstream_client():
        client = httpx.AsyncClient(transport=httpx.MockTransport(fake_pod), trust_env=False)
        clients.append(client)
        return client

    monkeypatch.setattr(dashboard_module, "crew_http_client", upstream_client)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://crew.example.test") as client:
        task = asyncio.create_task(client.get("/api/stream", headers={"Cookie": signed_cookie()}))
        try:
            await asyncio.wait_for(started.wait(), 1)
            if stop == "cancellation":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                response = await asyncio.wait_for(task, 1)
                assert response.status_code == 503
                assert response.json() == {"error": "crew_admission_unavailable"}
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert cancelled.is_set()
    assert clients[0].is_closed and lease.released
    assert ("release", "user-a") not in app.state.ratelimit_service.calls


async def test_cookie_identity_attributes_structured_and_database_logs(dashboard, monkeypatch):
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
    monkeypatch.setattr(
        dashboard_module,
        "crew_http_client",
        lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"private-response")),
            trust_env=False,
        ),
    )
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    handler.setFormatter(StructuredJsonFormatter())
    logger = logging.getLogger("src.shared.middleware.logging_middleware")
    monkeypatch.setattr(logger, "level", logging.INFO)
    logger.addHandler(handler)
    ticket = signed_cookie()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="https://crew.example.test") as client:
            response = await client.post("/api/chat/slots", headers={"Cookie": ticket}, content=b"private-body")
        assert response.status_code == 200
    finally:
        logger.removeHandler(handler)
    recorded.assert_awaited_once()
    row = recorded.await_args.kwargs
    assert row["user_id"] == "user-a" and row["org_id"] == "tenant-a"
    assert row["request_body_size"] is None and row["response_body_size"] is None
    completed = [json.loads(line) for line in output.getvalue().splitlines() if json.loads(line)["message"] == "Request completed"]
    assert len(completed) == 1
    assert completed[0]["user_id"] == "user-a" and completed[0]["org_id"] == "tenant-a"
    for sensitive in (ticket, "private-body", "private-response"):
        assert sensitive not in output.getvalue() and sensitive not in json.dumps(row)


async def test_http_relay_forwards_cookies_headers_and_safe_redirects(dashboard, monkeypatch):
    app, host_app, _, _ = dashboard
    host_app.dependency_overrides[crew_identity] = lambda: identity("user-a")
    observed = []

    def fake_pod(request):
        observed.append(request)
        if request.url.path == "/redirect":
            return httpx.Response(302, headers={"Location": "http://pod-a.test/?token=one"})
        if request.url.path == "/dashboard-redirect":
            return httpx.Response(302, headers={"Location": "https://crew.example.test/home"})
        if request.url.path == "/foreign-redirect":
            return httpx.Response(302, headers={"Location": "http://other-pod.test/private"})
        return httpx.Response(
            200,
            headers=[
                ("Set-Cookie", "mc_token_5476=one; HttpOnly; SameSite=Lax"),
                ("Set-Cookie", "mc_refresh_5476=two; Secure; HttpOnly"),
                ("Set-Cookie", "adp_crew=malicious; HttpOnly"),
                ("Connection", "x-hop"),
                ("X-Hop", "private"),
            ],
            content=b"ok",
        )

    monkeypatch.setattr(
        dashboard_module,
        "crew_http_client",
        lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(fake_pod),
            trust_env=False,
        ),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://crew.example.test") as client:
        response = await client.get(
            "/api/chat/slots",
            headers={
                "Cookie": f"mc_token_5476=one; {signed_cookie()}",
                "Authorization": "Bearer secret",
                "Connection": "x-hop",
                "X-Hop": "private",
                "X-Forwarded-For": "untrusted",
            },
        )
        assert response.status_code == 200 and response.content == b"ok"
        assert response.headers.get_list("set-cookie") == [
            "mc_token_5476=one; HttpOnly; SameSite=Lax; Secure",
            "mc_refresh_5476=two; Secure; HttpOnly",
        ]
        assert "x-hop" not in response.headers
        redirect = await client.get("/redirect", headers={"Cookie": signed_cookie()}, follow_redirects=False)
        assert redirect.status_code == 302
        assert redirect.headers["location"] == "https://crew.example.test/?token=one"
        same_host = await client.get("/dashboard-redirect", headers={"Cookie": signed_cookie()}, follow_redirects=False)
        assert same_host.status_code == 302
        assert same_host.headers["location"] == "https://crew.example.test/home"
        denied = await client.get("/foreign-redirect", headers={"Cookie": signed_cookie()})
        assert denied.status_code == 502
        assert "other-pod.test" not in denied.text
    assert observed[0].headers["cookie"] == "mc_token_5476=one"
    assert "authorization" not in observed[0].headers
    assert "x-hop" not in observed[0].headers
    assert observed[0].headers["x-forwarded-for"] != "untrusted"
    assert observed[0].headers["x-forwarded-proto"] == "https"


async def test_sse_first_frame_is_sent_before_upstream_finishes(dashboard, monkeypatch):
    app, host_app, _, _ = dashboard
    host_app.dependency_overrides[crew_identity] = lambda: identity("user-a")
    upstream_finishes = asyncio.Event()
    first_frame = asyncio.Event()
    received = []
    emitted_at = {}
    delivered_at = {}
    lease = FakeCrewLease()

    class Registry:
        async def acquire(self, tenant_id, user_id):
            return lease

    monkeypatch.setattr(dashboard_module, "crew_connection_registry", Registry)
    monkeypatch.setattr(dashboard_module, "RENEW_SECONDS", 0.01)

    class LiveStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            emitted_at["first"] = time.monotonic()
            yield b"data: first\n\n"
            await upstream_finishes.wait()
            emitted_at["second"] = time.monotonic()
            yield b"data: second\n\n"

    def fake_pod(request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=LiveStream())

    monkeypatch.setattr(
        dashboard_module,
        "crew_http_client",
        lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(fake_pod),
            trust_env=False,
        ),
    )
    request_started = False
    disconnected = asyncio.Event()

    async def receive():
        nonlocal request_started
        if not request_started:
            request_started = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.body" and message.get("body"):
            received.append(message["body"])
            delivered_at["first" if len(received) == 1 else "second"] = time.monotonic()
            first_frame.set()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "https",
        "root_path": "",
        "path": "/api/stream",
        "raw_path": b"/api/stream",
        "query_string": b"",
        "headers": [(b"host", b"crew.example.test"), (b"cookie", signed_cookie().encode())],
        "client": ("127.0.0.1", 45000),
        "server": ("crew.example.test", 443),
    }
    task = asyncio.create_task(app(scope, receive, send))
    try:
        await asyncio.wait_for(first_frame.wait(), 1)
        assert received == [b"data: first\n\n"]
        assert delivered_at["first"] - emitted_at["first"] < 1
        await asyncio.sleep(0.04)
        assert lease.renewals > 0 and not lease.released
        upstream_finishes.set()
        await asyncio.wait_for(task, 1)
        assert received == [b"data: first\n\n", b"data: second\n\n"]
        assert delivered_at["second"] - emitted_at["second"] < 1
        assert lease.released
    finally:
        upstream_finishes.set()
        disconnected.set()
        if not task.done():
            await asyncio.wait_for(task, 1)


class FakeCrewWebSocket:
    subprotocol = "crew.v1"
    close_code = 1000

    async def __aenter__(self):
        self.messages = asyncio.Queue()
        self.closed = False
        return self

    async def __aexit__(self, *_):
        self.closed = True

    async def send(self, message):
        await self.messages.put(None if message == "close" else message)

    def __aiter__(self):
        return self

    async def __anext__(self):
        message = await self.messages.get()
        if message is None:
            raise StopAsyncIteration
        return message


class FakeCrewLease:
    def __init__(self):
        self.released = False
        self.renewals = 0

    def remaining(self):
        return 45

    async def renew(self):
        self.renewals += 1
        return True

    async def release(self):
        self.released = True


@pytest.mark.parametrize("origin", ["https://crew.example.test", None])
def test_websocket_relays_text_binary_and_owner_cookies(dashboard, monkeypatch, origin):
    app, host_app, _, _ = dashboard
    host_app.dependency_overrides[crew_identity] = lambda: identity("user-a")
    lease = FakeCrewLease()
    admitted = []
    connections = []

    class Registry:
        async def acquire(self, tenant_id, user_id):
            admitted.append((tenant_id, user_id))
            return lease

    def fake_connect(uri, **kwargs):
        connections.append((uri, kwargs))
        return FakeCrewWebSocket()

    monkeypatch.setattr(websocket_module, "crew_connection_registry", Registry)
    monkeypatch.setattr(websocket_module, "crew_websocket_connect", fake_connect)
    monkeypatch.setattr(websocket_module, "RENEW_SECONDS", 0.01)
    with TestClient(app, base_url="https://crew.example.test") as client:
        with client.websocket_connect(
            "ws://crew.example.test/api/live?slot=1&slot=2",
            headers={
                "Cookie": f"mc_token_5476=owner-a; {signed_cookie()}",
                **({"Origin": origin} if origin is not None else {}),
                "Authorization": "Bearer adp-secret",
                "X-Forwarded-For": "untrusted",
            },
            subprotocols=["crew.v1"],
        ) as browser:
            assert browser.accepted_subprotocol == "crew.v1"
            browser.send_text("hello")
            assert browser.receive_text() == "hello"
            browser.send_bytes(b"frame")
            assert browser.receive_bytes() == b"frame"
            time.sleep(0.04)
            assert lease.renewals > 0
            browser.send_text("close")
            with pytest.raises(WebSocketDisconnect) as ended:
                browser.receive_text()
            assert ended.value.code == 1000
    assert lease.released
    assert admitted == [("tenant-a", "user-a")]
    uri, kwargs = connections[0]
    assert uri == "ws://crew.example.test/api/live?slot=1&slot=2"
    assert kwargs["host"] == "pod-a.test" and kwargs["port"] == 80
    assert kwargs["origin"] == origin
    assert kwargs["proxy"] is None and kwargs["subprotocols"] == ["crew.v1"]
    assert kwargs["additional_headers"] == {
        "Cookie": "mc_token_5476=owner-a",
        "X-Forwarded-Host": "crew.example.test",
        "X-Forwarded-Proto": "https",
        "X-Forwarded-For": "testclient",
    }


def test_websocket_limit_denies_before_upstream_connect(dashboard, monkeypatch):
    app, host_app, _, _ = dashboard
    host_app.dependency_overrides[crew_identity] = lambda: identity("user-a")
    calls = []

    class FullRegistry:
        async def acquire(self, tenant_id, user_id):
            calls.append((tenant_id, user_id))
            raise CrewConnectionLimitError

    monkeypatch.setattr(websocket_module, "crew_connection_registry", FullRegistry)
    monkeypatch.setattr(websocket_module, "crew_websocket_connect", lambda *args, **kwargs: pytest.fail("pod contacted"))
    with TestClient(app, base_url="https://crew.example.test") as client:
        with pytest.raises(WebSocketDenialResponse) as full:
            with client.websocket_connect("ws://crew.example.test/api/live", headers={"Cookie": signed_cookie()}):
                pass
        assert full.value.status_code == 429
    assert calls == [("tenant-a", "user-a")]


@pytest.mark.timeout(5)
def test_websocket_expired_admission_closes_and_releases(dashboard, monkeypatch):
    app, host_app, _, _ = dashboard
    host_app.dependency_overrides[crew_identity] = lambda: identity("user-a")

    class ExpiringLease(FakeCrewLease):
        async def renew(self):
            self.renewals += 1
            return False

    lease = ExpiringLease()

    class Registry:
        async def acquire(self, tenant_id, user_id):
            return lease

    monkeypatch.setattr(websocket_module, "crew_connection_registry", Registry)
    monkeypatch.setattr(websocket_module, "crew_websocket_connect", lambda *args, **kwargs: FakeCrewWebSocket())
    monkeypatch.setattr(websocket_module, "RENEW_SECONDS", 0.01)
    with TestClient(app, base_url="https://crew.example.test") as client:
        with client.websocket_connect("ws://crew.example.test/api/live", headers={"Cookie": signed_cookie()}) as browser:
            with pytest.raises(WebSocketDisconnect) as closed:
                browser.receive_text()
            assert closed.value.code == 1013
    assert lease.renewals == 1 and lease.released


def test_websocket_upstream_handshake_failure_does_not_upgrade_browser(dashboard, monkeypatch):
    app, host_app, _, _ = dashboard
    host_app.dependency_overrides[crew_identity] = lambda: identity("user-a")
    lease = FakeCrewLease()

    class Registry:
        async def acquire(self, tenant_id, user_id):
            return lease

    class RejectedConnection:
        async def __aenter__(self):
            raise OSError("pod unavailable")

        async def __aexit__(self, *_):
            pass

    monkeypatch.setattr(websocket_module, "crew_connection_registry", Registry)
    monkeypatch.setattr(websocket_module, "crew_websocket_connect", lambda *args, **kwargs: RejectedConnection())
    with TestClient(app, base_url="https://crew.example.test") as client:
        with pytest.raises(WebSocketDenialResponse) as rejected:
            with client.websocket_connect("ws://crew.example.test/api/live", headers={"Cookie": signed_cookie()}):
                pass
        assert rejected.value.status_code == 502
        assert "pod unavailable" not in rejected.value.text
    assert lease.released


@pytest.mark.timeout(5)
def test_http_and_websocket_share_the_owner_connection_cap(dashboard, monkeypatch):
    app, host_app, _, _ = dashboard
    host_app.dependency_overrides[crew_identity] = lambda: identity("user-a")
    server = fakeredis.FakeServer()
    forwarded = []

    def registry():
        return CrewConnectionRegistry(fakeredis.aioredis.FakeRedis(server=server), "crew-test", limit=1)

    def fake_pod(request):
        forwarded.append(request.url.path)
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(dashboard_module, "crew_connection_registry", registry)
    monkeypatch.setattr(websocket_module, "crew_connection_registry", registry)
    monkeypatch.setattr(websocket_module, "crew_websocket_connect", lambda *args, **kwargs: FakeCrewWebSocket())
    monkeypatch.setattr(
        dashboard_module,
        "crew_http_client",
        lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(fake_pod),
            trust_env=False,
        ),
    )
    with TestClient(app, base_url="https://crew.example.test") as client:
        with client.websocket_connect("ws://crew.example.test/api/live", headers={"Cookie": signed_cookie()}) as browser:
            blocked = client.get("/api/chat/slots", headers={"Cookie": signed_cookie()})
            assert blocked.status_code == 429
            assert blocked.json() == {"error": "crew_connection_limit"}
            assert forwarded == []
            browser.send_text("close")
            with pytest.raises(WebSocketDisconnect):
                browser.receive_text()
        admitted = client.get("/api/chat/slots", headers={"Cookie": signed_cookie()})
        assert admitted.status_code == 200
        assert forwarded == ["/api/chat/slots"]
    assert ("release", "user-a") not in app.state.ratelimit_service.calls


@pytest.mark.timeout(5)
def test_crew_proxy_applies_no_request_rate_admission(dashboard, monkeypatch):
    """The dashboard proxy never consults the per-user request-rate service (#8223 cause 24).

    Model traffic from the box is limited on the model route; debiting the dashboard's own
    requests from that bucket refused 188 of a first load's ~213 chunks. Even a denying
    rate service must not stop HTTP or WebSocket forwarding; the connection lease is the
    only proxy-level bound.
    """
    app, host_app, _, _ = dashboard
    host_app.dependency_overrides[crew_identity] = lambda: identity("user-a")
    app.state.ratelimit_service.allowed = False
    app.state.ratelimit_service.calls.clear()
    forwarded = []

    def fake_pod(request):
        forwarded.append(request.url.path)
        return httpx.Response(200, json={"ok": True})

    class Registry:
        async def acquire(self, tenant_id, user_id):
            return FakeCrewLease()

    monkeypatch.setattr(websocket_module, "crew_connection_registry", Registry)
    monkeypatch.setattr(dashboard_module, "crew_http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(fake_pod), trust_env=False))
    monkeypatch.setattr(websocket_module, "crew_websocket_connect", lambda *args, **kwargs: FakeCrewWebSocket())
    with TestClient(app, base_url="https://crew.example.test") as client:
        response = client.get("/api/chat/slots", headers={"Cookie": signed_cookie()})
        assert response.status_code == 200
        with client.websocket_connect("ws://crew.example.test/api/live", headers={"Cookie": signed_cookie()}) as browser:
            browser.send_text("close")
            with pytest.raises(WebSocketDisconnect):
                browser.receive_text()
    assert forwarded == ["/api/chat/slots"]
    assert app.state.ratelimit_service.calls == []


@pytest.mark.timeout(5)
def test_crew_proxy_forwards_when_the_rate_backend_is_unavailable(dashboard, monkeypatch):
    """A rate-limit backend outage cannot take the dashboard down: the proxy never calls it."""
    app, host_app, _, _ = dashboard
    host_app.dependency_overrides[crew_identity] = lambda: identity("user-a")

    async def unavailable(*args, **kwargs):
        raise RuntimeError("private backend failure")

    for name in ("check_rate_limit", "consume_rate_limit", "release_concurrent"):
        setattr(app.state.ratelimit_service, name, unavailable)
    monkeypatch.setattr(
        dashboard_module,
        "crew_http_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True})), trust_env=False),
    )
    with TestClient(app, base_url="https://crew.example.test") as client:
        response = client.get("/api/chat/slots", headers={"Cookie": signed_cookie()})
    assert response.status_code == 200


def test_gateway_app_mount_shares_rate_limit_service(monkeypatch):
    monkeypatch.setenv("CREW_DASHBOARD_ORIGIN", "https://crew.example.test")
    from src.app import create_app

    app = create_app()
    assert app.router.routes[0].app.state.ratelimit_service is app.state.ratelimit_service


@pytest.mark.timeout(270)
def test_websocket_frames_survive_240_seconds_through_fake_pod(dashboard, monkeypatch):
    app, host_app, _, _ = dashboard
    host_app.dependency_overrides[crew_identity] = lambda: identity("user-a")
    lease = FakeCrewLease()

    class Registry:
        async def acquire(self, tenant_id, user_id):
            assert (tenant_id, user_id) == ("tenant-a", "user-a")
            return lease

    monkeypatch.setattr(websocket_module, "crew_connection_registry", Registry)
    monkeypatch.setattr(websocket_module, "crew_websocket_connect", lambda *args, **kwargs: FakeCrewWebSocket())
    with TestClient(app, base_url="https://crew.example.test") as client:
        with client.websocket_connect("ws://crew.example.test/api/live", headers={"Cookie": signed_cookie()}) as browser:
            started = time.monotonic()
            browser.send_text("first")
            assert browser.receive_text() == "first"
            assert time.monotonic() - started < 1

            time.sleep(240)
            assert time.monotonic() - started >= 240
            assert lease.renewals >= 20 and not lease.released
            emitted_at = time.monotonic()
            browser.send_bytes(b"still-open")
            assert browser.receive_bytes() == b"still-open"
            assert time.monotonic() - emitted_at < 1
            browser.send_text("close")
            with pytest.raises(WebSocketDisconnect) as ended:
                browser.receive_text()
            assert ended.value.code == 1000
    assert lease.released


@pytest.mark.timeout(5)
async def test_sse_stays_open_between_frames_and_releases_on_disconnect(dashboard, monkeypatch):
    app, host_app, _, _ = dashboard
    host_app.dependency_overrides[crew_identity] = lambda: identity("user-a")
    lease = FakeCrewLease()
    first_frame = asyncio.Event()
    second_frame = asyncio.Event()
    release_second = asyncio.Event()
    keep_upstream_open = asyncio.Event()
    disconnected = asyncio.Event()
    forwarded = []

    class Registry:
        async def acquire(self, tenant_id, user_id):
            return lease

    class HeldStream(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            yield b"data: first\n\n"
            await release_second.wait()
            yield b"data: second\n\n"
            await keep_upstream_open.wait()

        async def aclose(self):
            self.closed = True

    upstream = HeldStream()
    monkeypatch.setattr(dashboard_module, "crew_connection_registry", Registry)
    monkeypatch.setattr(dashboard_module, "RENEW_SECONDS", 0.01)
    monkeypatch.setattr(
        dashboard_module,
        "crew_http_client",
        lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    headers={"content-type": "text/event-stream"},
                    stream=upstream,
                )
            ),
            trust_env=False,
        ),
    )
    request_started = False

    async def receive():
        nonlocal request_started
        if not request_started:
            request_started = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.body" and message.get("body"):
            forwarded.append(message["body"])
            (first_frame if len(forwarded) == 1 else second_frame).set()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "https",
        "root_path": "",
        "path": "/api/stream",
        "raw_path": b"/api/stream",
        "query_string": b"",
        "headers": [(b"host", b"crew.example.test"), (b"cookie", signed_cookie().encode())],
        "client": ("127.0.0.1", 45000),
        "server": ("crew.example.test", 443),
    }
    task = asyncio.create_task(app(scope, receive, send))
    try:
        await asyncio.wait_for(first_frame.wait(), 1)
        assert forwarded == [b"data: first\n\n"]
        await asyncio.sleep(0.06)
        assert not task.done() and not upstream.closed and not lease.released
        assert not second_frame.is_set() and lease.renewals >= 2
        release_second.set()
        await asyncio.wait_for(second_frame.wait(), 1)
        assert forwarded == [b"data: first\n\n", b"data: second\n\n"]
        assert not task.done() and not upstream.closed and not lease.released
        disconnected.set()
        await asyncio.wait_for(task, 1)
        assert upstream.closed and lease.released
        assert ("release", "user-a") not in app.state.ratelimit_service.calls
    finally:
        release_second.set()
        keep_upstream_open.set()
        disconnected.set()
        if not task.done():
            await asyncio.wait_for(task, 1)


@pytest.mark.timeout(5)
def test_websocket_pushes_pod_frames_without_browser_polling(dashboard, monkeypatch):
    app, host_app, _, _ = dashboard
    host_app.dependency_overrides[crew_identity] = lambda: identity("user-a")
    lease = FakeCrewLease()
    emitted_at = []

    class Registry:
        async def acquire(self, tenant_id, user_id):
            return lease

    class PushPod(FakeCrewWebSocket):
        async def __aenter__(self):
            await super().__aenter__()

            async def emit():
                await asyncio.sleep(0.04)
                emitted_at.append(time.monotonic())
                await self.messages.put("server text")
                await asyncio.sleep(0.04)
                emitted_at.append(time.monotonic())
                await self.messages.put(b"server bytes")

            self.producer = asyncio.create_task(emit())
            return self

        async def __aexit__(self, *args):
            await self.producer
            await super().__aexit__(*args)

    monkeypatch.setattr(websocket_module, "crew_connection_registry", Registry)
    monkeypatch.setattr(websocket_module, "crew_websocket_connect", lambda *args, **kwargs: PushPod())
    monkeypatch.setattr(websocket_module, "RENEW_SECONDS", 0.01)
    with TestClient(app, base_url="https://crew.example.test") as client:
        with client.websocket_connect("ws://crew.example.test/api/live", headers={"Cookie": signed_cookie()}) as browser:
            assert browser.receive_text() == "server text"
            assert time.monotonic() - emitted_at[0] < 1
            assert browser.receive_bytes() == b"server bytes"
            assert time.monotonic() - emitted_at[1] < 1
            assert lease.renewals >= 2 and not lease.released
            browser.send_text("close")
            with pytest.raises(WebSocketDisconnect) as ended:
                browser.receive_text()
            assert ended.value.code == 1000
    assert lease.released


@pytest.mark.timeout(5)
def test_browser_handoff_requires_bearer_and_issues_owner_bound_dashboard_cookie(dashboard, monkeypatch):
    from fastapi import HTTPException

    app, host_app, _, _ = dashboard
    auth_checks = []
    forwarded = []
    handoffs = []

    async def verified_user(request, authorization):
        auth_checks.append(authorization)
        if authorization != "Bearer valid":
            raise HTTPException(401, detail={"error": "invalid_adp_session"})
        return identity("user-a")

    async def mint_link(owner, target, *, now):
        handoffs.append((owner.user_id, target.lease.generation))
        return "single-use-link"

    def fake_pod(request):
        forwarded.append(request.headers.get("cookie"))
        return httpx.Response(200, json={"owner": request.url.host})

    class Registry:
        async def acquire(self, tenant_id, user_id):
            assert (tenant_id, user_id) == ("tenant-a", "user-a")
            return FakeCrewLease()

    monkeypatch.setattr(dashboard_module, "get_current_user", verified_user)
    monkeypatch.setattr(dashboard_module, "mint_crew_link", mint_link)
    monkeypatch.setattr(
        dashboard_module,
        "crew_http_client",
        lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(fake_pod),
            trust_env=False,
        ),
    )
    monkeypatch.setattr(websocket_module, "crew_connection_registry", Registry)
    websocket_calls = []

    def fake_websocket_connect(uri, **kwargs):
        websocket_calls.append(kwargs)
        return FakeCrewWebSocket()

    monkeypatch.setattr(websocket_module, "crew_websocket_connect", fake_websocket_connect)
    with TestClient(app, base_url="https://crew.example.test") as client:
        no_identity = client.get("/api/chat/slots", headers={"Cookie": "mc_token_5476=owner-a"})
        assert no_identity.status_code == 401
        cookie_only = client.get("/crew/handoff", headers={"Cookie": signed_cookie()})
        assert cookie_only.status_code == 401
        assert not handoffs and not forwarded

        handoff = client.get("/crew/handoff", headers={"Authorization": "Bearer valid"}, follow_redirects=False)
        assert handoff.status_code == 302
        assert handoff.headers["location"] == "https://crew.example.test/?token=single-use-link"
        session = client.cookies["adp_crew"]
        assert "Secure" in handoff.headers["set-cookie"]
        assert "HttpOnly" in handoff.headers["set-cookie"]
        assert "SameSite=lax" in handoff.headers["set-cookie"]
        assert "Domain=" not in handoff.headers["set-cookie"]
        assert handoffs == [("user-a", 1)]

        cookie = f"mc_token_5476=owner-a; adp_crew={session}"
        admitted = client.get("/api/chat/slots", headers={"Cookie": cookie})
        assert admitted.status_code == 200
        assert admitted.json() == {"owner": "pod-a.test"}
        assert forwarded == ["mc_token_5476=owner-a"]
        with client.websocket_connect("ws://crew.example.test/api/live", headers={"Cookie": cookie}) as browser:
            browser.send_text("hello")
            assert browser.receive_text() == "hello"
            browser.send_text("close")
            with pytest.raises(WebSocketDisconnect):
                browser.receive_text()
        assert websocket_calls[0]["additional_headers"]["Cookie"] == "mc_token_5476=owner-a"
        assert auth_checks == [None, None, "Bearer valid"]

        invalid_bearer = client.get("/api/chat/slots", headers={"Authorization": "Bearer invalid", "Cookie": cookie})
        assert invalid_bearer.status_code == 401
        assert forwarded == ["mc_token_5476=owner-a"]

        async def revoked(*_, **__):
            raise ChatAuthorizationRefusedError("chat_principal_not_tenant_member")

        monkeypatch.setattr(proxy, "tenant_member_principal", revoked)
        revoked_response = client.get("/api/chat/slots", headers={"Cookie": cookie})
        assert revoked_response.status_code == 404
        assert forwarded == ["mc_token_5476=owner-a"]


@pytest.mark.timeout(5)
def test_invalid_or_expired_dashboard_cookie_cannot_authenticate_browser(dashboard, monkeypatch):
    app, _, resolved, _ = dashboard
    monkeypatch.setattr(dashboard_module, "crew_http_client", lambda: pytest.fail("pod contacted"))
    with TestClient(app, base_url="https://crew.example.test") as client:
        for cookie in ("adp_crew=invalid.invalid", signed_cookie(now=0)):
            response = client.get("/api/chat/slots", headers={"Cookie": cookie})
            assert response.status_code == 401
            assert response.headers["cache-control"] == "no-store"
            with pytest.raises(WebSocketDenialResponse) as websocket:
                with client.websocket_connect("ws://crew.example.test/api/live", headers={"Cookie": cookie}):
                    pass
            assert websocket.value.status_code == 401
    assert resolved == []


@pytest.mark.timeout(5)
def test_handoff_audit_failure_returns_neither_link_nor_cookie(dashboard, monkeypatch):
    app, host_app, _, _ = dashboard
    host_app.dependency_overrides[crew_identity] = lambda: identity("user-a")
    audit_db = host_app.dependency_overrides[get_db]()
    audit_db.fail_commit = True

    async def mint_link(owner, target, *, now):
        return "single-use-link"

    monkeypatch.setattr(dashboard_module, "mint_crew_link", mint_link)
    with TestClient(app, base_url="https://crew.example.test") as client:
        response = client.get("/crew/handoff", follow_redirects=False)
    assert response.status_code == 503
    assert response.json() == {"detail": {"error": "crew_handoff_unavailable"}}
    assert "location" not in response.headers
    assert "set-cookie" not in response.headers
    assert "single-use-link" not in response.text
    assert not audit_db.rows and not audit_db.pending
    assert audit_db.rollbacks == 1


@pytest.mark.timeout(5)
def test_browser_json_handoff_sets_cookie_on_crew_host_with_cors(dashboard, monkeypatch):
    app, host_app, _, _ = dashboard
    app.add_middleware(CORSMiddleware, allow_origins=["https://adp.example.test"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
    minted = []

    async def verified_user(request, authorization):
        if authorization != "Bearer valid":
            raise HTTPException(401, detail={"error": "invalid_adp_session"})
        return identity("user-a")

    async def mint_link(owner, target, *, now):
        minted.append((owner.user_id, target.lease.generation))
        return "single-use-link-12345"

    from fastapi import HTTPException

    monkeypatch.setattr(dashboard_module, "get_current_user", verified_user)
    monkeypatch.setattr(dashboard_module, "mint_crew_link", mint_link)
    with TestClient(app, base_url="https://crew.example.test") as client:
        preflight = client.options(
            "/crew/handoff",
            headers={
                "Origin": "https://adp.example.test",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization",
            },
        )
        assert preflight.status_code == 200
        assert preflight.headers["access-control-allow-origin"] == "https://adp.example.test"
        refused = client.options(
            "/crew/handoff",
            headers={
                "Origin": "https://attacker.example.test",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization",
            },
        )
        assert refused.status_code == 400
        assert not minted
        response = client.get(
            "/crew/handoff",
            headers={
                "Origin": "https://adp.example.test",
                "Authorization": "Bearer valid",
                "Accept": "application/json",
            },
            follow_redirects=False,
        )
    assert response.status_code == 200
    assert response.json() == {"redirect_url": "https://crew.example.test/?token=single-use-link-12345"}
    assert response.headers["access-control-allow-origin"] == "https://adp.example.test"
    assert response.headers["access-control-allow-credentials"] == "true"
    assert response.headers["cache-control"] == "no-store"
    assert "Secure" in response.headers["set-cookie"]
    assert "HttpOnly" in response.headers["set-cookie"]
    assert minted == [("user-a", 1)]
    assert len(host_app.dependency_overrides[get_db]().rows) == 1


def test_dashboard_cookie_is_bound_to_target_kind_and_box(dashboard, monkeypatch):
    app, host_app, resolved, upstream = dashboard
    host_app.dependency_overrides[crew_identity] = lambda: identity("user-a")
    endpoint = "https://10.4.0.3:8443"

    class HostResolver:
        async def resolve(self, owner: CrewOwner, *, now: int):
            resolved.append(owner)
            return ResolvedCrewTarget(
                endpoint,
                CrewLease(
                    tenant_id="tenant-a",
                    user_id="user-a",
                    target_kind="ec2-instance",
                    target_ref="box-a",
                    generation=1,
                    expires_at=4_000_000_000,
                    endpoint=endpoint,
                ),
                "ready",
            )

    host_app.dependency_overrides[get_target_resolver] = lambda: HostResolver()
    forwarded = []

    def host_gateway(request):
        forwarded.append((request.url.host, request.headers.get("cookie")))
        return httpx.Response(200, json={"host": "current"})

    async def mock_host_client(_target):
        return httpx.AsyncClient(transport=httpx.MockTransport(host_gateway), trust_env=False)

    monkeypatch.setattr(dashboard_module, "crew_host_client", mock_host_client)
    with TestClient(app, base_url="https://crew.example.test") as client:
        for cookie in (signed_cookie(), signed_cookie(generation=2), signed_cookie(target_kind="ec2-instance", target_ref="box-b")):
            response = client.get("/api/chat/slots", headers={"Cookie": cookie})
            assert response.status_code == 409
            assert response.json() == {"detail": {"error": "crew_handoff_required"}}
            with pytest.raises(WebSocketDenialResponse) as denied:
                with client.websocket_connect("ws://crew.example.test/api/live", headers={"Cookie": cookie}):
                    pass
            assert denied.value.status_code == 409
        accepted = client.get(
            "/api/chat/slots",
            headers={
                "Cookie": signed_cookie(target_kind="ec2-instance", target_ref="box-a"),
            },
        )
        assert accepted.status_code == 200 and accepted.json() == {"host": "current"}
    assert forwarded == [("10.4.0.3", None)]
    assert upstream == []


def test_ec2_owner_handoff_mints_and_forwards_through_current_host(dashboard, monkeypatch):
    app, host_app, resolved, upstream = dashboard
    endpoint = "https://10.4.0.3:8443"
    run_id = "00000000-0000-4000-8000-000000000001"
    custody_requests = []
    forwarded = []
    signatures = []
    deliveries = []

    class HostResolver:
        async def resolve(self, owner: CrewOwner, *, now: int):
            resolved.append(owner)
            if owner.user_id != "user-a":
                raise CrewTargetMissingError("missing")
            return ResolvedCrewTarget(
                endpoint,
                CrewLease(
                    tenant_id="tenant-a",
                    user_id="user-a",
                    target_kind="ec2-instance",
                    target_ref="box-a",
                    generation=2,
                    expires_at=4_000_000_000,
                    endpoint=endpoint,
                ),
                "ready",
            )

    async def verified_user(request, authorization):
        if authorization not in ("Bearer owner", "Bearer foreign"):
            raise HTTPException(401, detail={"error": "invalid_adp_session"})
        return identity("user-a" if authorization == "Bearer owner" else "user-b")

    async def active_run(owner, target, *, now):
        assert (owner.user_id, target.lease.target_ref, target.lease.generation) == ("user-a", "box-a", 2)
        return run_id

    def sign(**kwargs):
        signatures.append(kwargs)
        return "signed-by-gateway"

    def host(request):
        assert request.url.host == "10.4.0.3" and request.url.port == 8443
        if request.url.path == "/mint":
            custody_requests.append(request)
            return httpx.Response(
                200,
                json={
                    "link_token": "single-use-link-12345",
                    "lease_generation": 2,
                    "expires_at": datetime.fromtimestamp(time.time() + 240, UTC).isoformat(),
                },
            )
        forwarded.append(request)
        return httpx.Response(200, json={"host": "current"})

    host_app.dependency_overrides[get_target_resolver] = lambda: HostResolver()
    monkeypatch.setattr(dashboard_module, "get_current_user", verified_user)
    monkeypatch.setattr(handoff_module, "active_crew_run", active_run)
    monkeypatch.setattr(handoff_module, "sign_envelope", sign)

    async def deliver(owner, target, *, now):
        deliveries.append((owner, target))

    monkeypatch.setattr(handoff_module, "deliver_model_token", deliver)

    async def mock_host_client(_target):
        return httpx.AsyncClient(transport=httpx.MockTransport(host), trust_env=False)

    monkeypatch.setattr(handoff_module, "crew_host_client", mock_host_client)
    monkeypatch.setattr(dashboard_module, "crew_host_client", handoff_module.crew_host_client)

    with TestClient(app, base_url="https://crew.example.test") as client:
        cookie_only = client.get("/crew/handoff", headers={"Cookie": signed_cookie()})
        assert cookie_only.status_code == 401
        foreign = client.get("/crew/handoff", headers={"Authorization": "Bearer foreign"})
        assert foreign.status_code == 404
        assert not custody_requests and not forwarded

        response = client.get("/crew/handoff", headers={"Authorization": "Bearer owner"}, follow_redirects=False)
        assert response.status_code == 302
        assert response.headers["location"] == "https://crew.example.test/?token=single-use-link-12345"
        assert "Secure" in response.headers["set-cookie"] and "HttpOnly" in response.headers["set-cookie"]
        session = client.cookies["adp_crew"]
        admitted = client.get("/api/chat/slots", headers={"Cookie": f"adp_crew={session}; mc_token_5476=owner"})
        assert admitted.status_code == 200 and admitted.json() == {"host": "current"}

    assert [owner.user_id for owner in resolved] == ["user-b", "user-a", "user-a"]
    assert not upstream
    assert len(custody_requests) == 1 and custody_requests[0].url.path == "/mint"
    assert len(deliveries) == 1 and deliveries[0][0] == CrewOwner("tenant-a", "user-a")
    assert custody_requests[0].headers["x-adp-crew-signature"] == "signed-by-gateway"
    body = json.loads(custody_requests[0].content)
    assert (body["run_id"], body["lease_generation"], body["user_id"]) == (run_id, 2, "user-a")
    assert signatures[0]["request_body"] == custody_requests[0].content
    assert signatures[0]["action"] == "dashboard.mint"
    assert len(forwarded) == 1 and forwarded[0].url.path == "/api/chat/slots"
    assert forwarded[0].headers.get("cookie") == "mc_token_5476=owner"
    assert forwarded[0].headers.get("authorization") is None
    assert len(host_app.dependency_overrides[get_db]().rows) == 1


def test_ec2_websocket_uses_pinned_host_tls_and_owner_ticket(dashboard, monkeypatch):
    app, host_app, _, _ = dashboard
    host_app.dependency_overrides[crew_identity] = lambda: identity("user-a")
    endpoint = "https://10.4.0.3:8443"
    tls_context = object()
    connections = []

    class HostResolver:
        async def resolve(self, owner: CrewOwner, *, now: int):
            return ResolvedCrewTarget(
                endpoint,
                CrewLease(
                    tenant_id=owner.tenant_id,
                    user_id=owner.user_id,
                    target_kind="ec2-instance",
                    target_ref="box-a",
                    generation=1,
                    expires_at=4_000_000_000,
                    endpoint=endpoint,
                ),
                "ready",
            )

    class Registry:
        async def acquire(self, tenant_id, user_id):
            assert (tenant_id, user_id) == ("tenant-a", "user-a")
            return FakeCrewLease()

    def connect(uri, **kwargs):
        connections.append((uri, kwargs))
        return FakeCrewWebSocket()

    host_app.dependency_overrides[get_target_resolver] = lambda: HostResolver()
    monkeypatch.setattr(websocket_module, "crew_connection_registry", Registry)

    async def mock_tls(_target):
        return tls_context

    monkeypatch.setattr(websocket_module, "crew_host_context", mock_tls)
    monkeypatch.setattr(websocket_module, "crew_websocket_connect", connect)
    with TestClient(app, base_url="https://crew.example.test") as client:
        with client.websocket_connect(
            "ws://crew.example.test/api/live",
            headers={
                "Cookie": signed_cookie(target_kind="ec2-instance", target_ref="box-a"),
            },
        ) as browser:
            browser.send_text("close")
            with pytest.raises(WebSocketDisconnect):
                browser.receive_text()
    uri, kwargs = connections[0]
    assert uri == "wss://crew.example.test/api/live"
    assert kwargs["host"] == "10.4.0.3" and kwargs["port"] == 8443
    assert kwargs["ssl"] is tls_context and kwargs["server_hostname"] == "10.4.0.3"

    async def missing_trust(_target):
        raise HTTPException(503, detail={"error": "crew_host_trust_unavailable"})

    monkeypatch.setattr(websocket_module, "crew_host_context", missing_trust)
    with TestClient(app, base_url="https://crew.example.test") as client:
        with pytest.raises(WebSocketDenialResponse) as denied:
            with client.websocket_connect(
                "ws://crew.example.test/api/live",
                headers={
                    "Cookie": signed_cookie(target_kind="ec2-instance", target_ref="box-a"),
                },
            ):
                pass
    assert denied.value.status_code == 503
    assert len(connections) == 1
