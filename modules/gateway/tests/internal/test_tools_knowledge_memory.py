"""Human memory requires original paid admission, isolation and one dispatch."""
# ruff: noqa: F811

import copy
import hashlib
import json
from uuid import uuid4

import pytest
from botocore.exceptions import ClientError
from fastapi import HTTPException
from starlette.responses import JSONResponse

from src.internal import tools_knowledge as mediator
from src.internal import tools_knowledge_memory as memory
from src.internal.tools_admissions import ToolCallAdmission
from src.internal.tools_authorize import ToolsAuthorizationBody
from tests.internal.test_tools_dynamic_catalogue import deployed  # noqa: F401
from tests.internal.test_tools_knowledge import HEADERS, PRINCIPAL, bridge, host_run, packet  # noqa: F401


def admitted(bridge, operation, args, *, host_claims=None):
    descriptor = {"schema_version": "1.0", "request_id": str(uuid4()), "execution_id": str(uuid4()), "requested_execution_id": None}
    original = ToolCallAdmission(
        table=bridge.runtime.store.table,
        client=bridge.runtime.store.client,
        tenant_id=PRINCIPAL["tenant_id"],
        user_id=PRINCIPAL["user_id"],
        team_id=PRINCIPAL["team_id"],
        run_id=host_claims["run_id"] if host_claims else None,
        workload_id=host_claims["workload_id"] if host_claims else None,
        body=ToolsAuthorizationBody(schema_version="1.0", tool="knowledge___" + operation, arguments=args, request_id=descriptor["request_id"]),
    )
    original.claim()
    row = bridge.deployed.items[(original.key["pk"]["S"], original.key["sk"]["S"])]
    row["status"] = {"S": "admitted"}
    permission = "knowledge." + operation
    bridge.policy["allowed_tools"].append(permission)
    bridge.policy["consented_tools"].append(permission)
    bridge.policy["tool_max_cost_usd"][permission] = "0.01"
    return descriptor, original.receipt_id, row


CASES = [
    (
        "remember",
        {"session_id": "fixture-session", "messages": [{"role": "user", "content": "fixture memory"}]},
        {"stored": True, "session_id": "fixture-session", "entry_id": "01ARZ3NDEKTSV4RRFFQ69G5FAV"},
    ),
    (
        "experience",
        {"action": "save", "persona": "developer", "content": "fixture memory"},
        {"status": "saved", "persona": "developer", "visibility": "private", "id": "01ARZ3NDEKTSV4RRFFQ69G5FAV"},
    ),
    (
        "experience",
        {"action": "recall", "persona": "developer", "query": "fixture memory"},
        {"status": "ok", "results": [{"id": "01ARZ3NDEKTSV4RRFFQ69G5FAV", "content": "fixture memory"}], "total": 1},
    ),
    ("experience", {"action": "list_syntheses", "persona": "developer"}, {"status": "ok", "syntheses": [], "total": 0}),
]


def door_result(bridge, monkeypatch, value):
    original = mediator.forward_door

    async def forward(method, url, headers, body):
        # Exercise the real Door signature verification from the wire fixture.
        await original(method, url, headers, body)
        return JSONResponse({"jsonrpc": "2.0", "id": json.loads(body)["id"], "result": {"content": [{"type": "text", "text": json.dumps(value)}]}})

    monkeypatch.setattr(mediator, "forward_door", forward)


@pytest.mark.parametrize("operation,args,value", CASES)
async def test_owned_memory_completion_is_bound_to_original_operation(bridge, monkeypatch, operation, args, value):
    descriptor, receipt, _ = admitted(bridge, operation, args)
    door_result(bridge, monkeypatch, value)
    response = await bridge.client.post(mediator.PATH, headers=HEADERS, json=packet(operation, args, receipt_id=receipt, knowledge_memory=descriptor))
    assert response.status_code == 200
    proof = response.json()["result"]["_meta"]["adp_knowledge_memory"]
    assert proof["principal"] == PRINCIPAL and proof["receipt_id"] == receipt
    assert proof["request_id"] == descriptor["request_id"] and proof["execution_id"] == descriptor["execution_id"]
    assert proof["arguments_sha256"] == hashlib.sha256(mediator.canonical(args)).hexdigest()
    assert proof["result_sha256"] == hashlib.sha256(mediator.canonical(value)).hexdigest()
    assert len(bridge.forwards) == 1
    fence = bridge.deployed.writes[-1]["Item"]
    assert fence["pk"]["S"].startswith("TOOLS_KNOWLEDGE_MEMORY#")
    assert not any("ttl" in key for key in fence)


@pytest.mark.parametrize("operation,args,value", CASES[:2])
async def test_host_memory_write_reopens_its_own_run_bound_admission(bridge, host_run, monkeypatch, operation, args, value):
    descriptor, receipt, _ = admitted(bridge, operation, args, host_claims=host_run.claims)
    door_result(bridge, monkeypatch, value)
    response = await bridge.client.post(
        mediator.PATH,
        headers=HEADERS,
        json=packet(operation, args, receipt_id=receipt, knowledge_memory=descriptor, **host_run.claims),
    )
    assert response.status_code == 200, response.text
    proof = response.json()["result"]["_meta"]["adp_knowledge_memory"]
    assert proof["receipt_id"] == receipt and proof["principal"] == PRINCIPAL
    assert len(bridge.forwards) == 1
    assert bridge.deployed.writes[-1]["Item"]["pk"]["S"].startswith("TOOLS_KNOWLEDGE_MEMORY#")


async def test_host_memory_cannot_reuse_an_unbound_human_admission(bridge, host_run):
    operation, args, _ = CASES[0]
    descriptor, receipt, _ = admitted(bridge, operation, args)
    response = await bridge.client.post(
        mediator.PATH,
        headers=HEADERS,
        json=packet(operation, args, receipt_id=receipt, knowledge_memory=descriptor, **host_run.claims),
    )
    assert response.status_code == 403 and not bridge.forwards


@pytest.mark.parametrize("fault", ["absent", "claimed", "binding", "receipt", "request", "execution", "extra", "native", "revoked"])
async def test_unfunded_foreign_or_changed_memory_never_dispatches(bridge, fault):
    operation, args, _ = CASES[1]
    descriptor, receipt, row = admitted(bridge, operation, args)
    extra = {}
    if fault == "absent":
        bridge.deployed.items.clear()
    elif fault == "claimed":
        row["status"] = {"S": "claimed"}
    elif fault == "binding":
        row["binding"] = {"S": "foreign-arguments"}
    elif fault == "receipt":
        receipt = str(uuid4())
    elif fault == "request":
        descriptor["request_id"] = str(uuid4())
    elif fault == "execution":
        descriptor["requested_execution_id"] = str(uuid4())
    elif fault == "extra":
        descriptor["principal"] = PRINCIPAL
    elif fault == "native":
        extra = {
            "native_test": {"operation_id": "x", "execution_id": "x", "request_id": "x", "request_sha256": "x"},
            "run_id": "fixture-run",
            "workload_id": "fixture-workload",
            "run_expires_at": 1040,
        }
    else:
        bridge.policy["consented_tools"] = []
        bridge.policy["consent_mode"] = "explicit"  # #8387: revocation is an explicit decision
    response = await bridge.client.post(
        mediator.PATH, headers=HEADERS, json=packet(operation, args, receipt_id=receipt, knowledge_memory=descriptor, **extra)
    )
    assert response.status_code == 403 or (fault == "native" and response.json()["result"]["isError"])
    assert not bridge.forwards


@pytest.mark.parametrize("lost_ack", [False, True])
async def test_resigned_retry_or_lost_claim_ack_cannot_repeat_memory(bridge, monkeypatch, lost_ack):
    operation, args, value = CASES[1]
    descriptor, receipt, _ = admitted(bridge, operation, args)
    door_result(bridge, monkeypatch, value)
    if lost_ack:
        original = bridge.runtime.store.client.put_item

        def ambiguous(**kwargs):
            original(**kwargs)
            raise ClientError({"Error": {"Code": "InternalServerError"}}, "PutItem")

        monkeypatch.setattr(bridge.runtime.store.client, "put_item", ambiguous)
    first = await bridge.client.post(mediator.PATH, headers=HEADERS, json=packet(operation, args, receipt_id=receipt, knowledge_memory=descriptor))
    assert first.status_code == (503 if lost_ack else 200)
    # A fresh valid RSA signature/transport ID still points at the same effect.
    again = await bridge.client.post(
        mediator.PATH, headers=HEADERS, json=packet(operation, args, receipt_id=receipt, knowledge_memory=descriptor, rpc_id="new-ingress-request")
    )
    assert again.status_code == 409
    assert len(bridge.forwards) == (0 if lost_ack else 1)


@pytest.mark.parametrize("value", [{"stored": True}, {"status": "saved"}, {"status": "ok"}, {"error": "storage unavailable"}])
async def test_incomplete_or_failed_write_never_receives_completion_proof(bridge, monkeypatch, value):
    operation, args, _ = CASES[1]
    descriptor, receipt, _ = admitted(bridge, operation, args)
    door_result(bridge, monkeypatch, value)
    response = await bridge.client.post(mediator.PATH, headers=HEADERS, json=packet(operation, args, receipt_id=receipt, knowledge_memory=descriptor))
    assert response.status_code == 502 or response.json()["result"]["isError"]
    assert "adp_knowledge_memory" not in response.text


@pytest.mark.parametrize(
    "operation,args",
    [
        ("remember", {"session_id": "x", "messages": []}),
        ("remember", {"session_id": "x", "messages": [{"role": [], "content": "x"}]}),
        ("experience", {"action": "save", "persona": "developer", "content": "x" * 8193}),
        ("experience", {"action": "save", "persona": "developer", "content": "x", "owner_sub": "foreign"}),
        ("experience", {"action": "recall", "persona": "developer", "query": "x", "limit": True}),
        ("experience", {"action": "recall", "persona": "developer", "query": "x", "limit": 26}),
        ("experience", {"action": "recall", "persona": "developer", "query": "x", "graph_expand": True}),
    ],
)
def test_unbounded_or_unsupported_memory_input_is_rejected(operation, args):
    with pytest.raises(HTTPException) as error:
        memory.validate_arguments(operation, copy.deepcopy(args))
    assert error.value.status_code == 422
