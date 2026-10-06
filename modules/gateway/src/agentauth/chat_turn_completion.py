"""Commit owner-delivery proof and processing-lock release in one transaction."""

import hashlib
import json
import time
from decimal import Decimal

from boto3.dynamodb.types import TypeDeserializer
from botocore.exceptions import BotoCoreError, ClientError

from src.agentauth import chat_delivery
from src.agentauth.chat_admission import _encoded
from src.agentauth.chat_authority import ChatSessionLease
from src.agentauth.chat_capability import ChatAuthorizationRefusedError, ChatAuthorizationUnavailableError, ChatLaunchStore
from src.agentauth.chat_history_store import ChatHistoryStore
from src.agentauth.chat_history_write import ChatHistoryConflictError
from src.agentauth.chat_pending_handoff import finish_pending_handoff, prepare_pending_handoff, valid_queue_receipt
from src.agentauth.chat_teardown import ChatTeardown
from src.agentauth.chat_terminal_delivery import terminal_delivery_payload, verify_terminal_delivery
from src.agentauth.chat_turn_finalization import ChatTurnFinalizer
from src.orchestration.chat_data_migration import _owns_context_row


def complete_delivered_turn(authority, capabilities, body, *, now):
    store = authority.store
    if store._read(f"CHAT-LAUNCH#{body.run_id}", "LAUNCH") is None:
        from src.agentauth.chat_pre_admission_completion import complete_cleaned_turn

        return complete_cleaned_turn(authority, body, now=now)
    launch = capabilities.launches.load(body.run_id)
    execution = store._read(f"TENANT#{launch.tenant_id}", f"EXEC#{launch.run_id}") or {}
    if "chat_terminal" not in execution:
        raise ChatHistoryConflictError("chat terminal outcome required")
    writer = ChatTurnFinalizer(authority, ChatHistoryStore(authority.context_table, capabilities))
    terminal = writer.finalize(body, now=now)
    item = verify_terminal_delivery(store, execution, launch, terminal)

    def first_completion_checks():
        header = writer.history._get(launch.session_id, "header")
        if not header or not _owns_context_row(header, (launch.tenant_id, launch.team_id, launch.user_id)):
            raise ChatAuthorizationRefusedError("chat completion context owner changed")
        lease = ChatSessionLease.model_validate(header.get("chatLease"))
        if (lease.run_id, lease.sandbox_uid, lease.generation, lease.expires_at) != (
            launch.run_id,
            launch.sandbox_uid,
            launch.lease_generation,
            1,
        ):
            raise ChatAuthorizationRefusedError("chat completion lease changed")
        evidence = ChatTeardown(authority, capabilities.launches)
        return [
            {"ConditionCheck": evidence._unchanged(execution)},
            {"ConditionCheck": evidence._unchanged(ChatLaunchStore.item(launch))},
            writer._unchanged(header),
        ]

    binding = {field: terminal[field] for field in ("run_id", "session_id", "attempt", "lease_generation", "sandbox_uid")}
    return complete_owner_delivery(authority, terminal, item, binding, first_completion_checks, now=now)


def complete_owner_delivery(authority, terminal, item, binding, first_completion_checks, *, now):
    store = authority.store
    queue_receipt = (item or {}).get("queue_message_id", {}).get("S")
    if item is None or item.get("status") != {"S": "queued"} or not isinstance(queue_receipt, str) or not 1 <= len(queue_receipt) <= 128:
        raise ChatHistoryConflictError("chat terminal queue receipt required")
    document = json.loads(item["document"]["S"])
    delivery = chat_delivery.ChatDelivery.model_validate(document["delivery"])
    receipt = {
        **binding,
        "delivery_id": document["delivery_id"],
        "task_id": delivery.task_id,
        "session_generation": delivery.session_generation,
        "processing_lock_released": True,
        "input_acknowledgement_ready": True,
    }
    if "completion_receipt" in item or "completion_pending" in item:
        previous = TypeDeserializer().deserialize(item.get("completion_receipt", item.get("completion_pending")))
        completed_at = previous.get("completed_at") if isinstance(previous, dict) else None
        if (
            isinstance(completed_at, bool)
            or not isinstance(completed_at, int | Decimal)
            or int(completed_at) != completed_at
            or completed_at < terminal["finalized_at"]
            or previous.get("processing_lock_released") is not True
            or previous.get("input_acknowledgement_ready") is not True
            or previous != {**receipt, "completed_at": completed_at}
        ):
            raise ChatAuthorizationUnavailableError("chat completion receipt inconsistent")
        receipt = {**receipt, "completed_at": int(completed_at)}
        if "completion_receipt" in item:
            if "pending_handoff" in item and not valid_queue_receipt(item):
                raise ChatAuthorizationUnavailableError("chat pending queue receipt missing")
            return receipt
        _, _, sessions = chat_delivery.response_transport()
        return finish_pending_handoff(authority, item, receipt, delivery, sessions)
    payload = terminal_delivery_payload(document)
    sent = {
        "delivery_id": document["delivery_id"],
        "task_id": delivery.task_id,
        "digest": hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        "status": "sent",
    }
    _, _, sessions = chat_delivery.response_transport()
    if sessions is None:
        raise ChatAuthorizationUnavailableError("chat owner storage unavailable")
    try:
        checks = first_completion_checks()
        row = sessions.get_item(Key={"session_id": delivery.session_id}, ConsistentRead=True).get("Item", {})
        thread = row.get("threads", {}).get(delivery.thread_id, {})
        checked_at = int(time.time())
        if (
            row.get("owner_principal") != delivery.owner_principal
            or row.get("created_at") != delivery.session_generation
            or row.get("channel") != "webchat"
            or row.get("expires_at", 0) <= checked_at
            or thread.get("processing_task_id") != delivery.task_id
        ):
            raise ChatAuthorizationRefusedError("chat completion owner or task changed")
        if thread.get("terminal_delivery") != sent:
            raise ChatHistoryConflictError("chat owner delivery receipt required")
        evidence = ChatTeardown(authority, None)
        promotion = prepare_pending_handoff(authority, evidence, delivery, thread, now=now)
        pending = thread.get("pending_turns", {})
        messages = thread.get("messages", [])
        archived = row.get("completed_terminal_deliveries", {})
        if not isinstance(archived, dict) or document["delivery_id"] in archived:
            raise ChatAuthorizationUnavailableError("chat completed delivery archive inconsistent")
        receipt["completed_at"] = now
        update = evidence._unchanged(item)
        update["ConditionExpression"] += " AND attribute_not_exists(completion_receipt) AND attribute_not_exists(completion_pending)"
        update["UpdateExpression"] = (
            "SET completion_receipt = :completion" if promotion is None else "SET completion_pending = :completion, pending_handoff = :handoff"
        )
        update["ExpressionAttributeValues"][":completion"] = {"M": _encoded(receipt)}
        if promotion is not None:
            update["ExpressionAttributeValues"][":handoff"] = {"S": json.dumps(promotion["document"], sort_keys=True, separators=(",", ":"))}
        values = {
            ":owner": delivery.owner_principal,
            ":generation": delivery.session_generation,
            ":channel": "webchat",
            ":now": checked_at,
            ":task": delivery.task_id,
            ":sent": sent,
            ":next_task": "" if promotion is None else promotion["task_id"],
            ":archive": {**archived, document["delivery_id"]: {**sent, "status": "completed", "thread_id": delivery.thread_id}},
        }
        archive_condition = "attribute_not_exists(completed_terminal_deliveries)"
        if "completed_terminal_deliveries" in row:
            archive_condition = "completed_terminal_deliveries = :previous"
            values[":previous"] = archived
        message_condition = "attribute_not_exists(threads.#thread.messages)"
        if "messages" in thread:
            message_condition = "threads.#thread.messages = :messages"
            values[":messages"] = messages
        pending_condition = "attribute_not_exists(threads.#thread.pending_turns)"
        if "pending_turns" in thread:
            pending_condition = "threads.#thread.pending_turns = :pending"
            values[":pending"] = pending
        session_update = "SET threads.#thread.processing_task_id = :next_task, completed_terminal_deliveries = :archive"
        if promotion is not None:
            session_update += (
                ", threads.#thread.pending_turns = :remaining, threads.#thread.messages = :remaining_messages, "
                "threads.#thread.scheduled_turns = :scheduled"
            )
            values.update({":remaining": promotion["pending"], ":remaining_messages": promotion["messages"], ":scheduled": promotion["scheduled"]})
            if "scheduled_turns" in thread:
                pending_condition += " AND threads.#thread.scheduled_turns = :previous_scheduled"
                values[":previous_scheduled"] = thread["scheduled_turns"]
            else:
                pending_condition += " AND attribute_not_exists(threads.#thread.scheduled_turns)"
        store.client.transact_write_items(
            TransactItems=[
                *checks,
                {"Update": update},
                {
                    "Update": {
                        "TableName": sessions.name,
                        "Key": _encoded({"session_id": delivery.session_id}),
                        "UpdateExpression": session_update,
                        "ConditionExpression": (
                            "owner_principal = :owner AND created_at = :generation AND #channel = :channel AND expires_at > :now "
                            "AND threads.#thread.processing_task_id = :task AND threads.#thread.terminal_delivery = :sent AND "
                            + archive_condition
                            + " AND "
                            + message_condition
                            + " AND "
                            + pending_condition
                        ),
                        "ExpressionAttributeNames": {"#thread": delivery.thread_id, "#channel": "channel"},
                        "ExpressionAttributeValues": _encoded(values),
                    }
                },
                *(promotion["checks"] if promotion is not None else []),
            ]
        )
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") in {"TransactionCanceledException", "ConditionalCheckFailedException"}:
            raise ChatHistoryConflictError("chat completion changed; retry") from None
        raise ChatAuthorizationUnavailableError("chat completion unavailable") from None
    except BotoCoreError:
        raise ChatAuthorizationUnavailableError("chat completion unavailable") from None
    if promotion is not None:
        item.update(completion_pending={"M": _encoded(receipt)}, pending_handoff=update["ExpressionAttributeValues"][":handoff"])
        return finish_pending_handoff(authority, item, receipt, delivery, sessions)
    return receipt
