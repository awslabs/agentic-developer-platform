import ast
import json
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.domain_proxy import superplane as proxy


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("FEATURE_SUPERPLANE_ENABLED", raising=False)
    monkeypatch.setattr(proxy, "registration", lambda: {"namespace": "superplane", "release_id": "a" * 64})
    app = FastAPI()
    app.include_router(proxy.router)
    with TestClient(app) as value:
        yield value


@pytest.mark.parametrize("revoke", [False, True])
@pytest.mark.parametrize("upstream_status", [200, 403, 409])
def test_organization_mutations_preserve_authentication_body_and_conflicts(client, monkeypatch, revoke, upstream_status):
    path = "/orgs/current/access/v1/grants" + ("/example-grant/revoke" if revoke else "")
    body = {
        "target_subject": "other-human",
        "principal_type": "human",
        "expected_revision": 1 if revoke else 0,
        "request_id": "11111111-1111-1111-1111-111111111111",
        "reason": "access_revocation" if revoke else "access_assignment",
    }
    if not revoke:
        body["permissions"] = ["organization:read"]
    calls = []

    def upstream(request):
        calls.append(request)
        return httpx.Response(upstream_status, json={"result": "domain-result"})

    original = httpx.AsyncClient
    monkeypatch.setattr(proxy.httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(upstream), **kw))
    assert client.post(f"/superplane/v1{path}", json=body).status_code == 401
    assert calls == []
    response = client.post(f"/superplane/v1{path}", json=body, headers={"Authorization": "Bearer user-token"})
    assert response.status_code == upstream_status and response.json() == {"result": "domain-result"}
    assert len(calls) == 1 and calls[0].url.path == path
    assert calls[0].headers["authorization"] == "Bearer user-token"
    assert json.loads(calls[0].content) == body
    assert client.delete(f"/superplane/v1{path}", headers={"Authorization": "Bearer user-token"}).status_code == 404
    assert len(calls) == 1


def test_all_public_routes_come_from_maintained_inventory():
    root = Path(__file__).resolve().parents[4]
    inventory = root / "modules/domain-apps/superplane/src/superplane-api/app/endpoint_inventory.py"
    tree = ast.parse(inventory.read_text())
    value = next(n.value for n in tree.body if isinstance(n, ast.AnnAssign) and getattr(n.target, "id", None) == "DOMAIN_ROUTES")
    expected = {ast.literal_eval(k) for k in value.keys} - {("POST", "/auth/token")}
    actual = {tuple(x) for x in json.loads(Path(proxy.__file__).with_name("superplane_routes.json").read_text())}
    # This existing Gateway-owned route resolves current installation authority;
    # its validation work is intentionally not forwarded to the domain API.
    gateway_owned = {("POST", "/provider-authorities/{credential_id}/validation")}
    mounted = {(method, route.path.removeprefix("/superplane/v1")) for route in proxy.router.routes for method in route.methods or ()}
    assert gateway_owned <= mounted
    assert actual == expected | gateway_owned


@pytest.mark.parametrize(
    "method,tail",
    [
        ("GET", "me"),
        ("POST", "grants"),
        ("POST", "grants/example-grant/revoke"),
    ],
)
def test_workspace_access_routes_forward_only_with_bearer(client, monkeypatch, method, tail):
    calls = []
    body = (
        {
            "target_subject": "human-approver",
            "principal_type": "human",
            "permissions": ["workspace:read"],
            "reason": "approver_setup",
            "expected_revision": 0,
            "request_id": "11111111-1111-1111-1111-111111111111",
        }
        if method == "POST"
        else None
    )
    if tail.endswith("/revoke"):
        body.pop("permissions")
        body.update(reason="access_revocation", expected_revision=1)

    def upstream(request):
        calls.append(request)
        return httpx.Response(403, json={"detail": "workspace authority refused"})

    original = httpx.AsyncClient
    monkeypatch.setattr(proxy.httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(upstream), **kw))
    route = f"/superplane/v1/workspaces/example/access/v1/{tail}"
    assert client.request(method, route).status_code == 401
    assert calls == []
    response = client.request(method, route, json=body, headers={"Authorization": "Bearer user-token"})
    assert response.status_code == 403
    assert len(calls) == 1
    assert calls[0].method == method
    assert str(calls[0].url).endswith(f"/workspaces/example/access/v1/{tail}")
    if body is not None:
        assert json.loads(calls[0].content) == body


@pytest.mark.parametrize(
    "path",
    [
        "/workspaces/example/access/v1/grants",
        "/orgs/current/access/v1/me",
        "/orgs/current/access/v1/grants",
    ],
)
@pytest.mark.parametrize("status", [200, 403])
def test_access_reads_preserve_authentication_pagination_and_domain_denials(client, monkeypatch, path, status):
    calls = []
    params = {"limit": "2", "after": "11111111-1111-1111-1111-111111111111"} if path.endswith("/grants") else {}
    payload = (
        {"detail": "explicit scoped authority required"}
        if status == 403
        else {"assignments": [], "next_after": None}
        if path.endswith("/grants")
        else {"subject": "human-reader", "effective_permissions": ["organization:read"]}
    )

    def upstream(request):
        calls.append(request)
        return httpx.Response(status, json=payload, headers={"cache-control": "no-store"})

    original = httpx.AsyncClient
    monkeypatch.setattr(proxy.httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(upstream), **kw))
    route = "/superplane/v1" + path
    assert client.get(route, params=params).status_code == 401
    assert calls == []
    response = client.get(route, params=params, headers={"Authorization": "Bearer user-token"})
    assert response.status_code == status
    assert response.json() == payload
    assert response.headers["cache-control"] == "no-store"
    assert len(calls) == 1
    assert calls[0].method == "GET" and calls[0].url.path == path
    assert dict(calls[0].url.params) == params
    assert calls[0].headers["authorization"] == "Bearer user-token"
    assert client.delete(route, headers={"Authorization": "Bearer user-token"}).status_code == 404
    assert client.get(route + "/unexpected", headers={"Authorization": "Bearer user-token"}).status_code == 404
    monkeypatch.setenv("FEATURE_SUPERPLANE_ENABLED", "false")
    assert client.get(route, headers={"Authorization": "Bearer user-token"}).status_code == 404
    assert len(calls) == 1


@pytest.mark.parametrize(
    "path",
    [
        "internal/observations/clusters",
        "internal/provider-operations",
        "health",
        "docs",
        "openapi.json",
        "auth/login",
        "auth/token",
        "workspaces/%252e%252e/internal",
        "workspaces/a%2Fb",
        "workspaces/a\\b",
    ],
)
def test_private_and_ambiguous_paths_never_forward(client, path):
    assert client.get("/superplane/v1/" + path, headers={"Authorization": "Bearer test"}).status_code == 404


def test_missing_bearer_and_feature_off(client, monkeypatch):
    assert client.get("/superplane/v1/workspaces").status_code == 401
    monkeypatch.setenv("FEATURE_SUPERPLANE_ENABLED", "false")
    assert client.get("/superplane/v1/workspaces", headers={"Authorization": "Bearer test"}).status_code == 404


@pytest.mark.parametrize(
    "fault,expected",
    [
        (None, 200),
        ("different-tenant", 409),
        ("forged", 401),
        ("expired", 401),
        ("different-subject", 401),
        ("different-pool", 401),
        ("revoked", 403),
        ("invalid-cognito", 401),
        ("malformed", 401),
    ],
)
def test_tenant_envelope_never_loses_its_verified_scope(client, monkeypatch, fault, expected):
    from contextlib import asynccontextmanager
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    import jwt
    from fastapi import HTTPException

    from src.auth import middleware, tenant_context
    from src.shared.schemas.auth import TokenContext

    now = datetime.now(UTC)
    signing_key = "synthetic-tenant-context-test-key-32-bytes"
    context = TokenContext(
        user_id="verified-human",
        org_id="home-tenant",
        team_id="",
        department_id="",
        account_type="human",
        auth_source="jwt",
        cognito_username="human-login",
        expires_at=now + timedelta(minutes=10),
    )
    verified = []

    async def cognito(authorization):
        verified.append(authorization)
        assert authorization == "Bearer original-cognito-token"
        if fault == "invalid-cognito":
            raise HTTPException(401, "invalid Cognito token")
        return context

    @asynccontextmanager
    async def session():
        yield object()

    memberships = []

    async def membership(db, caller, tenant, expected_membership):
        memberships.append(tenant)
        assert caller == context and expected_membership == "current-membership"
        if fault == "revoked":
            raise HTTPException(403, "membership revoked")
        return SimpleNamespace(id="canonical-human"), "current-membership", None

    monkeypatch.setattr(middleware, "validate_cognito_jwt", cognito)
    monkeypatch.setattr(tenant_context, "get_settings", lambda: SimpleNamespace(token_secret_key=signing_key, cognito_user_pool_id="pool"))
    monkeypatch.setattr(tenant_context, "get_session_factory", lambda: session)
    monkeypatch.setattr(tenant_context, "membership", membership)
    claims = {
        "iss": tenant_context.ISSUER,
        "aud": tenant_context.AUDIENCE,
        "pool": "other-pool" if fault == "different-pool" else "pool",
        "sub": "other-human" if fault == "different-subject" else "verified-human",
        "tenant": "other-tenant" if fault == "different-tenant" else "home-tenant",
        "user_id": "canonical-human",
        "membership_id": "current-membership",
        "iat": now - timedelta(minutes=1),
        "exp": now + timedelta(minutes=-1 if fault == "expired" else 5),
    }
    lease = jwt.encode(claims, "untrusted-signing-key-32-bytes-long" if fault == "forged" else signing_key, algorithm="HS256")
    envelope = f"adpctx1~{lease}~original-cognito-token"
    if fault == "malformed":
        envelope += "~extra"
    calls = []

    def upstream(request):
        calls.append(request)
        return httpx.Response(200, json={"workspaces": []})

    original = httpx.AsyncClient
    monkeypatch.setattr(proxy.httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(upstream), **kw))
    response = client.get("/superplane/v1/workspaces", headers={"Authorization": "Bearer " + envelope, "X-Org-Id": "forged"})
    assert response.status_code == expected
    if fault:
        assert calls == []
    else:
        assert len(calls) == 1 and verified == ["Bearer original-cognito-token"]
        assert memberships == ["home-tenant"]
        assert calls[0].headers["authorization"] == "Bearer original-cognito-token"
        assert "x-org-id" not in calls[0].headers
        assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "api/v1/research/findings"),
        ("GET", "api/v1/research/stats"),
        ("GET", "api/v1/research/proposals"),
        ("PATCH", "api/v1/research/proposals/11111111-1111-1111-1111-111111111111/approve"),
    ],
)
def test_research_routes_need_a_bearer_at_the_edge_too(client, method, path):
    """No unauthenticated caller reaches the research surface — issue #5682 (A02).

    These are the routes that were the reachability path for the A02 finding:
    the domain API served them to an anonymous caller with every tenant's rows
    merged, and all 12 are in this proxy's public allowlist, so the exposure was
    reachable from the internet rather than cluster-local.

    The API side is fixed (each handler now requires a server-derived tenant),
    and that is the fix that matters — this asserts the edge does not forward an
    anonymous request either, so the two layers agree. Pinned per-route rather
    than trusting the shared bearer check, because the allowlist is generated
    from the domain inventory and a future route could arrive without anyone
    re-reading this proxy.
    """
    assert client.request(method, "/superplane/v1/" + path).status_code == 401


def test_forward_only_trusted_transport_headers_and_preserve_denial(client, monkeypatch):
    calls = []

    def upstream(request):
        calls.append(request)
        return httpx.Response(403, json={"detail": "workspace access denied"}, headers={"set-cookie": "secret=bad", "x-org-id": "bad"})

    original = httpx.AsyncClient
    monkeypatch.setattr(proxy.httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(upstream), **kw))
    response = client.get(
        "/superplane/v1/workspaces/example?view=brief",
        headers={"Authorization": "Bearer user-token", "X-Org-Id": "spoofed", "X-Workspace-Id": "spoofed", "Cookie": "other=secret"},
    )
    assert response.status_code == 403
    assert len(calls) == 1
    assert str(calls[0].url) == "http://superplane-api.superplane.svc.cluster.local:8000/workspaces/example?view=brief"
    assert calls[0].headers["authorization"] == "Bearer user-token"
    assert "x-org-id" not in calls[0].headers and "cookie" not in calls[0].headers
    assert "set-cookie" not in response.headers and "x-org-id" not in response.headers


@pytest.mark.parametrize("status", [200, 403])
def test_retained_batch_result_reaches_domain_without_caching(client, monkeypatch, status):
    calls = []
    path = "/workspaces/ws-1/batch-jobs/job-1/result"
    payload = {"content": "accuracy=0.95"} if status == 200 else {"detail": "workspace access denied"}

    def upstream(request):
        calls.append(request)
        return httpx.Response(status, json=payload, headers={"cache-control": "no-store"})

    original = httpx.AsyncClient
    monkeypatch.setattr(proxy.httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(upstream), **kw))
    assert client.get("/superplane/v1" + path).status_code == 401
    response = client.get("/superplane/v1" + path, headers={"Authorization": "Bearer user-token"})
    assert response.status_code == status
    assert response.json() == payload
    assert response.headers["cache-control"] == "no-store"
    assert len(calls) == 1
    assert calls[0].url.path == path
    assert calls[0].headers["authorization"] == "Bearer user-token"
    assert client.post("/superplane/v1" + path, headers={"Authorization": "Bearer user-token"}).status_code == 404
    assert len(calls) == 1


@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "workspaces/preview"),
        ("POST", "workspaces/adopt"),
        ("GET", "workspaces/ws-1/lifecycle-proposals"),
        ("POST", "workspaces/ws-1/lifecycle-proposals/artifact-1/preview"),
        ("POST", "workspaces/ws-1/lifecycle-proposals/artifact-1/continue"),
        ("GET", "operations/op-1"),
        ("GET", "operations/by-idempotency/request-1"),
        ("POST", "operation-approvals"),
        ("GET", "operation-approvals/approval-1"),
        ("POST", "operation-approvals/approval-1/decision"),
    ],
)
def test_governed_onboarding_routes_forward_exact_body_and_preserve_denial(client, monkeypatch, method, path):
    calls = []

    def upstream(request):
        calls.append(request)
        return httpx.Response(403, json={"detail": "operation authority refused"})

    original = httpx.AsyncClient
    monkeypatch.setattr(proxy.httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(upstream), **kw))
    body = {"operation_id": "request-1", "approval_id": "approval-1"}
    assert client.request(method, "/superplane/v1/" + path).status_code == 401
    response = client.request(method, "/superplane/v1/" + path, json=body, headers={"Authorization": "Bearer user-token"})
    assert response.status_code == 403
    assert len(calls) == 1
    assert calls[0].url.path == "/" + path
    assert calls[0].method == method
    assert json.loads(calls[0].content) == body


@pytest.mark.parametrize(
    "action,body",
    [
        ("observe", {"expected_version": 6, "expected_fence": 11}),
        ("review", {"expected_version": 3, "idempotency_key": "11111111-1111-1111-1111-111111111111", "max_runtime_seconds": 10800}),
        ("decision", {"reauthorization_id": "22222222-2222-2222-2222-222222222222", "result": "allowed-once"}),
        ("resume", {"reauthorization_id": "22222222-2222-2222-2222-222222222222"}),
    ],
)
@pytest.mark.parametrize("upstream_status", [200, 403, 409])
def test_settled_bootstrap_recovery_preserves_bearer_body_and_domain_result(client, monkeypatch, action, body, upstream_status):
    calls = []
    recovery = "bootstrap-recovery" if action == "observe" else "settled-bootstrap-recovery"
    path = f"/workspaces/33333333-3333-3333-3333-333333333333/{recovery}/{action}"
    public_path = "/superplane/v1" + path
    content = json.dumps(body, indent=2).encode()
    payload = {"result": "domain-result"}

    def upstream(request):
        calls.append(request)
        return httpx.Response(upstream_status, json=payload)

    original = httpx.AsyncClient
    monkeypatch.setattr(proxy.httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(upstream), **kw))
    assert client.post(public_path, json=body).status_code == 401
    assert client.get(public_path, headers={"Authorization": "Bearer user-token"}).status_code == 404
    assert calls == []
    response = client.post(
        public_path,
        content=content,
        headers={"Authorization": "Bearer user-token", "Content-Type": "application/json", "X-Org-Id": "spoofed", "X-Workspace-Id": "spoofed"},
    )
    assert response.status_code == upstream_status
    assert response.json() == payload
    assert response.headers["cache-control"] == "no-store"
    assert len(calls) == 1
    assert calls[0].method == "POST"
    assert calls[0].url.path == path
    assert calls[0].headers["authorization"] == "Bearer user-token"
    assert calls[0].headers["content-type"] == "application/json"
    assert "x-org-id" not in calls[0].headers and "x-workspace-id" not in calls[0].headers
    assert calls[0].content == content
    monkeypatch.setenv("FEATURE_SUPERPLANE_ENABLED", "false")
    assert client.post(public_path, json=body, headers={"Authorization": "Bearer user-token"}).status_code == 404
    assert len(calls) == 1


def test_default_has_no_registration_and_no_network(monkeypatch):
    monkeypatch.delenv("BG_ENVIRONMENT", raising=False)
    monkeypatch.setattr(proxy, "_cache", (0, {}))
    monkeypatch.setattr(proxy.boto3, "client", lambda *a, **k: pytest.fail("unexpected AWS call"))
    assert proxy.registration() == {}


@pytest.fixture
def route_store(monkeypatch):
    from io import BytesIO
    from types import SimpleNamespace

    monkeypatch.setenv("BG_ENVIRONMENT", "dev")
    monkeypatch.setenv("BG_SUPERPLANE_ROUTE_BUCKET", "adp-terraform-state-123456789012")
    monkeypatch.setattr(proxy, "_cache", (0, {}))
    store = {"version": 2, "enabled": True, "namespace": "superplane", "release_id": "a" * 64, "installation_id": "b" * 24, "revision": "c" * 32}
    calls = []

    def get_object(**kwargs):
        calls.append(kwargs)
        return {"Body": BytesIO(json.dumps(store).encode())}

    def factory(service, **kwargs):
        assert service == "s3"
        return SimpleNamespace(get_object=get_object)

    monkeypatch.setattr(proxy.boto3, "client", factory)
    return store, calls


def test_registration_reads_only_the_conditional_route_object(route_store):
    store, calls = route_store
    assert proxy.registration() == store
    assert calls == [{"Bucket": "adp-terraform-state-123456789012", "Key": "domain-routes/dev/superplane/public-route.json"}]
    assert proxy.registration() == store
    assert len(calls) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", 1),
        ("enabled", False),
        ("namespace", "adp"),
        ("namespace", "foreign/route"),
        ("installation_id", ""),
        ("release_id", "unknown"),
        ("revision", ""),
    ],
)
def test_invalid_route_registration_is_off(route_store, field, value):
    store, _ = route_store
    store[field] = value
    assert proxy.registration() == {}


def test_route_read_failure_is_off_and_never_falls_back_to_ssm(route_store, monkeypatch):
    def unavailable(*args, **kwargs):
        raise RuntimeError("private provider error")

    monkeypatch.setattr(proxy.boto3, "client", unavailable)
    assert proxy.registration() == {}


def test_unconfigured_route_store_does_not_use_ambient_aws(monkeypatch):
    monkeypatch.setenv("BG_ENVIRONMENT", "dev")
    monkeypatch.delenv("BG_SUPERPLANE_ROUTE_BUCKET", raising=False)
    monkeypatch.delenv("BG_PLATFORM_BEDROCK_ACCOUNT_ID", raising=False)
    monkeypatch.setattr(proxy, "_cache", (0, {}))
    monkeypatch.setattr(proxy.boto3, "client", lambda *a, **kw: pytest.fail("unexpected AWS call"))
    assert proxy.registration() == {}


def test_route_bucket_uses_existing_platform_identity(monkeypatch):
    monkeypatch.delenv("BG_SUPERPLANE_ROUTE_BUCKET", raising=False)
    monkeypatch.setenv("BG_PLATFORM_BEDROCK_ACCOUNT_ID", "123456789012")
    assert proxy.route_bucket() == "adp-terraform-state-123456789012"
    assert "BG_SUPERPLANE_ROUTE_BUCKET" not in (Path(__file__).resolve().parents[2] / "k8s/configmap.yaml").read_text()


async def test_transport_capability_requires_deployed_configuration(route_store, monkeypatch):
    result = await proxy.installation_support()
    assert result["version"] == 2 and result["configured"] is True
    assert result["transport"] == "s3-conditional-domain-registration"
    assert result["features"] == ["account-vault-reference-v1"]
    monkeypatch.delenv("BG_SUPERPLANE_ROUTE_BUCKET")
    monkeypatch.delenv("BG_PLATFORM_BEDROCK_ACCOUNT_ID", raising=False)
    assert (await proxy.installation_support())["configured"] is False


def test_kubeconfig_post_forwards_only_bearer_and_preserves_domain_denial(client, monkeypatch):
    forwarded = []

    def upstream(request):
        forwarded.append(request)
        return httpx.Response(403, json={"detail": "workspace access denied"})

    original = httpx.AsyncClient
    monkeypatch.setattr(
        proxy.httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(upstream), **kwargs),
    )
    path = "/superplane/v1/workspaces/example/kubeconfig"
    assert client.post(path).status_code == 401
    assert client.get(path, headers={"Authorization": "Bearer example-token"}).status_code == 404
    assert (
        client.post(
            "/superplane/v1/internal/workspaces/example/kubeconfig",
            headers={"Authorization": "Bearer example-token"},
        ).status_code
        == 404
    )
    assert forwarded == []

    response = client.post(
        path,
        headers={
            "Authorization": "Bearer example-token",
            "X-Org-Id": "another-org",
            "X-ADP-Principal": "workspace-owner",
            "X-Workspace-Id": "other-workspace",
        },
    )
    assert response.status_code == 403
    assert response.json() == {"detail": "workspace access denied"}
    assert len(forwarded) == 1
    assert forwarded[0].method == "POST"
    assert forwarded[0].url.path == "/workspaces/example/kubeconfig"
    assert forwarded[0].headers["authorization"] == "Bearer example-token"
    assert all(header not in forwarded[0].headers for header in ("x-org-id", "x-adp-principal", "x-workspace-id"))
