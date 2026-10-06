"""Retry retained input through its trusted producer, never through sandbox authority."""

import hashlib
import json
import os
from datetime import timedelta

from botocore.exceptions import BotoCoreError, ClientError

from src.agentauth.chat_capability import ChatAuthorizationRefusedError, ChatAuthorizationUnavailableError
from src.agentauth.chat_history_write import ChatHistoryConflictError
from src.agentauth.model_policy import canonical_json
from src.orchestration import intake_wiring


def retry_pending_registration(delivery, selected, request):
    fields = ("session_id", "thread_id", "tenant_id", "team_id", "owner_principal", "session_generation")
    if (
        any(request.get(field) != getattr(delivery, field) for field in fields)
        or request.get("channel") != "webchat"
        or request.get("task_id") == delivery.task_id
        or request.get("message_id") == delivery.run_id
    ):
        raise ChatAuthorizationRefusedError("chat pending recovery binding changed")
    function = os.environ.get(intake_wiring.INTAKE_FUNCTION_ENV, "").strip()
    if not function:
        raise ChatAuthorizationUnavailableError("chat pending recovery unconfigured")
    binding = {field: getattr(delivery, field) for field in ("session_id", "thread_id", "owner_principal", "session_generation")}
    binding.update(processing_task=delivery.task_id, pending_id=selected)
    try:
        response = intake_wiring._get_lambda_client().invoke(
            FunctionName=function,
            InvocationType="RequestResponse",
            Payload=canonical_json({"source": "chat-pending-recovery", "binding": binding}),
        )
        payload = response.get("Payload")
        try:
            result = json.loads(payload.read(4097)) if payload is not None else None
        finally:
            if payload is not None:
                payload.close()
        if response.get("StatusCode") != 200 or response.get("FunctionError") or not isinstance(result, dict) or result.get("statusCode") != 200:
            raise ChatAuthorizationUnavailableError("chat pending registration unavailable")
    except (BotoCoreError, ClientError, ValueError, TypeError, OSError):
        raise ChatAuthorizationUnavailableError("chat pending registration unavailable") from None
    raise ChatHistoryConflictError("chat pending registration retried; reload protected state")


def retained_registration_matches(envelope, human_id, created, now):
    if envelope.get("channel") != "webchat" or created + timedelta(hours=2) <= now:
        return False
    sessions = intake_wiring._get_sessions_table()
    if sessions is None:
        return False
    try:
        row = sessions.get_item(Key={"session_id": envelope["session_id"]}, ConsistentRead=True).get("Item", {})
        thread = row.get("threads", {}).get(envelope["thread_id"], {})
        selected = hashlib.sha256(envelope["message_id"].encode()).hexdigest()
        record = thread.get("pending_turns", {}).get(selected, {})
        request = json.loads(record["request_json"])
        markers = [message for message in thread.get("messages", []) if message.get("pending_id") == selected]
        normalized = {
            **request,
            "persona": request["agent_type"],
            "source_ref": {**request.get("source_ref", {}), "repo": f"chat/{envelope['session_id']}"},
            "correlation": {"correlation_id": request["message_id"], "root_human_id": human_id, "is_human_rooted": True},
        }
        normalized.pop("parent_principal", None)
        return (
            canonical_json(normalized) == canonical_json(envelope)
            and row.get("owner_principal") == envelope["owner_principal"]
            and row.get("created_at") == envelope["session_generation"]
            and row.get("channel") == "webchat"
            and row.get("expires_at", 0) > now.timestamp()
            and bool(thread.get("processing_task_id"))
            and thread["processing_task_id"] != envelope["task_id"]
            and selected not in thread.get("scheduled_turns", {})
            and record.get("status") in {"registering", "registered"}
            and len(markers) == 1
            and markers[0].get("role") == "user"
            and markers[0].get("content") == request["message"][:10000]
            and created.timestamp() - 30 <= float(markers[0]["timestamp"]) <= created.timestamp() + 900
        )
    except (BotoCoreError, ClientError, KeyError, TypeError, ValueError, AttributeError):
        return False
