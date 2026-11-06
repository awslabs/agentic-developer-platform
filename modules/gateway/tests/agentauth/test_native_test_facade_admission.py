"""Real issuer/store/budget HTTP transfer; provider transport stays offline.

The fixture substitutes lifecycle readiness explicitly. Passing these tests is
not evidence of a complete production cleanup/settlement composition.
"""

import asyncio
import gzip
import hashlib
import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from botocore.exceptions import EndpointConnectionError

from src.agentauth.native_test_configuration import CONFIGURATION_KIND, deployment_key
from src.agentauth.native_test_effect_contract import ProtectedEffectQuoteAdapter
from src.agentauth.native_test_facade_admission import NativeFacadeAdmission
from src.agentauth.native_test_facade_contract import NativeFacadeCall
from src.agentauth.native_test_runtime import NativeTestRuntime
from src.agentauth.test_run_admission_store import AdmissionRefusedError, canonical, digest
from tests.agentauth import test_native_test_all_in_issuer as issuer_tests
from tests.agentauth import test_native_test_enrollment as old
from tests.migrations.conftest_postgres import pg_server, pg_url  # noqa: F401

admission, sessions, native, all_in = old.admission, old.sessions, old.native, issuer_tests.all_in


@pytest.fixture(autouse=True)
def facade_tool(monkeypatch, request):
    monkeypatch.setattr(old, "TOOL", getattr(request, "param", "websearch.search"))


@pytest.fixture
async def facade(all_in, monkeypatch, request):
    e, f = all_in, all_in.f
    call = {
        "schema_version": "1.0",
        "action": "call",
        "tool": old.TOOL.replace(".", "___"),
        "arguments": {"query": "fixed approved snowman ☃"} if old.TOOL.endswith(".search") else {"url": "https://example.test"},
        "execution_id": None,
        "request_id": str(uuid4()),
        "ttl_seconds": None,
    }
    if old.TOOL.startswith("atlassian."):
        call["arguments"] = {"cloudId": "site-one", "jql": "fixed approved snowman ☃"}
    if getattr(request, "param", None) == "owned":
        call["execution_id"] = str(uuid4())
    body = canonical(call).encode()
    e.effect_doc.update(
        request_sha256=NativeFacadeCall.parse(body).approved_digest(),
        request_binding="facade_call_v1",
        max_call_bytes=65536,
        max_response_bytes=262144,
        transport_attempts=1,
        claim_attempts=1,
    )
    effect = f.request.effects[1].model_copy(update={"contract_sha256": digest(e.effect_doc)})
    f.request = f.request.model_copy(update={"effects": [f.request.effects[0], effect]})
    e.configuration = e.configuration.model_copy(update={"effects": f.request.effects})
    if getattr(request, "param", None) == "smoke":
        from boto3.dynamodb.types import TypeSerializer

        listing_body = canonical(
            {"schema_version": "1.0", "action": "list", "tool": None, "arguments": {}, "request_id": None, "execution_id": None, "ttl_seconds": None}
        ).encode()
        listing_document = {
            **e.effect_doc,
            "effect_key": "native-effect:offline.list",
            "request_sha256": NativeFacadeCall.parse(listing_body).approved_digest(),
            "components": [{**term, "usd_per_unit": "1"} for term in e.effect_doc["components"]],
        }
        listing = effect.model_copy(
            update={"effect_key": "native-effect:offline.list", "action": "list", "contract_id": "list", "contract_sha256": digest(listing_document)}
        )
        f.request = f.request.model_copy(update={"effects": [*f.request.effects, listing]})
        e.configuration = e.configuration.model_copy(update={"effects": f.request.effects})
        listing_key = {**e.effect_source.key, "sk": {"S": "EFFECT_CONTRACT#list"}}
        f.fixture.client.put_item(TableName=f.store.table, Item=f.store.item(listing_key, "native_test_effect_contract_v1", listing_document))
        f.policy["catalog"] = [{"name": "websearch___search", "description": "Offline search", "inputSchema": {"type": "object"}}]
        f.fixture.client.put_item(
            TableName=f.store.table, Item={**f.policy_key, **{key: TypeSerializer().serialize(value) for key, value in f.policy.items()}}
        )
    f.fixture.client.put_item(TableName=f.store.table, Item=f.store.item(e.effect_source.key, "native_test_effect_contract_v1", e.effect_doc))
    f.fixture.client.put_item(
        TableName=f.store.table, Item=f.store.item(deployment_key("offline-gateway"), CONFIGURATION_KIND, e.configuration.model_dump())
    )
    approval, token = await old.enroll(f, monkeypatch)
    return SimpleNamespace(
        e=e,
        f=f,
        approval=approval,
        token=token,
        call=call,
        body=body,
        adapter=NativeFacadeAdmission(enrollment=f.issuer, budget=f.fixture.budget),
        operation=str(uuid4()),
    )


async def prepare(f, **kwargs):
    return await f.adapter.prepare(context=f.f.login, credential=f.token, operation_id=f.operation, body=f.body, **kwargs)


async def dispatch(f, offered, **kwargs):
    return await f.adapter.dispatch(
        **{
            "context": f.f.login,
            "credential": f.token,
            "operation_id": f.operation,
            "body": f.body,
            "proof": offered["native_admission"]["proof"],
            **kwargs,
        }
    )


@pytest.mark.parametrize(
    "change",
    [
        {"quoted_usd": None},
        {"reserved_usd": None},
        {"reserved_usd": "5"},
        {"quoted_usd": "21"},
    ],
)
async def test_native_quote_and_hold_required_before_facade_allow(facade, monkeypatch, change):
    from src.agentauth import native_test_facade_admission as admission_module

    original = admission_module._FacadeEffectAdmission.prepare_effect

    async def missing_price(service, **kwargs):
        prepared = await original(service, **kwargs)
        return replace(prepared, receipt=replace(prepared.receipt, **change))

    monkeypatch.setattr(admission_module._FacadeEffectAdmission, "prepare_effect", missing_price)
    with pytest.raises(AdmissionRefusedError, match="native_test_facade_price_unavailable"):
        await prepare(facade)
    assert (await facade.f.fixture.reservations.snapshot(facade.f.service._target(facade.approval.authority()))).total_usd == 8


async def test_one_shot_proof_survives_worker_boundary_without_resetting_original_meter(facade):
    f = facade
    offered = await prepare(f)
    operation = f.f.store.read_operation("tenant-one", f.approval.run_id, f.operation)
    assert offered["native_admission"]["proof"] not in canonical(operation)
    assert operation["facade_dispatch"]["proof_sha256"] == hashlib.sha256(offered["native_admission"]["proof"].encode()).hexdigest()
    assert offered["native_admission"]["quoted_usd"] == operation["quote"]["total_usd"]
    assert offered["native_admission"]["reserved_usd"] == offered["native_admission"]["quoted_usd"]
    assert offered["native_admission"]["run_cap_usd"] == f.approval.authority()["cap_usd"] == "20"
    # An independently constructed service restores only the original durable
    # quote/targets; no in-memory prepared authority crosses this boundary.
    f.adapter = NativeFacadeAdmission(enrollment=f.f.issuer, budget=f.f.fixture.budget)
    reply = await dispatch(f, offered)
    assert reply["status"] == "dispatch_started" and reply["reservation_id"] == offered["receipt_id"]
    replay = await prepare(f)
    assert replay["native_status"]["status"] == "dispatch_started" and "native_admission" not in replay
    with pytest.raises(AdmissionRefusedError):
        await dispatch(f, offered)
    assert (await f.f.fixture.reservations.snapshot(f.f.service._target(f.approval.authority()))).total_usd == 8


@pytest.mark.parametrize("drift", ["proof", "operation", "request", "source", "hold", "revoked", "expired"])
async def test_changed_dispatch_binding_never_consumes_original_intent(facade, drift):
    f = facade
    offered = await prepare(f)
    overrides = {}
    if drift == "proof":
        overrides["proof"] = "x" * 43
    elif drift == "operation":
        overrides["operation_id"] = str(uuid4())
    elif drift == "request":
        overrides["body"] = canonical({**f.call, "arguments": {"query": "unapproved"}}).encode()
    elif drift == "source":
        document = {**f.e.effect_doc, "state": "revoked"}
        f.f.fixture.client.put_item(TableName=f.f.store.table, Item=f.f.store.item(f.e.effect_source.key, "native_test_effect_contract_v1", document))
    elif drift == "hold":
        target = f.f.service._target(f.approval.authority())
        redis = await f.f.fixture.reservations._get_client()
        await redis.hdel(target.key(), offered["receipt_id"])
    elif drift == "revoked":
        await f.f.issuer.revoke(f.f.owner, run_id=f.approval.run_id)
    else:
        f.f.fixture.clock[0] += timedelta(seconds=61)
    with pytest.raises(AdmissionRefusedError):
        await dispatch(f, offered, **overrides)
    assert f.f.store.read_operation("tenant-one", f.approval.run_id, f.operation)["status"] == "admitted"


async def test_lost_dispatch_ack_consumes_without_replay_or_new_hold(facade, monkeypatch):
    f = facade
    offered = await prepare(f)
    original = f.f.fixture.client.transact_write_items

    def lost(**kwargs):
        reply = original(**kwargs)
        if any(item.get("Put", {}).get("Item", {}).get("status", {}).get("S") == "dispatch_started" for item in kwargs["TransactItems"]):
            raise EndpointConnectionError(endpoint_url="https://offline.invalid")
        return reply

    monkeypatch.setattr(f.f.fixture.client, "transact_write_items", lost)
    with pytest.raises(AdmissionRefusedError):
        await dispatch(f, offered)
    with pytest.raises(AdmissionRefusedError):
        await dispatch(f, offered)
    assert (await prepare(f))["native_status"]["status"] == "dispatch_started"
    assert (await f.f.fixture.reservations.snapshot(f.f.service._target(f.approval.authority()))).total_usd == 8


async def test_concurrent_consumers_have_exactly_one_durable_winner(facade):
    offered = await prepare(facade)
    replies = await asyncio.gather(dispatch(facade, offered), dispatch(facade, offered), return_exceptions=True)
    assert sum(isinstance(reply, dict) for reply in replies) == 1
    assert sum(isinstance(reply, AdmissionRefusedError) for reply in replies) == 1


@pytest.mark.parametrize("facade_tool", ["browser.start"], indirect=True)
async def test_second_stateful_session_never_claims_or_reserves(facade):
    f = facade
    offered = await prepare(f)
    assert offered["native_admission"]["session_deadline_at"] <= int(f.f.issuer.clock().timestamp()) + 300
    f.operation = str(uuid4())
    f.body = canonical({**f.call, "request_id": str(uuid4())}).encode()
    with pytest.raises(AdmissionRefusedError, match="claim_unavailable"):
        await prepare(f)
    assert f.f.store.read_operation("tenant-one", f.approval.run_id, f.operation) is None
    assert (await f.f.fixture.reservations.snapshot(f.f.service._target(f.approval.authority()))).total_usd == 8


@pytest.mark.parametrize("facade_tool", ["browser.start"], indirect=True)
async def test_late_call_keeps_original_run_behavior_deadlines(facade):
    f = facade
    f.f.fixture.clock[0] += timedelta(seconds=60)
    offered = await prepare(f)
    assert offered["native_admission"]["session_deadline_at"] <= f.approval.approved_at + 300
    assert offered["native_admission"]["cleanup_deadline_at"] <= f.approval.approved_at + 600


async def test_unsettled_all_in_money_survives_meter_expiry_and_generic_finalizers(facade, monkeypatch):
    from decimal import Decimal

    from src.budget.reservations import ReservationTarget
    from src.shared.schemas.budget import EnforcementResult, EntityType, PeriodType

    f = facade
    hierarchy = ReservationTarget("tenant-one", "user", f.f.login.user_id, "daily", "2026-01-01", Decimal("10"))

    async def check(_session, entity_type, _id, period_type, *_args, **_kwargs):
        return EnforcementResult(allowed=True), hierarchy if entity_type == EntityType.USER and period_type == PeriodType.DAILY else None

    monkeypatch.setattr(f.f.fixture.budget, "_check_entity_budget", check)
    offered = await prepare(f)
    operation = f.f.store.read_operation("tenant-one", f.approval.run_id, f.operation)
    targets = [ReservationTarget(**{**row, "headroom_usd": Decimal(row["headroom_usd"])}) for row in operation["targets"]]
    reservations = f.f.fixture.reservations
    redis = await reservations._get_client()
    assert all(target.retain_until_settlement for target in targets)
    assert all([await redis.ttl(target.key()) == -1 for target in targets])
    await reservations.reconcile(offered["receipt_id"], Decimal("0"), targets)
    assert not await reservations.reconcile_receipt(offered["receipt_id"], Decimal("0"), f.f.service._target(f.approval.authority()))
    assert not await reservations.observe(offered["receipt_id"], targets)
    f.f.fixture.clock[0] += timedelta(seconds=f.approval.meter_expires_at - int(f.f.fixture.clock[0].timestamp()) + 1)
    assert not await reservations.settle_expired_pending(offered["receipt_id"], f.f.service._target(f.approval.authority()))
    refused = await reservations.reserve("ordinary-five", Decimal("5"), [hierarchy])
    assert refused is not None and not refused.admitted
    allowed = await reservations.reserve("ordinary-four", Decimal("4"), [hierarchy])
    assert allowed is not None and allowed.admitted
    await reservations.reconcile("ordinary-four", Decimal("1"), [hierarchy])
    assert (await redis.hget(hierarchy.key(), offered["receipt_id"])).startswith("6:")
    assert await redis.hexists(hierarchy.key(), "retained:" + offered["receipt_id"])
    assert await redis.ttl(hierarchy.key()) == -1


async def test_exact_sdk_adapter_cannot_spend_facade_policy(facade):
    f = facade
    effect = f.approval.effects[1]
    source = ProtectedEffectQuoteAdapter(
        store=f.f.store,
        deployment_id="offline-gateway",
        contract_id=effect.contract_id,
        contract_sha256=effect.contract_sha256,
        source_sha256=effect.source_sha256,
    )
    with pytest.raises(AdmissionRefusedError):
        await source.quote(f.body, effect.effect_key, f.approval.authority(), now=f.f.issuer.clock())


@pytest.fixture
async def http_wire(facade, monkeypatch):
    import httpx
    from fastapi import FastAPI, HTTPException

    from src.agentauth import native_test_routes
    from src.agentauth.native_test_lifecycle import NativeTestLifecycle
    from src.internal import tools_authorize as routes

    tools_root = Path(__file__).resolve().parents[3] / "tools"
    monkeypatch.syspath_prepend(str(tools_root))
    monkeypatch.syspath_prepend(str(tools_root / "facade"))
    from tools_facade import native_admission as transfer
    from tools_facade import server

    f = facade
    for name, value in {
        "AWS_REGION": "us-east-1",
        "ADP_TOOLS_EXECUTION_TABLE": "offline-facade-executions",
        "ADP_TOOLS_INTERNAL_AUTH_URL": "https://gateway.internal/internal/v1/tools/authorize",
        "ADP_TOOLS_AGENTCORE_URL": "https://offline.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp",
        "ADP_TOOLS_FACADE_ROLE_ARN": "arn:aws:iam::123456789012:role/offline-facade",
        "ADP_TOOLS_ASSERTION_KEY_ARN": "offline-key",
    }.items():
        monkeypatch.setenv(name, value)
    f.f.fixture.client.create_table(
        TableName="offline-facade-executions",
        BillingMode="PAY_PER_REQUEST",
        KeySchema=[{"AttributeName": "execution_id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "execution_id", "AttributeType": "S"}],
    )
    app = FastAPI()
    app.state.native_test_runtime = NativeTestRuntime(app, f.f.store, enrollment=f.f.issuer, tool_admission=f.adapter)
    app.state.native_test_runtime.tool_lifecycle = NativeTestLifecycle(
        enrollment=f.f.issuer, budget=f.f.fixture.budget, execution_table="offline-facade-executions", execution_client=f.f.fixture.client
    )
    app.include_router(routes.router, prefix="/internal/v1")
    app.include_router(native_test_routes.router)
    app.dependency_overrides[routes.require_agent_transport] = lambda: None
    app.dependency_overrides[routes.get_agent_runtime] = lambda: None
    app.dependency_overrides[routes.get_db] = lambda: None

    async def login(token):
        if token != "Bearer offline-fixture-jwt":
            raise HTTPException(403, "unknown fixture")
        return f.f.login.model_copy()

    async def status_login(request: native_test_routes.Request):
        return await login(request.headers.get("authorization"))

    app.dependency_overrides[native_test_routes.get_cognito_user_context] = status_login

    monkeypatch.setattr(routes, "validate_cognito_jwt", login)
    monkeypatch.setattr(
        routes,
        "membership",
        AsyncMock(
            return_value=(
                SimpleNamespace(id="canonical-human", user_kind="human", is_shadow=False, team_id="team-one"),
                None,
                SimpleNamespace(id="team-one"),
            )
        ),
    )
    monkeypatch.setattr(
        routes,
        "verified_caller_identity",
        lambda request, settings: "arn:aws:sts::123456789012:assumed-role/offline-facade/pod" if request.headers.get("x-offline-facade") else None,
    )
    monkeypatch.setattr(
        routes,
        "get_rate_limit_service",
        lambda request: SimpleNamespace(consume_tool_rate_limit=AsyncMock(return_value=SimpleNamespace(allowed=True))),
    )
    monkeypatch.setattr(
        server,
        "signed_headers",
        lambda url, body, service, region, extra_headers=None: {
            "Content-Type": "application/json",
            "x-offline-facade": "verified",
            **(extra_headers or {}),
        },
    )
    monkeypatch.setattr(transfer, "time", SimpleNamespace(time=lambda: f.f.issuer.clock().timestamp()))
    state = {"provider": [], "kms": [], "gateway": [], "fault": None}
    dynamo_client = server.boto3.client

    def sdk(service, **kwargs):
        if service != "kms":
            return dynamo_client(service, **kwargs)
        assert kwargs["config"].retries == {"total_max_attempts": 1}

        def sign(**values):
            operation = f.f.store.read_operation("tenant-one", f.approval.run_id, state.get("operation_id", f.operation))
            assert operation["status"] == "dispatch_started"
            state["kms"].append(values)
            if state["fault"] == "signing_expiry":
                f.f.fixture.clock[0] += timedelta(seconds=61)
            return {"Signature": b"offline-signature"}

        return SimpleNamespace(sign=sign)

    monkeypatch.setattr(server.boto3, "client", sdk)
    gateway_transport = httpx.ASGITransport(app=app)
    original_client = httpx.AsyncClient

    class SlowResponse(httpx.AsyncByteStream):
        def __init__(self, payload):
            self.payload, self.chunks, self.closed = payload, 0, False

        async def __aiter__(self):
            if state["fault"] == "body_trickle":
                for _ in range(50):
                    self.chunks += 1
                    yield b" "
                    await asyncio.sleep(0.01)
            else:
                await asyncio.sleep(0.5)
            self.chunks += 1
            yield self.payload

        async def aclose(self):
            self.closed = True

    async def transport(request):
        if request.url.host == "gateway.internal":
            state["gateway"].append(request.url.path)
            if request.headers.get("x-adp-native-test-operation"):
                state["operation_id"] = request.headers["x-adp-native-test-operation"]
            response = await gateway_transport.handle_async_request(request)
            if request.url.path.endswith("native-dispatch") and state["fault"] == "lost_dispatch_ack":
                raise httpx.ReadTimeout("offline lost dispatch acknowledgement")
            return response
        assert request.url.host == "offline.gateway.bedrock-agentcore.us-east-1.amazonaws.com"
        packet = json.loads(request.content)
        state["provider"].append(packet)
        if state["fault"] == "headers_stall":
            await asyncio.sleep(0.5)
        reservation = f.f.store.read_operation("tenant-one", f.approval.run_id, state.get("operation_id", f.operation))["reservation_id"]
        if packet["method"] == "tools/list":
            result = {"tools": [{"name": "websearch___search", "inputSchema": {"type": "object"}}], "_meta": {"adp_receipt_id": reservation}}
            if state["fault"] == "next_cursor":
                result["nextCursor"] = "not-quoted-next-page"
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": packet["id"], "result": result})
        arguments = packet["params"]["arguments"]
        if packet["params"]["name"].startswith("knowledge___"):
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": packet["id"],
                    "result": {
                        "content": [{"type": "text", "text": "offline indexed fact"}],
                        "_meta": {"adp_receipt_id": reservation},
                    },
                },
            )
        from tools_facade.encoding import canonical as canonical_tool_result

        search_result = {"status": "completed", "results": [{"url": "https://example.test", "text": "offline fixture"}]}
        content = canonical_tool_result(search_result)
        result = {
            "jsonrpc": "2.0",
            "id": packet["id"],
            "result": {
                "structuredContent": {
                    "result": {
                        "status": "confirmed",
                        "execution_id": arguments["execution_id"],
                        "request_id": arguments["request_id"],
                        "receipt_id": reservation,
                        "result": search_result,
                        "artifact": {
                            "artifact_id": arguments["request_id"],
                            "content_type": "application/json",
                            "byte_length": len(content),
                            "content_sha256": hashlib.sha256(content).hexdigest(),
                        },
                    }
                }
            },
        }
        if state["fault"] == "gzip":
            return httpx.Response(
                200, content=gzip.compress(json.dumps(result).encode()), headers={"Content-Type": "application/json", "Content-Encoding": "gzip"}
            )
        if state["fault"] in {"body_trickle", "body_stall"}:
            state["response_stream"] = SlowResponse(json.dumps(result).encode())
            return httpx.Response(200, stream=state["response_stream"], headers={"Content-Type": "application/json"})
        return httpx.Response(200, json=result)

    monkeypatch.setattr(server.httpx, "AsyncClient", lambda **kwargs: original_client(transport=httpx.MockTransport(transport), **kwargs))
    async with original_client(transport=httpx.ASGITransport(app=server.create_app()), base_url="https://facade.internal") as client:
        yield SimpleNamespace(
            f=f,
            client=client,
            client_factory=original_client,
            state=state,
            app=app,
            headers={
                "authorization": "Bearer offline-fixture-jwt",
                "x-adp-native-test-capability": f.token,
                "x-adp-native-test-operation": f.operation,
            },
        )


@pytest.mark.parametrize("compressed", [False, True])
async def test_real_facade_to_gateway_to_provider_consumes_same_original_admission(http_wire, compressed):
    w = http_wire
    w.state["fault"] = "gzip" if compressed else None
    call = {key: value for key, value in w.f.call.items() if key not in {"schema_version", "action"}}
    response = await w.client.post("/agent/tools/call", json=call, headers=w.headers)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "confirmed"
    assert len(w.state["provider"]) == len(w.state["kms"]) == 1
    assert w.state["gateway"] == ["/internal/v1/tools/authorize", "/internal/v1/tools/native-dispatch"]
    replay = await w.client.post("/agent/tools/call", json=call, headers=w.headers)
    assert replay.status_code == 202 and replay.json()["native_status"]["replay"] is True
    assert len(w.state["provider"]) == len(w.state["kms"]) == 1
    assert "proof" not in response.text + replay.text
    assert (await w.f.f.fixture.reservations.snapshot(w.f.f.service._target(w.f.approval.authority()))).total_usd == 8


@pytest.mark.parametrize("fault", ["headers_stall", "body_stall", "body_trickle"])
async def test_native_response_total_deadline_keeps_unknown_hold_without_retry(http_wire, monkeypatch, fault):
    from tools_facade import server

    w = http_wire
    w.state["fault"] = fault
    deadlines = []

    def accelerated_timeout(seconds):
        deadlines.append(seconds)
        assert seconds == 30
        return asyncio.timeout(0.1)

    monkeypatch.setattr(server, "asyncio", SimpleNamespace(**{**vars(asyncio), "timeout": accelerated_timeout}))
    call = {key: value for key, value in w.f.call.items() if key not in {"schema_version", "action"}}
    response = await w.client.post("/agent/tools/call", json=call, headers=w.headers)
    assert response.status_code == 202, response.text
    assert response.json()["status"] == "unknown"
    assert deadlines == [30]
    assert len(w.state["provider"]) == len(w.state["kms"]) == 1
    if fault != "headers_stall":
        stream = w.state["response_stream"]
        assert stream.closed
        assert stream.chunks == 0 if fault == "body_stall" else 0 < stream.chunks < 50
    operation = w.f.f.store.read_operation("tenant-one", w.f.approval.run_id, w.f.operation)
    assert operation["status"] == "dispatch_started"
    replay = await w.client.post("/agent/tools/call", json=call, headers=w.headers)
    assert replay.status_code == 202 and replay.json()["native_status"]["replay"] is True
    assert len(w.state["provider"]) == len(w.state["kms"]) == 1
    assert (await w.f.f.fixture.reservations.snapshot(w.f.f.service._target(w.f.approval.authority()))).total_usd == 8


async def test_ordinary_provider_request_does_not_use_native_response_deadline(http_wire, monkeypatch):
    from tools_facade import server

    w = http_wire
    call = {key: value for key, value in w.f.call.items() if key not in {"schema_version", "action"}}
    assert (await w.client.post("/agent/tools/call", json=call, headers=w.headers)).status_code == 200

    def forbidden_timeout(seconds):
        raise AssertionError("Ordinary provider behavior must not use the native stream deadline")

    monkeypatch.setattr(server, "asyncio", SimpleNamespace(**{**vars(asyncio), "timeout": forbidden_timeout}))
    decision = server.Decision(
        schema_version="1.0",
        allowed=True,
        principal=server.Principal(user_id="canonical-human", root_human_id="canonical-human", tenant_id="tenant-one", team_id="team-one"),
        receipt_id=w.f.f.store.read_operation("tenant-one", w.f.approval.run_id, w.f.operation)["reservation_id"],
    )
    result = await server.AgentCoreClient()._request("tools/list", "tools/list", {}, decision)
    assert result["result"]["tools"] == [{"name": "websearch___search", "inputSchema": {"type": "object"}}]
    assert len(w.state["provider"]) == len(w.state["kms"]) == 2


@pytest.mark.parametrize("fault, signatures", [("lost_dispatch_ack", 0), ("signing_expiry", 1)])
async def test_ambiguous_or_expired_http_dispatch_never_reaches_provider(http_wire, fault, signatures):
    w = http_wire
    w.state["fault"] = fault
    call = {key: value for key, value in w.f.call.items() if key not in {"schema_version", "action"}}
    response = await w.client.post("/agent/tools/call", json=call, headers=w.headers)
    assert response.status_code in (202, 503), response.text
    assert w.state["provider"] == [] and len(w.state["kms"]) == signatures
    assert w.f.f.store.read_operation("tenant-one", w.f.approval.run_id, w.f.operation)["status"] == "dispatch_started"


async def test_http_dispatch_requires_verified_facade_and_authentic_fixture(http_wire):
    import httpx

    w = http_wire
    offer = await prepare(w.f)
    async with w.client_factory(transport=httpx.ASGITransport(app=w.app), base_url="https://gateway.internal") as client:
        reply = await client.post(
            "/internal/v1/tools/native-dispatch",
            json={"call": w.f.call, "proof": offer["native_admission"]["proof"]},
            headers={
                "x-adp-user-authorization": "Bearer offline-fixture-jwt",
                "x-adp-native-test-capability": w.f.token,
                "x-adp-native-test-operation": w.f.operation,
            },
        )
    assert reply.status_code == 403 and w.state["provider"] == []


@pytest.mark.parametrize("facade", ["owned"], indirect=True)
@pytest.mark.parametrize("binding", ["human", "other_test", "missing_native_binding"])
async def test_same_human_cannot_use_ordinary_or_another_tests_execution(http_wire, binding):
    import time

    from tools_facade.executions import ExecutionStore
    from tools_facade.server import Principal

    w = http_wire
    owner = Principal(user_id="canonical-human", root_human_id="canonical-human", tenant_id="tenant-one", team_id="team-one")
    run_id = str(uuid4()) if binding == "other_test" else w.f.approval.run_id
    workload = "native-test-" + w.f.approval.credential_id
    item = ExecutionStore._execution_item(
        w.f.call["execution_id"], "websearch___search", owner, "human" if binding == "human" else f"run:{run_id}:{workload}", int(time.time()) + 300
    )
    if binding != "missing_native_binding":
        item["native_test_run_id"] = {"S": run_id}
    w.f.f.fixture.client.put_item(TableName="offline-facade-executions", Item=item)
    response = await w.client.post(
        "/agent/tools/call", json={key: value for key, value in w.f.call.items() if key not in {"schema_version", "action"}}, headers=w.headers
    )
    assert response.status_code == 404
    assert w.state["provider"] == w.state["kms"] == []
