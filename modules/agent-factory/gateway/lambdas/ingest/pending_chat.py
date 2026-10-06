"""Retain authenticated follow-ups before attempting protected root registration."""

import hashlib
import json
import re
from decimal import Decimal

from botocore.exceptions import ClientError


class PendingChatConflict(Exception):
    pass


def _canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


def _intent(envelope):
    return {
        key: value
        for key, value in envelope.items()
        if key not in {"task_id", "enqueued_at", "arrived_at", "connection_id"}
    }


def _load(table, envelope, processing_task, now):
    row = table.get_item(Key={"session_id": envelope["session_id"]}, ConsistentRead=True).get(
        "Item", {}
    )
    thread = row.get("threads", {}).get(envelope["thread_id"], {})
    if (
        not processing_task
        or row.get("owner_principal") != envelope["owner_principal"]
        or row.get("created_at") != envelope["session_generation"]
        or row.get("channel") != "webchat"
        or row.get("expires_at", 0) <= now
        or thread.get("processing_task_id") != processing_task
    ):
        raise PendingChatConflict("pending chat owner or task changed")
    pending = thread.get("pending_turns", {})
    messages = thread.get("messages", [])
    if not isinstance(pending, dict) or not isinstance(messages, list):
        raise PendingChatConflict("pending chat state invalid")
    return thread, pending, messages


def _save(table, envelope, processing_task, now, thread, pending, messages=None):
    names = {"#thread": envelope["thread_id"], "#channel": "channel"}
    values = {
        ":owner": envelope["owner_principal"],
        ":generation": envelope["session_generation"],
        ":task": processing_task,
        ":channel": "webchat",
        ":now": Decimal(str(now)),
        ":pending": pending,
    }
    condition = (
        "owner_principal = :owner AND created_at = :generation AND #channel = :channel "
        "AND expires_at > :now AND threads.#thread.processing_task_id = :task"
    )
    update = "SET threads.#thread.pending_turns = :pending"
    if "pending_turns" in thread:
        condition += " AND threads.#thread.pending_turns = :previous"
        values[":previous"] = thread["pending_turns"]
    else:
        condition += " AND attribute_not_exists(threads.#thread.pending_turns)"
    if messages is not None:
        update += ", threads.#thread.messages = :messages"
        values[":messages"] = messages
        if "messages" in thread:
            condition += " AND threads.#thread.messages = :previous_messages"
            values[":previous_messages"] = thread["messages"]
        else:
            condition += " AND attribute_not_exists(threads.#thread.messages)"
    try:
        table.update_item(
            Key={"session_id": envelope["session_id"]},
            UpdateExpression=update,
            ConditionExpression=condition,
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
        return True
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False
        raise


def _validate_registration(request, registered):
    final = json.loads(registered)
    if (
        not isinstance(final, dict)
        or _canonical(final) != registered
        or any(final.get(key) != value for key, value in request.items())
        or final.get("persona") != request["agent_type"]
        or len(registered.encode()) > 65536
    ):
        raise PendingChatConflict("pending chat registration changed")
    return registered


def recover_pending_turn(table, binding, *, now, register):
    fields = {"session_id", "thread_id", "owner_principal", "session_generation", "processing_task", "pending_id"}
    if (
        not isinstance(binding, dict)
        or set(binding) != fields
        or any(not isinstance(binding[field], str) or not binding[field] for field in fields - {"session_generation"})
        or type(binding["session_generation"]) is not int
        or binding["session_generation"] <= 0
        or not re.fullmatch(r"[a-f0-9]{64}", binding["pending_id"])
    ):
        raise PendingChatConflict("pending chat recovery binding invalid")
    fence = {**binding, "channel": "webchat"}
    thread, pending, messages = _load(table, fence, binding["processing_task"], now)
    try:
        record = pending[binding["pending_id"]]
        request = json.loads(record["request_json"])
        markers = [message for message in messages if message.get("pending_id") == binding["pending_id"]]
        if (
            record.get("status") not in {"registering", "registered"}
            or _canonical(request) != record["request_json"]
            or any(request.get(field) != binding[field] for field in ("session_id", "thread_id", "owner_principal", "session_generation"))
            or hashlib.sha256(request["message_id"].encode()).hexdigest() != binding["pending_id"]
            or request["task_id"] == binding["processing_task"]
            or binding["pending_id"] in thread.get("scheduled_turns", {})
            or len(markers) != 1
            or markers[0].get("role") != "user"
            or markers[0].get("content") != request["message"][:10000]
        ):
            raise ValueError
    except (KeyError, TypeError, ValueError, AttributeError):
        raise PendingChatConflict("pending chat retained request invalid") from None
    buffer_pending_turn(table, request, processing_task=binding["processing_task"], now=now, register=register, retained_only=True)


def buffer_pending_turn(table, envelope, *, processing_task, now, register, retained_only=False):
    request = _canonical(envelope)
    if (
        envelope.get("channel") != "webchat"
        or not isinstance(envelope.get("message_id"), str)
        or not 1 <= len(envelope["message_id"]) <= 128
        or not envelope.get("user_id")
        or not envelope.get("tenant_id")
        or not isinstance(envelope.get("session_generation"), int)
        or isinstance(envelope["session_generation"], bool)
        or envelope["session_generation"] <= 0
        or len(request.encode()) > 60000
    ):
        raise PendingChatConflict("pending chat input invalid")
    pending_id = hashlib.sha256(envelope["message_id"].encode()).hexdigest()
    for _attempt in range(4):
        thread, pending, messages = _load(table, envelope, processing_task, now)
        scheduled = thread.get("scheduled_turns", {})
        if not isinstance(scheduled, dict):
            raise PendingChatConflict("pending chat scheduling state invalid")
        if pending_id in scheduled:
            if scheduled[pending_id].get("intent_digest") != hashlib.sha256(_canonical(_intent(envelope)).encode()).hexdigest():
                raise PendingChatConflict("pending chat input changed")
            return None
        previous = pending.get(pending_id)
        if previous is not None:
            if (
                not isinstance(previous, dict)
                or previous.get("status") not in {"registering", "registered"}
                or _intent(json.loads(previous["request_json"])) != _intent(envelope)
            ):
                raise PendingChatConflict("pending chat input changed")
            request = previous["request_json"]
            if previous["status"] == "registered":
                return _validate_registration(json.loads(request), previous["envelope_json"])
            break
        if retained_only:
            raise PendingChatConflict("pending chat retained input disappeared")
        if (
            len(pending) >= 32
            or sum(len(_canonical(record).encode()) for record in pending.values())
            + 2 * len(request.encode())
            > 128000
        ):
            raise PendingChatConflict("pending chat capacity exceeded")
        record = {"status": "registering", "request_json": request}
        marker = {
            "role": "user",
            "content": envelope["message"][:10000],
            "timestamp": Decimal(str(now)),
            "pending_id": pending_id,
        }
        if _save(
            table,
            envelope,
            processing_task,
            now,
            thread,
            {**pending, pending_id: record},
            [*messages, marker],
        ):
            break
    else:
        raise PendingChatConflict("pending chat changed; retry")

    retained = json.loads(request)
    registered = _validate_registration(retained, register(retained))
    record = {"status": "registered", "request_json": request, "envelope_json": registered}
    for _attempt in range(4):
        thread, pending, _messages = _load(table, envelope, processing_task, now)
        previous = pending.get(pending_id)
        if previous == record:
            return registered
        if previous != {"status": "registering", "request_json": request}:
            raise PendingChatConflict("pending chat registration changed")
        if _save(table, envelope, processing_task, now, thread, {**pending, pending_id: record}):
            return registered
    raise PendingChatConflict("pending chat changed; retry")
