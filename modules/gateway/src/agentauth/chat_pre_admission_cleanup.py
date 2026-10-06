"""Record positive exit/removal evidence without inventing a chat admission."""

import hashlib
import re

from botocore.exceptions import BotoCoreError, ClientError

from src.agentauth.chat_admission import _encoded
from src.agentauth.chat_capability import ChatAuthorizationRefusedError, ChatAuthorizationUnavailableError
from src.agentauth.chat_history_write import ChatHistoryConflictError
from src.agentauth.chat_pre_admission_terminal import prepare_cleaned_terminal
from src.agentauth.chat_teardown import ChatTeardown
from src.agentauth.chat_terminal_delivery import terminal_delivery_digest
from src.agentauth.model_policy import canonical_json


def recover_bound_cleanup(authority, delivery, pointer, execution, *, removed, now, creation=None):
    store = authority.store
    evidence = ChatTeardown(authority, None)
    run_id = delivery.run_id
    run_hash = hashlib.sha256(run_id.encode()).hexdigest()
    original = execution
    adopted = creation is not None and "workload_binding" not in execution
    if adopted:
        if removed or execution.get("status") != {"S": "pending"} or "chat_pre_admission_cleanup" in execution:
            raise ChatAuthorizationRefusedError("chat creation recovery fenced")
        identity = creation["binding"]["M"]
        observed_uid = authority.workloads.find_exited_sandbox(
            name=identity["pod_name"]["S"], run_hash=run_hash, image_digest=identity["image_digest"]["S"]
        )
        if observed_uid is None:
            raise ChatHistoryConflictError("chat reserved pod exit not confirmed")
        execution = {**execution, "status": {"S": "active"}, "workload_binding": {"S": observed_uid}}
    name = execution.get("pod_name", {}).get("S", "")
    uid = execution.get("workload_binding", {}).get("S", "")
    attempt = execution.get("current_attempt", {}).get("N", "")
    epoch = execution.get("current_credential_epoch", {}).get("N", "")
    if (
        execution.get("status", {}).get("S") not in ({"completed", "cancelled"} if "chat_pre_admission_terminal" in execution else {"active"})
        or execution.get("envelope_digest") != pointer.get("envelope_digest")
        or not re.fullmatch(rf"chat-turn-{run_hash[:12]}-[a-z0-9]{{1,20}}", name)
        or not re.fullmatch(r"[a-z0-9-]{8,128}", uid)
        or not re.fullmatch(r"[1-9][0-9]*", attempt)
        or not re.fullmatch(r"[1-9][0-9]*", epoch)
        or "chat_terminal" in execution
        or "chat_queued_terminal" in execution
    ):
        raise ChatHistoryConflictError("chat partial binding unavailable")
    expected_pod = _encoded({"pk": f"POD#{uid}", "sk": "BINDING", "invocation_id": run_id, "tenant_id": delivery.tenant_id, "attempt": int(attempt)})
    pod_binding = store._read(f"POD#{uid}", "BINDING")
    if (adopted and pod_binding is not None) or (not adopted and pod_binding != expected_pod):
        raise ChatAuthorizationRefusedError("chat original pod binding changed")
    key = _encoded({"pk": f"CHAT-LAUNCH#{run_id}", "sk": "PRE-ADMISSION-TEARDOWN"})
    previous = store._read(key["pk"]["S"], key["sk"]["S"])
    marker = execution.get("chat_pre_admission_cleanup")
    if (previous is None) != (marker is None) or (removed and previous is None):
        raise ChatAuthorizationRefusedError("chat pre-admission exit evidence required")
    image = previous.get("binding", {}).get("M", {}).get("image_digest", {}).get("S", "") if previous else None
    if previous is None:
        image = authority.workloads.exited_sandbox_image(name=name, uid=uid, run_hash=run_hash)
        if image is None:
            raise ChatHistoryConflictError("chat original pod exit not confirmed")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
        raise ChatAuthorizationRefusedError("chat cleanup image unavailable")
    if creation is not None and (creation["binding"]["M"]["pod_name"] != {"S": name} or creation["binding"]["M"]["image_digest"] != {"S": image}):
        raise ChatAuthorizationRefusedError("chat cleanup reservation changed")
    binding = _encoded(
        {
            **{
                field: getattr(delivery, field)
                for field in ("run_id", "tenant_id", "user_id", "team_id", "session_id", "task_id", "session_generation")
            },
            "envelope_digest": pointer["envelope_digest"]["S"],
            "pod_name": name,
            "pod_uid": uid,
            "image_digest": image,
            "attempt": int(attempt),
            "credential_epoch": int(epoch),
            "observation_scope": authority.workloads.observation_scope,
        }
    )
    if previous is not None and (
        previous.get("binding") != {"M": binding}
        or marker != {"M": binding}
        or not re.fullmatch(r"[1-9][0-9]*", previous.get("exited_at", {}).get("N", ""))
        or int(previous["exited_at"]["N"]) > now
        or (
            "removed_at" in previous
            and (
                not re.fullmatch(r"[1-9][0-9]*", previous["removed_at"].get("N", ""))
                or not int(previous["exited_at"]["N"]) <= int(previous["removed_at"]["N"]) <= now
            )
        )
    ):
        raise ChatAuthorizationRefusedError("chat pre-admission evidence changed")
    receipt = dict(previous) if previous else {**key, "binding": {"M": binding}, "exited_at": {"N": str(now)}}
    if removed and "removed_at" not in receipt and authority.workloads.is_absent(name=name):
        receipt["removed_at"] = {"N": str(now)}
    snapshot = evidence._unchanged(original)
    for field in ("chat_pre_admission_cleanup", "abort_command_id", "chat_terminal", "chat_queued_terminal", "chat_pre_admission_terminal"):
        if field not in execution:
            snapshot["ConditionExpression"] += f" AND attribute_not_exists({field})"
    if marker is None:
        snapshot["UpdateExpression"] = "SET chat_pre_admission_cleanup = :cleanup"
        snapshot["ExpressionAttributeValues"][":cleanup"] = {"M": binding}
    if adopted:
        snapshot["ConditionExpression"] += " AND attribute_not_exists(workload_binding)"
        snapshot["UpdateExpression"] += ", #recovered_status = :active, workload_binding = :uid"
        snapshot["ExpressionAttributeNames"]["#recovered_status"] = "status"
        snapshot["ExpressionAttributeValues"].update({":active": {"S": "active"}, ":uid": {"S": uid}})
    terminal_writes = []
    if "chat_pre_admission_terminal" in execution and "removed_at" not in receipt:
        raise ChatAuthorizationRefusedError("chat pre-admission terminal lacks removal")
    if "removed_at" in receipt:
        terminal, outbox = prepare_cleaned_terminal(store, execution, delivery, now)
        if not int(receipt["removed_at"]["N"]) <= terminal["finalized_at"] <= now:
            raise ChatAuthorizationRefusedError("chat pre-admission terminal precedes removal")
        if "chat_pre_admission_terminal" in execution:
            terminal_writes.append({"ConditionCheck": evidence._unchanged(outbox)})
        else:
            snapshot["ConditionExpression"] += " AND attribute_not_exists(chat_terminal_delivery_digest)"
            snapshot["UpdateExpression"] = (
                snapshot.get("UpdateExpression", "")
                + (", " if "UpdateExpression" in snapshot else "SET ")
                + ("#terminal_status = :terminal_status, chat_pre_admission_terminal = :terminal, chat_terminal_delivery_digest = :terminal_digest")
            )
            snapshot["ExpressionAttributeNames"]["#terminal_status"] = "status"
            snapshot["ExpressionAttributeValues"].update(
                {
                    ":terminal_status": {"S": "cancelled" if terminal["outcome"] == "cancelled" else "completed"},
                    ":terminal": {"S": canonical_json(terminal).decode()},
                    ":terminal_digest": terminal_delivery_digest(outbox),
                }
            )
            terminal_writes.append({"Put": {"TableName": store.table, "Item": outbox, "ConditionExpression": "attribute_not_exists(pk)"}})
    put = {"TableName": store.table, "Item": receipt, "ConditionExpression": "attribute_not_exists(pk)"}
    if previous is not None:
        put.update({field: value for field, value in evidence._unchanged(previous).items() if field != "Key"})
        if "removed_at" not in previous:
            put["ConditionExpression"] += " AND attribute_not_exists(removed_at)"
    try:
        store.client.transact_write_items(
            TransactItems=[
                {"ConditionCheck": evidence._unchanged(pointer)},
                {"Put": {"TableName": store.table, "Item": expected_pod, "ConditionExpression": "attribute_not_exists(pk)"}}
                if adopted
                else {"ConditionCheck": evidence._unchanged(pod_binding)},
                {"Update" if "UpdateExpression" in snapshot else "ConditionCheck": snapshot},
                *([{"ConditionCheck": evidence._unchanged(creation)}] if creation is not None else []),
                {
                    "ConditionCheck": {
                        "TableName": store.table,
                        "Key": _encoded({"pk": f"CHAT-LAUNCH#{run_id}", "sk": "LAUNCH"}),
                        "ConditionExpression": "attribute_not_exists(pk)",
                    }
                },
                {"Put": put},
                *terminal_writes,
            ]
        )
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") == "TransactionCanceledException":
            raise ChatHistoryConflictError("chat pre-admission cleanup changed; retry") from None
        raise ChatAuthorizationUnavailableError("chat pre-admission cleanup unavailable") from None
    except BotoCoreError:
        raise ChatAuthorizationUnavailableError("chat pre-admission cleanup unavailable") from None
    return {
        "state": "pre_admission_cleanup",
        "pod_name": name,
        "sandbox_uid": uid,
        "image_digest": image,
        "attempt": int(attempt),
        "removed": "removed_at" in receipt,
    }
