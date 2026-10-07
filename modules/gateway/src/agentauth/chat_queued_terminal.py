"""Owner-visible queued outcomes, without claiming sandbox cleanup or completion."""

import json
import re
from datetime import datetime

from botocore.exceptions import BotoCoreError, ClientError

from src.agentauth.bootstrap import envelope_digest
from src.agentauth.chat_capability import ChatAuthorizationRefusedError, ChatAuthorizationUnavailableError
from src.agentauth.chat_history_write import ChatHistoryConflictError
from src.agentauth.chat_teardown import ChatTeardown
from src.agentauth.chat_terminal_delivery import prepare_terminal_delivery, terminal_delivery_digest, verify_terminal_delivery
from src.agentauth.model_policy import canonical_json


def verify_cancellation(execution, delivery):
    try:
        attempt = int(execution["current_attempt"]["N"])
        digest = envelope_digest(
            {
                "operation": "chat.cancel",
                "run_id": delivery.run_id,
                "attempt": attempt,
                "tenant_id": delivery.tenant_id,
                "user_id": delivery.user_id,
                "session_id": delivery.session_id,
                "task_id": delivery.task_id,
            }
        )
        requested = datetime.fromisoformat(execution["abort_requested_at"]["S"])
        if (
            attempt < 1
            or requested.tzinfo is None
            or execution.get("abort_requested_attempt") != {"N": str(attempt)}
            or execution.get("abort_body_digest") != {"S": digest}
            or execution.get("abort_command_id") != {"S": "chat-cancel-" + digest}
        ):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise ChatHistoryConflictError("chat queued cancellation reconciliation required") from None


def queued_input_expired(execution, now):
    if "chat_user_turn" not in execution:
        return False
    try:
        reference = execution["chat_user_turn"]["M"]
        expiry = reference["expires_at"]["N"]
        if (
            set(reference) != {"input_digest", "message_digest", "expires_at"}
            or not re.fullmatch(r"[1-9][0-9]*", expiry)
            or any(not re.fullmatch(r"[0-9a-f]{64}", reference[field]["S"]) for field in ("input_digest", "message_digest"))
        ):
            raise ValueError
        return int(expiry) <= now
    except (KeyError, TypeError, ValueError):
        raise ChatAuthorizationUnavailableError("chat queued input expiry unavailable") from None


def queued_terminal(execution, delivery, now):
    cancelled = "abort_command_id" in execution
    if cancelled:
        verify_cancellation(execution, delivery)
    elif not queued_input_expired(execution, now):
        raise ChatHistoryConflictError("chat queued input has not expired")
    try:
        attempt = int(execution["current_attempt"]["N"])
        epoch = int(execution["current_credential_epoch"]["N"])
        if (
            attempt < 1
            or epoch < 1
            or type(now) is not int
            or now < 1
            or "workload_binding" in execution
            or "pod_name" in execution
            or "chat_terminal" in execution
        ):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise ChatHistoryConflictError("chat queued cancellation reconciliation required") from None
    return {
        "phase": "queued",
        "run_id": delivery.run_id,
        "session_id": delivery.session_id,
        "attempt": attempt,
        "credential_epoch": epoch,
        "outcome": "cancelled" if cancelled else "interrupted",
        "message_id": None,
        "terminal": True,
        "retryable": not cancelled,
        "automatic_replay_permitted": False,
        "accounting_status": "not_used",
        "cleanup_required": True,
        "finalized_at": now,
    }


def load_queued_terminal(execution, delivery):
    try:
        terminal = json.loads(execution["chat_queued_terminal"]["S"])
        if execution.get("status") != {"S": "cancelled" if terminal["outcome"] == "cancelled" else "completed"} or canonical_json(
            terminal
        ) != canonical_json(queued_terminal(execution, delivery, terminal["finalized_at"])):
            raise ValueError
        return terminal
    except (KeyError, TypeError, ValueError):
        raise ChatAuthorizationUnavailableError("chat queued cancellation receipt inconsistent") from None


def finalize_queued_turn(authority, capabilities, delivery, pointer, execution, *, now):
    store = authority.store
    if execution.get("envelope_digest") != pointer.get("envelope_digest"):
        raise ChatAuthorizationRefusedError("chat queued dispatch changed")
    evidence = ChatTeardown(authority, capabilities.launches)
    absent_launches = [
        {
            "ConditionCheck": {
                "TableName": store.table,
                "Key": {"pk": {"S": f"CHAT-LAUNCH#{delivery.run_id}"}, "sk": {"S": sort_key}},
                "ConditionExpression": "attribute_not_exists(pk)",
            }
        }
        for sort_key in ("LAUNCH", "CREATION")
    ]
    unchanged = evidence._unchanged(execution)
    unchanged["ConditionExpression"] += (
        " AND attribute_not_exists(workload_binding) AND attribute_not_exists(pod_name) AND attribute_not_exists(chat_terminal)"
        " AND attribute_not_exists(chat_sandbox_creation) AND attribute_not_exists(chat_pre_admission_cleanup)"
    )
    if "abort_command_id" not in execution:
        unchanged["ConditionExpression"] += " AND attribute_not_exists(abort_command_id)"
    if "chat_queued_terminal" in execution:
        terminal = load_queued_terminal(execution, delivery)
        outbox = verify_terminal_delivery(store, execution, delivery, terminal)
        transactions = [{"ConditionCheck": unchanged}, {"ConditionCheck": evidence._unchanged(outbox)}]
    else:
        if execution.get("status") != {"S": "pending"}:
            raise ChatAuthorizationRefusedError("chat queued execution changed")
        terminal = queued_terminal(execution, delivery, now)
        outbox = prepare_terminal_delivery(execution, delivery, terminal, None)
        if outbox is None:
            raise ChatAuthorizationRefusedError("chat queued delivery missing")
        unchanged["ConditionExpression"] += " AND attribute_not_exists(chat_queued_terminal) AND attribute_not_exists(chat_terminal_delivery_digest)"
        unchanged["UpdateExpression"] = "SET #terminal_status = :ended, chat_queued_terminal = :terminal, chat_terminal_delivery_digest = :digest"
        unchanged["ExpressionAttributeNames"]["#terminal_status"] = "status"
        unchanged["ExpressionAttributeValues"].update(
            {
                ":ended": {"S": "cancelled" if terminal["outcome"] == "cancelled" else "completed"},
                ":terminal": {"S": canonical_json(terminal).decode()},
                ":digest": terminal_delivery_digest(outbox),
            }
        )
        transactions = [
            {"Update": unchanged},
            {"Put": {"TableName": store.table, "Item": outbox, "ConditionExpression": "attribute_not_exists(pk)"}},
        ]
        from src.agentauth.chat_event_journal import terminal_event_writes

        transactions.extend(terminal_event_writes(authority, outbox, now=now))
    try:
        store.client.transact_write_items(TransactItems=[{"ConditionCheck": evidence._unchanged(pointer)}, *absent_launches, *transactions])
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") == "TransactionCanceledException":
            raise ChatHistoryConflictError("chat queued cancellation changed; retry") from None
        raise ChatAuthorizationUnavailableError("chat queued cancellation unavailable") from None
    except BotoCoreError:
        raise ChatAuthorizationUnavailableError("chat queued cancellation unavailable") from None
    return terminal
