"""Real tool route and consistent registry reads, before policy/rate/budget."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import boto3
import httpx
import pytest
from fastapi.testclient import TestClient
from moto import mock_aws

from src.agentauth.native_test_enrollment import CAPABILITY_HEADER, OPERATION_HEADER, NativeTestStore
from src.internal import tools_authorize as routes
from tests.internal.test_tools_authorize import BODY, HEADERS, HUMAN, bearer_identity, decision_api

__all__ = ["decision_api"]


@pytest.fixture
def native_tools(decision_api, monkeypatch):
    client, runtime, policy_read, policy, budget, db = decision_api
    with mock_aws():
        dynamo = boto3.client("dynamodb", region_name="us-east-1")
        dynamo.create_table(
            TableName="native-tools-registry",
            BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}, {"AttributeName": "sk", "AttributeType": "S"}],
        )
        store = NativeTestStore(table_name="native-tools-registry", client=dynamo)
        client.app.state.native_test_runtime.store = store
        rate = AsyncMock(return_value=SimpleNamespace(allowed=True))
        monkeypatch.setattr(routes, "get_rate_limit_service", lambda request: SimpleNamespace(consume_tool_rate_limit=rate))

        def designate(subject="login-subject", state="active"):
            document = dict(
                subject_id=subject,
                tenant_id="tenant-one",
                user_id=HUMAN,
                team_id="team-one",
                department_id="dept-one",
                owner_id="owner",
                deployment_id="offline",
                revision=str(uuid4()),
                revocation_epoch=0,
                state=state,
            )
            dynamo.put_item(TableName=store.table, Item=store.item(store.fixture_key(subject), "native_test_fixture_v1", document))

        yield SimpleNamespace(
            client=client, store=store, dynamo=dynamo, designate=designate, policy=policy, policy_read=policy_read, budget=budget, rate=rate, db=db
        )


def body(action):
    return BODY if action == "call" else {"schema_version": "1.0", "action": action, **({"tool": BODY["tool"]} if action != "list" else {})}


@pytest.mark.parametrize("action", ["call", "list", "recover", "close"])
@pytest.mark.parametrize("identity", ["bearer", "workload", "crew"])
@pytest.mark.parametrize("state", ["active", "revoked"])
def test_designated_identity_without_headers_refuses_every_tool_action(native_tools, monkeypatch, action, identity, state):
    f = native_tools
    f.designate(state=state)
    headers = HEADERS
    if identity == "bearer":
        bearer_identity(monkeypatch)
        headers = {"x-adp-user-authorization": "Bearer signed-login"}
    elif identity == "crew":
        monkeypatch.setattr(
            routes,
            "authenticate_crew",
            AsyncMock(
                return_value=(
                    SimpleNamespace(tenant_id="tenant-one", user_id=HUMAN, sandbox_uid="pod-one", expires_at=4102444800),
                    SimpleNamespace(run_id="run-one"),
                )
            ),
        )
        headers = {"x-adp-chat-capability": "verified-offline"}
    reply = f.client.post("/internal/v1/tools/authorize", json=body(action), headers=headers)
    assert reply.status_code == 403
    assert reply.json()["detail"] == "native_test_all_in_tool_dispatch_unavailable"
    assert reply.headers["cache-control"] == "no-store"
    f.policy_read.assert_not_called()
    f.budget.assert_not_awaited()
    f.rate.assert_not_awaited()


@pytest.mark.parametrize("action", ["call", "list", "recover", "close"])
def test_consistent_negative_reads_preserve_ordinary_tool_paths(native_tools, action):
    f = native_tools
    read = Mock(wraps=f.dynamo.get_item)
    f.dynamo.get_item = read
    reply = f.client.post("/internal/v1/tools/authorize", json=body(action), headers=HEADERS)
    assert reply.status_code == 200, reply.text
    assert len(read.call_args_list) == (6 if action == "call" else 4)
    assert all(call.kwargs["ConsistentRead"] is True for call in read.call_args_list)
    assert {call.kwargs["Key"]["pk"]["S"] for call in read.call_args_list} == {
        "TOOLS_TEST_SUBJECT#login-subject",
        "TOOLS_TEST_SUBJECT#" + HUMAN,
    }


@pytest.mark.parametrize("fault", ["missing_runtime", "missing_store", "read_error", "subject_missing", "forged_capability", "operation_only"])
def test_missing_registry_subject_or_unbound_native_headers_refuse(native_tools, fault):
    f = native_tools
    headers = dict(HEADERS)
    if fault == "missing_runtime":
        del f.client.app.state.native_test_runtime
    elif fault == "missing_store":
        f.client.app.state.native_test_runtime.store = None
    elif fault == "read_error":
        f.dynamo.get_item = Mock(side_effect=OSError("offline"))
    elif fault == "subject_missing":
        f.db.scalar.return_value.cognito_sub = None
    else:
        headers[CAPABILITY_HEADER if fault == "forged_capability" else OPERATION_HEADER] = "forged"
    assert f.client.post("/internal/v1/tools/authorize", json=BODY, headers=headers).status_code == 403
    f.policy_read.assert_not_called()
    f.budget.assert_not_awaited()


def test_raw_verified_jwt_subject_cannot_be_erased_by_alias(native_tools, monkeypatch):
    f = native_tools
    f.designate(subject="original-subject")
    context, _, _ = bearer_identity(monkeypatch)
    context._authenticated_subject_id = "original-subject"
    response = f.client.post("/internal/v1/tools/authorize", json=BODY, headers={"x-adp-user-authorization": "Bearer signed-login"})
    assert response.status_code == 403
    f.budget.assert_not_awaited()


def test_designation_created_during_authorization_cannot_receive_allow_receipt(native_tools):
    f = native_tools
    f.policy_read.side_effect = lambda **kwargs: (f.designate(), f.policy)[1]
    response = f.client.post("/internal/v1/tools/authorize", json=BODY, headers=HEADERS)
    assert response.status_code == 403
    f.client.app.state.tool_activity_recorder.assert_not_called()


def test_designation_during_activity_persistence_cannot_receive_allow_receipt(native_tools):
    f = native_tools
    f.client.app.state.tool_activity_recorder.side_effect = lambda **kwargs: f.designate()
    response = f.client.post("/internal/v1/tools/authorize", json=BODY, headers=HEADERS)
    assert response.status_code == 403
    assert response.json()["detail"] == "native_test_all_in_tool_dispatch_unavailable"
    f.client.app.state.tool_activity_recorder.assert_called_once()


@pytest.mark.parametrize("native_headers", [False, True])
def test_actual_facade_http_authorizer_cannot_forward_designated_paid_call(native_tools, monkeypatch, native_headers):
    f = native_tools
    f.designate()
    bearer_identity(monkeypatch)
    tools_root = Path(__file__).resolve().parents[3] / "tools"
    monkeypatch.syspath_prepend(str(tools_root))
    monkeypatch.syspath_prepend(str(tools_root / "facade"))
    from tools_facade import server

    offline_httpx = SimpleNamespace(**vars(httpx))
    gateway_responses = []

    class GatewayClient(httpx.AsyncClient):
        async def post(self, *args, **kwargs):
            response = await super().post(*args, **kwargs)
            gateway_responses.append((response.status_code, response.json()))
            return response

    offline_httpx.AsyncClient = lambda **kwargs: GatewayClient(transport=httpx.ASGITransport(app=f.client.app), **kwargs)
    monkeypatch.setattr(server, "httpx", offline_httpx)
    monkeypatch.setenv("ADP_TOOLS_INTERNAL_AUTH_URL", "https://offline.invalid/internal/v1/tools/authorize")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setattr(
        server, "signed_headers", lambda url, body, service, region, extra_headers: {"Content-Type": "application/json", **extra_headers}
    )
    # Keep the concrete provider boundary: native admission rejects a substituted
    # provider before the HTTP authorizer. Its actual network method stays inert.
    provider = server.AgentCoreClient()
    monkeypatch.setattr(provider, "invoke", AsyncMock())
    facade = TestClient(server.create_app(agentcore=provider))
    headers = {"authorization": "Bearer signed-login"}
    request_id = str(uuid4())
    if native_headers:
        headers.update({CAPABILITY_HEADER: "model-only-capability", OPERATION_HEADER: request_id})
    response = facade.post(
        "/agent/tools/call", json={"tool": BODY["tool"], "arguments": BODY["arguments"], "request_id": request_id}, headers=headers
    )
    assert response.status_code == 403, (response.text, gateway_responses)
    assert gateway_responses == [(403, {"detail": "native_test_all_in_tool_dispatch_unavailable"})]
    provider.invoke.assert_not_awaited()
    f.policy_read.assert_not_called()
    f.budget.assert_not_awaited()
