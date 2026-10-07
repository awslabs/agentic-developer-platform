"""Creation uncertainty never permits a second sandbox or a synthetic cleanup."""

import hashlib
import json
import os
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from botocore.exceptions import EndpointConnectionError

from src.agentauth.bootstrap import BootstrapRefusedError, envelope_digest
from src.agentauth.chat_admission import admit
from src.agentauth.chat_cancellation import CancelTurn, ChatCancellation
from src.agentauth.chat_capability import ChatAuthorizationRefusedError
from src.agentauth.chat_session_mailbox import AcceptedTurn, ChatSessionMailbox
from src.agentauth.workload import VerifiedPod
from tests.agentauth import test_chat_pre_admission_cleanup as cleanup
from tests.agentauth.test_external_roots import proof

client = cleanup.client
runtime = cleanup.runtime
store = cleanup.store
sts = cleanup.sts
retained_input_table = cleanup.retained_input_table
registered_owner = cleanup.registered_owner
transport = cleanup.transport
fixtures = cleanup.fixtures
recovery = cleanup.recovery
RESERVE = "/internal/v1/agent/chat/data/reserve"


@pytest.fixture
def unbound(runtime, transport, retained_input_table, registered_owner, sts, monkeypatch):
    recovery.supervisor(sts, monkeypatch)
    envelope = fixtures.history.prepare(runtime, message_id="run-write")
    run_hash = hashlib.sha256(b"run-write").hexdigest()
    pod = VerifiedPod(
        "original-created-uid",
        f"chat-turn-{run_hash[:12]}-{run_hash[12:32]}",
        "adp-gateway-agents",
        "adp-chat-sandbox",
        "127.0.0.1",
        image_digest=runtime[5].image_digest,
        run_hash=run_hash,
    )
    state = {"exited": False, "absent": False, "observations": []}
    monkeypatch.setattr(runtime[1].workloads, "_chat_sandbox", True)
    monkeypatch.setattr(runtime[1].workloads, "_digests", frozenset({pod.image_digest}))
    original_verify = runtime[1].workloads.verify_bound
    monkeypatch.setattr(
        runtime[1].workloads, "verify_bound", lambda **fields: pod if fields == {"name": pod.name, "uid": pod.uid} else original_verify(**fields)
    )

    def discover(**fields):
        assert fields == {"name": pod.name, "run_hash": run_hash, "image_digest": pod.image_digest}
        state["observations"].append("discover")
        return pod.uid if state["exited"] else None

    def exited(**fields):
        assert fields == {"name": pod.name, "uid": pod.uid, "run_hash": run_hash}
        state["observations"].append("exit")
        return pod.image_digest if state["exited"] else None

    monkeypatch.setattr(runtime[1].workloads, "find_exited_sandbox", discover)
    monkeypatch.setattr(runtime[1].workloads, "exited_sandbox_image", exited)
    monkeypatch.setattr(runtime[1].workloads, "is_absent", lambda **fields: fields == {"name": pod.name} and state["absent"])
    return pod, state, envelope


async def reserve(client, unbound, **changes):
    pod, _, envelope = unbound
    body = {"run_id": "run-write", "envelope_digest": envelope_digest(envelope), "image_digest": pod.image_digest, **changes}
    return await client.post(RESERVE, json=body, headers={"X-Adp-Producer-Proof": proof(envelope_digest(body))})


def creation(runtime):
    return runtime[1].store._read("CHAT-LAUNCH#run-write", "CREATION")


def execution(runtime):
    return fixtures.finalization.execution(runtime)


def admit_original(runtime, unbound, **changes):
    return admit(
        runtime[1], run_id="run-write", digest=envelope_digest(unbound[2]), pod=replace(unbound[0], **changes), team_id="team", now=runtime[-1]
    )


async def test_creation_permission_is_issued_once_then_only_the_original_pod_can_be_cleaned(client, runtime, unbound, transport):
    response = await reserve(client, unbound)
    assert response.status_code == 200, response.text
    assert response.json() == {
        "run_id": "run-write",
        "session_id": "session-a",
        "task_id": "task-a",
        "session_generation": fixtures.GENERATION,
        "session_mode": "ephemeral",
        "state": "create",
        "attempt": 1,
        "pod_name": unbound[0].name,
        "image_digest": unbound[0].image_digest,
    }
    saved = creation(runtime)
    assert execution(runtime)["chat_sandbox_creation"] == saved["binding"]
    assert "workload_binding" not in execution(runtime)
    assert (await reserve(client, unbound)).status_code == 409
    assert (await recovery.resume(client, runtime)).status_code == 409
    assert cleanup.receipt(runtime) is None
    assert "workload_binding" not in execution(runtime)
    unbound[1]["exited"] = True
    resumed = await recovery.resume(client, runtime)
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["state"] == "pre_admission_cleanup" and resumed.json()["sandbox_uid"] == unbound[0].uid
    assert execution(runtime)["workload_binding"] == {"S": unbound[0].uid}
    assert runtime[1].store._read(f"POD#{unbound[0].uid}", "BINDING")["invocation_id"] == {"S": "run-write"}
    assert (await cleanup.removal(client, runtime, unbound)).json()["removed"] is False
    unbound[1].update(absent=True)
    assert (await cleanup.removal(client, runtime, unbound)).json()["removed"] is True
    unbound[1]["observations"].clear()
    assert (await recovery.resume(client, runtime)).json()["removed"] is True
    assert not unbound[1]["observations"]
    assert (await reserve(client, unbound)).status_code == 409
    with pytest.raises(BootstrapRefusedError):
        admit_original(runtime, unbound)
    assert creation(runtime) == saved
    assert runtime[1].store._read("CHAT-LAUNCH#run-write", "LAUNCH") is None
    assert (await recovery.completion.complete(client, runtime)).status_code == 404
    assert recovery.completion.publication.session(transport)["threads"]["thread-a"]["processing_task_id"] == "task-a"
    assert transport.client.send_message.call_count == 1
    assert recovery.completion.publication.payload(transport)["status"] == "interrupted"


async def test_legitimate_reserved_creation_still_admits_without_waiting_for_exit(client, runtime, unbound):
    assert (await reserve(client, unbound)).status_code == 200
    launch = admit_original(runtime, unbound)
    assert launch.sandbox_uid == unbound[0].uid
    assert admit_original(runtime, unbound) == launch
    assert (await recovery.resume(client, runtime)).json()["state"] == "admitted"
    assert (await reserve(client, unbound)).status_code == 409
    assert not unbound[1]["observations"]


@pytest.mark.parametrize("created", [False, True])
async def test_persistent_launch_intent_can_resume_only_its_original_creation(client, runtime, unbound, monkeypatch, created):
    monkeypatch.setattr(runtime[1].workloads, "find_reserved_sandbox", lambda **_: unbound[0] if created else None)
    mailbox = ChatSessionMailbox(runtime[2])
    owner = ("tenant", "team", "human")
    mailbox.select_mode(session_id="session-a", owner=owner, mode="persistent", now=runtime[-1])
    mailbox.accept(session_id="session-a", owner=owner, turn=AcceptedTurn(turn_id="run-write", message=unbound[2]["message"]), now=runtime[-1])
    assert (await reserve(client, unbound)).status_code == 200
    saved = creation(runtime)
    for _restart in range(2):
        resumed = await recovery.resume(client, runtime)
        assert resumed.status_code == 200, resumed.text
        assert resumed.json()["state"] == "creation_reserved"
        assert resumed.json()["pod_name"] == unbound[0].name
        assert resumed.json()["session_mode"] == "persistent"
        assert resumed.json().get("sandbox_uid") == (unbound[0].uid if created else None)
        assert creation(runtime) == saved
        assert "workload_binding" not in execution(runtime)

    original = runtime[1].store.client.transact_write_items

    def cancelled(**request):
        runtime[1].store.client.update_item(
            TableName=runtime[1].store.table,
            Key={"pk": {"S": "TENANT#tenant"}, "sk": {"S": "EXEC#run-write"}},
            UpdateExpression="SET abort_command_id = :cancel",
            ExpressionAttributeValues={":cancel": {"S": "cancel-race"}},
        )
        return original(**request)

    monkeypatch.setattr(runtime[1].store.client, "transact_write_items", cancelled)
    assert (await recovery.resume(client, runtime)).status_code == 409
    assert creation(runtime) == saved


@pytest.mark.parametrize(
    "change", [{"name": "chat-turn-" + hashlib.sha256(b"run-write").hexdigest()[:12] + "-other"}, {"image_digest": "sha256:" + "b" * 64}]
)
async def test_admission_cannot_substitute_reserved_name_or_image(client, runtime, unbound, change):
    assert (await reserve(client, unbound)).status_code == 200
    with pytest.raises(ChatAuthorizationRefusedError):
        admit_original(runtime, unbound, **change)
    assert "workload_binding" not in execution(runtime)
    assert runtime[1].store._read("CHAT-LAUNCH#run-write", "LAUNCH") is None


async def test_binding_itself_cannot_overwrite_reserved_name(client, runtime, unbound):
    assert (await reserve(client, unbound)).status_code == 200
    with pytest.raises(BootstrapRefusedError):
        runtime[1].store.bind(
            invocation_id="run-write",
            digest=envelope_digest(unbound[2]),
            pod=replace(unbound[0], name=unbound[0].name + "x"),
            now=datetime.fromtimestamp(runtime[-1], UTC),
        )
    assert "workload_binding" not in execution(runtime)


@pytest.mark.parametrize("stage", ["reservation", "adoption"])
async def test_lost_transaction_response_never_reissues_create_permission(client, runtime, unbound, monkeypatch, stage):
    if stage == "adoption":
        assert (await reserve(client, unbound)).status_code == 200
        unbound[1]["exited"] = True
    protected = runtime[1].store
    original = protected.client.transact_write_items

    def lost(**kwargs):
        original(**kwargs)
        raise EndpointConnectionError(endpoint_url="https://storage.example.test")

    monkeypatch.setattr(protected.client, "transact_write_items", lost)
    result = await reserve(client, unbound) if stage == "reservation" else await recovery.resume(client, runtime)
    assert result.status_code == 503
    monkeypatch.setattr(protected.client, "transact_write_items", original)
    assert (await reserve(client, unbound)).status_code == 409
    result = await recovery.resume(client, runtime)
    assert result.status_code == (409 if stage == "reservation" else 200)
    assert creation(runtime) is not None
    assert (cleanup.receipt(runtime) is not None) is (stage == "adoption")


@pytest.mark.parametrize("stage", ["reservation", "adoption"])
@pytest.mark.parametrize("change", ["attempt", "abort", "binding", "dispatch", "launch", "reservation"])
async def test_concurrent_change_fences_creation_and_adoption(client, runtime, unbound, monkeypatch, stage, change):
    if stage == "adoption":
        assert (await reserve(client, unbound)).status_code == 200
        unbound[1]["exited"] = True
    protected = runtime[1].store
    original = protected.client.transact_write_items

    def race(**kwargs):
        if change in {"attempt", "abort", "binding"}:
            item = execution(runtime)
            field, value = {
                "attempt": ("current_attempt", {"N": "2"}),
                "abort": ("abort_command_id", {"S": "cancel"}),
                "binding": ("workload_binding", {"S": "foreign-pod"}),
            }[change]
            item[field] = value
        elif change == "dispatch":
            item = protected._read("INVOCATION#run-write", "DISPATCH")
            item["envelope_digest"] = {"S": "f" * 64}
        else:
            item = {"pk": {"S": "CHAT-LAUNCH#run-write"}, "sk": {"S": "LAUNCH" if change == "launch" else "CREATION"}}
        fixtures.write_protected(runtime, item)
        return original(**kwargs)

    monkeypatch.setattr(protected.client, "transact_write_items", race)
    response = await reserve(client, unbound) if stage == "reservation" else await recovery.resume(client, runtime)
    assert response.status_code == 409, response.text
    assert cleanup.receipt(runtime) is None
    assert "chat_pre_admission_cleanup" not in execution(runtime)
    assert protected._read(f"POD#{unbound[0].uid}", "BINDING") is None


@pytest.mark.parametrize("change", [{"tenant_id": "foreign"}, {"session_id": "foreign"}, {"attempt": 2}, {"pod_name": "foreign"}])
async def test_reservation_accepts_no_caller_selected_scope(client, runtime, unbound, change):
    assert (await reserve(client, unbound, **change)).status_code == 422
    assert creation(runtime) is None


@pytest.mark.parametrize("change", [{"run_id": "foreign"}, {"envelope_digest": "f" * 64}, {"image_digest": "sha256:" + "b" * 64}])
async def test_forged_root_or_unapproved_image_cannot_reserve(client, runtime, unbound, change):
    assert (await reserve(client, unbound, **change)).status_code == 404
    assert creation(runtime) is None


async def test_worker_cannot_reserve_sandbox(client, runtime, unbound, sts):
    sts["role"] = "worker"
    assert (await reserve(client, unbound)).status_code == 403
    assert creation(runtime) is None


@pytest.mark.parametrize("proof_kind", ["missing", "malformed", "unsigned", "wrong-body", "sts-refused"])
async def test_reservation_requires_verified_body_bound_proof(client, runtime, unbound, sts, proof_kind):
    body = {"run_id": "run-write", "envelope_digest": envelope_digest(unbound[2]), "image_digest": unbound[0].image_digest}
    digest = envelope_digest(body)
    headers = {
        "missing": {},
        "malformed": {"X-Adp-Producer-Proof": "invalid-proof"},
        "unsigned": {"X-Adp-Producer-Proof": proof(digest, signed=False)},
        "wrong-body": {"X-Adp-Producer-Proof": proof(envelope_digest({**body, "image_digest": "sha256:" + "b" * 64}))},
        "sts-refused": {"X-Adp-Producer-Proof": proof(digest)},
    }[proof_kind]
    if proof_kind == "sts-refused":
        sts["status"] = 403
    before = execution(runtime)
    assert (await client.post(RESERVE, json=body, headers=headers)).status_code == 403
    assert creation(runtime) is None
    assert execution(runtime) == before


@pytest.mark.parametrize("binding_field,value", [("tenant_id", "foreign-tenant"), ("personas", ["reviewer"])])
async def test_reservation_refuses_other_tenant_or_persona_supervision(client, runtime, unbound, monkeypatch, binding_field, value):
    bindings = json.loads(os.environ["ADP_MODEL_ROOT_BINDINGS"])
    bindings[0][binding_field] = value
    monkeypatch.setenv("ADP_MODEL_ROOT_BINDINGS", json.dumps(bindings))
    before = execution(runtime)
    assert (await reserve(client, unbound)).status_code == 404
    assert creation(runtime) is None
    assert execution(runtime) == before


async def test_expired_root_cannot_authorize_creation(client, runtime, unbound):
    protected = runtime[1].store
    grant = protected.authority.load_grant(principal="run-write#1", tenant_id="tenant")
    authority = protected._read("TENANT#tenant", f"AUTHORITY#{grant.authority.reference_id}")
    authority["expires_at"] = {"S": "2000-01-01T00:00:00Z"}
    fixtures.write_protected(runtime, authority)
    assert (await reserve(client, unbound)).status_code == 404
    assert creation(runtime) is None


async def test_foreign_pod_binding_cannot_be_adopted(client, runtime, unbound):
    assert (await reserve(client, unbound)).status_code == 200
    unbound[1]["exited"] = True
    fixtures.write_protected(runtime, {"pk": {"S": f"POD#{unbound[0].uid}"}, "sk": {"S": "BINDING"}, "invocation_id": {"S": "foreign"}})
    assert (await recovery.resume(client, runtime)).status_code == 404
    assert cleanup.receipt(runtime) is None
    assert "workload_binding" not in execution(runtime)


async def test_disappearing_pod_between_discovery_and_exit_does_not_establish_cleanup(client, runtime, unbound, monkeypatch):
    assert (await reserve(client, unbound)).status_code == 200
    unbound[1]["exited"] = True
    monkeypatch.setattr(runtime[1].workloads, "exited_sandbox_image", lambda **fields: None)
    assert (await recovery.resume(client, runtime)).status_code == 409
    assert cleanup.receipt(runtime) is None
    assert "workload_binding" not in execution(runtime)


async def test_cancelled_reservation_keeps_cleanup_ahead_of_terminal_delivery(client, runtime, unbound, transport):
    assert (await reserve(client, unbound)).status_code == 200
    service = ChatCancellation(runtime[1].store, runtime[2], transport.sessions)
    body = CancelTurn(session_id="session-a", task_id="task-a")
    service.cancel(body, service.resolve(body, "tenant", "human"))
    assert (await reserve(client, unbound)).status_code == 409
    assert (await recovery.resume(client, runtime)).status_code == 409
    assert "chat_queued_terminal" not in execution(runtime)
    assert "chat_pre_admission_terminal" not in execution(runtime)
    assert fixtures.outbox(runtime) is None
    unbound[1]["exited"] = True
    assert (await recovery.resume(client, runtime)).status_code == 200
    assert "abort_command_id" in execution(runtime)
    assert "chat_terminal" not in execution(runtime)
    transport.client.send_message.assert_not_called()
    unbound[1]["absent"] = True
    assert (await cleanup.removal(client, runtime, unbound)).json()["removed"] is True
    assert recovery.completion.publication.payload(transport)["status"] == "cancelled"
    assert execution(runtime)["status"] == {"S": "cancelled"}
    assert (await recovery.resume(client, runtime)).json()["removed"] is True
    assert transport.client.send_message.call_count == 1
    assert (await recovery.completion.complete(client, runtime)).status_code == 404
