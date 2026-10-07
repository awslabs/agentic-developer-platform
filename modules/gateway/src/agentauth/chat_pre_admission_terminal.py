"""Owner outcomes for removed sandboxes that never acquired chat admission."""

import json

from src.agentauth.chat_capability import ChatAuthorizationRefusedError, ChatAuthorizationUnavailableError
from src.agentauth.chat_queued_terminal import verify_cancellation
from src.agentauth.chat_terminal_delivery import prepare_terminal_delivery, verify_terminal_delivery
from src.agentauth.model_policy import canonical_json


def pre_admission_terminal(execution, delivery, now):
    cancelled = "abort_command_id" in execution
    if cancelled:
        verify_cancellation(execution, delivery)
    try:
        attempt = int(execution["current_attempt"]["N"])
        epoch = int(execution["current_credential_epoch"]["N"])
        uid = execution["workload_binding"]["S"]
        if attempt < 1 or epoch < 1 or not uid or type(now) is not int or now < 1:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise ChatAuthorizationRefusedError("chat pre-admission terminal scope invalid") from None
    return {
        "phase": "pre_admission",
        "run_id": delivery.run_id,
        "session_id": delivery.session_id,
        "attempt": attempt,
        "credential_epoch": epoch,
        "sandbox_uid": uid,
        "outcome": "cancelled" if cancelled else "interrupted",
        "message_id": None,
        "terminal": True,
        "retryable": not cancelled,
        "automatic_replay_permitted": False,
        "accounting_status": "not_used",
        "cleanup_required": False,
        "finalized_at": now,
    }


def load_pre_admission_terminal(execution, delivery):
    try:
        terminal = json.loads(execution["chat_pre_admission_terminal"]["S"])
        if execution.get("status") != {"S": "cancelled" if terminal["outcome"] == "cancelled" else "completed"} or canonical_json(
            terminal
        ) != canonical_json(pre_admission_terminal(execution, delivery, terminal["finalized_at"])):
            raise ValueError
        return terminal
    except (KeyError, TypeError, ValueError):
        raise ChatAuthorizationUnavailableError("chat pre-admission terminal inconsistent") from None


def prepare_cleaned_terminal(store, execution, delivery, now):
    if "chat_pre_admission_terminal" in execution:
        terminal = load_pre_admission_terminal(execution, delivery)
        outbox = verify_terminal_delivery(store, execution, delivery, terminal)
    else:
        if execution.get("status") != {"S": "active"}:
            raise ChatAuthorizationRefusedError("chat pre-admission execution changed")
        terminal = pre_admission_terminal(execution, delivery, now)
        outbox = prepare_terminal_delivery(execution, delivery, terminal, None)
    if outbox is None:
        raise ChatAuthorizationRefusedError("chat pre-admission delivery missing")
    return terminal, outbox
