"""An observed Slack token never substitutes for current ADP ownership."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.internal import slack_oauth
from src.internal.slack_reader import SlackIdentityObservation
from src.shared.models.base import Base
from src.shared.models.organization import Department, Organization, Team, User
from src.shared.models.vault import OAuthConnectionBinding, UserCredential


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as database:
        database.add(
            Organization(
                id="org-acme",
                name="Fixture",
                aws_accounts=[],
                role_mappings={},
                settings={},
                github_installation_ids=[],
                cognito_client_ids=[],
            )
        )
        database.add(Department(id="dept-eng", org_id="org-acme", name="Engineering"))
        database.add(Team(id="team-eng", org_id="org-acme", department_id="dept-eng", name="Team"))
        for owner_id in ("user-alice", "user-bob"):
            database.add(User(id=owner_id, org_id="org-acme", team_id="team-eng", email=f"{owner_id}@example.invalid"))
        await database.commit()
        yield database
    await engine.dispose()


@pytest.mark.asyncio
async def test_selected_slack_binding_rechecks_owner_revision_scopes_and_consent(session, monkeypatch):
    monkeypatch.setattr(slack_oauth, "get_settings", lambda: SimpleNamespace(oauth_workload_name="adp-tools-dev-oauth-user"))
    pointers = []
    for number, owner_id in enumerate(("user-alice", "user-bob"), start=1):
        pointer = UserCredential(
            id=f"00000000-0000-4000-8000-00000000000{number}",
            org_id="org-acme",
            user_id=owner_id,
            service="slack",
            provider="agentcore-identity",
            credential_type="oauth_token",
            label="AgentCore OAuth",
            secret_arn="agentcore-identity:adp-tools-dev-oauth-user:slack",
            scopes={"oauth_scopes": ["users:read"]},
            agent_consent=True,
        )
        session.add(pointer)
        session.add(slack_oauth.new_slack_binding(pointer, SlackIdentityObservation(f"U0000000{number}", "T00000001", ("users:read",))))
        pointers.append(pointer)
    await session.commit()
    selected, binding, claim = await slack_oauth.selected_slack_connection(session, user_id="user-alice", tenant_id="org-acme")
    assert selected.id == pointers[0].id and binding.provider_account_id == "T00000001:U00000001"
    assert claim["connection_id"] == pointers[0].id
    with pytest.raises(HTTPException):
        await slack_oauth.selected_slack_connection(session, user_id="user-bob", tenant_id="org-acme", expected=claim)
    pointers[0].agent_consent = False
    await session.commit()
    with pytest.raises(HTTPException):
        await slack_oauth.selected_slack_connection(session, user_id="user-alice", tenant_id="org-acme", expected=claim)
    pointers[0].agent_consent = True
    pointers[0].scopes = {"oauth_scopes": ["users:read", "chat:write"]}
    await session.commit()
    with pytest.raises(HTTPException):
        await slack_oauth.selected_slack_connection(session, user_id="user-alice", tenant_id="org-acme", expected=claim)
    await session.delete(pointers[0])
    await session.delete(await session.get(OAuthConnectionBinding, pointers[0].id))
    await session.commit()
    with pytest.raises(HTTPException):
        await slack_oauth.selected_slack_connection(session, user_id="user-alice", tenant_id="org-acme", expected=claim)


@pytest.mark.asyncio
async def test_database_failure_does_not_expose_connection_metadata():
    database = SimpleNamespace(execute=AsyncMock(side_effect=SQLAlchemyError("private connection failure")))
    with pytest.raises(HTTPException) as refused:
        await slack_oauth.selected_slack_connection(database, user_id="user-alice", tenant_id="org-acme")
    assert refused.value.status_code == 503
    assert refused.value.detail == "tool connection unavailable"
