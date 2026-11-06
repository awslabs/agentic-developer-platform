"""A worker identity comes from a live run, not façade-supplied JSON."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import boto3
import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from moto import mock_aws
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.activity.service import ActivityService
from src.agentauth.bootstrap import BootstrapRefusedError
from src.agentauth.execution import ExecutionStateError
from src.agentauth.native_test_enrollment import NativeTestStore
from src.agentauth.native_test_runtime import NativeTestRuntime
from src.agentauth.run_credential import CredentialError
from src.budget import enforcement_service
from src.budget.config import BudgetConfig
from src.budget.reservations import ReservationOutcome
from src.internal import tools_authorize as routes
from src.internal import tools_catalogue
from src.internal.tool_entitlements import effective_entitlement as real_effective_entitlement
from src.internal.tools_admissions import ToolCallAdmission
from src.internal.tools_policy import ToolsPolicyStore
from src.shared.database import get_db
from src.shared.models.budget import BudgetUsage
from src.shared.models.organization import User
from src.shared.models.tool_entitlement import ToolEntitlement
from src.shared.schemas.auth import TokenContext
from src.shared.schemas.budget import DenyReason
from tests.internal.test_tools_dynamic_catalogue import deployed
from tests.internal.test_tools_jira import dispatch

HUMAN = "00000000-0000-4000-8000-000000000001"
BODY = {"schema_version": "1.0", "tool": "websearch___search", "arguments": {"query": "example"}}
HEADERS = {"x-adp-run-credential": "credential-one", "x-adp-workload-token": "pod-one"}


@pytest.fixture
def decision_api(monkeypatch):
    monkeypatch.setenv("ADP_TOOLS_FACADE_ROLE_ARN", "arn:aws:iam::123456789012:role/tools-facade")
    monkeypatch.setattr(routes, "get_settings", lambda: object())
    monkeypatch.setattr(
        routes,
        "verified_caller_identity",
        lambda request, settings: "arn:aws:sts::123456789012:assumed-role/tools-facade/pod",
    )
    caller = SimpleNamespace(tenant_id="tenant-one", invocation_id="run-one")
    grant = SimpleNamespace(authority=SimpleNamespace(org_id="tenant-one", human_id=HUMAN), expires_at=datetime.now(UTC) + timedelta(minutes=5))
    runtime = SimpleNamespace(
        authenticate=Mock(return_value=(SimpleNamespace(uid="pod-one"), caller, {}, grant)),
        validate_flow=AsyncMock(),
        store=SimpleNamespace(table="authority", client=object()),
    )
    db = SimpleNamespace(
        scalar=AsyncMock(
            return_value=SimpleNamespace(
                user_kind="human",
                is_shadow=False,
                team_id="team-one",
                cognito_sub="login-subject",
            )
        )
    )
    policy = {
        "status": "active",
        "allowed_tools": ["websearch.search"],
        "consented_tools": ["websearch.search"],
        "tool_max_cost_usd": {"websearch.search": "0.05"},
    }
    read = Mock(return_value=policy)
    monkeypatch.setattr(routes, "ToolsPolicyStore", lambda **kwargs: SimpleNamespace(get=read))
    deployed = [
        "websearch.search",
        "browser.start",
        "code_interpreter.start",
        "github.get_user",
        "jira.get_issue",
        "slack.get_user",
        "slack.post_message",
    ]
    monkeypatch.setattr(routes, "gateway_catalogue", lambda: {"tools": [{"name": name} for name in deployed]})
    monkeypatch.setattr(routes, "effective_entitlement", AsyncMock(return_value=(True, None)))
    # #8387: the tenant posture and the entitled set are read only for a default-mode owner; the
    # fixture tenant grants by default and entitles nothing until a test says otherwise.
    monkeypatch.setattr(routes, "resolve_tenant_consent_default", AsyncMock(return_value="granted"))
    monkeypatch.setattr(routes, "entitled_tool_names", AsyncMock(return_value={}))
    budget = AsyncMock(return_value=SimpleNamespace(allowed=True))
    monkeypatch.setattr(routes, "BudgetEnforcementService", lambda: SimpleNamespace(check_budget_hierarchy=budget))
    rate = AsyncMock(return_value=SimpleNamespace(allowed=True))
    monkeypatch.setattr(routes, "get_rate_limit_service", lambda request: SimpleNamespace(consume_tool_rate_limit=rate))
    monkeypatch.setenv("WEBHOOK_EVENTS_TABLE", "test-webhook-events")
    recorder = Mock()
    monkeypatch.setattr(routes, "ActivityService", lambda **kwargs: SimpleNamespace(record_tool_call=recorder))
    app = FastAPI()
    native_store = NativeTestStore(table_name="native-test-authority", client=object())
    native_store.designation = Mock(return_value=None)
    app.state.native_test_runtime = NativeTestRuntime(app, native_store)
    app.state.tool_activity_recorder = recorder
    app.include_router(routes.router, prefix="/internal/v1")

    def verified_transport(request: Request):
        request.state.token_context = SimpleNamespace(scope="internal")

    app.dependency_overrides[routes.require_agent_transport] = verified_transport
    app.dependency_overrides[routes.get_agent_runtime] = lambda: runtime
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app), runtime, read, policy, budget, db


def owned_connector(provider="slack", scopes=None, **changes):
    return SimpleNamespace(
        **{
            "id": "owned-connection",
            "org_id": "tenant-one",
            "user_id": HUMAN,
            "team_id": None,
            "domain_app_id": None,
            "service": "jira" if provider == "atlassian" else provider,
            "provider": "agentcore-identity",
            "credential_type": "oauth_token",
            "secret_arn": f"agentcore-identity:adp-tools-dev-oauth-user:{provider}",
            "scopes": {"oauth_scopes": scopes if scopes is not None else ["users:read"]},
            "agent_consent": True,
            "expires_at": None,
            **changes,
        }
    )


def slack_policy(policy):
    permissions = ["slack.get_user", "slack.post_message"]
    policy.update(
        allowed_tools=permissions,
        consented_tools=permissions,
        tool_max_cost_usd={permission: "0.05" for permission in permissions},
        catalog=[
            {"name": permission.replace(".", "___"), "description": "Offline connector fixture", "inputSchema": {"type": "object"}}
            for permission in permissions
        ],
    )


def test_slack_post_requires_approved_owner_fixture_and_current_write_grant(decision_api, monkeypatch):
    from src.internal import slack_oauth

    client, _, _, policy, budget, db = decision_api
    slack_policy(policy)
    catalogue = routes.gateway_catalogue()
    for entry in catalogue["tools"]:
        if entry["name"].startswith("slack."):
            entry.update(connector="slack", description="Private Slack fixture", inputSchema={"type": "object"})
    monkeypatch.setattr(routes, "gateway_catalogue", lambda: catalogue)
    monkeypatch.setattr(routes, "get_settings", lambda: SimpleNamespace(oauth_workload_name="adp-tools-dev-oauth-user"))
    monkeypatch.setenv("ADP_TOOLS_SLACK_MEDIATOR_ENABLED", "true")
    monkeypatch.setenv("ADP_TOOLS_SLACK_POST_ENABLED", "true")
    fixture = {
        "owner_id": HUMAN,
        "team_id": "T00000001",
        "channel_id": "C00000001",
        "message": "ADP isolated fixture message.",
        "run_marker": "7d787d28-5c0e-4c86-b56f-fc13bb332b00",
        "approved": True,
        "isolated": True,
        "cleanup_method": "chat.delete",
    }
    monkeypatch.setenv("ADP_TOOLS_SLACK_POST_FIXTURE", json.dumps(fixture))
    context = SimpleNamespace(account_type="human", auth_source="jwt", org_id="tenant-one", user_id=HUMAN)
    owner = SimpleNamespace(id=HUMAN, team_id="team-one", user_kind="human", is_shadow=False)
    monkeypatch.setattr(routes, "authenticate_human", AsyncMock(return_value=(context, owner, "team-one")))
    binding = SimpleNamespace(observed_scopes=["chat:write", "users:read"], provider_account_id="T00000001:U00000001")
    credential = owned_connector(scopes=["users:read", "chat:write"])
    connection = {"connection_id": credential.id, "revision": "binding-revision"}
    selected = AsyncMock(return_value=(credential, binding, connection))
    monkeypatch.setattr(slack_oauth, "selected_slack_connection", selected)
    user = db.scalar.return_value
    headers = {"x-adp-user-authorization": "Bearer synthetic-owner-jwt"}

    def listing():
        db.scalar.side_effect = [user, credential, credential]
        return client.post("/internal/v1/tools/authorize", json={"schema_version": "1.0", "action": "list"}, headers=headers)

    def call():
        db.scalar.side_effect = [user, credential]
        return client.post("/internal/v1/tools/authorize", json={"schema_version": "1.0", "tool": "slack___post_message"}, headers=headers)

    assert {entry["name"] for entry in listing().json()["tools"]} == {"slack___get_user", "slack___post_message"}
    allowed = call()
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["connection_id"] == credential.id
    assert allowed.json()["slack_connection"] == connection
    assert allowed.json()["connection_scopes"] == ["users:read", "chat:write"]
    assert budget.call_args.kwargs["reserve"] is True
    for change in ({"approved": False}, {"channel_id": "D00000001"}, {"owner_id": "other-owner"}):
        monkeypatch.setenv("ADP_TOOLS_SLACK_POST_FIXTURE", json.dumps({**fixture, **change}))
        assert [entry["name"] for entry in listing().json()["tools"]] == ["slack___get_user"]
        budget.reset_mock()
        refused = call()
        assert refused.status_code == 403 and budget.await_count == 0
    monkeypatch.setenv("ADP_TOOLS_SLACK_POST_FIXTURE", json.dumps(fixture))
    credential.scopes = {"oauth_scopes": ["users:read"]}
    assert [entry["name"] for entry in listing().json()["tools"]] == ["slack___get_user"]
    budget.reset_mock()
    assert call().status_code == 403 and budget.await_count == 0


@pytest.mark.parametrize(
    "scopes,visible",
    [
        (None, []),
        (["users:read"], ["slack___get_user"]),
        (["users:read", "chat:write"], ["slack___get_user"]),
        (["chat:write"], []),
        (["users:read", "admin"], []),
        (["read:user"], []),
        (["users:read", "users:read"], []),
    ],
)
def test_slack_discovery_and_call_use_actual_owned_grants(decision_api, monkeypatch, scopes, visible):
    from src.internal import slack_oauth

    client, _, _, policy, budget, db = decision_api
    slack_policy(policy)
    catalogue = routes.gateway_catalogue()
    for entry in catalogue["tools"]:
        if entry["name"].startswith("slack."):
            entry.update(connector="slack", description="Private Slack fixture", inputSchema={"type": "object"})
    monkeypatch.setattr(routes, "gateway_catalogue", lambda: catalogue)
    monkeypatch.setenv("ADP_TOOLS_SLACK_MEDIATOR_ENABLED", "true")
    monkeypatch.setattr(routes, "get_settings", lambda: SimpleNamespace(oauth_workload_name="adp-tools-dev-oauth-user"))
    context = SimpleNamespace(account_type="human", auth_source="jwt", org_id="tenant-one", user_id=HUMAN)
    owner = SimpleNamespace(id=HUMAN, team_id="team-one", user_kind="human", is_shadow=False)
    monkeypatch.setattr(routes, "authenticate_human", AsyncMock(return_value=(context, owner, "team-one")))
    headers = {"x-adp-user-authorization": "Bearer synthetic-owner-jwt"}
    user = db.scalar.return_value
    credential = owned_connector(scopes=scopes) if scopes is not None else None
    selected = AsyncMock()
    if visible:
        selected.return_value = (
            credential,
            SimpleNamespace(observed_scopes=scopes, provider_account_id="T00000001:U00000001"),
            {"connection_id": credential.id, "revision": "bound"},
        )
    else:
        selected.side_effect = HTTPException(403, "Slack connection unqualified")
    monkeypatch.setattr(slack_oauth, "selected_slack_connection", selected)
    db.scalar.side_effect = [user, credential, credential]
    listed = client.post("/internal/v1/tools/authorize", json={"schema_version": "1.0", "action": "list"}, headers=headers)
    assert listed.status_code == 200
    assert [tool["name"] for tool in listed.json()["tools"]] == visible
    assert budget.await_count == len(visible)
    assert all(call.kwargs["reserve"] is False for call in budget.call_args_list)
    client.app.state.tool_activity_recorder.assert_not_called()
    for tool in ("slack___get_user", "slack___post_message"):
        budget.reset_mock()
        db.scalar.side_effect = [user, credential]
        response = client.post("/internal/v1/tools/authorize", json={"schema_version": "1.0", "tool": tool}, headers=headers)
        assert response.status_code == (200 if tool in visible else 403)
        if tool not in visible:
            assert response.json()["detail"] == "Slack connection unqualified"
            budget.assert_not_awaited()


@pytest.mark.parametrize(
    "change",
    [
        {"user_id": "another-owner"},
        {"org_id": "another-tenant"},
        {"team_id": "shared-team"},
        {"domain_app_id": "shared-domain"},
        {"agent_consent": False},
        {"secret_arn": "agentcore-identity:another-oauth-user:slack"},
        {"expires_at": datetime.now(UTC) - timedelta(seconds=1)},
    ],
)
def test_discovery_never_advertises_unowned_disconnected_or_expired_write_grant(decision_api, monkeypatch, change):
    client, _, _, policy, budget, db = decision_api
    slack_policy(policy)
    monkeypatch.setattr(routes, "get_settings", lambda: SimpleNamespace(oauth_workload_name="adp-tools-dev-oauth-user"))
    user = db.scalar.return_value
    credential = owned_connector(scopes=["users:read", "chat:write"], **change)
    db.scalar.side_effect = [user, credential, credential]
    response = client.post("/internal/v1/tools/authorize", json={"schema_version": "1.0", "action": "list"}, headers=HEADERS)
    assert response.status_code == 200 and response.json()["tools"] == []
    budget.assert_not_awaited()
    client.app.state.tool_activity_recorder.assert_not_called()


def test_discovery_reloads_connection_after_disconnect_and_keeps_tool_consent_required(decision_api, monkeypatch):
    from src.internal import slack_oauth

    client, _, _, policy, budget, _ = decision_api
    slack_policy(policy)
    catalogue = routes.gateway_catalogue()
    for entry in catalogue["tools"]:
        if entry["name"].startswith("slack."):
            entry.update(connector="slack", description="Private Slack fixture", inputSchema={"type": "object"})
    monkeypatch.setattr(routes, "gateway_catalogue", lambda: catalogue)
    monkeypatch.setenv("ADP_TOOLS_SLACK_MEDIATOR_ENABLED", "true")
    context = SimpleNamespace(account_type="human", auth_source="jwt", org_id="tenant-one", user_id=HUMAN)
    owner = SimpleNamespace(id=HUMAN, team_id="team-one", user_kind="human", is_shadow=False)
    monkeypatch.setattr(routes, "authenticate_human", AsyncMock(return_value=(context, owner, "team-one")))
    credential = owned_connector(scopes=["users:read", "chat:write"])
    selected = AsyncMock(
        return_value=(
            credential,
            SimpleNamespace(observed_scopes=["users:read", "chat:write"], provider_account_id="T00000001:U00000001"),
            {"connection_id": credential.id},
        )
    )
    monkeypatch.setattr(slack_oauth, "selected_slack_connection", selected)
    headers = {"x-adp-user-authorization": "Bearer synthetic-owner-jwt"}
    listing = {"schema_version": "1.0", "action": "list"}

    def listed():
        return [row["name"] for row in client.post("/internal/v1/tools/authorize", json=listing, headers=headers).json()["tools"]]

    assert listed() == ["slack___get_user"]
    policy["consented_tools"] = ["slack.post_message"]
    assert listed() == []
    policy["consented_tools"] = ["slack.get_user"]
    selected.side_effect = HTTPException(403, "Slack connection unqualified")
    budget.reset_mock()
    assert listed() == []
    budget.assert_not_awaited()


@pytest.mark.parametrize("action", ["list", "call"])
def test_connection_read_failure_refuses_before_budget_or_activity(decision_api, monkeypatch, action):
    from src.internal import slack_oauth

    client, _, _, policy, budget, db = decision_api
    slack_policy(policy)
    monkeypatch.setenv("ADP_TOOLS_SLACK_MEDIATOR_ENABLED", "true")
    context = SimpleNamespace(account_type="human", auth_source="jwt", org_id="tenant-one", user_id=HUMAN)
    owner = SimpleNamespace(id=HUMAN, team_id="team-one", user_kind="human", is_shadow=False)
    monkeypatch.setattr(routes, "authenticate_human", AsyncMock(return_value=(context, owner, "team-one")))
    selected = AsyncMock(side_effect=HTTPException(503, "tool connection unavailable"))
    monkeypatch.setattr(slack_oauth, "selected_slack_connection", selected)
    body = {"schema_version": "1.0", "action": action}
    if action == "call":
        body["tool"] = "slack___get_user"
    response = client.post("/internal/v1/tools/authorize", json=body, headers={"x-adp-user-authorization": "Bearer synthetic-owner-jwt"})
    assert response.status_code == 503 and response.json()["detail"] == "tool connection unavailable"
    assert "private" not in response.text
    budget.assert_not_awaited()
    client.app.state.tool_activity_recorder.assert_not_called()


@mock_aws
def test_same_id_gateway_retry_reuses_first_budget_and_receipt(decision_api, monkeypatch):
    client, runtime, _, _, budget, _ = decision_api
    store = boto3.client("dynamodb", region_name="us-east-1")
    store.create_table(
        TableName="authority",
        BillingMode="PAY_PER_REQUEST",
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}, {"AttributeName": "sk", "AttributeType": "S"}],
    )
    runtime.store.client = store
    client.app.state.native_test_runtime.store.document = Mock(return_value=None)
    rate = AsyncMock(return_value=SimpleNamespace(allowed=True))
    monkeypatch.setattr(routes, "get_rate_limit_service", lambda request: SimpleNamespace(consume_tool_rate_limit=rate))
    payload = {**BODY, "request_id": str(uuid4())}
    first = client.post("/internal/v1/tools/authorize", json=payload, headers=HEADERS)
    rate.return_value = SimpleNamespace(allowed=False, retry_after_seconds=12)
    budget.return_value = SimpleNamespace(allowed=False, deny_reason=DenyReason.BUDGET_EXCEEDED)
    repeated = client.post("/internal/v1/tools/authorize", json=payload, headers=HEADERS)
    assert first.status_code == 200 and repeated.status_code == 202
    assert repeated.json() == {
        "schema_version": "1.0",
        "tool_status": {
            "status": "pending",
            "replay": True,
            "receipt_id": first.json()["receipt_id"],
            "reservation_status": "estimated_hold",
            "recovery_path": "/agent/tools/operations/recover",
        },
    }
    assert budget.await_count == rate.await_count == 1
    assert budget.await_args.kwargs["request_id"] == first.json()["receipt_id"]
    client.app.state.tool_activity_recorder.assert_called_once()
    assert client.app.state.tool_activity_recorder.call_args.kwargs["request_id"] == payload["request_id"]
    assert client.app.state.tool_activity_recorder.call_args.kwargs["execution_id"] is None
    changed = client.post("/internal/v1/tools/authorize", json={**payload, "arguments": {"query": "other"}}, headers=HEADERS)
    assert changed.status_code == 409
    assert budget.await_count == rate.await_count == 1


@mock_aws
def test_lost_admission_reply_retains_original_hold_and_rejects_foreign_run(decision_api, monkeypatch):
    client, runtime, _, _, budget, _ = decision_api
    store = boto3.client("dynamodb", region_name="us-east-1")
    store.create_table(
        TableName="authority",
        BillingMode="PAY_PER_REQUEST",
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}, {"AttributeName": "sk", "AttributeType": "S"}],
    )
    runtime.store.client = store
    client.app.state.native_test_runtime.store.document = Mock(return_value=None)
    rate = AsyncMock(return_value=SimpleNamespace(allowed=True))
    monkeypatch.setattr(routes, "get_rate_limit_service", lambda request: SimpleNamespace(consume_tool_rate_limit=rate))

    def lost_reply(self):
        raise HTTPException(503, "tool admission unavailable")

    monkeypatch.setattr(ToolCallAdmission, "finish", lost_reply)
    payload = {**BODY, "request_id": str(uuid4())}
    first = client.post("/internal/v1/tools/authorize", json=payload, headers=HEADERS)
    assert first.status_code == 503
    retained = store.get_item(
        TableName="authority",
        Key={
            "pk": {"S": "TENANT#tenant-one"},
            "sk": {"S": f"TOOLS_CALL#{HUMAN}#{payload['request_id']}"},
        },
        ConsistentRead=True,
    )["Item"]
    receipt_id = retained["receipt_id"]["S"]
    assert retained["status"]["S"] == "claimed"
    assert budget.await_args.kwargs["request_id"] == receipt_id
    repeated = client.post("/internal/v1/tools/authorize", json=payload, headers=HEADERS)
    assert repeated.status_code == 202
    assert repeated.json()["tool_status"] == {
        "status": "pending",
        "replay": True,
        "receipt_id": receipt_id,
        "reservation_status": "unknown",
        "recovery_path": "/agent/tools/operations/recover",
    }
    assert budget.await_count == rate.await_count == 1
    runtime.authenticate.return_value[1].invocation_id = "foreign-run"
    foreign = client.post("/internal/v1/tools/authorize", json=payload, headers=HEADERS)
    assert foreign.status_code in (403, 404, 409) and receipt_id not in foreign.text
    assert budget.await_count == rate.await_count == 1


@mock_aws
def test_signed_tool_outcome_requires_original_owner_and_cannot_claim_settlement(decision_api, monkeypatch):
    client = decision_api[0]
    resource = boto3.resource("dynamodb", region_name="us-east-1")
    resource.create_table(
        TableName="tool-outcome-test",
        BillingMode="PAY_PER_REQUEST",
        KeySchema=[{"AttributeName": "event_id", "KeyType": "HASH"}, {"AttributeName": "arrived_at", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": "event_id", "AttributeType": "S"}, {"AttributeName": "arrived_at", "AttributeType": "S"}],
    )
    service = ActivityService(table_name="tool-outcome-test", dynamodb_resource=resource)
    receipt_id, request_id, execution_id = str(uuid4()), str(uuid4()), str(uuid4())
    service.record_tool_call(
        receipt_id=receipt_id,
        request_id=request_id,
        user_id=HUMAN,
        tenant_id="tenant-one",
        team_id="team-one",
        tool="websearch___search",
        estimated_cost_usd=Decimal("0.05"),
        run_id="run-one",
    )
    monkeypatch.setenv("WEBHOOK_EVENTS_TABLE", "tool-outcome-test")
    monkeypatch.setattr(routes, "ActivityService", lambda **kwargs: service)
    payload = {
        "schema_version": "1.0",
        "receipt_id": receipt_id,
        "tenant_id": "tenant-one",
        "user_id": HUMAN,
        "team_id": "team-one",
        "run_id": "run-one",
        "tool": "websearch___search",
        "request_id": request_id,
        "execution_id": execution_id,
        "result_status": "unknown",
    }
    path = "/internal/v1/tools/outcome"
    for changed in ({"tenant_id": "foreign"}, {"request_id": str(uuid4())}, {"run_id": "foreign"}):
        refused = client.post(path, json={**payload, **changed})
        assert refused.status_code == 404 and receipt_id not in refused.text
    for invalid in ({"settled_cost_usd": "0"}, {"result": {"private": "secret"}}):
        assert client.post(path, json={**payload, **invalid}).status_code == 422
    assert client.post(path, json=payload).json() == {"status": "recorded"}
    confirmed = client.post(path, json={**payload, "result_status": "confirmed", "cleanup_status": "confirmed"})
    assert confirmed.status_code == 200
    item = service._table.scan()["Items"][0]
    assert item["result_status"] == item["cleanup_status"] == "confirmed"
    assert item["settled_cost_usd"] is None and item["execution_id"] == execution_id
    assert not {"result", "arguments", "credential"} & item.keys()
    monkeypatch.setattr(routes, "verified_caller_identity", lambda request, settings: None)
    assert client.post(path, json=payload).status_code == 403
    assert service._table.scan()["Items"][0]["result_status"] == "confirmed"


@pytest.mark.parametrize("agent_consent", [True, False, None])
@pytest.mark.parametrize(
    "tool,provider,scopes,expected",
    [
        ("github___get_user", "github", ["read:user"], None),
        ("jira___get_issue", "atlassian", ["read:jira-work"], None),
    ],
)
def test_decision_uses_only_owned_connection_scopes(decision_api, monkeypatch, tool, provider, scopes, expected, agent_consent):
    client, _, _, policy, budget, db = decision_api
    permission = tool.replace("___", ".", 1)
    policy["allowed_tools"].append(permission)
    policy["consented_tools"].append(permission)
    policy["tool_max_cost_usd"][permission] = "0.05"
    monkeypatch.setattr(routes, "get_settings", lambda: SimpleNamespace(oauth_workload_name="adp-tools-dev-oauth-user"))
    user = db.scalar.return_value
    credential = SimpleNamespace(
        id="connection-alice",
        org_id="tenant-one",
        user_id=HUMAN,
        team_id=None,
        domain_app_id=None,
        service="jira" if provider == "atlassian" else provider,
        provider="agentcore-identity",
        credential_type="oauth_token",
        secret_arn=f"agentcore-identity:adp-tools-dev-oauth-user:{provider}",
        scopes={"oauth_scopes": scopes},
        expires_at=None,
        agent_consent=agent_consent,
    )
    db.scalar.side_effect = [user, credential]
    reply = client.post("/internal/v1/tools/authorize", json={"schema_version": "1.0", "tool": tool}, headers=HEADERS)
    allowed_scopes = expected if agent_consent is True else None
    assert reply.status_code == (200 if allowed_scopes else 403)
    if allowed_scopes:
        assert reply.json()["connection_scopes"] == expected
        assert reply.json()["connection_id"] is None
        budget.assert_awaited_once()
        assert budget.call_args.kwargs["reserve"] is True
    else:
        denials = {"github___get_user": "GitHub connection unqualified", "jira___get_issue": "jira_connection_unqualified"}
        assert reply.json()["detail"] == denials.get(tool, "scope_not_granted")
        budget.assert_not_awaited()
        client.app.state.tool_activity_recorder.assert_not_called()
    assert db.scalar.await_count == (1 if tool in {"github___get_user", "jira___get_issue"} else 2)


def test_vendor_decision_never_selects_another_owners_connection(decision_api, monkeypatch):
    client, _, _, policy, budget, db = decision_api
    policy["allowed_tools"].append("github.get_user")
    policy["consented_tools"].append("github.get_user")
    policy["tool_max_cost_usd"]["github.get_user"] = "0.05"
    monkeypatch.setattr(routes, "get_settings", lambda: SimpleNamespace(oauth_workload_name="adp-tools-dev-oauth-user"))
    user = db.scalar.return_value
    credential = SimpleNamespace(
        id="connection-bob",
        org_id="tenant-one",
        user_id="user-bob",
        team_id=None,
        domain_app_id=None,
        agent_consent=True,
        secret_arn="agentcore-identity:adp-tools-dev-oauth-user:github",
        scopes={"oauth_scopes": ["read:user"]},
        expires_at=None,
    )
    db.scalar.side_effect = [user, credential]
    reply = client.post("/internal/v1/tools/authorize", json={"schema_version": "1.0", "tool": "github___get_user"}, headers=HEADERS)
    assert reply.status_code == 403
    assert reply.json()["detail"] == "GitHub connection unqualified"
    budget.assert_not_awaited()
    client.app.state.tool_activity_recorder.assert_not_called()


@pytest.mark.parametrize("app_grant", [False, True])
def test_github_decision_signs_current_owned_provider_observation(decision_api, monkeypatch, app_grant):
    from src.internal import github_oauth

    client, _, _, policy, budget, db = decision_api
    policy["allowed_tools"].append("github.get_user")
    policy["consented_tools"].append("github.get_user")
    policy["tool_max_cost_usd"]["github.get_user"] = "0.05"
    monkeypatch.setenv("ADP_TOOLS_GITHUB_MEDIATOR_ENABLED", "true")
    monkeypatch.setattr(routes, "get_settings", lambda: SimpleNamespace(oauth_workload_name="adp-tools-dev-oauth-user"))
    binding = {"connection_id": "owned-id", "revision": "revision-one", "account_id": "213"}
    selected = AsyncMock()
    monkeypatch.setattr(github_oauth, "selected_github_connection", selected)
    context = SimpleNamespace(account_type="human", auth_source="jwt", org_id="tenant-one", user_id=HUMAN)
    user = SimpleNamespace(id=HUMAN, team_id="team-one", user_kind="human", is_shadow=False)
    monkeypatch.setattr(routes, "authenticate_human", AsyncMock(return_value=(context, user, "team-one")))
    credential = SimpleNamespace(
        id="owned-id",
        org_id="tenant-one",
        user_id=HUMAN,
        team_id=None,
        domain_app_id=None,
        agent_consent=True,
        secret_arn="agentcore-identity:adp-tools-dev-oauth-user:github",
        scopes={"github_app_grant": {"schema_version": 1, "repository_access": False}} if app_grant else {"oauth_scopes": ["read:user"]},
        expires_at=None,
    )
    selected.return_value = (credential, object(), binding)
    db.scalar.side_effect = [credential]
    result = client.post(
        "/internal/v1/tools/authorize",
        json={"schema_version": "1.0", "tool": "github___get_user"},
        headers={"x-adp-user-authorization": "Bearer synthetic-owner-jwt"},
    )
    assert result.status_code == 200, result.text
    assert result.json()["connection_id"] == "owned-id"
    assert result.json()["github_connection"] == binding
    assert result.json()["connection_scopes"] == ["read:user"]
    selected.assert_awaited_once_with(db, user_id=HUMAN, tenant_id="tenant-one")
    budget.assert_awaited_once()


def test_jira_decision_signs_current_owned_provider_observation(decision_api, monkeypatch):
    from src.internal import jira_oauth

    client, _, _, policy, budget, db = decision_api
    policy["allowed_tools"].append("jira.get_issue")
    policy["consented_tools"].append("jira.get_issue")
    policy["tool_max_cost_usd"]["jira.get_issue"] = "0.05"
    monkeypatch.setenv("ADP_TOOLS_JIRA_MEDIATOR_ENABLED", "true")
    monkeypatch.setattr(routes, "get_settings", lambda: SimpleNamespace(oauth_workload_name="adp-tools-dev-oauth-user"))
    binding = {"connection_id": "owned-id", "revision": "revision-one", "account_id": "fixture-account", "resources_sha256": "a" * 64}
    selected = AsyncMock()
    monkeypatch.setattr(jira_oauth, "selected_jira_connection", selected)
    context = SimpleNamespace(account_type="human", auth_source="jwt", org_id="tenant-one", user_id=HUMAN)
    user = SimpleNamespace(id=HUMAN, team_id="team-one", user_kind="human", is_shadow=False)
    monkeypatch.setattr(routes, "authenticate_human", AsyncMock(return_value=(context, user, "team-one")))
    credential = SimpleNamespace(
        id="owned-id",
        org_id="tenant-one",
        user_id=HUMAN,
        team_id=None,
        domain_app_id=None,
        agent_consent=True,
        secret_arn="agentcore-identity:adp-tools-dev-oauth-user:atlassian",
        scopes={"oauth_scopes": ["read:jira-work", "read:me", "offline_access"]},
        expires_at=None,
    )
    selected.return_value = (credential, object(), binding)
    db.scalar.side_effect = [credential]
    result = client.post(
        "/internal/v1/tools/authorize",
        json={"schema_version": "1.0", "tool": "jira___get_issue"},
        headers={"x-adp-user-authorization": "Bearer synthetic-owner-jwt"},
    )
    assert result.status_code == 200, result.text
    assert result.json()["connection_id"] == "owned-id"
    assert result.json()["jira_connection"] == binding
    selected.assert_awaited_once_with(db, user_id=HUMAN, tenant_id="tenant-one")
    budget.assert_awaited_once()


def test_disconnected_or_other_user_connection_never_grants_scope(decision_api, monkeypatch):
    client, _, _, policy, budget, db = decision_api
    tool = "slack___post_message"
    permission = "slack.post_message"
    policy["allowed_tools"].append(permission)
    policy["consented_tools"].append(permission)
    policy["tool_max_cost_usd"][permission] = "0.05"
    monkeypatch.setattr(routes, "get_settings", lambda: SimpleNamespace(oauth_workload_name="adp-tools-dev-oauth-user"))
    user = db.scalar.return_value
    for credential in (
        None,
        SimpleNamespace(
            org_id="tenant-one",
            user_id="other-user",
            team_id=None,
            domain_app_id=None,
            secret_arn="agentcore-identity:adp-tools-dev-oauth-user:slack",
            scopes={"oauth_scopes": ["users:read"]},
            expires_at=None,
        ),
    ):
        db.scalar.reset_mock(side_effect=True)
        db.scalar.side_effect = [user, credential]
        reply = client.post("/internal/v1/tools/authorize", json={"schema_version": "1.0", "tool": tool}, headers=HEADERS)
        assert reply.status_code == 403
        assert reply.json()["detail"] == "Slack connection unqualified"
        budget.assert_not_awaited()


def test_host_memory_write_denied_with_named_consent_reason_before_budget(decision_api, monkeypatch):
    client, _, _, policy, budget, _ = decision_api
    client.app.state.native_test_runtime = NativeTestRuntime(client.app, None, configured=False)
    monkeypatch.setattr(
        routes,
        "authenticate_crew_host",
        AsyncMock(
            return_value=SimpleNamespace(
                tenant_id="tenant-one",
                user_id=HUMAN,
                run_id="fixture-host-run",
                instance_id="fixture-instance",
                generation=3,
                expires_at=4102444800,
            )
        ),
    )
    monkeypatch.setattr(routes, "gateway_catalogue", lambda: {"tools": [{"name": "knowledge.remember"}]})
    policy.update(allowed_tools=["knowledge.remember"], consented_tools=[], tool_max_cost_usd={"knowledge.remember": "0.01"})
    request = {
        "schema_version": "1.0",
        "tool": "knowledge___remember",
        "request_id": str(uuid4()),
        "arguments": {"session_id": "fixture-session", "messages": [{"role": "user", "content": "fixture"}]},
    }
    response = client.post(
        "/internal/v1/tools/authorize",
        json=request,
        headers={"x-adp-crew-host-assertion": "fixture-host-proof", "x-agent-runid": "fixture-host-run"},
    )
    assert response.status_code == 403 and response.json()["detail"] == "tool_not_consented"
    budget.assert_not_awaited()
    workload = client.post("/internal/v1/tools/authorize", json=request, headers=HEADERS)
    assert workload.status_code == 403 and workload.json()["detail"] == "knowledge memory human admission required"
    policy["consented_tools"] = ["knowledge.remember"]
    admission = SimpleNamespace(receipt_id="fixture-receipt", claim=Mock(return_value=True), finish=Mock())
    captured = []
    monkeypatch.setattr(routes, "ToolCallAdmission", lambda **kwargs: captured.append(kwargs) or admission)
    allowed = client.post(
        "/internal/v1/tools/authorize",
        json=request,
        headers={"x-adp-crew-host-assertion": "fixture-host-proof", "x-agent-runid": "fixture-host-run"},
    )
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["run_id"] == "fixture-host-run" and allowed.json()["host_generation"] == 3
    assert captured[0]["run_id"] == "fixture-host-run" and captured[0]["workload_id"] == "fixture-instance"
    assert allowed.json()["receipt_id"] == "fixture-receipt"
    budget.assert_awaited_once()
    admission.claim.assert_called_once()
    admission.finish.assert_called_once()


def test_verified_run_receives_only_server_resolved_principal(decision_api):
    client, runtime, policy_read, _, budget, db = decision_api
    reply = client.post("/internal/v1/tools/authorize", json=BODY, headers=HEADERS)
    assert reply.status_code == 200
    assert reply.json()["schema_version"] == "1.0" and reply.json()["allowed"] is True
    assert reply.json()["principal"] == {
        "user_id": HUMAN,
        "root_human_id": HUMAN,
        "tenant_id": "tenant-one",
        "team_id": "team-one",
    }
    assert reply.json()["receipt_id"]
    assert reply.json()["run_expires_at"] > int(datetime.now(UTC).timestamp())
    assert reply.json()["workload_id"] == "pod-one"
    assert runtime.authenticate.call_count == 2
    runtime.authenticate.assert_called_with("credential-one", "pod-one")
    policy_read.assert_called_once_with(tenant_id="tenant-one", owner_id=HUMAN)
    assert budget.await_count == 1 and db.scalar.await_count == 1
    client.app.state.tool_activity_recorder.assert_called_once_with(
        receipt_id=reply.json()["receipt_id"],
        user_id=HUMAN,
        tenant_id="tenant-one",
        team_id="team-one",
        tool=BODY["tool"],
        estimated_cost_usd=Decimal("0.05"),
        run_id="run-one",
        request_id=None,
        execution_id=None,
        task_id=None,
        story_id=None,
        consent_source="explicit",
    )


def test_ordinary_tool_authorize_passes_when_no_registry_is_bound_and_refuses_when_bound_but_unbuilt(decision_api):
    """Mirrors the model-route boundary: an environment with no native test
    registry bound has never designated anyone, so ordinary tool identity
    stands. A registry that IS bound but could not be built still fails closed.

    Regression: #8203 refused every /tools/authorize call on a deployment with
    no ADP_NATIVE_TEST_AUTHORITY_TABLE with 403 native_test_registry_unavailable.
    """
    client, _, _, _, budget, _ = decision_api
    app = client.app

    app.state.native_test_runtime = NativeTestRuntime(app, None, configured=False)
    reply = client.post("/internal/v1/tools/authorize", json=BODY, headers=HEADERS)
    assert reply.status_code == 200 and reply.json()["allowed"] is True
    budget.assert_awaited_once()

    # ...but a caller presenting native test headers is still refused.
    reply = client.post("/internal/v1/tools/authorize", json=BODY, headers={**HEADERS, "x-adp-native-test-capability": "forged"})
    assert reply.status_code == 403 and reply.json()["detail"] == "native_test_designation_refused"
    budget.assert_awaited_once()

    app.state.native_test_runtime = NativeTestRuntime(app, None)  # bound (configured=True) but no store
    reply = client.post("/internal/v1/tools/authorize", json=BODY, headers=HEADERS)
    assert reply.status_code == 403 and reply.json()["detail"] == "native_test_registry_unavailable"
    budget.assert_awaited_once()


def test_verified_execution_binds_only_its_own_task_and_story(decision_api):
    client, runtime, _, _, _, _ = decision_api
    pod, caller, _, grant = runtime.authenticate.return_value
    runtime.authenticate.return_value = (
        pod,
        caller,
        {"task_id": {"S": "tsk-verified"}, "repo": {"S": "example/project"}, "issue_number": {"N": "123"}},
        grant,
    )
    reply = client.post("/internal/v1/tools/authorize", json=BODY, headers=HEADERS)
    assert reply.status_code == 200
    assert client.app.state.tool_activity_recorder.call_args.kwargs["task_id"] == "tsk-verified"
    assert client.app.state.tool_activity_recorder.call_args.kwargs["story_id"] == "example/project#123"


def test_laptop_call_does_not_invent_a_task_or_story(decision_api, monkeypatch):
    client, _, _, _, _, _ = decision_api
    bearer_identity(monkeypatch)
    reply = client.post("/internal/v1/tools/authorize", json=BODY, headers={"x-adp-user-authorization": "Bearer signed-login"})
    assert reply.status_code == 200
    assert client.app.state.tool_activity_recorder.call_args.kwargs["task_id"] is None
    assert client.app.state.tool_activity_recorder.call_args.kwargs["story_id"] is None


def test_tool_rate_limit_rejects_before_budget_reservation(decision_api, monkeypatch):
    client, _, _, _, budget, _ = decision_api
    rate = AsyncMock(return_value=SimpleNamespace(allowed=False, retry_after_seconds=12))
    monkeypatch.setattr(routes, "get_rate_limit_service", lambda request: SimpleNamespace(consume_tool_rate_limit=rate))
    reply = client.post("/internal/v1/tools/authorize", json=BODY, headers=HEADERS)
    assert reply.status_code == 429
    assert reply.json() == {"detail": "tool_rate_limit_exceeded"}
    assert reply.headers["retry-after"] == "12"
    rate.assert_awaited_once()
    context, tool = rate.await_args.args
    assert (context.org_id, context.user_id, tool) == ("tenant-one", HUMAN, BODY["tool"])
    budget.assert_not_awaited()
    client.app.state.tool_activity_recorder.assert_not_called()


def test_tool_activity_store_must_be_configured(decision_api, monkeypatch):
    client, _, _, _, budget, _ = decision_api
    monkeypatch.delenv("WEBHOOK_EVENTS_TABLE")
    reply = client.post("/internal/v1/tools/authorize", json=BODY, headers=HEADERS)
    assert reply.status_code == 503 and reply.json() == {"detail": "tool activity unavailable"}
    budget.assert_not_awaited()
    client.app.state.tool_activity_recorder.assert_not_called()


def test_recovery_rechecks_run_without_new_tool_grant_or_budget(decision_api):
    client, runtime, policy_read, policy, budget, _ = decision_api
    policy["status"] = "disabled"
    body = {"schema_version": "1.0", "action": "recover", "tool": BODY["tool"]}
    reply = client.post("/internal/v1/tools/authorize", json=body, headers=HEADERS)
    assert reply.status_code == 200 and reply.json()["principal"]["user_id"] == HUMAN
    assert runtime.authenticate.call_count == 2
    policy_read.assert_not_called()
    budget.assert_not_awaited()
    for invalid in ({**body, "arguments": {"query": "example"}}, {"schema_version": "1.0", "action": "recover"}):
        assert client.post("/internal/v1/tools/authorize", json=invalid, headers=HEADERS).status_code == 422
    runtime.authenticate.side_effect = ExecutionStateError("execution_not_active:completed")
    assert client.post("/internal/v1/tools/authorize", json=body, headers=HEADERS).status_code == 404
    policy_read.assert_not_called()


def test_recovery_rechecks_laptop_membership_without_policy(decision_api, monkeypatch):
    client, _, policy_read, policy, budget, _ = decision_api
    _, validator, member = bearer_identity(monkeypatch)
    policy["status"] = "disabled"
    body = {"schema_version": "1.0", "action": "recover", "tool": BODY["tool"]}
    headers = {"x-adp-user-authorization": "Bearer signed-login"}
    assert client.post("/internal/v1/tools/authorize", json=body, headers=headers).status_code == 200
    assert validator.await_count == member.await_count == 2
    policy_read.assert_not_called()
    budget.assert_not_awaited()
    member.side_effect = HTTPException(403, "membership revoked")
    assert client.post("/internal/v1/tools/authorize", json=body, headers=headers).status_code == 403


def test_close_rechecks_run_but_not_revoked_tool_policy(decision_api):
    client, runtime, policy_read, policy, budget, _ = decision_api
    policy["status"] = "disabled"
    body = {"schema_version": "1.0", "action": "close", "tool": BODY["tool"]}
    reply = client.post("/internal/v1/tools/authorize", json=body, headers=HEADERS)
    assert reply.status_code == 200 and reply.json()["principal"]["user_id"] == HUMAN
    assert runtime.authenticate.call_count == 2
    policy_read.assert_not_called()
    budget.assert_not_awaited()
    for invalid in ({**body, "arguments": {"query": "example"}}, {"schema_version": "1.0", "action": "close"}):
        assert client.post("/internal/v1/tools/authorize", json=invalid, headers=HEADERS).status_code == 422
    runtime.authenticate.side_effect = ExecutionStateError("execution_not_active:completed")
    assert client.post("/internal/v1/tools/authorize", json=body, headers=HEADERS).status_code == 404


def test_close_requires_current_human_membership_after_policy_revocation(decision_api, monkeypatch):
    client, _, policy_read, policy, budget, _ = decision_api
    _, validator, member = bearer_identity(monkeypatch)
    policy["status"] = "disabled"
    body = {"schema_version": "1.0", "action": "close", "tool": BODY["tool"]}
    headers = {"x-adp-user-authorization": "Bearer signed-login"}
    assert client.post("/internal/v1/tools/authorize", json=body, headers=headers).status_code == 200
    assert validator.await_count == member.await_count == 2
    policy_read.assert_not_called()
    budget.assert_not_awaited()
    member.side_effect = HTTPException(403, "membership revoked")
    assert client.post("/internal/v1/tools/authorize", json=body, headers=headers).status_code == 403


def bearer_identity(monkeypatch):
    context = SimpleNamespace(user_id="login-subject", org_id="tenant-one", team_id="old-team", account_type="human", auth_source="jwt")
    user = SimpleNamespace(id=HUMAN, user_kind="human", is_shadow=False, team_id="team-one")
    validator = AsyncMock(return_value=context)
    member = AsyncMock(return_value=(user, "membership-one", SimpleNamespace(id="team-one")))
    monkeypatch.setattr(routes, "validate_cognito_jwt", validator)
    monkeypatch.setattr(routes, "membership", member)
    return context, validator, member


def test_laptop_bearer_resolves_canonical_member_before_budget_and_receipt(decision_api, monkeypatch):
    client, runtime, policy_read, _, budget, db = decision_api
    context, validator, member = bearer_identity(monkeypatch)
    reply = client.post("/internal/v1/tools/authorize", json=BODY, headers={"x-adp-user-authorization": "Bearer signed-login"})
    assert reply.status_code == 200
    assert reply.json()["principal"] == {
        "user_id": HUMAN,
        "root_human_id": HUMAN,
        "tenant_id": "tenant-one",
        "team_id": "team-one",
    }
    assert reply.json()["receipt_id"]
    assert validator.await_count == 2
    validator.assert_awaited_with("Bearer signed-login")
    member.assert_awaited_with(db, context, "tenant-one")
    policy_read.assert_called_once_with(tenant_id="tenant-one", owner_id=HUMAN)
    assert budget.await_count == 1
    runtime.authenticate.assert_not_called()
    db.scalar.assert_not_awaited()


@pytest.mark.parametrize("request_body", [BODY, {"schema_version": "1.0", "action": "list"}])
def test_foreign_tenant_bearer_denied_before_policy_or_provider(decision_api, monkeypatch, request_body):
    client, runtime, policy_read, _, budget, _ = decision_api
    _, _, member = bearer_identity(monkeypatch)
    member.side_effect = HTTPException(403, "tenant_not_visible")
    reply = client.post("/internal/v1/tools/authorize", json=request_body, headers={"x-adp-user-authorization": "Bearer foreign-tenant"})
    assert reply.status_code == 403
    policy_read.assert_not_called()
    budget.assert_not_awaited()
    runtime.authenticate.assert_not_called()


def test_revoked_bearer_denied_before_policy(decision_api, monkeypatch):
    client, _, policy_read, _, budget, _ = decision_api
    _, validator, _ = bearer_identity(monkeypatch)
    validator.side_effect = HTTPException(401, "expired")
    reply = client.post("/internal/v1/tools/authorize", json=BODY, headers={"x-adp-user-authorization": "Bearer revoked"})
    assert reply.status_code == 403
    policy_read.assert_not_called()
    budget.assert_not_awaited()


def test_catalog_intersects_entitlement_consent_and_budget(decision_api, monkeypatch):
    client, runtime, policy_read, policy, budget, _ = decision_api
    bearer_identity(monkeypatch)
    policy["catalog"] = [
        {"name": "websearch___search", "description": "Search", "inputSchema": {"type": "object"}},
        {"name": "browser___navigate", "description": "Browse", "inputSchema": {"type": "object"}},
    ]
    reply = client.post(
        "/internal/v1/tools/authorize", json={"schema_version": "1.0", "action": "list"}, headers={"x-adp-user-authorization": "Bearer signed-login"}
    )
    assert reply.status_code == 200
    assert reply.json()["tools"] == policy["catalog"][:1]
    assert reply.json()["principal"]["user_id"] == HUMAN
    assert budget.await_count == 1
    policy_read.assert_called_once()
    runtime.authenticate.assert_not_called()


def test_catalog_budget_refusal_omits_unaffordable_tool(decision_api, monkeypatch):
    client, _, _, policy, budget, _ = decision_api
    bearer_identity(monkeypatch)
    policy["catalog"] = [{"name": "websearch___search", "description": "Search", "inputSchema": {"type": "object"}}]
    budget.return_value = SimpleNamespace(allowed=False)
    reply = client.post(
        "/internal/v1/tools/authorize", json={"schema_version": "1.0", "action": "list"}, headers={"x-adp-user-authorization": "Bearer signed-login"}
    )
    assert reply.status_code == 200 and reply.json()["tools"] == []


@pytest.mark.parametrize("bearer", [False, True])
def test_repeated_discovery_checks_ledger_without_reserving_paid_budget(decision_api, monkeypatch, bearer):
    client, _, _, policy, _, _ = decision_api
    headers = HEADERS
    if bearer:
        bearer_identity(monkeypatch)
        headers = {"x-adp-user-authorization": "Bearer signed-login"}
    policy["catalog"] = [
        {"name": "websearch___search", "description": "Search", "inputSchema": {"type": "object"}},
        {"name": "browser___navigate", "description": "Browse", "inputSchema": {"type": "object"}},
    ]
    policy["allowed_tools"].append("browser.navigate")
    policy["consented_tools"].append("browser.navigate")
    policy["tool_max_cost_usd"]["browser.navigate"] = "0.20"
    budget_row = SimpleNamespace(budget_amount_usd=Decimal("0.10"), enforcement_mode="hard")
    usage_row = SimpleNamespace(total_cost_usd=Decimal("0"))

    async def ledger_read(statement):
        row = usage_row if statement.column_descriptions[0]["entity"] is BudgetUsage else budget_row
        return Mock(scalar_one_or_none=Mock(return_value=row))

    ledger = SimpleNamespace(execute=AsyncMock(side_effect=ledger_read))
    reservations = SimpleNamespace(enabled=True, reserve=AsyncMock(return_value=ReservationOutcome(admitted=True)), observe=AsyncMock())
    service = enforcement_service.BudgetEnforcementService(db_session=ledger, reservations=reservations)
    monkeypatch.setattr(enforcement_service, "budget_config", BudgetConfig(budget_check_enabled=True, budget_reservation_enabled=True))
    monkeypatch.setattr(service, "_check_person_budget_contained", AsyncMock(return_value=None))
    monkeypatch.setattr(service, "_note_check_succeeded", AsyncMock())
    monkeypatch.setattr(routes, "BudgetEnforcementService", lambda: service)
    for _ in range(2):
        reply = client.post("/internal/v1/tools/authorize", json={"schema_version": "1.0", "action": "list"}, headers=headers)
        assert reply.status_code == 200 and reply.json()["tools"] == policy["catalog"][:1]
    assert ledger.execute.await_count > 0
    reservations.reserve.assert_not_awaited()
    reservations.observe.assert_not_awaited()
    paid = client.post("/internal/v1/tools/authorize", json=BODY, headers=headers)
    assert paid.status_code == 200
    reservations.reserve.assert_awaited_once()
    request_id, cost, targets = reservations.reserve.await_args.args
    assert request_id == paid.json()["receipt_id"] and cost == Decimal("0.05") and targets
    assert {("user", HUMAN, "daily"), ("org", "tenant-one", "daily")} <= {
        (target.entity_type, target.entity_id, target.period_type) for target in targets
    }
    assert all(target.ttl_seconds and target.ttl_seconds > 120 for target in targets)
    usage_row.total_cost_usd = Decimal("0.10")
    unavailable = client.post("/internal/v1/tools/authorize", json={"schema_version": "1.0", "action": "list"}, headers=headers)
    assert unavailable.status_code == 200 and unavailable.json()["tools"] == []
    denied = client.post("/internal/v1/tools/authorize", json=BODY, headers=headers)
    assert denied.status_code == 429 and denied.json() == {"detail": "tool_budget_exceeded"}
    assert int(denied.headers["retry-after"]) > 0
    assert client.post("/internal/v1/tools/authorize", json={**BODY, "reserve": False}, headers=headers).status_code == 422
    assert reservations.reserve.await_count == 1
    reservations.reserve.return_value = None
    usage_row.total_cost_usd = Decimal("0")
    assert client.post("/internal/v1/tools/authorize", json=BODY, headers=headers).status_code == 503


def test_tool_estimates_accumulate_against_daily_user_and_tenant_cap(decision_api, monkeypatch):
    import fakeredis.aioredis

    from src.budget.reservations import ReservationStore

    client, runtime, _, _, _, _ = decision_api
    budget_row = SimpleNamespace(budget_amount_usd=Decimal("0.10"), enforcement_mode="hard")
    usage_row = SimpleNamespace(total_cost_usd=Decimal("0"))

    async def ledger_read(statement):
        row = usage_row if statement.column_descriptions[0]["entity"] is BudgetUsage else budget_row
        return Mock(scalar_one_or_none=Mock(return_value=row))

    ledger = SimpleNamespace(execute=AsyncMock(side_effect=ledger_read))
    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    reservations = ReservationStore(redis_url=None, ttl_seconds=120, client=redis_client)
    service = enforcement_service.BudgetEnforcementService(db_session=ledger, reservations=reservations)
    monkeypatch.setattr(enforcement_service, "budget_config", BudgetConfig(budget_check_enabled=True, budget_reservation_enabled=True))
    monkeypatch.setattr(service, "_check_person_budget_contained", AsyncMock(return_value=None))
    monkeypatch.setattr(service, "_note_check_succeeded", AsyncMock())
    monkeypatch.setattr(routes, "BudgetEnforcementService", lambda: service)

    assert client.post("/internal/v1/tools/authorize", json=BODY, headers=HEADERS).status_code == 200
    assert client.post("/internal/v1/tools/authorize", json=BODY, headers=HEADERS).status_code == 200
    runtime.authenticate.return_value[3].authority.human_id = "00000000-0000-4000-8000-000000000002"
    refused = client.post("/internal/v1/tools/authorize", json=BODY, headers=HEADERS)
    assert refused.status_code == 429 and refused.json() == {"detail": "tool_budget_exceeded"}


def test_malformed_grants_never_expose_catalog(decision_api, monkeypatch):
    client, _, _, policy, budget, _ = decision_api
    bearer_identity(monkeypatch)
    policy["allowed_tools"] = "websearch.search"
    policy["catalog"] = [{"name": "websearch___search", "description": "Search", "inputSchema": {"type": "object"}}]
    reply = client.post(
        "/internal/v1/tools/authorize", json={"schema_version": "1.0", "action": "list"}, headers={"x-adp-user-authorization": "Bearer signed-login"}
    )
    assert reply.status_code == 503
    budget.assert_not_awaited()


def test_mixed_or_duplicate_credentials_denied_before_validation(decision_api, monkeypatch):
    client, _, policy_read, _, _, _ = decision_api
    _, validator, _ = bearer_identity(monkeypatch)
    for headers in (
        {"x-adp-user-authorization": "Bearer signed-login", **HEADERS},
        [("x-adp-user-authorization", "Bearer signed-login"), ("x-adp-user-authorization", "Bearer another")],
    ):
        reply = client.post("/internal/v1/tools/authorize", json=BODY, headers=headers)
        assert reply.status_code == 403
    validator.assert_not_awaited()
    policy_read.assert_not_called()


def test_malformed_list_body_cannot_bypass_tool_validation(decision_api):
    client, _, _, _, _, _ = decision_api
    for body in (
        {"schema_version": "1.0", "action": "list", "tool": "websearch___search"},
        {"schema_version": "1.0", "action": "list", "arguments": {"query": "all"}},
    ):
        assert client.post("/internal/v1/tools/authorize", json=body, headers=HEADERS).status_code == 422


def test_wrong_facade_role_refused_before_run_or_policy(decision_api, monkeypatch):
    client, runtime, policy_read, _, budget, _ = decision_api
    monkeypatch.setattr(routes, "verified_caller_identity", lambda request, settings: "arn:aws:sts::123456789012:assumed-role/other/pod")
    assert client.post("/internal/v1/tools/authorize", json=BODY, headers=HEADERS).status_code == 403
    runtime.authenticate.assert_not_called()
    policy_read.assert_not_called()
    budget.assert_not_awaited()


@pytest.mark.parametrize(
    "refusal",
    [
        CredentialError("expired credential"),
        BootstrapRefusedError("ended grant"),
        ExecutionStateError("execution_not_active:cancelled"),
    ],
)
def test_expired_run_cannot_reach_policy_or_provider(decision_api, refusal):
    client, runtime, policy_read, _, budget, _ = decision_api
    runtime.authenticate.side_effect = refusal
    reply = client.post("/internal/v1/tools/authorize", json=BODY, headers=HEADERS)
    assert reply.status_code == 404 and reply.json()["detail"] == "unknown_run"
    policy_read.assert_not_called()
    budget.assert_not_awaited()


@pytest.mark.parametrize(
    "refusal",
    [
        BootstrapRefusedError("lease ended"),
        ExecutionStateError("execution_not_active:completed"),
    ],
)
def test_lease_ending_after_policy_recheck_denies_without_decision(decision_api, refusal):
    client, runtime, policy_read, _, budget, _ = decision_api
    runtime.authenticate.side_effect = [runtime.authenticate.return_value, refusal]
    reply = client.post("/internal/v1/tools/authorize", json=BODY, headers=HEADERS)
    assert reply.status_code == 404 and reply.json()["detail"] == "unknown_run"
    assert runtime.authenticate.call_count == 2
    policy_read.assert_called_once()
    budget.assert_awaited_once()


@pytest.mark.parametrize("restriction", ["allowed_tools", "consented_tools", "status"])
def test_missing_entitlement_or_consent_refused(decision_api, restriction):
    client, _, _, policy, budget, _ = decision_api
    policy[restriction] = "disabled" if restriction == "status" else []
    assert client.post("/internal/v1/tools/authorize", json=BODY, headers=HEADERS).status_code == 403
    budget.assert_not_awaited()


def test_missing_or_invalid_provider_ceiling_refused(decision_api):
    client, _, _, policy, budget, _ = decision_api
    for ceiling in ({}, {"websearch.search": "NaN"}, {"websearch.search": "-1"}):
        policy["tool_max_cost_usd"] = ceiling
        assert client.post("/internal/v1/tools/authorize", json=BODY, headers=HEADERS).status_code == 403
    budget.assert_not_awaited()


@pytest.mark.parametrize("tool", ["websearch___search", "browser___start", "code_interpreter___start"])
def test_budget_denial_covers_paid_tool_families(decision_api, tool):
    client, _, _, policy, budget, _ = decision_api
    permission = tool.replace("___", ".", 1)
    policy["allowed_tools"] = [permission]
    policy["consented_tools"] = [permission]
    policy["tool_max_cost_usd"] = {permission: "0.05"}
    budget.return_value = SimpleNamespace(allowed=False, deny_reason=DenyReason.BUDGET_EXCEEDED)
    reply = client.post("/internal/v1/tools/authorize", json={"schema_version": "1.0", "tool": tool}, headers=HEADERS)
    assert reply.status_code == 429 and reply.json() == {"detail": "tool_budget_exceeded"}
    assert reply.headers["retry-after"] == "60"
    budget.assert_awaited_once()
    assert budget.await_args.kwargs["reserve"] is True
    client.app.state.tool_activity_recorder.assert_not_called()


def test_budget_denial_and_wrong_tenant_fail_closed(decision_api):
    client, runtime, _, _, budget, _ = decision_api
    budget.return_value = SimpleNamespace(allowed=False, deny_reason=DenyReason.BUDGET_EXCEEDED)
    denied = client.post("/internal/v1/tools/authorize", json=BODY, headers=HEADERS)
    assert denied.status_code == 429 and denied.json() == {"detail": "tool_budget_exceeded"}
    assert denied.headers["retry-after"] == "60"
    budget.return_value = SimpleNamespace(allowed=False, deny_reason=DenyReason.CHECK_UNAVAILABLE)
    unavailable = client.post("/internal/v1/tools/authorize", json=BODY, headers=HEADERS)
    assert unavailable.status_code == 503 and unavailable.headers["retry-after"] == "5"
    budget.reset_mock()
    runtime.authenticate.return_value[1].tenant_id = "other-tenant"
    assert client.post("/internal/v1/tools/authorize", json=BODY, headers=HEADERS).status_code == 404
    budget.assert_not_awaited()


def test_caller_cannot_supply_identity_or_unsupported_contract(decision_api):
    client, _, _, _, _, _ = decision_api
    assert client.post("/internal/v1/tools/authorize", json={**BODY, "user_id": HUMAN}, headers=HEADERS).status_code == 422
    assert client.post("/internal/v1/tools/authorize", json={**BODY, "schema_version": "2.0"}, headers=HEADERS).status_code == 422


def test_internal_router_exposes_authorize_once():
    from src.internal.routes import router

    assert [route.path for route in router.routes].count("/internal/v1/tools/authorize") == 1


def real_bearer_identity(monkeypatch, **overrides):
    """Each validation builds a fresh TokenContext, as the Cognito validator does in production."""
    exp = datetime.now(UTC) + timedelta(minutes=5)

    def build():
        values = dict(
            user_id="login-subject",
            org_id="tenant-one",
            team_id="team-one",
            department_id="dept-one",
            account_type="human",
            cognito_username="login@example.test",
            expires_at=exp,
        )
        values.update(overrides)
        return TokenContext(**values)

    validator = AsyncMock(side_effect=lambda token: build())
    user = SimpleNamespace(id=HUMAN, user_kind="human", is_shadow=False, team_id="team-one")
    member = AsyncMock(return_value=(user, "membership-one", SimpleNamespace(id="team-one")))
    monkeypatch.setattr(routes, "validate_cognito_jwt", validator)
    monkeypatch.setattr(routes, "membership", member)
    return validator


def test_bearer_recheck_compares_identity_not_context_object(decision_api, monkeypatch):
    # TokenContext carries per-request private budget state, so two validations
    # of one token are never ``==``; the re-check must compare the identity.
    client, *_ = decision_api
    validator = real_bearer_identity(monkeypatch)
    reply = client.post("/internal/v1/tools/authorize", json=BODY, headers={"x-adp-user-authorization": "Bearer signed-login"})
    assert reply.status_code == 200, reply.text
    assert reply.json()["principal"]["user_id"] == HUMAN
    assert validator.await_count == 2
    listing = client.post(
        "/internal/v1/tools/authorize", json={"schema_version": "1.0", "action": "list"}, headers={"x-adp-user-authorization": "Bearer signed-login"}
    )
    assert listing.status_code == 200, listing.text


def test_bearer_recheck_still_refuses_a_changed_identity(decision_api, monkeypatch):
    client, *_ = decision_api
    first = TokenContext(
        user_id="login-subject",
        org_id="tenant-one",
        team_id="team-one",
        department_id="dept-one",
        account_type="human",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    second = first.model_copy(update={"org_id": "tenant-two"})
    validator = AsyncMock(side_effect=[first, second])
    user = SimpleNamespace(id=HUMAN, user_kind="human", is_shadow=False, team_id="team-one")
    monkeypatch.setattr(routes, "validate_cognito_jwt", validator)
    monkeypatch.setattr(routes, "membership", AsyncMock(return_value=(user, "membership-one", SimpleNamespace(id="team-one"))))
    reply = client.post("/internal/v1/tools/authorize", json=BODY, headers={"x-adp-user-authorization": "Bearer signed-login"})
    assert reply.status_code == 403
    assert reply.json()["detail"] == "tool authority refused"


@pytest.mark.asyncio
async def test_production_route_denies_team_x_and_allows_other_team(decision_api, monkeypatch):
    client, _, _, policy, budget, _ = decision_api
    monkeypatch.setattr(routes, "effective_entitlement", real_effective_entitlement)
    policy.update(
        {
            "allowed_tools": ["browser.start"],
            "consented_tools": ["browser.start"],
            "tool_max_cost_usd": {"browser.start": "0.05"},
            "catalog": [{"name": "browser___start", "description": "Open browser", "inputSchema": {"type": "object"}}],
        }
    )
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(User.__table__.create)
        await connection.run_sync(ToolEntitlement.__table__.create)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def database():
        async with sessions() as db:
            yield db

    client.app.dependency_overrides[get_db] = database
    try:
        async with sessions() as db:
            db.add(User(id=HUMAN, org_id="tenant-one", team_id="team-x", email="owner@example.invalid", cognito_sub="login-subject"))
            db.add_all(
                [
                    ToolEntitlement(org_id="tenant-one", team_id="", tool_name="browser.start", enabled=True, max_cost_usd=Decimal("0.20")),
                    ToolEntitlement(org_id="tenant-one", team_id="team-x", tool_name="browser.start", enabled=False),
                    ToolEntitlement(org_id="tenant-one", team_id="team-y", tool_name="browser.start", enabled=True, max_cost_usd=Decimal("0.03")),
                ]
            )
            await db.commit()
        async with AsyncClient(transport=ASGITransport(app=client.app), base_url="http://test") as transport:
            body = {"schema_version": "1.0", "tool": "browser___start", "arguments": {}}
            listing = {"schema_version": "1.0", "action": "list"}
            denied = await transport.post("/internal/v1/tools/authorize", json=body, headers=HEADERS)
            assert denied.status_code == 403
            hidden = await transport.post("/internal/v1/tools/authorize", json=listing, headers=HEADERS)
            assert hidden.status_code == 200 and hidden.json()["tools"] == []
            budget.assert_not_awaited()
            async with sessions() as db:
                owner = await db.get(User, HUMAN)
                owner.team_id = "team-y"
                await db.commit()
            permitted = await transport.post("/internal/v1/tools/authorize", json=body, headers=HEADERS)
            assert permitted.status_code == 200
            assert budget.call_args.args[1] == Decimal("0.03")
            visible = await transport.post("/internal/v1/tools/authorize", json=listing, headers=HEADERS)
            assert visible.status_code == 200 and visible.json()["tools"] == policy["catalog"]
            async with sessions() as db:
                owner = await db.get(User, HUMAN)
                owner.team_id = "team-z"
                await db.commit()
            inherited = await transport.post("/internal/v1/tools/authorize", json=body, headers=HEADERS)
            assert inherited.status_code == 200
            assert budget.call_args.args[1] == Decimal("0.05")
            billed = budget.await_count
            recorded = client.app.state.tool_activity_recorder.call_count
            monkeypatch.setattr(routes, "gateway_catalogue", lambda: {"tools": []})
            removed = await transport.post("/internal/v1/tools/authorize", json=body, headers=HEADERS)
            assert removed.status_code == 403 and removed.json()["detail"] == "tool_undeployed"
            assert (await transport.post("/internal/v1/tools/authorize", json=listing, headers=HEADERS)).json()["tools"] == []
            assert budget.await_count == billed
            assert client.app.state.tool_activity_recorder.call_count == recorded
            monkeypatch.setattr(routes, "gateway_catalogue", lambda: {"tools": [{"name": "browser.start"}]})
            async with sessions() as db:
                owner = await db.get(User, HUMAN)
                owner.team_id = "team-y"
                tenant = await db.get(ToolEntitlement, ("tenant-one", "", "browser.start"))
                tenant.enabled = False
                await db.commit()
            assert (await transport.post("/internal/v1/tools/authorize", json=body, headers=HEADERS)).status_code == 403
            assert (await transport.post("/internal/v1/tools/authorize", json=listing, headers=HEADERS)).json()["tools"] == []
            assert budget.await_count == billed
    finally:
        await engine.dispose()


def test_unavailable_gateway_catalogue_fails_closed_before_provider_work(decision_api, monkeypatch):
    client, _, _, policy, budget, _ = decision_api
    policy["catalog"] = [{"name": "websearch___search", "description": "Search", "inputSchema": {"type": "object"}}]
    monkeypatch.setattr(routes, "gateway_catalogue", Mock(side_effect=HTTPException(503, "tool catalogue unavailable")))
    for body in (BODY, {"schema_version": "1.0", "action": "list"}):
        denied = client.post("/internal/v1/tools/authorize", json=body, headers=HEADERS)
        assert denied.status_code == 503 and denied.json()["detail"] == "tool catalogue unavailable"
    budget.assert_not_awaited()


def test_tool_entitlement_database_failure_refuses_without_billing(decision_api, monkeypatch):
    client, _, _, policy, budget, _ = decision_api
    policy["catalog"] = [{"name": "websearch___search", "description": "Search", "inputSchema": {"type": "object"}}]
    monkeypatch.setattr(routes, "effective_entitlement", AsyncMock(side_effect=SQLAlchemyError("database unavailable")))
    response = client.post("/internal/v1/tools/authorize", json=BODY, headers=HEADERS)
    assert response.status_code == 503
    assert response.json()["detail"] == "tool entitlement unavailable"
    listing = client.post("/internal/v1/tools/authorize", json={"schema_version": "1.0", "action": "list"}, headers=HEADERS)
    assert listing.status_code == 503 and listing.json()["detail"] == "tool entitlement unavailable"
    budget.assert_not_awaited()


@pytest.mark.parametrize(
    "restriction,reason",
    [
        ("tenant", "tool_not_entitled"),
        ("user", "tool_not_allowed"),
        ("consent", "tool_not_consented"),
        ("cost", "tool_cost_policy_missing"),
    ],
)
def test_call_and_list_share_tenant_first_eligibility_without_provider_work(decision_api, monkeypatch, restriction, reason):
    client, _, _, policy, budget, db = decision_api
    policy["catalog"] = [{"name": "websearch___search", "description": "Search", "inputSchema": {"type": "object"}}]
    if restriction == "tenant":
        monkeypatch.setattr(routes, "effective_entitlement", AsyncMock(return_value=(False, None)))
        policy["allowed_tools"] = []
    elif restriction == "user":
        policy["allowed_tools"] = []
    elif restriction == "consent":
        policy["consented_tools"] = []
    else:
        policy["tool_max_cost_usd"] = {}
    rate = Mock(side_effect=AssertionError("rate must not run for a denied tool"))
    monkeypatch.setattr(routes, "get_rate_limit_service", rate)
    denied = client.post("/internal/v1/tools/authorize", json=BODY, headers=HEADERS)
    assert denied.status_code == 403 and denied.json()["detail"] == reason
    assert db.scalar.await_count == 1
    hidden = client.post("/internal/v1/tools/authorize", json={"schema_version": "1.0", "action": "list"}, headers=HEADERS)
    assert hidden.status_code == 200 and hidden.json()["tools"] == []
    assert budget.await_count == 0
    rate.assert_not_called()


def test_missing_cost_policy_hides_only_affected_tool_on_list(decision_api):
    client, _, _, policy, budget, _ = decision_api
    policy["allowed_tools"].append("browser.start")
    policy["consented_tools"].append("browser.start")
    policy["catalog"] = [
        {"name": "websearch___search", "description": "Search", "inputSchema": {"type": "object"}},
        {"name": "browser___start", "description": "Browse", "inputSchema": {"type": "object"}},
    ]
    listed = client.post("/internal/v1/tools/authorize", json={"schema_version": "1.0", "action": "list"}, headers=HEADERS)
    assert listed.status_code == 200
    assert [entry["name"] for entry in listed.json()["tools"]] == ["websearch___search"]
    assert budget.await_count == 1


@pytest.mark.parametrize(
    ("connector", "operation", "properties"),
    [
        ("jira", "get_issue", {"cloudId": {"type": "string"}, "issueIdOrKey": {"type": "string"}}),
        ("slack", "get_user", {"user": {"type": "string"}}),
        ("slack", "post_message", {"channel": {"type": "string"}, "text": {"type": "string"}}),
    ],
)
def test_connector_parameters_survive_policy_and_discovery(decision_api, monkeypatch, connector, operation, properties):
    client, _, _, policy, _, db = decision_api
    monkeypatch.setattr(routes, "get_settings", lambda: SimpleNamespace(oauth_workload_name="adp-tools-dev-oauth-user"))
    provider = "atlassian" if connector == "jira" else connector
    scopes = ["read:jira-work"] if connector == "jira" else ["users:read", "chat:write"]
    db.scalar.side_effect = [db.scalar.return_value, owned_connector(provider, scopes)]
    control = Mock()
    monkeypatch.setenv("ADP_TOOLS_GATEWAY_ROLE_ARN", "test-tools-role")
    control.get_gateway.return_value = {"roleArn": "test-tools-role"}
    control.get_paginator.side_effect = lambda name: SimpleNamespace(
        paginate=lambda **kwargs: [
            {
                "items": [{"gatewayId": "gateway-one", "name": "adp-tools"}]
                if name == "list_gateways"
                else [{"targetId": connector, "status": "READY"}]
            }
        ]
    )
    spec = Path(__file__).resolve().parents[3] / "tools" / "platform" / "schemas" / f"{connector}.yaml"
    control.get_gateway_target.return_value = {
        "name": connector,
        "targetConfiguration": {"mcp": {"openApiSchema": {"inlinePayload": spec.read_text()}}},
        "credentialProviderConfigurations": [{"credentialProviderType": "OAUTH"}],
    }
    monkeypatch.setattr(tools_catalogue.boto3, "client", lambda service: control)
    tool_name = f"{connector}.{operation}"
    tool = next(entry for entry in tools_catalogue.gateway_catalogue()["tools"] if entry["name"] == tool_name)
    expected = {"type": "object", "properties": properties, "required": list(properties)}
    assert tool["inputSchema"] == expected
    catalog = [{"name": tool_name.replace(".", "___"), "description": tool["description"], "inputSchema": tool["inputSchema"]}]
    authority = Mock()
    authority.get_item.return_value = {}
    store = ToolsPolicyStore(table_name="authority", client=authority)
    store.put_allowed(
        tenant_id="tenant-one",
        owner_id=HUMAN,
        allowed_tools=[tool_name],
        tool_max_cost_usd={tool_name: Decimal("0.20")},
        catalog=catalog,
        expected_revision=0,
    )
    authority.get_item.return_value = {"Item": authority.put_item.call_args.kwargs["Item"]}
    policy.clear()
    policy.update(store.get(tenant_id="tenant-one", owner_id=HUMAN))
    assert policy["catalog"][0]["inputSchema"] == expected
    policy["consented_tools"] = [tool_name]
    response = client.post("/internal/v1/tools/authorize", json={"schema_version": "1.0", "action": "list"}, headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["tools"] == ([] if connector == "jira" or tool_name.startswith("slack.") else catalog)


def test_fresh_user_policy_catalogue_becomes_discoverable_only_after_consent(decision_api):
    client, _, _, policy, budget, _ = decision_api
    authority = Mock()
    authority.get_item.return_value = {}
    catalog = [
        {"name": "browser___start", "description": "Start browser", "inputSchema": {"type": "object", "properties": {"url": {"type": "string"}}}},
        {"name": "code_interpreter___start", "description": "Start interpreter", "inputSchema": {"type": "object"}},
    ]
    store = ToolsPolicyStore(table_name="authority", client=authority)
    created = store.put_allowed(
        tenant_id="tenant-one",
        owner_id=HUMAN,
        allowed_tools=["browser.start", "code_interpreter.start"],
        tool_max_cost_usd={"browser.start": Decimal("0.20"), "code_interpreter.start": Decimal("0.30")},
        catalog=catalog,
        expected_revision=0,
    )
    assert created["consented_tools"] == []
    authority.get_item.return_value = {"Item": authority.put_item.call_args.kwargs["Item"]}
    policy.clear()
    policy.update(store.get(tenant_id="tenant-one", owner_id=HUMAN))
    listing = {"schema_version": "1.0", "action": "list"}
    before = client.post("/internal/v1/tools/authorize", json=listing, headers=HEADERS)
    assert before.status_code == 200 and before.json()["tools"] == []
    budget.assert_not_awaited()
    policy["consented_tools"] = ["browser.start", "code_interpreter.start"]
    after = client.post("/internal/v1/tools/authorize", json=listing, headers=HEADERS)
    assert after.status_code == 200
    assert after.json()["tools"] == catalog
    called = client.post(
        "/internal/v1/tools/authorize",
        json={
            "schema_version": "1.0",
            "tool": "browser___start",
            "arguments": {"url": "https://example.test"},
        },
        headers=HEADERS,
    )
    assert called.status_code == 200
    assert called.json()["allowed"] is True


def test_first_party_platform_tools_are_never_authorized_for_the_facade(decision_api, monkeypatch):
    """#8113: platform tools run on the chat data plane; the façade/AgentCore path refuses them by name, not as 'undeployed'."""
    client, _, _, policy, budget, _ = decision_api
    policy["allowed_tools"].append("platform.my_work_items")
    policy["consented_tools"].append("platform.my_work_items")
    policy["tool_max_cost_usd"]["platform.my_work_items"] = "0.01"
    monkeypatch.setattr(routes, "gateway_catalogue", lambda: {"tools": [{"name": "platform.my_work_items"}, {"name": "websearch.search"}]})
    reply = client.post("/internal/v1/tools/authorize", json={"schema_version": "1.0", "tool": "platform___my_work_items"}, headers=HEADERS)
    assert reply.status_code == 403 and reply.json()["detail"] == "tool_chat_data_plane_only"
    budget.assert_not_awaited()
    client.app.state.tool_activity_recorder.assert_not_called()
    listing = client.post("/internal/v1/tools/authorize", json={"schema_version": "1.0", "action": "list"}, headers=HEADERS)
    assert listing.status_code == 200
    assert "platform___my_work_items" not in {tool["name"] for tool in listing.json()["tools"]}


@pytest.mark.parametrize("granted,consented,ready", [(True, True, True), (False, True, True), (True, False, True), (True, True, False)])
def test_dynamic_knowledge_metadata_refresh_never_changes_authority(decision_api, monkeypatch, granted, consented, ready):
    import copy

    client, _, read, policy, _, _ = decision_api
    bearer_identity(monkeypatch)
    stale = {"name": "knowledge___search", "description": "Stale", "inputSchema": {"type": "object", "properties": {"stale": {"type": "string"}}}}
    core = {"name": "websearch___search", "description": "Search", "inputSchema": {"type": "object"}}
    policy["catalog"] = [core, stale]
    if granted:
        policy["allowed_tools"].append("knowledge.search")
    if consented:
        policy["consented_tools"].append("knowledge.search")
    policy["tool_max_cost_usd"]["knowledge.search"] = "0.01"
    fresh = {
        "name": "knowledge.search",
        "connector": "knowledge",
        "description": "Actual native schema",
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    }
    monkeypatch.setattr(
        routes,
        "gateway_catalogue",
        lambda: {
            "tools": [{"name": "websearch.search"}, *([fresh] if ready else [])],
            "connectors": [{"name": "knowledge", "dynamic": True, "discovery_status": "ready" if ready else "unavailable"}],
        },
    )
    before = copy.deepcopy(policy)
    response = client.post(
        "/internal/v1/tools/authorize", json={"schema_version": "1.0", "action": "list"}, headers={"x-adp-user-authorization": "Bearer signed-login"}
    )
    assert response.status_code == 200
    listing = response.json()["tools"]
    assert listing[0] == core
    assert len(listing) == (2 if granted and consented and ready else 1)
    if len(listing) == 2:
        assert listing[1] == {"name": "knowledge___search", "description": fresh["description"], "inputSchema": fresh["inputSchema"]}
    assert response.json()["dynamic_catalogue_unavailable"] is (not ready)
    assert policy == before


def test_removed_knowledge_target_revokes_stale_discovery_and_call_without_engine_regression(decision_api, monkeypatch):
    client, _, _, policy, budget, _ = decision_api
    policy["allowed_tools"].append("knowledge.search")
    policy["consented_tools"].append("knowledge.search")
    policy["tool_max_cost_usd"]["knowledge.search"] = "0.01"
    core = {"name": "websearch___search", "description": "Engine", "inputSchema": {"type": "object"}}
    stale = {"name": "knowledge___search", "description": "Stale", "inputSchema": {"type": "object"}}
    policy["catalog"] = [core, stale]
    present = [True]

    def catalogue():
        tools = [{"name": "websearch.search"}]
        connectors = [{"name": "websearch", "dynamic": False}]
        if present[0]:
            tools.append({"name": "knowledge.search", "connector": "knowledge", "description": "Fresh", "inputSchema": {"type": "object"}})
            connectors.append({"name": "knowledge", "dynamic": True, "discovery_status": "ready"})
        return {"tools": tools, "connectors": connectors}

    monkeypatch.setattr(routes, "gateway_catalogue", catalogue)
    list_body = {"schema_version": "1.0", "action": "list"}
    before = client.post("/internal/v1/tools/authorize", json=list_body, headers=HEADERS)
    assert before.status_code == 200
    assert [tool["name"] for tool in before.json()["tools"]] == ["websearch___search", "knowledge___search"]

    present[0] = False
    after = client.post("/internal/v1/tools/authorize", json=list_body, headers=HEADERS)
    assert after.status_code == 200
    assert after.json()["tools"] == [core]
    budget.reset_mock()
    denied = client.post("/internal/v1/tools/authorize", json={"schema_version": "1.0", "tool": "knowledge___search"}, headers=HEADERS)
    assert denied.status_code == 403 and denied.json()["detail"] == "tool_undeployed"
    budget.assert_not_awaited()
    engine = client.post("/internal/v1/tools/authorize", json=BODY, headers=HEADERS)
    assert engine.status_code == 200
    budget.assert_awaited_once()
    assert policy["allowed_tools"] == policy["consented_tools"] == ["websearch.search", "knowledge.search"]


def test_credential_schema_replaces_stale_catalogue_only_when_granted_and_consented(decision_api, monkeypatch):
    client, _, _, policy, _, _ = decision_api
    bearer_identity(monkeypatch)
    name = "credentials.list"
    stale = {
        "name": "credentials___list",
        "description": "Old selector",
        "inputSchema": {"type": "object", "properties": {"user_id": {"type": "string"}}},
    }
    fresh = {
        "name": name,
        "connector": "credentials",
        "description": "List metadata",
        "inputSchema": {"type": "object", "properties": {"service": {"type": "string"}}},
    }
    policy["catalog"] = [stale]
    policy["allowed_tools"].append(name)
    policy["consented_tools"].append(name)
    policy["tool_max_cost_usd"][name] = "0.01"
    present = [True]
    monkeypatch.setattr(routes, "gateway_catalogue", lambda: {"tools": [fresh] if present[0] else [], "connectors": []})
    headers = {"x-adp-user-authorization": "Bearer signed-login"}
    listed = client.post("/internal/v1/tools/authorize", json={"schema_version": "1.0", "action": "list"}, headers=headers)
    assert listed.status_code == 200
    assert listed.json()["tools"] == [{"name": "credentials___list", "description": "List metadata", "inputSchema": fresh["inputSchema"]}]
    present[0] = False
    removed = client.post("/internal/v1/tools/authorize", json={"schema_version": "1.0", "action": "list"}, headers=headers)
    assert removed.status_code == 200 and removed.json()["tools"] == []


@pytest.mark.parametrize("operation", ["assume_role", "materialize"])
@pytest.mark.parametrize("native_enabled", [False, True])
def test_credential_private_operations_never_execute_through_generic_authorization(decision_api, monkeypatch, operation, native_enabled):
    client, _, _, policy, budget, _ = decision_api
    bearer_identity(monkeypatch)
    permission = f"credentials.{operation}"
    policy["allowed_tools"].append(permission)
    policy["consented_tools"].append(permission)
    policy["tool_max_cost_usd"][permission] = "0.01"
    fresh = {"name": permission, "connector": "credentials", "description": "Private delivery", "inputSchema": {"type": "object"}}
    if native_enabled:
        from src.auth.native_credential_authority import NativeConfig
        from src.internal import tools_catalogue

        monkeypatch.setenv("ADP_NATIVE_CREDENTIALS_ENABLED", "true")
        monkeypatch.delenv("ADP_TOOLS_GATEWAY_ROLE_ARN", raising=False)
        monkeypatch.setattr(NativeConfig, "configured", lambda: object())
        catalogue = tools_catalogue.tool_catalogue()
        fresh = next(tool for tool in catalogue["tools"] if tool["name"] == permission)
        assert fresh["native_private_delivery"] is True
    monkeypatch.setattr(routes, "gateway_catalogue", lambda: {"tools": [fresh], "connectors": []})
    headers = {"x-adp-user-authorization": "Bearer signed-login"}
    listed = client.post("/internal/v1/tools/authorize", json={"schema_version": "1.0", "action": "list"}, headers=headers)
    assert listed.status_code == 200 and listed.json()["tools"] == []
    budget.reset_mock()
    denied = client.post("/internal/v1/tools/authorize", json={"schema_version": "1.0", "tool": f"credentials___{operation}"}, headers=headers)
    assert denied.status_code == 403 and denied.json()["detail"] == "private_delivery_unavailable"
    budget.assert_not_awaited()


@pytest.mark.parametrize("fault", ("wrong_user", "missing_scope", "withdrawn_oauth_consent", "withdrawn_tool_consent"))
def test_github_mediator_denies_foreign_owner_or_missing_authority_before_budget(decision_api, monkeypatch, fault):
    from src.internal import github_oauth

    client, _, _, policy, budget, db = decision_api
    policy["allowed_tools"].append("github.get_user")
    if fault != "withdrawn_tool_consent":
        policy["consented_tools"].append("github.get_user")
    policy["tool_max_cost_usd"]["github.get_user"] = "0.05"
    monkeypatch.setenv("ADP_TOOLS_GITHUB_MEDIATOR_ENABLED", "true")
    monkeypatch.setattr(routes, "get_settings", lambda: SimpleNamespace(oauth_workload_name="adp-tools-dev-oauth-user"))
    context = SimpleNamespace(account_type="human", auth_source="jwt", org_id="tenant-one", user_id=HUMAN)
    user = SimpleNamespace(id=HUMAN, team_id="team-one", user_kind="human", is_shadow=False)
    monkeypatch.setattr(routes, "authenticate_human", AsyncMock(return_value=(context, user, "team-one")))
    credential = SimpleNamespace(
        id="owned-id",
        org_id="tenant-one",
        user_id="user-bob" if fault == "wrong_user" else HUMAN,
        agent_consent=fault != "withdrawn_oauth_consent",
        scopes={"oauth_scopes": ["repo"] if fault == "missing_scope" else ["read:user"]},
    )

    async def selected_connection(db, *, user_id, tenant_id):
        if (
            credential.user_id != user_id
            or tenant_id != credential.org_id
            or credential.agent_consent is not True
            or credential.scopes != {"oauth_scopes": ["read:user"]}
        ):
            raise HTTPException(403, "GitHub connection unqualified")
        return credential, object(), {"connection_id": credential.id, "account_id": "213"}

    selected = AsyncMock(side_effect=selected_connection)
    monkeypatch.setattr(github_oauth, "selected_github_connection", selected)
    reply = client.post(
        "/internal/v1/tools/authorize",
        json={"schema_version": "1.0", "tool": "github___get_user"},
        headers={"x-adp-user-authorization": "Bearer synthetic-owner-jwt"},
    )
    assert reply.status_code == 403
    assert reply.json()["detail"] == ("tool_not_consented" if fault == "withdrawn_tool_consent" else "GitHub connection unqualified")
    selected.assert_awaited_once_with(db, user_id=HUMAN, tenant_id="tenant-one")
    db.scalar.assert_not_awaited()
    budget.assert_not_awaited()
    client.app.state.tool_activity_recorder.assert_not_called()


def test_github_disconnected_oauth_never_falls_back_to_present_legacy_pat(decision_api, monkeypatch):
    from src.internal import github_oauth

    client, _, _, policy, budget, db = decision_api
    policy["allowed_tools"].append("github.get_user")
    policy["consented_tools"].append("github.get_user")
    policy["tool_max_cost_usd"]["github.get_user"] = "0.05"
    monkeypatch.setenv("ADP_TOOLS_GITHUB_MEDIATOR_ENABLED", "true")
    context = SimpleNamespace(account_type="human", auth_source="jwt", org_id="tenant-one", user_id=HUMAN)
    user = SimpleNamespace(id=HUMAN, team_id="team-one", user_kind="human", is_shadow=False)
    monkeypatch.setattr(routes, "authenticate_human", AsyncMock(return_value=(context, user, "team-one")))
    disconnected = AsyncMock(side_effect=HTTPException(403, "GitHub connection unqualified"))
    monkeypatch.setattr(github_oauth, "selected_github_connection", disconnected)
    db.scalar.return_value = SimpleNamespace(
        id="legacy-pat",
        org_id="tenant-one",
        user_id=HUMAN,
        service="github",
        label="github-pat",
        credential_type="bearer",
        agent_consent=True,
    )
    reply = client.post(
        "/internal/v1/tools/authorize",
        json={"schema_version": "1.0", "tool": "github___get_user"},
        headers={"x-adp-user-authorization": "Bearer synthetic-owner-jwt"},
    )
    assert reply.status_code == 403 and reply.json()["detail"] == "GitHub connection unqualified"
    disconnected.assert_awaited_once_with(db, user_id=HUMAN, tenant_id="tenant-one")
    db.scalar.assert_not_awaited()
    budget.assert_not_awaited()
    client.app.state.tool_activity_recorder.assert_not_called()


@pytest.mark.parametrize("fault", ("other_site", "denied_issue", "missing_scope", "missing_consent", "disconnected"))
def test_jira_unqualified_connection_cannot_reach_direct_target(decision_api, monkeypatch, fault):
    client, _, _, policy, budget, db = decision_api
    policy["allowed_tools"].append("jira.get_issue")
    policy["consented_tools"].append("jira.get_issue")
    policy["tool_max_cost_usd"]["jira.get_issue"] = "0.05"
    monkeypatch.setattr(routes, "get_settings", lambda: SimpleNamespace(oauth_workload_name="adp-oauth-user"))
    context = SimpleNamespace(account_type="human", auth_source="jwt", org_id="tenant-one", user_id=HUMAN)
    owner = SimpleNamespace(id=HUMAN, team_id="team-one", user_kind="human", is_shadow=False)
    monkeypatch.setattr(routes, "authenticate_human", AsyncMock(return_value=(context, owner, "team-one")))
    db.scalar.return_value = (
        SimpleNamespace(
            id="owned-jira-pointer",
            user_id=HUMAN,
            org_id="tenant-one",
            agent_consent=fault != "missing_consent",
            scopes={"oauth_scopes": ["read:jira-work"] if fault != "missing_scope" else []},
            secret_arn="agentcore-identity:adp-oauth-user:atlassian",
            expires_at=None,
        )
        if fault != "disconnected"
        else None
    )
    reply = client.post(
        "/internal/v1/tools/authorize",
        json={
            "schema_version": "1.0",
            "tool": "jira___get_issue",
            "arguments": {
                "cloudId": "00000000-0000-4000-8000-000000000002" if fault == "other_site" else "00000000-0000-4000-8000-000000000001",
                "issueIdOrKey": "99999" if fault == "denied_issue" else "10001",
            },
        },
        headers={"x-adp-user-authorization": "Bearer synthetic-owner-jwt"},
    )
    assert reply.status_code == 403 and reply.json()["detail"] == "jira_connection_unqualified"
    db.scalar.assert_not_awaited()
    budget.assert_not_awaited()
    client.app.state.tool_activity_recorder.assert_not_called()


def test_jira_unqualified_direct_target_is_not_discoverable(decision_api, monkeypatch):
    client, _, _, policy, budget, db = decision_api
    policy["allowed_tools"].append("jira.get_issue")
    policy["consented_tools"].append("jira.get_issue")
    policy["tool_max_cost_usd"]["jira.get_issue"] = "0.05"
    policy["catalog"] = [
        {
            "name": "jira___get_issue",
            "description": "Synthetic Jira issue read",
            "inputSchema": {"type": "object", "properties": {}},
        }
    ]
    context = SimpleNamespace(account_type="human", auth_source="jwt", org_id="tenant-one", user_id=HUMAN)
    owner = SimpleNamespace(id=HUMAN, team_id="team-one", user_kind="human", is_shadow=False)
    monkeypatch.setattr(routes, "authenticate_human", AsyncMock(return_value=(context, owner, "team-one")))
    reply = client.post(
        "/internal/v1/tools/authorize",
        json={"schema_version": "1.0", "action": "list"},
        headers={"x-adp-user-authorization": "Bearer synthetic-owner-jwt"},
    )
    assert reply.status_code == 200
    assert not any(entry["name"] == "jira___get_issue" for entry in reply.json()["tools"])
    db.scalar.assert_not_awaited()
    budget.assert_not_awaited()


@pytest.mark.parametrize("fault", [None, "ceiling", "oversized", "missing_request", "run", "budget_denied"])
def test_memory_reserves_bounded_estimate_only_for_valid_human_call(decision_api, monkeypatch, fault):
    client, _, _, policy, budget, _ = decision_api
    client.app.state.native_test_runtime.store.document = Mock(return_value=None)
    policy["allowed_tools"].append("knowledge.experience")
    policy["consented_tools"].append("knowledge.experience")
    policy["tool_max_cost_usd"]["knowledge.experience"] = "0.00001" if fault == "ceiling" else "1.00"
    monkeypatch.setattr(routes, "gateway_catalogue", lambda: {"tools": [{"name": "knowledge.experience"}]})
    context = SimpleNamespace(account_type="human", auth_source="jwt", org_id="tenant-one", user_id=HUMAN)
    user = SimpleNamespace(id=HUMAN, team_id="team-one", user_kind="human", is_shadow=False)
    monkeypatch.setattr(routes, "authenticate_human", AsyncMock(return_value=(context, user, "team-one")))
    original_receipt = str(uuid4())
    admission = SimpleNamespace(receipt_id=original_receipt, claim=Mock(return_value=True), finish=Mock())
    monkeypatch.setattr(routes, "ToolCallAdmission", lambda **kwargs: admission)
    if fault == "budget_denied":
        budget.return_value = SimpleNamespace(allowed=False, deny_reason=DenyReason.BUDGET_EXCEEDED)
    body = {
        "schema_version": "1.0",
        "tool": "knowledge___experience",
        "request_id": str(uuid4()),
        "arguments": {"action": "save", "persona": "developer", "content": "x" * (8193 if fault == "oversized" else 20)},
    }
    if fault == "missing_request":
        del body["request_id"]
    response = client.post(
        "/internal/v1/tools/authorize", json=body, headers=HEADERS if fault == "run" else {"x-adp-user-authorization": "Bearer synthetic-owner-jwt"}
    )
    if fault is None:
        assert response.status_code == 200, response.text
        assert response.json()["receipt_id"] == original_receipt
        budget.assert_awaited_once()
        assert budget.await_args.args[1] == Decimal("0.01")
        assert budget.await_args.kwargs == {"request_id": original_receipt, "reserve": True, "tool_estimate": True}
        admission.finish.assert_called_once()
    elif fault == "budget_denied":
        assert response.status_code == 429
        admission.finish.assert_not_called()
    else:
        assert response.status_code in {403, 422}
        budget.assert_not_awaited()
        admission.claim.assert_not_called()


def test_default_posture_allows_an_entitled_tool_without_a_consent_list(decision_api):
    """#8387 AC-01: tenant entitlement is the consent; the receipt says so."""
    client, _, _, policy, budget, _ = decision_api
    policy["consented_tools"] = []
    policy["allowed_tools"] = []
    policy["tool_max_cost_usd"] = {}
    routes.entitled_tool_names.return_value = {"websearch.search": Decimal("0.20")}
    reply = client.post("/internal/v1/tools/authorize", json=BODY, headers=HEADERS)
    assert reply.status_code == 200, reply.text
    assert reply.json()["allowed"] is True
    assert client.app.state.tool_activity_recorder.call_args.kwargs["consent_source"] == "default"
    assert client.app.state.tool_activity_recorder.call_args.kwargs["estimated_cost_usd"] == Decimal("0.20")  # entitlement ceiling
    routes.resolve_tenant_consent_default.assert_awaited()


def test_default_posture_without_any_policy_row_still_decides(decision_api):
    client, _, read, _, _, _ = decision_api
    read.return_value = None
    routes.entitled_tool_names.return_value = {"websearch.search": Decimal("0.20")}
    assert client.post("/internal/v1/tools/authorize", json=BODY, headers=HEADERS).status_code == 200
    routes.entitled_tool_names.return_value = {}
    # Nothing entitled and no admin row: the tool is not even allowed.
    assert client.post("/internal/v1/tools/authorize", json=BODY, headers=HEADERS).json()["detail"] == "tool_not_allowed"


def test_opted_out_tool_is_refused_under_the_default_posture(decision_api):
    """#8387 AC-02."""
    client, _, _, policy, budget, _ = decision_api
    policy.update({"consented_tools": [], "consent_mode": "default", "opted_out_tools": ["websearch.search"]})
    routes.entitled_tool_names.return_value = {"websearch.search": Decimal("0.20")}
    reply = client.post("/internal/v1/tools/authorize", json=BODY, headers=HEADERS)
    assert reply.status_code == 403 and reply.json()["detail"] == "tool_not_consented"
    budget.assert_not_awaited()


def test_explicit_tenant_posture_keeps_opt_in(decision_api):
    """#8387 AC-04: a default-mode row in an explicit tenant is refused until the owner consents."""
    client, _, read, policy, budget, _ = decision_api
    policy["consented_tools"] = []
    routes.resolve_tenant_consent_default.return_value = "explicit"
    routes.entitled_tool_names.return_value = {"websearch.search": Decimal("0.20")}
    reply = client.post("/internal/v1/tools/authorize", json=BODY, headers=HEADERS)
    assert reply.status_code == 403 and reply.json()["detail"] == "tool_not_consented"
    routes.entitled_tool_names.assert_not_awaited()
    read.return_value = None
    assert client.post("/internal/v1/tools/authorize", json=BODY, headers=HEADERS).status_code == 403


def test_listing_under_the_default_posture_describes_entitled_tools_from_the_deployed_catalogue(decision_api, monkeypatch):
    client, _, _, policy, _, _ = decision_api
    policy.update({"allowed_tools": [], "consented_tools": [], "tool_max_cost_usd": {}, "catalog": []})
    monkeypatch.setattr(
        routes,
        "gateway_catalogue",
        lambda: {"tools": [{"name": "websearch.search", "description": "Search", "inputSchema": {"type": "object"}, "connector": "websearch"}]},
    )
    routes.entitled_tool_names.return_value = {"websearch.search": Decimal("0.20")}
    listing = client.post("/internal/v1/tools/authorize", json={"schema_version": "1.0", "action": "list"}, headers=HEADERS)
    assert listing.status_code == 200, listing.text
    assert [tool["name"] for tool in listing.json()["tools"]] == ["websearch___search"]
    policy.update({"consent_mode": "default", "opted_out_tools": ["websearch.search"]})
    assert client.post("/internal/v1/tools/authorize", json={"schema_version": "1.0", "action": "list"}, headers=HEADERS).json()["tools"] == []


def test_atlassian_named_reads_require_current_owned_site_for_list_and_call(decision_api, monkeypatch):
    from src.internal import tool_adapter_authority as adapter_authority

    client, _, _, policy, _, _ = decision_api
    aliases = ("search_jira", "get_jira_issue", "search_confluence", "get_confluence_page")
    names = {"atlassian___" + alias for alias in aliases}
    for alias in aliases:
        permission = "atlassian." + alias
        policy["allowed_tools"].append(permission)
        policy["consented_tools"].append(permission)
        policy["tool_max_cost_usd"][permission] = "0.05"
    monkeypatch.setenv("ADP_TOOLS_ATLASSIAN_MEDIATOR_ENABLED", "true")
    monkeypatch.setattr(
        routes,
        "gateway_catalogue",
        lambda: {
            "tools": [
                {
                    "name": "atlassian." + alias,
                    "connector": "atlassian",
                    "description": alias,
                    "inputSchema": {"type": "object", "properties": {"cloudId": {"type": "string"}}},
                }
                for alias in aliases
            ],
        },
    )
    connection = {"id": "owner-connection", "revision": "revision-1", "site_id": "site-one"}
    scopes = ["search:jira:agent-interface", "read:jira:agent-interface", "search:confluence:agent-interface", "read:confluence:agent-interface"]
    current = AsyncMock(return_value=(connection, scopes))
    monkeypatch.setattr(adapter_authority, "current_atlassian_read", current)
    listing = client.post("/internal/v1/tools/authorize", json={"schema_version": "1.0", "action": "list"}, headers=HEADERS)
    assert listing.status_code == 200, listing.text
    assert {entry["name"] for entry in listing.json()["tools"]} == names
    call = client.post(
        "/internal/v1/tools/authorize",
        json={
            "schema_version": "1.0",
            "tool": "atlassian___get_jira_issue",
            "arguments": {"cloudId": "site-one"},
        },
        headers=HEADERS,
    )
    assert call.status_code == 200, call.text
    assert call.json()["atlassian_connection"] == connection
    assert call.json()["connection_id"] == connection["id"]
    assert call.json()["connection_scopes"] == scopes
    current.side_effect = HTTPException(403, "connection_changed")
    denied = client.post(
        "/internal/v1/tools/authorize",
        json={
            "schema_version": "1.0",
            "tool": "atlassian___get_jira_issue",
            "arguments": {"cloudId": "other-site"},
        },
        headers=HEADERS,
    )
    assert denied.status_code == 403
    refreshed_listing = client.post("/internal/v1/tools/authorize", json={"schema_version": "1.0", "action": "list"}, headers=HEADERS)
    assert refreshed_listing.status_code == 200
    assert refreshed_listing.json()["tools"] == []


def test_jira_write_call_requires_owner_opt_in_and_emits_exact_approval(decision_api, monkeypatch):
    import hashlib

    from src.internal import tool_adapter_authority
    from src.internal.tools_dynamic_catalogue import canonical

    client, _, _, policy, budget, _ = decision_api
    client.app.state.native_test_runtime.store.document = Mock(return_value=None)
    permission = "atlassian.create_jira_issue"
    policy.update(consent_mode="default", allowed_tools=[permission], consented_tools=[], tool_max_cost_usd={permission: "0.05"}, revision=7)
    routes.entitled_tool_names.return_value = {permission: Decimal("0.05")}
    monkeypatch.setenv("ADP_TOOLS_ATLASSIAN_MEDIATOR_ENABLED", "true")
    monkeypatch.setenv("ADP_TOOLS_ATLASSIAN_WRITES_ENABLED", "true")
    monkeypatch.setattr(
        routes,
        "gateway_catalogue",
        lambda: {
            "tools": [
                {
                    "name": "atlassian.create_jira_issue",
                    "connector": "atlassian",
                    "description": "Create Jira issue",
                    "inputSchema": {"type": "object", "properties": {"cloudId": {"type": "string"}}},
                }
            ]
        },
    )
    connection = {"id": "connection-one", "revision": "revision-one", "site_id": "site-one"}
    monkeypatch.setattr(tool_adapter_authority, "current_atlassian_read", AsyncMock(return_value=(connection, ["write:jira:agent-interface"])))
    request_id = str(uuid4())
    arguments = {"cloudId": "site-one", "fields": {"summary": "Súmmary"}}
    body = {"schema_version": "1.0", "tool": "atlassian___create_jira_issue", "request_id": request_id, "arguments": arguments}
    denied = client.post("/internal/v1/tools/authorize", json=body, headers=HEADERS)
    assert denied.status_code == 403 and denied.json()["detail"] == "tool_not_consented"
    budget.assert_not_awaited()
    policy["consented_tools"] = [permission]
    admission = SimpleNamespace(receipt_id=str(uuid4()), claim=Mock(return_value=True), finish=Mock())
    monkeypatch.setattr(routes, "ToolCallAdmission", lambda **kwargs: admission)
    allowed = client.post("/internal/v1/tools/authorize", json=body, headers=HEADERS)
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["atlassian_approval"] == {
        "tool": "atlassian___create_jira_issue",
        "request_id": request_id,
        "payload_sha256": hashlib.sha256(canonical(arguments)).hexdigest(),
        "policy_revision": 7,
        "consent_source": "explicit",
    }
    assert allowed.json()["atlassian_connection"] == connection
    admission.finish.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.usefixtures("dispatch")
async def test_mixed_catalogue_preserves_owned_legacy_jira_authorization_and_read(decision_api, request, monkeypatch):
    import copy
    import hashlib
    import time

    from src.internal import jira_oauth, tool_adapter_authority, tools_jira
    from src.internal import tools_dynamic_catalogue as dynamic
    from tests.internal import test_tools_jira as legacy_fixtures
    from tests.internal.test_tools_dynamic_catalogue import GATEWAY_ID, TARGET_ID

    client, _, _, policy, budget, db = decision_api
    state = request.getfixturevalue(deployed.__name__)
    legacy = request.getfixturevalue(dispatch.__name__)
    endpoint = "https://fixture.execute-api.us-east-1.amazonaws.com/dev/internal/v1/tools/"
    jira_target = copy.deepcopy(state.target)
    jira_target.update(targetId="fixture-jira", name="jira")
    jira_target["targetConfiguration"]["mcp"]["mcpServer"]["endpoint"] = endpoint + "jira/mcp"
    static_target = copy.deepcopy(state.target)
    static_target.update(targetId="fixture-static", name="websearch")
    static_target["targetConfiguration"] = {
        "mcp": {
            "lambda": {
                "toolSchema": {
                    "inlinePayload": [
                        {"name": "search", "description": "Static search", "inputSchema": {"type": "object"}},
                    ]
                }
            }
        },
    }
    state.target["name"] = "atlassian"
    state.target["targetConfiguration"]["mcp"]["mcpServer"]["endpoint"] = endpoint + "atlassian/mcp"
    targets = {TARGET_ID: state.target, "fixture-jira": jira_target, "fixture-static": static_target}
    original_paginator = state.control.get_paginator
    monkeypatch.setattr(state.control, "get_gateway_target", lambda **kwargs: copy.deepcopy(targets[kwargs["targetId"]]))
    monkeypatch.setattr(
        state.control,
        "get_paginator",
        lambda operation: SimpleNamespace(
            paginate=lambda **kwargs: [{"items": [{"targetId": target_id, "status": "READY"} for target_id in targets]}]
        )
        if operation == "list_gateway_targets"
        else original_paginator(operation),
    )
    monkeypatch.setattr(dynamic.time, "time", lambda: time.time_ns() / 1_000_000_000)
    for namespace, target_id in (("jira", "fixture-jira"), ("atlassian", TARGET_ID)):
        prefix = "ADP_TOOLS_" + namespace.upper()
        for suffix, value in {
            "MEDIATOR_ENABLED": "true",
            "SOURCE_REVISION": "a" * 40,
            "MEDIATOR_URL": endpoint + namespace + "/mcp",
            "GATEWAY_ID": GATEWAY_ID,
            "TARGET_ID": target_id,
        }.items():
            monkeypatch.setenv(prefix + "_" + suffix, value)
    aliases = ("search_jira", "get_jira_issue", "search_confluence", "get_confluence_page")
    schema = {"type": "object", "properties": {"cloudId": {"type": "string"}}}
    atl_tools = [{"name": alias, "description": alias, "inputSchema": schema} for alias in aliases]
    monkeypatch.setenv("ADP_TOOLS_ATLASSIAN_READS_SHA256", hashlib.sha256(dynamic.canonical(atl_tools)).hexdigest())
    now = int(time.time_ns() / 1_000_000_000)
    dynamic.publish_catalogue(
        binding=dynamic.target_binding(GATEWAY_ID, jira_target, state.gateway),
        tools=tools_jira.TOOLS,
        client=state.store,
        table_name="fixture-authority",
        now=now,
    )
    monkeypatch.setattr(routes, "gateway_catalogue", tools_catalogue.gateway_catalogue)
    monkeypatch.setattr(routes, "get_settings", lambda: SimpleNamespace(oauth_workload_name="adp-tools-dev-oauth-user"))
    context = SimpleNamespace(account_type="human", auth_source="jwt", org_id="tenant-one", user_id=HUMAN)
    owner = SimpleNamespace(id=HUMAN, team_id="team-one", user_kind="human", is_shadow=False)
    monkeypatch.setattr(routes, "authenticate_human", AsyncMock(return_value=(context, owner, "team-one")))
    permission = "jira.get_issue"
    policy["allowed_tools"].append(permission)
    policy["consented_tools"].append(permission)
    policy["tool_max_cost_usd"][permission] = "0.05"
    policy["catalog"] = [{"name": "websearch___search", "description": "Static search", "inputSchema": {"type": "object"}}]
    connection = {"connection_id": "owned-connection", "revision": "revision-one", "account_id": "account-one", "resources_sha256": "a" * 64}
    credential = owned_connector(
        provider="atlassian",
        id="owned-connection",
        org_id="tenant-one",
        user_id=HUMAN,
        scopes=["read:jira-work", "read:me", "offline_access"],
    )
    selected = AsyncMock(return_value=(credential, object(), connection))
    monkeypatch.setattr(jira_oauth, "selected_jira_connection", selected)
    atlassian = AsyncMock(side_effect=HTTPException(403, "connection_changed"))
    monkeypatch.setattr(tool_adapter_authority, "current_atlassian_read", atlassian)
    headers = {"x-adp-user-authorization": "Bearer synthetic-owner-jwt"}

    def authorize_jira():
        db.scalar.side_effect = [credential]
        response = client.post(
            "/internal/v1/tools/authorize",
            json={
                "schema_version": "1.0",
                "tool": "jira___get_issue",
                "arguments": {
                    "cloudId": legacy_fixtures.SITE.cloud_id,
                    "issueIdOrKey": "EXAMPLE-1",
                },
            },
            headers=headers,
        )
        assert response.status_code == 200, response.text
        assert response.json()["jira_connection"] == connection
        assert response.json()["connection_id"] == legacy_fixtures.CLAIMS["connection_id"]
        return response.json()

    tools_catalogue.invalidate_catalogue_cache()
    try:
        initial = tools_catalogue.gateway_catalogue()
        assert [tool["name"] for tool in initial["tools"]] == ["jira.get_issue", "websearch.search"]
        original = authorize_jira()
        dynamic.publish_catalogue(
            binding=dynamic.target_binding(GATEWAY_ID, state.target, state.gateway),
            tools=atl_tools,
            client=state.store,
            table_name="fixture-authority",
            now=now,
        )
        mixed = tools_catalogue.gateway_catalogue()
        assert [tool["name"] for tool in mixed["tools"]] == sorted(
            ["jira.get_issue", "websearch.search", *("atlassian." + alias for alias in aliases)]
        )
        assert {entry["name"]: entry["discovery_status"] for entry in mixed["connectors"] if entry["dynamic"]} == {
            "atlassian": "ready",
            "jira": "ready",
        }
        denied_listing = client.post("/internal/v1/tools/authorize", json={"schema_version": "1.0", "action": "list"}, headers=headers)
        assert denied_listing.status_code == 200, denied_listing.text
        assert {tool["name"] for tool in denied_listing.json()["tools"]} == {"jira___get_issue", "websearch___search"}
        for alias in aliases:
            permission = "atlassian." + alias
            policy["allowed_tools"].append(permission)
            policy["consented_tools"].append(permission)
            policy["tool_max_cost_usd"][permission] = "0.05"
        atlassian.side_effect = None
        atlassian.return_value = (
            {"id": "atlassian-only", "revision": "revision-2", "site_id": "site-2"},
            [
                "read:jira:agent-interface",
                "search:jira:agent-interface",
                "read:confluence:agent-interface",
                "search:confluence:agent-interface",
            ],
        )
        listing = client.post("/internal/v1/tools/authorize", json={"schema_version": "1.0", "action": "list"}, headers=headers)
        assert listing.status_code == 200, listing.text
        assert {tool["name"] for tool in listing.json()["tools"]} == {
            "jira___get_issue",
            "websearch___search",
            *("atlassian___" + alias for alias in aliases),
        }
        retained = authorize_jira()
        for key in ("jira_connection", "connection_id", "connection_scopes", "allowed"):
            assert retained[key] == original[key]
        legacy_result = await tools_jira.serve(legacy_fixtures.PACKET, SimpleNamespace())
        assert json.loads(legacy_result.body)["result"]["content"] == [
            {
                "type": "text",
                "text": json.dumps(legacy[3].return_value),
            }
        ]
        assert legacy[0].await_count == legacy[3].await_count == 1
        selected.assert_awaited()
        budget.assert_awaited()
    finally:
        tools_catalogue.invalidate_catalogue_cache()


@mock_aws
def test_jira_write_report_repair_requires_reconciler_and_original_receipt(decision_api, monkeypatch):
    client = decision_api[0]
    resource = boto3.resource("dynamodb", region_name="us-east-1")
    resource.create_table(
        TableName="write-repair-test",
        BillingMode="PAY_PER_REQUEST",
        KeySchema=[{"AttributeName": "event_id", "KeyType": "HASH"}, {"AttributeName": "arrived_at", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": "event_id", "AttributeType": "S"}, {"AttributeName": "arrived_at", "AttributeType": "S"}],
    )
    activity = ActivityService(table_name="write-repair-test", dynamodb_resource=resource)
    receipt_id, request_id, execution_id = str(uuid4()), str(uuid4()), str(uuid4())
    tool = "atlassian___create_jira_issue"
    activity.record_tool_call(
        receipt_id=receipt_id,
        request_id=request_id,
        user_id=HUMAN,
        tenant_id="tenant-one",
        team_id="team-one",
        tool=tool,
        estimated_cost_usd=Decimal("0.05"),
        run_id="run-one",
    )
    monkeypatch.setenv("WEBHOOK_EVENTS_TABLE", "write-repair-test")
    monkeypatch.setattr(routes, "ActivityService", lambda **kwargs: activity)
    role = "arn:aws:iam::123456789012:role/tools-reconciler"
    monkeypatch.setenv("ADP_TOOLS_RECONCILER_ROLE_ARN", role)
    body = {
        "schema_version": "1.0",
        "receipt_id": receipt_id,
        "tenant_id": "tenant-one",
        "user_id": HUMAN,
        "team_id": "team-one",
        "run_id": "run-one",
        "tool": tool,
        "request_id": request_id,
        "execution_id": execution_id,
        "result_status": "unknown",
        "operation_id": f"OP#tenant-one#{HUMAN}#{tool}#{request_id}",
        "payload_sha256": "a" * 64,
        "backend_generation": 1,
    }
    path = "/internal/v1/tools/outcome-repair"
    assert client.post(path, json=body).status_code == 403
    monkeypatch.setattr(routes, "verified_caller_identity", lambda request, settings: "arn:aws:sts::123456789012:assumed-role/tools-reconciler/pod")
    for changed in ({"user_id": "other"}, {"receipt_id": str(uuid4())}, {"operation_id": "OP#other"}, {"request_id": str(uuid4())}):
        assert client.post(path, json={**body, **changed}).status_code in {403, 404, 422}
    assert client.post(path, json=body).json() == {"status": "recorded"}
    assert client.post(path, json={**body, "result_status": "confirmed"}).status_code == 200
    assert activity._table.scan()["Items"][0]["result_status"] == "confirmed"
    assert client.post(path, json={**body, "tool": "jira___delete_issue"}).status_code == 422
    monkeypatch.setattr(routes, "verified_caller_identity", lambda request, settings: None)
    assert client.post(path, json=body).status_code == 403


def test_signed_l5_facade_uses_verified_human_and_http_adapter(decision_api, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[4] / "modules/L5-services/src"))
    with monkeypatch.context() as isolated:
        isolated.setattr(boto3, "client", lambda *args, **kwargs: object())
        from src import l5_integration

    client, _, _, _, _, db = decision_api
    actor = SimpleNamespace(account_type="human", auth_source="jwt", org_id="tenant-one")
    member = SimpleNamespace(id=HUMAN, user_kind="human", is_shadow=False, team_id="team-one")
    monkeypatch.setattr(routes, "validate_cognito_jwt", AsyncMock(return_value=actor))
    monkeypatch.setattr(routes, "membership", AsyncMock(return_value=(member, None, None)))
    listing = AsyncMock(return_value={"tools": [{"name": "l5___get_configuration"}]})
    called = AsyncMock(
        return_value={
            "structuredContent": {"http_status": 200, "data": {"id": "opaque"}},
            "content": [{"type": "text", "text": '{"http_status":200,"data":{"id":"opaque"}}'}],
        }
    )
    monkeypatch.setattr(l5_integration, "list_l5_mcp_tools", listing)
    monkeypatch.setattr(l5_integration, "call_l5_mcp_tool", called)
    headers = {"x-adp-user-authorization": "Bearer verified-login"}
    response = client.post("/internal/v1/tools/l5-list", headers=headers)
    assert response.status_code == 200
    assert response.json() == {"tools": [{"name": "l5___get_configuration"}], "root_human_id": HUMAN}
    arguments = {"product_id": "product-one", "project_id": "project-one", "service_id": "security"}
    response = client.post(
        "/internal/v1/tools/l5-call",
        headers=headers,
        json={
            "tool": "l5___get_configuration",
            "arguments": arguments,
        },
    )
    assert response.status_code == 200
    assert response.json() == called.return_value
    assert called.await_args.args[0].arguments == arguments
    assert len(called.await_args.args) == 1
    assert called.await_args.kwargs["request"].url.path == "/internal/v1/tools/l5-call"
    assert called.await_args.kwargs["actor"] is actor
    assert called.await_args.kwargs["db"] is db
    assert listing.await_args.args == (actor,)
    refused = client.post(
        "/internal/v1/tools/l5-call",
        json={
            "tool": "l5___get_configuration",
            "arguments": arguments,
        },
    )
    assert refused.status_code == 403
    assert called.await_count == 1
