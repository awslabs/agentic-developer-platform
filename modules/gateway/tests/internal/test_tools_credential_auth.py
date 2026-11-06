"""Exercise the production broker authentication dependency, not an injected route state."""

import base64
import hashlib
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from src.internal import auth_deps, tools_credential_auth
from src.internal.credential_routes import get_secrets_manager
from src.internal.credential_routes import router as credential_router
from src.shared.database import get_db

ROLE = "arn:aws:iam::123456789012:role/adp-tools-dev-gateway"
FACADE = "arn:aws:iam::123456789012:role/adp-dev-role-tools-facade"
BODY = {"service": "aws", "method": "GET", "url": "https://fixture.example.invalid/status"}


def encoded(value):
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def assertion(body=BODY, **changes):
    now = int(time.time())
    claims = {
        "caller_role_arn": FACADE,
        "principal": {"user_id": "user-alice", "tenant_id": "org-test", "team_id": "team-eng"},
        "root_human_id": "user-alice",
        "tool": "credentials___proxy_request",
        "method": "tools/call",
        "arguments_sha256": hashlib.sha256(tools_credential_auth._canonical(body)).hexdigest(),
        "receipt_id": "receipt-1",
        "issued_at": now - 1,
        "expires_at": now + 20,
        **changes,
    }
    return encoded(tools_credential_auth._canonical({"claims": claims, "signature": encoded(b"signature")}))


class Session:
    def __init__(self, rows):
        self.rows = rows

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def scalars(self, statement):
        return self.rows

    async def scalar(self, statement):
        return "user-alice"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("ADP_TOOLS_GATEWAY_ROLE_ARN", ROLE)
    monkeypatch.setenv("ADP_TOOLS_FACADE_ROLE_ARN", FACADE)
    monkeypatch.setenv("ADP_TOOLS_ASSERTION_KEY_ARN", "arn:aws:kms:us-east-1:123456789012:key/fixture")
    monkeypatch.setattr(auth_deps, "has_caller_identity_assertion", lambda request: True)
    monkeypatch.setattr(
        auth_deps, "verified_caller_identity", lambda request, settings: "arn:aws:sts::123456789012:assumed-role/adp-tools-dev-gateway/session"
    )
    monkeypatch.setattr(
        tools_credential_auth, "verified_caller_identity", lambda request: "arn:aws:sts::123456789012:assumed-role/adp-tools-dev-gateway/session"
    )
    kms = SimpleNamespace(verify=lambda **kwargs: {"SignatureValid": True})
    monkeypatch.setattr(tools_credential_auth.boto3, "client", lambda service: kms)
    monkeypatch.setattr("src.agentauth.routes.get_agent_runtime", lambda: SimpleNamespace(store=SimpleNamespace(table="test", client=None)))
    policy = {"status": "active", "allowed_tools": ["credentials.proxy_request"], "consented_tools": ["credentials.proxy_request"]}
    monkeypatch.setattr(tools_credential_auth.ToolsPolicyStore, "get", lambda self, **kwargs: policy)
    monkeypatch.setattr(tools_credential_auth, "effective_entitlement", AsyncMock(return_value=(True, None)))
    rows = [SimpleNamespace(id="cred-1", agent_consent=True)]
    monkeypatch.setattr(tools_credential_auth, "get_session_factory", lambda: lambda: Session(rows))

    app = FastAPI()

    @app.post("/internal/v1/proxy-request")
    async def broker(request: Request, _: None = Depends(auth_deps.verify_internal_or_irsa)):
        return {"consented": sorted(request.state.agent_tools_credential_authority.credential_ids)}

    return TestClient(app), policy, rows, kms


def send(client, body=BODY, signed=None):
    return client.post(
        "/internal/v1/proxy-request",
        json=body,
        headers={
            "X-Caller-Identity": "arn:aws:sts::123456789012:assumed-role/adp-tools-dev-gateway/session",
            "x-adp-assertion": signed or assertion(),
        },
    )


def test_verified_gateway_is_bound_to_request_and_current_consent(client):
    transport, policy, rows, _ = client
    assert send(transport).json() == {"consented": ["cred-1"]}
    forged = {**BODY, "agent_id": "another-agent"}
    assert send(transport, forged, signed=assertion(body=forged)).status_code == 403
    assert send(transport, signed=assertion(tool="credentials___raw_read")).status_code == 403
    rows[0].agent_consent = False
    assert send(transport).json() == {"consented": []}
    policy["consented_tools"].clear()
    policy["consent_mode"] = "explicit"  # #8387: an owner's revocation is explicit; default mode would re-grant
    denied = send(transport)
    assert denied.status_code == 403
    assert denied.json()["detail"]["error"] == "not_consented"


@pytest.mark.parametrize("operation", ["list", "proxy_request"])
@pytest.mark.parametrize("state", ["absent", "revoked"])
def test_list_and_proxy_reject_missing_tool_consent_before_handler(client, operation, state):
    _, policy, _, _ = client
    permission = "credentials." + operation
    policy["allowed_tools"] = [permission]
    policy["consented_tools"] = [permission] if state == "revoked" else []
    # #8387: both an explicit empty list and a revocation are explicit decisions; the tenant
    # posture may not re-grant them.
    policy["consent_mode"] = "explicit"
    if state == "revoked":
        policy["consented_tools"].clear()
    provider_calls = []
    app = FastAPI()

    @app.get("/internal/v1/user-credentials")
    @app.post("/internal/v1/proxy-request")
    async def provider_action(request: Request, _: None = Depends(auth_deps.verify_internal_or_irsa)):
        provider_calls.append(request.url.path)
        return {"status": "unexpected"}

    arguments = {"service": "aws"} if operation == "list" else BODY
    headers = {
        "X-Caller-Identity": "arn:aws:sts::123456789012:assumed-role/adp-tools-dev-gateway/session",
        "x-adp-assertion": assertion(body=arguments, tool="credentials___" + operation),
    }
    transport = TestClient(app)
    if operation == "list":
        denied = transport.get("/internal/v1/user-credentials", params=arguments, headers=headers)
    else:
        denied = transport.post("/internal/v1/proxy-request", json=arguments, headers=headers)
    assert denied.status_code == 403
    assert denied.json()["detail"] == {"error": "not_consented"}
    assert provider_calls == []


def test_signature_and_edge_identity_must_both_be_verified(client, monkeypatch):
    transport, _, _, kms = client
    monkeypatch.setattr(kms, "verify", lambda **kwargs: {"SignatureValid": False})
    assert send(transport).status_code == 403
    monkeypatch.setattr(tools_credential_auth, "verified_caller_identity", lambda request: "arn:aws:sts::123456789012:assumed-role/other/session")
    assert send(transport).status_code == 403


def test_gateway_raw_read_returns_404_with_or_without_tool_assertion(client, monkeypatch):
    monkeypatch.setattr(
        auth_deps,
        "extract_iam_identity_from_headers",
        lambda request: pytest.fail("tools gateway must not enter worker authentication"),
    )
    app = FastAPI()
    app.include_router(credential_router)
    app.dependency_overrides[get_db] = lambda: object()
    app.dependency_overrides[get_secrets_manager] = lambda: object()
    transport = TestClient(app)
    body = {**BODY, "service": "aws", "label": "prod"}
    headers = {
        "X-Caller-Identity": "arn:aws:sts::123456789012:assumed-role/adp-tools-dev-gateway/session",
        "x-adp-assertion": assertion(body=body, tool="credentials___raw_read"),
    }
    signed = transport.post("/internal/v1/credential-raw-read", json=body, headers=headers)
    assert signed.status_code == 404
    assert signed.json()["detail"] == "not found"

    unsigned = transport.post(
        "/internal/v1/credential-raw-read",
        json=body,
        headers={"X-Caller-Identity": headers["X-Caller-Identity"]},
    )
    assert unsigned.status_code == 404
    assert unsigned.json()["detail"] == "not found"


def test_broker_second_fence_rechecks_current_team_entitlement(client, monkeypatch):
    transport, policy, _, _ = client
    assert send(transport).status_code == 200
    grant = AsyncMock(return_value=(False, None))
    monkeypatch.setattr(tools_credential_auth, "effective_entitlement", grant)
    denied = send(transport)
    assert denied.status_code == 403
    assert denied.json()["detail"]["error"] == "not_entitled"
    grant.assert_awaited_with(grant.call_args.args[0], tenant_id="org-test", team_id="team-eng", tool_name="credentials.proxy_request")
    assert policy["consented_tools"] == ["credentials.proxy_request"]
    grant.side_effect = SQLAlchemyError("database unavailable")
    assert send(transport).status_code == 503


@pytest.mark.parametrize("selector", ["user_id", "agent_id", "task_id", "tenant_id", "team_id", "run_id", "invocation_id"])
def test_even_signed_caller_identity_selectors_are_denied(client, selector, monkeypatch):
    transport, _, _, kms = client
    monkeypatch.setattr(kms, "verify", lambda **kwargs: pytest.fail("identity selector reached signature or provider"))
    body = {**BODY, selector: "forged"}
    assert send(transport, body, signed=assertion(body=body)).status_code == 403


def test_signed_run_claims_cannot_create_task_authority(client):
    transport, _, _, _ = client
    assert send(transport, signed=assertion(run_id="forged-run")).status_code == 403


@pytest.mark.parametrize(
    "operation,path",
    [
        ("assume_role", "/internal/v1/credential-assume-role"),
        ("materialize", "/internal/v1/credential-materialize"),
    ],
)
def test_private_operations_refused_before_generic_receipt_or_provider_delivery(client, operation, path):
    app = FastAPI()

    @app.post(path)
    async def forbidden(request: Request, _: None = Depends(auth_deps.verify_internal_or_irsa)):
        pytest.fail("private operation reached provider")

    body = {"service": "aws"}
    response = TestClient(app).post(
        path,
        json=body,
        headers={
            "X-Caller-Identity": "arn:aws:sts::123456789012:assumed-role/adp-tools-dev-gateway/session",
            "x-adp-assertion": assertion(body=body, tool=f"credentials___{operation}"),
        },
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "private credential delivery unavailable"


def test_absent_current_human_membership_fails_closed(client, monkeypatch):
    monkeypatch.setattr(Session, "scalar", AsyncMock(return_value=None))
    assert send(client[0]).status_code == 403
