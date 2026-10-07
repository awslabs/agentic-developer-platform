"""Immutable owner delivery intents, committed atomically with terminal outcomes."""

import json
from decimal import Decimal

from src.agentauth.bootstrap import envelope_digest
from src.agentauth.chat_capability import ChatAuthorizationRefusedError, ChatAuthorizationUnavailableError
from src.agentauth.chat_delivery import ChatDelivery


def prepare_terminal_delivery(execution, launch, terminal, reply):
    if "chat_delivery" not in execution:
        if "chat_terminal_delivery_digest" in execution:
            raise ChatAuthorizationUnavailableError("chat terminal delivery registration missing")
        return None
    try:
        delivery = ChatDelivery.model_validate_json(execution["chat_delivery"]["S"])
        if any(getattr(delivery, field) != getattr(launch, field) for field in ("run_id", "tenant_id", "user_id", "team_id", "session_id")):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise ChatAuthorizationRefusedError("chat terminal delivery owner mismatch") from None
    outcome = terminal["outcome"]
    if outcome == "completed":
        content = reply.get("content") if isinstance(reply, dict) else None
    elif outcome == "cancelled":
        content = "This turn was cancelled."
    elif outcome == "failed":
        content = "The assistant could not complete this turn."
    elif outcome == "interrupted":
        content = (
            "This turn was interrupted before a result was saved. You can retry it."
            if terminal["retryable"]
            else "This turn was interrupted. It was not replayed automatically; check the conversation before retrying."
        )
    else:
        raise ChatAuthorizationUnavailableError("chat terminal delivery outcome invalid")
    if not isinstance(content, str) or not content or len(content.encode("utf-8")) > 131_072:
        raise ChatAuthorizationUnavailableError("chat terminal delivery content invalid")
    document = {
        "version": 1,
        "delivery_id": "chat-terminal-" + envelope_digest(terminal),
        "delivery": delivery.model_dump(),
        "terminal": terminal,
        "content": content,
    }
    return {
        "pk": {"S": f"CHAT-DELIVERY#{launch.run_id}"},
        "sk": {"S": "TERMINAL"},
        "document": {"S": json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"))},
        "status": {"S": "pending"},
    }


def terminal_delivery_digest(item):
    return {"S": envelope_digest(json.loads(item["document"]["S"]))}


def terminal_delivery_payload(document):
    delivery = ChatDelivery.model_validate(document["delivery"])
    terminal = document["terminal"]
    return {
        "terminal_delivery": True,
        "strict_delivery": True,
        "channel": "webchat",
        "delivery_id": document["delivery_id"],
        "session_id": delivery.session_id,
        "session_generation": delivery.session_generation,
        "owner_principal": delivery.owner_principal,
        "thread_id": delivery.thread_id,
        "task_id": delivery.task_id,
        "status": terminal["outcome"],
        "retryable": terminal["retryable"],
        "accounting_status": terminal["accounting_status"],
        "text": document["content"],
    }


def verify_terminal_delivery(store, execution, launch, terminal):
    terminal = dict(terminal)
    for field in ("attempt", "lease_generation", "credential_epoch", "finalized_at"):
        if field not in terminal:
            continue
        value = terminal[field]
        if isinstance(value, bool) or not isinstance(value, int | Decimal) or int(value) != value or value < 1:
            raise ChatAuthorizationUnavailableError("chat terminal delivery receipt invalid")
        terminal[field] = int(value)
    if "chat_delivery" not in execution:
        return prepare_terminal_delivery(execution, launch, terminal, None)
    item = store._read(f"CHAT-DELIVERY#{launch.run_id}", "TERMINAL")
    try:
        document = json.loads(item["document"]["S"])
        expected = prepare_terminal_delivery(execution, launch, terminal, {"content": document["content"]})
        if document != json.loads(expected["document"]["S"]) or execution.get("chat_terminal_delivery_digest") != terminal_delivery_digest(item):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise ChatAuthorizationUnavailableError("chat terminal delivery inconsistent") from None
    return item
