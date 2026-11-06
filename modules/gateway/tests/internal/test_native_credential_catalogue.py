"""Configured native permissions can be administered without a broker target."""

from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.requests import Request

from src.admin import tool_routes
from src.auth import native_credential_authority
from src.internal import tools_catalogue
from src.internal.tools_platform import PLATFORM_TOOL_NAMES, implicitly_allowed
from src.shared.models.tool_entitlement import ToolEntitlement

NAMES = {"credentials.assume_role", "credentials.materialize"}


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.delenv("ADP_TOOLS_GATEWAY_ROLE_ARN", raising=False)
    for key, value in {
        "ENABLED": "true",
        "ISSUER": "https://native.example.test",
        "APPROVAL_ORIGIN": "https://app.example.test",
        "KEY_ID": "fixture-key",
        "KEY_ARN": "arn:aws:kms:us-east-1:123456789012:key/fixture-key",
    }.items():
        monkeypatch.setenv("ADP_NATIVE_CREDENTIALS_" + key, value)
    monkeypatch.setattr(
        native_credential_authority,
        "get_settings",
        lambda: SimpleNamespace(cognito_cli_client_id="native", cognito_client_id="browser"),
    )


def test_configured_native_permissions_exist_without_broker_or_implicit_grants(configured):
    catalogue = tools_catalogue.tool_catalogue()
    assert {tool["name"] for tool in catalogue["tools"]} == NAMES | PLATFORM_TOOL_NAMES
    native = [tool for tool in catalogue["tools"] if tool["name"] in NAMES]
    for tool in native:
        assert tool["native_private_delivery"] is True
        assert not tool.get("first_party") and not tool.get("read_only")
        assert not implicitly_allowed(tool["name"])
        assert tool["inputSchema"]["required"] == ["service", "label"]
        assert tool["inputSchema"]["additionalProperties"] is False
        assert set(tool["inputSchema"]["properties"]) == (
            {"service", "label", "purpose"} if tool["name"].endswith("assume_role") else {"service", "label"}
        )
    assert [entry["name"] for entry in catalogue["connectors"]] == ["credentials", "platform"]
    # The generic execution catalogue still needs its independently deployed gateway.
    with pytest.raises(HTTPException) as error:
        tools_catalogue.gateway_catalogue()
    assert error.value.status_code == 503
    native[0]["inputSchema"]["properties"].clear()
    assert tools_catalogue.tool_catalogue()["tools"][0]["inputSchema"]["properties"]


@pytest.mark.parametrize("enabled", [None, "false", "TRUE"])
def test_disabled_issuer_does_not_register_permissions(configured, monkeypatch, enabled):
    if enabled is None:
        monkeypatch.delenv("ADP_NATIVE_CREDENTIALS_ENABLED")
    else:
        monkeypatch.setenv("ADP_NATIVE_CREDENTIALS_ENABLED", enabled)
    assert {tool["name"] for tool in tools_catalogue.tool_catalogue()["tools"]} == PLATFORM_TOOL_NAMES


@pytest.mark.parametrize("missing", ["ISSUER", "APPROVAL_ORIGIN", "KEY_ID", "KEY_ARN"])
def test_enabled_incomplete_issuer_is_unavailable(configured, monkeypatch, missing):
    monkeypatch.delenv("ADP_NATIVE_CREDENTIALS_" + missing)
    with pytest.raises(HTTPException) as error:
        tools_catalogue.tool_catalogue()
    assert error.value.status_code == 503
    assert error.value.detail == {"error": "native_credentials_unavailable"}


def test_issuer_client_validation_and_gateway_failure_are_not_hidden(configured, monkeypatch):
    monkeypatch.setattr(native_credential_authority, "get_settings", lambda: SimpleNamespace(cognito_cli_client_id="same", cognito_client_id="same"))
    with pytest.raises(HTTPException):
        tools_catalogue.tool_catalogue()
    monkeypatch.setattr(
        native_credential_authority, "get_settings", lambda: SimpleNamespace(cognito_cli_client_id="native", cognito_client_id="browser")
    )
    monkeypatch.setenv("ADP_TOOLS_GATEWAY_ROLE_ARN", "fixture-tools")
    monkeypatch.setattr(tools_catalogue, "gateway_catalogue", Mock(side_effect=HTTPException(503, "tool catalogue unavailable")))
    with pytest.raises(HTTPException) as error:
        tools_catalogue.tool_catalogue()
    assert error.value.detail == "tool catalogue unavailable"


@pytest.mark.parametrize("existing", [set(), {"credentials.assume_role"}, NAMES])
def test_native_permissions_compose_with_ready_targets_without_duplicates(configured, monkeypatch, existing):
    monkeypatch.setenv("ADP_TOOLS_GATEWAY_ROLE_ARN", "fixture-tools")
    deployed = {
        "tools": [
            {"name": "credentials.list", "connector": "credentials", "inputSchema": {"type": "object"}},
            *[
                {"name": name, "connector": "credentials", "inputSchema": {"type": "object", "properties": {"service": {"type": "string"}}}}
                for name in sorted(existing)
            ],
        ],
        "connectors": [{"name": "credentials", "credential_kinds": ["GATEWAY_IAM_ROLE"], "dynamic": False}],
    }
    before = deepcopy(deployed)
    monkeypatch.setattr(tools_catalogue, "gateway_catalogue", lambda: deployed)
    merged = tools_catalogue.tool_catalogue()
    assert [tool["name"] for tool in merged["tools"]] == sorted(NAMES | PLATFORM_TOOL_NAMES | {"credentials.list"})
    assert [connector["name"] for connector in merged["connectors"]] == ["credentials", "platform"]
    for original in deployed["tools"]:
        assert next(tool for tool in merged["tools"] if tool["name"] == original["name"]) == original
    assert merged["connectors"][0] == deployed["connectors"][0]
    assert deployed == before


@pytest.mark.asyncio
async def test_admin_can_select_native_permission_but_policy_writer_does_not_consent(configured, monkeypatch):
    monkeypatch.setattr(tool_routes, "authorize", AsyncMock())
    monkeypatch.setattr(tool_routes, "require_policy_owner", AsyncMock())
    monkeypatch.setattr(tool_routes, "write_admin_audit", AsyncMock())
    monkeypatch.setattr(tool_routes, "mark_admin_effects", Mock())
    store = Mock()
    store.put_allowed.return_value = {
        "allowed_tools": ["credentials.assume_role"],
        "consented_tools": [],
        "tool_max_cost_usd": {"credentials.assume_role": Decimal("1")},
        "revision": 1,
    }
    monkeypatch.setattr(tool_routes, "ToolsPolicyStore", lambda **kwargs: store)
    result = await tool_routes.put_user_tool_policy(
        "owner",
        tool_routes.UserToolPolicyWrite(
            tenant_id="tenant",
            allowed_tools=["credentials.assume_role"],
            tool_max_cost_usd={"credentials.assume_role": "1"},
            expected_revision=0,
        ),
        Request({"type": "http", "headers": []}),
        object(),
        object(),
        SimpleNamespace(store=SimpleNamespace(table="authority", client=object())),
    )
    assert result["consented_tools"] == []
    written = store.put_allowed.call_args.kwargs
    assert written["allowed_tools"] == ["credentials.assume_role"] and written["expected_revision"] == 0
    assert [tool["name"] for tool in written["catalog"]] == ["credentials___assume_role"]
    assert "consented_tools" not in written


@pytest.mark.asyncio
async def test_admin_can_entitle_native_permission_with_real_catalogue(configured, monkeypatch):
    from src.internal.tool_entitlements import effective_entitlement

    monkeypatch.setattr(tool_routes, "authorize", AsyncMock())
    monkeypatch.setattr(tool_routes, "write_admin_audit", AsyncMock())
    monkeypatch.setattr(tool_routes, "mark_admin_effects", Mock())
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(ToolEntitlement.__table__.create)
    try:
        async with async_sessionmaker(engine)() as db:
            read = dict(tenant_id="tenant", team_id="team", tool_name="credentials.assume_role")
            assert await effective_entitlement(db, **read) == (False, None)
            body = tool_routes.EntitlementWrite(
                tenant_id="tenant",
                tool_name="credentials.assume_role",
                enabled=True,
                max_cost_usd="1",
                expected_revision=0,
            )
            result = await tool_routes.put_entitlement(body, Request({"type": "http", "headers": []}), object(), db)
            assert result["revision"] == 1 and result["enabled"] is True
            assert await effective_entitlement(db, **read) == (True, Decimal("1"))
            with pytest.raises(HTTPException) as error:
                await tool_routes.put_entitlement(body, Request({"type": "http", "headers": []}), object(), db)
            assert error.value.status_code == 409
    finally:
        await engine.dispose()
