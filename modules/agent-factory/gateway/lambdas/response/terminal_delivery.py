"""Owner-fenced terminal delivery without legacy queue or lock bookkeeping."""

import hashlib
import json
import re
import time

from botocore.exceptions import ClientError


def deliver_terminal(response, sessions, router):
    fields = {
        "terminal_delivery", "strict_delivery", "channel", "delivery_id", "session_id", "session_generation",
        "owner_principal", "thread_id", "task_id", "status", "retryable", "accounting_status", "text",
    }
    if (
        set(response) != fields
        or response["terminal_delivery"] is not True
        or response["strict_delivery"] is not True
        or response["channel"] != "webchat"
        or not isinstance(response["delivery_id"], str)
        or not re.fullmatch(r"chat-terminal-[a-f0-9]{64}", response["delivery_id"])
        or any(not isinstance(response[field], str) or not 1 <= len(response[field]) <= 128 for field in ("session_id", "thread_id", "task_id"))
        or not isinstance(response["owner_principal"], str) or not 1 <= len(response["owner_principal"]) <= 1024
        or type(response["session_generation"]) is not int or response["session_generation"] <= 0
        or response["status"] not in {"completed", "failed", "cancelled", "interrupted"}
        or type(response["retryable"]) is not bool
        or (response["retryable"] and response["status"] != "interrupted")
        or response["accounting_status"] not in {"not_used", "settled", "unresolved"}
        or not isinstance(response["text"], str) or not response["text"] or len(response["text"].encode()) > 131_072
        or sessions is None
    ):
        raise ValueError("invalid terminal delivery")
    now = int(time.time())
    digest = hashlib.sha256(json.dumps(response, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    receipt = {"delivery_id": response["delivery_id"], "task_id": response["task_id"], "digest": digest, "status": "persisted"}
    owner_condition = (
        "owner_principal = :owner AND created_at = :generation AND #channel = :channel "
        "AND expires_at > :now AND threads.#thread.processing_task_id = :task"
    )
    names = {"#thread": response["thread_id"], "#channel": "channel"}
    values = {
        ":owner": response["owner_principal"], ":generation": response["session_generation"],
        ":channel": "webchat", ":now": now, ":task": response["task_id"],
    }
    key = {"session_id": response["session_id"]}
    session = sessions.get_item(Key=key, ConsistentRead=True).get("Item", {})
    thread = session.get("threads", {}).get(response["thread_id"], {})
    if (
        session.get("owner_principal") != values[":owner"] or session.get("created_at") != values[":generation"]
        or session.get("channel") != "webchat" or session.get("expires_at", 0) <= now
    ):
        raise ValueError("terminal delivery session changed")
    archived = session.get("completed_terminal_deliveries", {})
    if not isinstance(archived, dict):
        raise ValueError("terminal delivery archive invalid")
    if response["delivery_id"] in archived:
        if archived[response["delivery_id"]] != {**receipt, "status": "completed", "thread_id": response["thread_id"]}:
            raise ValueError("terminal delivery conflicts with completed receipt")
        return
    if thread.get("processing_task_id") != response["task_id"]:
        raise ValueError("terminal delivery task changed")
    previous = thread.get("terminal_delivery")
    if previous is None or previous.get("task_id") != response["task_id"]:
        message = {
            "role": "assistant", "content": response["text"][:10000], "timestamp": now,
            "task_id": response["task_id"], "delivery_id": response["delivery_id"], "status": response["status"],
        }
        try:
            sessions.update_item(
                Key=key,
                UpdateExpression=(
                    "SET messages = list_append(if_not_exists(messages, :empty), :message), "
                    "threads.#thread.messages = list_append(if_not_exists(threads.#thread.messages, :empty), :message), "
                    "threads.#thread.terminal_delivery = :receipt, last_response = :text, last_response_task_id = :task, updated_at = :now"
                ),
                ConditionExpression=owner_condition + " AND (attribute_not_exists(threads.#thread.terminal_delivery) OR threads.#thread.terminal_delivery.task_id <> :task)",
                ExpressionAttributeNames=names,
                ExpressionAttributeValues={**values, ":empty": [], ":message": [message], ":receipt": receipt, ":text": response["text"][:10000]},
            )
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
            raise ValueError("terminal delivery changed; retry") from None
    elif previous == {**receipt, "status": "sent"}:
        return
    elif previous != receipt:
        raise ValueError("terminal delivery conflicts with receipt")
    metadata = {
        **{field: response[field] for field in (
            "strict_delivery", "owner_principal", "session_id", "session_generation", "thread_id", "task_id",
            "delivery_id", "status", "retryable", "accounting_status",
        )},
        "terminal_delivery": True,
    }
    if not router.route(response["text"], metadata, response["task_id"]):
        raise RuntimeError("terminal websocket delivery unavailable")
    sessions.update_item(
        Key=key,
        UpdateExpression="SET threads.#thread.terminal_delivery = :sent",
        ConditionExpression=owner_condition + " AND threads.#thread.terminal_delivery = :receipt",
        ExpressionAttributeNames=names,
        ExpressionAttributeValues={**values, ":receipt": receipt, ":sent": {**receipt, "status": "sent"}},
    )
