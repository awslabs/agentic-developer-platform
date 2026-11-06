"""Dev Box Crew tools ride the host's signed model identity and end with the lease (#8380)."""

import json
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request

from src.agentauth.chat_crew_run import CrewRunStore
from src.crew import host_tools
from src.crew.model_token import mint_model_token
from src.crew.targets import CrewLease, CrewLeaseFencedError, CrewOwner, ResolvedCrewTarget
from src.proxy.routes import get_token_context, set_agent_run_id_from_header
from src.shared.schemas.auth import TokenContext
from tests.agentauth.test_chat_crew_run import registry as registry_fixture

registry = registry_fixture
INSTANCE = "i-0123456789abcdef0"


def host_context(instance_id=INSTANCE, *, auth_source="iam", account_type="service"):
    identity = TokenContext(
        user_id="gateway-service",
        org_id="tenant-a",
        team_id="",
        department_id="",
        account_type=account_type,
        auth_source=auth_source,
        scope="internal",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    identity._dev_box_instance_id = instance_id
    identity._dev_box_role_arn = "arn:aws:iam::000000000000:role/guest"
    return identity


@pytest.fixture
def active(registry, monkeypatch):
    now = int(time.time())
    store = CrewRunStore(registry, host_role_arn="arn:aws:iam::000000000000:role/guest")
    run_id = store.mint(
        tenant_id="tenant-a",
        user_id="owner-a",
        session_id="box-a",
        lease_generation=3,
        lease_expires_at=now + 300,
        target_kind="ec2-instance",
        target_ref=INSTANCE,
        deployment_id="dep-a",
        now=now,
    )
    monkeypatch.setattr("src.crew.handoff.current_crew_store", lambda: store)
    monkeypatch.setattr("src.agentauth.chat_crew_run.current_crew_store", lambda: store)
    monkeypatch.setattr("src.crew.model_token._secret", lambda: b"fictional-test-signing-key")
    owner = CrewOwner("tenant-a", "owner-a")
    endpoint = "https://10.4.0.3:8443"
    target = ResolvedCrewTarget(
        endpoint,
        CrewLease(
            tenant_id="tenant-a",
            user_id="owner-a",
            target_kind="ec2-instance",
            target_ref="box-a",
            generation=3,
            expires_at=now + 300,
            endpoint=endpoint,
        ),
        "ready",
        host_run_id=run_id,
        host_instance_ref=INSTANCE,
        host_fingerprint="a" * 64,
    )
    resolver = SimpleNamespace(target=target, fenced=False)

    async def resolve(selected_owner, *, now):
        assert selected_owner == owner
        if resolver.fenced:
            raise CrewLeaseFencedError("lease_fenced")
        return resolver.target

    resolver.resolve = resolve
    monkeypatch.setattr("src.crew.proxy.get_target_resolver", lambda: resolver)
    return SimpleNamespace(store=store, owner=owner, target=target, run_id=run_id, resolver=resolver, now=now)


@pytest.fixture
def relay(active, monkeypatch):
    app = FastAPI()
    app.include_router(host_tools.router)
    identity = SimpleNamespace(context=host_context())

    async def authenticated(request: Request):
        request.state.token_context = identity.context
        return identity.context

    async def run_header(request: Request):
        return request.headers.get("x-agent-runid")

    app.dependency_overrides[get_token_context] = authenticated
    app.dependency_overrides[set_agent_run_id_from_header] = run_header
    forwarded = []

    def facade(request):
        forwarded.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"tools": [{"name": "websearch___search"}]})
        return httpx.Response(200, json={"request_id": json.loads(request.content)["request_id"], "status": "confirmed"})

    monkeypatch.setattr(host_tools.httpx, "AsyncClient", lambda **kwargs: AsyncClient(transport=httpx.MockTransport(facade), **kwargs))
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://host")
    return SimpleNamespace(client=client, forwarded=forwarded, identity=identity)


async def post_tools(relay, active, token, **changes):
    headers = {"X-Agent-RunId": active.run_id, "Authorization": "fictional-sigv4"}
    if token is not None:
        headers["X-Adp-Crew-Model-Token"] = token
    body = {"method": "GET", "path": "/tools", **changes}
    return await relay.client.post("/crew/tools", json=body, headers=headers)


async def test_host_discovery_and_call_reach_the_facade_with_only_a_host_assertion(active, relay):
    token = await mint_model_token(active.owner, active.target, now=active.now)
    listing = await post_tools(relay, active, token)
    assert listing.status_code == 200, listing.text
    assert listing.json() == {"status": 200, "body": {"tools": [{"name": "websearch___search"}]}}
    call = await post_tools(
        relay,
        active,
        token,
        method="POST",
        path="/tools/call",
        body={"tool": "websearch___search", "arguments": {"query": "example"}, "request_id": "00000000-0000-4000-8000-000000000099"},
    )
    assert call.status_code == 200, call.text
    assert call.json()["body"]["status"] == "confirmed"
    assert [str(request.url) for request in relay.forwarded] == [host_tools.facade_endpoint(), host_tools.facade_endpoint() + "/call"]
    for request in relay.forwarded:
        assert request.headers["x-agent-runid"] == active.run_id
        # The route mints at its own wall clock; a slow CI runner may be a second past the fixture's `now`.
        claims = host_tools.verify_host_tools_assertion(request.headers[host_tools.ASSERTION_HEADER], now=int(time.time()))
        assert claims["run"] == active.run_id and claims["instance"] == INSTANCE and claims["owner"] == "owner-a"
        assert claims["exp"] - claims["nbf"] <= host_tools.ASSERTION_LIFETIME_SECONDS
        for leaked in ("authorization", "x-adp-crew-model-token", "x-adp-run-credential", "x-adp-chat-capability", "x-adp-user-authorization"):
            assert leaked not in request.headers


@pytest.mark.parametrize("consented", [False, True])
async def test_host_memory_call_preserves_consent_response(active, relay, monkeypatch, consented):
    token = await mint_model_token(active.owner, active.target, now=active.now)
    if not consented:

        def refusing(request):
            return httpx.Response(403, json={"detail": "tool_not_consented"})

        monkeypatch.setattr(host_tools.httpx, "AsyncClient", lambda **kwargs: AsyncClient(transport=httpx.MockTransport(refusing), **kwargs))
    response = await post_tools(
        relay,
        active,
        token,
        method="POST",
        path="/tools/call",
        body={
            "tool": "knowledge___remember",
            "arguments": {"session_id": "fixture-session", "messages": []},
            "request_id": "00000000-0000-4000-8000-000000000099",
        },
    )
    assert response.status_code == 200
    assert response.json()["status"] == (200 if consented else 403)
    assert response.json()["body"] == (
        {"request_id": "00000000-0000-4000-8000-000000000099", "status": "confirmed"} if consented else {"detail": "tool_not_consented"}
    )
    if consented:
        assert len(relay.forwarded) == 1 and relay.forwarded[0].headers[host_tools.ASSERTION_HEADER]


@pytest.mark.parametrize(
    "path", ["/tools/../internal", "https://attacker.invalid/tools", "/tools/%2e%2e/internal", "/tools#fragment", "/internal/tools"]
)
async def test_relay_cannot_escape_the_tools_routes(active, relay, path):
    token = await mint_model_token(active.owner, active.target, now=active.now)
    assert (await post_tools(relay, active, token, path=path)).status_code == 400
    assert not relay.forwarded


async def test_missing_or_foreign_model_token_never_reaches_the_facade(active, relay):
    assert (await post_tools(relay, active, None)).status_code == 401
    assert (await post_tools(relay, active, "not-a-token")).status_code == 401
    assert (await post_tools(relay, active, "eyJ0ZXN0IjoidG9rZW4ifQ.c2lnbmF0dXJl")).status_code == 401
    assert not relay.forwarded


@pytest.mark.parametrize("change", ["instance", "human", "jwt", "no-run"])
async def test_only_the_current_host_with_instance_identity_is_admitted(active, relay, change):
    token = await mint_model_token(active.owner, active.target, now=active.now)
    if change == "instance":
        relay.identity.context = host_context("i-11111111111111111")
    elif change == "human":
        relay.identity.context = host_context(account_type="human")
    elif change == "jwt":
        relay.identity.context = host_context(auth_source="jwt")
    if change == "no-run":
        response = await relay.client.post(
            "/crew/tools", json={"method": "GET", "path": "/tools"}, headers={"X-Adp-Crew-Model-Token": token, "Authorization": "fictional-sigv4"}
        )
    else:
        response = await post_tools(relay, active, token)
    assert response.status_code == 403, response.text
    assert not relay.forwarded


async def test_fenced_or_revoked_lease_ends_tools_access(active, relay):
    token = await mint_model_token(active.owner, active.target, now=active.now)
    active.resolver.fenced = True
    assert (await post_tools(relay, active, token)).status_code == 409
    active.resolver.fenced = False
    active.store.revoke_for_lease(tenant_id="tenant-a", session_id="box-a", lease_generation=3, expected_run_id=active.run_id)
    assert (await post_tools(relay, active, token)).status_code in (401, 403, 409)
    assert not relay.forwarded


async def test_facade_failure_is_a_recoverable_relay_status_not_a_retry(active, relay, monkeypatch):
    token = await mint_model_token(active.owner, active.target, now=active.now)

    def broken(request):
        raise httpx.ConnectError("facade down")

    monkeypatch.setattr(host_tools.httpx, "AsyncClient", lambda **kwargs: AsyncClient(transport=httpx.MockTransport(broken), **kwargs))
    response = await post_tools(relay, active, token)
    assert response.status_code == 200
    assert response.json()["status"] == 502


def authorize_request(headers):
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/tools/authorize",
        "headers": [(name.encode(), value.encode()) for name, value in headers],
        "query_string": b"",
    }
    return Request(scope)


async def test_authorize_plane_admits_the_assertion_only_while_the_target_still_resolves(active):
    assertion = host_tools.mint_host_tools_assertion(active.owner, active.target, active.run_id, now=active.now)
    identity = await host_tools.authenticate_crew_host(
        authorize_request([(host_tools.ASSERTION_HEADER, assertion), ("x-agent-runid", active.run_id)])
    )
    assert identity == host_tools.CrewHostToolsIdentity(
        tenant_id="tenant-a",
        user_id="owner-a",
        run_id=active.run_id,
        instance_id=INSTANCE,
        box_id="box-a",
        generation=3,
        expires_at=active.target.lease.expires_at,
    )
    # The same assertion is refused once the owner's box moved on or the lease fenced.
    moved = ResolvedCrewTarget(
        active.target.endpoint,
        active.target.lease.model_copy(update={"generation": 4}),
        "ready",
        host_run_id=active.run_id,
        host_instance_ref="i-22222222222222222",
        host_fingerprint="b" * 64,
    )
    active.resolver.target = moved
    with pytest.raises(HTTPException) as refused:
        await host_tools.authenticate_crew_host(authorize_request([(host_tools.ASSERTION_HEADER, assertion), ("x-agent-runid", active.run_id)]))
    assert refused.value.status_code == 404
    active.resolver.target = active.target
    active.resolver.fenced = True
    with pytest.raises(HTTPException) as fenced:
        await host_tools.authenticate_crew_host(authorize_request([(host_tools.ASSERTION_HEADER, assertion), ("x-agent-runid", active.run_id)]))
    assert fenced.value.status_code == 404


@pytest.mark.parametrize("variant", ["forged", "expired", "other-run-header", "with-crew-proof", "with-run-credential", "duplicate"])
async def test_authorize_plane_refuses_forged_stale_or_ambiguous_assertions(active, monkeypatch, variant):
    assertion = host_tools.mint_host_tools_assertion(active.owner, active.target, active.run_id, now=active.now)
    headers = [(host_tools.ASSERTION_HEADER, assertion), ("x-agent-runid", active.run_id)]
    if variant == "forged":
        encoded, _signed = assertion.split(".")
        headers[0] = (host_tools.ASSERTION_HEADER, f"{encoded}.{'A' * 43}")
    elif variant == "expired":
        monkeypatch.setattr(host_tools.time, "time", lambda: active.now + host_tools.ASSERTION_LIFETIME_SECONDS + 1)
    elif variant == "other-run-header":
        headers[1] = ("x-agent-runid", "00000000-0000-4000-8000-000000000000")
    elif variant == "with-crew-proof":
        headers.append(("x-adp-chat-capability", "capability"))
    elif variant == "with-run-credential":
        headers.append(("x-adp-run-credential", "credential"))
    elif variant == "duplicate":
        headers.append((host_tools.ASSERTION_HEADER, assertion))
    with pytest.raises(HTTPException) as refused:
        await host_tools.authenticate_crew_host(authorize_request(headers))
    assert refused.value.status_code == 403


async def test_assertion_is_bound_to_the_live_lease_and_never_outlives_it(active):
    short = ResolvedCrewTarget(
        active.target.endpoint,
        active.target.lease.model_copy(update={"expires_at": active.now + 5}),
        "ready",
        host_run_id=active.run_id,
        host_instance_ref=INSTANCE,
        host_fingerprint="a" * 64,
    )
    claims = host_tools.verify_host_tools_assertion(
        host_tools.mint_host_tools_assertion(active.owner, short, active.run_id, now=active.now), now=active.now
    )
    assert claims["exp"] == active.now + 5
    with pytest.raises(HTTPException) as fenced:
        host_tools.mint_host_tools_assertion(active.owner, short, active.run_id, now=active.now + 5)
    assert fenced.value.status_code == 409
    with pytest.raises(HTTPException):
        host_tools.mint_host_tools_assertion(active.owner, active.target, "another-run", now=active.now)


async def test_the_catalogue_listing_is_remembered_per_run_for_a_minute(active, relay, monkeypatch):
    """#8223 Dev Box latency: Claude Code lists tools every time Kiro Crew spawns it for a turn,
    and the façade's authority round trip cost 3.8–12 s each time. Only the exact `GET /tools`
    listing is cached, per (tenant, owner, run), for LISTING_CACHE_TTL_SECONDS; calls and
    operation reads always reach the façade, and a different run never shares an entry."""
    host_tools._LISTING_CACHE.clear()
    token = await mint_model_token(active.owner, active.target, now=active.now)
    first = await post_tools(relay, active, token)
    second = await post_tools(relay, active, token)
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert [r.method for r in relay.forwarded] == ["GET"], "the second listing must be served from the relay cache"
    # A tool call is never cached.
    call = await post_tools(
        relay,
        active,
        token,
        method="POST",
        path="/tools/call",
        body={"tool": "websearch___search", "arguments": {}, "request_id": "11111111-1111-4111-8111-111111111111"},
    )
    assert call.status_code == 200 and [r.method for r in relay.forwarded] == ["GET", "POST"]
    # Another run of the same owner gets its own listing.
    other_run = active.store.mint(
        tenant_id="tenant-a",
        user_id="owner-a",
        session_id="box-a",
        lease_generation=4,
        lease_expires_at=active.now + 300,
        target_kind="ec2-instance",
        target_ref=INSTANCE,
        deployment_id="dep-a",
        now=active.now,
    )
    assert host_tools._cached_listing(("tenant-a", "owner-a", other_run), active.now) is None
    # The entry expires after the TTL.
    monkeypatch.setattr(host_tools.time, "time", lambda: active.now + host_tools.LISTING_CACHE_TTL_SECONDS + 1)
    token = await mint_model_token(active.owner, active.target, now=active.now + host_tools.LISTING_CACHE_TTL_SECONDS + 1)
    third = await post_tools(relay, active, token)
    assert third.status_code == 200 and [r.method for r in relay.forwarded] == ["GET", "POST", "GET"]
    host_tools._LISTING_CACHE.clear()


async def test_a_failed_or_foreign_listing_is_never_remembered(active, relay, monkeypatch):
    host_tools._LISTING_CACHE.clear()
    token = await mint_model_token(active.owner, active.target, now=active.now)

    def refusing(request):
        return httpx.Response(503, json={"error": "tool authority unavailable"})

    monkeypatch.setattr(host_tools.httpx, "AsyncClient", lambda **kwargs: AsyncClient(transport=httpx.MockTransport(refusing), **kwargs))
    refused = await post_tools(relay, active, token)
    assert refused.status_code == 200 and refused.json()["status"] == 503
    assert host_tools._LISTING_CACHE == {}
