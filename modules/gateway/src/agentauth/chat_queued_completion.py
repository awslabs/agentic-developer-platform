"""Complete owner-delivered queued outcomes that never received creation authority."""

from src.agentauth.chat_admission import _encoded
from src.agentauth.chat_capability import ChatAuthorizationRefusedError
from src.agentauth.chat_queued_terminal import load_queued_terminal
from src.agentauth.chat_teardown import ChatTeardown
from src.agentauth.chat_terminal_delivery import verify_terminal_delivery
from src.agentauth.chat_turn_completion import complete_owner_delivery


def complete_queued_turn(authority, delivery, pointer, *, now):
    store = authority.store
    execution = store._read(f"TENANT#{delivery.tenant_id}", f"EXEC#{delivery.run_id}") or {}
    absent = (
        "workload_binding",
        "pod_name",
        "chat_sandbox_creation",
        "chat_pre_admission_cleanup",
        "chat_pre_admission_terminal",
        "chat_terminal",
    )
    if (
        execution.get("envelope_digest") != pointer.get("envelope_digest")
        or execution.get("repo") != {"S": f"chat/{delivery.session_id}"}
        or any(field in execution for field in absent)
    ):
        raise ChatAuthorizationRefusedError("chat queued completion scope changed")
    terminal = load_queued_terminal(execution, delivery)
    if terminal["finalized_at"] > now:
        raise ChatAuthorizationRefusedError("chat queued completion clock invalid")
    evidence = ChatTeardown(authority, None)
    unchanged = evidence._unchanged(execution)
    unchanged["ConditionExpression"] += "".join(f" AND attribute_not_exists({field})" for field in absent)
    checks = [{"ConditionCheck": evidence._unchanged(pointer)}, {"ConditionCheck": unchanged}]
    for sort_key in ("CREATION", "LAUNCH", "PRE-ADMISSION-TEARDOWN", "TEARDOWN"):
        if store._read(f"CHAT-LAUNCH#{delivery.run_id}", sort_key) is not None:
            raise ChatAuthorizationRefusedError("chat queued creation fence unavailable")
        checks.append(
            {
                "ConditionCheck": {
                    "TableName": store.table,
                    "Key": _encoded({"pk": f"CHAT-LAUNCH#{delivery.run_id}", "sk": sort_key}),
                    "ConditionExpression": "attribute_not_exists(pk)",
                }
            }
        )
    item = verify_terminal_delivery(store, execution, delivery, terminal)
    binding = {
        **{field: terminal[field] for field in ("run_id", "session_id", "attempt", "credential_epoch")},
        "phase": "queued",
        "terminal": terminal,
        "creation_fenced": True,
    }
    return complete_owner_delivery(authority, terminal, item, binding, lambda: checks, now=now)
