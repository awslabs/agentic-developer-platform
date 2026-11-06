"""Real original SQL/Redis/Dynamo admission with an offline adapter boundary."""

import asyncio
import hashlib
import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from src.agentauth.native_test_atlassian_dispatch import NativeAtlassianDispatch
from src.agentauth.native_test_atlassian_runtime import NativeAtlassianRuntime
from src.agentauth.native_test_lifecycle import NativeTestLifecycle
from src.agentauth.native_test_runtime import NativeTestRuntime
from src.agentauth.test_run_admission_store import AdmissionRefusedError, canonical
from tests.agentauth import test_native_test_facade_admission as wire
from tests.migrations.conftest_postgres import pg_server, pg_url  # noqa: F401

admission, sessions, native, all_in = wire.admission, wire.sessions, wire.native, wire.all_in
facade_tool, facade = wire.facade_tool, wire.facade
pytestmark = pytest.mark.parametrize("facade_tool", ["atlassian.search_jira", "atlassian.create_jira_issue"], indirect=True)


@pytest.fixture
async def atlassian(facade, monkeypatch):
    f = facade
    offered = await wire.prepare(f)
    await wire.dispatch(f, offered)
    runtime = NativeTestRuntime(None, f.f.store, enrollment=f.f.issuer, tool_admission=f.adapter)
    runtime.tool_lifecycle = NativeTestLifecycle(
        enrollment=f.f.issuer,
        budget=f.f.fixture.budget,
        execution_table="offline-executions",
        execution_client=f.f.fixture.client,
    )
    bridge = object.__new__(NativeAtlassianRuntime)
    bridge.runtime, bridge.enrollment, bridge.budget = runtime, f.f.issuer, f.f.fixture.budget
    bridge.binding = SimpleNamespace(deployment_id=f.approval.deployment_id, release_sha256=f.approval.effects[1].source_sha256)
    bridge.catalogue = SimpleNamespace(read=lambda approval: {"tools": [{"name": f.approval.tools[0]}]})
    monkeypatch.setattr(bridge, "coverage", lambda: "offline-coverage")
    monkeypatch.setattr(bridge, "require_ready", AsyncMock(return_value=None))
    monkeypatch.setattr(runtime, "require_enrollment", lambda: f.f.issuer)
    f.f.issuer._effect_runtime = bridge
    now = int(f.f.issuer.clock().timestamp())
    claims = {
        **{key: offered[key] for key in ("run_id", "workload_id", "run_expires_at")},
        "principal": {key: offered["principal"][key] for key in ("user_id", "tenant_id", "team_id")},
        "root_human_id": offered["principal"]["user_id"],
        "expires_at": now + 30,
        "native_atlassian": {
            "operation_id": f.operation,
            "execution_id": offered["native_admission"]["execution_id"],
            "request_id": f.call["request_id"],
            "request_sha256": offered["native_admission"]["request_sha256"],
        },
    }
    binding = {
        "receipt_id": offered["receipt_id"],
        "operation_id": str(uuid4()),
        "release": {"offline": True},
        "payload_sha256": hashlib.sha256(
            json.dumps(f.call["arguments"], sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        ).hexdigest(),
    }
    return SimpleNamespace(f=f, runtime=runtime, bridge=bridge, claims=claims, binding=binding)


async def verify(f, **changes):
    return await NativeAtlassianDispatch.verify(
        **{
            "runtime": f.runtime,
            "claims": f.claims,
            "operation": f.f.call["tool"].split("___")[1],
            "binding": f.binding,
            **changes,
        }
    )


def original(f):
    return f.f.f.store.read_operation(f.f.approval.designation.tenant_id, f.f.approval.run_id, f.f.operation)


async def test_original_admission_and_utf8_payload_hash_are_consumed_once(atlassian):
    f = atlassian
    handoff = await verify(f)
    assert "native_atlassian_dispatch" not in original(f)
    assert "snowman" not in canonical(original(f))
    handoff.claim("adapter-admission")
    recorded = original(f)
    assert recorded["native_atlassian_dispatch"]["status"] == "dispatch_started"
    assert recorded["native_atlassian_dispatch"]["admission_id"] == "adapter-admission"
    for _ in range(2):
        with pytest.raises(AdmissionRefusedError):
            await verify(f)
    with pytest.raises(AdmissionRefusedError):
        handoff.claim("another-admission")
    snapshot = await f.f.f.fixture.reservations.snapshot(f.f.f.service._target(f.f.approval.authority()))
    assert snapshot.total_usd == 8


async def test_original_receipt_read_survives_revocation_without_new_reservation(atlassian):
    from src.internal.tools_authorize import ToolsAuthorizationBody

    fixture = atlassian
    before = original(fixture)
    await fixture.f.f.issuer.revoke(fixture.f.f.owner, run_id=fixture.f.approval.run_id)
    fixture.f.f.fixture.clock[0] += timedelta(seconds=301)
    body = ToolsAuthorizationBody.model_validate({**fixture.f.call, "action": "recover"})
    result = await fixture.runtime.tool_lifecycle.authorize_atlassian_receipt(
        context=fixture.f.f.login,
        credential=fixture.f.token,
        operation_id=fixture.f.operation,
        body=body,
    )
    assert result["native_receipt_read"] == {
        "tool": body.tool,
        "request_id": str(body.request_id),
        "execution_id": before["facade_dispatch"]["execution_id"],
        "original_operation_id": fixture.f.operation,
    }
    assert result["receipt_id"] == before["reservation_id"]
    assert original(fixture) == before
    assert "native_admission" not in result and "native_cleanup" not in result


async def test_original_receipt_reads_share_the_existing_three_read_allowance(atlassian):
    from src.internal.tools_authorize import ToolsAuthorizationBody

    fixture = atlassian
    lifecycle = fixture.runtime.tool_lifecycle
    before = original(fixture)
    await lifecycle.status(context=fixture.f.f.login, credential=fixture.f.token, run_id=fixture.f.approval.run_id)
    arguments = {
        "context": fixture.f.f.login,
        "credential": fixture.f.token,
        "operation_id": fixture.f.operation,
        "body": ToolsAuthorizationBody.model_validate({**fixture.f.call, "action": "recover"}),
    }
    for _ in range(2):
        result = await lifecycle.authorize_atlassian_receipt(**arguments)
        assert result["receipt_id"] == before["reservation_id"]
    with pytest.raises(AdmissionRefusedError, match="status_reads_exhausted"):
        await lifecycle.authorize_atlassian_receipt(**arguments)
    assert original(fixture) == before


@pytest.mark.parametrize("changed", ["arguments", "request", "execution", "operation", "tool", "expired"])
async def test_original_receipt_read_refuses_substitution(atlassian, changed):
    from src.internal.tools_authorize import ToolsAuthorizationBody

    fixture = atlassian
    value = {**fixture.f.call, "action": "recover"}
    operation_id = fixture.f.operation
    if changed == "arguments":
        value["arguments"] = {**value["arguments"], "cloudId": "foreign-site"}
    elif changed in {"request", "execution"}:
        value[changed + "_id"] = str(uuid4())
    elif changed == "operation":
        operation_id = str(uuid4())
    elif changed == "tool":
        value["tool"] = "atlassian___comment_jira_issue"
    elif changed == "expired":
        fixture.f.f.fixture.clock[0] += timedelta(seconds=601)
    before = original(fixture)
    with pytest.raises(AdmissionRefusedError):
        await fixture.runtime.tool_lifecycle.authorize_atlassian_receipt(
            context=fixture.f.f.login,
            credential=fixture.f.token,
            operation_id=operation_id,
            body=ToolsAuthorizationBody.model_validate(value),
        )
    assert original(fixture) == before


@pytest.mark.parametrize(
    "drift",
    [
        "runtime",
        "principal",
        "run_id",
        "workload_id",
        "run_expires_at",
        "host_generation",
        "operation_id",
        "execution_id",
        "request_id",
        "request_sha256",
        "payload",
        "receipt",
        "tool",
        "revoked",
        "source",
        "hold",
        "cleanup",
    ],
)
async def test_substitution_and_revocation_are_refused_before_dispatch(atlassian, drift):
    f = atlassian
    changes = {}
    if drift == "runtime":
        changes["runtime"] = SimpleNamespace(**vars(f.runtime))
    elif drift == "principal":
        f.claims["principal"]["user_id"] = "foreign"
    elif drift in {"run_id", "workload_id", "run_expires_at", "host_generation"}:
        f.claims[drift] = 1 if drift.endswith("at") or drift == "host_generation" else str(uuid4())
    elif drift in {"operation_id", "execution_id", "request_id", "request_sha256"}:
        f.claims["native_atlassian"][drift] = "a" * 64 if drift.endswith("sha256") else str(uuid4())
    elif drift == "payload":
        f.binding["payload_sha256"] = "a" * 64
    elif drift == "receipt":
        f.binding["receipt_id"] = "foreign"
    elif drift == "tool":
        changes["operation"] = "other"
    elif drift == "revoked":
        await f.f.f.issuer.revoke(f.f.f.owner, run_id=f.f.approval.run_id)
    elif drift == "source":
        store = f.f.f.store
        store.client.put_item(
            TableName=store.table,
            Item=store.item(f.f.e.effect_source.key, "native_test_effect_contract_v1", {**f.f.e.effect_doc, "state": "revoked"}),
        )
    elif drift == "hold":
        target = f.f.f.service._target(f.f.approval.authority())
        await (await f.f.f.fixture.reservations._get_client()).hdel(target.key(), original(f)["reservation_id"])
    elif drift == "cleanup":
        f.f.f.store.client.put_item(TableName=f.f.f.store.table, Item=f.f.f.store._item({**original(f), "native_cleanup_attempts_used": 1}))
    with pytest.raises(AdmissionRefusedError):
        await verify(f, **changes)
    assert "native_atlassian_dispatch" not in original(f)


async def test_concurrent_adapter_claims_have_one_winner(atlassian):
    prepared = await asyncio.gather(*(verify(atlassian) for _ in range(3)))
    results = await asyncio.gather(*(asyncio.to_thread(value.claim, str(uuid4())) for value in prepared), return_exceptions=True)
    assert sum(value is None for value in results) == 1
    assert sum(isinstance(value, AdmissionRefusedError) for value in results) == 2


async def test_lost_claim_ack_is_not_permission_to_send_again(atlassian, monkeypatch):
    f = atlassian
    handoff = await verify(f)
    original_transaction = f.f.f.store.transaction

    def lost(items):
        original_transaction(items)
        raise AdmissionRefusedError("offline lost acknowledgement")

    monkeypatch.setattr(f.f.f.store, "transaction", lost)
    with pytest.raises(AdmissionRefusedError):
        handoff.claim("original-admission")
    monkeypatch.setattr(f.f.f.store, "transaction", original_transaction)
    with pytest.raises(AdmissionRefusedError):
        await verify(f)
    assert original(f)["native_atlassian_dispatch"]["status"] == "dispatch_started"


async def test_delayed_claim_leaves_original_hold_intact(atlassian):
    f = atlassian
    handoff = await verify(f)
    f.f.f.fixture.clock[0] += timedelta(seconds=31)
    with pytest.raises(AdmissionRefusedError):
        handoff.claim("expired-admission")
    assert "native_atlassian_dispatch" not in original(f)
