"""Real login subjects and workspace user ids need not have the same value."""

from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from l5_services.models.base import Base as L5Base
from l5_services.models.configuration import Configuration, ConfigurationReceipt
from sqlalchemy import select

from src import l5_integration
from src.auth.dependencies import get_current_user
from src.internal import tools_l5
from src.shared.models.organization import User
from tests.products import test_l5_configuration_host
from tests.products.test_i05 import REQUEST

l5_owner = test_l5_configuration_host.l5_owner


@pytest.fixture
async def login_alias(app, db_session, l5_owner, monkeypatch):
    monkeypatch.setattr(tools_l5, "files", lambda package: Path(__file__).resolve().parents[4] / "docs/l5-services")
    tools_l5.l5_tool_catalogue.cache_clear()
    tools_l5.l5_operations.cache_clear()
    owner = await db_session.get(User, l5_owner.user_id)
    owner.cognito_sub = str(uuid4())
    await db_session.commit()
    login = l5_owner.model_copy(update={"user_id": owner.cognito_sub})
    login._authenticated_subject_id = owner.cognito_sub
    app.dependency_overrides[get_current_user] = lambda: login
    yield login, owner
    tools_l5.l5_tool_catalogue.cache_clear()
    tools_l5.l5_operations.cache_clear()


@pytest.mark.asyncio
async def test_mounted_draft_and_receipt_use_verified_workspace_owner(app, test_client, test_engine, db_session, login_alias):
    login, owner = login_alias
    async with test_engine.begin() as connection:
        await connection.run_sync(L5Base.metadata.create_all)
    response = await test_client.post("/v1/products", json=REQUEST, headers={"Idempotency-Key": "canonical_product_00001"})
    assert response.status_code == 201, response.text
    selected = response.json()
    path = f"/v1/products/{selected['product_id']}/projects/{selected['project_id']}/services/security/configuration"
    body = {"capabilities": ["code_review"]}
    headers = {"Idempotency-Key": "canonical_security_draft_00001", "If-None-Match": "*"}
    created = await test_client.put(path, json=body, headers=headers)
    assert created.status_code == 201, created.text
    assert created.json()["scope"]["owner_id"] == owner.id != login.user_id
    row = await db_session.scalar(select(Configuration).where(Configuration.configuration_id == created.json()["configuration_id"]))
    receipt = await db_session.scalar(select(ConfigurationReceipt).where(ConfigurationReceipt.configuration_id == row.configuration_id))
    assert row.owner_id == receipt.principal_id == owner.id
    assert (await test_client.get(path)).json()["configuration_id"] == row.configuration_id
    mcp = await test_client.post(
        "/v1/l5/mcp/tools/call",
        json={
            "tool": "l5___get_configuration",
            "arguments": {
                "product_id": selected["product_id"],
                "project_id": selected["project_id"],
                "service_id": "security",
            },
        },
    )
    assert mcp.status_code == 200, mcp.text
    assert not mcp.json().get("isError"), mcp.text
    assert mcp.json()["structuredContent"]["data"] == created.json()
    # The agent facade invokes the handler directly, without FastAPI resolving
    # dependencies. It must get the same owner as the public MCP/REST routes.
    direct = await l5_integration.call_l5_mcp_tool(
        l5_integration.L5ToolCall(
            tool="l5___get_configuration",
            arguments={
                "product_id": selected["product_id"],
                "project_id": selected["project_id"],
                "service_id": "security",
            },
        ),
        request=SimpleNamespace(app=app),
        actor=login,
        db=db_session,
    )
    assert not direct.get("isError")
    assert direct["structuredContent"]["data"] == created.json()
    replay = await test_client.put(path, json=body, headers=headers)
    assert replay.status_code == 201, replay.text
    assert replay.json()["configuration_id"] == row.configuration_id
    assert login.user_id == owner.cognito_sub


@pytest.mark.asyncio
async def test_context_copy_preserves_authenticated_subject_and_service_identity(db_session, login_alias):
    login, owner = login_alias
    selected = await l5_integration.l5_actor(db_session, login)
    assert selected.user_id == owner.id
    assert selected._authenticated_subject_id == login._authenticated_subject_id == owner.cognito_sub
    assert selected is not login and login.user_id == owner.cognito_sub
    service = login.model_copy(update={"account_type": "service", "user_id": "registered-worker"})
    assert await l5_integration.l5_actor(db_session, service) is service


@pytest.mark.asyncio
async def test_unknown_or_foreign_login_cannot_become_l5_owner(db_session, login_alias):
    from fastapi import HTTPException

    login, _ = login_alias
    for changes in ({"user_id": str(uuid4())}, {"org_id": "another-workspace", "is_admin": False}):
        with pytest.raises(HTTPException) as denied:
            await l5_integration.l5_actor(db_session, login.model_copy(update=changes))
        assert denied.value.status_code == 404


@pytest.mark.asyncio
async def test_mcp_does_not_resolve_unknown_login_or_bypass_service_grants(app, test_client, login_alias):
    login, _ = login_alias
    arguments = {
        "tool": "l5___get_configuration",
        "arguments": {
            "product_id": "some-product",
            "project_id": "some-project",
            "service_id": "security",
        },
    }
    for changes in (
        {"user_id": str(uuid4())},
        {"org_id": "another-workspace", "is_admin": False},
        {"account_type": "service", "user_id": "registered-worker", "agent_registry_id": None},
    ):
        app.dependency_overrides[get_current_user] = lambda changes=changes: login.model_copy(update=changes)
        denied = await test_client.post("/v1/l5/mcp/tools/call", json=arguments)
        assert denied.status_code == 404, denied.text
        assert denied.json() == {"detail": {"code": "not_found"}}
