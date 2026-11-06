"""Memory completion is evidence bound, replay safe, and preserves saved data."""

import copy
import hashlib
import json
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from tools_facade.encoding import canonical
from tools_facade.executions import ExecutionStore
from tools_facade.knowledge_memory import MEMORY_TOOLS
from tools_facade.reconciler import run_once


def prepare(tool, *, host=False):
    authority = SimpleNamespace(
        principal=SimpleNamespace(tenant_id="tenant-one", user_id="person-one", team_id="team-one"),
        run_id="fixture-host-run" if host else None,
        workload_id="fixture-instance" if host else None,
        run_expires_at=1200 if host else None,
        host_generation=3 if host else None,
        receipt_id=str(uuid4()),
    )
    store = ExecutionStore(clock=lambda: 1000)
    request_id = uuid4()
    args = {"content": "Unicode memory: café ✓"}
    key, operation, created = store.claim(None, request_id, tool, args, authority)
    assert created
    store.dispatched(key)
    value = {"status": "saved", "content": "Unicode memory: café ✓"}
    proof = {
        "schema_version": "1.0",
        "tool": tool,
        "principal": {"tenant_id": "tenant-one", "user_id": "person-one", "team_id": "team-one"},
        "receipt_id": authority.receipt_id,
        "arguments_sha256": hashlib.sha256(canonical(args)).hexdigest(),
        "request_id": str(request_id),
        "execution_id": operation["operation_execution_id"]["S"],
        "result_sha256": hashlib.sha256(canonical(value)).hexdigest(),
    }
    response = {
        "result": {"content": [{"type": "text", "text": json.dumps(value)}], "_meta": {"adp_knowledge_memory": proof}}
    }
    return store, authority, key, operation, response, args


@pytest.mark.parametrize("tool", sorted(MEMORY_TOOLS))
@pytest.mark.parametrize("scheduled", [False, True])
def test_proved_memory_closes_without_deleting_saved_entries_or_dispatching_again(tool, scheduled):
    store, authority, key, operation, response, args = prepare(tool)
    identifier = UUID(operation["operation_execution_id"]["S"])
    confirmed = store.confirmed(key, response)
    assert confirmed["result_status"] == {"S": "success"}
    _, replay, created = store.claim(identifier, UUID(operation["request_id"]["S"]), tool, args, authority)
    assert not created and replay["result_json"] == confirmed["result_json"]
    assert store.stateless_ready(identifier)
    store.close(identifier, tool, authority)
    if scheduled:
        run_once(
            ExecutionStore(clock=lambda: 1001), stop_provider=lambda *_: pytest.fail("memory close must not dispatch")
        )
    else:
        assert store.confirm_closed(identifier, tool, authority)["cleanup_status"] == "confirmed"
    root = store._client().get_item(
        TableName=store._table(), Key={"execution_id": {"S": str(identifier)}}, ConsistentRead=True
    )["Item"]
    assert root["cleanup_evidence"] == {"S": "memory_operations_completed"}
    assert store.cleanup_proved(root)
    # Closing only settles the operation; the saved memory remains persistent.
    original = store.lookup(identifier, UUID(operation["request_id"]["S"]), tool, authority)
    assert original["result_json"] == confirmed["result_json"]


@pytest.mark.parametrize("tool", sorted(MEMORY_TOOLS))
def test_host_memory_completion_is_bound_to_run_and_can_close(tool):
    store, authority, key, operation, response, _ = prepare(tool, host=True)
    identifier = UUID(operation["operation_execution_id"]["S"])
    assert operation["memory_contract"] == {"S": "host"}
    store.confirmed(key, response)
    assert store.stateless_ready(identifier)
    store.close(identifier, tool, authority)
    assert store.confirm_closed(identifier, tool, authority)["cleanup_status"] == "confirmed"


@pytest.mark.parametrize(
    "fault",
    [
        "absent",
        "receipt",
        "owner",
        "tenant",
        "team",
        "tool",
        "args",
        "request",
        "execution",
        "result",
        "legacy",
        "run",
        "sibling",
    ],
)
def test_missing_mismatched_or_unsettled_evidence_cannot_close_memory(fault):
    store, authority, key, operation, response, args = prepare("knowledge___remember")
    identifier = UUID(operation["operation_execution_id"]["S"])
    changed = copy.deepcopy(response)
    proof = changed["result"]["_meta"]["adp_knowledge_memory"]
    if fault == "absent":
        del changed["result"]["_meta"]
    elif fault in {"owner", "tenant", "team"}:
        proof["principal"][{"owner": "user_id", "tenant": "tenant_id", "team": "team_id"}[fault]] = "foreign"
    elif fault in {"receipt", "request", "execution"}:
        proof[fault + "_id"] = str(uuid4())
    elif fault == "args":
        proof["arguments_sha256"] = "0" * 64
    elif fault == "tool":
        proof["tool"] = "knowledge___experience"
    elif fault == "result":
        changed["result"]["content"][0]["text"] = "{}"
    elif fault in {"legacy", "run"}:
        options = {"TableName": store._table(), "Key": {"execution_id": {"S": key}}}
        if fault == "legacy":
            store._client().update_item(**options, UpdateExpression="REMOVE memory_contract")
        else:
            store._client().update_item(
                **options,
                UpdateExpression="SET binding = :run",
                ExpressionAttributeValues={":run": {"S": "run:foreign:workload"}},
            )
    elif fault == "sibling":
        store.confirmed(key, response)
        sibling, _, _ = store.claim(identifier, uuid4(), "knowledge___remember", args, authority)
        store.dispatched(sibling)
    if fault != "sibling":
        with pytest.raises(HTTPException, match="knowledge memory completion unavailable"):
            store.confirmed(key, changed)
    assert not store.stateless_ready(identifier)
    store.close(identifier, "knowledge___remember", authority)
    assert store.confirm_closed(identifier, "knowledge___remember", authority)["cleanup_status"] == "unknown"
    run_once(
        ExecutionStore(clock=lambda: 1001), stop_provider=lambda *_: pytest.fail("uncertain memory cannot redispatch")
    )


@pytest.mark.parametrize("tool", sorted(MEMORY_TOOLS))
@pytest.mark.parametrize("failure", [None, "timeout", "missing_proof"])
@pytest.mark.parametrize("host", [False, True])
def test_public_memory_call_replay_and_restart_use_original_receipt(tool, failure, host):
    import httpx
    from fastapi.testclient import TestClient

    from tools_facade.server import create_app

    class Authority:
        def decision(self):
            return {
                "schema_version": "1.0",
                "allowed": True,
                "receipt_id": str(uuid4()),
                "principal": {"user_id": "owner", "root_human_id": "owner", "tenant_id": "tenant", "team_id": "team"},
                **({"run_id": "fixture-host-run", "workload_id": "fixture-instance",
                    "run_expires_at": 4102444800, "host_generation": 3} if host else {}),
            }

        async def list_tools(self, headers):
            return {
                **self.decision(),
                "tools": [{"name": tool, "description": "memory", "inputSchema": {"type": "object"}}],
            }

        async def authorize(self, tool, arguments, headers):
            return self.decision()

        async def close(self, tool, headers):
            return self.decision()

        async def recover(self, tool, headers, **kwargs):
            return self.decision()

    class Provider:
        calls = 0

        async def invoke(self, selected, arguments, decision):
            self.calls += 1
            reference = decision._knowledge_memory
            assert reference["request_id"] == body["request_id"]
            assert reference["requested_execution_id"] is None
            assert arguments == body["arguments"] and "knowledge_memory" not in arguments
            if failure == "timeout":
                raise httpx.ReadTimeout("write acknowledgement lost")
            value = {"stored": True}
            result = {"content": [{"type": "text", "text": json.dumps(value)}]}
            if failure != "missing_proof":
                result["_meta"] = {
                    "adp_knowledge_memory": {
                        "schema_version": "1.0",
                        "tool": selected,
                        "principal": decision.principal.model_dump(exclude={"root_human_id"}),
                        "receipt_id": decision.receipt_id,
                        "arguments_sha256": hashlib.sha256(canonical(arguments)).hexdigest(),
                        "request_id": reference["request_id"],
                        "execution_id": reference["execution_id"],
                        "result_sha256": hashlib.sha256(canonical(value)).hexdigest(),
                    }
                }
            return {"jsonrpc": "2.0", "result": result}

    authority, provider = Authority(), Provider()
    headers = ({"x-adp-crew-host-assertion": "fixture-host-proof", "x-agent-runid": "fixture-host-run"}
               if host else {"Authorization": "Bearer synthetic-owner"})
    body = {"tool": tool, "request_id": str(uuid4()), "arguments": {"content": "owned memory"}}
    client = TestClient(create_app(gateway=authority, agentcore=provider))
    first = client.post("/agent/tools/call", headers=headers, json=body)
    assert first.status_code == (202 if failure else 200), first.text
    restarted = TestClient(create_app(gateway=authority, agentcore=provider))
    repeated = restarted.post("/agent/tools/call", headers=headers, json=body)
    assert repeated.json() == first.json() and provider.calls == 1
    changed = restarted.post("/agent/tools/call", headers=headers, json={**body, "arguments": {"content": "different"}})
    assert changed.status_code == 409 and provider.calls == 1
    closed = restarted.post(
        f"/agent/tools/executions/{first.json()['execution_id']}/close?tool={tool}", headers=headers
    )
    assert closed.json()["cleanup_status"] == ("unknown" if failure else "confirmed")
    assert provider.calls == 1
