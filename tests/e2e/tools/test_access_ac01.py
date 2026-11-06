"""SCN-8065-01 drives supported gateway routes against isolated local stores."""

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import boto3
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from moto import mock_aws
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

os.environ["TESTING"] = "1"
os.environ["AWS_CONFIG_FILE"] = "/dev/null"
os.environ["AWS_SHARED_CREDENTIALS_FILE"] = "/dev/null"
os.environ["AWS_ACCESS_KEY_ID"] = "testing"
os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
os.environ["AWS_EC2_METADATA_DISABLED"] = "true"
os.environ["BG_TOKEN_SECRET_KEY"] = "test-secret-key-do-not-use-in-production"
os.environ["JWT_SECRET_KEY"] = "test-secret-key-do-not-use-in-production"

# Initialize authentication before the audited routes, as gateway startup does.
# Starting with admin.audit instead recurses through auth and rate-limit routes.
from src.auth.dependencies import get_current_user
from src.auth import vault_routes

from src.admin import tool_routes
from src.agentauth.native_test_enrollment import FixtureDesignation, NativeTestStore
from src.agentauth.native_test_runtime import NativeTestRuntime
from src.agentauth.routes import get_agent_runtime
from src.auth.middleware import get_current_user_context
from src.internal import tools_authorize
from src.internal.tools_policy import ToolsPolicyStore
from src.shared.database import get_db
from src.shared.models.audit import AuditLog
from src.shared.models.base import Base
from src.shared.models.organization import Department, Organization, Team, User
from src.shared.models.tool_entitlement import ToolEntitlement
from src.shared.schemas.auth import TokenContext
from tests.e2e.tools import access_integration, runner
from tools_facade.server import create_app

SHA = "a" * 40
IMAGE = "example.invalid/adp-tools-facade@sha256:" + "b" * 64
GATEWAY_IMAGE = "example.invalid/adp-gateway@sha256:" + "c" * 64


class IdleClient:
    version = "test"

    def __init__(self, command):
        self.command = command

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass


class GatewayAdapter:
    def __init__(self, client):
        self.client = client

    def request(self, body):
        response = self.client.post("/internal/v1/tools/authorize", json={"schema_version": "1.0", **body},
                                    headers={"x-adp-run-credential": "local", "x-adp-workload-token": "local"})
        if response.status_code != 200:
            raise HTTPException(response.status_code, response.json().get("detail", "tool authority refused"))
        return response.json()

    async def authorize(self, tool, arguments, headers):
        return self.request({"tool": tool, "arguments": arguments})

    async def list_tools(self, headers):
        return self.request({"action": "list"})


class SyntheticProvider:
    def __init__(self):
        self.calls = []

    async def invoke(self, tool, arguments, decision):
        self.calls.append((tool, decision.principal.tenant_id, decision.principal.user_id))
        return {"jsonrpc": "2.0", "result": {"content": [{"text": "local fixture"}]}}

    def read_calls(self):
        return list(self.calls)


class GatewayDriver:
    isolated = True
    authorized = True
    fresh_user = True
    ready_targets = True

    def __init__(self, client, facade, provider, sessions, store, database, executions, recorder,
                 runtime, owner, admin, tenant, team):
        self.client, self.sessions, self.store, self.database = client, sessions, store, database
        self.facade, self.provider, self.executions, self.runtime = facade, provider, executions, runtime
        self.catalogue = None
        self.recorder, self.owner, self.admin, self.tenant, self.team = recorder, owner, admin, tenant, team
        self.actions = []

    def read_policy(self):
        policy = self.store.get(tenant_id=self.tenant, owner_id=self.owner)
        return {"allowed_tools": policy["allowed_tools"], "consented_tools": policy["consented_tools"]} if policy else {
            "allowed_tools": [], "consented_tools": [],
        }

    def step(self, action, *, attempt_id):
        self.actions.append(action)
        if action in {"grant_tenant", "grant_team"}:
            receipts = []
            for name in access_integration.CORE_TOOLS:
                body = {"tenant_id": self.tenant, "tool_name": name, "enabled": True,
                        "max_cost_usd": "0.20", "expected_revision": 0}
                if action == "grant_team":
                    body["team_id"] = self.team
                response = self.client.put("/admin/tools/entitlements", json=body)
                if response.status_code != 200:
                    return {"status": response.status_code}
                receipts.append(response.headers["X-Admin-Operation-Id"])
            return {"status": 200, "audit_operation_ids": receipts}
        if action == "allow":
            response = self.client.put(f"/admin/tools/users/{self.owner}/policy", json={
                "tenant_id": self.tenant, "allowed_tools": list(access_integration.CORE_TOOLS),
                "tool_max_cost_usd": {name: "0.20" for name in access_integration.CORE_TOOLS},
                "expected_revision": 0,
            })
            return {"status": response.status_code, "audit_operation_ids": [response.headers.get("X-Admin-Operation-Id")],
                    "consented_tools": response.json().get("consented_tools")}
        if action == "consent":
            before = self.client.get("/auth/tools/consent")
            if before.status_code != 200:
                return {"status": before.status_code}
            response = self.client.put("/auth/tools/consent", json={
                "consented_tools": list(access_integration.CORE_TOOLS), "expected_revision": before.json()["revision"],
            })
            return {"status": response.status_code, "audit_operation_ids": [response.json().get("operation_id")]}
        if action == "discover":
            response = self.facade.get("/agent/tools", headers={"x-adp-run-credential": "local",
                                                               "x-adp-workload-token": "local"})
            return {"status": response.status_code,
                    "tools": [tool["name"].replace("___", ".", 1) for tool in response.json().get("tools", [])]}
        if action == "call":
            statuses = []
            for name in access_integration.CORE_TOOLS:
                response = self.facade.post("/agent/tools/call", json={
                    "tool": name.replace(".", "___", 1), "arguments": {}, "request_id": str(uuid4()),
                }, headers={"x-adp-run-credential": "local", "x-adp-workload-token": "local"})
                statuses.append(response.status_code)
            return {"status": 200 if statuses == [200] * len(statuses) else next(code for code in statuses if code != 200),
                    "provider_call_count": len(self.provider.read_calls())}
        raise ValueError("unknown access scenario action")

    def deny_case(self, case, *, attempt_id):
        self.actions.append("deny:" + case)
        tool = {"not_consented": "websearch.search", "team_disabled": "browser.start",
                "tenant_disabled": "code_interpreter.start", "other_tenant": "websearch.search"}[case]
        receipts = []
        if case in {"team_disabled", "tenant_disabled"}:
            body = {"tenant_id": self.tenant, "tool_name": tool, "enabled": False,
                    "max_cost_usd": "0.20", "expected_revision": 1}
            if case == "team_disabled":
                body["team_id"] = self.team
            changed = self.client.put("/admin/tools/entitlements", json=body)
            if changed.status_code != 200:
                return {"status": changed.status_code}
            receipts.append(changed.headers["X-Admin-Operation-Id"])
        foreign = str(uuid4()) if case == "other_tenant" else None
        original = self.runtime.authenticate.return_value
        if foreign:
            self.runtime.authenticate.return_value = (
                original[0], SimpleNamespace(tenant_id=foreign, invocation_id="foreign-run"), original[2], original[3])
        try:
            report = self.client.get(f"/admin/tools/users/{self.owner}/access",
                                     params={"tenant_id": foreign or self.tenant})
            listing = self.facade.get("/agent/tools", headers={"x-adp-run-credential": "local",
                                                               "x-adp-workload-token": "local"})
            call = self.facade.post("/agent/tools/call", json={
                "tool": tool.replace(".", "___", 1), "arguments": {}, "request_id": str(uuid4()),
            }, headers={"x-adp-run-credential": "local", "x-adp-workload-token": "local"})
        finally:
            self.runtime.authenticate.return_value = original
        return {"status": 200, "audit_operation_ids": receipts,
                "diagnostic_status": report.status_code,
                "reason": ("tenant_boundary" if foreign else next(
                    item["reason"] for item in report.json()["tools"] if item["name"] == tool))
                if report.status_code in {200, 403} else None,
                "listed_tools": sorted(item["name"].replace("___", ".", 1)
                                       for item in listing.json().get("tools", [])),
                "list_status": listing.status_code, "call_status": call.status_code,
                "call_reason": call.json().get("detail"), "foreign_tenant": foreign,
                "provider_call_count": len(self.provider.read_calls())}

    def reconcile_action(self, action, *, attempt_id, execution=None):
        self.actions.append("reconcile:" + action)
        policy_url = f"/admin/tools/users/{self.owner}/policy"
        all_tools = list(access_integration.CORE_TOOLS)
        if action in {"stale_policy", "advance_policy", "reconcile", "stale_reconcile"}:
            allowed = all_tools[1:] if action in {"reconcile", "stale_reconcile"} else all_tools
            revision = {"stale_policy": 0, "advance_policy": 1, "reconcile": 3, "stale_reconcile": 3}[action]
            response = self.client.put(policy_url, json={
                "tenant_id": self.tenant, "allowed_tools": allowed,
                "tool_max_cost_usd": {name: "0.10" for name in allowed}, "expected_revision": revision,
            })
        elif action == "stale_entitlement":
            response = self.client.put("/admin/tools/entitlements", json={
                "tenant_id": self.tenant, "tool_name": all_tools[0], "enabled": False,
                "max_cost_usd": "0.20", "expected_revision": 0,
            })
        elif action == "stale_consent":
            response = self.client.put("/auth/tools/consent", json={
                "consented_tools": all_tools, "expected_revision": 1,
            })
        elif action in {"remove_target", "check_removed"}:
            if action == "remove_target":
                self.catalogue["tools"][:] = [tool for tool in self.catalogue["tools"] if tool["name"] != all_tools[0]]
            response = self.client.get(f"/admin/tools/users/{self.owner}/access", params={"tenant_id": self.tenant})
            if response.status_code != 200:
                return {"status": response.status_code}
            reason = next(tool["reason"] for tool in response.json()["tools"] if tool["name"] == all_tools[0])
            listing = self.step("discover", attempt_id=attempt_id)
            if listing["status"] != 200:
                return listing
            refused = self.facade.post("/agent/tools/call", json={
                "tool": all_tools[0].replace(".", "___", 1), "arguments": {}, **execution,
            }, headers={"x-adp-run-credential": "local", "x-adp-workload-token": "local"})
            return {"status": refused.status_code, "call_reason": refused.json().get("detail"),
                    "diagnostic_reason": reason, "discovered_tools": listing["tools"],
                    "provider_call_count": len(self.provider.read_calls())}
        elif action == "call_before_removal":
            response = self.facade.post("/agent/tools/call", json={
                "tool": all_tools[0].replace(".", "___", 1), "arguments": {}, "request_id": str(uuid4()),
            }, headers={"x-adp-run-credential": "local", "x-adp-workload-token": "local"})
            return {"status": response.status_code, "execution": {
                "request_id": response.json().get("request_id"),
                "execution_id": response.json().get("execution_id")},
                "provider_call_count": len(self.provider.read_calls())}
        else:
            raise ValueError("unknown reconciliation action")
        return {"status": response.status_code,
                "audit_operation_ids": [response.headers["X-Admin-Operation-Id"]] if response.status_code == 200 else [],
                "revision": response.json().get("revision"),
                "consented_tools": response.json().get("consented_tools")}

    def cleanup_owned(self, *, attempt_id):
        if self.catalogue is not None:
            self.catalogue["tools"][:] = [{"name": name, "description": name, "inputSchema": {"type": "object"}}
                                        for name in access_integration.CORE_TOOLS]
        for item in self.executions.scan(ProjectionExpression="execution_id")["Items"]:
            self.executions.delete_item(Key={"execution_id": item["execution_id"]})
        self.store.client.delete_item(TableName=self.store.table_name,
                                      Key={"pk": {"S": f"TENANT#{self.tenant}"}, "sk": {"S": f"TOOLS_POLICY#{self.owner}"}})

        async def clean():
            async with self.sessions() as db:
                await db.execute(delete(AuditLog).where(AuditLog.org_id == self.tenant))
                await db.execute(delete(ToolEntitlement).where(ToolEntitlement.org_id == self.tenant))
                await db.execute(delete(User).where(User.org_id == self.tenant))
                await db.execute(delete(Team).where(Team.org_id == self.tenant))
                await db.execute(delete(Department).where(Department.org_id == self.tenant))
                await db.execute(delete(Organization).where(Organization.id == self.tenant))
                await db.commit()

        asyncio.run(clean())


class IndependentObserver:
    def __init__(self, driver):
        self.driver = driver

    def observe(self, *, attempt_id):
        owner, tenant, team = self.driver.owner, self.driver.tenant, self.driver.team
        policy = self.driver.store.get(tenant_id=tenant, owner_id=owner)

        async def read():
            async with self.driver.sessions() as db:
                grants = (await db.scalars(select(ToolEntitlement).where(ToolEntitlement.org_id == tenant))).all()
                receipts = (await db.scalars(select(AuditLog).where(AuditLog.org_id == tenant))).all()
                verified = [row.details["operation_id"] for row in receipts
                            if row.event_type in {"admin_update_tool_entitlement", "admin_update_user_tool_policy",
                                                  "tools_consent_updated"}
                            and row.actor_id == (owner if row.event_type == "tools_consent_updated" else self.driver.admin)]
                return sorted(row.tool_name for row in grants if row.team_id == "" and row.enabled), (
                    sorted(row.tool_name for row in grants if row.team_id == team and row.enabled)
                ), verified

        tenant_grants, team_grants, audited = asyncio.run(read())
        return {"source": "independent-gateway-readback", "allowed_tools": policy["allowed_tools"],
                "consented_tools": policy["consented_tools"], "tenant_grants": tenant_grants,
                "team_grants": team_grants, "provider_call_count": len(self.driver.provider.read_calls()),
                "audit_operation_ids": audited}

    def observe_denial(self, *, attempt_id, foreign_tenant=None):
        observed = self.observe(attempt_id=attempt_id)
        if foreign_tenant:
            async def read_foreign():
                async with self.driver.sessions() as db:
                    return (await db.scalar(select(User.id).where(User.org_id == foreign_tenant,
                                                                   User.id == self.driver.owner))) is not None

            observed["foreign_owner_exists"] = asyncio.run(read_foreign())
            observed["foreign_policy_exists"] = self.driver.store.get(
                tenant_id=foreign_tenant, owner_id=self.driver.owner) is not None
        observed["activity_call_count"] = self.driver.recorder.call_count
        return observed

    def observe_reconciliation(self, *, attempt_id):
        observed = self.observe(attempt_id=attempt_id)
        policy = self.driver.store.get(tenant_id=self.driver.tenant, owner_id=self.driver.owner)
        observed["policy_revision"] = int(policy["revision"])
        observed["catalog_tools"] = sorted(tool["name"].replace("___", ".", 1) for tool in policy["catalog"])
        observed["deployed_tools"] = sorted(tool["name"] for tool in self.driver.catalogue["tools"])
        observed["activity_call_count"] = self.driver.recorder.call_count
        return observed

    def verify_cleanup(self, *, attempt_id):
        if [tool["name"] for tool in self.driver.catalogue["tools"]] != list(access_integration.CORE_TOOLS):
            return False
        if self.driver.executions.scan(Select="COUNT")["Count"]:
            return False
        if self.driver.store.get(tenant_id=self.driver.tenant, owner_id=self.driver.owner) is not None:
            return False

        async def read():
            async with self.driver.sessions() as db:
                user = await db.scalar(select(User.id).where(User.id == self.driver.owner))
                grant = await db.scalar(select(ToolEntitlement).where(ToolEntitlement.org_id == self.driver.tenant))
                return user is None and grant is None

        return asyncio.run(read())


@pytest.fixture
def gateway_fixture(monkeypatch):
    tenant, team, owner, admin, subject = (str(uuid4()) for _ in range(5))
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def setup():
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions() as db:
            db.add(Organization(id=tenant, name="Isolated scenario", aws_accounts=[], role_mappings={},
                                settings={"tools": {"consent_default": "explicit"}},
                                github_installation_ids=[], cognito_client_ids=[]))
            db.add(Department(id="dept-one", org_id=tenant, name="Local"))
            db.add(Team(id=team, org_id=tenant, department_id="dept-one", name="Local"))
            db.add_all([User(id=owner, org_id=tenant, team_id=team, cognito_sub=subject, email="fresh@example.invalid"),
                        User(id=admin, org_id=tenant, team_id=team, email="admin@example.invalid")])
            await db.commit()

    asyncio.run(setup())
    with mock_aws():
        database = boto3.client("dynamodb", region_name="us-east-1")
        database.create_table(TableName="authority", BillingMode="PAY_PER_REQUEST",
                              KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
                              AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"},
                                                    {"AttributeName": "sk", "AttributeType": "S"}])
        executions = boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName="local-executions-" + tenant, BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "execution_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "execution_id", "AttributeType": "S"}])
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        monkeypatch.setenv("ADP_TOOLS_EXECUTION_TABLE", executions.name)
        store = ToolsPolicyStore(table_name="authority", client=database)
        runtime_context = (
            SimpleNamespace(uid="local"), SimpleNamespace(tenant_id=tenant, invocation_id="run-one"), {},
            SimpleNamespace(authority=SimpleNamespace(org_id=tenant, human_id=owner),
                            expires_at=datetime.now(UTC) + timedelta(minutes=5)),
        )
        runtime = SimpleNamespace(authenticate=Mock(return_value=runtime_context), validate_flow=AsyncMock(),
                                  store=SimpleNamespace(table="authority", client=database))
        admin_context = TokenContext(user_id=admin, org_id=tenant, team_id=team, department_id="dept-one",
                                     account_type="human", auth_source="jwt", expires_at=datetime.now(UTC) + timedelta(hours=1))
        owner_context = TokenContext(user_id=owner, org_id=tenant, team_id=team, department_id="dept-one",
                                     account_type="human", auth_source="jwt", expires_at=datetime.now(UTC) + timedelta(hours=1))
        catalogue = {"tools": [{"name": name, "description": name, "inputSchema": {"type": "object"}}
                               for name in access_integration.CORE_TOOLS], "connectors": []}
        monkeypatch.setattr(tool_routes, "tool_catalogue", lambda: catalogue)
        monkeypatch.setattr(tools_authorize, "gateway_catalogue", lambda: catalogue)

        async def permission(self, caller, permission, *, target_org_id):
            if caller.user_id != admin or target_org_id != tenant:
                raise HTTPException(403, "not a scoped administrator")

        monkeypatch.setattr(tool_routes.AccessControl, "check_permission", permission)
        monkeypatch.setenv("ADP_TOOLS_FACADE_ROLE_ARN", "arn:aws:iam::123456789012:role/local-facade")
        monkeypatch.setenv("WEBHOOK_EVENTS_TABLE", "local-events")
        monkeypatch.setattr(tools_authorize, "get_settings", lambda: object())
        monkeypatch.setattr(tools_authorize, "verified_caller_identity", lambda request, settings:
                            "arn:aws:sts::123456789012:assumed-role/local-facade/local")
        monkeypatch.setattr(tools_authorize, "BudgetEnforcementService", lambda: SimpleNamespace(
            check_budget_hierarchy=AsyncMock(return_value=SimpleNamespace(allowed=True))))
        monkeypatch.setattr(tools_authorize, "get_rate_limit_service", lambda request: SimpleNamespace(
            consume_tool_rate_limit=AsyncMock(return_value=SimpleNamespace(allowed=True))))
        recorder = Mock()
        monkeypatch.setattr(tools_authorize, "ActivityService", lambda **kwargs: SimpleNamespace(record_tool_call=recorder))
        app = FastAPI()
        # Ordinary callers need a real negative designation read. The isolated
        # registry shares this Moto table; no native capability is enrolled.
        app.state.native_test_runtime = NativeTestRuntime(
            app, NativeTestStore(table_name="authority", client=database))
        app.include_router(tool_routes.router)
        app.include_router(vault_routes.router)
        app.include_router(tools_authorize.router, prefix="/internal/v1")

        async def db_override():
            async with sessions() as db:
                yield db

        app.dependency_overrides[get_db] = db_override
        app.dependency_overrides[get_current_user] = lambda: admin_context
        app.dependency_overrides[get_current_user_context] = lambda: owner_context
        app.dependency_overrides[get_agent_runtime] = lambda: runtime
        app.dependency_overrides[tools_authorize.require_agent_transport] = lambda: None
        with TestClient(app, raise_server_exceptions=False) as client:
            provider = SyntheticProvider()
            with TestClient(create_app(gateway=GatewayAdapter(client), agentcore=provider),
                            raise_server_exceptions=False) as facade:
                driver = GatewayDriver(client, facade, provider, sessions, store, database, executions, recorder,
                                       runtime, owner, admin, tenant, team)
                driver.catalogue = catalogue
                yield driver, IndependentObserver(driver)
    asyncio.run(engine.dispose())


@pytest.mark.parametrize("fault,reason", [
    ("missing_registry", "native_test_registry_unavailable"),
    ("unreadable_registry", "test_authority_unavailable"),
    ("missing_subject", "native_test_subject_unavailable"),
    ("designated_subject", "native_test_all_in_tool_dispatch_unavailable"),
    ("revoked_subject", "native_test_all_in_tool_dispatch_unavailable"),
    ("designated_human_alias", "native_test_all_in_tool_dispatch_unavailable"),
])
def test_access_fixture_preserves_native_designation_boundary(gateway_fixture, fault, reason):
    driver, observer = gateway_fixture
    attempt_id = str(uuid4())
    for action in ("grant_tenant", "grant_team", "allow", "consent"):
        assert driver.step(action, attempt_id=attempt_id)["status"] == 200
    assert driver.step("discover", attempt_id=attempt_id) == {
        "status": 200, "tools": list(access_integration.CORE_TOOLS)}
    registry = driver.client.app.state.native_test_runtime

    async def read_subject():
        async with driver.sessions() as db:
            user = await db.get(User, driver.owner)
            subject = user.cognito_sub
            if fault == "missing_subject":
                user.cognito_sub = None
                await db.commit()
            return subject

    subject = asyncio.run(read_subject())
    assert subject and subject != driver.owner
    if fault == "missing_registry":
        driver.client.app.state.native_test_runtime = None
    elif fault == "unreadable_registry":
        registry.store.table = "missing-native-registry"
    elif fault in {"designated_subject", "revoked_subject", "designated_human_alias"}:
        designation = FixtureDesignation(
            subject_id=driver.owner if fault == "designated_human_alias" else subject,
            tenant_id=driver.tenant, user_id=driver.owner, team_id=driver.team,
            department_id="dept-one", owner_id=driver.admin, deployment_id="offline-access",
            revision=str(uuid4()), revocation_epoch=0,
            state="revoked" if fault == "revoked_subject" else "active",
        )
        driver.database.put_item(TableName=registry.store.table, Item=registry.store.item(
            registry.store.fixture_key(designation.subject_id), "native_test_fixture_v1", designation.model_dump()))
    headers = {"x-adp-run-credential": "local", "x-adp-workload-token": "local"}
    listing = driver.facade.get("/agent/tools", headers=headers)
    call = driver.facade.post("/agent/tools/call", headers=headers, json={
        "tool": "websearch___search", "arguments": {}, "request_id": str(uuid4()),
    })
    for response in (listing, call):
        assert response.status_code == 403
        assert response.json()["detail"] == reason
    assert driver.provider.read_calls() == []
    assert driver.recorder.call_count == 0
    driver.cleanup_owned(attempt_id=attempt_id)
    assert observer.verify_cleanup(attempt_id=attempt_id)


def test_fresh_user_grants_consent_discovery_and_independent_cleanup(gateway_fixture):
    driver, observer = gateway_fixture
    evidence = access_integration.access_ac01(driver, observer)
    assert evidence["status"] == "pass", evidence
    assert evidence["discovered_tools"] == list(access_integration.CORE_TOOLS)
    assert evidence["provider_call_count"] == 3
    assert [call[0] for call in driver.provider.read_calls()] == [
        tool.replace(".", "___", 1) for tool in access_integration.CORE_TOOLS]
    assert all(call[1:] == (driver.tenant, driver.owner) for call in driver.provider.read_calls())
    assert driver.recorder.call_count == 3
    assert len(evidence["audit_operation_ids"]) == 8
    assert evidence["cleanup"]["status"] == "confirmed"


def test_administrator_grant_does_not_invoke_provider_before_owner_consent(gateway_fixture):
    driver, observer = gateway_fixture
    attempt_id = str(uuid4())
    for action in ("grant_tenant", "grant_team", "allow"):
        assert driver.step(action, attempt_id=attempt_id)["status"] == 200
    assert driver.read_policy()["consented_tools"] == []
    assert driver.step("discover", attempt_id=attempt_id)["tools"] == []
    assert driver.step("call", attempt_id=attempt_id)["status"] == 403
    assert driver.provider.read_calls() == []
    assert driver.recorder.call_count == 0
    driver.cleanup_owned(attempt_id=attempt_id)
    assert observer.verify_cleanup(attempt_id=attempt_id)


def test_selector_refuses_nonisolated_or_missing_oracle_before_any_action(gateway_fixture):
    driver, observer = gateway_fixture
    driver.isolated = False
    with pytest.raises(access_integration.FixtureUnavailable):
        access_integration.access_ac01(driver, observer)
    assert driver.actions == []
    driver.isolated = True
    with pytest.raises(access_integration.FixtureUnavailable):
        access_integration.access_ac01(driver, None)
    assert driver.actions == []


def test_runner_selects_scn01_without_claiming_missing_fixture(tmp_path, monkeypatch, gateway_fixture):
    driver, observer = gateway_fixture
    monkeypatch.setattr(runner, "verify_gateway", lambda url: None)
    arguments = ("SCN-8065-01", tmp_path, "https://example.invalid", SHA, GATEWAY_IMAGE, IMAGE)
    assert runner.evaluate(*arguments, command=["adp", "mcp"], client_factory=IdleClient) is False
    monkeypatch.setenv("GITHUB_RUN_ID", "local-scn01")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    assert runner.evaluate(*arguments, command=["adp", "mcp"], client_factory=IdleClient,
                           integration_driver_factory=lambda *_: driver, integration_observer=observer) is True
    report = json.loads((tmp_path / "evidence.json").read_text())
    assert (report["github_run_id"], report["github_run_attempt"]) == ("local-scn01", "1")
    row, = report["scenarios"]
    assert row["scenario_id"] == "SCN-8065-01" and row["status"] == "pass"
    assert row["attempt_id"] and row["cleanup"] == {"status": "confirmed"}
    assert observer.verify_cleanup(attempt_id=row["attempt_id"]) is True
