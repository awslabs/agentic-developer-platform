"""Owner-bound tool admission uses the original signed invocation and durable release."""

import base64
import hashlib
import hmac
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.agentauth import routes as agent_routes
from src.auth import atlassian_mcp_routes
from src.internal import tool_adapter_authority, tools_knowledge
from src.internal.auth_deps import verify_internal_or_irsa
from src.internal.github_oauth import credential_digest
from src.shared.database import get_db
from src.shared.models.base import Base
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Department, Organization, Team, TeamMembership, User
from src.shared.models.tool_entitlement import ToolEntitlement
from src.shared.models.vault import OAuthConnectionBinding, OAuthToolReceipt, ToolAdapterActivation, UserCredential
from tests.internal.test_tools_dynamic_catalogue import deployed

RELEASE = {
    "contract_version": "1.0",
    "source_revision": "a" * 40,
    "image_digest": "sha256:" + "b" * 64,
    "group": "atlassian-foundation",
    "group_generation": 1,
    "backend_generation": 1,
}
KEY = "arn:aws:kms:us-east-1:123456789012:key/11111111-1111-1111-1111-111111111111"
ADAPTER_ROLE = "arn:aws:iam::123456789012:role/adp-adapter"
FACADE_ROLE = "arn:aws:iam::123456789012:role/adp-facade"
SCOPES = ["read:jira:agent-interface", "search:jira:agent-interface", "offline_access"]


def test_jira_write_requires_original_operation_and_release(monkeypatch):
    from fastapi import HTTPException

    connection = {"id": "binding-one", "revision": "revision-one", "site_id": "site-one"}
    tool = "atlassian___create_jira_issue"
    claims = {
        "tool": tool,
        "principal": {"tenant_id": "org-one", "user_id": "owner-one"},
        "receipt_id": "receipt-one",
        "arguments_sha256": "a" * 64,
        "connection": connection,
    }
    request_id = "00000000-0000-4000-8000-000000000099"
    approval = {"tool": tool, "request_id": request_id, "payload_sha256": "a" * 64, "policy_revision": 1, "consent_source": "explicit"}
    claims["atlassian_approval"] = approval
    recorded = {
        "tool": tool,
        "operation_id": f"OP#org-one#owner-one#{tool}#{request_id}",
        "execution_id": "00000000-0000-4000-8000-000000000098",
        "request_id": request_id,
        "receipt_id": "receipt-one",
        "payload_sha256": "a" * 64,
        "connection": connection,
        "release": RELEASE,
        "dispatch_intent": "dispatched",
        "approval": approval,
    }
    monkeypatch.setenv("ADP_TOOLS_ATLASSIAN_WRITES_ENABLED", "true")
    monkeypatch.setenv("ADP_TOOLS_ATLASSIAN_WRITES_SHA256", "c" * 64)
    tool_adapter_authority.require_recorded_write({**claims, "atlassian_operation": recorded}, RELEASE)
    for changed in (
        {**recorded, "payload_sha256": "b" * 64},
        {**recorded, "release": {**RELEASE, "backend_generation": 2}},
        {**recorded, "operation_id": "OP#other"},
        {**recorded, "dispatch_intent": "claimed"},
        {**recorded, "receipt_id": "other"},
        {**recorded, "approval": {**approval, "policy_revision": 2}},
    ):
        with pytest.raises(HTTPException) as refused:
            tool_adapter_authority.require_recorded_write({**claims, "atlassian_operation": changed}, RELEASE)
        assert refused.value.status_code == 403
    monkeypatch.delenv("ADP_TOOLS_ATLASSIAN_WRITES_ENABLED")
    with pytest.raises(HTTPException):
        tool_adapter_authority.require_recorded_write({**claims, "atlassian_operation": recorded}, RELEASE)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def encode(value):
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


@pytest.fixture
async def authority(monkeypatch):
    monkeypatch.setattr(tool_adapter_authority, "verified_connection", AsyncMock(return_value=True))
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        db.add_all(
            [
                Organization(
                    id="org-acme", name="Acme", aws_accounts=[], role_mappings={}, settings={}, github_installation_ids=[], cognito_client_ids=[]
                ),
                Department(id="dept-eng", org_id="org-acme", name="Engineering"),
                Team(id="team-eng", org_id="org-acme", department_id="dept-eng", name="Engineering"),
                User(id="alice", org_id="org-acme", team_id="team-eng", email="alice@example.test"),
                User(id="bob", org_id="org-acme", team_id="team-eng", email="bob@example.test"),
                TenantMembership(id="tenant-alice", tenant_id="org-acme", user_id="alice", is_active=True),
                TeamMembership(id="team-alice", org_id="org-acme", user_id="alice", team_id="team-eng", is_primary=True),
                ToolEntitlement(org_id="org-acme", team_id="", tool_name="atlassian.get_jira_issue", enabled=True),
                ToolAdapterActivation(group=RELEASE["group"], status="active", release=dict(RELEASE)),
            ]
        )
        credential = UserCredential(
            id="credential-alice",
            org_id="org-acme",
            user_id="alice",
            service="atlassian-mcp",
            credential_type="oauth_token",
            provider="agentcore-identity",
            label="Atlassian MCP OAuth",
            secret_arn="agentcore-identity:adp-tools-dev-atlassian-mcp-oauth-user:atlassian-mcp",
            scopes={"oauth_scopes": SCOPES},
            strict=True,
            agent_consent=True,
        )
        db.add(credential)
        await db.flush()
        binding = OAuthConnectionBinding(
            credential_id=credential.id,
            org_id="org-acme",
            user_id="alice",
            provider="atlassian-mcp",
            revision="revision-1",
            credential_digest=credential_digest(credential),
            provider_account_id="provider-alice",
            observed_scopes=SCOPES,
            observed_resources=[{"site_id": "site-1", "scopes": SCOPES}],
            selected_site_id="site-1",
            verified_at=datetime.now(UTC),
        )
        db.add(binding)
        await db.commit()
        app = FastAPI()
        app.include_router(tool_adapter_authority.router)

        async def get_session():
            yield db

        async def iam(request: Request):
            request.state.token_context = SimpleNamespace(
                auth_source="iam",
                user_id="adapter",
                scope="internal",
                org_id="__adapter__",
                credential_scopes={"tools:adapter:authorize", "tools:adapter:dispatch", "internal:cross-tenant"},
            )

        app.dependency_overrides[get_db] = get_session
        app.dependency_overrides[verify_internal_or_irsa] = iam
        monkeypatch.setattr(tool_adapter_authority, "verified_caller_identity", lambda request: ADAPTER_ROLE)
        monkeypatch.setattr(
            tool_adapter_authority,
            "get_settings",
            lambda: SimpleNamespace(
                atlassian_identity_compatibility_verified=True,
                oauth_workload_name="adp-tools-dev-oauth-user",
            ),
        )
        monkeypatch.setattr(agent_routes, "get_agent_runtime", lambda: SimpleNamespace(store=SimpleNamespace(table="fixture", client=MagicMock())))
        monkeypatch.setattr(
            tools_knowledge.ToolsPolicyStore,
            "get",
            lambda self, **kwargs: {
                "status": "active",
                "consent_mode": "explicit",
                "allowed_tools": ["atlassian.get_jira_issue"],
                "consented_tools": ["atlassian.get_jira_issue"],
                "tool_max_cost_usd": {"atlassian.get_jira_issue": "1.00"},
                "revision": 1,
            },
        )
        monkeypatch.setenv("ADP_TOOL_ADAPTER_OBSERVE_URL", "https://adapter.example.test/internal/v1/tool-adapters/observe")
        monkeypatch.setenv("ADP_TOOL_ADAPTER_ASSERTION_KEY_ARN", KEY)
        monkeypatch.setenv("ADP_TOOL_ADAPTER_CORE_ROLE_ARN", "arn:aws:iam::123456789012:role/adp-core")
        monkeypatch.setenv("ADP_TOOL_ADAPTER_ADAPTER_ROLE_ARN", ADAPTER_ROLE)
        monkeypatch.setenv("ADP_TOOL_ADAPTER_RELEASE", json.dumps(RELEASE))
        monkeypatch.setenv("ADP_TOOLS_FACADE_ROLE_ARN", FACADE_ROLE)
        monkeypatch.setenv("ADP_TOOLS_ASSERTION_KEY_ARN", KEY)
        kms = MagicMock()
        kms.verify.side_effect = lambda **kwargs: {
            "SignatureValid": kwargs["KeyId"] == KEY
            and hmac.compare_digest(kwargs["Signature"], hmac.digest(b"fixture-key", kwargs["Message"], "sha256"))
        }
        monkeypatch.setattr(tool_adapter_authority.boto3, "client", lambda name, **kwargs: kms if name == "kms" else MagicMock())
        yield TestClient(app, raise_server_exceptions=True), db, credential, binding, kms
    await engine.dispose()


def request_body(*, claims_update=None, release=None, connection=None, operation="get_jira_issue"):
    now = int(time.time())
    release = dict(release or RELEASE)
    claims = {
        "audience": "tool-adapters:authorize",
        "purpose": "tool_call",
        "connector": "atlassian",
        "caller_role_arn": FACADE_ROLE,
        "method": "tools/call",
        "tool": "atlassian___" + operation,
        "rpc_id": "operation-1",
        "receipt_id": "receipt-1",
        "arguments_sha256": hashlib.sha256(canonical({"issue": "ADP-1"})).hexdigest(),
        "adapter_release": release,
        "principal": {"user_id": "alice", "tenant_id": "org-acme", "team_id": "team-eng"},
        "root_human_id": "alice",
        "connection": connection or {"id": "credential-alice", "revision": "revision-1", "site_id": "site-1"},
        "issued_at": now,
        "expires_at": now + 15,
    }
    claims.update(claims_update or {})
    signature = hmac.digest(b"fixture-key", canonical(claims), "sha256")
    return {
        "schema_version": "1.0",
        "purpose": "tool_call",
        "connector": "atlassian",
        "operation": operation,
        "binding": {"operation_id": "operation-1", "receipt_id": "receipt-1", "payload_sha256": claims["arguments_sha256"], "release": release},
        "signed_assertion": encode(canonical({"claims": claims, "signature": encode(signature)})),
        "deadline_at_ms": int(time.time() * 1000) + 5000,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("write_consent", [False, True])
async def test_tool_identity_lookup_preserves_consent_scopes_and_site_authorization(authority, write_consent):
    client, db, credential, binding, _ = authority
    consent_scopes = list(atlassian_mcp_routes.WRITE_SCOPES if write_consent else atlassian_mcp_routes.SCOPES)
    credential.scopes = {"oauth_scopes": consent_scopes}
    binding.credential_digest = credential_digest(credential)
    binding.observed_scopes = ["read:me", "read:account", "read:jira:agent-interface", "search:jira:agent-interface"]
    binding.observed_resources = [{"site_id": "site-1", "scopes": ["read:jira:agent-interface", "search:jira:agent-interface"]}]
    await db.commit()

    admitted = client.post("/internal/v1/tool-adapters/authorize", json=request_body())
    assert admitted.status_code == 200, admitted.text
    # Identity indexes the saved OAuth grant by its original requested scopes.
    # The narrower scopes returned for this Jira site identify a different grant.
    assert admitted.json()["context"]["scopes"] == consent_scopes
    assert admitted.json()["context"]["site_id"] == "site-1"

    binding.observed_resources = [{"site_id": "site-1", "scopes": ["search:jira:agent-interface"]}]
    await db.commit()
    refused = client.post("/internal/v1/tool-adapters/authorize", json=request_body())
    assert refused.status_code == 403
    assert refused.json()["detail"]["error"] == "connection_changed"


def write_request_body(operation):
    body = request_body(operation=operation)
    envelope = json.loads(base64.urlsafe_b64decode(body["signed_assertion"] + "=="))
    claims = envelope["claims"]
    request_id = "00000000-0000-4000-8000-000000000099"
    approval = {
        "tool": claims["tool"],
        "request_id": request_id,
        "payload_sha256": claims["arguments_sha256"],
        "policy_revision": 1,
        "consent_source": "explicit",
    }
    claims["atlassian_approval"] = approval
    claims["atlassian_operation"] = {
        "tool": claims["tool"],
        "operation_id": f"OP#org-acme#alice#{claims['tool']}#{request_id}",
        "execution_id": "00000000-0000-4000-8000-000000000098",
        "request_id": request_id,
        "receipt_id": claims["receipt_id"],
        "payload_sha256": claims["arguments_sha256"],
        "connection": claims["connection"],
        "release": RELEASE,
        "dispatch_intent": "dispatched",
        "approval": approval,
    }
    body["signed_assertion"] = encode(
        canonical(
            {
                "claims": claims,
                "signature": encode(hmac.digest(b"fixture-key", canonical(claims), "sha256")),
            }
        )
    )
    return body


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["create_jira_issue", "update_jira_issue", "comment_jira_issue", "transition_jira_issue"])
@pytest.mark.parametrize(
    "change", ["approved", "read_only", "revoked", "wrong_site", "foreign", "denied_policy", "changed_policy", "changed_approval", "changed_payload"]
)
async def test_jira_write_denials_leave_no_unauthorized_admission(authority, monkeypatch, operation, change):
    client, db, credential, binding, _ = authority
    permission = "atlassian." + operation
    db.add(ToolEntitlement(org_id="org-acme", team_id="", tool_name=permission, enabled=True))
    credential.scopes = {"oauth_scopes": [*SCOPES, "write:jira:agent-interface"]}
    binding.observed_scopes = [*SCOPES, "write:jira:agent-interface"]
    binding.observed_resources = [{"site_id": "site-1", "scopes": [*SCOPES, "write:jira:agent-interface"]}]
    binding.credential_digest = credential_digest(credential)
    await db.commit()
    monkeypatch.setenv("ADP_TOOLS_ATLASSIAN_WRITES_ENABLED", "true")
    monkeypatch.setenv("ADP_TOOLS_ATLASSIAN_WRITES_SHA256", "c" * 64)
    policy = {
        "status": "active",
        "consent_mode": "explicit",
        "allowed_tools": [permission],
        "consented_tools": [permission],
        "tool_max_cost_usd": {permission: "1.00"},
        "revision": 1,
    }
    monkeypatch.setattr(tools_knowledge.ToolsPolicyStore, "get", lambda self, **kwargs: policy)
    body = write_request_body(operation)
    if change == "read_only":
        credential.scopes = {"oauth_scopes": list(SCOPES)}
        binding.credential_digest = credential_digest(credential)
    elif change == "revoked":
        credential.agent_consent = False
        binding.credential_digest = credential_digest(credential)
    elif change == "wrong_site":
        binding.observed_resources = [{"site_id": "site-1", "scopes": list(SCOPES)}]
    elif change == "foreign":
        body = write_request_body(operation)
        envelope = json.loads(base64.urlsafe_b64decode(body["signed_assertion"] + "=="))
        claims = envelope["claims"]
        claims["principal"]["user_id"] = "bob"
        claims["root_human_id"] = "bob"
        body["signed_assertion"] = encode(
            canonical(
                {
                    "claims": claims,
                    "signature": encode(hmac.digest(b"fixture-key", canonical(claims), "sha256")),
                }
            )
        )
    elif change == "denied_policy":
        policy["consented_tools"] = []
    elif change == "changed_policy":
        policy["revision"] = 2
    elif change == "changed_approval":
        envelope = json.loads(base64.urlsafe_b64decode(body["signed_assertion"] + "=="))
        claims = envelope["claims"]
        claims["atlassian_approval"]["policy_revision"] = 2
        body["signed_assertion"] = encode(
            canonical(
                {
                    "claims": claims,
                    "signature": encode(hmac.digest(b"fixture-key", canonical(claims), "sha256")),
                }
            )
        )
    elif change == "changed_payload":
        body["binding"]["payload_sha256"] = "b" * 64
    await db.commit()
    before = len((await db.scalars(select(OAuthToolReceipt))).all())
    response = client.post("/internal/v1/tool-adapters/authorize", json=body)
    assert response.status_code == (200 if change == "approved" else 403), (operation, change, response.text)
    after = len((await db.scalars(select(OAuthToolReceipt))).all())
    assert after - before == (1 if change == "approved" else 0)


@pytest.mark.asyncio
async def test_jira_write_dispatch_rechecks_original_approval_before_effect(authority, monkeypatch):
    client, db, credential, binding, _ = authority
    permission = "atlassian.create_jira_issue"
    db.add(ToolEntitlement(org_id="org-acme", team_id="", tool_name=permission, enabled=True))
    credential.scopes = {"oauth_scopes": [*SCOPES, "write:jira:agent-interface"]}
    binding.observed_scopes = [*SCOPES, "write:jira:agent-interface"]
    binding.observed_resources = [{"site_id": "site-1", "scopes": [*SCOPES, "write:jira:agent-interface"]}]
    binding.credential_digest = credential_digest(credential)
    await db.commit()
    monkeypatch.setenv("ADP_TOOLS_ATLASSIAN_WRITES_ENABLED", "true")
    monkeypatch.setenv("ADP_TOOLS_ATLASSIAN_WRITES_SHA256", "c" * 64)
    policy = {
        "status": "active",
        "consent_mode": "explicit",
        "allowed_tools": [permission],
        "consented_tools": [permission],
        "tool_max_cost_usd": {permission: "1.00"},
        "revision": 1,
    }
    monkeypatch.setattr(tools_knowledge.ToolsPolicyStore, "get", lambda self, **kwargs: policy)
    admitted = client.post("/internal/v1/tool-adapters/authorize", json=write_request_body("create_jira_issue"))
    assert admitted.status_code == 200, admitted.text
    assert admitted.json()["context"]["scopes"] == [*SCOPES, "write:jira:agent-interface"]
    payload = dispatch_body(admitted.json())
    policy["revision"] = 2
    denied = client.post("/internal/v1/tool-adapters/dispatch", json=payload)
    assert denied.status_code == 403
    receipt = await db.get(OAuthToolReceipt, admitted.json()["admission_id"])
    assert receipt.consumed_at is None
    policy["revision"] = 1
    allowed = client.post("/internal/v1/tool-adapters/dispatch", json=payload)
    assert allowed.status_code == 200, allowed.text
    assert receipt.consumed_at is not None
    assert client.post("/internal/v1/tool-adapters/dispatch", json=payload).status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("compatibility_verified", [False, True])
async def test_signed_observation_admission_denies_foreign_owner_and_superseded_attempt(authority, monkeypatch, compatibility_verified):
    from src.shared.models.vault import OAuthConnectionAttempt

    client, db, credential, binding, kms = authority
    settings = tool_adapter_authority.get_settings()
    settings.atlassian_identity_compatibility_verified = compatibility_verified
    monkeypatch.setattr(tool_adapter_authority, "get_settings", lambda: settings)
    original_iam = client.app.dependency_overrides[verify_internal_or_irsa]

    async def observation_iam(request: Request):
        await original_iam(request)
        request.state.token_context.credential_scopes.add("tools:adapter:observe")

    client.app.dependency_overrides[verify_internal_or_irsa] = observation_iam
    attempt = OAuthConnectionAttempt(
        state_digest="f" * 64,
        org_id="org-acme",
        user_id="alice",
        provider="atlassian-mcp",
        session_uri="opaque-session",
        requested_scopes=SCOPES,
        connection_snapshot={
            "credential_sha256": credential_digest(credential),
            "binding_revision": binding.revision,
            "config_revision": "c" * 64,
        },
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
        identity_completed_at=datetime.now(UTC),
    )
    db.add(attempt)
    await db.commit()

    def signed_request(owner):
        deadline = int(time.time() * 1000) + 5000
        binding_claim = {
            "operation_id": attempt.state_digest,
            "receipt_id": attempt.state_digest,
            "payload_sha256": hashlib.sha256(
                canonical(
                    {
                        "owner": owner,
                        "tenant": "org-acme",
                        "attempt": attempt.state_digest,
                    }
                )
            ).hexdigest(),
            "release": RELEASE,
        }
        claims = {
            "audience": "tool-adapters:observe",
            "caller_role_arn": "arn:aws:iam::123456789012:role/adp-core",
            "purpose": "connection_observe",
            "user_id": owner,
            "tenant_id": "org-acme",
            "attempt_id": attempt.state_digest,
            "config_revision": "c" * 64,
            "binding": binding_claim,
            "deadline_at_ms": deadline,
        }
        return {
            "schema_version": "1.0",
            "purpose": "connection_observe",
            "connector": "atlassian",
            "operation": "observe",
            "binding": binding_claim,
            "signed_assertion": encode(
                canonical(
                    {
                        "claims": claims,
                        "signature": encode(hmac.digest(b"fixture-key", canonical(claims), "sha256")),
                    }
                )
            ),
            "deadline_at_ms": deadline,
        }

    allowed = client.post("/internal/v1/tool-adapters/authorize", json=signed_request("alice"))
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["identity"]["canonical_user_id"] == "alice"
    assert allowed.json()["context"]["connection_id"] is None
    foreign = client.post("/internal/v1/tool-adapters/authorize", json=signed_request("bob"))
    assert foreign.status_code == 403, foreign.text
    attempt.superseded_at = datetime.now(UTC)
    await db.commit()
    stale = client.post("/internal/v1/tool-adapters/authorize", json=signed_request("alice"))
    assert stale.status_code == 403, stale.text
    assert kms.verify.call_count == 3
    if not compatibility_verified:
        monkeypatch.setattr(tool_adapter_authority, "verified_connection", AsyncMock(return_value=False))
        assert client.post("/internal/v1/tool-adapters/authorize", json=request_body()).status_code == 403


@pytest.mark.asyncio
async def test_atlassian_tool_admission_resolves_signed_owner_policy_site_and_identity(authority, monkeypatch):
    client, _, _, _, kms = authority
    root = Path(__file__).resolve().parents[4]
    monkeypatch.syspath_prepend(str(root / "modules/tools"))
    from adp_tools.adapter_contracts import Admission, AuthorizeRequest

    response = client.post("/internal/v1/tool-adapters/authorize", json=AuthorizeRequest.model_validate(request_body()).model_dump())
    assert response.status_code == 200, response.text
    admission = response.json()
    Admission.model_validate(admission)
    assert admission["purpose"] == "tool_call" and admission["context"] == {
        "tenant_id": "org-acme",
        "canonical_user_id": "alice",
        "connection_id": "credential-alice",
        "binding_revision": "revision-1",
        "site_id": "site-1",
        "scopes": SCOPES,
    }
    assert admission["identity"]["canonical_user_id"] == "alice"
    assert "token" not in response.text.lower() and "provider-alice" not in response.text
    assert kms.verify.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    ["purpose", "role", "transport_role", "owner", "site", "receipt", "generation", "new_generation", "reconnect", "fenced", "scope", "policy"],
)
async def test_atlassian_tool_admission_denies_stale_or_substituted_authority(authority, change, monkeypatch):
    client, db, credential, binding, kms = authority
    body = request_body()
    if change == "purpose":
        body = request_body(claims_update={"purpose": "connection_observe"})
    elif change == "role":
        body = request_body(claims_update={"caller_role_arn": ADAPTER_ROLE})
    elif change == "transport_role":
        monkeypatch.setattr(tool_adapter_authority, "verified_caller_identity", lambda request: FACADE_ROLE)
    elif change == "owner":
        body = request_body(claims_update={"principal": {"user_id": "bob", "tenant_id": "org-acme", "team_id": "team-eng"}, "root_human_id": "bob"})
    elif change == "site":
        body = request_body(connection={"id": "credential-alice", "revision": "revision-1", "site_id": "site-2"})
    elif change == "receipt":
        body["binding"]["receipt_id"] = "another-receipt"
    elif change == "generation":
        changed_release = {**RELEASE, "group_generation": 2}
        body = request_body(release=changed_release)
    elif change == "new_generation":
        activation = await db.get(ToolAdapterActivation, RELEASE["group"])
        activation.release = {**RELEASE, "group_generation": 2}
        await db.commit()
    elif change == "reconnect":
        binding.revision = "revision-2"
        await db.commit()
    elif change == "fenced":
        activation = await db.get(ToolAdapterActivation, RELEASE["group"])
        activation.status = "fenced"
        await db.commit()
    elif change == "scope":
        binding.observed_resources = [{"site_id": "site-1", "scopes": []}]
        await db.commit()
    elif change == "policy":
        monkeypatch.setattr(
            tools_knowledge.ToolsPolicyStore,
            "get",
            lambda self, **kwargs: {
                "status": "active",
                "consent_mode": "explicit",
                "allowed_tools": [],
                "consented_tools": [],
                "tool_max_cost_usd": {},
                "revision": 2,
            },
        )
    response = client.post("/internal/v1/tool-adapters/authorize", json=body)
    assert response.status_code == 403, (change, response.text)
    assert "accessToken" not in response.text and "provider-alice" not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["absent_group", "revoked_member", "invalid_contract", "expired", "invalid_shape", "missing_receipt", "bad_digest"]
)
async def test_atlassian_tool_admission_fails_closed_before_dispatch(authority, change):
    client, db, _, _, kms = authority
    body = request_body()
    if change == "absent_group":
        await db.delete(await db.get(ToolAdapterActivation, RELEASE["group"]))
        await db.commit()
    elif change == "revoked_member":
        member = await db.get(TenantMembership, "tenant-alice")
        member.is_active = False
        await db.commit()
    elif change == "invalid_contract":
        body["binding"]["release"]["contract_version"] = "2.0"
    elif change == "expired":
        body["deadline_at_ms"] = 1
    elif change == "invalid_shape":
        body["binding"]["extra"] = "untrusted"
    elif change == "missing_receipt":
        body = request_body(claims_update={"receipt_id": ""})
        body["binding"]["receipt_id"] = ""
    elif change == "bad_digest":
        body = request_body(claims_update={"arguments_sha256": "not-a-sha256"})
    response = client.post("/internal/v1/tool-adapters/authorize", json=body)
    assert response.status_code == 403, (change, response.text)
    assert "credential-alice" not in response.text


def dispatch_body(admission):
    return {
        "schema_version": "1.0",
        "admission_id": admission["admission_id"],
        "binding": admission["binding"],
        "binding_revision": admission["context"]["binding_revision"],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        None,
        "catalogue_cursor_loop",
        "catalogue_missing_final",
        "catalogue_deadline",
        "provider_unauthorized",
        "provider_forbidden",
        "not_found",
        "rate_limited",
        "rate_limited_recovered",
        "provider_unavailable",
        "provider_unavailable_recovered",
        "timeout",
        "publication_failure",
    ],
)
async def test_gateway_lists_and_calls_all_pinned_multi_page_reads(authority, request, monkeypatch, fault):
    import httpx

    from src.internal import tool_adapter_authority as core
    from src.internal import tools_catalogue
    from src.internal import tools_dynamic_catalogue as dynamic

    catalogue_state = request.getfixturevalue(deployed.__name__)
    root = Path(__file__).resolve().parents[4]
    monkeypatch.syspath_prepend(str(root / "modules/tools"))
    monkeypatch.syspath_prepend(str(root / "modules/tools/facade"))
    monkeypatch.syspath_prepend(str(root / "modules/tools/adapters"))
    monkeypatch.syspath_prepend(str(root / "modules/tools/interceptor"))
    from tool_adapters import atlassian, atlassian_route, dispatch
    from tool_adapters.atlassian import READ_TOOLS, read_tool
    from tool_adapters.atlassian_observe import ProviderResponseError
    from tool_adapters.config import AdapterConfiguration
    from tool_adapters.server import create_app
    from tools_facade import server as facade
    from tools_interceptor.handler import handler

    client, db, credential, binding, kms = authority
    from tests.internal.test_tools_dynamic_catalogue import GATEWAY_ID, ROLE, TARGET_ID

    endpoint = "https://fixture.execute-api.us-east-1.amazonaws.com/dev/internal/v1/tools/atlassian/mcp"
    catalogue_state.target["name"] = "atlassian"
    catalogue_state.target["targetConfiguration"]["mcp"]["mcpServer"]["endpoint"] = endpoint
    original_paginator = catalogue_state.control.get_paginator
    monkeypatch.setattr(
        catalogue_state.control,
        "get_paginator",
        lambda operation: SimpleNamespace(paginate=lambda **kwargs: [{"items": [{"name": "atlassian", "targetId": TARGET_ID, "status": "READY"}]}])
        if operation == "list_gateway_targets"
        else original_paginator(operation),
    )
    monkeypatch.setattr(
        dynamic.boto3,
        "client",
        lambda name, **kwargs: kms if name == "kms" else catalogue_state.store if name == "dynamodb" else catalogue_state.control,
    )
    monkeypatch.setattr(dynamic.time, "time", lambda: time.time_ns() / 1_000_000_000)
    for name, value in {
        "ADP_TOOLS_ATLASSIAN_MEDIATOR_ENABLED": "true",
        "ADP_TOOLS_ATLASSIAN_SOURCE_REVISION": RELEASE["source_revision"],
        "ADP_TOOLS_ATLASSIAN_MEDIATOR_URL": endpoint,
        "ADP_TOOLS_ATLASSIAN_GATEWAY_ID": GATEWAY_ID,
        "ADP_TOOLS_ATLASSIAN_TARGET_ID": TARGET_ID,
        "ADP_TOOLS_AGENTCORE_URL": "https://fixture.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp",
    }.items():
        monkeypatch.setenv(name, value)
    scopes = [
        "read:jira:agent-interface",
        "search:jira:agent-interface",
        "read:confluence:agent-interface",
        "search:confluence:agent-interface",
        "offline_access",
    ]
    credential.scopes = {"oauth_scopes": scopes}
    binding.credential_digest = credential_digest(credential)
    binding.observed_scopes = scopes
    binding.observed_resources = [{"site_id": site_id, "scopes": scopes} for site_id in ("site-1", "site-2")]
    for alias in READ_TOOLS:
        if alias != "get_jira_issue":
            db.add(ToolEntitlement(org_id="org-acme", team_id="", tool_name="atlassian." + alias, enabled=True))
    await db.commit()
    monkeypatch.setattr(
        tools_knowledge.ToolsPolicyStore,
        "get",
        lambda self, **kwargs: {
            "status": "active",
            "consent_mode": "explicit",
            "allowed_tools": ["atlassian." + alias for alias in READ_TOOLS],
            "consented_tools": ["atlassian." + alias for alias in READ_TOOLS],
            "tool_max_cost_usd": {"atlassian." + alias: "1.00" for alias in READ_TOOLS},
            "revision": 1,
        },
    )
    schema = {"type": "object", "properties": {"cloudId": {"type": "string"}, "id": {"type": "string"}}, "required": ["cloudId", "id"]}
    published = [{"name": alias, "description": alias, "inputSchema": schema} for alias in sorted(READ_TOOLS)]
    digest = hashlib.sha256(dynamic.canonical(published)).hexdigest()
    monkeypatch.setenv("ADP_TOOLS_ATLASSIAN_READS_SHA256", digest)
    original_iam = client.app.dependency_overrides[verify_internal_or_irsa]

    async def publication_iam(request: Request):
        await original_iam(request)
        request.state.token_context.credential_scopes.add("tools:adapter:observe")

    client.app.dependency_overrides[verify_internal_or_irsa] = publication_iam
    monkeypatch.setattr(facade, "signed_headers", lambda *args: {})
    monkeypatch.setattr(
        facade.boto3,
        "client",
        lambda name, **kwargs: kms if name == "kms" else catalogue_state.store if name == "dynamodb" else catalogue_state.control,
    )
    kms.sign.side_effect = lambda **kwargs: {"Signature": hmac.digest(b"fixture-key", kwargs["Message"], "sha256")}
    config = AdapterConfiguration.model_validate(
        {
            "schema_version": "1.0",
            "mode": "observation",
            "release": RELEASE,
            "observation": {
                "core_authorize_url": "https://core.example.test/internal/v1/tool-adapters/authorize",
                "core_role_arn": "arn:aws:iam::123456789012:role/fixture-core",
                "assertion_key_arn": KEY,
                "edge_provenance_sha256": hashlib.sha256(b"fixture-edge").hexdigest(),
                "identity_workload": "adp-tools-dev-atlassian-mcp-oauth-user",
                "identity_provider": "adp-tools-dev-atlassian-mcp-user",
                "config_revision": "a" * 64,
                "subject_schema_sha256": "b" * 64,
                "sites_schema_sha256": "c" * 64,
                "gateway_role_arn": ROLE,
                "read_schemas": {alias: schema for alias in READ_TOOLS},
                "read_schemas_sha256": {alias: hashlib.sha256(canonical(schema)).hexdigest() for alias in READ_TOOLS},
            },
        }
    )
    monkeypatch.setattr(atlassian_route, "_sign_dispatch", lambda url, payload: {"content-type": "application/json"})
    monkeypatch.setattr(dispatch, "_sign_dispatch", lambda url, payload: {"content-type": "application/json"})
    fixture_results = {
        "search_jira": ("JIRA-SEARCH-17", "jira-search-sentinel"),
        "get_jira_issue": ("JIRA-ISSUE-42", "jira-issue-sentinel"),
        "search_confluence": ("CONF-SEARCH-31", "confluence-search-sentinel"),
        "get_confluence_page": ("CONF-PAGE-9", "confluence-page-sentinel"),
    }
    provider_pages = []
    provider_calls = []
    site_state = {"id": "site-1"}
    fault_state = {"name": None}
    clock = {"now": 0.0, "delays": []}
    fault_attempts = {"list": 0, "call": 0}

    class ProviderTransport:
        def __init__(self, *, deadline):
            assert deadline > time.monotonic()

        def send(self, method, params, token):
            assert token == "fixture-transient-token"
            if method == "initialize":
                return {"result": {"protocolVersion": "2025-03-26"}}
            if method == "notifications/initialized":
                return None
            if method == "tools/list":
                provider_pages.append(params)
                if not params:
                    return {
                        "result": {
                            "tools": [{"name": READ_TOOLS[alias][0], "inputSchema": schema} for alias in ("search_jira", "get_jira_issue")],
                            "nextCursor": "next-page",
                        }
                    }
                assert params == {"cursor": "next-page"}
                return {
                    "result": {
                        "tools": [{"name": READ_TOOLS[alias][0], "inputSchema": schema} for alias in ("search_confluence", "get_confluence_page")]
                        + [{"name": "createJiraIssue", "inputSchema": schema}]
                    }
                }
            assert method == "tools/call" and params["arguments"] == {"cloudId": site_state["id"], "id": "fixture-id"}
            alias = next(alias for alias, (upstream, _) in READ_TOOLS.items() if upstream == params["name"])
            provider_calls.append(alias)
            result_id, sentinel = fixture_results[alias]
            return {"result": {"structuredContent": {"id": result_id, "content": sentinel, "site": site_state["id"]}}}

    class FaultTransport(atlassian.ReadMCPTransport):
        def _attempt(self, method, params, token):
            fault_name = fault_state["name"]
            if method == "tools/list":
                fault_attempts["list"] += 1
                if clock["now"] >= self.deadline:
                    raise ValueError("provider deadline exceeded")
                if params and fault_name == "catalogue_cursor_loop":
                    return {"result": {"tools": [], "nextCursor": "next-page"}}
                if params and fault_name == "catalogue_missing_final":
                    return {"result": {"tools": []}}
                response = ProviderTransport.send(self, method, params, token)
                if not params and fault_name == "catalogue_deadline":
                    clock["now"] += 21
                return response
            if method == "tools/call":
                fault_attempts["call"] += 1
                if fault_name in {"provider_unauthorized", "provider_forbidden", "not_found"}:
                    raise ProviderResponseError(fault_name)
                if fault_name == "timeout":
                    raise TimeoutError
                if fault_name in {"rate_limited", "rate_limited_recovered"} and (fault_name == "rate_limited" or fault_attempts["call"] == 1):
                    raise ProviderResponseError("rate_limited", retry_after=2)
                if fault_name in {"provider_unavailable", "provider_unavailable_recovered"} and (
                    fault_name == "provider_unavailable" or fault_attempts["call"] == 1
                ):
                    raise ProviderResponseError("provider_unavailable")
            return ProviderTransport.send(self, method, params, token)

    def provider_read(token, alias, arguments, pins, *, deadline, reviewed_schemas=None):
        assert reviewed_schemas == config.observation.read_schemas
        transport = FaultTransport if fault_state["name"] is not None else ProviderTransport
        return read_tool(token, alias, arguments, pins, deadline=deadline, transport_factory=transport, reviewed_schemas=reviewed_schemas)

    async def identity(admission, configuration):
        assert admission.context.canonical_user_id == "alice" and admission.context.site_id == site_state["id"]
        return "fixture-transient-token"

    monkeypatch.setattr(atlassian_route, "retrieve_access_token", identity)
    monkeypatch.setattr(atlassian_route, "read_tool", provider_read)
    gateway_list = []

    class ControlledGateway:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, content, headers):
            if url.startswith("https://core.example.test/"):
                response = client.post(url.removeprefix("https://core.example.test"), content=content, headers=headers)
                return httpx.Response(response.status_code, request=httpx.Request("POST", url), content=response.content)
            packet = json.loads(content)
            event = {"mcp": {"gatewayRequest": {"body": packet, "headers": headers}}}
            relayed = handler(event, None)["mcp"]["transformedGatewayRequest"]["body"]
            if packet["method"] == "tools/call":
                relayed["params"]["name"] = relayed["params"]["name"].removeprefix("atlassian___")
            response = adapter.post(
                "/internal/v1/tools/atlassian/mcp",
                json=relayed,
                headers={"x-adp-edge-provenance": "fixture-edge", "x-caller-identity": ROLE},
            )
            if response.status_code != 200:
                return httpx.Response(response.status_code, request=httpx.Request("POST", url), content=response.content)
            if packet["method"] == "tools/list":
                gateway_list.extend(response.json()["result"]["tools"])
            event["mcp"]["gatewayResponse"] = {"statusCode": 200, "body": response.json()}
            completed = handler(event, None)["mcp"]["transformedGatewayResponse"]["body"]
            return httpx.Response(200, request=httpx.Request("POST", url), json=completed)

    monkeypatch.setattr(facade.httpx, "AsyncClient", ControlledGateway)
    monkeypatch.setattr(core, "observation_settings", lambda: (None, None, None, RELEASE))
    adapter = TestClient(create_app(config, source_revision=RELEASE["source_revision"]))
    decision = facade.Decision.model_validate(
        {
            "schema_version": "1.0",
            "allowed": True,
            "principal": {"user_id": "alice", "tenant_id": "org-acme", "team_id": "team-eng", "root_human_id": "alice"},
            "receipt_id": "receipt-list",
            "connection_id": credential.id,
            "connection_scopes": scopes,
            "atlassian_connection": {"id": credential.id, "revision": binding.revision, "site_id": "site-1"},
        }
    )
    tools_catalogue.invalidate_catalogue_cache()
    try:
        assert tools_catalogue.gateway_catalogue()["tools"] == []
        discovered = await facade.AgentCoreClient().discover(decision, require_search=False)
        expected_names = {"atlassian___" + alias for alias in READ_TOOLS}
        assert {tool["name"] for tool in gateway_list} == set(READ_TOOLS)
        assert len(gateway_list) == 4 and "createJiraIssue" not in {tool["name"] for tool in gateway_list}
        assert discovered["tools_list_sha256"] == hashlib.sha256(canonical(gateway_list)).hexdigest()
        catalogue = tools_catalogue.gateway_catalogue()
        assert [tool["name"] for tool in catalogue["tools"]] == sorted("atlassian." + alias for alias in READ_TOOLS)
        assert {tool["name"] for tool in catalogue["tools"]} == {name.replace("___", ".") for name in expected_names}
        for alias, (result_id, sentinel) in fixture_results.items():
            current = decision.model_copy(update={"receipt_id": "receipt-" + alias})
            result = await facade.AgentCoreClient().invoke(
                "atlassian___" + alias,
                {"cloudId": "site-1", "id": "fixture-id"},
                current,
            )
            assert result["result"]["structuredContent"] == {"id": result_id, "content": sentinel, "site": "site-1"}
            assert result["result"]["_meta"]["adp_receipt_id"] == current.receipt_id
        assert provider_calls == list(fixture_results)
        assert provider_pages == [{}, {"cursor": "next-page"}] * 4
        site_state["id"] = "site-2"
        binding.selected_site_id = "site-2"
        binding.revision = "revision-2"
        await db.commit()
        updated = decision.model_copy(
            update={
                "receipt_id": "receipt-new-site",
                "atlassian_connection": {"id": credential.id, "revision": "revision-2", "site_id": "site-2"},
            }
        )
        result = await facade.AgentCoreClient().invoke(
            "atlassian___get_confluence_page",
            {"cloudId": "site-2", "id": "fixture-id"},
            updated,
        )
        assert result["result"]["structuredContent"] == {
            "id": "CONF-PAGE-9",
            "content": "confluence-page-sentinel",
            "site": "site-2",
        }
        assert provider_calls == [*fixture_results, "get_confluence_page"]
        assert provider_pages == [{}, {"cursor": "next-page"}] * 5
        if fault is None:
            return
        if fault == "publication_failure":
            monkeypatch.setattr(core, "observation_settings", lambda: (None, None, None, {**RELEASE, "group_generation": 2}))
            from src.internal.tools_dynamic_catalogue import _key, target_binding

            target = target_binding(GATEWAY_ID, catalogue_state.target, catalogue_state.gateway)
            record = catalogue_state.items[(_key(target)["pk"]["S"], _key(target)["sk"]["S"])]
            document = json.loads(record["document"]["S"])
            document["expires_at"] = document["observed_at"]
            record["document"]["S"] = dynamic.canonical(document).decode()
            with pytest.raises(httpx.HTTPStatusError) as error:
                await facade.AgentCoreClient().discover(updated, require_search=False)
            assert error.value.response.status_code == 503
            catalogue = tools_catalogue.gateway_catalogue()
            assert catalogue["tools"] == []
            assert next(connector for connector in catalogue["connectors"] if connector["name"] == "atlassian")["discovery_status"] == "unavailable"
            assert provider_calls == [*fixture_results, "get_confluence_page"]
            return
        fault_state["name"] = fault
        clock["now"] = time.monotonic_ns() / 1_000_000_000

        def advance(seconds):
            clock["delays"].append(seconds)
            clock["now"] += seconds

        monkeypatch.setattr(atlassian, "time", SimpleNamespace(monotonic=lambda: clock["now"], sleep=advance))
        attempted = updated.model_copy(update={"receipt_id": "receipt-fault-" + fault})
        if fault in {"rate_limited_recovered", "provider_unavailable_recovered"}:
            result = await facade.AgentCoreClient().invoke(
                "atlassian___get_jira_issue",
                {"cloudId": "site-2", "id": "fixture-id"},
                attempted,
            )
            assert result["result"]["structuredContent"]["id"] == "JIRA-ISSUE-42"
            assert provider_calls == [*fixture_results, "get_confluence_page", "get_jira_issue"]
        else:
            expected_status, expected_error = {
                "catalogue_cursor_loop": (503, "catalogue_incomplete"),
                "catalogue_missing_final": (503, "catalogue_incomplete"),
                "catalogue_deadline": (503, "provider deadline exceeded"),
                "provider_unauthorized": (401, "provider_unauthorized"),
                "provider_forbidden": (403, "provider_forbidden"),
                "not_found": (404, "not_found"),
                "rate_limited": (429, "rate_limited"),
                "provider_unavailable": (503, "provider_unavailable"),
                "timeout": (503, "provider_unavailable"),
            }[fault]
            with pytest.raises(httpx.HTTPStatusError) as error:
                await facade.AgentCoreClient().invoke(
                    "atlassian___get_jira_issue",
                    {"cloudId": "site-2", "id": "fixture-id"},
                    attempted,
                )
            assert error.value.response.status_code == expected_status
            assert error.value.response.json()["detail"]["error"] == expected_error
            assert provider_calls == [*fixture_results, "get_confluence_page"]
        retried = {"rate_limited", "rate_limited_recovered", "provider_unavailable", "provider_unavailable_recovered"}
        expected_calls = 0 if fault.startswith("catalogue_") else 2 if fault in retried else 1
        assert fault_attempts == {"list": 2, "call": expected_calls}
        assert clock["delays"] == ([2] if fault in {"rate_limited", "rate_limited_recovered"} else [])
    finally:
        tools_catalogue.invalidate_catalogue_cache()


@pytest.mark.asyncio
async def test_adapter_dispatch_consumes_one_receipt_and_rejects_replay(authority, monkeypatch):
    client, db, _, _, kms = authority
    root = Path(__file__).resolve().parents[4]
    monkeypatch.syspath_prepend(str(root / "modules/tools"))
    from adp_tools.adapter_contracts import Admission, DispatchReceipt, DispatchRequest

    admission = Admission.model_validate(client.post("/internal/v1/tool-adapters/authorize", json=request_body()).json())
    request = DispatchRequest.model_validate(dispatch_body(admission.model_dump())).model_dump()
    response = client.post("/internal/v1/tool-adapters/dispatch", json=request)
    assert response.status_code == 200, response.text
    DispatchReceipt.model_validate(response.json())
    assert response.json()["admission_id"] == admission.admission_id
    replay = client.post("/internal/v1/tool-adapters/dispatch", json=request)
    assert replay.status_code == 403
    duplicate = client.post("/internal/v1/tool-adapters/authorize", json=request_body()).json()
    assert duplicate["admission_id"] != admission.admission_id
    assert client.post("/internal/v1/tool-adapters/dispatch", json=dispatch_body(duplicate)).status_code == 409
    from src.shared.models.vault import OAuthToolReceipt

    receipt = await db.get(OAuthToolReceipt, admission.admission_id)
    assert receipt.consumed_at is not None and receipt.authorization["binding"] == admission.binding.model_dump()
    assert all("token" not in str(item).lower() for item in (receipt.authorization, response.json()))


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["fenced", "new_generation", "reconnect", "site", "consent", "changed_receipt", "changed_revision"])
async def test_adapter_dispatch_rechecks_generation_and_owner_before_receipt(authority, change):
    client, db, credential, binding, kms = authority
    admission = client.post("/internal/v1/tool-adapters/authorize", json=request_body()).json()
    assert "admission_id" in admission
    request = dispatch_body(admission)
    if change == "fenced":
        activation = await db.get(ToolAdapterActivation, RELEASE["group"])
        activation.status = "fenced"
        await db.commit()
    elif change == "new_generation":
        activation = await db.get(ToolAdapterActivation, RELEASE["group"])
        activation.release = {**RELEASE, "group_generation": 2}
        await db.commit()
    elif change == "reconnect":
        binding.revision = "revision-2"
        await db.commit()
    elif change == "site":
        binding.selected_site_id = "site-2"
        await db.commit()
    elif change == "consent":
        credential.agent_consent = False
        await db.commit()
    elif change == "changed_receipt":
        request["binding"]["receipt_id"] = "replay"
    elif change == "changed_revision":
        request["binding_revision"] = "revision-2"
    response = client.post("/internal/v1/tool-adapters/dispatch", json=request)
    assert response.status_code == 403, (change, response.text)
    from src.shared.models.vault import OAuthToolReceipt

    receipt = await db.get(OAuthToolReceipt, admission["admission_id"])
    assert receipt.consumed_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["registry_scope", "transport_role", "malformed_receipt"])
async def test_adapter_dispatch_denies_untrusted_transport_or_corrupt_receipt(authority, monkeypatch, change):
    from src.shared.models.vault import OAuthToolReceipt

    client, db, _, _, _ = authority
    admission = client.post("/internal/v1/tool-adapters/authorize", json=request_body()).json()
    receipt = await db.get(OAuthToolReceipt, admission["admission_id"])
    if change == "registry_scope":
        original = client.app.dependency_overrides[verify_internal_or_irsa]

        async def ungranted(request: Request):
            await original(request)
            request.state.token_context.credential_scopes.discard("tools:adapter:dispatch")

        client.app.dependency_overrides[verify_internal_or_irsa] = ungranted
    elif change == "transport_role":
        monkeypatch.setattr(tool_adapter_authority, "verified_caller_identity", lambda request: FACADE_ROLE)
    else:
        receipt.authorization = {**receipt.authorization, "expires_at": "not-a-timestamp"}
        await db.commit()
    refused = client.post("/internal/v1/tool-adapters/dispatch", json=dispatch_body(admission))
    assert refused.status_code == 403, refused.text
    assert (await db.get(OAuthToolReceipt, admission["admission_id"])).consumed_at is None


@pytest.mark.asyncio
async def test_adapter_consumer_rechecks_core_dispatch_through_schema_validated_transport(authority, monkeypatch):
    import httpx
    from fastapi import HTTPException

    client, db, _, _, _ = authority
    root = Path(__file__).resolve().parents[4]
    monkeypatch.syspath_prepend(str(root / "modules/tools/adapters"))
    monkeypatch.syspath_prepend(str(root / "modules/tools"))
    from adp_tools.adapter_contracts import Admission
    from tool_adapters import dispatch
    from tool_adapters.config import AdapterConfiguration

    config = AdapterConfiguration.model_validate(
        {
            "schema_version": "1.0",
            "mode": "observation",
            "release": RELEASE,
            "observation": {
                "core_authorize_url": "https://core.example.test/internal/v1/tool-adapters/authorize",
                "core_role_arn": "arn:aws:iam::123456789012:role/adp-core",
                "assertion_key_arn": KEY,
                "edge_provenance_sha256": "c" * 64,
                "identity_workload": "adp-tools-dev-atlassian-mcp-oauth-user",
                "identity_provider": "adp-tools-dev-atlassian-mcp-user",
                "config_revision": "a" * 64,
                "subject_schema_sha256": "b" * 64,
                "sites_schema_sha256": "c" * 64,
            },
        }
    )
    admission = Admission.model_validate(client.post("/internal/v1/tool-adapters/authorize", json=request_body()).json())
    monkeypatch.setattr(dispatch, "_sign_dispatch", lambda url, payload: {"content-type": "application/json"})
    native_post = httpx.AsyncClient.post

    async def core_post(self, url, *, content, headers):
        assert url == "https://core.example.test/internal/v1/tool-adapters/dispatch"
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client.app), base_url="https://core.example.test") as forwarded:
            return await native_post(forwarded, url, content=content, headers=headers)

    monkeypatch.setattr(dispatch.httpx.AsyncClient, "post", core_post)
    receipt = await dispatch.consume_dispatch(admission, config)
    assert receipt.admission_id == admission.admission_id
    with pytest.raises(HTTPException) as replay:
        await dispatch.consume_dispatch(admission, config)
    assert replay.value.status_code == 503
    activation = await db.get(ToolAdapterActivation, RELEASE["group"])
    activation.status = "fenced"
    await db.commit()
    with pytest.raises(HTTPException) as fenced:
        await dispatch.consume_dispatch(admission, config)
    assert fenced.value.status_code == 503


@pytest.mark.asyncio
async def test_disconnect_fences_fresh_admission_and_existing_dispatch_even_with_provider_outage(authority, monkeypatch):
    from unittest.mock import AsyncMock

    from sqlalchemy import select

    from src.auth import atlassian_mcp_routes
    from src.auth.middleware import get_current_user_context
    from src.shared.models.audit import AuditLog
    from tests.auth.test_connections_routes import caller

    client, db, credential, binding, _ = authority
    client.app.include_router(atlassian_mcp_routes.router)
    client.app.dependency_overrides[get_current_user_context] = lambda: caller("alice")
    provider = AsyncMock(side_effect=RuntimeError("provider unavailable"))
    monkeypatch.setattr(atlassian_mcp_routes, "observe_connection", provider)
    admission = client.post("/internal/v1/tool-adapters/authorize", json=request_body())
    assert admission.status_code == 200, admission.text
    path = "/auth/connections/atlassian-mcp"
    assert client.delete(path).status_code == 409
    stale = client.request("DELETE", path, json={"connection_revision": "stale-revision"})
    assert stale.status_code == 409
    assert await db.get(UserCredential, credential.id) is not None
    response = client.request("DELETE", path, json={"connection_revision": binding.revision})
    assert response.status_code == 200, response.text
    assert response.json()["revocation_complete"] is False
    assert response.json()["provider_token_revocation"] == "not_attempted"
    db.expire_all()
    assert await db.get(UserCredential, credential.id) is None
    assert await db.get(OAuthConnectionBinding, credential.id) is None
    denied = client.post("/internal/v1/tool-adapters/authorize", json=request_body())
    assert denied.status_code == 403, denied.text
    dispatch = client.post("/internal/v1/tool-adapters/dispatch", json=dispatch_body(admission.json()))
    assert dispatch.status_code == 403, dispatch.text
    provider.assert_not_awaited()
    repeated = client.request("DELETE", path, json={"connection_revision": binding.revision})
    assert repeated.status_code == 200, repeated.text
    events = (await db.scalars(select(AuditLog).where(AuditLog.event_type == "oauth_connection_disconnected"))).all()
    assert len(events) == 1 and events[0].actor_id == "alice"

    from datetime import timedelta

    from src.shared.models.vault import OAuthConnectionAttempt

    pending = OAuthConnectionAttempt(
        state_digest="f" * 64,
        org_id="org-acme",
        user_id="alice",
        provider="atlassian-mcp",
        session_uri="opaque-session",
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
        identity_completion_started_at=datetime.now(UTC),
        identity_completed_at=datetime.now(UTC),
    )
    db.add(pending)
    await db.commit()
    pending_id = pending.state_digest
    cancelled = client.delete(path)
    assert cancelled.status_code == 200, cancelled.text
    db.expire_all()
    assert (await db.get(OAuthConnectionAttempt, pending_id)).superseded_at is not None
    provider.assert_not_awaited()


@pytest.mark.asyncio
async def test_two_user_callback_replay_and_release_fence_through_real_admission_routes(authority, monkeypatch, record_property):
    from unittest.mock import AsyncMock

    from src.auth.middleware import get_current_user_context
    from src.shared.identity.oauth_setup import get_oauth_setup
    from src.shared.models.vault import OAuthToolReceipt
    from tests.auth.test_connections_routes import caller

    client, db, credential, binding, _ = authority
    client.app.include_router(atlassian_mcp_routes.router)
    client.app.dependency_overrides[get_current_user_context] = lambda: caller("alice")
    setup = SimpleNamespace(
        settings=SimpleNamespace(oauth_workload_name="adp-tools-dev-oauth-user", atlassian_identity_compatibility_verified=True),
        secret_store=object(),
        atlassian_mcp_status=lambda **kwargs: {"status": "ready"},
    )
    client.app.dependency_overrides[get_oauth_setup] = lambda: setup
    identity = MagicMock()
    identity.get_workload_access_token_for_user_id.return_value = {"workloadAccessToken": "workload-sentinel"}
    identity.get_resource_oauth2_token.return_value = {
        "sessionStatus": "IN_PROGRESS",
        "authorizationUrl": "https://example.test/consent",
        "sessionUri": "opaque-session",
    }
    client.app.dependency_overrides[atlassian_mcp_routes.get_identity_client] = lambda: identity
    monkeypatch.setattr(atlassian_mcp_routes, "read_tenant_policy_observation", AsyncMock(return_value="allowed"))
    monkeypatch.setattr(
        atlassian_mcp_routes,
        "atlassian_mcp_config",
        lambda settings, secret_store: {"return_url": "https://example.test/api/auth/connections/atlassian-mcp/return", "metadata_sha256": "c" * 64},
    )
    observation = AsyncMock(
        side_effect=lambda *, attempt, user_id, tenant_id: SimpleNamespace(
            attempt_id=attempt.state_digest,
            config_revision="c" * 64,
            purpose="connection_observe",
            provider_user_id="provider-alice",
            sites=(SimpleNamespace(site_id="site-1", scopes=tuple(SCOPES[:2])),),
            granted_scopes=atlassian_mcp_routes.SCOPES,
        )
    )
    monkeypatch.setattr(atlassian_mcp_routes, "observe_connection", observation)

    admitted = client.post("/internal/v1/tool-adapters/authorize", json=request_body())
    assert admitted.status_code == 200, admitted.text
    old_revision = binding.revision
    start = client.post(
        "/auth/connections/atlassian-mcp/start",
        json={
            "capability_set": "reads",
            "reconnect_revision": old_revision,
        },
    )
    assert start.status_code == 200, start.text
    callback = {"state": start.json()["state"], "session_uri": start.json()["session_uri"]}
    client.app.dependency_overrides[get_current_user_context] = lambda: caller("bob")
    foreign = client.post("/auth/connections/atlassian-mcp/complete", json=callback)
    assert foreign.status_code == 403
    identity.complete_resource_token_auth.assert_not_called()
    observation.assert_not_awaited()
    client.app.dependency_overrides[get_current_user_context] = lambda: caller("alice")
    completed = client.post("/auth/connections/atlassian-mcp/complete", json=callback)
    assert completed.status_code == 201, completed.text
    assert completed.json()["id"] == credential.id and completed.json()["revision"] != old_revision
    replay = client.post("/auth/connections/atlassian-mcp/complete", json=callback)
    assert replay.status_code == 403
    identity.complete_resource_token_auth.assert_called_once()
    observation.assert_awaited_once()

    stale = client.post("/internal/v1/tool-adapters/dispatch", json=dispatch_body(admitted.json()))
    assert stale.status_code == 403, stale.text
    old_receipt = await db.get(OAuthToolReceipt, admitted.json()["admission_id"])
    assert old_receipt.consumed_at is None
    assert client.post("/internal/v1/tool-adapters/authorize", json=request_body()).status_code == 403
    fresh = client.post(
        "/internal/v1/tool-adapters/authorize",
        json=request_body(
            connection={
                "id": credential.id,
                "revision": completed.json()["revision"],
                "site_id": "site-1",
            }
        ),
    )
    assert fresh.status_code == 200, fresh.text
    activation = await db.get(ToolAdapterActivation, RELEASE["group"])
    activation.status = "fenced"
    await db.commit()
    refused = client.post("/internal/v1/tool-adapters/dispatch", json=dispatch_body(fresh.json()))
    assert refused.status_code == 403, refused.text
    assert (await db.get(OAuthToolReceipt, fresh.json()["admission_id"])).consumed_at is None
    assert (
        client.post(
            "/internal/v1/tool-adapters/authorize",
            json=request_body(
                connection={
                    "id": credential.id,
                    "revision": completed.json()["revision"],
                    "site_id": "site-1",
                }
            ),
        ).status_code
        == 403
    )
    identity.complete_resource_token_auth.assert_called_once()
    observation.assert_awaited_once()
    record_property(
        "identity_observation",
        json.dumps(
            {
                "owner": "alice",
                "foreign_status": foreign.status_code,
                "callback_status": completed.status_code,
                "replay_status": replay.status_code,
                "old_dispatch_status": stale.status_code,
                "fenced_dispatch_status": refused.status_code,
                "fresh_admission_status": fresh.status_code,
                "fenced_admission_status": 403,
                "old_attempt": start.json()["attempt_id"],
                "generation": RELEASE["group_generation"],
                "provider_calls": identity.complete_resource_token_auth.call_count,
            }
        ),
    )


@pytest.mark.asyncio
async def test_cached_or_forged_atlassian_read_is_denied_before_provider(authority, monkeypatch):
    import httpx

    root = Path(__file__).resolve().parents[4]
    monkeypatch.syspath_prepend(str(root / "modules/tools"))
    monkeypatch.syspath_prepend(str(root / "modules/tools/facade"))
    monkeypatch.syspath_prepend(str(root / "modules/tools/adapters"))
    monkeypatch.syspath_prepend(str(root / "modules/tools/interceptor"))
    from tool_adapters import atlassian_route, dispatch
    from tool_adapters.atlassian import READ_TOOLS
    from tool_adapters.config import AdapterConfiguration
    from tool_adapters.server import create_app
    from tools_facade import server as facade
    from tools_interceptor.handler import handler

    client, db, _, current_binding, kms = authority
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("ADP_TOOLS_AGENTCORE_URL", "https://fixture.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp")
    monkeypatch.setenv("ADP_TOOLS_FACADE_ROLE_ARN", FACADE_ROLE)
    monkeypatch.setenv("ADP_TOOLS_ASSERTION_KEY_ARN", KEY)
    monkeypatch.setenv("ADP_TOOL_ADAPTER_RELEASE", json.dumps(RELEASE))
    kms.sign.side_effect = lambda **kwargs: {"Signature": hmac.digest(b"fixture-key", kwargs["Message"], "sha256")}
    monkeypatch.setattr(facade.boto3, "client", lambda name, **kwargs: kms if name == "kms" else None)
    monkeypatch.setattr(facade, "signed_headers", lambda *args: {})
    gateway_role = "arn:aws:iam::123456789012:role/fixture-gateway"
    schema = {"type": "object", "properties": {"cloudId": {"type": "string"}, "id": {"type": "string"}}, "required": ["cloudId", "id"]}
    config = AdapterConfiguration.model_validate(
        {
            "schema_version": "1.0",
            "mode": "observation",
            "release": RELEASE,
            "observation": {
                "core_authorize_url": "https://core.example.test/internal/v1/tool-adapters/authorize",
                "core_role_arn": "arn:aws:iam::123456789012:role/fixture-core",
                "assertion_key_arn": KEY,
                "edge_provenance_sha256": hashlib.sha256(b"fixture-edge").hexdigest(),
                "identity_workload": "adp-tools-dev-atlassian-mcp-oauth-user",
                "identity_provider": "adp-tools-dev-atlassian-mcp-user",
                "config_revision": "a" * 64,
                "subject_schema_sha256": "b" * 64,
                "sites_schema_sha256": "c" * 64,
                "gateway_role_arn": gateway_role,
                "read_schemas": {alias: schema for alias in READ_TOOLS},
                "read_schemas_sha256": {alias: hashlib.sha256(canonical(schema)).hexdigest() for alias in READ_TOOLS},
            },
        }
    )
    from fastapi.testclient import TestClient

    adapter = TestClient(create_app(config, source_revision="a" * 40))
    monkeypatch.setattr(atlassian_route, "_sign_dispatch", lambda url, payload: {"content-type": "application/json"})
    monkeypatch.setattr(dispatch, "_sign_dispatch", lambda url, payload: {"content-type": "application/json"})
    identity_calls = []

    async def access_token(admission, configuration):
        assert admission.context.canonical_user_id == "alice"
        identity_calls.append((admission.context.tenant_id, admission.context.binding_revision, admission.context.site_id))
        return "fixture-transient-token"

    provider_calls = []

    def provider_read(token, alias, arguments, pins, *, deadline, reviewed_schemas=None):
        assert token == "fixture-transient-token" and alias == "get_jira_issue"
        assert arguments == {"cloudId": "site-1", "id": "fixture-id"}
        assert pins == config.observation.read_schemas_sha256 and deadline > time.monotonic()
        assert reviewed_schemas == config.observation.read_schemas
        provider_calls.append(alias)
        return {"structuredContent": {"id": "JIRA-ISSUE-42"}}

    monkeypatch.setattr(atlassian_route, "retrieve_access_token", access_token)
    monkeypatch.setattr(atlassian_route, "read_tool", provider_read)
    admissions = []
    core_denials = []

    class ControlledGateway:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, content, headers):
            if url.startswith("https://core.example.test/"):
                response = client.post(url.removeprefix("https://core.example.test"), content=content, headers=headers)
                if url.endswith("/authorize") and response.status_code == 200:
                    admissions.append(response.json())
                elif url.endswith("/authorize"):
                    core_denials.append(response.status_code)
                return httpx.Response(response.status_code, request=httpx.Request("POST", url), content=response.content)
            packet = json.loads(content)
            event = {"mcp": {"gatewayRequest": {"body": packet, "headers": headers}}}
            forwarded = handler(event, None)["mcp"]
            assert "transformedGatewayRequest" in forwarded, forwarded
            relayed = forwarded["transformedGatewayRequest"]["body"]
            relayed["params"]["name"] = relayed["params"]["name"].removeprefix("atlassian___")
            response = adapter.post(
                "/internal/v1/tools/atlassian/mcp",
                json=relayed,
                headers={"x-adp-edge-provenance": "fixture-edge", "x-caller-identity": gateway_role},
            )
            if response.status_code != 200:
                return httpx.Response(response.status_code, request=httpx.Request("POST", url), content=response.content)
            event["mcp"]["gatewayResponse"] = {"statusCode": 200, "body": response.json()}
            completed = handler(event, None)["mcp"]["transformedGatewayResponse"]["body"]
            return httpx.Response(200, request=httpx.Request("POST", url), json=completed)

    monkeypatch.setattr(facade.httpx, "AsyncClient", ControlledGateway)
    decision = facade.Decision.model_validate(
        {
            "schema_version": "1.0",
            "allowed": True,
            "principal": {"user_id": "alice", "tenant_id": "org-acme", "team_id": "team-eng", "root_human_id": "alice"},
            "receipt_id": "receipt-ac01",
            "connection_id": "credential-alice",
            "connection_scopes": SCOPES,
            "atlassian_connection": {"id": "credential-alice", "revision": "revision-1", "site_id": "site-1"},
        }
    )
    result = await facade.AgentCoreClient().invoke("atlassian___get_jira_issue", {"cloudId": "site-1", "id": "fixture-id"}, decision)
    assert result["result"]["structuredContent"]["id"] == "JIRA-ISSUE-42"
    assert len(admissions) == 1
    assert admissions[0]["context"]["canonical_user_id"] == "alice"
    assert admissions[0]["context"]["tenant_id"] == "org-acme"
    assert admissions[0]["context"]["connection_id"] == "credential-alice"
    assert admissions[0]["context"]["binding_revision"] == "revision-1"
    assert admissions[0]["context"]["site_id"] == "site-1"
    assert admissions[0]["operation"] == "get_jira_issue"
    assert admissions[0]["binding"]["receipt_id"] == "receipt-ac01"
    assert result["result"]["_meta"]["adp_receipt_id"] == "receipt-ac01"
    assert provider_calls == ["get_jira_issue"]

    arguments = {"cloudId": "site-1", "id": "fixture-id"}
    denied_decisions = (
        decision.model_copy(
            update={
                "receipt_id": "receipt-foreign-user",
                "principal": decision.principal.model_copy(update={"user_id": "bob", "root_human_id": "bob"}),
            }
        ),
        decision.model_copy(
            update={
                "receipt_id": "receipt-foreign-tenant",
                "principal": decision.principal.model_copy(update={"tenant_id": "org-other"}),
            }
        ),
        decision.model_copy(
            update={
                "receipt_id": "receipt-foreign-site",
                "atlassian_connection": {**decision.atlassian_connection, "site_id": "site-other"},
            }
        ),
    )
    for denied_index, denied in enumerate(denied_decisions, start=1):
        with pytest.raises(httpx.HTTPStatusError) as error:
            await facade.AgentCoreClient().invoke("atlassian___get_jira_issue", arguments, denied)
        assert error.value.response.status_code in {403, 503}
        assert core_denials == [403] * denied_index
        assert len(admissions) == 1
        assert provider_calls == ["get_jira_issue"]
        assert identity_calls == [("org-acme", "revision-1", "site-1")]

    current_binding.revision = "revision-2"
    await db.commit()
    cached_decision = decision.model_copy(update={"receipt_id": "receipt-stale-revision"})
    with pytest.raises(httpx.HTTPStatusError) as error:
        await facade.AgentCoreClient().invoke("atlassian___get_jira_issue", arguments, cached_decision)
    assert error.value.response.status_code == 503
    assert core_denials == [403] * 4
    assert len(admissions) == 1
    assert provider_calls == ["get_jira_issue"]
    assert identity_calls == [("org-acme", "revision-1", "site-1")]

    fresh_decision = decision.model_copy(
        update={
            "receipt_id": "receipt-new-revision",
            "atlassian_connection": {**decision.atlassian_connection, "revision": "revision-2"},
        }
    )
    refreshed = await facade.AgentCoreClient().invoke("atlassian___get_jira_issue", arguments, fresh_decision)
    assert refreshed["result"]["structuredContent"]["id"] == "JIRA-ISSUE-42"
    assert refreshed["result"]["_meta"]["adp_receipt_id"] == "receipt-new-revision"
    assert len(admissions) == 2
    assert admissions[-1]["context"]["binding_revision"] == "revision-2"
    assert provider_calls == ["get_jira_issue", "get_jira_issue"]
    assert identity_calls == [("org-acme", "revision-1", "site-1"), ("org-acme", "revision-2", "site-1")]

    current_binding.observed_resources = [{"site_id": "site-1", "scopes": []}]
    await db.commit()
    revoked_decision = fresh_decision.model_copy(update={"receipt_id": "receipt-revoked-scope"})
    with pytest.raises(httpx.HTTPStatusError) as error:
        await facade.AgentCoreClient().invoke("atlassian___get_jira_issue", arguments, revoked_decision)
    assert error.value.response.status_code == 503
    assert core_denials == [403] * 5
    assert len(admissions) == 2
    assert provider_calls == ["get_jira_issue", "get_jira_issue"]
    assert identity_calls == [("org-acme", "revision-1", "site-1"), ("org-acme", "revision-2", "site-1")]


@pytest.mark.asyncio
async def test_legacy_adapter_admission_reopens_owned_github_connection(authority, monkeypatch):
    from src.internal import github_oauth
    from src.shared.models.vault import OAuthToolReceipt

    client, db, _, _, _ = authority
    monkeypatch.setenv("ADP_TOOL_ADAPTER_LEGACY_ENABLED", "true")
    monkeypatch.setattr(github_oauth, "get_settings", lambda: SimpleNamespace(oauth_workload_name="adp-tools-dev-oauth-user"))
    credential = UserCredential(
        id="github-alice",
        org_id="org-acme",
        user_id="alice",
        service="github",
        credential_type="oauth_token",
        provider="agentcore-identity",
        label="GitHub",
        secret_arn="agentcore-identity:adp-tools-dev-oauth-user:github",
        scopes={"oauth_scopes": ["read:user"]},
        strict=True,
        agent_consent=True,
    )
    db.add(credential)
    await db.flush()
    binding = OAuthConnectionBinding(
        credential_id=credential.id,
        org_id="org-acme",
        user_id="alice",
        provider="github",
        revision="00000000-0000-4000-8000-000000000001",
        credential_digest=credential_digest(credential),
        provider_account_id="17",
        observed_scopes=["read:user"],
        verified_at=datetime.now(UTC),
    )
    db.add(binding)
    db.add(ToolEntitlement(org_id="org-acme", team_id="", tool_name="github.get_user", enabled=True))
    await db.commit()
    monkeypatch.setattr(
        tools_knowledge.ToolsPolicyStore,
        "get",
        lambda self, **kwargs: {
            "status": "active",
            "consent_mode": "explicit",
            "allowed_tools": ["github.get_user"],
            "consented_tools": ["github.get_user"],
            "tool_max_cost_usd": {"github.get_user": "1.00"},
            "revision": 1,
        },
    )
    connection = github_oauth.binding_claim(binding)

    def request(connection_claim):
        body = request_body(
            operation="get_user",
            connection=connection_claim,
            claims_update={
                "connector": "github",
                "tool": "github___get_user",
                "connection_id": credential.id,
                "github_connection": connection_claim,
                "connection_scopes": ["read:user"],
            },
        )
        body["connector"] = "github"
        return body

    response = client.post("/internal/v1/tool-adapters/authorize", json=request(connection))
    assert response.status_code == 200, response.text
    admission = response.json()
    assert admission["connector"] == "github"
    assert admission["identity"] == {
        "workload": "adp-tools-dev-oauth-user",
        "provider": "adp-tools-dev-github-user",
        "canonical_user_id": "alice",
    }
    assert admission["context"]["legacy"] == {
        "provider_account_id": "17",
        "observed_scopes": ["read:user"],
        "resources_sha256": None,
        "app_id": None,
        "site_scopes": [],
    }
    assert admission["context"]["connection_id"] == credential.id
    assert admission["context"]["binding_revision"] == binding.revision
    dispatched = client.post("/internal/v1/tool-adapters/dispatch", json=dispatch_body(admission))
    assert dispatched.status_code == 200, dispatched.text
    assert dispatched.json()["admission_id"] == admission["admission_id"]
    replay = client.post("/internal/v1/tool-adapters/dispatch", json=dispatch_body(admission))
    assert replay.status_code == 403
    stale = client.post("/internal/v1/tool-adapters/authorize", json=request({**connection, "revision": "stale"}))
    assert stale.status_code == 403

    import httpx

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "tools/adapters"))
    from tool_adapters import atlassian_route, dispatch, legacy_execution, legacy_route
    from tool_adapters.config import AdapterConfiguration
    from tool_adapters.legacy_github import GithubObservation
    from tool_adapters.server import create_app

    arguments = {}
    signed = request(connection)
    envelope = json.loads(tool_adapter_authority.decode(signed["signed_assertion"]))
    claims = envelope["claims"]
    claims["receipt_id"] = "receipt-relay-2"
    claims["arguments_sha256"] = hashlib.sha256(canonical(arguments)).hexdigest()
    signed["binding"]["receipt_id"] = claims["receipt_id"]
    signed["binding"]["payload_sha256"] = claims["arguments_sha256"]
    claims["binding"] = signed["binding"]
    signed["signed_assertion"] = encode(
        canonical(
            {
                "claims": claims,
                "signature": encode(hmac.digest(b"fixture-key", canonical(claims), "sha256")),
            }
        )
    )
    config = AdapterConfiguration.model_validate(
        {
            "schema_version": "1.0",
            "mode": "observation",
            "release": RELEASE,
            "observation": {
                "core_authorize_url": "https://core.example.test/internal/v1/tool-adapters/authorize",
                "core_role_arn": "arn:aws:iam::123456789012:role/adp-core",
                "assertion_key_arn": KEY,
                "edge_provenance_sha256": hashlib.sha256(b"fixture-edge").hexdigest(),
                "identity_workload": "adp-tools-dev-atlassian-mcp-oauth-user",
                "identity_provider": "adp-tools-dev-atlassian-mcp-user",
                "legacy_identity_workload": "adp-tools-dev-oauth-user",
                "config_revision": "a" * 64,
                "subject_schema_sha256": "b" * 64,
                "sites_schema_sha256": "c" * 64,
                "gateway_role_arn": FACADE_ROLE,
            },
        }
    )
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.ASGITransport(app=client.app), **kwargs))
    for module in (atlassian_route, legacy_route, dispatch):
        monkeypatch.setattr(module, "_sign_dispatch", lambda url, payload: {"content-type": "application/json"})
    calls = []

    async def identity(*args):
        calls.append("identity")
        return "fixture-user-token"

    async def github_user(token):
        assert token == "fixture-user-token"
        calls.append(("GET", "/user"))
        return GithubObservation("17", ("read:user",), {"id": 17, "login": "fixture"})

    monkeypatch.setattr(legacy_route, "retrieve_access_token", identity)
    monkeypatch.setattr(legacy_execution, "observe_github_user", github_user)
    packet = {
        "jsonrpc": "2.0",
        "id": claims["rpc_id"],
        "method": "tools/call",
        "params": {
            "name": "get_user",
            "arguments": {"adp_assertion": signed["signed_assertion"]},
        },
    }
    headers = {"x-adp-edge-provenance": "fixture-edge", "x-caller-identity": FACADE_ROLE}
    with TestClient(create_app(config, source_revision=RELEASE["source_revision"])) as adapter:
        result = adapter.post("/internal/v1/tools/github/mcp", json=packet, headers=headers)
        assert result.status_code == 200, result.text
        assert json.loads(result.json()["result"]["content"][0]["text"])["id"] == 17
        assert calls == ["identity", ("GET", "/user")]
        receipt_id = hashlib.sha256(
            canonical({"principal": claims["principal"], "receipt": claims["receipt_id"], "tool": "github___get_user"})
        ).hexdigest()
        assert await db.get(OAuthToolReceipt, receipt_id) is not None
        replay = adapter.post("/internal/v1/tools/github/mcp", json=packet, headers=headers)
        assert replay.status_code in {403, 503}
        assert calls == ["identity", ("GET", "/user")]


@pytest.mark.asyncio
@pytest.mark.parametrize("connector,operation", [("jira", "get_issue"), ("slack", "get_user"), ("slack", "post_message")])
async def test_legacy_adapter_relays_other_verbs_through_core(authority, monkeypatch, connector, operation):
    import httpx

    from src.internal import jira_oauth, slack_oauth
    from src.shared.models.vault import OAuthToolReceipt

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "tools/adapters"))
    from tool_adapters import atlassian_route, dispatch, legacy_execution, legacy_route
    from tool_adapters.config import AdapterConfiguration
    from tool_adapters.legacy_jira import JiraSiteObservation, JiraUserObservation
    from tool_adapters.server import create_app

    client, db, _, _, _ = authority
    monkeypatch.setenv("ADP_TOOL_ADAPTER_LEGACY_ENABLED", "true")
    marker = "11111111-1111-4111-8111-111111111111"
    if connector == "jira":
        monkeypatch.setattr(jira_oauth, "get_settings", lambda: SimpleNamespace(oauth_workload_name="adp-tools-dev-oauth-user"))
        site_id = "11111111-1111-4111-8111-111111111111"
        scopes = ["read:jira-work", "read:me", "offline_access"]
        observed_scopes = ["read:jira-work", "read:me"]
        resources = [{"cloud_id": site_id, "scopes": ["read:jira-work"]}]
        arguments = {"cloudId": site_id, "issueIdOrKey": "ADP-1"}
    else:
        monkeypatch.setattr(slack_oauth, "get_settings", lambda: SimpleNamespace(oauth_workload_name="adp-tools-dev-oauth-user"))
        site_id = None
        scopes = ["users:read", "chat:write"]
        observed_scopes = ["chat:write", "users:read"]
        resources = None
        arguments = (
            {"user": "U12345678"}
            if operation == "get_user"
            else {
                "channel": "C12345678",
                "text": f"Test message [run:{marker}]",
                "run_marker": marker,
            }
        )
        if operation == "post_message":
            monkeypatch.setenv("ADP_TOOLS_SLACK_POST_ENABLED", "true")
            monkeypatch.setenv(
                "ADP_TOOLS_SLACK_POST_FIXTURE",
                json.dumps(
                    {
                        "owner_id": "alice",
                        "team_id": "T12345678",
                        "channel_id": "C12345678",
                        "message": "Test message",
                        "run_marker": marker,
                        "approved": True,
                        "isolated": True,
                        "cleanup_method": "chat.delete",
                    }
                ),
            )
    credential = UserCredential(
        id=f"{connector}-alice",
        org_id="org-acme",
        user_id="alice",
        service=connector,
        credential_type="oauth_token",
        provider="agentcore-identity",
        label=connector,
        secret_arn=f"agentcore-identity:adp-tools-dev-oauth-user:{'atlassian' if connector == 'jira' else 'slack'}",
        scopes={"oauth_scopes": scopes},
        strict=True,
        agent_consent=True,
    )
    db.add(credential)
    await db.flush()
    binding = OAuthConnectionBinding(
        credential_id=credential.id,
        org_id="org-acme",
        user_id="alice",
        provider="atlassian" if connector == "jira" else "slack",
        revision="00000000-0000-4000-8000-000000000001",
        credential_digest=credential_digest(credential),
        provider_account_id="account:1" if connector == "jira" else "T12345678:U12345678",
        observed_scopes=observed_scopes,
        observed_resources=resources,
        verified_at=datetime.now(UTC),
    )
    db.add(binding)
    db.add(ToolEntitlement(org_id="org-acme", team_id="", tool_name=f"{connector}.{operation}", enabled=True))
    await db.commit()
    monkeypatch.setattr(
        tools_knowledge.ToolsPolicyStore,
        "get",
        lambda self, **kwargs: {
            "status": "active",
            "consent_mode": "explicit",
            "allowed_tools": [f"{connector}.{operation}"],
            "consented_tools": [f"{connector}.{operation}"],
            "tool_max_cost_usd": {f"{connector}.{operation}": "1.00"},
            "revision": 1,
        },
    )
    connection = (jira_oauth if connector == "jira" else slack_oauth).binding_claim(binding)
    claims_update = {
        "connector": connector,
        "tool": f"{connector}___{operation}",
        "connection_id": credential.id,
        f"{connector}_connection": connection,
        "connection_scopes": observed_scopes,
    }
    if site_id is not None:
        claims_update["site_id"] = site_id
    if operation == "post_message":
        claims_update["run_marker"] = marker
    signed = request_body(operation=operation, connection=connection, claims_update=claims_update)
    signed["connector"] = connector
    envelope = json.loads(tool_adapter_authority.decode(signed["signed_assertion"]))
    claims = envelope["claims"]
    claims["arguments_sha256"] = hashlib.sha256(canonical(arguments)).hexdigest()
    signed["binding"]["payload_sha256"] = claims["arguments_sha256"]
    claims["binding"] = signed["binding"]
    signed["signed_assertion"] = encode(
        canonical(
            {
                "claims": claims,
                "signature": encode(hmac.digest(b"fixture-key", canonical(claims), "sha256")),
            }
        )
    )
    config = AdapterConfiguration.model_validate(
        {
            "schema_version": "1.0",
            "mode": "observation",
            "release": RELEASE,
            "observation": {
                "core_authorize_url": "https://core.example.test/internal/v1/tool-adapters/authorize",
                "core_role_arn": "arn:aws:iam::123456789012:role/adp-core",
                "assertion_key_arn": KEY,
                "edge_provenance_sha256": hashlib.sha256(b"fixture-edge").hexdigest(),
                "identity_workload": "adp-tools-dev-atlassian-mcp-oauth-user",
                "identity_provider": "adp-tools-dev-atlassian-mcp-user",
                "legacy_identity_workload": "adp-tools-dev-oauth-user",
                "config_revision": "a" * 64,
                "subject_schema_sha256": "b" * 64,
                "sites_schema_sha256": "c" * 64,
                "gateway_role_arn": FACADE_ROLE,
            },
        }
    )
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.ASGITransport(app=client.app), **kwargs))
    for module in (atlassian_route, legacy_route, dispatch):
        monkeypatch.setattr(module, "_sign_dispatch", lambda url, payload: {"content-type": "application/json"})
    calls = []

    async def identity(*args):
        calls.append("identity")
        return "fixture-user-token"

    async def jira_user(token):
        assert token == "fixture-user-token"
        calls.append(("GET", "/me"))
        return JiraUserObservation("account:1")

    async def jira_sites(token):
        assert token == "fixture-user-token"
        calls.append(("GET", "/oauth/token/accessible-resources"))
        return (JiraSiteObservation(site_id, ("read:jira-work",)),)

    async def jira_issue(token, *, protected_site, cloud_id, issue_id_or_key):
        assert (token, protected_site, cloud_id, issue_id_or_key) == (
            "fixture-user-token",
            JiraSiteObservation(site_id, ("read:jira-work",)),
            site_id,
            "ADP-1",
        )
        calls.append(("GET", f"/ex/jira/{site_id}/rest/api/3/issue/ADP-1"))
        return {"id": "1", "key": "ADP-1", "fields": {"summary": "Fixture issue"}}

    async def slack_user(token, *, protected_identity, user_id):
        assert (token, protected_identity.team_id, user_id) == ("fixture-user-token", "T12345678", "U12345678")
        calls.append(("GET", "/api/users.info"))
        return {"id": user_id, "name": "Fixture member"}

    async def slack_post(token, *, protected_identity, fixture, owner_id, channel_id, run_marker, text):
        assert (token, protected_identity.team_id, owner_id, channel_id, run_marker, text) == (
            "fixture-user-token",
            "T12345678",
            "alice",
            "C12345678",
            marker,
            arguments["text"],
        )
        assert fixture.run_marker == marker
        calls.append(("POST", "/api/chat.postMessage"))
        return {"channel_id": channel_id, "ts": "1234567890.000001", "user_id": "U12345678", "text": text, "run_marker": marker}

    monkeypatch.setattr(legacy_route, "retrieve_access_token", identity)
    monkeypatch.setattr(legacy_execution, "observe_jira_user", jira_user)
    monkeypatch.setattr(legacy_execution, "observe_jira_sites", jira_sites)
    monkeypatch.setattr(legacy_execution, "read_jira_issue", jira_issue)
    monkeypatch.setattr(legacy_execution, "read_slack_user", slack_user)
    monkeypatch.setattr(legacy_execution, "post_slack_fixture", slack_post)
    expected_effects = {
        ("jira", "get_issue"): [("GET", "/me"), ("GET", "/oauth/token/accessible-resources"), ("GET", f"/ex/jira/{site_id}/rest/api/3/issue/ADP-1")],
        ("slack", "get_user"): [("GET", "/api/users.info")],
        ("slack", "post_message"): [("POST", "/api/chat.postMessage")],
    }[connector, operation]
    expected_results = {
        ("jira", "get_issue"): {"id": "1", "key": "ADP-1", "fields": {"summary": "Fixture issue"}},
        ("slack", "get_user"): {"id": "U12345678", "name": "Fixture member"},
        ("slack", "post_message"): {
            "channel_id": "C12345678",
            "ts": "1234567890.000001",
            "user_id": "U12345678",
            "text": arguments.get("text"),
            "run_marker": marker,
        },
    }[connector, operation]
    packet = {
        "jsonrpc": "2.0",
        "id": claims["rpc_id"],
        "method": "tools/call",
        "params": {"name": operation, "arguments": {**arguments, "adp_assertion": signed["signed_assertion"]}},
    }
    headers = {"x-adp-edge-provenance": "fixture-edge", "x-caller-identity": FACADE_ROLE}
    with TestClient(create_app(config, source_revision=RELEASE["source_revision"])) as adapter:
        response = adapter.post(f"/internal/v1/tools/{connector}/mcp", json=packet, headers=headers)
        assert response.status_code == 200, response.text
        assert json.loads(response.json()["result"]["content"][0]["text"]) == expected_results
        assert calls == ["identity", *expected_effects]
        receipt_id = hashlib.sha256(
            canonical({"principal": claims["principal"], "receipt": claims["receipt_id"], "tool": f"{connector}___{operation}"})
        ).hexdigest()
        assert await db.get(OAuthToolReceipt, receipt_id) is not None
        if operation == "post_message":
            marker_id = hashlib.sha256(
                canonical(
                    {
                        "tool": "slack___post_message",
                        "owner_id": "alice",
                        "run_marker": marker,
                    }
                )
            ).hexdigest()
            assert await db.get(OAuthToolReceipt, marker_id) is not None
        replay = adapter.post(f"/internal/v1/tools/{connector}/mcp", json=packet, headers=headers)
        assert replay.status_code in {403, 503}
        assert calls == ["identity", *expected_effects]
        if operation == "post_message":
            claims["receipt_id"] = "receipt-second-attempt"
            claims["binding"]["receipt_id"] = claims["receipt_id"]
            packet["params"]["arguments"]["adp_assertion"] = encode(
                canonical(
                    {
                        "claims": claims,
                        "signature": encode(hmac.digest(b"fixture-key", canonical(claims), "sha256")),
                    }
                )
            )
            refused = adapter.post(f"/internal/v1/tools/{connector}/mcp", json=packet, headers=headers)
            assert refused.status_code == 503
            assert calls == ["identity", *expected_effects]


@pytest.mark.asyncio
async def test_legacy_slack_dispatch_consumes_durable_marker_before_effect(authority, monkeypatch):
    from src.internal import slack_oauth
    from src.shared.models.vault import OAuthToolReceipt

    client, db, _, _, _ = authority
    marker = "11111111-1111-4111-8111-111111111111"
    monkeypatch.setenv("ADP_TOOL_ADAPTER_LEGACY_ENABLED", "true")
    monkeypatch.setenv("ADP_TOOLS_SLACK_POST_ENABLED", "true")
    monkeypatch.setenv(
        "ADP_TOOLS_SLACK_POST_FIXTURE",
        json.dumps(
            {
                "owner_id": "alice",
                "team_id": "T12345678",
                "channel_id": "C12345678",
                "message": "Test message",
                "run_marker": marker,
                "approved": True,
                "isolated": True,
                "cleanup_method": "chat.delete",
            }
        ),
    )
    monkeypatch.setattr(slack_oauth, "get_settings", lambda: SimpleNamespace(oauth_workload_name="adp-tools-dev-oauth-user"))
    credential = UserCredential(
        id="slack-alice",
        org_id="org-acme",
        user_id="alice",
        service="slack",
        credential_type="oauth_token",
        provider="agentcore-identity",
        label="Slack",
        secret_arn="agentcore-identity:adp-tools-dev-oauth-user:slack",
        scopes={"oauth_scopes": ["users:read", "chat:write"]},
        strict=True,
        agent_consent=True,
    )
    db.add(credential)
    await db.flush()
    binding = OAuthConnectionBinding(
        credential_id=credential.id,
        org_id="org-acme",
        user_id="alice",
        provider="slack",
        revision="00000000-0000-4000-8000-000000000001",
        credential_digest=credential_digest(credential),
        provider_account_id="T12345678:U12345678",
        observed_scopes=["chat:write", "users:read"],
        verified_at=datetime.now(UTC),
    )
    db.add(binding)
    db.add(ToolEntitlement(org_id="org-acme", team_id="", tool_name="slack.post_message", enabled=True))
    await db.commit()
    monkeypatch.setattr(
        tools_knowledge.ToolsPolicyStore,
        "get",
        lambda self, **kwargs: {
            "status": "active",
            "consent_mode": "explicit",
            "allowed_tools": ["slack.post_message"],
            "consented_tools": ["slack.post_message"],
            "tool_max_cost_usd": {"slack.post_message": "1.00"},
            "revision": 1,
        },
    )
    connection = slack_oauth.binding_claim(binding)
    credential_id = credential.id

    def request(run_marker=marker):
        body = request_body(
            operation="post_message",
            connection=connection,
            claims_update={
                "connector": "slack",
                "tool": "slack___post_message",
                "connection_id": credential_id,
                "slack_connection": connection,
                "connection_scopes": ["chat:write"],
                "run_marker": run_marker,
            },
        )
        body["connector"] = "slack"
        return body

    admission = client.post("/internal/v1/tool-adapters/authorize", json=request())
    assert admission.status_code == 200, admission.text
    payload = dispatch_body(admission.json())
    dispatched = client.post("/internal/v1/tool-adapters/dispatch", json=payload)
    assert dispatched.status_code == 200, dispatched.text
    marker_id = hashlib.sha256(
        canonical(
            {
                "tool": "slack___post_message",
                "owner_id": "alice",
                "run_marker": marker,
            }
        )
    ).hexdigest()
    assert await db.get(OAuthToolReceipt, marker_id) is not None
    assert client.post("/internal/v1/tool-adapters/dispatch", json=payload).status_code == 403
    retry = client.post("/internal/v1/tool-adapters/authorize", json=request())
    assert retry.status_code == 200
    assert client.post("/internal/v1/tool-adapters/dispatch", json=dispatch_body(retry.json())).status_code == 409
    wrong = client.post("/internal/v1/tool-adapters/authorize", json=request("22222222-2222-4222-8222-222222222222"))
    assert wrong.status_code == 200
    assert client.post("/internal/v1/tool-adapters/dispatch", json=dispatch_body(wrong.json())).status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("connector", ["github", "jira", "slack"])
async def test_legacy_catalogue_matches_existing_gateway_and_never_publishes_partial(authority, monkeypatch, connector):
    from src.internal import tools_dynamic_catalogue, tools_github, tools_jira, tools_slack

    client, _, _, _, _ = authority
    original_iam = client.app.dependency_overrides[verify_internal_or_irsa]

    async def observation_iam(request: Request):
        await original_iam(request)
        request.state.token_context.credential_scopes.add("tools:adapter:observe")

    client.app.dependency_overrides[verify_internal_or_irsa] = observation_iam
    monkeypatch.setenv("ADP_TOOL_ADAPTER_LEGACY_ENABLED", "true")
    monkeypatch.setenv("ADP_TOOLS_" + connector.upper() + "_SOURCE_REVISION", RELEASE["source_revision"])
    monkeypatch.setenv("AGENT_AUTHORITY_TABLE", "fixture")
    monkeypatch.setattr(tools_knowledge, "current_binding", lambda **kwargs: {"target_name": connector})
    published = []
    monkeypatch.setattr(tools_dynamic_catalogue, "publish_catalogue", lambda **kwargs: published.append(kwargs))
    tools = {"github": tools_github.TOOLS, "jira": tools_jira.TOOLS, "slack": tools_slack.TOOLS}[connector]
    if connector == "slack":
        tools = tools[:1]
    payload = {"schema_version": "1.0", "release": RELEASE, "connector": connector, "tools": tools}
    response = client.post("/internal/v1/tool-adapters/catalogue/legacy", json=payload)
    assert response.status_code == 200, response.text
    assert len(published) == 1 and published[0]["tools"] == tools
    for changed in (
        {**payload, "tools": tools + [{"name": "unreviewed", "description": "wrong", "inputSchema": {"type": "object"}}]},
        {**payload, "release": {**RELEASE, "backend_generation": RELEASE["backend_generation"] + 1}},
    ):
        denied = client.post("/internal/v1/tool-adapters/catalogue/legacy", json=changed)
        assert denied.status_code in {403, 503}
        assert len(published) == 1


@pytest.mark.asyncio
async def test_jira_reconciliation_requires_signed_original_generation_and_current_read_scope(authority, monkeypatch):
    client, db, credential, connection, _ = authority
    monkeypatch.setenv("ADP_TOOLS_RECONCILER_ROLE_ARN", "arn:aws:iam::123456789012:role/adp-reconciler")
    monkeypatch.setenv("ADP_TOOLS_RECONCILER_KEY_ARN", KEY)
    read_arguments = {"cloudId": "site-1", "issueIdOrKey": "ISSUE-42"}
    binding = {
        "operation_id": "OP#org-acme#alice#atlassian___update_jira_issue#request-one",
        "receipt_id": "receipt-one",
        "payload_sha256": "b" * 64,
        "release": RELEASE,
    }
    original = {
        "operation_id": binding["operation_id"],
        "receipt_id": binding["receipt_id"],
        "payload_sha256": binding["payload_sha256"],
        "tool": "atlassian___update_jira_issue",
        "request_id": "request-one",
        "release": RELEASE,
        "connection": {"id": credential.id, "revision": connection.revision, "site_id": "site-1"},
    }
    claims = {
        "purpose": "reconcile",
        "audience": "tool-adapters:reconcile",
        "caller_role_arn": "arn:aws:iam::123456789012:role/adp-reconciler",
        "principal": {"tenant_id": "org-acme", "user_id": "alice", "team_id": "team-eng"},
        "connection": original["connection"],
        "atlassian_operation": original,
        "reconcile_tool": "get_jira_issue",
        "read_arguments_sha256": hashlib.sha256(canonical(read_arguments)).hexdigest(),
        "deadline_at_ms": int(time.time() * 1000) + 5000,
    }

    def send(overrides=None, arguments=None, bind=None):
        candidate = {**claims, **(overrides or {})}
        signature = hmac.digest(b"fixture-key", canonical(candidate), "sha256")
        body = {
            "schema_version": "1.0",
            "purpose": "reconcile",
            "connector": "atlassian",
            "operation": "get_jira_issue",
            "binding": bind or binding,
            "read_arguments": arguments or read_arguments,
            "signed_assertion": encode(canonical({"claims": candidate, "signature": encode(signature)})),
            "deadline_at_ms": claims["deadline_at_ms"],
        }
        return client.post("/internal/v1/tool-adapters/authorize", json=body)

    granted = send()
    assert granted.status_code == 200, granted.text
    assert granted.json()["purpose"] == "reconcile"
    assert granted.json()["context"]["scopes"] == credential.scopes["oauth_scopes"]
    assert granted.json()["binding"]["release"]["backend_generation"] == 1
    for denied in (
        send(arguments={"cloudId": "site-2", "issueIdOrKey": "ISSUE-42"}),
        send(bind={**binding, "release": {**RELEASE, "backend_generation": 2}}),
        send(overrides={"principal": {**claims["principal"], "user_id": "bob"}}),
        send(overrides={"read_arguments_sha256": "a" * 64}),
    ):
        assert denied.status_code == 403
    credential.scopes = {"oauth_scopes": ["search:jira:agent-interface"]}
    connection.credential_digest = credential_digest(credential)
    await db.commit()
    assert send().status_code == 403
