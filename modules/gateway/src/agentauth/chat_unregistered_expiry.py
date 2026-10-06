"""Fence expired retained inputs with terminal-only records, never renewed grants."""

from datetime import UTC, datetime, timedelta

from src.agentauth.bootstrap import envelope_digest
from src.agentauth.chat_delivery import ChatDelivery, delivery_lookup_item, protected_delivery
from src.agentauth.chat_queued_terminal import queued_terminal
from src.agentauth.chat_terminal_delivery import prepare_terminal_delivery, terminal_delivery_digest
from src.agentauth.chat_user_turn import protected_user_turn
from src.agentauth.model_policy import canonical_json


def prepare_unregistered_expiry(authority, previous, selected, envelope, thread, *, now):
    created = datetime.fromisoformat(envelope["arrived_at"])
    if created.tzinfo is None or created.timestamp() <= 0:
        raise ValueError("invalid retained arrival")
    expiry = created + timedelta(hours=2)
    if expiry > datetime.fromtimestamp(now, UTC):
        return None
    metadata = {"chat_delivery": protected_delivery(envelope, previous.user_id)}
    delivery = ChatDelivery.model_validate_json(metadata["chat_delivery"]["S"])
    fields = ("session_id", "thread_id", "tenant_id", "user_id", "team_id", "owner_principal", "session_generation")
    markers = [message for message in thread["messages"] if message.get("pending_id") == selected]
    if (
        any(getattr(delivery, field) != getattr(previous, field) for field in fields)
        or delivery.run_id == previous.run_id
        or delivery.task_id == previous.task_id
        or thread.get("processing_task_id") != previous.task_id
        or selected in thread.get("scheduled_turns", {})
        or len(markers) != 1
        or markers[0].get("role") != "user"
        or markers[0].get("content") != envelope["message"][:10000]
        or not created.timestamp() - 30 <= float(markers[0]["timestamp"]) <= created.timestamp() + 900
    ):
        raise ValueError("expired retained owner binding changed")
    metadata["chat_user_turn"], _ = protected_user_turn(envelope, human_id=delivery.user_id, expires_at=int(expiry.timestamp()))
    execution = {
        "pk": {"S": f"TENANT#{delivery.tenant_id}"},
        "sk": {"S": f"EXEC#{delivery.run_id}"},
        "invocation_id": {"S": delivery.run_id},
        "tenant_id": {"S": delivery.tenant_id},
        "current_attempt": {"N": "1"},
        "current_credential_epoch": {"N": "1"},
        "min_acceptable_credential_epoch": {"N": "1"},
        "status": {"S": "completed"},
        "repo": {"S": envelope["source_ref"]["repo"]},
        "persona": {"S": envelope["persona"]},
        "arrived_at": {"S": envelope["arrived_at"]},
        "envelope_digest": {"S": envelope_digest(envelope)},
        "chat_unregistered_expiry": {"BOOL": True},
        **metadata,
    }
    pointer = {
        "pk": {"S": f"INVOCATION#{delivery.run_id}"},
        "sk": {"S": "DISPATCH"},
        "tenant_id": {"S": delivery.tenant_id},
        "envelope_digest": execution["envelope_digest"],
        "execution_metadata_digest": {"S": envelope_digest(metadata)},
        "arrived_at": execution["arrived_at"],
    }
    terminal = queued_terminal(execution, delivery, now)
    outbox = prepare_terminal_delivery(execution, delivery, terminal, None)
    execution.update(chat_queued_terminal={"S": canonical_json(terminal).decode()}, chat_terminal_delivery_digest=terminal_delivery_digest(outbox))
    transactions = [
        {"Put": {"TableName": authority.store.table, "Item": item, "ConditionExpression": "attribute_not_exists(pk)"}}
        for item in (execution, pointer, delivery_lookup_item(metadata, envelope, delivery.user_id), outbox)
    ]
    transactions.append(
        {
            "ConditionCheck": {
                "TableName": authority.store.table,
                "Key": {"pk": execution["pk"], "sk": {"S": f"GRANT#{delivery.run_id}#1"}},
                "ConditionExpression": "attribute_not_exists(pk)",
            }
        }
    )
    return delivery, pointer, execution, transactions
