"""Complete an owner-delivered turn without fabricating sandbox admission."""

import re

from src.agentauth.chat_admission import _encoded
from src.agentauth.chat_capability import ChatAuthorizationRefusedError
from src.agentauth.chat_delivery import load_registered_delivery
from src.agentauth.chat_pre_admission_terminal import load_pre_admission_terminal
from src.agentauth.chat_sandbox_creation import load_creation
from src.agentauth.chat_teardown import ChatTeardown
from src.agentauth.chat_terminal_delivery import verify_terminal_delivery
from src.agentauth.chat_turn_completion import complete_owner_delivery


def complete_cleaned_turn(authority, body, *, now):
    store = authority.store
    pointer = store._read(f"INVOCATION#{body.run_id}", "DISPATCH") or {}
    tenant = pointer.get("tenant_id", {}).get("S")
    if not tenant or pointer.get("envelope_digest") != {"S": body.envelope_digest}:
        raise ChatAuthorizationRefusedError("chat cleaned completion root changed")
    delivery = load_registered_delivery(authority, body.run_id, tenant)
    execution = store._read(f"TENANT#{tenant}", f"EXEC#{body.run_id}") or {}
    if "chat_pre_admission_terminal" not in execution:
        raise ChatAuthorizationRefusedError("chat cleaned terminal required")
    terminal = load_pre_admission_terminal(execution, delivery)
    teardown = store._read(f"CHAT-LAUNCH#{body.run_id}", "PRE-ADMISSION-TEARDOWN") or {}
    image = teardown.get("binding", {}).get("M", {}).get("image_digest", {}).get("S", "")
    binding = _encoded(
        {
            **{
                field: getattr(delivery, field)
                for field in ("run_id", "tenant_id", "user_id", "team_id", "session_id", "task_id", "session_generation")
            },
            "envelope_digest": body.envelope_digest,
            "pod_name": body.pod_name,
            "pod_uid": body.pod_uid,
            "image_digest": image,
            "attempt": terminal["attempt"],
            "credential_epoch": terminal["credential_epoch"],
            "observation_scope": authority.workloads.observation_scope,
        }
    )
    if (
        execution.get("repo") != {"S": f"chat/{delivery.session_id}"}
        or execution.get("envelope_digest") != pointer.get("envelope_digest")
        or execution.get("pod_name") != {"S": body.pod_name}
        or execution.get("workload_binding") != {"S": body.pod_uid}
        or execution.get("chat_pre_admission_cleanup") != {"M": binding}
        or teardown.get("binding") != {"M": binding}
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", image)
        or any(not re.fullmatch(r"[1-9][0-9]*", teardown.get(field, {}).get("N", "")) for field in ("exited_at", "removed_at"))
        or not int(teardown["exited_at"]["N"]) <= int(teardown["removed_at"]["N"]) <= terminal["finalized_at"] <= now
        or any(field in execution for field in ("chat_terminal", "chat_queued_terminal"))
    ):
        raise ChatAuthorizationRefusedError("chat cleaned completion evidence changed")
    creation = load_creation(authority, delivery, pointer, execution)
    evidence = ChatTeardown(authority, None)
    execution_check = evidence._unchanged(execution)
    execution_check["ConditionExpression"] += " AND attribute_not_exists(chat_terminal) AND attribute_not_exists(chat_queued_terminal)"
    pod = _encoded({"pk": f"POD#{body.pod_uid}", "sk": "BINDING", "invocation_id": body.run_id, "tenant_id": tenant, "attempt": terminal["attempt"]})
    checks = [
        {"ConditionCheck": evidence._unchanged(pointer)},
        {"ConditionCheck": execution_check},
        {"ConditionCheck": evidence._unchanged(teardown)},
        {"ConditionCheck": evidence._unchanged(pod)},
        {
            "ConditionCheck": {
                "TableName": store.table,
                "Key": _encoded({"pk": f"CHAT-LAUNCH#{body.run_id}", "sk": "LAUNCH"}),
                "ConditionExpression": "attribute_not_exists(pk)",
            }
        },
        {"ConditionCheck": evidence._unchanged(creation)}
        if creation is not None
        else {
            "ConditionCheck": {
                "TableName": store.table,
                "Key": _encoded({"pk": f"CHAT-LAUNCH#{body.run_id}", "sk": "CREATION"}),
                "ConditionExpression": "attribute_not_exists(pk)",
            }
        },
    ]
    item = verify_terminal_delivery(store, execution, delivery, terminal)
    receipt_binding = {
        **{field: terminal[field] for field in ("run_id", "session_id", "attempt", "credential_epoch", "sandbox_uid")},
        "phase": "pre_admission",
        "terminal": terminal,
    }
    return complete_owner_delivery(authority, terminal, item, receipt_binding, lambda: checks, now=now)
