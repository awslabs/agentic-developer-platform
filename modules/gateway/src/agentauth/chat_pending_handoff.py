"""Fenced promotion and retryable publication of registered owner follow-ups."""

import hashlib
import json
import os
import re
from functools import lru_cache

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from src.agentauth.bootstrap import envelope_digest
from src.agentauth.chat_admission import _encoded
from src.agentauth.chat_capability import ChatAuthorizationRefusedError, ChatAuthorizationUnavailableError
from src.agentauth.chat_delivery import ChatDelivery, load_registered_delivery, verify_delivery_session
from src.agentauth.chat_history_write import ChatHistoryConflictError
from src.agentauth.chat_pending_registration import retry_pending_registration
from src.agentauth.chat_queued_terminal import load_queued_terminal, verify_cancellation
from src.agentauth.chat_unregistered_expiry import prepare_unregistered_expiry
from src.agentauth.model_policy import canonical_json


@lru_cache(maxsize=1)
def input_transport():
    queue = os.environ.get("ADP_CHAT_INPUT_QUEUE_URL", "")
    match = re.fullmatch(r"https://sqs\.([a-z0-9-]+)\.amazonaws\.com(?:\.cn)?/[0-9]{12}/[A-Za-z0-9_-]+\.fifo", queue)
    if not match:
        raise ChatAuthorizationUnavailableError("chat input transport unconfigured")
    client = boto3.client("sqs", region_name=match[1], config=Config(connect_timeout=3, read_timeout=5, retries={"total_max_attempts": 1}))
    return client, queue


def _intent_digest(request):
    return envelope_digest({key: value for key, value in request.items() if key not in {"task_id", "enqueued_at", "arrived_at", "connection_id"}})


def prepare_pending_handoff(authority, evidence, delivery, thread, *, now):
    pending, messages = thread.get("pending_turns", {}), thread.get("messages", [])
    if not isinstance(pending, dict) or not isinstance(messages, list) or any(not isinstance(message, dict) for message in messages):
        raise ChatHistoryConflictError("chat pending input reconciliation required")
    users = [message for message in messages if message.get("role") == "user"]
    if not pending and not users:
        return None
    identifiers = [message.get("pending_id") for message in users]
    if not identifiers or any(not isinstance(identifier, str) for identifier in identifiers):
        raise ChatHistoryConflictError("chat pending input reconciliation required")
    if len(set(identifiers)) != len(identifiers) or set(identifiers) != set(pending):
        raise ChatHistoryConflictError("chat pending input reconciliation required")
    try:
        selected = identifiers[0]
        record = pending[selected]
        request = json.loads(record["request_json"])
        run_id = request["message_id"]
        if selected != hashlib.sha256(run_id.encode()).hexdigest() or record["status"] not in {"registering", "registered"}:
            raise ValueError
        envelope = {
            **request,
            "persona": request["agent_type"],
            "source_ref": {**request.get("source_ref", {}), "repo": f"chat/{delivery.session_id}"},
            "correlation": {"correlation_id": run_id, "root_human_id": delivery.user_id, "is_human_rooted": True},
        }
        envelope.pop("parent_principal", None)
        envelope.pop("session_mode", None)
        pointer = authority.store._read(f"INVOCATION#{run_id}", "DISPATCH") or {}
        if pointer and pointer.get("envelope_digest") != {"S": envelope_digest(envelope)}:
            for mode in ("ephemeral", "persistent"):
                candidate = {**envelope, "session_mode": mode}
                if pointer.get("envelope_digest") == {"S": envelope_digest(candidate)}:
                    envelope = candidate
                    break
        if envelope.get("session_mode") == "persistent":
            accepted = authority.context_table.get_item(
                Key={"PK": f"session#{delivery.session_id}", "SK": f"mailbox-id#{run_id}"}, ConsistentRead=True
            ).get("Item")
            if accepted is None:
                retry_pending_registration(delivery, selected, request)
        encoded = canonical_json(envelope).decode()
        if record["status"] == "registered" and record.get("envelope_json") != encoded:
            raise ValueError
        expired = None
        if record["status"] == "registering" and not pointer:
            expired = prepare_unregistered_expiry(authority, delivery, selected, envelope, thread, now=now)
            if expired is None:
                retry_pending_registration(delivery, selected, request)
        if expired is not None:
            next_delivery, pointer, execution, registration = expired
        else:
            next_delivery = load_registered_delivery(authority, run_id, delivery.tenant_id)
            execution = authority.store._read(f"TENANT#{delivery.tenant_id}", f"EXEC#{run_id}") or {}
        fields = ("session_id", "thread_id", "tenant_id", "user_id", "team_id", "owner_principal", "session_generation")
        if any(getattr(next_delivery, field) != getattr(delivery, field) for field in fields):
            raise ValueError
        if next_delivery.task_id == delivery.task_id or run_id == delivery.run_id:
            raise ValueError
        digest = {"S": envelope_digest(envelope)}
        if pointer.get("envelope_digest") != digest or execution.get("envelope_digest") != digest:
            raise ValueError
        cancelled = "abort_command_id" in execution
        if cancelled:
            verify_cancellation(execution, next_delivery)
            if execution.get("status") == {"S": "cancelled"}:
                load_queued_terminal(execution, next_delivery)
        allowed = {"pending", "cancelled"} if cancelled else {"pending"}
        if execution.get("chat_unregistered_expiry") == {"BOOL": True}:
            if load_queued_terminal(execution, next_delivery)["outcome"] != "interrupted":
                raise ValueError
            allowed = {"completed"}
        absent = ("workload_binding", "pod_name", "chat_sandbox_creation", "chat_pre_admission_cleanup")
        if execution.get("status", {}).get("S") not in allowed or any(field in execution for field in absent):
            raise ChatHistoryConflictError("chat pending execution reconciliation required")
        scheduled = thread.get("scheduled_turns", {})
        if not isinstance(scheduled, dict) or selected in scheduled:
            raise ValueError
        input_transport()
        execution_check = evidence._unchanged(execution)
        execution_check["ConditionExpression"] += "".join(f" AND attribute_not_exists({field})" for field in absent)
        if not cancelled:
            execution_check["ConditionExpression"] += " AND attribute_not_exists(abort_command_id)"
        if expired is None:
            registration = [{"ConditionCheck": evidence._unchanged(pointer)}, {"ConditionCheck": execution_check}]
        return {
            "document": {"envelope_json": encoded, "delivery": next_delivery.model_dump()},
            "task_id": next_delivery.task_id,
            "pending": {key: value for key, value in pending.items() if key != selected},
            "messages": [message for message in messages if message.get("pending_id") != selected],
            "scheduled": {**scheduled, selected: {"intent_digest": _intent_digest(request), "task_id": next_delivery.task_id}},
            "checks": [
                *registration,
                *[
                    {
                        "ConditionCheck": {
                            "TableName": authority.store.table,
                            "Key": _encoded({"pk": f"CHAT-LAUNCH#{run_id}", "sk": sort_key}),
                            "ConditionExpression": "attribute_not_exists(pk)",
                        }
                    }
                    for sort_key in ("LAUNCH", "CREATION")
                ],
            ],
        }
    except (KeyError, TypeError, ValueError, IndexError):
        raise ChatAuthorizationRefusedError("chat pending input binding changed") from None


def finish_pending_handoff(authority, item, receipt, previous_delivery, sessions):
    try:
        document = json.loads(item["pending_handoff"]["S"])
        delivery = ChatDelivery.model_validate(document["delivery"])
        envelope = json.loads(document["envelope_json"])
        registered = load_registered_delivery(authority, delivery.run_id, delivery.tenant_id)
        pointer = authority.store._read(f"INVOCATION#{delivery.run_id}", "DISPATCH") or {}
        fields = ("session_id", "thread_id", "tenant_id", "user_id", "team_id", "owner_principal", "session_generation")
        if (
            delivery != registered
            or delivery.task_id == receipt["task_id"]
            or any(getattr(delivery, field) != getattr(previous_delivery, field) for field in fields)
            or delivery.session_id != receipt["session_id"]
            or delivery.session_generation != receipt["session_generation"]
            or canonical_json(envelope).decode() != document["envelope_json"]
            or pointer.get("envelope_digest") != {"S": envelope_digest(envelope)}
        ):
            raise ValueError
        verify_delivery_session(delivery, sessions)
        client, queue = input_transport()
        response = client.send_message(
            QueueUrl=queue,
            MessageGroupId=delivery.session_id,
            MessageDeduplicationId=delivery.task_id,
            MessageBody=document["envelope_json"],
        )
        message_id = response.get("MessageId")
        if not isinstance(message_id, str) or not 1 <= len(message_id) <= 128:
            raise ChatAuthorizationUnavailableError("chat pending handoff uncertain")
        authority.store.client.update_item(
            TableName=authority.store.table,
            Key={key: item[key] for key in ("pk", "sk")},
            UpdateExpression="SET completion_receipt = :receipt, input_queue_message_id = :message",
            ConditionExpression="completion_pending = :receipt AND pending_handoff = :handoff AND attribute_not_exists(completion_receipt)",
            ExpressionAttributeValues={":receipt": {"M": _encoded(receipt)}, ":handoff": item["pending_handoff"], ":message": {"S": message_id}},
        )
        return receipt
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            latest = authority.store._read(item["pk"]["S"], item["sk"]["S"]) or {}
            if (
                latest.get("pending_handoff") == item["pending_handoff"]
                and latest.get("completion_receipt") == {"M": _encoded(receipt)}
                and valid_queue_receipt(latest)
            ):
                return receipt
        raise ChatAuthorizationUnavailableError("chat pending handoff unavailable") from None
    except BotoCoreError:
        raise ChatAuthorizationUnavailableError("chat pending handoff unavailable") from None
    except (KeyError, TypeError, ValueError):
        raise ChatAuthorizationRefusedError("chat pending handoff binding changed") from None


def valid_queue_receipt(item):
    message_id = item.get("input_queue_message_id", {}).get("S")
    return isinstance(message_id, str) and 1 <= len(message_id) <= 128
