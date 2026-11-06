"""Atomic step cardinality with Moto; no issuer or funding proof is asserted."""

import hashlib
from datetime import UTC, datetime
from uuid import uuid4

import boto3
import pytest
from botocore.exceptions import EndpointConnectionError
from moto import mock_aws

from src.agentauth.native_test_enrollment import NativeTestStore
from src.agentauth.native_test_facade_admission import _FacadeEffectAdmission, native_interpreter_request_key
from src.agentauth.native_test_facade_contract import FacadeEffectQuoteAdapter, InterpreterStepCostContract, NativeFacadeCall
from src.agentauth.test_run_admission_store import AdmissionRefusedError, canonical, digest


@pytest.fixture
def state(monkeypatch):
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(
            TableName="offline-steps",
            BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}, {"AttributeName": "sk", "AttributeType": "S"}],
        )
        store = NativeTestStore(table_name="offline-steps", client=client)
        authority = {
            "tenant_id": "offline",
            "user_id": "offline-user",
            "run_id": str(uuid4()),
            "dispatch_expires_at": 1300,
            "cleanup_expires_at": 1600,
        }
        client.put_item(
            TableName=store.table,
            Item={
                **store.authority_key("offline", authority["run_id"]),
                "kind": {"S": "tools_test_run_v1"},
                "document": {"S": canonical(authority)},
                "dispatch_expires_at": {"N": "1300"},
            },
        )
        # Exercise the real journal and authority-document CAS. Full enrollment,
        # protected source registration and reservation remain outside this test.
        monkeypatch.setattr(store, "authority_guards", lambda value, now: [store._guard(value, now)])
        yield store, authority


def prepared(state, step, execution_id=None, request_id=None):
    store, authority = state
    arguments = {
        "start": {},
        "execute": {"session_id": "a" * 64, "language": "python", "code": "print(42)"},
        "result": {"session_id": "a" * 64, "provider_id": "offline-task"},
        "file": {"session_id": "a" * 64, "path": "/tmp/offline.txt"},
    }["execute" if step == "execute_status" else step]
    body = canonical(
        {
            "schema_version": "1.0",
            "action": "recover" if step == "execute_status" else "call",
            "tool": "code_interpreter___" + ("execute" if step == "execute_status" else step),
            "arguments": arguments,
            "request_id": request_id or str(uuid4()),
            "execution_id": execution_id,
            "ttl_seconds": 300 if step == "start" else None,
        }
    ).encode()
    contract = InterpreterStepCostContract(
        schema_version="1.0",
        state="active",
        scope="model_tool_setup_cleanup",
        effect_key="native-effect:offline-" + step,
        request_sha256="a" * 64,
        source_sha256="b" * 64,
        pricing_revision="c" * 64,
        not_before=1,
        expires_at=1600,
        quote_seconds=60,
        session_seconds=300,
        cleanup_seconds=600,
        cleanup_attempts=2,
        request_binding="facade_call_v1",
        max_call_bytes=65536,
        max_response_bytes=262144,
        transport_attempts=1,
        claim_attempts=1,
        interpreter_step=step,
        max_step_requests=1,
        components=[
            {
                "component": "offline-only",
                "unit": "offline-only",
                "quantity": "1",
                "usd_per_unit": "1",
                "pricing_evidence_sha256": "d" * 64,
                "enforced_limit_sha256": "e" * 64,
            }
        ],
    )
    service = _FacadeEffectAdmission(
        store=store,
        effect_key=contract.effect_key,
        contract=contract,
        secret="offline-proof",
        run_started_at=1000,
        clock=lambda: datetime.fromtimestamp(1000, UTC),
    )
    operation = {
        "tenant_id": "offline",
        "run_id": authority["run_id"],
        "operation_id": str(uuid4()),
        "status": "preparing",
        "version": 1,
        "owner_token": str(uuid4()),
        "reservation_confirmed": False,
        "binding": {"request_sha256": hashlib.sha256(body).hexdigest()},
    }
    operation["binding_digest"] = digest(operation["binding"])
    return service, service._new_operation(operation, authority, body), body


def dispatched_start(state):
    store, authority = state
    service, operation, _ = prepared(state, "start")
    assert service._claim_operation(authority, operation, 1000)[1]
    # Explicit offline stand-in for the state supplied by actual admission;
    # this helper neither issues approval nor reserves funds.
    operation.update(status="dispatch_started", reservation_confirmed=True)
    store.client.put_item(TableName=store.table, Item=store._item(operation))
    return operation


def test_start_replay_cannot_replace_or_replenish_original_slot(state):
    _, authority = state
    service, operation, _ = prepared(state, "start")
    assert service._claim_operation(authority, operation, 1000) == (operation, True)
    assert service._claim_operation(authority, operation, 1000) == (operation, False)
    other, replacement, _ = prepared(state, "start")
    with pytest.raises(AdmissionRefusedError):
        other._claim_operation(authority, replacement, 1000)


@pytest.mark.parametrize("step", ("execute", "execute_status", "result", "file"))
def test_child_slot_requires_original_session_and_is_one_use(state, step):
    store, authority = state
    original = dispatched_start(state)
    foreign, bad, _ = prepared(state, step, str(uuid4()))
    with pytest.raises(AdmissionRefusedError, match="original_interpreter"):
        foreign._claim_operation(authority, bad, 1000)
    execution = original["facade_dispatch"]["execution_id"]
    request_id = None
    if step in {"execute_status", "result"}:
        execute_service, execute, _ = prepared(state, "execute", execution)
        assert execute_service._claim_operation(authority, execute, 1000)[1]
        execute.update(status="dispatch_started", reservation_confirmed=True)
        store.client.put_item(TableName=store.table, Item=store._item(execute))
        request_id = execute["interpreter_request"]["request_id"]
    service, operation, _ = prepared(state, step, execution, request_id)
    assert service._claim_operation(authority, operation, 1000) == (operation, True)
    assert service._claim_operation(authority, operation, 1000) == (operation, False)
    other, replacement, _ = prepared(state, step, execution, request_id)
    with pytest.raises(AdmissionRefusedError):
        other._claim_operation(authority, replacement, 1000)


@pytest.mark.parametrize("status,funded", (("preparing", False), ("admitted", True), ("unknown", True), ("dispatch_started", False)))
def test_unconfirmed_start_cannot_claim_child(state, status, funded):
    store, authority = state
    original = dispatched_start(state)
    original.update(status=status, reservation_confirmed=funded)
    store.client.put_item(TableName=store.table, Item=store._item(original))
    service, operation, _ = prepared(state, "execute", original["facade_dispatch"]["execution_id"])
    with pytest.raises(AdmissionRefusedError, match="original_interpreter_start"):
        service._claim_operation(authority, operation, 1000)


def test_lost_claim_ack_keeps_original_step_and_cannot_dispatch(state, monkeypatch):
    store, authority = state
    service, operation, _ = prepared(state, "start")
    transact = store.client.transact_write_items

    def lost(**kwargs):
        transact(**kwargs)
        raise EndpointConnectionError(endpoint_url="https://offline.invalid")

    monkeypatch.setattr(store.client, "transact_write_items", lost)
    assert service._claim_operation(authority, operation, 1000) == (operation, False)
    other, replacement, _ = prepared(state, "start")
    with pytest.raises(AdmissionRefusedError):
        other._claim_operation(authority, replacement, 1000)


def test_step_quote_cannot_authorize_another_operation(state):
    service, _, body = prepared(state, "execute", str(uuid4()))
    adapter = object.__new__(FacadeEffectQuoteAdapter)
    service.contract.request_sha256 = NativeFacadeCall.parse(body).approved_digest()
    assert adapter._request_permitted(body, service.contract)
    for step in ("start", "result", "file"):
        _, _, changed = prepared(state, step, None if step == "start" else str(uuid4()))
        assert not adapter._request_permitted(changed, service.contract)


def test_start_claim_atomically_retains_request_and_execution_owners(state):
    store, authority = state
    original = dispatched_start(state)
    request = original["interpreter_request"]
    assert (
        store.document(native_interpreter_request_key(store, "offline", "offline-user", request["request_id"]), "native_test_interpreter_request_v1")
        == request
    )
    assert store.document(store.key("offline", "EXECUTION_OWNER", request["execution_id"]), "native_test_interpreter_execution_v1") == {
        key: request[key] for key in ("run_id", "execution_id", "user_id", "operation_id")
    }


def test_execute_status_digest_is_distinct_from_executing_the_same_code(state):
    service, _, body = prepared(state, "execute_status", str(uuid4()))
    service.contract.request_sha256 = NativeFacadeCall.parse(body).approved_digest()
    adapter = object.__new__(FacadeEffectQuoteAdapter)
    assert adapter._request_permitted(body, service.contract)
    _, _, execute = prepared(state, "execute", str(uuid4()))
    assert not adapter._request_permitted(execute, service.contract)


@pytest.mark.parametrize("step", ("execute_status", "result"))
def test_task_read_requires_original_dispatched_execute(state, step):
    _, authority = state
    start = dispatched_start(state)
    service, operation, _ = prepared(state, step, start["facade_dispatch"]["execution_id"])
    with pytest.raises(AdmissionRefusedError, match="original_interpreter_execute"):
        service._claim_operation(authority, operation, 1000)
