"""Retry durable turn notifications; discovery never grants execution authority."""

import asyncio
import hashlib
import json
import logging
import os
import time

from boto3.dynamodb.conditions import Key
from boto3.dynamodb.types import TypeDeserializer
from starlette.concurrency import run_in_threadpool

from src.agentauth.bootstrap import envelope_digest
from src.agentauth.chat_capability import ChatAuthorizationRefusedError
from src.agentauth.chat_delivery import ChatDelivery, load_registered_delivery, verify_delivery_session
from src.agentauth.chat_pending_handoff import input_transport
from src.agentauth.chat_session_cleanup import publish_session_cleanup
from src.orchestration.chat_data_migration import _owns_context_row

logger = logging.getLogger(__name__)
RETRY_SECONDS = 60


def publish_notification(authority, sessions, item, *, now, transport=input_transport):
    table = authority.context_table
    if not item["SK"].startswith("turn#"):
        raise ChatAuthorizationRefusedError("chat notification key invalid")
    run_id = item["SK"][5:]
    delivery = load_registered_delivery(authority, run_id, item["tenantId"])
    owner = (delivery.tenant_id, delivery.team_id, delivery.user_id)
    if item.get("PK") != "chat-notifications" or item.get("sessionId") != delivery.session_id or not _owns_context_row(item, owner):
        raise ChatAuthorizationRefusedError("chat notification owner changed")
    pointer = authority.store._read(f"INVOCATION#{run_id}", "DISPATCH")
    terminal = authority.store._read(f"CHAT-DELIVERY#{run_id}", "TERMINAL") or {}
    if "completion_receipt" in terminal:
        receipt = TypeDeserializer().deserialize(terminal["completion_receipt"])
        if any(receipt.get(field) != getattr(delivery, field) for field in ("run_id", "session_id", "task_id", "session_generation")) or (
            receipt.get("input_acknowledgement_ready") is not True
        ):
            raise ChatAuthorizationRefusedError("chat notification completion changed")
        table.delete_item(
            Key={"PK": item["PK"], "SK": item["SK"]},
            ConditionExpression="sessionId = :session AND #sequence = :sequence",
            ExpressionAttributeNames={"#sequence": "sequence"},
            ExpressionAttributeValues={":session": delivery.session_id, ":sequence": item["sequence"]},
        )
        return "completed"
    if item.get("publishedAt", 0) + RETRY_SECONDS > now:
        return "waiting"
    partition = f"session#{delivery.session_id}"
    header = table.get_item(Key={"PK": partition, "SK": "header"}, ConsistentRead=True).get("Item", {})
    receipt = table.get_item(Key={"PK": partition, "SK": f"mailbox-id#{run_id}"}, ConsistentRead=True).get("Item", {})
    sequence = int(item["sequence"])
    entry = table.get_item(Key={"PK": partition, "SK": f"mailbox#{sequence:08d}"}, ConsistentRead=True).get("Item", {})
    if (
        not all(_owns_context_row(row, owner) for row in (header, receipt, entry))
        or sequence < 1
        or item["sequence"] != sequence
        or header.get("sessionTurnSequence", 0) < sequence
        or receipt.get("sequence") != sequence
        or receipt.get("mode") != "persistent"
        or entry.get("mode") != "persistent"
        or entry.get("turnId") != run_id
        or any(row.get("ttl", 0) <= now for row in (item, header, receipt, entry))
        or receipt.get("digest") != envelope_digest({"turn_id": run_id, "message": entry.get("message")})
    ):
        raise ChatAuthorizationRefusedError("chat notification mailbox changed")
    if "completion_pending" in terminal:
        handoff = json.loads(terminal["pending_handoff"]["S"])
        next_delivery = ChatDelivery.model_validate(handoff["delivery"])
        if (
            any(
                getattr(next_delivery, field) != getattr(delivery, field)
                for field in ("tenant_id", "team_id", "user_id", "session_id", "thread_id", "owner_principal", "session_generation")
            )
            or load_registered_delivery(authority, next_delivery.run_id, next_delivery.tenant_id) != next_delivery
        ):
            raise ChatAuthorizationRefusedError("chat notification handoff changed")
        verify_delivery_session(next_delivery, sessions)
    else:
        verify_delivery_session(delivery, sessions)
    notification = {
        "notification_version": 1,
        "message_id": run_id,
        "session_id": delivery.session_id,
        "task_id": delivery.task_id,
        "session_generation": delivery.session_generation,
        "session_mode": "persistent",
        "envelope_digest": pointer["envelope_digest"]["S"],
    }
    client, queue = transport()
    response = client.send_message(
        QueueUrl=queue,
        MessageGroupId=delivery.session_id,
        MessageDeduplicationId=hashlib.sha256(f"{run_id}:{now // RETRY_SECONDS}".encode()).hexdigest(),
        MessageBody=json.dumps(notification, sort_keys=True, separators=(",", ":")),
    )
    if not isinstance(response.get("MessageId"), str) or not response["MessageId"]:
        raise RuntimeError("chat notification publication uncertain")
    table.update_item(
        Key={"PK": item["PK"], "SK": item["SK"]},
        UpdateExpression="SET publishedAt = :now",
        ConditionExpression="sessionId = :session AND #sequence = :sequence",
        ExpressionAttributeNames={"#sequence": "sequence"},
        ExpressionAttributeValues={":now": now, ":session": delivery.session_id, ":sequence": sequence},
    )
    return "published"


def recover_notification_page(authority, sessions, *, now, cursor=None, transport=input_transport, capabilities=None):
    page = authority.context_table.query(
        KeyConditionExpression=Key("PK").eq("chat-notifications"),
        ConsistentRead=True,
        Limit=100,
        **({"ExclusiveStartKey": cursor} if cursor else {}),
    )
    for item in page.get("Items", []):
        try:
            if item["SK"].startswith("cleanup#"):
                if capabilities is None:
                    raise ChatAuthorizationRefusedError("chat cleanup authority unavailable")
                publish_session_cleanup(authority, capabilities, item, now=now, transport=transport)
                continue
            publish_notification(authority, sessions, item, now=now, transport=transport)
        except Exception:
            if item["SK"].startswith("cleanup#"):
                logger.warning("Chat session cleanup recovery deferred", extra={"event": "chat_session_cleanup_deferred"})
            else:
                logger.warning("Chat notification recovery deferred", extra={"event": "chat_notification_recovery_deferred"})
    return page.get("LastEvaluatedKey")


async def maintain_chat_notifications():
    cursor = None
    while True:
        if os.environ.get("ADP_CHAT_DATA_ENABLED") == "true":
            try:
                from src.agentauth.chat_data_routes import runtime
                from src.orchestration.intake_wiring import _get_sessions_table

                authority, _capabilities = await run_in_threadpool(runtime)
                sessions = await run_in_threadpool(_get_sessions_table)
                cursor = await run_in_threadpool(
                    recover_notification_page, authority, sessions, now=int(time.time()), cursor=cursor, capabilities=_capabilities
                )
            except Exception:
                logger.warning("Chat notification recovery unavailable", extra={"event": "chat_notification_recovery_unavailable"})
        await asyncio.sleep(1 if cursor else 30)
