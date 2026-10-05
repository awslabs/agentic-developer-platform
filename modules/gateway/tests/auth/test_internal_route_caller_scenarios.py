"""Mounted S10 caller acceptance, real provenance/registry conversion and handlers.

Only provider boundaries are replaced: registry storage and the SQL session.
SQLite executes real route queries/writes. Any attempted DNS or AWS operation fails the test even if application error handling catches it.
"""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import Request
from sqlalchemy import select

from src.shared.database import get_db
from src.shared.models.organization import Organization, User
from src.shared.models.vault import MagicLinkNonce, UserIdentity

_INTERNAL_KEY = "test-internal-api-key-s10"
_EDGE_SECRET = "test-edge-provenance-secret-s10"
PRIVILEGED_ARN = "arn:aws:sts::123456789012:assumed-role/adp-dev-agent-scaledjob-role/session"
ROLE_ARN = "arn:aws:iam::123456789012:role/adp-dev-agent-scaledjob-role"
HEADERS = {"X-Caller-Identity": PRIVILEGED_ARN, "X-Adp-Edge-Provenance": _EDGE_SECRET}
ENDPOINTS = ["resolve-user", "resolve-installation", "issue-magic-link"]
BODIES = {
    "resolve-user": {"provider": "slack", "provider_user_id": "workspace:user", "org_id": "tenant-a"},
    "resolve-installation": {"installation_id": "fixture-installation"},
    "issue-magic-link": {"provider": "slack", "provider_user_id": "workspace:new-user"},
}


@pytest.fixture(autouse=True)
def no_external_providers(monkeypatch):
    dns = Mock(side_effect=AssertionError("S10 tests must not resolve network addresses"))
    provider = Mock(name="unused AWS provider")
    aws = Mock(return_value=provider)
    monkeypatch.setattr("socket.getaddrinfo", dns)
    monkeypatch.setattr("boto3.client", aws)
    monkeypatch.setattr("boto3.session.Session.client", aws)
    yield
    dns.assert_not_called()
    assert provider.mock_calls == []


@pytest.fixture
async def scenario(db_session, monkeypatch):
    from src.app import create_app
    from src.shared.config import get_settings

    settings = get_settings().model_copy(
        update={
            "trust_apigw_headers": True,
            "apigw_provenance_secret": _EDGE_SECRET,
            "internal_api_key": _INTERNAL_KEY,
            "magic_link_secret": "s10-magic-link-fixture-secret-32-bytes",
            "gateway_base_url": "https://gateway.test",
        }
    )
    for module in ("src.internal.auth_deps", "src.auth.middleware", "src.internal.routes"):
        monkeypatch.setattr(f"{module}.get_settings", lambda: settings)
    monkeypatch.setenv("AGENT_AUTHORITY_ENABLED", "false")
    entry = {
        "agent_id": "scaledjob-worker",
        "agent_name": "scaledjob-worker",
        "role_arn": ROLE_ARN,
        "org_id": "__platform__",
        "team_id": "__agents__",
        "scope": "internal",
        "credential_scopes": ["fixture:read"],
        "status": "active",
    }
    lookup = Mock(side_effect=lambda role: entry if role == ROLE_ARN else None)
    monkeypatch.setattr("src.auth.agent_registry.get_agent_registry_service", lambda: SimpleNamespace(get_agent_by_role_arn=lookup))
    db_session.add(Organization(id="tenant-a", name="S10 tenant", github_installation_ids=["fixture-installation"]))
    db_session.add(User(id="user-a", org_id="tenant-a", team_id="team-a", email="s10@example.test", is_shadow=False))
    await db_session.flush()
    db_session.add(
        UserIdentity(
            user_id="user-a", org_id="tenant-a", team_id="team-a", provider="slack", provider_user_id="workspace:user", verification_method="oauth"
        )
    )
    await db_session.commit()
    contexts = []

    async def local_db(request: Request):
        try:
            yield db_session
        finally:
            contexts.append(getattr(request.state, "token_context", None))

    @asynccontextmanager
    async def audit_session():
        yield db_session

    monkeypatch.setattr("src.admin.middleware.get_session_factory", lambda: audit_session)
    app = create_app()
    app.dependency_overrides[get_db] = local_db
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://gateway.test") as client:
        yield SimpleNamespace(app=app, client=client, entry=entry, lookup=lookup, contexts=contexts, db=db_session)


async def request(scenario, endpoint, headers):
    return await scenario.client.post(f"/internal/v1/{endpoint}", json=BODIES[endpoint], headers=headers)


@pytest.mark.parametrize("endpoint", ENDPOINTS)
@pytest.mark.parametrize(
    "headers,error",
    [
        ({}, "verified IAM caller identity required"),
        ({"Authorization": "Bearer human-session"}, "verified IAM caller identity required"),
        ({"X-Caller-Identity": PRIVILEGED_ARN}, "invalid_caller_identity"),
        ({**HEADERS, "X-Adp-Edge-Provenance": "forged"}, "invalid_caller_identity"),
        ({"X-Caller-Identity": PRIVILEGED_ARN, "X-Internal-Api-Key": _INTERNAL_KEY}, "invalid_caller_identity"),
    ],
)
async def test_anonymous_human_and_forged_callers_rejected(scenario, endpoint, headers, error):
    response = await request(scenario, endpoint, headers)
    assert response.status_code == 403, response.text
    detail = response.json()["detail"]
    assert (detail["error"] if isinstance(detail, dict) else detail) == error
    scenario.lookup.assert_not_called()
    assert all(context is None for context in scenario.contexts)


@pytest.mark.parametrize("endpoint", ENDPOINTS)
@pytest.mark.parametrize("scope", ["shared", "personal", "", "admin", "External"])
async def test_registered_other_tenant_cannot_claim_internal_scope(scenario, endpoint, scope):
    scenario.entry.update(org_id="tenant-b", team_id="team-b", scope=scope)
    response = await request(scenario, endpoint, {**HEADERS, "X-Agent-OrgId": "__platform__", "X-Agent-Scope": "internal"})
    assert response.status_code == 403, response.text
    assert response.json()["detail"]["error"] == "not_internal_plane"
    scenario.lookup.assert_called_once_with(ROLE_ARN)
    assert all(context is None for context in scenario.contexts)


@pytest.mark.parametrize("endpoint", ENDPOINTS)
@pytest.mark.parametrize("mode", ["internal", "platform"])
async def test_valid_caller_reaches_real_handler_with_verified_context(scenario, endpoint, mode):
    scenario.entry["scope"] = mode
    scenario.entry["credential_scopes"] = [
        "internal:identity:resolve",
        "internal:identity:link",
        "internal:installation:resolve",
        "internal:cross-tenant",
    ]
    headers = {**HEADERS, "X-Agent-OrgId": "attacker-attribution", "X-Agent-UserId": "attacker-user"}
    response = await request(scenario, endpoint, headers)
    if endpoint == "resolve-user":
        assert response.status_code == 200, response.text
        assert response.json() == {"user_id": "user-a", "org_id": "tenant-a", "team_id": "team-a", "is_shadow": False, "verification_method": "oauth"}
    elif endpoint == "resolve-installation":
        assert response.status_code == 200, response.text
        assert response.json() == {"tenant_id": "tenant-a", "created_via": "operator", "revocation_checked": True}
    else:
        assert response.status_code == 201, response.text
        assert response.json()["magic_link_url"].startswith("https://gateway.test/auth/link/magic?token=")
        nonce = (await scenario.db.execute(select(MagicLinkNonce))).scalar_one()
        assert nonce.provider == "slack" and nonce.provider_user_id == "workspace:new-user"
    assert len(scenario.contexts) == 1
    context = scenario.contexts[0]
    scenario.lookup.assert_called_once_with(ROLE_ARN)
    assert context.user_id == "iam-agent:scaledjob-worker"
    assert context.org_id == "__platform__" and context.team_id == "__agents__"
    assert context.auth_source == "iam" and context.scope == mode
    assert context.agent_registry_id == "scaledjob-worker"
    assert context.credential_scopes == scenario.entry["credential_scopes"]
    assert not context.is_admin


@pytest.mark.parametrize("endpoint", ENDPOINTS)
async def test_shared_secret_cannot_enter_machine_plane(scenario, endpoint):
    response = await request(scenario, endpoint, {"X-Internal-Api-Key": _INTERNAL_KEY})
    assert response.status_code == 403
    assert response.json()["detail"] == "verified IAM caller identity required"
    scenario.lookup.assert_not_called()


@pytest.mark.parametrize("endpoint", ENDPOINTS)
async def test_registered_caller_without_operation_grant_denied(scenario, endpoint):
    response = await request(scenario, endpoint, HEADERS)
    assert response.status_code == 403
    assert response.json()["detail"] == "internal service capability required"


@pytest.mark.parametrize("endpoint", ENDPOINTS)
async def test_verified_human_session_cannot_enter_machine_plane(scenario, endpoint, monkeypatch):
    from datetime import UTC, datetime, timedelta

    import jwt

    from src.auth.cognito_jwt import CognitoJWTValidator
    from src.auth.dependencies import get_current_user

    key = "s10-local-jwt-provider-key-at-least-32-bytes"
    token = jwt.encode(
        {
            "sub": "human-a",
            "iss": "https://issuer.test",
            "token_use": "access",
            "custom:org_id": "tenant-a",
            "custom:team_id": "team-a",
            "exp": datetime.now(UTC) + timedelta(minutes=5),
        },
        key,
        algorithm="HS256",
    )
    validator = Mock()
    validator.validate_token.side_effect = lambda value: CognitoJWTValidator._parse_claims(validator, jwt.decode(value, key, algorithms=["HS256"]))
    monkeypatch.setattr("src.auth.dependencies._get_cognito_validator", lambda: validator)
    # This scenario isolates machine-plane classification; revocation is covered
    # with real membership rows in test_membership_revocation.py.
    monkeypatch.setattr("src.admin.membership_revocation.require_not_revoked_context", AsyncMock())
    # Exercise the actual human dependency with a cryptographically verified token
    # before presenting that same token to the real internal route.
    human_request = Request({"type": "http", "method": "GET", "path": "/human-control", "headers": []})
    human = await get_current_user(human_request, authorization=f"Bearer {token}")
    assert human.account_type == "human" and human.user_id == "human-a"
    assert human.org_id == "tenant-a"
    response = await request(scenario, endpoint, {"Authorization": f"Bearer {token}"})
    assert response.status_code == 403 and response.json()["detail"] == "verified IAM caller identity required"
    scenario.lookup.assert_not_called()
    validator.validate_token.assert_called_once_with(token)


DISPATCH = "b5e9835b-fc24-4231-96f2-e8b8ca3681be"
ADAPTER_CASES = [
    ("dispatch/claim", {"dispatch_id": DISPATCH}, DISPATCH),
    ("dispatch/settle", {"dispatch_id": DISPATCH, "lease_token": "lease", "publication_outcome": "confirmed", "sqs_message_id": "message"}, DISPATCH),
    ("recovery/claim", {"shard": "v1#00", "cursor": None, "limit": 1}, "v1#00"),
    (
        "recovery/settle",
        {
            "work_id": DISPATCH,
            "lease_token": "lease",
            "evidence": {
                "kind": "publication",
                "observed": True,
                "observed_at": "2026-09-25T00:00:00Z",
            },
        },
        DISPATCH,
    ),
]


@pytest.mark.parametrize("path,body,identity", ADAPTER_CASES)
@pytest.mark.parametrize("proof_kind", ["malformed", "sts-refused"])
async def test_mounted_adapter_exception_requires_real_producer_proof(scenario, monkeypatch, path, body, identity, proof_kind):
    from src.agentauth import task_dispatch_routes
    from tests.agentauth.test_work_producer import proof

    runtime = SimpleNamespace(
        env={
            "ADP_TASK_API_ADMISSION_ENABLED": "true",
            "ADP_TASK_API_RECOVERY_ENABLED": "true",
            "ADP_TASK_DISPATCH_PRODUCER_ROLES": ROLE_ARN,
            "ADP_TASK_RECOVERY_PRODUCER_ROLES": ROLE_ARN,
        }
    )
    store = Mock()
    scenario.app.dependency_overrides[task_dispatch_routes.get_agent_runtime] = lambda: runtime
    scenario.app.dependency_overrides[task_dispatch_routes.work_store] = lambda: store
    requests = []

    def sts_response(request):
        requests.append(request)
        assert request.url == "https://sts.us-east-1.amazonaws.com/"
        assert request.content == b"Action=GetCallerIdentity&Version=2011-06-15"
        return httpx.Response(403, text="signature rejected by fixture STS")

    original = httpx.AsyncClient
    monkeypatch.setattr(
        "src.agentauth.work_routes.httpx.AsyncClient",
        lambda **kwargs: original(
            transport=httpx.MockTransport(sts_response),
            **kwargs,
        ),
    )
    response = await scenario.client.post(
        f"/internal/v1/tasks/{path}",
        json={
            "schema_version": "1.0",
            **body,
            "producer_proof": "not-base64" if proof_kind == "malformed" else proof(identity),
        },
        headers={"X-Internal-Api-Key": _INTERNAL_KEY, **HEADERS},
    )
    assert response.status_code == 403 and response.json() == {"detail": "forbidden"}
    assert len(requests) == (1 if proof_kind == "sts-refused" else 0)
    assert store.mock_calls == []
