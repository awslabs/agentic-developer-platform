"""Partial binding recovery retains positive pod evidence without acknowledging input."""

import hashlib
import json
import os
from datetime import UTC, datetime

import pytest
from botocore.exceptions import EndpointConnectionError

from src.agentauth.bootstrap import BootstrapRefusedError, envelope_digest
from src.agentauth.chat_admission import admit
from src.agentauth.chat_capability import ChatAuthorizationRefusedError
from src.agentauth.chat_delivery import load_registered_delivery
from src.agentauth.chat_pre_admission_cleanup import recover_bound_cleanup
from src.agentauth.workload import VerifiedPod
from tests.agentauth import test_chat_queued_terminal as queued
from tests.agentauth.test_external_roots import proof

client = queued.client
runtime = queued.runtime
store = queued.store
sts = queued.sts
retained_input_table = queued.retained_input_table
registered_owner = queued.registered_owner
transport = queued.transport
recovery = queued.recovery
fixtures = queued.fixtures
TEARDOWN = "/internal/v1/agent/chat/data/teardown"


@pytest.fixture
def partial(runtime, transport, retained_input_table, registered_owner, sts, monkeypatch):
    recovery.supervisor(sts, monkeypatch)
    envelope = fixtures.history.prepare(runtime, message_id="run-write")
    run_hash = hashlib.sha256(b"run-write").hexdigest()
    pod = VerifiedPod(
        "original-pod-uid",
        f"chat-turn-{run_hash[:12]}-abcde",
        "adp-gateway-agents",
        "adp-chat-sandbox",
        "127.0.0.1",
        image_digest=runtime[5].image_digest,
        run_hash=run_hash,
    )
    runtime[1].store.bind(invocation_id="run-write", digest=envelope_digest(envelope), pod=pod, now=datetime.fromtimestamp(runtime[-1], UTC))
    state = {"exited": True, "absent": False, "observations": []}

    def exited(**fields):
        assert fields == {"name": pod.name, "uid": pod.uid, "run_hash": run_hash}
        state["observations"].append("exit")
        return pod.image_digest if state["exited"] else None

    def absent(**fields):
        assert fields == {"name": pod.name}
        state["observations"].append("absence")
        return state["absent"]

    monkeypatch.setattr(runtime[1].workloads, "exited_sandbox_image", exited)
    monkeypatch.setattr(runtime[1].workloads, "is_absent", absent)
    return pod, state, envelope


def receipt(runtime):
    return runtime[1].store._read("CHAT-LAUNCH#run-write", "PRE-ADMISSION-TEARDOWN")


async def removal(client, runtime, partial, **changes):
    pod, _, envelope = partial
    body = {"run_id": "run-write", "envelope_digest": envelope_digest(envelope), "pod_name": pod.name, "pod_uid": pod.uid, **changes}
    return await client.post(TEARDOWN, json=body, headers={"X-Adp-Producer-Proof": proof(envelope_digest(body))})


def recover_direct(runtime):
    authority = runtime[1]
    return recover_bound_cleanup(
        authority,
        load_registered_delivery(authority, "run-write", "tenant"),
        authority.store._read("INVOCATION#run-write", "DISPATCH"),
        queued.execution(runtime),
        removed=False,
        now=runtime[-1],
    )


def admit_direct(runtime, partial):
    pod, _, envelope = partial
    return admit(runtime[1], run_id="run-write", digest=envelope_digest(envelope), pod=pod, team_id="team", now=runtime[-1])


async def test_partial_binding_cleanup_uses_positive_exit_and_original_identity_without_ack(client, runtime, partial, transport):
    pod, state, _ = partial
    before = runtime[2].scan()["Items"]
    response = await recovery.resume(client, runtime)
    assert response.status_code == 200, response.text
    assert response.json() == {
        "state": "pre_admission_cleanup",
        "run_id": "run-write",
        "session_id": "session-a",
        "task_id": "task-a",
        "session_generation": fixtures.GENERATION,
        "pod_name": pod.name,
        "sandbox_uid": pod.uid,
        "image_digest": pod.image_digest,
        "attempt": 1,
        "removed": False,
    }
    saved = receipt(runtime)
    assert saved["exited_at"] == {"N": str(runtime[-1])}
    assert queued.execution(runtime)["chat_pre_admission_cleanup"] == saved["binding"]
    assert (await removal(client, runtime, partial)).json()["removed"] is False
    state["absent"] = True
    assert (await removal(client, runtime, partial)).json()["removed"] is True
    stored = receipt(runtime)
    state["observations"].clear()
    state["exited"] = False
    assert (await recovery.resume(client, runtime)).json()["removed"] is True
    assert (await removal(client, runtime, partial)).json()["removed"] is True
    assert not state["observations"] and receipt(runtime) == stored
    assert runtime[1].store._read("CHAT-LAUNCH#run-write", "LAUNCH") is None
    assert runtime[1].store._read("CHAT-LAUNCH#run-write", "TEARDOWN") is None
    assert runtime[2].scan()["Items"] == before
    assert (await recovery.completion.complete(client, runtime)).status_code == 404
    assert recovery.completion.publication.session(transport)["threads"]["thread-a"]["processing_task_id"] == "task-a"
    assert transport.client.send_message.call_count == 1
    assert recovery.completion.publication.payload(transport)["status"] == "interrupted"


async def test_missing_or_running_pod_does_not_prove_exit(client, runtime, partial):
    partial[1].update(exited=False, absent=True)
    assert (await recovery.resume(client, runtime)).status_code == 409
    assert (await removal(client, runtime, partial)).status_code == 404
    assert receipt(runtime) is None
    assert fixtures.outbox(runtime) is None
    assert "chat_pre_admission_cleanup" not in queued.execution(runtime)
    assert partial[1]["observations"] == ["exit"]


@pytest.mark.parametrize("field,value", [("pod_uid", "foreign-pod-uid"), ("pod_name", "foreign-pod"), ("envelope_digest", "f" * 64)])
async def test_stale_removal_notification_cannot_claim_cleanup(client, runtime, partial, field, value):
    assert (await recovery.resume(client, runtime)).status_code == 200
    before = receipt(runtime)
    partial[1]["absent"] = True
    partial[1]["observations"].clear()
    assert (await removal(client, runtime, partial, **{field: value})).status_code == 404
    assert receipt(runtime) == before and not partial[1]["observations"]


@pytest.mark.parametrize("field", ["current_attempt", "current_credential_epoch", "workload_binding", "pod_name"])
async def test_rebound_execution_refuses_old_evidence(client, runtime, partial, field):
    assert (await recovery.resume(client, runtime)).status_code == 200
    before = receipt(runtime)
    item = queued.execution(runtime)
    item[field] = {"N": "2"} if field.startswith("current_") else {"S": "other-pod"}
    fixtures.write_protected(runtime, item)
    assert (await recovery.resume(client, runtime)).status_code in {404, 409}
    assert receipt(runtime) == before


@pytest.mark.parametrize("change", ["attempt", "binding", "dispatch", "abort", "launch", "storage"])
async def test_snapshot_races_never_authorize_deletion(client, runtime, partial, monkeypatch, change):
    protected = runtime[1].store
    original = protected.client.transact_write_items

    def transact(**kwargs):
        if any(item.get("Put", {}).get("Item", {}).get("sk") == {"S": "PRE-ADMISSION-TEARDOWN"} for item in kwargs["TransactItems"]):
            if change == "storage":
                raise EndpointConnectionError(endpoint_url="https://storage.example.test")
            if change in {"attempt", "abort"}:
                item = queued.execution(runtime)
                item["current_attempt" if change == "attempt" else "abort_command_id"] = {"N": "2"} if change == "attempt" else {"S": "cancel"}
            elif change == "binding":
                item = protected._read(f"POD#{partial[0].uid}", "BINDING")
                item["invocation_id"] = {"S": "foreign-run"}
            elif change == "dispatch":
                item = protected._read("INVOCATION#run-write", "DISPATCH")
                item["envelope_digest"] = {"S": "f" * 64}
            else:
                item = {"pk": {"S": "CHAT-LAUNCH#run-write"}, "sk": {"S": "LAUNCH"}}
            fixtures.write_protected(runtime, item)
        return original(**kwargs)

    monkeypatch.setattr(protected.client, "transact_write_items", transact)
    assert (await recovery.resume(client, runtime)).status_code == (503 if change == "storage" else 409)
    assert receipt(runtime) is None and "chat_pre_admission_cleanup" not in queued.execution(runtime)


@pytest.mark.parametrize("stage", ["exit", "removal"])
async def test_lost_transaction_response_recovers_durable_evidence(client, runtime, partial, monkeypatch, stage):
    if stage == "removal":
        assert (await recovery.resume(client, runtime)).status_code == 200
        partial[1]["absent"] = True
    protected = runtime[1].store
    original = protected.client.transact_write_items

    def lost(**kwargs):
        original(**kwargs)
        raise EndpointConnectionError(endpoint_url="https://storage.example.test")

    monkeypatch.setattr(protected.client, "transact_write_items", lost)
    response = await recovery.resume(client, runtime) if stage == "exit" else await removal(client, runtime, partial)
    assert response.status_code == 503
    saved = receipt(runtime)
    monkeypatch.setattr(protected.client, "transact_write_items", original)
    partial[1]["observations"].clear()
    response = await recovery.resume(client, runtime) if stage == "exit" else await removal(client, runtime, partial)
    assert response.status_code == 200 and receipt(runtime) == saved
    assert not partial[1]["observations"]


@pytest.mark.parametrize("winner", ["cleanup", "admission"])
async def test_cleanup_and_admission_are_mutually_exclusive(client, runtime, partial, monkeypatch, winner):
    protected = runtime[1].store
    original = protected.client.transact_write_items
    raced = []

    def transact(**kwargs):
        writes = kwargs["TransactItems"]
        target = "LAUNCH" if winner == "cleanup" else "PRE-ADMISSION-TEARDOWN"
        if not raced and any(item.get("Put", {}).get("Item", {}).get("sk") == {"S": target} for item in writes):
            raced.append(True)
            recover_direct(runtime) if winner == "cleanup" else admit_direct(runtime, partial)
        return original(**kwargs)

    monkeypatch.setattr(protected.client, "transact_write_items", transact)
    if winner == "cleanup":
        with pytest.raises(ChatAuthorizationRefusedError):
            admit_direct(runtime, partial)
        assert receipt(runtime) is not None
        assert protected._read("CHAT-LAUNCH#run-write", "LAUNCH") is None
    else:
        assert (await recovery.resume(client, runtime)).status_code == 409
        assert receipt(runtime) is None
        assert protected._read("CHAT-LAUNCH#run-write", "LAUNCH") is not None
    assert raced


async def test_worker_role_cannot_observe_or_request_cleanup(client, runtime, partial, sts):
    sts["role"] = "worker"
    assert (await recovery.resume(client, runtime)).status_code == 403
    assert (await removal(client, runtime, partial)).status_code == 403
    assert not partial[1]["observations"]


async def test_changed_observation_scope_cannot_reuse_exit(client, runtime, partial, monkeypatch):
    assert (await recovery.resume(client, runtime)).status_code == 200
    monkeypatch.setattr(runtime[1].workloads, "_namespace", "foreign")
    assert (await removal(client, runtime, partial)).status_code == 404
    assert "removed_at" not in receipt(runtime)


async def test_stale_resume_cannot_erase_concurrent_removal(client, runtime, partial, monkeypatch):
    assert (await recovery.resume(client, runtime)).status_code == 200
    protected = runtime[1].store
    original = protected.client.transact_write_items

    def race(**kwargs):
        item = receipt(runtime)
        item["removed_at"] = {"N": str(runtime[-1])}
        fixtures.write_protected(runtime, item)
        return original(**kwargs)

    monkeypatch.setattr(protected.client, "transact_write_items", race)
    assert (await recovery.resume(client, runtime)).status_code == 409
    assert receipt(runtime)["removed_at"] == {"N": str(runtime[-1])}


@pytest.mark.parametrize("field,value", [("binding", {"M": {}}), ("exited_at", {"N": "0"}), ("removed_at", {"N": "1"})])
async def test_malformed_receipt_cannot_authorize_deletion(client, runtime, partial, field, value):
    assert (await recovery.resume(client, runtime)).status_code == 200
    item = receipt(runtime)
    item[field] = value
    fixtures.write_protected(runtime, item)
    partial[1]["observations"].clear()
    assert (await recovery.resume(client, runtime)).status_code == 404
    assert not partial[1]["observations"]


@pytest.mark.parametrize("field,value", [("tenant_id", "foreign"), ("personas", ["foreign"])])
async def test_other_supervisor_scope_cannot_observe_original_pod(client, runtime, partial, monkeypatch, field, value):
    bindings = json.loads(os.environ["ADP_MODEL_ROOT_BINDINGS"])
    bindings[0][field] = value
    monkeypatch.setenv("ADP_MODEL_ROOT_BINDINGS", json.dumps(bindings))
    assert (await recovery.resume(client, runtime)).status_code == 404
    assert not partial[1]["observations"]


async def test_cancellation_and_expiry_do_not_prevent_original_pod_cleanup(client, runtime, partial):
    item = queued.execution(runtime)
    item["abort_command_id"] = {"S": "owner-cancel"}
    fixtures.write_protected(runtime, item)
    protected = runtime[1].store
    grant = protected.authority.load_grant(principal="run-write#1", tenant_id="tenant")
    authority = protected._read("TENANT#tenant", f"AUTHORITY#{grant.authority.reference_id}")
    authority["expires_at"] = {"S": "2000-01-01T00:00:00Z"}
    fixtures.write_protected(runtime, authority)
    with pytest.raises(BootstrapRefusedError):
        protected.live_grant(invocation_id="run-write", tenant_id="tenant", attempt=1, now=datetime.fromtimestamp(runtime[-1], UTC))
    assert (await recovery.resume(client, runtime)).status_code == 200
    assert queued.execution(runtime)["abort_command_id"] == {"S": "owner-cancel"}
    assert queued.execution(runtime)["status"] == {"S": "active"}
