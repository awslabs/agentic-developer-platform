"""Identity consent is bound to a signed-in ADP owner, not a caller-supplied ID."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.admin.connections import oauth_apps
from src.auth import atlassian_mcp_routes, connections_routes
from src.auth.dependencies import get_current_user
from src.auth.middleware import get_current_user_context
from src.shared.database import get_db
from src.shared.models.audit import AuditLog
from src.shared.models.base import Base
from src.shared.models.organization import Department, Organization, Team, User
from src.shared.models.vault import OAuthConnectionAttempt, OAuthConnectionBinding, UserCredential
from src.shared.schemas.auth import TokenContext


def caller(user_id="user-alice", org_id="org-acme", account_type="human"):
    return TokenContext(
        user_id=user_id,
        org_id=org_id,
        team_id="team-eng",
        department_id="dept-eng",
        account_type=account_type,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        db.add(
            Organization(
                id="org-acme", name="Acme Corp", aws_accounts=[], role_mappings={}, settings={}, github_installation_ids=[], cognito_client_ids=[]
            )
        )
        db.add(Department(id="dept-eng", org_id="org-acme", name="Engineering"))
        db.add(Team(id="team-eng", org_id="org-acme", department_id="dept-eng", name="Eng Team"))
        db.add(User(id="user-alice", org_id="org-acme", team_id="team-eng", email="alice@example.com"))
        db.add(User(id="user-bob", org_id="org-acme", team_id="team-eng", email="bob@example.com"))
        await db.commit()
        yield db
    await engine.dispose()


@pytest.fixture
def api(session, monkeypatch):
    monkeypatch.setattr(
        connections_routes,
        "get_settings",
        lambda: SimpleNamespace(
            oauth_workload_name="adp-tools-dev-oauth-user", oauth_return_url="https://example.com/api/auth/connections/oauth/return"
        ),
    )
    from src.internal import credential_routes, github_oauth, jira_provider

    monkeypatch.setattr(credential_routes, "_identity_access_token", AsyncMock(return_value="synthetic-owner-token"))
    monkeypatch.setattr(
        github_oauth, "observe_github_user", AsyncMock(return_value=github_oauth.GithubObservation("213", ("read:user",), {"id": 213}))
    )
    monkeypatch.setattr(jira_provider, "observe_jira_user", AsyncMock(return_value=jira_provider.JiraUserObservation("fixture-account")))
    monkeypatch.setattr(
        jira_provider,
        "observe_jira_sites",
        AsyncMock(return_value=(jira_provider.JiraSiteObservation("00000000-0000-4000-8000-000000000001", ("read:jira-work",)),)),
    )
    identity = MagicMock()
    identity.get_workload_access_token_for_user_id.return_value = {"workloadAccessToken": "private-workload-token"}
    identity.get_resource_oauth2_token.return_value = {"authorizationUrl": "https://jira.example.com/authorize", "sessionUri": "opaque-session"}
    app = FastAPI()
    app.include_router(connections_routes.router)
    app.include_router(atlassian_mcp_routes.router)

    async def db_override():
        yield session

    app.dependency_overrides[get_db] = db_override
    app.dependency_overrides[get_current_user_context] = lambda: caller()
    app.dependency_overrides[connections_routes.get_identity_client] = lambda: identity
    return TestClient(app, raise_server_exceptions=False), app, identity


@pytest.mark.asyncio
async def test_atlassian_mcp_status_requires_current_human_and_never_changes_setup(api):
    from src.shared.identity.oauth_setup import get_oauth_setup

    client, app, _ = api
    setup = MagicMock()
    app.dependency_overrides[get_oauth_setup] = lambda: setup
    for status in (
        {"status": "setup_required", "owner": "platform_admin", "action": "Complete setup"},
        {"status": "policy_blocked", "owner": "atlassian_organization_admin", "action": "Allow client"},
        {"status": "ready", "owner": None, "action": None},
    ):
        setup.atlassian_mcp_status.return_value = status
        response = client.get("/auth/connections/atlassian-mcp/status")
        assert response.status_code == 200
        if status["status"] == "ready":
            assert response.json()["status"] == "consent_required"
        else:
            assert response.json() == status
        assert "secret" not in response.text
    setup.prepare_atlassian_mcp.assert_not_called()
    app.dependency_overrides[get_current_user_context] = lambda: caller(account_type="service")
    response = client.get("/auth/connections/atlassian-mcp/status")
    assert response.status_code == 403
    assert setup.atlassian_mcp_status.call_count == 3
    assert client.post("/auth/connections/atlassian-mcp/status").status_code == 405
    assert client.post("/auth/connections/atlassian-mcp/prepare").status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reply",
    [
        {},
        {"authorizationUrl": "https://example.test/authorize"},
        {"sessionUri": "opaque-session"},
        {"sessionStatus": "FAILED", "authorizationUrl": "https://example.test/authorize", "sessionUri": "opaque-session"},
        {"sessionStatus": "COMPLETED", "authorizationUrl": "https://example.test/authorize", "sessionUri": "opaque-session"},
    ],
)
async def test_atlassian_mcp_start_refuses_incomplete_or_terminal_identity_session(api, session, monkeypatch, reply):
    from src.shared.identity.oauth_setup import get_oauth_setup

    client, app, identity = api
    setup = MagicMock()
    setup.settings.oauth_workload_name = "adp-tools-dev-oauth-user"
    app.dependency_overrides[get_oauth_setup] = lambda: setup
    monkeypatch.setattr(
        atlassian_mcp_routes,
        "_consent_plan",
        AsyncMock(
            return_value={
                "return_url": "https://example.test/api/auth/connections/atlassian-mcp/return",
                "metadata_sha256": "a" * 64,
            }
        ),
    )
    identity.get_resource_oauth2_token.return_value = reply
    response = client.post("/auth/connections/atlassian-mcp/start", json={"capability_set": "reads"})
    assert response.status_code == 502
    assert response.json() == {"detail": {"error": "identity_unavailable"}}
    assert (await session.scalars(select(OAuthConnectionAttempt))).all() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("bootstrap", [False, True])
@pytest.mark.parametrize("identity_status", [None, "IN_PROGRESS"])
async def test_atlassian_mcp_consent_is_isolated_from_legacy_and_requires_observation(
    api, session, monkeypatch, record_property, bootstrap, identity_status
):
    client, app, identity = api
    from src.shared.identity.oauth_setup import get_oauth_setup

    setup = MagicMock()
    setup.settings = SimpleNamespace(
        oauth_workload_name="adp-tools-dev-oauth-user",
        atlassian_identity_compatibility_verified=not bootstrap,
        atlassian_metadata_sha256="a" * 64,
    )
    setup.atlassian_mcp_status.return_value = {"status": "consent_required" if bootstrap else "ready"}
    app.dependency_overrides[get_oauth_setup] = lambda: setup
    monkeypatch.setattr(atlassian_mcp_routes, "read_tenant_policy_observation", AsyncMock(return_value=None if bootstrap else "allowed"))
    monkeypatch.setattr(
        atlassian_mcp_routes,
        "atlassian_mcp_config",
        lambda settings, secret_store: {"return_url": "https://example.test/api/auth/connections/atlassian-mcp/return", "metadata_sha256": "a" * 64},
    )
    legacy = client.post("/auth/connections/oauth/atlassian/start").json()
    assert client.post("/auth/connections/oauth/return", json={"state": legacy["state"], "session_uri": legacy["session_uri"]}).status_code == 201
    identity.complete_resource_token_auth.reset_mock()
    identity.get_resource_oauth2_token.return_value = {
        **({"sessionStatus": identity_status} if identity_status is not None else {}),
        "authorizationUrl": "https://example.test/authorize",
        "sessionUri": "opaque-session",
    }

    started = client.post("/auth/connections/atlassian-mcp/start", json={"capability_set": "reads"})
    assert started.status_code == 200, started.text
    params = identity.get_resource_oauth2_token.call_args.kwargs
    assert params["resourceCredentialProviderName"] == "adp-tools-dev-atlassian-mcp-user"
    assert params["resources"] == ["https://mcp.atlassian.com/v2/mcp"]
    assert params["resourceOauth2ReturnUrl"].endswith("/atlassian-mcp/return")
    assert "read:jira-work" not in params["scopes"]
    assert "write:jira:agent-interface" not in params["scopes"]
    callback = {"state": started.json()["state"], "session_uri": started.json()["session_uri"]}
    browser_callback = {"state": callback["state"], "session_sha256": hashlib.sha256(callback["session_uri"].encode()).hexdigest()}
    status_path = "/auth/connections/atlassian-mcp/attempts/" + started.json()["attempt_id"]
    assert client.get(status_path).json() == {"status": "pending"}
    returned = client.get(
        "/auth/connections/atlassian-mcp/return", params={"state": callback["state"], "session_id": callback["session_uri"]}, follow_redirects=False
    )
    assert returned.status_code == 303
    assert returned.headers["location"] == (
        f"/settings/connections?oauth_consent=returned&atlassian_state={callback['state']}"
        f"&atlassian_session_sha256={browser_callback['session_sha256']}"
    )
    assert callback["session_uri"] not in returned.headers["location"]
    for query in (
        [("state", callback["state"]), ("session_id", callback["session_uri"]), ("state", callback["state"])],
        [("state", callback["state"]), ("session_id", callback["session_uri"]), ("session_id", "different")],
        [("state", callback["state"])],
    ):
        refused = client.get("/auth/connections/atlassian-mcp/return", params=query, follow_redirects=False)
        assert refused.headers["location"] == "/settings/connections?oauth_consent=error"
    assert client.post("/auth/connections/atlassian-mcp/complete-return", json={**browser_callback, "session_sha256": "0" * 64}).status_code == 403
    identity.complete_resource_token_auth.assert_not_called()
    assert client.post("/auth/connections/atlassian-mcp/complete", json={**callback, "session_uri": "other-session"}).status_code == 403
    app.dependency_overrides[get_current_user_context] = lambda: caller(user_id="user-bob")
    assert client.post("/auth/connections/atlassian-mcp/complete", json=callback).status_code == 403
    assert client.post("/auth/connections/atlassian-mcp/complete-return", json=browser_callback).status_code == 403
    assert client.get(status_path).status_code == 404
    app.dependency_overrides[get_current_user_context] = lambda: caller()
    assert client.post("/auth/connections/atlassian-mcp/complete", json=callback).status_code == 503
    assert (await session.execute(select(UserCredential).where(UserCredential.service == "atlassian-mcp"))).scalars().all() == []
    attempt = await session.get(OAuthConnectionAttempt, started.json()["attempt_id"])
    assert attempt.identity_completed_at is not None and attempt.consumed_at is None
    assert client.get(status_path).json() == {"status": "observing"}
    identity.complete_resource_token_auth.assert_called_once_with(userIdentifier={"userId": "user-alice"}, sessionUri="opaque-session")

    async def observed(*, attempt, user_id, tenant_id):
        assert user_id == "user-alice" and tenant_id == "org-acme"
        assert attempt.state_digest == started.json()["attempt_id"]
        return SimpleNamespace(
            attempt_id=attempt.state_digest,
            config_revision="a" * 64,
            purpose="connection_observe",
            provider_user_id="atlassian-user-1",
            sites=(
                SimpleNamespace(site_id="site-1", scopes=("read:jira:agent-interface",)),
                SimpleNamespace(site_id="site-2", scopes=("read:confluence:agent-interface",)),
            ),
            granted_scopes=tuple(atlassian_mcp_routes.SCOPES),
        )

    monkeypatch.setattr(atlassian_mcp_routes, "observe_connection", observed)
    completed = (
        client.post("/auth/connections/atlassian-mcp/complete-return", json=browser_callback)
        if bootstrap
        else client.post("/auth/connections/atlassian-mcp/complete", json=callback)
    )
    assert completed.status_code == 201, completed.text
    identity.complete_resource_token_auth.assert_called_once()
    assert completed.json()["provider"] == "agentcore-identity"
    assert client.get("/auth/connections/atlassian-mcp/status").json()["status"] == "ready"
    assert client.get(status_path).json() == {"status": "completed", "id": completed.json()["id"], "revision": completed.json()["revision"]}
    assert client.post("/auth/connections/atlassian-mcp/complete-return", json=browser_callback).status_code == 403
    assert client.post("/auth/connections/atlassian-mcp/complete", json=callback).status_code == 403
    rows = (await session.execute(select(UserCredential).where(UserCredential.user_id == "user-alice"))).scalars().all()
    assert {row.service for row in rows} == {"jira", "atlassian-mcp"}
    binding = await session.get(OAuthConnectionBinding, completed.json()["id"])
    assert binding.provider_account_id == "atlassian-user-1"
    assert binding.observed_resources == [
        {"site_id": "site-1", "scopes": ["read:jira:agent-interface"]},
        {"site_id": "site-2", "scopes": ["read:confluence:agent-interface"]},
    ]
    assert binding.selected_site_id is None
    chosen = client.post("/auth/connections/atlassian-mcp/select-site", json={"connection_revision": binding.revision, "site_id": "site-2"})
    assert chosen.status_code == 200, chosen.text
    assert chosen.json()["selected_site_id"] == "site-2"
    assert chosen.json()["revision"] != completed.json()["revision"]
    assert client.get("/auth/connections/atlassian-mcp/status").json()["connection"]["selected_site_id"] == "site-2"
    assert client.get("/auth/connections/atlassian-mcp/status").json()["status"] == "ready"
    setup.settings.atlassian_metadata_sha256 = "b" * 64
    assert client.get("/auth/connections/atlassian-mcp/status").json()["status"] == "consent_required"
    setup.settings.atlassian_metadata_sha256 = "a" * 64
    app.dependency_overrides[get_current_user_context] = lambda: caller(user_id="user-bob")
    bob_site = {"connection_revision": chosen.json()["revision"], "site_id": "site-2"}
    assert client.post("/auth/connections/atlassian-mcp/select-site", json=bob_site).status_code == 403
    app.dependency_overrides[get_current_user_context] = lambda: caller()
    stale = {"connection_revision": completed.json()["revision"], "site_id": "site-1"}
    unknown = {"connection_revision": chosen.json()["revision"], "site_id": "site-3"}
    assert client.post("/auth/connections/atlassian-mcp/select-site", json=stale).status_code == 409
    assert client.post("/auth/connections/atlassian-mcp/select-site", json=unknown).status_code == 403
    binding.observed_resources = [*binding.observed_resources, {"site_id": "site-unscoped", "scopes": []}]
    await session.commit()
    unscoped = {"connection_revision": chosen.json()["revision"], "site_id": "site-unscoped"}
    assert client.post("/auth/connections/atlassian-mcp/select-site", json=unscoped).status_code == 403
    assert binding.selected_site_id == "site-2"
    assert "opaque-session" not in str(rows) + str(binding.__dict__)
    assert client.post("/auth/connections/atlassian-mcp/start", json={"capability_set": "reads"}).status_code == 409
    assert client.post("/auth/connections/atlassian-mcp/start", json={"capability_set": "jira-writes"}).status_code == 409
    record_property(
        "identity_observation",
        json.dumps(
            {
                "owner": binding.user_id,
                "tenant": binding.org_id,
                "attempt": started.json()["attempt_id"],
                "site": binding.selected_site_id,
                "provider_calls": identity.complete_resource_token_auth.call_count,
                "token_persisted": "opaque-session" in str(rows) + str(binding.__dict__),
                "foreign_site_status": 403,
                "stale_site_status": 409,
            }
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ("foreign_owner", "foreign_tenant", "changed_site", "callback_site", "wrong_session"))
async def test_atlassian_mcp_binding_denials_before_identity_or_provider(api, session, monkeypatch, fault, record_property):
    from src.internal.github_oauth import credential_digest
    from src.shared.identity.oauth_setup import get_oauth_setup

    client, app, identity = api
    setup = MagicMock()
    setup.settings = SimpleNamespace(
        oauth_workload_name="adp-tools-dev-oauth-user",
        atlassian_identity_compatibility_verified=True,
    )
    setup.atlassian_mcp_status.return_value = {"status": "ready"}
    app.dependency_overrides[get_oauth_setup] = lambda: setup
    monkeypatch.setattr(atlassian_mcp_routes, "read_tenant_policy_observation", AsyncMock(return_value="allowed"))
    monkeypatch.setattr(
        atlassian_mcp_routes,
        "atlassian_mcp_config",
        lambda settings, secret_store: {
            "return_url": "https://example.test/api/auth/connections/atlassian-mcp/return",
            "metadata_sha256": "a" * 64,
        },
    )
    observation = AsyncMock(side_effect=AssertionError("callback must not reach the adapter"))
    monkeypatch.setattr(atlassian_mcp_routes, "observe_connection", observation)
    identity.get_resource_oauth2_token.return_value = {
        "sessionStatus": "IN_PROGRESS",
        "authorizationUrl": "https://example.test/authorize",
        "sessionUri": "opaque-session",
    }
    reconnect_revision = None
    if fault == "changed_site":
        credential = UserCredential(
            id="owned-atlassian",
            org_id="org-acme",
            user_id="user-alice",
            service="atlassian-mcp",
            provider="agentcore-identity",
            credential_type="oauth_token",
            label="Atlassian MCP OAuth",
            secret_arn="agentcore-identity:adp-tools-dev-atlassian-mcp-oauth-user:atlassian-mcp",
            scopes={"oauth_scopes": list(atlassian_mcp_routes.SCOPES)},
            strict=True,
            agent_consent=True,
        )
        session.add(credential)
        await session.flush()
        binding = OAuthConnectionBinding(
            credential_id=credential.id,
            org_id="org-acme",
            user_id="user-alice",
            provider="atlassian-mcp",
            revision="initial-revision",
            credential_digest=credential_digest(credential),
            provider_account_id="subject-alice",
            observed_scopes=list(atlassian_mcp_routes.SCOPES),
            observed_resources=[
                {"site_id": "site-1", "scopes": ["read:jira:agent-interface"]},
                {"site_id": "site-2", "scopes": ["read:confluence:agent-interface"]},
            ],
            selected_site_id="site-1",
            verified_at=datetime.now(UTC),
        )
        session.add(binding)
        await session.commit()
        reconnect_revision = binding.revision

    started = client.post(
        "/auth/connections/atlassian-mcp/start",
        json={"capability_set": "reads", **({"reconnect_revision": reconnect_revision} if reconnect_revision else {})},
    )
    assert started.status_code == 200, started.text
    attempt = (await session.execute(select(OAuthConnectionAttempt).where(OAuthConnectionAttempt.provider == "atlassian-mcp"))).scalar_one()
    callback = {"state": started.json()["state"], "session_uri": started.json()["session_uri"]}
    before = (
        identity.get_workload_access_token_for_user_id.call_count,
        identity.get_resource_oauth2_token.call_count,
        identity.complete_resource_token_auth.call_count,
    )
    if fault == "foreign_owner":
        app.dependency_overrides[get_current_user_context] = lambda: caller("user-bob")
    elif fault == "foreign_tenant":
        app.dependency_overrides[get_current_user_context] = lambda: caller(org_id="another-tenant")
    elif fault == "changed_site":
        changed = client.post(
            "/auth/connections/atlassian-mcp/select-site",
            json={"connection_revision": reconnect_revision, "site_id": "site-2"},
        )
        assert changed.status_code == 200, changed.text
        assert changed.json()["revision"] != reconnect_revision
    elif fault == "callback_site":
        callback["site_id"] = "site-2"
    else:
        callback["session_uri"] = "another-session"
    refused = client.post("/auth/connections/atlassian-mcp/complete", json=callback)
    assert refused.status_code == {"changed_site": 409, "callback_site": 422}.get(fault, 403), refused.text
    assert (
        identity.get_workload_access_token_for_user_id.call_count,
        identity.get_resource_oauth2_token.call_count,
        identity.complete_resource_token_auth.call_count,
    ) == before
    observation.assert_not_awaited()
    assert attempt.identity_completed_at is None and attempt.consumed_at is None
    assert (await session.execute(select(UserCredential).where(UserCredential.service == "atlassian-mcp"))).scalars().all() == (
        [credential] if fault == "changed_site" else []
    )
    record_property(
        "identity_observation",
        json.dumps(
            {
                "fault": fault,
                "status": refused.status_code,
                "provider_calls_before": before,
                "provider_calls_after": (
                    identity.get_workload_access_token_for_user_id.call_count,
                    identity.get_resource_oauth2_token.call_count,
                    identity.complete_resource_token_auth.call_count,
                ),
                "observation_calls": observation.await_count,
                "attempt": attempt.state_digest,
            }
        ),
    )


@pytest.mark.asyncio
async def test_atlassian_mcp_adapter_observation_serializes_and_verifies_real_routes(api, session, monkeypatch, record_property):
    import hashlib
    import hmac
    import json
    from pathlib import Path

    import httpx
    from botocore.credentials import Credentials
    from fastapi import Request

    root = Path(__file__).resolve().parents[4]
    monkeypatch.syspath_prepend(str(root / "modules/tools/adapters"))
    monkeypatch.syspath_prepend(str(root / "modules/tools"))
    from adp_tools.adapter_contracts import ObserveRequest
    from tool_adapters import atlassian_observe, observation
    from tool_adapters.config import AdapterConfiguration
    from tool_adapters.server import create_app

    from src.internal import tool_adapter_authority, tool_adapter_observation
    from src.internal.auth_deps import verify_internal_or_irsa
    from src.shared.identity.oauth_setup import get_oauth_setup

    client, app, identity = api
    app.include_router(tool_adapter_authority.router)
    release = {
        "contract_version": "1.0",
        "source_revision": "a" * 40,
        "image_digest": "sha256:" + "b" * 64,
        "group": "atlassian-foundation",
        "group_generation": 1,
        "backend_generation": 1,
    }
    config_revision = "c" * 64
    key = "arn:aws:kms:us-east-1:123456789012:key/11111111-1111-1111-1111-111111111111"
    core_role = "arn:aws:iam::123456789012:role/adp-core"
    adapter_role = "arn:aws:iam::123456789012:role/adp-adapter"
    adapter_url = "https://adapter.example.test/internal/v1/tool-adapters/observe"
    core_url = "https://core.example.test/internal/v1/tool-adapters/authorize"
    tool_schema = hashlib.sha256(b"{}").hexdigest()
    configured = AdapterConfiguration.model_validate(
        {
            "schema_version": "1.0",
            "mode": "observation",
            "release": release,
            "observation": {
                "core_authorize_url": core_url,
                "core_role_arn": core_role,
                "assertion_key_arn": key,
                "edge_provenance_sha256": hashlib.sha256(b"edge-fixture").hexdigest(),
                "identity_workload": "adp-tools-dev-atlassian-mcp-oauth-user",
                "identity_provider": "adp-tools-dev-atlassian-mcp-user",
                "config_revision": config_revision,
                "subject_schema_sha256": tool_schema,
                "sites_schema_sha256": tool_schema,
            },
        }
    )
    adapter_app = create_app(configured, source_revision="a" * 40)
    setup = MagicMock()
    setup.settings = SimpleNamespace(
        oauth_workload_name="adp-tools-dev-oauth-user",
        atlassian_identity_compatibility_verified=True,
    )
    setup.atlassian_mcp_status.return_value = {"status": "ready"}
    app.dependency_overrides[get_oauth_setup] = lambda: setup
    monkeypatch.setattr(atlassian_mcp_routes, "read_tenant_policy_observation", AsyncMock(return_value="allowed"))
    monkeypatch.setattr(
        atlassian_mcp_routes,
        "atlassian_mcp_config",
        lambda settings, secret_store: {
            "return_url": "https://example.test/api/auth/connections/atlassian-mcp/return",
            "metadata_sha256": config_revision,
        },
    )
    monkeypatch.setattr(
        tool_adapter_authority,
        "get_settings",
        lambda: SimpleNamespace(
            atlassian_identity_compatibility_verified=True,
            oauth_workload_name="adp-tools-dev-oauth-user",
        ),
    )
    monkeypatch.setattr(tool_adapter_observation, "get_settings", lambda: SimpleNamespace(aws_region="us-east-1"))
    monkeypatch.setattr(
        tool_adapter_authority,
        "verified_caller_identity",
        lambda request: "arn:aws:sts::123456789012:assumed-role/adp-adapter/fixture",
    )

    async def authorized_adapter(request: Request):
        request.state.token_context = SimpleNamespace(
            auth_source="iam",
            user_id="adapter",
            scope="internal",
            org_id="__adapter__",
            credential_scopes={"tools:adapter:observe", "internal:cross-tenant"},
        )

    app.dependency_overrides[verify_internal_or_irsa] = authorized_adapter
    monkeypatch.setenv("ADP_TOOL_ADAPTER_OBSERVE_URL", adapter_url)
    monkeypatch.setenv("ADP_TOOL_ADAPTER_ASSERTION_KEY_ARN", key)
    monkeypatch.setenv("ADP_TOOL_ADAPTER_CORE_ROLE_ARN", core_role)
    monkeypatch.setenv("ADP_TOOL_ADAPTER_ADAPTER_ROLE_ARN", adapter_role)
    monkeypatch.setenv("ADP_TOOL_ADAPTER_RELEASE", json.dumps(release))
    monkeypatch.setenv("AWS_REGION", "us-east-1")

    class KMS:
        def sign(self, **kwargs):
            assert kwargs["KeyId"] == key and kwargs["MessageType"] == "RAW"
            assert kwargs["SigningAlgorithm"] == "RSASSA_PSS_SHA_256"
            return {"Signature": hmac.digest(b"local-fixture", kwargs["Message"], "sha256")}

        def verify(self, **kwargs):
            return {
                "SignatureValid": kwargs["KeyId"] == key
                and hmac.compare_digest(kwargs["Signature"], hmac.digest(b"local-fixture", kwargs["Message"], "sha256")),
            }

    def sdk_client(service, **kwargs):
        if service == "kms" and "config" in kwargs:
            assert kwargs["config"].retries["total_max_attempts"] == 1
            assert 0 < kwargs["config"].connect_timeout <= 2
            assert 0 < kwargs["config"].read_timeout <= 5
        return {"kms": KMS(), "bedrock-agentcore": identity}[service]

    monkeypatch.setattr(tool_adapter_observation.boto3, "client", sdk_client)
    monkeypatch.setattr(
        tool_adapter_observation.boto3,
        "Session",
        lambda: SimpleNamespace(get_credentials=lambda: Credentials("testing", "testing", "testing")),
    )
    sdk_calls = []

    def resource_token(**kwargs):
        sdk_calls.append(kwargs)
        if kwargs.get("forceAuthentication"):
            return {"sessionStatus": "IN_PROGRESS", "sessionUri": "isolated-session", "authorizationUrl": "https://example.test/consent"}
        return {
            "sessionStatus": "COMPLETED",
            "accessToken": ("bob-token-sentinel" if kwargs["workloadIdentityToken"] == "workload-user-bob" else "transient-token-sentinel"),
        }

    identity.get_resource_oauth2_token.side_effect = resource_token
    identity.get_workload_access_token_for_user_id.side_effect = lambda **kwargs: {"workloadAccessToken": "workload-" + kwargs["userId"]}
    methods = []

    class Provider:
        def __init__(self, *, deadline):
            pass

        def send(self, method, params, token):
            assert token in {"transient-token-sentinel", "bob-token-sentinel"}
            methods.append(method)
            if method == "initialize":
                return {"result": {"protocolVersion": "2025-03-26"}}
            if method == "notifications/initialized":
                return None
            if method == "tools/list":
                return {
                    "result": {
                        "tools": [
                            {"name": "atlassianUserInfo", "inputSchema": {}},
                            {"name": "getAccessibleAtlassianResources", "inputSchema": {}},
                        ]
                    }
                }
            if params["name"] == "atlassianUserInfo":
                return {
                    "result": {
                        "structuredContent": {
                            "accountId": "subject-bob" if token == "bob-token-sentinel" else "subject-1",
                        }
                    }
                }
            resources = [{"id": "site-2", "scopes": ["read:confluence:agent-interface", "search:confluence:agent-interface"]}]
            if token != "bob-token-sentinel":
                resources.append({"id": "site-1", "scopes": ["read:jira:agent-interface", "search:jira:agent-interface"]})
            return {"result": {"structuredContent": {"resources": resources}}}

    async def provider(token, settings):
        return atlassian_observe.collect_observation(token, settings, transport_factory=Provider)

    monkeypatch.setattr(observation, "observe_provider", provider)
    native_post = httpx.AsyncClient.post
    signed_requests = []

    async def private_post(self, url, *, content, headers):
        if url == adapter_url:
            payload = ObserveRequest.model_validate_json(content)
            assert payload.binding.release == configured.release
            signed_requests.append(json.loads(content))
            target = adapter_app
            headers = {**headers, "x-adp-edge-provenance": "edge-fixture", "x-caller-identity": core_role}
        else:
            assert url == core_url
            target = app
            headers = {**headers, "x-caller-identity": adapter_role}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=target), base_url=url) as private:
            return await native_post(private, url, content=content, headers=headers)

    monkeypatch.setattr(httpx.AsyncClient, "post", private_post)
    started = client.post("/auth/connections/atlassian-mcp/start", json={"capability_set": "reads"})
    assert started.status_code == 200, started.text
    result = client.post(
        "/auth/connections/atlassian-mcp/complete",
        json={
            "state": started.json()["state"],
            "session_uri": started.json()["session_uri"],
        },
    )
    assert result.status_code == 201, result.text
    assert len(sdk_calls) == 2
    assert sdk_calls[1]["resourceCredentialProviderName"] == "adp-tools-dev-atlassian-mcp-user"
    assert methods == ["initialize", "notifications/initialized", "tools/list", "tools/call", "tools/call"]
    binding = await session.get(OAuthConnectionBinding, result.json()["id"])
    assert binding.provider_account_id == "subject-1" and binding.selected_site_id is None
    assert {item["site_id"] for item in binding.observed_resources} == {"site-1", "site-2"}
    assert "transient-token-sentinel" not in str(binding.__dict__) + str((await session.execute(select(UserCredential))).scalars().all())
    alice_status = client.get("/auth/connections/atlassian-mcp/status").json()["connection"]
    assert alice_status["selected_site_id"] is None and set(alice_status["sites"]) == {"site-1", "site-2"}
    assert "connection" not in setup.atlassian_mcp_status.return_value
    selected = client.post(
        "/auth/connections/atlassian-mcp/select-site",
        json={
            "connection_revision": result.json()["revision"],
            "site_id": "site-1",
        },
    )
    assert selected.status_code == 200, selected.text
    assert selected.json()["revision"] != result.json()["revision"]
    app.dependency_overrides[get_current_user_context] = lambda: caller(user_id="user-bob")
    assert "connection" not in client.get("/auth/connections/atlassian-mcp/status").json()
    bob_start = client.post("/auth/connections/atlassian-mcp/start", json={"capability_set": "reads"})
    assert bob_start.status_code == 200, bob_start.text
    bob_result = client.post(
        "/auth/connections/atlassian-mcp/complete",
        json={
            "state": bob_start.json()["state"],
            "session_uri": bob_start.json()["session_uri"],
        },
    )
    assert bob_result.status_code == 201, bob_result.text
    bob_binding = await session.get(OAuthConnectionBinding, bob_result.json()["id"])
    assert bob_binding.provider_account_id == "subject-bob" and bob_binding.selected_site_id == "site-2"
    assert set(bob_binding.observed_scopes) == {
        "read:me",
        "read:account",
        "read:confluence:agent-interface",
        "search:confluence:agent-interface",
    }
    assert bob_binding.observed_resources == [
        {
            "site_id": "site-2",
            "scopes": ["read:confluence:agent-interface", "search:confluence:agent-interface"],
        }
    ]
    assert (
        client.post(
            "/auth/connections/atlassian-mcp/select-site",
            json={
                "connection_revision": bob_result.json()["revision"],
                "site_id": "site-1",
            },
        ).status_code
        == 403
    )
    assert client.get("/auth/connections/atlassian-mcp/status").json()["connection"]["sites"] == ["site-2"]
    app.dependency_overrides[get_current_user_context] = lambda: caller()
    assert client.get("/auth/connections/atlassian-mcp/status").json()["connection"]["selected_site_id"] == "site-1"
    assert (await session.get(OAuthConnectionBinding, result.json()["id"])).provider_account_id == "subject-1"
    credentials = (await session.scalars(select(UserCredential).where(UserCredential.service == "atlassian-mcp"))).all()
    assert {row.user_id for row in credentials} == {"user-alice", "user-bob"} and len(credentials) == 2
    assert len(sdk_calls) == 4 and sdk_calls[3]["workloadIdentityToken"] == "workload-user-bob"
    assert "bob-token-sentinel" not in str(bob_binding.__dict__) + str((await session.execute(select(UserCredential))).scalars().all())
    private_headers = {"x-adp-edge-provenance": "edge-fixture", "x-caller-identity": core_role}
    with TestClient(adapter_app, raise_server_exceptions=False) as adapter_client:
        replay = adapter_client.post("/internal/v1/tool-adapters/observe", json=signed_requests[0], headers=private_headers)
        assert replay.status_code == 503
        forged = {**signed_requests[0], "attempt_id": "another-attempt"}
        denied = adapter_client.post("/internal/v1/tool-adapters/observe", json=forged, headers=private_headers)
        assert denied.status_code == 403
    assert len(sdk_calls) == 4
    record_property(
        "identity_observation",
        json.dumps(
            {
                "owner_count": len(credentials),
                "owners": sorted(row.user_id for row in credentials),
                "site": (await session.get(OAuthConnectionBinding, result.json()["id"])).selected_site_id,
                "replay_status": replay.status_code,
                "forged_status": denied.status_code,
                "provider_calls_after_denial": len(sdk_calls),
                "attempt": signed_requests[0]["attempt_id"],
                "generation": release["group_generation"],
                "token_persisted": "bob-token-sentinel" in str(bob_binding.__dict__) + str(credentials),
            }
        ),
    )


@pytest.mark.asyncio
async def test_atlassian_admin_prepare_http_role_boundary(api, monkeypatch):
    from src.shared.identity.oauth_setup import get_oauth_setup

    monkeypatch.setattr(oauth_apps, "atlassian_mcp_config", lambda settings, store: {"registration_mode": "retained-deployment-client"})
    monkeypatch.setattr(oauth_apps, "get_session_factory", lambda: object())
    monkeypatch.setattr(oauth_apps, "process_setup_once", AsyncMock(return_value="resolved"))

    _, app, _ = api
    app.include_router(oauth_apps.router)
    setup = MagicMock()
    setup.prepare_atlassian_mcp.return_value = {"status": "prepared", "return_url": "https://example.test/return"}
    app.dependency_overrides[get_oauth_setup] = lambda: setup
    app.dependency_overrides[get_current_user] = lambda: caller()
    client = TestClient(app, raise_server_exceptions=False)
    denied = client.post("/admin/connections/atlassian-mcp/prepare")
    assert denied.status_code == 403
    setup.prepare_atlassian_mcp.assert_not_called()
    app.dependency_overrides[get_current_user] = lambda: caller().model_copy(update={"is_admin": True})
    permitted = client.post("/admin/connections/atlassian-mcp/prepare")
    assert permitted.status_code == 200, permitted.text
    assert permitted.json()["status"] == "prepared"
    setup.prepare_atlassian_mcp.assert_called_once()


@pytest.mark.asyncio
async def test_jira_consent_creates_only_owner_pointer_after_callback(api, session):
    client, _, identity = api
    started = client.post("/auth/connections/oauth/atlassian/start")
    assert started.status_code == 200, started.text
    assert started.json()["authorization_url"] == "https://jira.example.com/authorize"
    assert started.json()["session_uri"] == "opaque-session"
    params = identity.get_resource_oauth2_token.call_args.kwargs
    assert params["scopes"] == ["read:jira-work", "read:me", "offline_access"]
    assert params["oauth2Flow"] == "USER_FEDERATION"
    assert params["forceAuthentication"] is True
    assert params["resourceOauth2ReturnUrl"] == "https://example.com/api/auth/connections/oauth/return"
    assert params["resourceCredentialProviderName"] == "adp-tools-dev-atlassian-user"
    state = started.json()["state"]
    assert state == params["customState"]
    attempt = (await session.execute(select(OAuthConnectionAttempt))).scalar_one()
    assert attempt.requested_scopes == ["read:jira-work", "read:me", "offline_access"]
    result = client.post("/auth/connections/oauth/return", json={"state": state, "session_uri": "opaque-session"})
    assert result.status_code == 201, result.text
    assert "token" not in result.text
    identity.complete_resource_token_auth.assert_called_once_with(userIdentifier={"userId": "user-alice"}, sessionUri="opaque-session")
    rows = (await session.execute(select(UserCredential))).scalars().all()
    assert len(rows) == 1
    assert rows[0].org_id == "org-acme" and rows[0].user_id == "user-alice"
    assert rows[0].service == "jira" and rows[0].provider == "agentcore-identity"
    assert rows[0].credential_type == "oauth_token" and rows[0].secret_arn.startswith("agentcore-identity:")
    assert rows[0].scopes == {"oauth_scopes": ["read:jira-work", "read:me", "offline_access"]}
    receipts = (await session.execute(select(AuditLog).where(AuditLog.event_type == "oauth_connection_created"))).scalars().all()
    assert len(receipts) == 1
    assert receipts[0].actor_id == "user-alice" and receipts[0].org_id == "org-acme"
    assert receipts[0].details["connection_id"] == rows[0].id
    assert receipts[0].details["provider"] == "atlassian"
    assert "opaque-session" not in str(receipts[0].details)
    from src.auth.vault_schemas import CredentialResponse

    visible = CredentialResponse.from_model(rows[0]).model_dump()
    assert visible["provider"] == "agentcore-identity"
    assert "secret_arn" not in visible
    assert "private-workload-token" not in str(rows[0].__dict__)
    assert client.post("/auth/connections/oauth/return", json={"state": state, "session_uri": "opaque-session"}).status_code == 403


@pytest.mark.asyncio
async def test_callback_rejects_different_owner_and_wrong_session(api, session):
    client, app, identity = api
    client.post("/auth/connections/oauth/github/start")
    state = identity.get_resource_oauth2_token.call_args.kwargs["customState"]
    app.dependency_overrides[get_current_user_context] = lambda: caller("user-bob")
    assert client.post("/auth/connections/oauth/return", json={"state": state, "session_uri": "opaque-session"}).status_code == 403
    app.dependency_overrides[get_current_user_context] = lambda: caller()
    assert client.post("/auth/connections/oauth/return", json={"state": state, "session_uri": "wrong-session"}).status_code == 403
    assert client.post("/auth/connections/oauth/return", json={"state": "unknown", "session_uri": "opaque-session"}).status_code == 403
    identity.complete_resource_token_auth.assert_not_called()
    assert (await session.execute(select(UserCredential))).scalars().all() == []


@pytest.mark.asyncio
async def test_callback_uses_requested_scope_snapshot_not_changed_defaults(api, session, monkeypatch):
    client, _, identity = api
    started = client.post("/auth/connections/oauth/github/start")
    assert started.status_code == 200
    scopes = connections_routes.PROVIDER_SCOPES["github"]
    monkeypatch.setitem(connections_routes.PROVIDER_SCOPES, "github", (scopes[0], ["read:user", "repo"]))
    result = client.post("/auth/connections/oauth/return", json={"state": started.json()["state"], "session_uri": "opaque-session"})
    assert result.status_code == 201
    credential = (await session.execute(select(UserCredential))).scalar_one()
    assert credential.scopes == {"oauth_scopes": ["read:user"]}
    assert identity.get_resource_oauth2_token.call_args.kwargs["scopes"] == ["read:user"]


@pytest.mark.asyncio
async def test_github_observation_is_bound_to_owner_and_removed_at_disconnect(api, session, monkeypatch):
    from src.internal import credential_routes, github_oauth

    monkeypatch.setenv("ADP_TOOLS_GITHUB_MEDIATOR_ENABLED", "true")
    token_exchange = AsyncMock(return_value="synthetic-owner-token")
    observe = AsyncMock(return_value=github_oauth.GithubObservation("213", ("read:user",), {"id": 213}))
    monkeypatch.setattr(credential_routes, "_identity_access_token", token_exchange)
    monkeypatch.setattr(github_oauth, "observe_github_user", observe)
    client, _, _ = api
    started = client.post("/auth/connections/oauth/github/start").json()
    connected = client.post("/auth/connections/oauth/return", json={"state": started["state"], "session_uri": started["session_uri"]})
    assert connected.status_code == 201, connected.text
    binding = (await session.execute(select(OAuthConnectionBinding))).scalar_one()
    assert binding.user_id == "user-alice" and binding.provider_account_id == "213"
    assert binding.observed_scopes == ["read:user"] and binding.credential_id == connected.json()["id"]
    token_exchange.assert_awaited_once()
    assert token_exchange.call_args.args[0].user_id == token_exchange.call_args.args[1].id == "user-alice"
    observe.assert_awaited_once_with("synthetic-owner-token")
    assert "synthetic-owner-token" not in connected.text
    disconnected = client.delete("/auth/connections/oauth/github")
    assert disconnected.status_code == 200
    assert disconnected.json() == {
        "status": "disconnected",
        "identity_token_revoked": False,
        "provider_access_token_expires_at": None,
        "private_assertion_max_seconds": 30,
        "provider_token_revocation": "not_attempted",
        "revocation_complete": False,
    }
    assert (await session.execute(select(OAuthConnectionBinding))).scalars().all() == []


@pytest.mark.asyncio
async def test_missing_or_invalid_scope_snapshot_requires_new_consent(api, session):
    client, _, identity = api
    started = client.post("/auth/connections/oauth/slack/start").json()
    attempt = (await session.execute(select(OAuthConnectionAttempt))).scalar_one()
    for snapshot in (None, ["users:read", "users:read"], ["bad scope"], []):
        attempt.requested_scopes = snapshot
        await session.commit()
        response = client.post("/auth/connections/oauth/return", json={"state": started["state"], "session_uri": started["session_uri"]})
        assert response.status_code == 403
        assert response.json()["detail"]["error"] == "invalid_consent"
    identity.complete_resource_token_auth.assert_not_called()
    assert (await session.execute(select(UserCredential))).scalars().all() == []


@pytest.mark.asyncio
async def test_slack_read_consent_records_provider_identity_and_disconnect_clears_binding(api, session, monkeypatch):
    from src.internal import credential_routes, slack_reader

    monkeypatch.setenv("ADP_TOOLS_SLACK_MEDIATOR_ENABLED", "true")
    token_exchange = AsyncMock(return_value="synthetic-owner-token")
    observe = AsyncMock(return_value=slack_reader.SlackIdentityObservation("U00000001", "T00000001", ("users:read",)))
    monkeypatch.setattr(credential_routes, "_identity_access_token", token_exchange)
    monkeypatch.setattr(slack_reader, "observe_slack_identity", observe)
    client, _, identity = api
    started = client.post("/auth/connections/oauth/slack/start").json()
    assert identity.get_resource_oauth2_token.call_args.kwargs["scopes"] == ["users:read"]
    connected = client.post("/auth/connections/oauth/return", json={"state": started["state"], "session_uri": started["session_uri"]})
    assert connected.status_code == 201, connected.text
    binding = (await session.execute(select(OAuthConnectionBinding))).scalar_one()
    assert binding.user_id == "user-alice" and binding.provider_account_id == "T00000001:U00000001"
    assert binding.observed_scopes == ["users:read"] and binding.credential_id == connected.json()["id"]
    token_exchange.assert_awaited_once()
    observe.assert_awaited_once_with("synthetic-owner-token")
    assert "synthetic-owner-token" not in connected.text
    assert client.delete("/auth/connections/oauth/slack").status_code == 200
    assert (await session.execute(select(OAuthConnectionBinding))).scalars().all() == []


@pytest.mark.asyncio
async def test_slack_write_first_connection_requires_explicit_request_and_vendor_grant(api, session, monkeypatch):
    from src.internal import credential_routes, slack_reader

    monkeypatch.setenv("ADP_TOOLS_SLACK_MEDIATOR_ENABLED", "true")
    monkeypatch.setattr(credential_routes, "_identity_access_token", AsyncMock(return_value="synthetic-owner-token"))
    observe = AsyncMock(return_value=slack_reader.SlackIdentityObservation("U00000001", "T00000001", ("chat:write", "users:read")))
    monkeypatch.setattr(slack_reader, "observe_slack_identity", observe)
    client, _, identity = api
    monkeypatch.setitem(connections_routes.PROVIDER_SCOPES, "slack", ("slack", ["users:read", "chat:write"]))
    read_start = client.post("/auth/connections/oauth/slack/start")
    assert read_start.status_code == 200
    assert identity.get_resource_oauth2_token.call_args.kwargs["scopes"] == ["users:read"]
    write_start = client.post("/auth/connections/oauth/slack/start", json={"chat_write": True})
    assert write_start.status_code == 200
    assert identity.get_resource_oauth2_token.call_args.kwargs["scopes"] == ["users:read", "chat:write"]
    callback = {"state": write_start.json()["state"], "session_uri": write_start.json()["session_uri"]}
    connected = client.post("/auth/connections/oauth/return", json=callback)
    assert connected.status_code == 201, connected.text
    pointer = (await session.execute(select(UserCredential))).scalar_one()
    binding = (await session.execute(select(OAuthConnectionBinding))).scalar_one()
    assert pointer.scopes == {"oauth_scopes": ["users:read", "chat:write"]}
    assert binding.observed_scopes == ["chat:write", "users:read"]
    assert binding.credential_id == connected.json()["id"]
    observe.assert_awaited_once_with("synthetic-owner-token")
    assert "synthetic-owner-token" not in connected.text


@pytest.mark.asyncio
async def test_slack_write_reconsent_requires_explicit_request_and_renews_owned_binding(api, session, monkeypatch):
    from src.internal import credential_routes, slack_oauth, slack_reader
    from src.internal.tools_connection import connection_scopes

    monkeypatch.setenv("ADP_TOOLS_SLACK_MEDIATOR_ENABLED", "true")
    monkeypatch.setattr(slack_oauth, "get_settings", connections_routes.get_settings)
    monkeypatch.setattr(credential_routes, "_identity_access_token", AsyncMock(return_value="synthetic-owner-token"))
    observation = AsyncMock(return_value=slack_reader.SlackIdentityObservation("U00000001", "T00000001", ("users:read",)))
    monkeypatch.setattr(slack_reader, "observe_slack_identity", observation)
    client, _, identity = api
    started = client.post("/auth/connections/oauth/slack/start").json()
    original = client.post("/auth/connections/oauth/return", json={"state": started["state"], "session_uri": started["session_uri"]})
    assert original.status_code == 201, original.text
    old_id = original.json()["id"]
    binding = (await session.execute(select(OAuthConnectionBinding))).scalar_one()
    old_revision = binding.revision
    old_grant = binding.observed_scopes[:]
    assert client.post("/auth/connections/oauth/slack/start").status_code == 409
    assert client.post("/auth/connections/oauth/github/start", json={"chat_write": True}).status_code == 400
    assert client.post("/auth/connections/oauth/slack/start", json={"chat_write": "true"}).status_code == 422
    write_start = client.post("/auth/connections/oauth/slack/start", json={"chat_write": True})
    assert write_start.status_code == 200, write_start.text
    assert identity.get_resource_oauth2_token.call_args.kwargs["scopes"] == ["users:read", "chat:write"]
    assert identity.get_resource_oauth2_token.call_args.kwargs["forceAuthentication"] is True
    state_digest = hashlib.sha256(write_start.json()["state"].encode()).hexdigest()
    pending = (await session.execute(select(OAuthConnectionAttempt).where(OAuthConnectionAttempt.state_digest == state_digest))).scalar_one()
    assert pending.requested_scopes == ["users:read", "chat:write"]
    pointer = (await session.execute(select(UserCredential))).scalar_one()
    assert pointer.scopes == {"oauth_scopes": ["users:read"]} and binding.revision == old_revision
    connection = {"tenant_id": "org-acme", "owner_id": "user-alice", "workload_name": "adp-tools-dev-oauth-user"}
    read, reason = await connection_scopes(session, tool="slack___get_user", **connection)
    denied, missing = await connection_scopes(session, tool="slack___post_message", **connection)
    assert read == ["users:read"] and reason is None and denied == [] and missing == "missing_scope"
    observation.return_value = slack_reader.SlackIdentityObservation("U00000001", "T00000001", ("chat:write", "users:read"))
    callback = {"state": write_start.json()["state"], "session_uri": write_start.json()["session_uri"]}
    completed = client.post("/auth/connections/oauth/return", json=callback)
    assert completed.status_code == 201, completed.text
    assert completed.json()["id"] == old_id
    assert pointer.scopes == {"oauth_scopes": ["users:read", "chat:write"]}
    assert binding.revision != old_revision and binding.observed_scopes == ["chat:write", "users:read"]
    assert binding.provider_account_id == "T00000001:U00000001" and old_grant == ["users:read"]
    assert binding.credential_digest == slack_oauth.credential_digest(pointer)
    audited = (await session.execute(select(AuditLog).where(AuditLog.event_type == "oauth_connection_reconsented"))).scalar_one()
    assert audited.details["connection_id"] == old_id
    assert client.post("/auth/connections/oauth/return", json=callback).status_code == 403
    assert "synthetic-owner-token" not in completed.text


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ("scope_missing", "changed_account"))
async def test_slack_write_consent_refuses_provider_downgrade_or_account_switch(api, session, monkeypatch, fault):
    from src.internal import credential_routes, slack_oauth, slack_reader

    monkeypatch.setenv("ADP_TOOLS_SLACK_MEDIATOR_ENABLED", "true")
    monkeypatch.setattr(slack_oauth, "get_settings", connections_routes.get_settings)
    monkeypatch.setattr(credential_routes, "_identity_access_token", AsyncMock(return_value="synthetic-owner-token"))
    observation = AsyncMock(return_value=slack_reader.SlackIdentityObservation("U00000001", "T00000001", ("users:read",)))
    monkeypatch.setattr(slack_reader, "observe_slack_identity", observation)
    client, _, identity = api
    read_start = client.post("/auth/connections/oauth/slack/start").json()
    callback = {"state": read_start["state"], "session_uri": read_start["session_uri"]}
    assert client.post("/auth/connections/oauth/return", json=callback).status_code == 201
    credential = (await session.execute(select(UserCredential))).scalar_one()
    binding = (await session.execute(select(OAuthConnectionBinding))).scalar_one()
    prior_revision = binding.revision
    write_start = client.post("/auth/connections/oauth/slack/start", json={"chat_write": True}).json()
    observation.return_value = slack_reader.SlackIdentityObservation(
        "U00000002" if fault == "changed_account" else "U00000001",
        "T00000001",
        ("chat:write", "users:read") if fault == "changed_account" else ("users:read",),
    )
    refused = client.post("/auth/connections/oauth/return", json={"state": write_start["state"], "session_uri": write_start["session_uri"]})
    assert refused.status_code == 403
    assert refused.json()["detail"]["error"] == ("write_scope_not_granted" if fault == "scope_missing" else "slack_account_changed")
    assert credential.scopes == {"oauth_scopes": ["users:read"]} and binding.revision == prior_revision
    assert binding.observed_scopes == ["users:read"] and identity.complete_resource_token_auth.call_count == 2


@pytest.mark.asyncio
async def test_slack_write_consent_is_unavailable_without_mediator(api, session, monkeypatch):
    client, _, identity = api
    assert client.post("/auth/connections/oauth/slack/start", json={"chat_write": True}).status_code == 503
    identity.get_resource_oauth2_token.assert_not_called()
    monkeypatch.setenv("ADP_TOOLS_SLACK_MEDIATOR_ENABLED", "true")
    started = client.post("/auth/connections/oauth/slack/start", json={"chat_write": True}).json()
    monkeypatch.delenv("ADP_TOOLS_SLACK_MEDIATOR_ENABLED")
    assert client.post("/auth/connections/oauth/return", json={"state": started["state"], "session_uri": started["session_uri"]}).status_code == 503
    identity.complete_resource_token_auth.assert_not_called()
    assert (await session.execute(select(UserCredential))).scalars().all() == []


@pytest.mark.asyncio
async def test_expired_attempt_and_identity_failure_do_not_create_pointer(api, session):
    client, _, identity = api
    client.post("/auth/connections/oauth/slack/start")
    state = identity.get_resource_oauth2_token.call_args.kwargs["customState"]
    attempt = (await session.execute(select(OAuthConnectionAttempt))).scalar_one()
    attempt.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await session.commit()
    assert client.post("/auth/connections/oauth/return", json={"state": state, "session_uri": "opaque-session"}).status_code == 403
    attempt.expires_at = datetime.now(UTC) + timedelta(minutes=1)
    await session.commit()
    identity.complete_resource_token_auth.side_effect = RuntimeError("secret must never be returned")
    result = client.post("/auth/connections/oauth/return", json={"state": state, "session_uri": "opaque-session"})
    assert result.status_code == 502 and "secret must never" not in result.text
    assert (await session.execute(select(UserCredential))).scalars().all() == []


def test_unauthorized_or_unconfigured_start(api, monkeypatch):
    client, app, identity = api
    app.dependency_overrides[get_current_user_context] = lambda: caller(account_type="service")
    assert client.post("/auth/connections/oauth/atlassian/start").status_code == 403
    app.dependency_overrides[get_current_user_context] = lambda: caller()
    assert client.post("/auth/connections/oauth/unknown/start").status_code == 404
    monkeypatch.setattr(connections_routes, "get_settings", lambda: SimpleNamespace(oauth_workload_name="", oauth_return_url=""))
    assert client.post("/auth/connections/oauth/atlassian/start").status_code == 503
    identity.get_resource_oauth2_token.assert_not_called()


@pytest.mark.asyncio
async def test_disconnect_removes_only_owned_pointer_and_cancels_pending_consent(api, session):
    client, app, identity = api
    pointer = UserCredential(
        id="oauth-alice",
        org_id="org-acme",
        user_id="user-alice",
        service="jira",
        label="AgentCore OAuth",
        credential_type="oauth_token",
        provider="agentcore-identity",
        secret_arn="agentcore-identity:adp-tools-dev-oauth-user:atlassian",
        scopes={"oauth_scopes": ["read:jira-work", "read:me", "offline_access"]},
        agent_consent=True,
    )
    session.add(pointer)
    await session.commit()
    app.dependency_overrides[get_current_user_context] = lambda: caller("user-bob")
    other = client.delete("/auth/connections/oauth/atlassian")
    assert other.status_code == 200
    assert (await session.execute(select(UserCredential).where(UserCredential.id == "oauth-alice"))).scalar_one_or_none()
    app.dependency_overrides[get_current_user_context] = lambda: caller()
    result = client.delete("/auth/connections/oauth/atlassian")
    assert result.status_code == 200
    assert result.json() == {
        "status": "disconnected",
        "identity_token_revoked": False,
        "provider_access_token_expires_at": None,
        "private_assertion_max_seconds": 30,
        "provider_token_revocation": "not_attempted",
        "revocation_complete": False,
    }
    assert (await session.execute(select(UserCredential))).scalars().all() == []
    receipts = (await session.execute(select(AuditLog).where(AuditLog.event_type == "oauth_connection_disconnected"))).scalars().all()
    assert len(receipts) == 1
    assert receipts[0].details["connection_id"] == "oauth-alice"
    assert receipts[0].details["private_assertion_max_seconds"] == 30
    assert receipts[0].details["provider_token_revocation"] == "not_attempted"
    assert receipts[0].details["revocation_complete"] is False
    assert receipts[0].details["provider_access_token_expires_at"] is None
    assert receipts[0].actor_id == "user-alice" and receipts[0].details["user_id"] == "user-alice"
    assert client.delete("/auth/connections/oauth/atlassian").status_code == 200
    started = client.post("/auth/connections/oauth/atlassian/start")
    state = started.json()["state"]
    assert client.delete("/auth/connections/oauth/atlassian").status_code == 200
    refused = client.post("/auth/connections/oauth/return", json={"state": state, "session_uri": "opaque-session"})
    assert refused.status_code == 403
    identity.complete_resource_token_auth.assert_not_called()
    cancelled = (await session.execute(select(AuditLog).where(AuditLog.event_type == "oauth_connection_cancelled"))).scalars().all()
    assert len(cancelled) == 1 and cancelled[0].actor_id == "user-alice"
    reconnect = client.post("/auth/connections/oauth/atlassian/start")
    result = client.post("/auth/connections/oauth/return", json={"state": reconnect.json()["state"], "session_uri": "opaque-session"})
    assert result.status_code == 201
    created = (await session.execute(select(AuditLog).where(AuditLog.event_type == "oauth_connection_created"))).scalars().all()
    assert len(created) == 1 and created[0].details["connection_id"] == result.json()["id"]
    assert created[0].details["connection_id"] != receipts[0].details["connection_id"]
    assert created[0].actor_id == receipts[0].actor_id == "user-alice"


@pytest.mark.asyncio
async def test_generic_vault_delete_does_not_treat_identity_pointer_as_secret(api, session):
    from src.auth import vault_routes

    client, app, _ = api
    app.include_router(vault_routes.router)
    sm = MagicMock()
    app.dependency_overrides[vault_routes.get_secrets_manager] = lambda: sm
    session.add(
        UserCredential(
            id="oauth-alice",
            org_id="org-acme",
            user_id="user-alice",
            service="jira",
            label="AgentCore OAuth",
            credential_type="oauth_token",
            provider="agentcore-identity",
            secret_arn="agentcore-identity:adp-tools-dev-oauth-user:atlassian",
        )
    )
    await session.commit()
    result = client.delete("/auth/credentials/oauth-alice")
    assert result.status_code == 409 and result.json()["detail"]["error"] == "use_oauth_disconnect"
    sm.delete_secret.assert_not_called()
    assert (await session.execute(select(UserCredential).where(UserCredential.id == "oauth-alice"))).scalar_one_or_none()


@pytest.mark.parametrize("state", [None, "unknown", "active"])
def test_browser_return_redirects_to_settings_without_completing_consent(api, state):
    client, _, identity = api
    if state == "active":
        state = client.post("/auth/connections/oauth/github/start").json()["state"]
    response = client.get("/auth/connections/oauth/return", params={"state": state} if state else {}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/settings/connections?oauth_consent=returned"
    assert "opaque-session" not in response.headers["location"]
    failed = client.get("/auth/connections/oauth/return?error=access_denied", follow_redirects=False)
    assert failed.headers["location"] == "/settings/connections?oauth_consent=error"
    identity.complete_resource_token_auth.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ("foreign_owner", "wrong_state", "wrong_session", "replayed_callback"))
async def test_refused_callback_never_exchanges_or_observes_another_token(api, session, monkeypatch, fault):
    from src.internal import credential_routes, github_oauth

    monkeypatch.setenv("ADP_TOOLS_GITHUB_MEDIATOR_ENABLED", "true")
    token_exchange = AsyncMock(return_value="synthetic-owner-token")
    provider = AsyncMock(return_value=github_oauth.GithubObservation("213", ("read:user",), {"id": 213}))
    monkeypatch.setattr(credential_routes, "_identity_access_token", token_exchange)
    monkeypatch.setattr(github_oauth, "observe_github_user", provider)
    client, app, identity = api
    started = client.post("/auth/connections/oauth/github/start")
    assert started.status_code == 200
    attempt = started.json()
    callback = {"state": attempt["state"], "session_uri": attempt["session_uri"]}
    if fault == "foreign_owner":
        app.dependency_overrides[get_current_user_context] = lambda: caller("user-bob")
    elif fault == "wrong_state":
        callback["state"] = "foreign-state"
    elif fault == "wrong_session":
        callback["session_uri"] = "foreign-session"
    elif fault == "replayed_callback":
        assert client.post("/auth/connections/oauth/return", json=callback).status_code == 201
    before = (identity.complete_resource_token_auth.call_count, token_exchange.await_count, provider.await_count)
    refused = client.post("/auth/connections/oauth/return", json=callback)
    assert refused.status_code == 403
    assert (identity.complete_resource_token_auth.call_count, token_exchange.await_count, provider.await_count) == before
    assert provider.await_count == (1 if fault == "replayed_callback" else 0)
    assert "synthetic-owner-token" not in refused.text


@pytest.mark.asyncio
async def test_provider_readiness_is_authenticated_and_distinguishes_failed_checks(api):
    client, app, _ = api
    from src.shared.identity.oauth_setup import get_oauth_setup

    setup = MagicMock()
    setup.status.side_effect = [
        {"provider": "github", "service": "github", "status": "registered"},
        {"provider": "atlassian", "service": "jira", "status": "setup_required"},
        RuntimeError("private provider error"),
    ]
    app.dependency_overrides[get_oauth_setup] = lambda: setup
    result = client.get("/auth/connections/oauth/providers")
    assert result.status_code == 200
    assert [item["status"] for item in result.json()["providers"]] == ["registered", "setup_required", "unavailable"]
    assert "private" not in result.text
    setup.register.assert_not_called()
    app.dependency_overrides[get_current_user_context] = lambda: caller(account_type="service")
    setup.reset_mock()
    assert client.get("/auth/connections/oauth/providers").status_code == 403
    setup.status.assert_not_called()


@pytest.mark.asyncio
async def test_atlassian_dynamic_admin_commits_intent_before_worker(api, session, monkeypatch):
    from src.shared.identity.oauth_setup import get_oauth_setup
    from src.shared.models.oauth_setup import OAuthRecoveryJob, OAuthSetupState

    _, app, _ = api
    app.include_router(oauth_apps.router)
    setup = MagicMock()
    plan = {"registration_mode": "dynamic-client-registration", "resource": "synthetic-pinned-resource"}
    setup.prepare_atlassian_mcp.return_value = {"status": "setup_required"}
    monkeypatch.setattr(oauth_apps, "atlassian_mcp_config", lambda settings, store: plan)
    factory = object()
    monkeypatch.setattr(oauth_apps, "get_session_factory", lambda: factory)
    called = []

    async def consume(sessions, *, setup, owner, now):
        assert sessions is factory and owner.startswith("admin-")
        assert not session.in_transaction()
        called.append(now)
        return "idle"

    monkeypatch.setattr(oauth_apps, "process_setup_once", consume)
    app.dependency_overrides[get_oauth_setup] = lambda: setup
    app.dependency_overrides[get_current_user] = lambda: caller().model_copy(update={"is_admin": True})
    response = TestClient(app, raise_server_exceptions=False).post("/admin/connections/atlassian-mcp/prepare")
    assert response.status_code == 200, response.text
    assert response.json() == {"status": "setup_required"}
    assert len(called) == 1
    state = await session.get(OAuthSetupState, "atlassian-mcp")
    job = await session.get(OAuthRecoveryJob, (await session.execute(select(OAuthRecoveryJob.id))).scalar_one())
    assert state.phase == "pending" and state.registration_mode == "dynamic-client-registration"
    assert job.state == "pending" and job.expected_revision == state.config_digest
    setup.prepare_atlassian_mcp.assert_called_once_with(before_effects=None)


@pytest.mark.asyncio
async def test_atlassian_status_reads_only_matching_tenant_policy(api, session):
    from src.shared.identity.oauth_setup import get_oauth_setup
    from src.shared.identity.oauth_setup_store import record_tenant_policy_observation
    from src.shared.models.oauth_setup import OAuthSetupState

    _, app, _ = api
    setup = MagicMock()
    setup.atlassian_mcp_status.side_effect = lambda *, policy_result: {
        "status": "policy_blocked" if policy_result == "blocked" else "ready" if policy_result == "allowed" else "setup_required"
    }
    app.dependency_overrides[get_oauth_setup] = lambda: setup
    client = TestClient(app, raise_server_exceptions=False)
    assert client.get("/auth/connections/atlassian-mcp/status").json() == {"status": "setup_required"}
    now = datetime.now(UTC)
    session.add(
        OAuthSetupState(
            deployment_key="atlassian-mcp",
            tenant_id="platform",
            registration_mode="retained-deployment-client",
            config_digest="a" * 64,
            phase="prepared",
            updated_at=now,
        )
    )
    await session.flush()
    await record_tenant_policy_observation(
        session,
        setup_key="atlassian-mcp",
        tenant_id="org-unrelated",
        config_digest="a" * 64,
        result="allowed",
        evidence_digest="b" * 64,
        now=now,
    )
    await session.commit()
    assert client.get("/auth/connections/atlassian-mcp/status").json() == {"status": "setup_required"}
    await record_tenant_policy_observation(
        session,
        setup_key="atlassian-mcp",
        tenant_id="org-acme",
        config_digest="a" * 64,
        result="blocked",
        evidence_digest="c" * 64,
        now=now,
    )
    await session.commit()
    assert client.get("/auth/connections/atlassian-mcp/status").json() == {"status": "policy_blocked"}
    assert setup.atlassian_mcp_status.call_args.kwargs == {"policy_result": "blocked"}
    state = await session.get(OAuthSetupState, "atlassian-mcp")
    state.config_digest = "d" * 64
    await session.commit()
    assert client.get("/auth/connections/atlassian-mcp/status").json() == {"status": "setup_required"}


# ---------------------------------------------------------------------------
# #8555: repository access for the owner's own tools (Dev Box gh/git, `adp github token`)
# ---------------------------------------------------------------------------


async def connect_app_owner(api, session, monkeypatch):
    from src.internal import github_oauth

    monkeypatch.setattr(github_oauth, "get_settings", lambda: SimpleNamespace(oauth_workload_name="adp-tools-dev-oauth-user", github_app_id="123"))
    monkeypatch.setattr(
        github_oauth,
        "github_app_client",
        AsyncMock(
            return_value={
                "client_id": "Iv1fixture",
                "client_secret": "synthetic-secret",
                "app_id": "123",
            }
        ),
    )
    monkeypatch.setattr(
        github_oauth,
        "observe_github_app_user",
        AsyncMock(
            return_value=github_oauth.GithubObservation(
                "213",
                (),
                {"id": 213, "login": "alice-gh"},
                ({"kind": "github_app_user", "schema_version": 1, "app_id": 123, "installation_ids": [42, 43], "account_id": "213"},),
            )
        ),
    )
    client, _, _ = api
    started = client.post("/auth/connections/oauth/github/start", json={"repository_access": True})
    assert started.status_code == 200
    connected = client.post("/auth/connections/oauth/return", json={"state": started.json()["state"], "session_uri": started.json()["session_uri"]})
    assert connected.status_code == 201, connected.text
    return (await session.execute(select(UserCredential))).scalar_one()


@pytest.mark.asyncio
async def test_app_provider_reports_only_a_reverified_owner_login(api, session, monkeypatch):
    from src.auth import connections_routes
    from src.internal import github_oauth
    from src.shared.identity.oauth_setup import get_oauth_setup

    await connect_app_owner(api, session, monkeypatch)
    client, app, identity = api
    monkeypatch.setattr(connections_routes, "get_identity_client", lambda: identity)
    setup = MagicMock()
    setup.status.side_effect = lambda provider: {"provider": provider, "service": provider, "status": "registered"}
    app.dependency_overrides[get_oauth_setup] = lambda: setup
    response = client.get("/auth/connections/oauth/providers")
    assert response.status_code == 200
    github = response.json()["providers"][0]
    assert github["connection_status"] == "verified" and github["login"] == "alice-gh"
    assert "synthetic-owner-token" not in response.text
    github_oauth.observe_github_app_user.return_value = github_oauth.GithubObservation(
        "214",
        (),
        {"id": 214, "login": "other-owner"},
        ({"kind": "github_app_user", "schema_version": 1, "app_id": 123, "installation_ids": [42], "account_id": "214"},),
    )
    denied = client.get("/auth/connections/oauth/providers").json()["providers"][0]
    assert denied["connection_status"] == "reconnect_required" and "login" not in denied


@pytest.mark.asyncio
async def test_nine_hour_old_app_connection_mints_or_reports_a_readable_refresh_failure(api, session, monkeypatch):
    from fastapi import HTTPException

    from src.crew.github_access import SWITCH
    from src.internal import credential_routes, github_oauth

    credential = await connect_app_owner(api, session, monkeypatch)
    binding = await session.get(OAuthConnectionBinding, credential.id)
    credential.created_at = datetime.now(UTC) - timedelta(hours=9)
    binding.verified_at = datetime.now(UTC) - timedelta(hours=9)
    await session.commit()
    monkeypatch.setenv(SWITCH, "true")
    success = api[0].post("/auth/connections/oauth/github/token")
    assert success.status_code == 200, success.text
    assert success.json()["login"] == "alice-gh"
    assert success.json()["token"] == "synthetic-owner-token"
    assert success.json()["expires_at"] <= int(datetime.now(UTC).timestamp()) + 600
    github_oauth.observe_github_app_user.assert_awaited()

    exchange = AsyncMock(side_effect=HTTPException(502, detail={"error": "identity_unavailable"}))
    monkeypatch.setattr(credential_routes, "_identity_access_token", exchange)
    failed = api[0].post("/auth/connections/oauth/github/token")
    assert failed.status_code == 409
    assert failed.json()["detail"]["error"] == "github_reconnect_required"
    assert "Reconnect GitHub from the Dev Box page" in failed.json()["detail"]["message"]
    assert "synthetic-owner-token" not in failed.text
    assert failed.headers["cache-control"] == "no-store"
    assert exchange.await_count == 1


@pytest.mark.asyncio
async def test_disconnect_commits_app_denial_before_revocation(api, session, monkeypatch):
    from src.auth import connections_routes
    from src.crew.github_access import SWITCH

    credential = await connect_app_owner(api, session, monkeypatch)
    monkeypatch.setenv(SWITCH, "true")
    monkeypatch.setattr(connections_routes, "get_identity_client", lambda: api[2])
    observed = []

    async def revoke(client_id, client_secret, token):
        assert (client_id, client_secret, token) == ("Iv1fixture", "synthetic-secret", "synthetic-owner-token")
        assert await session.get(UserCredential, credential.id) is None
        assert await session.get(OAuthConnectionBinding, credential.id) is None
        assert (await session.execute(select(AuditLog).where(AuditLog.event_type == "oauth_connection_disconnected"))).scalar_one()
        observed.append("local-denial-committed")
        return "confirmed", 204

    monkeypatch.setattr(connections_routes, "revoke_github_grant", revoke)
    disconnected = api[0].delete("/auth/connections/oauth/github")
    assert disconnected.status_code == 200, disconnected.text
    assert disconnected.json()["provider_token_revocation"] == "confirmed"
    assert observed == ["local-denial-committed"]
    audits = (await session.execute(select(AuditLog).where(AuditLog.event_type == "github_token_revoked"))).scalars().all()
    assert len(audits) == 1 and audits[0].details == {"connection_id": credential.id, "outcome": "confirmed", "http_status": 204}
    assert "synthetic-owner-token" not in str(audits[0].details)
    denied = api[0].post("/auth/connections/oauth/github/token")
    assert denied.status_code == 404 and denied.json()["detail"]["error"] == "github_not_connected"


@pytest.mark.asyncio
async def test_pause_commits_app_denial_and_rotates_binding_before_revocation(api, session, monkeypatch):
    from src.auth import connections_routes, vault_routes
    from src.crew.github_access import SWITCH

    credential = await connect_app_owner(api, session, monkeypatch)
    monkeypatch.setenv(SWITCH, "true")
    binding = await session.get(OAuthConnectionBinding, credential.id)
    original_revision = binding.revision
    observed = []

    async def revoke(client_id, client_secret, token):
        await session.refresh(credential)
        await session.refresh(binding)
        assert credential.agent_consent is False
        assert binding.revision != original_revision
        assert (await session.execute(select(AuditLog).where(AuditLog.event_type == "github_consent_paused"))).scalar_one()
        assert (await session.execute(select(OAuthConnectionAttempt))).scalars().all() == []
        observed.append("local-denial-committed")
        return "confirmed", 204

    monkeypatch.setattr(connections_routes, "get_identity_client", lambda: api[2])
    monkeypatch.setattr(connections_routes, "revoke_github_grant", revoke)
    api[1].include_router(vault_routes.router)
    paused = api[0].put(f"/auth/credentials/{credential.id}/agent-consent", json={"agent_consent": False})
    assert paused.status_code == 200, paused.text
    assert observed == ["local-denial-committed"]
    audits = (await session.execute(select(AuditLog).where(AuditLog.event_type == "github_token_revoked"))).scalars().all()
    assert len(audits) == 1 and audits[0].details["outcome"] == "confirmed"
    denied = api[0].post("/auth/connections/oauth/github/token")
    assert denied.status_code == 403 and denied.json()["detail"]["error"] == "github_consent_required"


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["pause", "disconnect"])
@pytest.mark.parametrize("revocation_status", [204, 503])
async def test_github_app_owner_lifecycle_audits_revocation_and_requires_fresh_consent(
    api,
    session,
    monkeypatch,
    action,
    revocation_status,
):
    from src.auth import vault_routes
    from src.crew.github_access import SWITCH
    from src.internal import credential_routes

    credential = await connect_app_owner(api, session, monkeypatch)
    monkeypatch.setenv(SWITCH, "true")
    client, app, identity = api
    app.include_router(vault_routes.router)
    monkeypatch.setattr(connections_routes, "get_identity_client", lambda: identity)
    monkeypatch.setattr(credential_routes, "_validate_proxy_url", lambda url, settings: httpx.URL(url))
    assert client.post("/auth/connections/oauth/github/token").status_code == 200
    pending = client.post("/auth/connections/oauth/github/start", json={"reconnect": True}).json()
    revisions = (await session.get(OAuthConnectionBinding, credential.id)).revision
    await session.commit()
    requests = []

    def provider(request):
        assert request.method == "DELETE" and request.url.path == "/applications/Iv1fixture/grant"
        assert request.headers["authorization"].startswith("Basic ")
        assert json.loads(request.content) == {"access_token": "synthetic-owner-token"}
        requests.append(request)
        return httpx.Response(revocation_status)

    original_client = httpx.AsyncClient
    monkeypatch.setattr(connections_routes.httpx, "AsyncClient", lambda **kwargs: original_client(transport=httpx.MockTransport(provider), **kwargs))
    if action == "pause":
        changed = client.put(f"/auth/credentials/{credential.id}/agent-consent", json={"agent_consent": False})
        assert changed.status_code == 200, changed.text
        assert (await session.get(OAuthConnectionBinding, credential.id)).revision != revisions
    else:
        changed = client.delete("/auth/connections/oauth/github")
        assert changed.status_code == 200, changed.text
        assert changed.json()["provider_token_revocation"] == ("confirmed" if revocation_status == 204 else "failed")
        assert await session.get(UserCredential, credential.id) is None
        assert await session.get(OAuthConnectionBinding, credential.id) is None
    assert len(requests) == 1
    audit = (await session.execute(select(AuditLog).where(AuditLog.event_type == "github_token_revoked"))).scalar_one()
    assert audit.details == {
        "connection_id": credential.id,
        "outcome": "confirmed" if revocation_status == 204 else "failed",
        "http_status": revocation_status,
    }
    assert "synthetic-owner-token" not in str(audit.details) and "synthetic-secret" not in str(audit.details)
    denied = client.post("/auth/connections/oauth/github/token")
    assert denied.status_code == (403 if action == "pause" else 404)
    assert (
        client.post(
            "/auth/connections/oauth/return",
            json={
                "state": pending["state"],
                "session_uri": pending["session_uri"],
            },
        ).status_code
        == 403
    )
    if action == "pause":
        reenabled = client.put(f"/auth/credentials/{credential.id}/agent-consent", json={"agent_consent": True})
        assert reenabled.status_code == 409 and reenabled.json()["detail"]["error"] == "github_reconnect_required"
        started = client.post("/auth/connections/oauth/github/start", json={"repository_access": True})
        assert started.status_code == 200, started.text
        if revocation_status == 503:
            from src.internal import github_oauth

            original = github_oauth.observe_github_app_user.return_value
            github_oauth.observe_github_app_user.return_value = github_oauth.GithubObservation(
                "214",
                (),
                {"id": 214, "login": "other-owner"},
                original.resources,
            )
            refused = client.post(
                "/auth/connections/oauth/return",
                json={
                    "state": started.json()["state"],
                    "session_uri": started.json()["session_uri"],
                },
            )
            assert refused.status_code == 403 and refused.json()["detail"]["error"] == "github_account_changed"
            await session.refresh(credential)
            assert credential.agent_consent is False
            github_oauth.observe_github_app_user.return_value = original
        returned = client.post(
            "/auth/connections/oauth/return",
            json={
                "state": started.json()["state"],
                "session_uri": started.json()["session_uri"],
            },
        )
        assert returned.status_code == 201, returned.text
        await session.refresh(credential)
        assert credential.agent_consent is True
        assert (await session.get(OAuthConnectionBinding, credential.id)).revision != revisions
        assert client.post("/auth/connections/oauth/github/token").status_code == 200


@pytest.mark.asyncio
async def test_github_app_revoke_uses_basic_client_auth_and_access_token_body(monkeypatch):
    import httpx

    from src.auth import connections_routes
    from src.internal import credential_routes

    requests = []

    def provider(request):
        requests.append(request)
        return httpx.Response(204)

    original_client = httpx.AsyncClient
    monkeypatch.setattr(connections_routes.httpx, "AsyncClient", lambda **kwargs: original_client(transport=httpx.MockTransport(provider), **kwargs))
    monkeypatch.setattr(credential_routes, "_validate_proxy_url", lambda url, settings: httpx.URL(url))
    monkeypatch.setattr(connections_routes, "get_settings", lambda: object())
    outcome, status = await connections_routes.revoke_github_grant("Iv1fixture", "synthetic-secret", "synthetic-owner-token")
    assert (outcome, status) == ("confirmed", 204)
    assert len(requests) == 1
    assert requests[0].method == "DELETE"
    assert str(requests[0].url) == "https://api.github.com/applications/Iv1fixture/grant"
    assert requests[0].headers["authorization"].startswith("Basic ")
    assert requests[0].content == b'{"access_token":"synthetic-owner-token"}'


@pytest.mark.asyncio
@pytest.mark.parametrize("failure,expected_status", [(404, 404), (503, 503), ("timeout", None)])
async def test_github_revoke_reports_404_5xx_and_timeout_without_exposing_credentials(monkeypatch, failure, expected_status):
    from src.internal import credential_routes

    def provider(request):
        if failure == "timeout":
            raise httpx.ReadTimeout("synthetic timeout", request=request)
        return httpx.Response(failure)

    original_client = httpx.AsyncClient
    monkeypatch.setattr(connections_routes.httpx, "AsyncClient", lambda **kwargs: original_client(transport=httpx.MockTransport(provider), **kwargs))
    monkeypatch.setattr(credential_routes, "_validate_proxy_url", lambda url, settings: httpx.URL(url))
    monkeypatch.setattr(connections_routes, "get_settings", lambda: object())
    outcome = await connections_routes.revoke_github_grant("Iv1fixture", "synthetic-secret", "synthetic-owner-token")
    assert outcome == ("failed", expected_status)
    assert "synthetic-secret" not in str(outcome) and "synthetic-owner-token" not in str(outcome)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure,expected_status", [(404, 404), (503, 503), ("timeout", None)])
async def test_disconnect_keeps_denial_and_audits_failed_github_revoke(api, session, monkeypatch, caplog, failure, expected_status):
    from src.crew.github_access import SWITCH
    from src.internal import credential_routes

    credential = await connect_app_owner(api, session, monkeypatch)
    monkeypatch.setenv(SWITCH, "true")
    monkeypatch.setattr(connections_routes, "get_identity_client", lambda: api[2])
    monkeypatch.setattr(connections_routes, "get_settings", lambda: object())
    monkeypatch.setattr(credential_routes, "_validate_proxy_url", lambda url, settings: httpx.URL(url))

    def provider(request):
        if failure == "timeout":
            raise httpx.ReadTimeout("synthetic timeout", request=request)
        return httpx.Response(failure)

    original_client = httpx.AsyncClient
    monkeypatch.setattr(connections_routes.httpx, "AsyncClient", lambda **kwargs: original_client(transport=httpx.MockTransport(provider), **kwargs))
    disconnected = api[0].delete("/auth/connections/oauth/github")
    assert disconnected.status_code == 200, disconnected.text
    assert disconnected.json()["provider_token_revocation"] == "failed"
    assert await session.get(UserCredential, credential.id) is None
    assert await session.get(OAuthConnectionBinding, credential.id) is None
    audits = (await session.execute(select(AuditLog).where(AuditLog.event_type == "github_token_revoked"))).scalars().all()
    assert len(audits) == 1 and audits[0].details == {
        "connection_id": credential.id,
        "outcome": "failed",
        "http_status": expected_status,
    }
    assert f"GitHub grant revocation failed for connection {credential.id} (HTTP {expected_status})" in caplog.text
    assert "synthetic-secret" not in caplog.text and "synthetic-owner-token" not in caplog.text
    denied = api[0].post("/auth/connections/oauth/github/token")
    assert denied.status_code == 404 and denied.json()["detail"]["error"] == "github_not_connected"


@pytest.mark.asyncio
async def test_disconnect_denies_before_identity_client_setup_failure(api, session, monkeypatch, caplog):
    credential = await connect_app_owner(api, session, monkeypatch)

    def unavailable():
        assert session.get_transaction() is None
        raise RuntimeError("identity client unavailable")

    monkeypatch.setattr(connections_routes, "get_identity_client", unavailable)
    disconnected = api[0].delete("/auth/connections/oauth/github")
    assert disconnected.status_code == 200, disconnected.text
    assert disconnected.json()["provider_token_revocation"] == "failed"
    assert await session.get(UserCredential, credential.id) is None
    audit = (await session.execute(select(AuditLog).where(AuditLog.event_type == "github_token_revoked"))).scalar_one()
    assert audit.details == {"connection_id": credential.id, "outcome": "failed", "http_status": None}
    assert "GitHub grant revocation failed" in caplog.text


@pytest.mark.asyncio
async def test_pause_keeps_denial_and_audits_failed_github_revoke(api, session, monkeypatch, caplog):
    from src.auth import vault_routes
    from src.crew.github_access import SWITCH
    from src.internal import credential_routes

    credential = await connect_app_owner(api, session, monkeypatch)
    monkeypatch.setenv(SWITCH, "true")
    original_revision = (await session.get(OAuthConnectionBinding, credential.id)).revision
    monkeypatch.setattr(connections_routes, "get_identity_client", lambda: api[2])
    monkeypatch.setattr(connections_routes, "get_settings", lambda: object())
    monkeypatch.setattr(credential_routes, "_validate_proxy_url", lambda url, settings: httpx.URL(url))
    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        connections_routes.httpx,
        "AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(lambda request: httpx.Response(503)), **kwargs),
    )
    api[1].include_router(vault_routes.router)
    paused = api[0].put(f"/auth/credentials/{credential.id}/agent-consent", json={"agent_consent": False})
    assert paused.status_code == 200, paused.text
    await session.refresh(credential)
    binding = await session.get(OAuthConnectionBinding, credential.id)
    await session.refresh(binding)
    assert credential.agent_consent is False and binding.revision != original_revision
    audit = (await session.execute(select(AuditLog).where(AuditLog.event_type == "github_token_revoked"))).scalar_one()
    assert audit.details == {"connection_id": credential.id, "outcome": "failed", "http_status": 503}
    assert "GitHub grant revocation failed" in caplog.text
    denied = api[0].post("/auth/connections/oauth/github/token")
    assert denied.status_code == 403 and denied.json()["detail"]["error"] == "github_consent_required"


@pytest.mark.asyncio
async def test_github_app_consent_wires_installation_evidence_to_box_mint(api, session, monkeypatch):
    from src.crew import github_access
    from src.internal import credential_routes, github_oauth

    monkeypatch.setenv(github_access.SWITCH, "true")
    monkeypatch.setattr(github_oauth, "get_settings", lambda: SimpleNamespace(oauth_workload_name="adp-tools-dev-oauth-user", github_app_id="123"))
    app_client = {"client_id": "Iv1fixture", "client_secret": "synthetic-secret", "app_id": "123"}
    monkeypatch.setattr(github_oauth, "github_app_client", AsyncMock(return_value=app_client))
    observed = github_oauth.GithubObservation(
        "213",
        (),
        {"id": 213, "login": "alice-gh"},
        ({"kind": "github_app_user", "schema_version": 1, "app_id": 123, "installation_ids": [42, 43], "account_id": "213"},),
    )
    app_observe = AsyncMock(return_value=observed)
    monkeypatch.setattr(github_oauth, "observe_github_app_user", app_observe)
    monkeypatch.setattr(credential_routes, "_identity_access_token", AsyncMock(return_value="synthetic-owner-token"))
    client, _, identity = api
    started = client.post("/auth/connections/oauth/github/start", json={"repository_access": True})
    assert started.status_code == 200, started.text
    assert identity.get_resource_oauth2_token.call_args.kwargs["scopes"] == []
    attempt = (await session.execute(select(OAuthConnectionAttempt))).scalar_one()
    assert attempt.requested_scopes == []
    connected = client.post("/auth/connections/oauth/return", json={"state": started.json()["state"], "session_uri": started.json()["session_uri"]})
    assert connected.status_code == 201, connected.text
    credential = (await session.execute(select(UserCredential))).scalar_one()
    binding = (await session.execute(select(OAuthConnectionBinding))).scalar_one()
    assert credential.scopes == {"github_app_grant": {"schema_version": 1, "repository_access": True}}
    assert binding.observed_scopes == []
    assert binding.observed_resources == list(observed.resources)
    minted = client.post("/auth/connections/oauth/github/token")
    assert minted.status_code == 200, minted.text
    assert minted.json()["login"] == "alice-gh"
    assert app_observe.await_count == 2
    binding.observed_resources[0]["installation_ids"] = []
    from sqlalchemy.orm.attributes import flag_modified

    flag_modified(binding, "observed_resources")
    await session.commit()
    denied = client.post("/auth/connections/oauth/github/token")
    assert denied.status_code == 409 and denied.json()["detail"]["error"] == "github_reconnect_required"
    assert app_observe.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", [None, "client", "owner", "no_installation", "incomplete_page", "incomplete_last_page", "oversized_page"])
async def test_app_consent_endpoint_requires_complete_paginated_installation_evidence(api, session, monkeypatch, fault):
    from src.internal import credential_routes, github_oauth

    monkeypatch.setattr(github_oauth, "get_settings", lambda: SimpleNamespace(oauth_workload_name="fixture-oauth-user", github_app_id="123"))
    monkeypatch.setattr(
        github_oauth,
        "github_app_client",
        AsyncMock(
            return_value={
                "client_id": "Iv1fixture",
                "client_secret": "synthetic-secret",
                "app_id": "123",
            }
        ),
    )
    monkeypatch.setattr(credential_routes, "_identity_access_token", AsyncMock(return_value="synthetic-owner-token"))
    monkeypatch.setattr(credential_routes, "_validate_proxy_url", lambda url, settings: httpx.URL(url))
    pages = []

    def provider(request):
        if request.url.path == "/user":
            return httpx.Response(200, json={"id": 213, "login": "fixture-user", "type": "User"})
        if request.url.path == "/applications/Iv1fixture/token":
            return httpx.Response(
                200,
                json={
                    "app": {"client_id": "wrong-client" if fault == "client" else "Iv1fixture"},
                    "user": {"id": 214 if fault == "owner" else 213},
                },
            )
        if request.url.path == "/user/installations":
            assert request.url.params["per_page"] == "100"
            page = int(request.url.params["page"])
            pages.append(page)
            if page == 1:
                installations = [
                    {
                        "id": index,
                        "app_id": 456 if fault == "no_installation" else 123,
                        "description": "fixture" * (1600 if fault == "oversized_page" else 110),
                    }
                    for index in range(1, 101 if fault != "incomplete_page" else 100)
                ]
            else:
                installations = [] if fault == "incomplete_last_page" else [{"id": 101, "app_id": 456}]
            return httpx.Response(200, json={"total_count": 101, "installations": installations})
        return httpx.Response(404)

    original_client = httpx.AsyncClient
    monkeypatch.setattr(github_oauth.httpx, "AsyncClient", lambda **kwargs: original_client(transport=httpx.MockTransport(provider), **kwargs))
    client, _, identity = api
    started = client.post("/auth/connections/oauth/github/start", json={"repository_access": True})
    assert started.status_code == 200, started.text
    assert identity.get_resource_oauth2_token.call_args.kwargs["scopes"] == []
    attempt = (await session.execute(select(OAuthConnectionAttempt))).scalar_one()
    returned = client.post(
        "/auth/connections/oauth/return",
        json={
            "state": started.json()["state"],
            "session_uri": started.json()["session_uri"],
        },
    )
    if fault is None:
        assert returned.status_code == 201, returned.text
        binding = (await session.execute(select(OAuthConnectionBinding))).scalar_one()
        credential = (await session.execute(select(UserCredential))).scalar_one()
        assert binding.provider_account_id == "213"
        assert binding.observed_resources[0]["installation_ids"] == list(range(1, 101))
        assert credential.scopes == {"github_app_grant": {"schema_version": 1, "repository_access": True}}
        assert attempt.consumed_at is not None
        assert pages == [1, 2]
    else:
        assert returned.status_code == 403 and returned.json()["detail"]["error"] == "github_reconnect_required"
        assert await session.scalar(select(OAuthConnectionBinding)) is None
        assert await session.scalar(select(UserCredential)) is None
        assert attempt.consumed_at is None
        assert pages == ([] if fault in ("client", "owner") else [1] if fault in ("incomplete_page", "oversized_page") else [1, 2])
    assert "synthetic-owner-token" not in returned.text


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["client", "owner", "installations"])
async def test_app_substitution_refuses_connection_and_mint(api, session, monkeypatch, fault):
    from src.crew.github_access import SWITCH
    from src.internal import credential_routes, github_oauth

    monkeypatch.setenv(SWITCH, "true")
    monkeypatch.setattr(github_oauth, "get_settings", lambda: SimpleNamespace(oauth_workload_name="adp-tools-dev-oauth-user", github_app_id="123"))
    monkeypatch.setattr(
        github_oauth,
        "github_app_client",
        AsyncMock(
            return_value={
                "client_id": "Iv1fixture",
                "client_secret": "synthetic-secret",
                "app_id": "123",
            }
        ),
    )
    monkeypatch.setattr(credential_routes, "_validate_proxy_url", lambda url, settings: httpx.URL(url))
    mode = {"fault": fault}

    def provider(request):
        if request.url.path == "/user":
            return httpx.Response(
                200,
                json={
                    "id": 214 if mode["fault"] in ("owner", "owner_changed") else 213,
                    "login": "fixture-user",
                    "type": "User",
                },
            )
        if request.url.path == "/applications/Iv1fixture/token":
            return httpx.Response(
                200,
                json={
                    "app": {"client_id": "other-client" if mode["fault"] == "client" else "Iv1fixture"},
                    "user": {"id": 214 if mode["fault"] == "owner_changed" else 213},
                },
            )
        if request.url.path == "/user/installations":
            return httpx.Response(
                200,
                json={
                    "total_count": 1,
                    "installations": [
                        {
                            "id": 42,
                            "app_id": 456 if mode["fault"] == "installations" else 123,
                        }
                    ],
                },
            )
        return httpx.Response(404)

    original_client = httpx.AsyncClient
    monkeypatch.setattr(github_oauth.httpx, "AsyncClient", lambda **kwargs: original_client(transport=httpx.MockTransport(provider), **kwargs))
    client, _, _ = api
    started = client.post("/auth/connections/oauth/github/start", json={"repository_access": True}).json()
    refused = client.post("/auth/connections/oauth/return", json={"state": started["state"], "session_uri": started["session_uri"]})
    assert refused.status_code == 403 and refused.json()["detail"]["error"] == "github_reconnect_required"
    assert await session.scalar(select(OAuthConnectionBinding)) is None
    assert await session.scalar(select(UserCredential)) is None

    mode["fault"] = None
    started = client.post("/auth/connections/oauth/github/start", json={"repository_access": True}).json()
    connected = client.post("/auth/connections/oauth/return", json={"state": started["state"], "session_uri": started["session_uri"]})
    assert connected.status_code == 201, connected.text
    assert client.post("/auth/connections/oauth/github/token").status_code == 200

    mode["fault"] = "owner_changed" if fault == "owner" else fault
    if fault == "owner":
        started = client.post("/auth/connections/oauth/github/start", json={"reconnect": True}).json()
        refused = client.post("/auth/connections/oauth/return", json={"state": started["state"], "session_uri": started["session_uri"]})
        assert refused.status_code == 403 and refused.json()["detail"]["error"] == "github_account_changed"
        assert (await session.scalar(select(OAuthConnectionBinding))).provider_account_id == "213"
    refused = client.post("/auth/connections/oauth/github/token")
    assert refused.status_code == 403 and refused.json()["detail"]["error"] == "github_reconnect_required"
    assert refused.headers["cache-control"] == "no-store"
    assert "synthetic-owner-token" not in refused.text


@pytest.mark.asyncio
async def test_legacy_github_repository_grant_remains_bound_but_requires_app_reconnect_for_token(api, session, monkeypatch):
    from src.crew import github_access
    from src.internal import credential_routes, github_oauth
    from src.shared.identity.oauth_providers import GITHUB_REPOSITORY_SCOPES

    monkeypatch.setenv(github_access.SWITCH, "true")
    monkeypatch.setattr(github_oauth, "get_settings", lambda: SimpleNamespace(oauth_workload_name="adp-tools-dev-oauth-user"))
    exchange = AsyncMock(return_value="gho_fixture_owner_token")
    monkeypatch.setattr(credential_routes, "_identity_access_token", exchange)
    granted = github_oauth.GithubObservation("213", ("read:user", "repo", "workflow"), {"id": 213, "login": "alice-gh"})
    monkeypatch.setattr(github_oauth, "observe_github_user", AsyncMock(return_value=granted))
    client, _, identity = api
    started = client.post("/auth/connections/oauth/github/start", json={"repository_access": True})
    assert started.status_code == 200, started.text
    assert identity.get_resource_oauth2_token.call_args.kwargs["scopes"] == GITHUB_REPOSITORY_SCOPES
    attempt = (await session.execute(select(OAuthConnectionAttempt))).scalar_one()
    assert attempt.requested_scopes == GITHUB_REPOSITORY_SCOPES
    connected = client.post("/auth/connections/oauth/return", json={"state": started.json()["state"], "session_uri": started.json()["session_uri"]})
    assert connected.status_code == 201, connected.text
    credential = (await session.execute(select(UserCredential))).scalar_one()
    binding = (await session.execute(select(OAuthConnectionBinding))).scalar_one()
    assert credential.scopes == {"oauth_scopes": GITHUB_REPOSITORY_SCOPES} and credential.agent_consent is True
    assert binding.observed_scopes == ["read:user", "repo", "workflow"]
    exchanges = exchange.await_count
    issued = client.post("/auth/connections/oauth/github/token")
    assert issued.status_code == 409 and issued.json()["detail"]["error"] == "github_reconnect_required"
    assert "gho_fixture_owner_token" not in issued.text
    assert issued.headers["cache-control"] == "no-store"
    assert exchange.await_count == exchanges
    events = [row.event_type for row in (await session.execute(select(AuditLog))).scalars().all()]
    assert events == ["oauth_connection_created"]
    # Reconnecting keeps the repository grant without asking for it again.
    session.expire_all()
    restarted = client.post("/auth/connections/oauth/github/start", json={"reconnect": True})
    assert restarted.status_code == 200, restarted.text
    assert identity.get_resource_oauth2_token.call_args.kwargs["scopes"] == GITHUB_REPOSITORY_SCOPES
    monkeypatch.delenv(github_access.SWITCH)
    disabled = client.post("/auth/connections/oauth/github/token")
    assert disabled.status_code == 503 and disabled.json()["detail"]["error"] == "github_user_access_disabled"


@pytest.mark.asyncio
async def test_github_repository_consent_is_refused_when_github_grants_less(api, session, monkeypatch):
    from src.internal import credential_routes, github_oauth

    monkeypatch.setattr(credential_routes, "_identity_access_token", AsyncMock(return_value="gho_fixture_owner_token"))
    # The owner unticked `repo` on GitHub's screen: profile-only grant for a repository request.
    profile_only = github_oauth.GithubObservation("213", ("read:user",), {"id": 213})
    monkeypatch.setattr(github_oauth, "observe_github_user", AsyncMock(return_value=profile_only))
    client, _, _ = api
    started = client.post("/auth/connections/oauth/github/start", json={"repository_access": True}).json()
    connected = client.post("/auth/connections/oauth/return", json={"state": started["state"], "session_uri": started["session_uri"]})
    assert connected.status_code == 403 and connected.json()["detail"]["error"] == "requested_scope_not_granted"
    assert (await session.execute(select(UserCredential))).scalars().all() == []
    assert (await session.execute(select(OAuthConnectionBinding))).scalars().all() == []


@pytest.mark.asyncio
async def test_owner_token_without_repository_consent_names_the_missing_step(api, session, monkeypatch):
    from src.crew import github_access
    from src.internal import credential_routes, github_oauth

    monkeypatch.setenv(github_access.SWITCH, "true")
    monkeypatch.setattr(github_oauth, "get_settings", lambda: SimpleNamespace(oauth_workload_name="adp-tools-dev-oauth-user"))
    monkeypatch.setattr(credential_routes, "_identity_access_token", AsyncMock(return_value="gho_fixture_owner_token"))
    client, _, _ = api
    missing = client.post("/auth/connections/oauth/github/token")
    assert missing.status_code == 404 and missing.json()["detail"]["error"] == "github_not_connected"
    started = client.post("/auth/connections/oauth/github/start").json()
    assert client.post("/auth/connections/oauth/return", json={"state": started["state"], "session_uri": started["session_uri"]}).status_code == 201
    profile_only = client.post("/auth/connections/oauth/github/token")
    assert profile_only.status_code == 403 and profile_only.json()["detail"]["error"] == "github_repository_consent_required"
    assert "gho_" not in profile_only.text
    # Repository access is GitHub-only; other providers refuse the request shape.
    assert client.post("/auth/connections/oauth/slack/start", json={"repository_access": True}).status_code == 400
    assert [row.event_type for row in (await session.execute(select(AuditLog))).scalars().all()] == ["oauth_connection_created"]


@pytest.mark.asyncio
async def test_first_consent_inserts_the_credential_before_its_binding_under_enforced_foreign_keys(api, session, monkeypatch):
    """Dev 2026-10-25: the first live GitHub consent failed with a Postgres ForeignKeyViolation because the
    binding INSERT ran before the credential INSERT (no relationship() between the mappers). SQLite does
    not enforce foreign keys unless asked, so every earlier run passed; this test asks."""
    from sqlalchemy import text

    from src.internal import credential_routes, github_oauth

    await session.execute(text("PRAGMA foreign_keys=ON"))
    monkeypatch.setattr(credential_routes, "_identity_access_token", AsyncMock(return_value="synthetic-owner-token"))
    observation = github_oauth.GithubObservation("213", ("read:user",), {"id": 213, "login": "alice"})
    monkeypatch.setattr(github_oauth, "observe_github_user", AsyncMock(return_value=observation))
    client, _, _ = api
    started = client.post("/auth/connections/oauth/github/start").json()
    connected = client.post("/auth/connections/oauth/return", json={"state": started["state"], "session_uri": started["session_uri"]})
    assert connected.status_code == 201, connected.text
    binding = (await session.execute(select(OAuthConnectionBinding))).scalar_one()
    credential = (await session.execute(select(UserCredential))).scalar_one()
    assert binding.credential_id == credential.id
