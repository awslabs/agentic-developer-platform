"""Admin tool discovery and scoped, revision-checked entitlement changes."""

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.admin import tool_routes
from src.admin.config import ROLE_PERMISSIONS, AdminRole, Permission
from src.agentauth.routes import get_agent_runtime
from src.auth.dependencies import get_current_user
from src.internal import tools_catalogue
from src.shared.database import get_db
from src.shared.models.audit import AuditLog
from src.shared.models.organization import Organization, Team, TeamMembership, User
from src.shared.models.tool_entitlement import ToolEntitlement


@pytest.mark.asyncio
async def test_admin_routes_require_scoped_human_and_reject_stale_or_foreign_team(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(ToolEntitlement.__table__.create)
        await connection.run_sync(Team.__table__.create)
        await connection.run_sync(AuditLog.__table__.create)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    checks = []

    async def check_permission(self, caller, permission, *, target_org_id):
        checks.append((permission, target_org_id))
        if caller.org_id != target_org_id:
            raise HTTPException(403, "wrong tenant")

    monkeypatch.setattr(tool_routes.AccessControl, "check_permission", check_permission)
    monkeypatch.setattr(
        tool_routes,
        "tool_catalogue",
        lambda: {
            "tools": [
                {
                    "name": "browser.start",
                    "connector": "browser",
                    "description": "Start",
                    "credential_kinds": ["GATEWAY_IAM_ROLE"],
                    "inputSchema": {"type": "object"},
                }
            ],
            "connectors": [],
        },
    )
    caller = SimpleNamespace(account_type="human", auth_source="jwt", org_id="tenant-one", user_id="admin-one")
    app = FastAPI()
    app.include_router(tool_routes.router)
    app.dependency_overrides[get_current_user] = lambda: caller

    async def get_session():
        async with session_factory() as db:
            yield db

    app.dependency_overrides[get_db] = get_session
    try:
        async with session_factory() as db:
            db.add_all(
                [
                    Team(id="team-x", org_id="tenant-one", department_id="d", name="X"),
                    Team(id="foreign", org_id="tenant-two", department_id="d", name="Foreign"),
                ]
            )
            await db.commit()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            base = "/admin/tools"
            assert (await client.get(base + "/catalog", params={"tenant_id": "tenant-two"})).status_code == 403
            assert (await client.get(base + "/catalog", params={"tenant_id": "tenant-one"})).json()["tools"][0]["name"] == "browser.start"
            body = {"tenant_id": "tenant-one", "tool_name": "browser.start", "enabled": True, "max_cost_usd": "0.20", "expected_revision": 0}
            assert (await client.put(base + "/entitlements", json={**body, "team_id": "foreign"})).status_code == 404
            assert (await client.put(base + "/entitlements", json={**body, "max_cost_usd": "-1"})).status_code == 422
            assert (await client.put(base + "/entitlements", json={**body, "tool_name": "other.missing"})).status_code == 422
            premature_team = await client.put(base + "/entitlements", json={**body, "team_id": "team-x"})
            assert premature_team.status_code == 409
            assert "tenant" in premature_team.json()["detail"]
            saved = await client.put(base + "/entitlements", json=body)
            assert saved.status_code == 200
            assert saved.json()["revision"] == 1
            assert saved.headers["X-Admin-Operation-Id"]
            assert isinstance(saved.json()["max_cost_usd"], str)
            assert (await client.put(base + "/entitlements", json=body)).status_code == 409
            team_grant = await client.put(base + "/entitlements", json={**body, "team_id": "team-x", "max_cost_usd": "0.10"})
            assert team_grant.status_code == 200
            assert team_grant.json()["max_cost_usd"] == "0.10"
            disabled = await client.put(base + "/entitlements", json={**body, "team_id": "team-x", "enabled": False, "expected_revision": 1})
            assert disabled.status_code == 200
            listed = (await client.get(base + "/entitlements", params={"tenant_id": "tenant-one", "team_id": "team-x"})).json()
            assert len(listed["entitlements"]) == 2
            assert isinstance(listed["entitlements"][0]["max_cost_usd"], str)
            changed = await client.put(base + "/entitlements", json={**body, "expected_revision": 1, "enabled": False})
            assert changed.status_code == 200
            assert changed.json()["revision"] == 2
            async with session_factory() as db:
                receipts = (await db.scalars(select(AuditLog))).all()
                successful = [row for row in receipts if row.event_type == "admin_update_tool_entitlement"]
                assert len(successful) == 4
                assert successful[0].details["operation_id"] == saved.headers["X-Admin-Operation-Id"]
                assert successful[0].details["target_tenant"] == "tenant-one"
                assert successful[0].details["revision"] == 1
                assert successful[0].details["team_id"] is None
                assert successful[0].details["enabled"] is True
                assert successful[0].details["max_cost_usd"] == "0.20"
            caller.account_type = "service"
            assert (await client.put(base + "/entitlements", json={**body, "expected_revision": 2})).status_code == 403
            assert checks and all(permission == Permission.TOOLS_MANAGE for permission, _ in checks)
    finally:
        await engine.dispose()
    assert Permission.TOOLS_MANAGE in ROLE_PERMISSIONS[AdminRole.ORG_ADMIN]
    assert Permission.TOOLS_MANAGE not in ROLE_PERMISSIONS[AdminRole.MEMBER]


def test_catalogue_reads_deployed_target_schemas_and_refuses_ambiguity(monkeypatch):
    monkeypatch.setenv("ADP_TOOLS_GATEWAY_ROLE_ARN", "arn:aws:iam::123456789012:role/tools")
    client = Mock()

    def paginator(name):
        if name == "list_gateways":
            return SimpleNamespace(paginate=lambda: [{"items": [{"gatewayId": "g1", "name": "adp-tools"}, {"gatewayId": "g2", "name": "other"}]}])
        return SimpleNamespace(paginate=lambda **kwargs: [{"items": [{"targetId": "t1", "status": "READY"}, {"targetId": "t2", "status": "READY"}]}])

    client.get_paginator.side_effect = paginator
    client.get_gateway.return_value = {"roleArn": "arn:aws:iam::123456789012:role/tools"}
    browser_schema = {
        "type": "object",
        "properties": [
            {"name": "request_id", "type": "string", "required": True},
            {"name": "url", "type": "string", "description": "Target", "required": True},
        ],
    }
    client.get_gateway_target.side_effect = [
        {
            "name": "browser",
            "targetConfiguration": {
                "mcp": {
                    "lambda": {
                        "toolSchema": {
                            "inlinePayload": [
                                {"name": "start", "description": "Open browser", "inputSchema": browser_schema},
                            ]
                        }
                    }
                }
            },
            "credentialProviderConfigurations": [{"credentialProviderType": "GATEWAY_IAM_ROLE"}],
        },
        {
            "name": "github",
            "targetConfiguration": {
                "mcp": {"openApiSchema": {"inlinePayload": "paths:\n  /user:\n    get:\n      operationId: get_user\n      summary: Read profile\n"}}
            },
            "credentialProviderConfigurations": [{"credentialProviderType": "OAUTH"}],
        },
    ]
    monkeypatch.setattr(tools_catalogue.boto3, "client", lambda service: client)
    assert tools_catalogue.gateway_catalogue() == {
        "tools": [
            {
                "name": "browser.start",
                "description": "Open browser",
                "connector": "browser",
                "credential_kinds": ["GATEWAY_IAM_ROLE"],
                "inputSchema": {"type": "object", "properties": {"url": {"type": "string", "description": "Target"}}, "required": ["url"]},
            },
            {
                "name": "github.get_user",
                "description": "Read profile",
                "connector": "github",
                "credential_kinds": ["OAUTH"],
                "inputSchema": {"type": "object"},
            },
        ],
        "connectors": [
            {"name": "browser", "credential_kinds": ["GATEWAY_IAM_ROLE"], "dynamic": False},
            {"name": "github", "credential_kinds": ["OAUTH"], "dynamic": False},
        ],
    }
    client.get_gateway_target.reset_mock()
    client.get_paginator.side_effect = lambda name: (
        SimpleNamespace(paginate=lambda: [{"items": [{"gatewayId": "g1", "name": "adp-tools"}]}])
        if name == "list_gateways"
        else SimpleNamespace(paginate=lambda **kwargs: [{"items": [{"targetId": "t1", "status": "DELETING"}]}])
    )
    tools_catalogue.invalidate_catalogue_cache()  # the control plane changed; the gateway caches a good read for CATALOGUE_TTL_SECONDS
    assert tools_catalogue.gateway_catalogue()["tools"] == []
    client.get_gateway_target.assert_not_called()
    client.get_gateway.return_value = {"roleArn": "another-role"}
    tools_catalogue.invalidate_catalogue_cache()
    with pytest.raises(HTTPException) as error:
        tools_catalogue.gateway_catalogue()
    assert error.value.status_code == 503


@pytest.mark.asyncio
async def test_user_policy_admin_route_is_scoped_audited_and_cannot_consent(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Team.__table__.create)
        await connection.run_sync(User.__table__.create)
        await connection.run_sync(TeamMembership.__table__.create)
        await connection.run_sync(ToolEntitlement.__table__.create)
        await connection.run_sync(AuditLog.__table__.create)
        await connection.run_sync(Organization.__table__.create)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async def check_permission(self, caller, permission, *, target_org_id):
        if target_org_id != caller.org_id:
            raise HTTPException(403, "wrong tenant")

    monkeypatch.setattr(tool_routes.AccessControl, "check_permission", check_permission)
    monkeypatch.setattr(
        tool_routes,
        "tool_catalogue",
        lambda: {
            "tools": [
                {"name": "browser.start", "description": "Start browser", "inputSchema": {"type": "object"}},
                {"name": "code_interpreter.start", "description": "Start interpreter", "inputSchema": {"type": "object"}},
            ],
            "connectors": [],
        },
    )
    store = Mock()
    store.get.return_value = None
    store.put_allowed.return_value = {
        "allowed_tools": ["browser.start"],
        "consented_tools": [],
        "tool_max_cost_usd": {"browser.start": Decimal("0.20")},
        "revision": 1,
    }
    monkeypatch.setattr(tool_routes, "ToolsPolicyStore", lambda **kwargs: store)
    caller = SimpleNamespace(account_type="human", auth_source="jwt", org_id="tenant-one", user_id="admin-one")
    app = FastAPI()
    app.include_router(tool_routes.router)
    app.dependency_overrides[get_current_user] = lambda: caller
    app.dependency_overrides[get_agent_runtime] = lambda: SimpleNamespace(store=SimpleNamespace(table="authority", client=Mock()))

    async def get_session():
        async with session_factory() as db:
            yield db

    app.dependency_overrides[get_db] = get_session
    try:
        async with session_factory() as db:
            db.add_all(
                [
                    Team(id="team-x", org_id="tenant-one", department_id="d", name="X"),
                    User(id="user-one", org_id="tenant-one", team_id="team-x", email="one@example.test"),
                    User(id="user-two", org_id="tenant-two", team_id="team-x", email="two@example.test"),
                    ToolEntitlement(org_id="tenant-one", team_id="", tool_name="browser.start", enabled=True),
                ]
            )
            await db.commit()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            path = "/admin/tools/users/user-one/policy"
            body = {
                "tenant_id": "tenant-one",
                "allowed_tools": ["browser.start"],
                "tool_max_cost_usd": {"browser.start": "0.20"},
                "expected_revision": 0,
            }
            assert (await client.get(path, params={"tenant_id": "tenant-one"})).json()["revision"] == 0
            assert (await client.put(path, json={**body, "tenant_id": "tenant-two"})).status_code == 403
            assert (await client.put("/admin/tools/users/user-two/policy", json=body)).status_code == 404
            assert (await client.put(path, json={**body, "allowed_tools": ["browser.start", "browser.start"]})).status_code == 422
            unknown = {**body, "allowed_tools": ["unknown.call"], "tool_max_cost_usd": {"unknown.call": "0.20"}}
            assert (await client.put(path, json=unknown)).status_code == 422
            assert (await client.put(path, json={**body, "tool_max_cost_usd": {"browser.start": "-1"}})).status_code == 422
            saved = await client.put(path, json=body)
            assert saved.status_code == 200
            assert saved.json()["consented_tools"] == []
            assert saved.json()["tool_max_cost_usd"] == {"browser.start": "0.20"}
            assert saved.headers["X-Admin-Operation-Id"]
            store.get.return_value = {
                "status": "active",
                "revision": 1,
                "allowed_tools": ["browser.start"],
                "consented_tools": [],
                "tool_max_cost_usd": {"browser.start": "0.20"},
            }
            access = await client.get("/admin/tools/users/user-one/access", params={"tenant_id": "tenant-one"})
            assert access.status_code == 200 and access.json()["policy_revision"] == 1
            # #8387: the tenant grants consent by default, so the entitled, never-edited row is ready.
            assert access.json()["consent_mode"] == "default"
            assert next(tool for tool in access.json()["tools"] if tool["name"] == "browser.start")["reason"] is None
            store.get.return_value["consent_mode"] = "explicit"
            access = await client.get("/admin/tools/users/user-one/access", params={"tenant_id": "tenant-one"})
            assert access.json()["consent_mode"] == "explicit"
            assert next(tool for tool in access.json()["tools"] if tool["name"] == "browser.start")["reason"] == "not_consented"
            shown = (await client.get(path, params={"tenant_id": "tenant-one"})).json()
            assert shown["consent_mode"] == "explicit" and shown["effective_consented_tools"] == [] and shown["consented_tools"] == []
            assert (await client.get("/admin/tools/users/user-two/access", params={"tenant_id": "tenant-one"})).status_code == 404
            assert (await client.get("/admin/tools/users/user-one/access", params={"tenant_id": "tenant-two"})).status_code == 403
            store.put_allowed.assert_called_once_with(
                tenant_id="tenant-one",
                owner_id="user-one",
                allowed_tools=["browser.start"],
                tool_max_cost_usd={"browser.start": Decimal("0.20")},
                catalog=[{"name": "browser___start", "description": "Start browser", "inputSchema": {"type": "object"}}],
                expected_revision=0,
            )
            async with session_factory() as db:
                receipts = (await db.scalars(select(AuditLog))).all()
                successful = [row for row in receipts if row.event_type == "admin_update_user_tool_policy"]
                assert len(successful) == 1
                assert successful[0].details["operation_id"] == saved.headers["X-Admin-Operation-Id"]
                assert successful[0].details["revision"] == 1
                assert successful[0].details["allowed_tools"] == ["browser.start"]
                assert successful[0].details["consented_tools"] == []
                assert successful[0].details["tool_max_cost_usd"] == {"browser.start": "0.20"}
            caller.account_type = "service"
            assert (await client.put(path, json=body)).status_code == 403
    finally:
        await engine.dispose()


def test_public_catalogue_omits_provider_recovery_and_preserves_other_connectors(monkeypatch):
    role = "arn:aws:iam::123456789012:role/tools"
    monkeypatch.setenv("ADP_TOOLS_GATEWAY_ROLE_ARN", role)
    public_operations = {
        "websearch": ("search",),
        "code-interpreter": ("start", "execute", "result", "file", "close"),
        "browser": ("start", "step", "inspect", "close"),
    }
    schema = {
        "type": "object",
        "properties": [
            {"name": "execution_id", "type": "string", "required": True},
            {"name": "request_id", "type": "string", "required": True},
            {"name": "adp_assertion", "type": "string", "required": False},
            {"name": "handle", "type": "string", "required": True},
        ],
    }
    operations = {family: (*names, "operation_get", "artifact_read", "execution_close") for family, names in public_operations.items()}
    assert sum(map(len, operations.values())) == 19
    operations.update({"github": ("operation_get",), "knowledge": ("artifact_read",), "vendor": ("execution_close",)})
    targets = {
        family: {
            "name": family,
            "targetConfiguration": {"mcp": {"lambda": {"toolSchema": {"inlinePayload": [{"name": name, "inputSchema": schema} for name in names]}}}},
            "credentialProviderConfigurations": [{"credentialProviderType": "GATEWAY_IAM_ROLE"}],
        }
        for family, names in operations.items()
    }
    client = Mock()
    client.get_gateway.return_value = {"roleArn": role}
    client.get_gateway_target.side_effect = lambda **kwargs: targets[kwargs["targetId"]]
    client.get_paginator.side_effect = lambda name: SimpleNamespace(
        paginate=lambda **kwargs: [
            {
                "items": [{"gatewayId": "g1", "name": "adp-tools"}]
                if name == "list_gateways"
                else [{"targetId": family, "status": "READY"} for family in targets]
            }
        ]
    )
    monkeypatch.setattr(tools_catalogue.boto3, "client", lambda service: client)
    catalogue = tools_catalogue.gateway_catalogue()
    public_names = {f"{family.replace('-', '_')}.{name}" for family, names in public_operations.items() for name in names}
    assert {tool["name"] for tool in catalogue["tools"]} == public_names | {
        "github.operation_get",
        "knowledge.artifact_read",
        "vendor.execution_close",
    }
    assert len(catalogue["tools"]) == 13
    assert {connector["name"] for connector in catalogue["connectors"]} == set(targets)
    for tool in catalogue["tools"]:
        assert tool["inputSchema"] == {"type": "object", "properties": {"handle": {"type": "string", "description": ""}}, "required": ["handle"]}


def test_public_schema_removes_internal_gateway_fields():
    schema = tools_catalogue.public_tool_schema(
        {
            "type": "object",
            "properties": [
                {"name": "execution_id", "type": "string", "required": True},
                {"name": "request_id", "type": "string", "required": True},
                {"name": "adp_assertion", "type": "string", "required": False},
                {"name": "url", "type": "string", "description": "Public target", "required": True},
            ],
        }
    )
    assert schema == {"type": "object", "properties": {"url": {"type": "string", "description": "Public target"}}, "required": ["url"]}


@pytest.mark.asyncio
async def test_tenant_consent_default_is_scoped_audited_and_clearable(monkeypatch):
    """#8387: an administrator reads and sets the tenant posture; None returns to the platform flag."""
    monkeypatch.setenv("ADP_TOOLS_CONSENT_DEFAULT", "granted")
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        for model in (Organization, AuditLog):
            await connection.run_sync(model.__table__.create)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async def check_permission(self, caller, permission, *, target_org_id):
        if target_org_id != caller.org_id:
            raise HTTPException(403, "wrong tenant")

    monkeypatch.setattr(tool_routes.AccessControl, "check_permission", check_permission)
    caller = SimpleNamespace(account_type="human", auth_source="jwt", org_id="tenant-one", user_id="admin-one")
    app = FastAPI()
    app.include_router(tool_routes.router)
    app.dependency_overrides[get_current_user] = lambda: caller

    async def get_session():
        async with session_factory() as db:
            yield db

    app.dependency_overrides[get_db] = get_session
    try:
        async with session_factory() as db:
            db.add(Organization(id="tenant-one", name="One", settings={"crew": {"allowed_targets": ["eks-pod"]}}))
            await db.commit()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            shown = await client.get("/admin/tools/consent-default", params={"tenant_id": "tenant-one"})
            assert shown.status_code == 200
            assert shown.json() == {
                "tenant_id": "tenant-one",
                "consent_default": "granted",
                "tenant_override": None,
                "platform_default": "granted",
                "choices": ["granted", "explicit"],
            }
            assert (await client.get("/admin/tools/consent-default", params={"tenant_id": "tenant-two"})).status_code == 403
            assert (await client.put("/admin/tools/consent-default", json={"tenant_id": "tenant-one", "consent_default": "maybe"})).status_code == 422
            saved = await client.put("/admin/tools/consent-default", json={"tenant_id": "tenant-one", "consent_default": "explicit"})
            assert saved.status_code == 200 and saved.json()["consent_default"] == "explicit" and saved.json()["tenant_override"] == "explicit"
            assert saved.headers["X-Admin-Operation-Id"]
            async with session_factory() as db:
                settings = (await db.get(Organization, "tenant-one")).settings
                assert settings == {"crew": {"allowed_targets": ["eks-pod"]}, "tools": {"consent_default": "explicit"}}
            cleared = await client.put("/admin/tools/consent-default", json={"tenant_id": "tenant-one", "consent_default": None})
            assert cleared.status_code == 200 and cleared.json()["tenant_override"] is None and cleared.json()["consent_default"] == "granted"
            async with session_factory() as db:
                assert (await db.get(Organization, "tenant-one")).settings == {"crew": {"allowed_targets": ["eks-pod"]}}
                receipts = (await db.scalars(select(AuditLog))).all()
            assert [row.event_type for row in receipts].count("admin_update_tools_consent_default") == 2
    finally:
        await engine.dispose()
