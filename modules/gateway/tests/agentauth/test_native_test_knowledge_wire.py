"""Actual facade/native HTTP handoff; the AgentCore provider stays offline."""

import json
from uuid import uuid4

import pytest

from tests.agentauth import test_native_test_facade_admission as wire
from tests.migrations.conftest_postgres import pg_server, pg_url  # noqa: F401

admission, sessions, native, all_in = wire.admission, wire.sessions, wire.native, wire.all_in
facade_tool, facade, http_wire = wire.facade_tool, wire.facade, wire.http_wire
pytestmark = pytest.mark.parametrize("facade_tool", ["knowledge.search"], indirect=True)


async def test_native_call_uses_one_held_dispatch_without_implicit_discovery(http_wire):
    w = http_wire
    reply = await w.client.post(
        "/agent/tools/call", json={key: value for key, value in w.f.call.items() if key not in {"schema_version", "action"}}, headers=w.headers
    )
    assert reply.status_code == 200, reply.text
    receipt = reply.json()
    assert receipt["status"] == "confirmed"
    assert len(w.state["provider"]) == len(w.state["kms"]) == 1
    assert w.state["provider"][0]["params"]["arguments"] == w.f.call["arguments"]
    claims = json.loads(w.state["kms"][0]["Message"])
    assert claims["native_test"]["operation_id"] == w.f.operation
    assert claims["native_test"]["request_id"] == w.f.call["request_id"]
    assert claims["native_test"]["execution_id"] == receipt["execution_id"]
    assert claims["rpc_id"] == w.state["provider"][0]["id"]
    # Original read cleanup is a local settled-operation fence, never another
    # Knowledge call. The native status still reports retained, pending funds.
    closed = await w.client.post(
        "/agent/tools/executions/" + receipt["execution_id"] + "/close?tool=knowledge___search",
        headers={**w.headers, "x-adp-native-test-operation": str(uuid4())},
    )
    assert closed.status_code == 200, closed.text
    assert closed.json()["cleanup_status"] == "confirmed"
    assert len(w.state["provider"]) == 1
    operation = w.f.f.store.read_operation("tenant-one", w.f.approval.run_id, w.f.operation)
    state = w.app.state.native_test_runtime.tool_lifecycle._execution_status(w.f.approval, operation)
    assert state["cleanup_status"] == "confirmed"


async def test_asserted_native_header_without_native_decision_cannot_dispatch(http_wire, monkeypatch):
    w = http_wire
    monkeypatch.setattr(
        w.f.adapter,
        "prepare",
        wire.AsyncMock(
            return_value={
                "schema_version": "1.0",
                "allowed": True,
                "receipt_id": "not-native",
                "principal": {"user_id": "canonical-human", "root_human_id": "canonical-human", "tenant_id": "tenant-one", "team_id": "team-one"},
            }
        ),
    )
    reply = await w.client.post(
        "/agent/tools/call", json={key: value for key, value in w.f.call.items() if key not in {"schema_version", "action"}}, headers=w.headers
    )
    assert reply.status_code == 403
    assert w.state["provider"] == [] and w.state["kms"] == []
