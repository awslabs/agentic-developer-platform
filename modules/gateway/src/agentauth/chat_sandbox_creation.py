"""Single-use creation reservations; an uncertain reservation is never reissued."""

import hashlib
import re
from datetime import UTC, datetime

from botocore.exceptions import BotoCoreError, ClientError

from src.agentauth.chat_admission import _encoded, root_identity
from src.agentauth.chat_capability import ChatAuthorizationRefusedError, ChatAuthorizationUnavailableError
from src.agentauth.chat_history_write import ChatHistoryConflictError
from src.agentauth.chat_teardown import ChatTeardown


def creation_binding(delivery, pointer, execution, image):
    if execution.get("envelope_digest") != pointer.get("envelope_digest"):
        raise ChatAuthorizationRefusedError("chat creation dispatch changed")
    run_hash = hashlib.sha256(delivery.run_id.encode()).hexdigest()
    attempt = execution.get("current_attempt", {}).get("N", "")
    epoch = execution.get("current_credential_epoch", {}).get("N", "")
    if not re.fullmatch(r"[1-9][0-9]*", attempt) or not re.fullmatch(r"[1-9][0-9]*", epoch) or not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
        raise ChatAuthorizationRefusedError("chat creation scope invalid")
    return _encoded(
        {
            **{
                field: getattr(delivery, field)
                for field in ("run_id", "tenant_id", "user_id", "team_id", "session_id", "task_id", "session_generation")
            },
            "envelope_digest": pointer["envelope_digest"]["S"],
            "pod_name": f"chat-turn-{run_hash[:12]}-{run_hash[12:32]}",
            "image_digest": image,
            "attempt": int(attempt),
            "credential_epoch": int(epoch),
        }
    )


def load_creation(authority, delivery, pointer, execution):
    item = authority.store._read(f"CHAT-LAUNCH#{delivery.run_id}", "CREATION")
    marker = execution.get("chat_sandbox_creation")
    if item is None and marker is None:
        return None
    image = (item or {}).get("binding", {}).get("M", {}).get("image_digest", {}).get("S", "")
    binding = {"M": creation_binding(delivery, pointer, execution, image)}
    if item is None or item.get("binding") != binding or marker != binding or execution.get("pod_name") != binding["M"]["pod_name"]:
        raise ChatAuthorizationRefusedError("chat creation reservation changed")
    return item


def reserve_creation(authority, delivery, pointer, execution, image, *, now):
    store = authority.store
    absent = (
        "workload_binding",
        "pod_name",
        "abort_command_id",
        "chat_terminal",
        "chat_queued_terminal",
        "chat_pre_admission_cleanup",
        "chat_sandbox_creation",
    )
    if execution.get("status") != {"S": "pending"} or any(field in execution for field in absent):
        raise ChatHistoryConflictError("chat creation already reserved or fenced")
    if not authority.workloads.approved_sandbox_image(image):
        raise ChatAuthorizationRefusedError("chat creation image refused")
    _, grant, _ = root_identity(authority, delivery.run_id, pointer["envelope_digest"]["S"], now)
    binding = creation_binding(delivery, pointer, execution, image)
    evidence = ChatTeardown(authority, None)
    update = evidence._unchanged(execution)
    update["ConditionExpression"] += "".join(f" AND attribute_not_exists({field})" for field in absent)
    update["UpdateExpression"] = "SET pod_name = :pod_name, chat_sandbox_creation = :creation"
    update["ExpressionAttributeValues"].update({":pod_name": binding["pod_name"], ":creation": {"M": binding}})
    key = _encoded({"pk": f"CHAT-LAUNCH#{delivery.run_id}", "sk": "CREATION"})
    try:
        store.client.transact_write_items(
            TransactItems=[
                {"ConditionCheck": evidence._unchanged(pointer)},
                {"Update": update},
                store._authority_check(grant),
                store._grant_check(grant, datetime.fromtimestamp(now, UTC)),
                {
                    "ConditionCheck": {
                        "TableName": store.table,
                        "Key": _encoded({"pk": f"CHAT-LAUNCH#{delivery.run_id}", "sk": "LAUNCH"}),
                        "ConditionExpression": "attribute_not_exists(pk)",
                    }
                },
                {"Put": {"TableName": store.table, "Item": {**key, "binding": {"M": binding}}, "ConditionExpression": "attribute_not_exists(pk)"}},
            ]
        )
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") == "TransactionCanceledException":
            raise ChatHistoryConflictError("chat creation reservation changed; observe instead") from None
        raise ChatAuthorizationUnavailableError("chat creation reservation unavailable") from None
    except BotoCoreError:
        raise ChatAuthorizationUnavailableError("chat creation reservation uncertain; observe instead") from None
    return {"state": "create", "pod_name": binding["pod_name"]["S"], "image_digest": image, "attempt": int(binding["attempt"]["N"])}


def admission_creation_check(authority, run_id, tenant_id, pod):
    from src.agentauth.chat_delivery import load_registered_delivery

    store = authority.store
    key = _encoded({"pk": f"CHAT-LAUNCH#{run_id}", "sk": "CREATION"})
    execution = store._read(f"TENANT#{tenant_id}", f"EXEC#{run_id}") or {}
    if store._read(key["pk"]["S"], key["sk"]["S"]) is None and "chat_sandbox_creation" not in execution:
        return {"ConditionCheck": {"TableName": store.table, "Key": key, "ConditionExpression": "attribute_not_exists(pk)"}}
    delivery = load_registered_delivery(authority, run_id, tenant_id)
    pointer = store._read(f"INVOCATION#{run_id}", "DISPATCH") or {}
    creation = load_creation(authority, delivery, pointer, execution)
    binding = creation["binding"]["M"]
    if binding["pod_name"] != {"S": pod.name} or binding["image_digest"] != {"S": pod.image_digest}:
        raise ChatAuthorizationRefusedError("chat admission creation mismatch")
    return {"ConditionCheck": ChatTeardown(authority, None)._unchanged(creation)}
