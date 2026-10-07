"""Retryable handoff of protected terminal outcomes to the owner response FIFO."""

import json

from boto3.dynamodb.types import TypeDeserializer
from botocore.exceptions import BotoCoreError, ClientError

from src.agentauth import chat_delivery
from src.agentauth.chat_capability import ChatAuthorizationRefusedError, ChatAuthorizationUnavailableError
from src.agentauth.chat_delivery import ChatDelivery, verify_delivery_session
from src.agentauth.chat_terminal_delivery import terminal_delivery_payload, verify_terminal_delivery


def publish_terminal_delivery(authority, launches, terminal):
    launch = launches.load(terminal["run_id"])
    store = authority.store
    execution = store._read(f"TENANT#{launch.tenant_id}", f"EXEC#{launch.run_id}") or {}
    if TypeDeserializer().deserialize(execution.get("chat_terminal", {"NULL": True})) != terminal:
        raise ChatAuthorizationRefusedError("chat terminal publication changed")
    item = verify_terminal_delivery(store, execution, launch, terminal)
    if item is None:
        return
    _publish_verified_item(store, item)


def publish_queued_terminal_delivery(authority, delivery, terminal):
    from src.agentauth.chat_queued_terminal import load_queued_terminal

    store = authority.store
    execution = store._read(f"TENANT#{delivery.tenant_id}", f"EXEC#{delivery.run_id}") or {}
    if load_queued_terminal(execution, delivery) != terminal:
        raise ChatAuthorizationRefusedError("chat queued publication changed")
    item = verify_terminal_delivery(store, execution, delivery, terminal)
    if item is None:
        raise ChatAuthorizationRefusedError("chat queued delivery missing")
    _publish_verified_item(store, item)


def publish_pre_admission_terminal_delivery(authority, delivery):
    from src.agentauth.chat_pre_admission_terminal import load_pre_admission_terminal

    store = authority.store
    execution = store._read(f"TENANT#{delivery.tenant_id}", f"EXEC#{delivery.run_id}") or {}
    terminal = load_pre_admission_terminal(execution, delivery)
    item = verify_terminal_delivery(store, execution, delivery, terminal)
    if item is None:
        raise ChatAuthorizationRefusedError("chat pre-admission delivery missing")
    _publish_verified_item(store, item)


def _publish_verified_item(store, item):
    if item.get("status") == {"S": "queued"}:
        receipt = item.get("queue_message_id", {}).get("S")
        if isinstance(receipt, str) and 1 <= len(receipt) <= 128:
            return
        raise ChatAuthorizationUnavailableError("chat terminal queue receipt missing")
    if item.get("status") != {"S": "pending"}:
        raise ChatAuthorizationUnavailableError("chat terminal publication state invalid")
    document = json.loads(item["document"]["S"])
    delivery = ChatDelivery.model_validate(document["delivery"])
    client, queue, sessions = chat_delivery.response_transport()
    payload = terminal_delivery_payload(document)
    try:
        verify_delivery_session(delivery, sessions)
        response = client.send_message(
            QueueUrl=queue,
            MessageGroupId=delivery.session_id,
            MessageDeduplicationId=document["delivery_id"],
            MessageBody=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        )
        message_id = response.get("MessageId")
        if not isinstance(message_id, str) or not 1 <= len(message_id) <= 128:
            raise ChatAuthorizationUnavailableError("chat terminal handoff uncertain")
        store.client.update_item(
            TableName=store.table,
            Key={field: item[field] for field in ("pk", "sk")},
            UpdateExpression="SET #status = :queued, queue_message_id = :message",
            ConditionExpression="#document = :document AND #status = :pending",
            ExpressionAttributeNames={"#document": "document", "#status": "status"},
            ExpressionAttributeValues={
                ":document": item["document"],
                ":pending": {"S": "pending"},
                ":queued": {"S": "queued"},
                ":message": {"S": message_id},
            },
        )
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            latest = store._read(item["pk"]["S"], item["sk"]["S"]) or {}
            receipt = latest.get("queue_message_id", {}).get("S")
            if (
                latest.get("document") == item["document"]
                and latest.get("status") == {"S": "queued"}
                and isinstance(receipt, str)
                and 1 <= len(receipt) <= 128
            ):
                return
        raise ChatAuthorizationUnavailableError("chat terminal handoff unavailable") from None
    except BotoCoreError:
        raise ChatAuthorizationUnavailableError("chat terminal handoff unavailable") from None
