"""Real original journal/holds, offline lifecycle coverage and Door transport.

The fixture substitutes deployment readiness; it cannot qualify a live release.
"""

import asyncio
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from src.agentauth.native_test_knowledge_dispatch import NativeKnowledgeDispatch
from src.agentauth.native_test_knowledge_runtime import NativeKnowledgeRuntime
from src.agentauth.native_test_lifecycle import NativeTestLifecycle
from src.agentauth.native_test_runtime import NativeTestRuntime
from src.agentauth.test_run_admission_store import AdmissionRefusedError, canonical, digest
from tests.agentauth import test_native_test_facade_admission as wire
from tests.migrations.conftest_postgres import pg_server, pg_url  # noqa: F401

admission, sessions, native, all_in = wire.admission, wire.sessions, wire.native, wire.all_in
facade_tool, facade = wire.facade_tool, wire.facade
pytestmark = pytest.mark.parametrize("facade_tool", ["knowledge.search"], indirect=True)


@pytest.fixture
async def knowledge(facade, monkeypatch):
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
    binding = {"private": "offline-target"}
    bridge = object.__new__(NativeKnowledgeRuntime)
    bridge.runtime, bridge.enrollment, bridge.budget = runtime, f.f.issuer, f.f.fixture.budget
    bridge.binding = SimpleNamespace(
        deployment_id=f.approval.designation.deployment_id,
        release_sha256=f.approval.effects[1].source_sha256,
        catalogue=SimpleNamespace(target_binding_sha256=digest(binding)),
    )
    # Exact production coverage is tested separately. Original SQL membership,
    # Dynamo consent/authority/quote guards and Redis funding remain real here.
    bridge.catalogue = SimpleNamespace(read=lambda approval: {"tools": [{"name": "knowledge.search"}]})
    monkeypatch.setattr(bridge, "coverage", lambda: "offline-coverage")
    monkeypatch.setattr(bridge, "require_ready", AsyncMock(return_value=None))
    monkeypatch.setattr(runtime, "require_enrollment", lambda: f.f.issuer)
    f.f.issuer._effect_runtime = bridge
    now = int(f.f.issuer.clock().timestamp())
    claims = {
        **{key: offered[key] for key in ("run_id", "workload_id", "run_expires_at", "receipt_id")},
        "principal": {key: offered["principal"][key] for key in ("user_id", "tenant_id", "team_id")},
        "root_human_id": offered["principal"]["user_id"],
        "rpc_id": str(uuid4()),
        "expires_at": now + 30,
        "native_test": {
            "operation_id": f.operation,
            "execution_id": offered["native_admission"]["execution_id"],
            "request_id": f.call["request_id"],
            "request_sha256": offered["native_admission"]["request_sha256"],
        },
    }
    return SimpleNamespace(f=f, runtime=runtime, bridge=bridge, binding=binding, claims=claims)


async def prepare(k, **changes):
    return await NativeKnowledgeDispatch.prepare(
        **{
            "runtime": k.runtime,
            "claims": k.claims,
            "operation": "search",
            "arguments": k.f.call["arguments"],
            "binding": k.binding,
            **changes,
        }
    )


def original(k):
    return k.f.f.store.read_operation(k.f.approval.designation.tenant_id, k.f.approval.run_id, k.f.operation)


async def test_single_original_claim_records_only_hashes_and_preserves_all_holds(knowledge):
    k = knowledge
    handoff = await prepare(k)
    handoff.before_send()
    answer = {"jsonrpc": "2.0", "result": {"content": [{"type": "text", "text": "private indexed fact"}]}}
    await handoff.received(answer)
    operation = original(k)
    evidence = operation["native_knowledge_dispatch"]
    assert evidence["status"] == "response_received" and evidence["response_sha256"] == digest(answer)
    assert "private indexed fact" not in canonical(operation)
    assert k.f.call["arguments"]["query"] not in canonical(operation)
    assert operation["status"] == "dispatch_started"
    for _ in range(2):
        with pytest.raises(AdmissionRefusedError):
            await prepare(k)
    with pytest.raises(AdmissionRefusedError):
        handoff.before_send()
    snapshot = await k.f.f.fixture.reservations.snapshot(k.f.f.service._target(k.f.approval.authority()))
    assert snapshot.total_usd == 8


@pytest.mark.parametrize(
    "drift",
    [
        "runtime",
        "principal",
        "run",
        "workload",
        "expiry",
        "receipt",
        "operation",
        "execution",
        "request",
        "digest",
        "arguments",
        "target",
        "revoked",
        "source",
        "hold",
        "cleanup",
    ],
)
async def test_substituted_or_revoked_original_never_reaches_door_claim(knowledge, drift):
    k = knowledge
    changes = {}
    if drift == "runtime":
        changes["runtime"] = SimpleNamespace(**vars(k.runtime))
    elif drift == "principal":
        k.claims["principal"]["user_id"] = "foreign"
    elif drift in {"run", "workload", "expiry", "receipt"}:
        field = {"run": "run_id", "workload": "workload_id", "expiry": "run_expires_at", "receipt": "receipt_id"}[drift]
        k.claims[field] = 0 if drift == "expiry" else str(uuid4())
    elif drift in {"operation", "execution", "request", "digest"}:
        field = drift + "_id" if drift != "digest" else "request_sha256"
        k.claims["native_test"][field] = str(uuid4()) if drift != "digest" else "a" * 64
    elif drift == "arguments":
        changes["arguments"] = {"query": "foreign"}
    elif drift == "target":
        changes["binding"] = {"private": "foreign-target"}
    elif drift == "revoked":
        await k.f.f.issuer.revoke(k.f.f.owner, run_id=k.f.approval.run_id)
    elif drift == "source":
        store = k.f.f.store
        store.client.put_item(
            TableName=store.table,
            Item=store.item(k.f.e.effect_source.key, "native_test_effect_contract_v1", {**k.f.e.effect_doc, "state": "revoked"}),
        )
    elif drift == "hold":
        target = k.f.f.service._target(k.f.approval.authority())
        await (await k.f.f.fixture.reservations._get_client()).hdel(target.key(), original(k)["reservation_id"])
    elif drift == "cleanup":
        record = {**original(k), "native_cleanup_attempts_used": 1}
        k.f.f.store.client.put_item(TableName=k.f.f.store.table, Item=k.f.f.store._item(record))
    with pytest.raises(AdmissionRefusedError):
        await prepare(k, **changes)
    assert "native_knowledge_dispatch" not in original(k)


async def test_concurrent_claims_dispatch_at_most_once(knowledge):
    outcomes = await asyncio.gather(*(prepare(knowledge) for _ in range(3)), return_exceptions=True)
    assert sum(type(value) is NativeKnowledgeDispatch for value in outcomes) == 1
    assert sum(isinstance(value, AdmissionRefusedError) for value in outcomes) == 2


async def test_lost_claim_ack_never_grants_replay_or_new_operation(knowledge, monkeypatch):
    k = knowledge
    transaction = k.f.f.store.transaction

    def lost(items):
        transaction(items)
        raise AdmissionRefusedError("offline lost acknowledgement")

    monkeypatch.setattr(k.f.f.store, "transaction", lost)
    with pytest.raises(AdmissionRefusedError):
        await prepare(k)
    monkeypatch.setattr(k.f.f.store, "transaction", transaction)
    assert original(k)["native_knowledge_dispatch"]["status"] == "dispatch_started"
    with pytest.raises(AdmissionRefusedError):
        await prepare(k)
    forged = deepcopy(k.claims)
    forged["native_test"]["operation_id"] = str(uuid4())
    with pytest.raises(AdmissionRefusedError):
        await prepare(k, claims=forged)


async def test_delayed_send_does_not_use_expired_hold(knowledge):
    k = knowledge
    handoff = await prepare(k)
    k.f.f.fixture.clock[0] += timedelta(seconds=31)
    with pytest.raises(AdmissionRefusedError):
        handoff.before_send()
    assert original(k)["native_knowledge_dispatch"]["status"] == "dispatch_started"


@pytest.mark.parametrize("fault", ["redirect", "oversize", "lost_response"])
async def test_door_transport_failure_never_replays_original_claim(knowledge, monkeypatch, fault):
    import httpx

    from src.agentauth import native_test_knowledge_dispatch as transport

    k = knowledge
    handoff = await prepare(k)
    calls = []

    def response(request):
        calls.append(request)
        if fault == "lost_response":
            raise httpx.ReadTimeout("offline lost Door response")
        return httpx.Response(
            302 if fault == "redirect" else 200,
            headers={"content-type": "application/json", "location": "https://foreign.invalid"},
            content=b"x" * 262145,
        )

    client = httpx.AsyncClient
    monkeypatch.setattr(transport.httpx, "AsyncClient", lambda **kw: client(transport=httpx.MockTransport(response), **kw))
    with pytest.raises((AdmissionRefusedError, httpx.ReadTimeout)):
        await handoff.forward("POST", "http://context-mcp/mcp/", {}, b"{}")
    with pytest.raises(AdmissionRefusedError):
        await prepare(k)
    assert len(calls) == 1
    assert original(k)["native_knowledge_dispatch"]["status"] == "dispatch_started"
