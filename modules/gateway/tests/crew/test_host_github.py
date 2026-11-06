"""A Dev Box's gh/git act as the owner only through the owner's consented GitHub connection (#8555)."""

import time
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from src.crew import github_access, host_github
from src.crew.model_token import mint_model_token
from src.internal import credential_routes, github_oauth
from src.proxy.routes import get_token_context, set_agent_run_id_from_header
from src.shared.identity.oauth_providers import GITHUB_REPOSITORY_SCOPES
from src.shared.models.audit import AuditLog
from src.shared.models.base import Base
from src.shared.models.organization import Department, Organization, Team, User
from src.shared.models.vault import OAuthConnectionBinding, UserCredential
from tests.crew.test_host_tools import INSTANCE, active, host_context, registry  # noqa: F401  (fixtures)

WORKLOAD = "adp-tools-dev-oauth-user"


@pytest.fixture
async def session(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        tenant = dict(id="tenant-a", name="Tenant A", aws_accounts=[], role_mappings={}, settings={})
        db.add(Organization(**tenant, github_installation_ids=[], cognito_client_ids=[]))
        db.add(Department(id="dept", org_id="tenant-a", name="Eng"))
        db.add(Team(id="team", org_id="tenant-a", department_id="dept", name="Eng"))
        db.add(User(id="owner-a", org_id="tenant-a", team_id="team", email="owner@example.com"))
        await db.commit()
        monkeypatch.setattr(host_github, "get_session_factory", lambda: factory)
        monkeypatch.setattr(github_oauth, "get_settings", lambda: SimpleNamespace(oauth_workload_name=WORKLOAD, github_app_id="123"))
        yield db
    await engine.dispose()


async def connect(session, *, scopes=None, observed=None, consent=True, account="213"):
    app_grant = scopes is None
    scopes = list(GITHUB_REPOSITORY_SCOPES) if scopes is None else scopes
    credential = UserCredential(
        id="conn-1",
        org_id="tenant-a",
        user_id="owner-a",
        service="github",
        credential_type="oauth_token",
        provider="agentcore-identity",
        label="AgentCore OAuth",
        secret_arn=f"agentcore-identity:{WORKLOAD}:github",
        scopes={"github_app_grant": {"schema_version": 1, "repository_access": True}} if app_grant else {"oauth_scopes": scopes},
        strict=True,
        agent_consent=consent,
    )
    session.add(credential)
    await session.flush()
    session.add(
        OAuthConnectionBinding(
            credential_id="conn-1",
            org_id="tenant-a",
            user_id="owner-a",
            provider="github",
            revision="00000000-0000-4000-8000-000000000001",
            credential_digest=github_oauth.credential_digest(credential),
            provider_account_id=account,
            observed_scopes=[] if app_grant else sorted(scopes if observed is None else observed),
            observed_resources=[{"kind": "github_app_user", "schema_version": 1, "app_id": 123, "installation_ids": [42], "account_id": account}]
            if app_grant
            else None,
            verified_at=datetime.now(UTC),
        )
    )
    await session.commit()
    return credential


@pytest.fixture
def api(active, session, monkeypatch):  # noqa: F811
    monkeypatch.setenv(github_access.SWITCH, "true")
    monkeypatch.setattr(credential_routes, "_identity_access_token", AsyncMock(return_value="gho_fixture_owner_token"))
    observation = github_oauth.GithubObservation(
        "213",
        (),
        {"id": 213, "login": "owner-login"},
        ({"kind": "github_app_user", "schema_version": 1, "app_id": 123, "installation_ids": [42], "account_id": "213"},),
    )
    observe = AsyncMock(return_value=observation)
    monkeypatch.setattr(github_oauth, "observe_github_app_user", observe)
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
    app = FastAPI()
    app.include_router(host_github.router)
    identity = SimpleNamespace(context=host_context())

    async def authenticated(request: Request):
        request.state.token_context = identity.context
        return identity.context

    async def run_header(request: Request):
        return request.headers.get("x-agent-runid")

    app.dependency_overrides[get_token_context] = authenticated
    app.dependency_overrides[set_agent_run_id_from_header] = run_header
    app.dependency_overrides[host_github.get_identity_client] = lambda: MagicMock()
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://host")
    return SimpleNamespace(client=client, identity=identity, observe=observe, observation=observation)


async def mint(api, active, token):  # noqa: F811
    headers = {"X-Agent-RunId": active.run_id, "Authorization": "fictional-sigv4"}
    if token is not None:
        headers["X-Adp-Crew-Model-Token"] = token
    return await api.client.post("/crew/github-token", headers=headers)


async def test_current_host_receives_the_owners_token_and_an_audit_row_without_the_token(api, active, session):  # noqa: F811
    await connect(session)
    token = await mint_model_token(active.owner, active.target, now=active.now)
    response = await mint(api, active, token)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["token"] == "gho_fixture_owner_token" and body["login"] == "owner-login"
    assert github_access.TOKEN_HORIZON_SECONDS == 600
    assert active.now + github_access.TOKEN_HORIZON_SECONDS - 5 <= body["expires_at"] <= int(time.time()) + github_access.TOKEN_HORIZON_SECONDS
    assert response.headers["cache-control"] == "no-store"
    api.observe.assert_awaited_once_with("gho_fixture_owner_token", client_id="Iv1fixture", client_secret="synthetic-secret", app_id="123")
    audit = (await session.execute(select(AuditLog))).scalar_one()
    assert audit.event_type == "crew_github_token_minted" and audit.actor_id == "owner-a" and audit.org_id == "tenant-a"
    assert audit.details["run_id"] == active.run_id and audit.details["instance_id"] == INSTANCE and audit.details["login"] == "owner-login"
    assert "gho_fixture_owner_token" not in str(audit.details)
    credential = (await session.execute(select(UserCredential))).scalar_one()
    assert credential.last_used_at is not None


async def test_switch_off_refuses_before_any_identity_or_provider_call(api, active, session, monkeypatch):  # noqa: F811
    await connect(session)
    monkeypatch.delenv(github_access.SWITCH)
    token = await mint_model_token(active.owner, active.target, now=active.now)
    response = await mint(api, active, token)
    assert response.status_code == 503 and response.json()["detail"]["error"] == "github_user_access_disabled"
    api.observe.assert_not_awaited()
    assert (await session.execute(select(AuditLog))).scalars().all() == []


@pytest.mark.parametrize(
    "setup,status,error",
    [
        (dict(), 404, "github_not_connected"),
        (dict(consent=False), 403, "github_consent_required"),
        (dict(scopes=["read:user"]), 403, "github_repository_consent_required"),
        (dict(scopes=list(GITHUB_REPOSITORY_SCOPES)), 409, "github_reconnect_required"),
        # The owner asked for repository access but unticked `repo` on GitHub's screen.
        (dict(scopes=list(GITHUB_REPOSITORY_SCOPES), observed=["read:user"]), 403, "github_repository_consent_required"),
        # An unreviewed grant shape never qualifies.
        (dict(scopes=["read:user", "repo"]), 409, "github_reconnect_required"),
    ],
)
async def test_each_missing_prerequisite_is_named_and_leaves_no_audit_row(api, active, session, setup, status, error):  # noqa: F811
    if setup is not None and (setup or "consent" in setup or "scopes" in setup or "observed" in setup):
        await connect(session, **setup)
    token = await mint_model_token(active.owner, active.target, now=active.now)
    response = await mint(api, active, token)
    assert response.status_code == status, response.text
    assert response.json()["detail"]["error"] == error
    api.observe.assert_not_awaited()
    assert (await session.execute(select(AuditLog))).scalars().all() == []


async def test_identity_or_account_change_requires_reconnect(api, active, session, monkeypatch):  # noqa: F811
    await connect(session)
    token = await mint_model_token(active.owner, active.target, now=active.now)
    unavailable = HTTPException(502, detail={"error": "identity_unavailable"})
    monkeypatch.setattr(credential_routes, "_identity_access_token", AsyncMock(side_effect=unavailable))
    response = await mint(api, active, token)
    assert response.status_code == 409 and response.json()["detail"]["error"] == "github_reconnect_required"
    monkeypatch.setattr(credential_routes, "_identity_access_token", AsyncMock(return_value="gho_other"))
    api.observe.return_value = github_oauth.GithubObservation(
        "999", api.observation.scopes, {"id": 999, "login": "someone-else"}, api.observation.resources
    )
    response = await mint(api, active, token)
    assert response.status_code == 403 and response.json()["detail"]["error"] == "github_reconnect_required"
    assert "gho_other" not in response.text
    assert (await session.execute(select(AuditLog))).scalars().all() == []


async def test_fenced_or_foreign_host_never_reaches_the_connection(api, active, session):  # noqa: F811
    await connect(session)
    token = await mint_model_token(active.owner, active.target, now=active.now)
    assert (await mint(api, active, None)).status_code == 401
    assert (await mint(api, active, "not-a-token")).status_code == 401
    api.identity.context = host_context("i-0fedcba9876543210")
    assert (await mint(api, active, token)).status_code == 403
    api.identity.context = host_context()
    active.resolver.fenced = True
    response = await mint(api, active, token)
    assert response.status_code == 409 and response.json()["detail"]["error"] == "lease_fenced"
    api.observe.assert_not_awaited()
