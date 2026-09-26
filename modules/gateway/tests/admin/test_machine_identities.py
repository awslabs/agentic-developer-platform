"""CLI-11 actual serializers, domain writes and durable provider admission."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.admin import machine_accounts, machine_agents
from src.admin.agent_schemas import AgentUpdateRequest
from src.admin.agent_service import AgentService
from src.auth.dependencies import get_current_user
from src.shared.database import get_db
from src.shared.exceptions import ConflictError
from src.shared.models.audit import AuditLog
from src.shared.models.base import Base
from src.shared.models.organization import Department, Organization, ServiceAccount, Team, User
from src.shared.schemas.auth import TokenContext


@pytest.fixture
async def fixture(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'machine.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        db.add_all(
            [
                Organization(id="org", name="Organization"),
                Department(id="dept", org_id="org", name="Department"),
                Team(id="team", org_id="org", department_id="dept", name="Team"),
                User(id="human", org_id="org", team_id="team", email="human@example.test"),
            ]
        )
        await db.commit()
    actor = TokenContext(
        user_id="human",
        org_id="org",
        team_id="team",
        department_id="dept",
        account_type="human",
        is_admin=True,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        auth_source="jwt",
    )
    app = FastAPI()
    app.include_router(machine_accounts.router, prefix="/admin")
    app.include_router(machine_agents.router, prefix="/admin")

    async def db_dependency():
        async with factory() as db:
            yield db

    app.dependency_overrides[get_db] = db_dependency
    app.dependency_overrides[get_current_user] = lambda: actor
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, factory, actor
    await engine.dispose()


@pytest.mark.asyncio
async def test_sql_account_real_boundary_retry_revision_delete(fixture):
    client, factory, actor = fixture
    body = {
        "operation_id": "56240000-0000-4000-8000-000000000010",
        "account": {"name": "Fixture", "department_id": "dept", "team_id": "team", "iam_role_arn": "arn:aws:iam::123456789012:role/fixture"},
    }
    path = "/admin/organizations/org/service-accounts"
    first = await client.post(path + "/register", json=body)
    assert first.status_code == 200, first.text
    retry = await client.post(path + "/register", json=body)
    assert retry.status_code == 200 and retry.json()["id"] == first.json()["id"], retry.text
    async with factory() as db:
        assert len(list(await db.scalars(select(ServiceAccount)))) == 1
    target = path + "/" + first.json()["id"] + "/identity"
    changed = await client.patch(target, json={"expected_revision": first.json()["revision"], "account": {"name": "Updated"}})
    assert changed.status_code == 200 and changed.json()["name"] == "Updated", changed.text
    stale = await client.delete(target, params={"expected_revision": first.json()["revision"]})
    assert stale.status_code == 409
    deleted = await client.delete(target, params={"expected_revision": changed.json()["revision"]})
    assert deleted.status_code == 200 and deleted.json()["deleted"]
    assert (await client.get(target)).status_code == 404
    # Durable receipt survives deletion: retry cannot create a replacement identity.
    assert (await client.post(path + "/register", json=body)).status_code == 404
    async with factory() as db:
        assert not list(await db.scalars(select(ServiceAccount)))
        assert list(await db.scalars(select(AuditLog)))


@pytest.mark.asyncio
async def test_account_foreign_scope_and_invalid_relationship_refused(fixture):
    client, factory, actor = fixture
    body = {
        "operation_id": "56240000-0000-4000-8000-000000000011",
        "account": {"name": "Fixture", "department_id": "foreign", "team_id": "team", "iam_role_arn": "arn:aws:iam::123456789012:role/fixture"},
    }
    path = "/admin/organizations/org/service-accounts/register"
    assert (await client.post(path, json=body)).status_code == 422
    assert (await client.post(path.replace("/org/", "/foreign/"), json=body)).status_code == 403
    actor.account_type = "service"
    assert (await client.post(path, json=body)).status_code == 403
    async with factory() as db:
        assert not list(await db.scalars(select(ServiceAccount)))


@pytest.mark.asyncio
async def test_provider_lost_response_does_not_remint(fixture, monkeypatch):
    client, factory, actor = fixture
    provider = SimpleNamespace(create_agent=AsyncMock(side_effect=RuntimeError("lost provider response")))
    monkeypatch.setattr(machine_agents, "service", lambda kind: provider)
    body = {"operation_id": "56240000-0000-4000-8000-000000000012", "org_id": "org", "agent": {"name": "Fixture"}}
    with pytest.raises(RuntimeError):
        await client.post("/admin/machine-agents/cognito-client/register", json=body)
    retry = await client.post("/admin/machine-agents/cognito-client/register", json=body)
    assert retry.status_code == 409 and retry.json()["detail"]["error"] == "registration_pending"
    assert provider.create_agent.await_count == 1
    changed = {**body, "operation_id": "56240000-0000-4000-8000-000000000013"}
    assert (await client.post("/admin/machine-agents/cognito-client/register", json=changed)).status_code == 409
    assert provider.create_agent.await_count == 1


@pytest.mark.asyncio
async def test_concurrent_provider_registration_admits_once(fixture, monkeypatch):
    client, factory, actor = fixture
    entered, release = asyncio.Event(), asyncio.Event()

    async def create(_):
        entered.set()
        await release.wait()
        return SimpleNamespace(client_id="client")

    row = SimpleNamespace(model_dump=lambda **_: {"client_id": "client", "name": "Fixture", "org_id": "org", "status": "active"})
    provider = SimpleNamespace(create_agent=AsyncMock(side_effect=create), get_agent=AsyncMock(return_value=row))
    monkeypatch.setattr(machine_agents, "service", lambda kind: provider)
    body = {"operation_id": "56240000-0000-4000-8000-000000000014", "org_id": "org", "agent": {"name": "Fixture"}}
    first = asyncio.create_task(client.post("/admin/machine-agents/cognito-client/register", json=body))
    await entered.wait()
    second = await client.post("/admin/machine-agents/cognito-client/register", json=body)
    assert second.status_code == 409
    release.set()
    assert (await first).status_code == 200
    replay = await client.post("/admin/machine-agents/cognito-client/register", json=body)
    assert replay.status_code == 200 and replay.json()["id"] == "client"
    assert provider.create_agent.await_count == 1


@pytest.mark.asyncio
async def test_cognito_retirement_keeps_receipt_and_uses_exact_client():
    table = MagicMock()
    table.get_item.return_value = {"Item": {"client_id": "client", "org_id": "org", "retirement_operation_id": "operation"}}
    provider = AgentService.__new__(AgentService)
    provider.dynamodb = MagicMock()
    provider.dynamodb.Table.return_value = table
    provider.table_name = "clients"
    provider.user_pool_id = "pool"
    provider.cognito = MagicMock()
    provider.get_agent = AsyncMock(return_value=SimpleNamespace(status="retired"))
    result = await provider.retire_agent("client", "org", expected_updated_at="old", operation_id="operation")
    assert result.status == "retired"
    provider.cognito.delete_user_pool_client.assert_called_once_with(UserPoolId="pool", ClientId="client")
    assert table.update_item.call_args.kwargs["ExpressionAttributeValues"][":operation"] == "operation"
    table.delete_item.assert_not_called()
    with pytest.raises(ConflictError):
        await provider.retire_agent("client", "org", expected_updated_at="old", operation_id="another")
    assert provider.cognito.delete_user_pool_client.call_count == 1


@pytest.mark.asyncio
async def test_cognito_updates_cannot_revive_a_retirement_tombstone():
    provider = AgentService.__new__(AgentService)
    provider.get_agent = AsyncMock(return_value=SimpleNamespace())
    provider.dynamodb = MagicMock()
    provider.table_name = "clients"
    table = provider.dynamodb.Table.return_value
    table.update_item.side_effect = RuntimeError("inspect admission only")
    with pytest.raises(RuntimeError):
        await provider.update_agent("client", "org", AgentUpdateRequest(status="active"), expected_updated_at="prior")
    condition = table.update_item.call_args.kwargs["ConditionExpression"]
    assert "attribute_not_exists(retirement_operation_id)" in condition
    assert "updated_at = :expected_updated_at" in condition


@pytest.mark.asyncio
async def test_cognito_retirement_stale_revision_never_calls_provider_delete():
    from botocore.exceptions import ClientError

    table = MagicMock()
    table.get_item.return_value = {"Item": {"client_id": "client", "org_id": "org"}}
    table.update_item.side_effect = ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")
    provider = AgentService.__new__(AgentService)
    provider.dynamodb = MagicMock()
    provider.dynamodb.Table.return_value = table
    provider.table_name = "clients"
    provider.user_pool_id = "pool"
    provider.cognito = MagicMock()
    with pytest.raises(ConflictError):
        await provider.retire_agent("client", "org", expected_updated_at="reviewed", operation_id="operation")
    condition = table.update_item.call_args.kwargs["ConditionExpression"]
    assert "updated_at = :expected" in condition and "org_id = :org" in condition
    provider.cognito.delete_user_pool_client.assert_not_called()


@pytest.mark.asyncio
async def test_machine_capabilities_require_human_and_correct_identity_type_permission(fixture):
    from src.admin.config import Permission
    from src.cli_capabilities.contract import BY_ID, _permitted

    _, _, actor = fixture
    access = SimpleNamespace(check_permission_for_discovery=AsyncMock(return_value=True))
    assert await _permitted(BY_ID["machine.agent.registry.manage"], actor, access) is True
    assert access.check_permission_for_discovery.call_args.args[1] == Permission.AGENT_REGISTER
    assert await _permitted(BY_ID["machine.agent.cognito.manage"], actor, access) is True
    assert access.check_permission_for_discovery.call_args.args[1] == Permission.ORG_UPDATE
    actor.account_type = "service"
    assert await _permitted(BY_ID["machine.agent.cognito.manage"], actor, access) is False
