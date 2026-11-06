"""A reserved sandbox pod that never started is cleaned from its absence; an admitted one never is (#8061)."""

import pytest
from botocore.exceptions import EndpointConnectionError

from src.agentauth.chat_delivery import load_registered_delivery
from src.agentauth.chat_history_write import ChatHistoryConflictError
from src.agentauth.chat_pre_admission_cleanup import never_started_uid, recover_bound_cleanup
from tests.agentauth import test_chat_sandbox_creation as reservation

client = reservation.client
runtime = reservation.runtime
store = reservation.store
sts = reservation.sts
retained_input_table = reservation.retained_input_table
registered_owner = reservation.registered_owner
transport = reservation.transport
fixtures = reservation.fixtures
recovery = reservation.recovery
cleanup = reservation.cleanup
unbound = reservation.unbound
reserve = reservation.reserve
creation = reservation.creation
execution = reservation.execution
SUPPLIED_UID = "01234567-89ab-cdef-0123-456789abcdef"


def recover_direct(runtime, **changes):
    authority = runtime[1]
    return recover_bound_cleanup(
        authority,
        load_registered_delivery(authority, "run-write", "tenant"),
        authority.store._read("INVOCATION#run-write", "DISPATCH"),
        execution(runtime),
        removed=False,
        now=runtime[-1],
        creation=creation(runtime),
        **changes,
    )


def pod_binding(runtime, uid):
    return runtime[1].store._read(f"POD#{uid}", "BINDING")


async def test_never_started_absent_pod_is_cleaned_from_absence_with_a_stable_sentinel(client, runtime, unbound, transport):
    pod, state, _ = unbound
    assert (await reserve(client, unbound)).status_code == 200
    saved = creation(runtime)
    # Still Pending (image pulling): neither exit nor absence is observable, so nothing changes.
    assert (await recovery.resume(client, runtime)).status_code == 409
    assert cleanup.receipt(runtime) is None and "workload_binding" not in execution(runtime)
    assert state["observations"] == ["discover"]
    # Deleted by the supervisor and garbage-collected: the run was never admitted, so absence suffices.
    state.update(absent=True)
    state["observations"].clear()
    resumed = await recovery.resume(client, runtime)
    assert resumed.status_code == 200, resumed.text
    sentinel = never_started_uid(saved)
    assert sentinel.startswith("never-started-") and sentinel != pod.uid
    assert resumed.json() == {
        "state": "pre_admission_cleanup",
        "run_id": "run-write",
        "session_id": "session-a",
        "task_id": "task-a",
        "session_generation": fixtures.GENERATION,
        "pod_name": pod.name,
        "sandbox_uid": sentinel,
        "image_digest": pod.image_digest,
        "attempt": 1,
        "removed": False,
    }
    receipt = cleanup.receipt(runtime)
    assert receipt["exit_evidence"] == {"S": "pre_admission_absence"}
    assert receipt["exited_at"] == {"N": str(runtime[-1])} and "removed_at" not in receipt
    assert receipt["binding"]["M"]["pod_uid"] == {"S": sentinel} and receipt["binding"]["M"]["image_digest"] == {"S": pod.image_digest}
    assert execution(runtime)["workload_binding"] == {"S": sentinel} and execution(runtime)["chat_pre_admission_cleanup"] == receipt["binding"]
    assert pod_binding(runtime, sentinel)["invocation_id"] == {"S": "run-write"}
    # The image comes from the reservation, never from an exit observation the pod cannot give.
    assert state["observations"] == ["discover"]
    # A redelivery that no longer knows the uid derives the same sentinel and the same receipt.
    state["observations"].clear()
    again = await recovery.resume(client, runtime)
    assert again.status_code == 200 and again.json()["sandbox_uid"] == sentinel and cleanup.receipt(runtime) == receipt
    assert not state["observations"]
    # Downstream is the ordinary pre-admission cleanup: removal, interrupted terminal, no second sandbox.
    assert (await cleanup.removal(client, runtime, unbound, pod_uid=sentinel)).json() == {"run_id": "run-write", "pod_uid": sentinel, "removed": True}
    assert (await recovery.resume(client, runtime)).json()["removed"] is True
    assert (await reserve(client, unbound)).status_code == 409
    assert creation(runtime) == saved
    assert runtime[1].store._read("CHAT-LAUNCH#run-write", "LAUNCH") is None
    payload = fixtures.replay(runtime)["events"][0]["payload"]
    assert payload["status"] == "interrupted" and payload["automatic_replay_permitted"] is False
    assert transport.client.send_message.call_count == 1
    assert recovery.completion.publication.payload(transport)["status"] == "interrupted"


async def test_supervisor_named_pod_uid_is_recorded_when_the_pod_is_absent(client, runtime, unbound):
    pod, state, _ = unbound
    assert (await reserve(client, unbound)).status_code == 200
    state["absent"] = True
    # Only the reserved pod may be named, and only with a uid the cleanup binding can carry.
    assert (await recovery.resume(client, runtime, pod_name="chat-turn-ffffffffffff-abcde", pod_uid=SUPPLIED_UID)).status_code == 404
    assert (await recovery.resume(client, runtime, pod_name=pod.name, pod_uid="UPPER-CASE-UID")).status_code == 404
    assert cleanup.receipt(runtime) is None and "workload_binding" not in execution(runtime)
    resumed = await recovery.resume(client, runtime, pod_name=pod.name, pod_uid=SUPPLIED_UID)
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["state"] == "pre_admission_cleanup" and resumed.json()["sandbox_uid"] == SUPPLIED_UID
    assert execution(runtime)["workload_binding"] == {"S": SUPPLIED_UID}
    assert cleanup.receipt(runtime)["exit_evidence"] == {"S": "pre_admission_absence"}
    assert pod_binding(runtime, SUPPLIED_UID)["invocation_id"] == {"S": "run-write"}
    assert pod_binding(runtime, never_started_uid(creation(runtime))) is None
    # A later redelivery without the uid keeps what was recorded.
    assert (await recovery.resume(client, runtime)).json()["sandbox_uid"] == SUPPLIED_UID
    assert (await cleanup.removal(client, runtime, unbound, pod_uid=SUPPLIED_UID)).json()["removed"] is True


async def test_observed_exit_outranks_the_supervisor_named_uid(client, runtime, unbound):
    pod, state, _ = unbound
    assert (await reserve(client, unbound)).status_code == 200
    state.update(exited=True, absent=True)
    resumed = await recovery.resume(client, runtime, pod_name=pod.name, pod_uid=SUPPLIED_UID)
    assert resumed.status_code == 200 and resumed.json()["sandbox_uid"] == pod.uid
    assert "exit_evidence" not in cleanup.receipt(runtime)
    assert pod_binding(runtime, SUPPLIED_UID) is None


async def test_admitted_run_keeps_requiring_exit_evidence_when_the_pod_is_absent(client, runtime, unbound):
    pod, state, _ = unbound
    assert (await reserve(client, unbound)).status_code == 200
    launch = reservation.admit_original(runtime, unbound)
    assert launch.sandbox_uid == pod.uid
    state["absent"] = True
    with pytest.raises(ChatHistoryConflictError, match="exit not confirmed"):
        recover_direct(runtime)
    assert cleanup.receipt(runtime) is None
    assert (await recovery.resume(client, runtime)).json()["state"] == "admitted"


async def test_launch_without_execution_binding_still_refuses_absence_as_evidence(client, runtime, unbound):
    pod, state, _ = unbound
    assert (await reserve(client, unbound)).status_code == 200
    state["absent"] = True
    fixtures.write_protected(runtime, {"pk": {"S": "CHAT-LAUNCH#run-write"}, "sk": {"S": "LAUNCH"}})
    with pytest.raises(ChatHistoryConflictError, match="reserved pod exit not confirmed"):
        recover_direct(runtime, pod_uid=SUPPLIED_UID)
    assert cleanup.receipt(runtime) is None and "workload_binding" not in execution(runtime)
    assert pod_binding(runtime, SUPPLIED_UID) is None


@pytest.mark.parametrize("change", ["launch", "binding", "storage"])
async def test_admission_racing_the_absence_cleanup_fences_it(client, runtime, unbound, monkeypatch, change):
    pod, state, _ = unbound
    assert (await reserve(client, unbound)).status_code == 200
    state["absent"] = True
    protected = runtime[1].store
    original = protected.client.transact_write_items

    def transact(**kwargs):
        if any(item.get("Put", {}).get("Item", {}).get("sk") == {"S": "PRE-ADMISSION-TEARDOWN"} for item in kwargs["TransactItems"]):
            if change == "storage":
                raise EndpointConnectionError(endpoint_url="https://storage.example.test")
            if change == "launch":
                fixtures.write_protected(runtime, {"pk": {"S": "CHAT-LAUNCH#run-write"}, "sk": {"S": "LAUNCH"}})
            else:
                item = execution(runtime)
                item["workload_binding"] = {"S": pod.uid}
                fixtures.write_protected(runtime, item)
        return original(**kwargs)

    monkeypatch.setattr(protected.client, "transact_write_items", transact)
    assert (await recovery.resume(client, runtime, pod_name=pod.name, pod_uid=SUPPLIED_UID)).status_code == (503 if change == "storage" else 409)
    assert cleanup.receipt(runtime) is None
    assert pod_binding(runtime, SUPPLIED_UID) is None
