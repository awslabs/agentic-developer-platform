"""
Agent Gateway — Response Lambda (Thread-Aware)

Routes agent responses to channels and manages per-thread re-enqueue:
- Appends response to session messages + thread messages
- Routes via channel routers (WebSocket, Slack, REST)
- Checks thread for buffered user messages → re-enqueues if found
- Clears thread processing lock when idle
"""

import json
import logging
import os
import time
import uuid
from decimal import Decimal
from typing import Any

import boto3
from botocore.exceptions import ClientError

from routers.websocket import WebSocketRouter
from routers.slack import SlackRouter
from routers.rest import RestRouter

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Ensure router loggers propagate at INFO (Lambda root defaults to WARNING)
logging.getLogger("routers").setLevel(logging.INFO)

INPUT_QUEUE_URL = os.environ.get("INPUT_QUEUE_URL", "")
SESSIONS_TABLE_NAME = os.environ.get("SESSIONS_TABLE_NAME", "")
WS_API_ENDPOINT = os.environ.get("WS_API_ENDPOINT", "")
WS_API_ID = os.environ.get("WS_API_ID", "")
ENVIRONMENT = os.environ.get("ENVIRONMENT", "dev")
REGION = os.environ.get("AWS_REGION_NAME", "us-east-1")

sqs = boto3.client("sqs", region_name=REGION)
dynamodb = boto3.resource("dynamodb", region_name=REGION)
secrets_client = boto3.client("secretsmanager", region_name=REGION)
sessions_table = dynamodb.Table(SESSIONS_TABLE_NAME) if SESSIONS_TABLE_NAME else None

ws_router = WebSocketRouter(WS_API_ENDPOINT, sessions_table=sessions_table)
slack_router = SlackRouter(secrets_client, environment=ENVIRONMENT)
rest_router = RestRouter(sessions_table) if sessions_table else None


def lambda_handler(event: dict, context) -> dict:
    records = event.get("Records", [])
    failures = []
    for record in records:
        try:
            _process_response(json.loads(record["body"]))
        except Exception as e:
            logger.error("Failed: %s — %s", record.get("messageId"), e)
            failures.append({"itemIdentifier": record.get("messageId", "")})
    return {"batchItemFailures": failures} if failures else {"statusCode": 200}


def _process_response(response: dict) -> None:
    task_id = response.get("task_id", "")
    session_id = response.get("session_id", "")
    thread_id = response.get("thread_id", "")
    status = response.get("status", "")
    channel = response.get("channel", "")
    now = int(time.time())

    # -----------------------------------------------------------------------
    # AG-UI event path (Phase 2, Issue #97)
    # When the worker sends an AG-UI event envelope (ag_ui_event=true),
    # we forward the event payload as-is wrapped in type="ag_ui". No
    # thread bookkeeping — AG-UI events are mid-turn ephemera (lifecycle
    # events like RUN_FINISHED are paired with a legacy sendResponse that
    # handles bookkeeping).
    # -----------------------------------------------------------------------
    if response.get("ag_ui_event"):
        ag_ui_payload = response.get("event", {})
        logger.info(
            "AG-UI event: task=%s session=%s event_type=%s",
            task_id, session_id, ag_ui_payload.get("event_type", "?"),
        )
        _route_ag_ui_event(response, ag_ui_payload, task_id)
        return

    # Accept `text` (TS chat-agent), `result` (legacy Python worker), or
    # `content` (generic) — in that priority order for ALL statuses.
    # Previously `text` was only read for progress frames, causing terminal
    # frames from the TS worker to arrive with empty content (issue #89).
    content = response.get("text") or response.get("result") or response.get("content") or ""

    logger.info(
        "Response: task=%s session=%s thread=%s channel=%s status=%s",
        task_id, session_id, thread_id, channel, status,
    )

    is_progress = status == "progress"

    # Only the top-level field is trusted. `channel_metadata` originated as
    # client-facing platform data and must not be allowed to fabricate an owner.
    owner_principal = str(response.get("owner_principal", "") or "")
    session_generation = response.get("session_generation")
    metadata = dict(response.get("channel_metadata", response.get("platform_data", {})) or {})
    metadata.pop("owner_principal", None)
    if response.get("connection_id"):
        metadata["connection_id"] = response["connection_id"]
    if session_id:
        metadata["session_id"] = session_id
    if owner_principal:
        metadata["owner_principal"] = owner_principal

    # 1. Persist. Skip for progress frames — they're UI ephemera, not
    # conversation history. The chat agent records the final assistant turn
    # via LCM; we don't want progress previews polluting the gateway sessions
    # table's message list.
    session_state_authorized = False
    if session_id and sessions_table and not is_progress:
        if owner_principal and session_generation:
            session_state_authorized = _append_response(
                session_id, content, task_id, now, owner_principal,
                session_generation,
            )
        else:
            logger.warning(
                "OWNERSHIP REFUSED response persistence session=%s task=%s: "
                "task has no owner or session generation",
                session_id, task_id,
            )

    # 2. Route to channel (progress frames go through the same router path —
    # the UI decides how to render based on `status` / `type`).
    if is_progress:
        # Let the WS router emit a distinct frame type so UIs can style
        # progress differently from final replies.
        metadata["response_type"] = "progress"
        metadata["progress_kind"] = response.get("kind", "")
        metadata["progress_turn"] = response.get("turn", 0)
    elif status:
        # Forward terminal status ("completed" / "failed" / "notification") so
        # clients can reliably distinguish the final reply from the ingest
        # Lambda's escalation_note ack (both come through as type=response).
        metadata["status"] = status

    if channel in ("webchat", "websocket"):
        ws_router.route(content, metadata, task_id)
    elif channel == "slack":
        slack_router.route(content, metadata, task_id)
    elif channel in ("cli", "rest", "poll") and rest_router:
        rest_router.route(content, metadata, task_id)
    elif metadata.get("connection_id"):
        ws_router.route(content, metadata, task_id)
    elif rest_router:
        rest_router.route(content, metadata, task_id)

    # 3. Thread bookkeeping — skip for progress frames. The thread isn't done
    # until the final reply lands; re-enqueue and lock clearing only happen
    # then.
    if is_progress:
        return

    # Thread-aware re-enqueue (only for long_running threads)
    if session_state_authorized and thread_id:
        _check_thread_and_reenqueue(
            session_id, thread_id, response, now, owner_principal,
            session_generation, task_id,
        )
    elif session_state_authorized:
        # Legacy: no thread_id, clear session-level lock
        _clear_session_processing(
            session_id, owner_principal, session_generation, task_id,
        )


def _route_ag_ui_event(response: dict, ag_ui_payload: dict, task_id: str) -> None:
    """Route an AG-UI event to the appropriate channel.

    The event payload is forwarded as-is inside a `type: "ag_ui"` WS frame.
    The frontend AG-UI consumer reads `event_type` to dispatch to the right
    handler. Chunk-splitting in the WS router handles oversized payloads.
    """
    channel = response.get("channel", "")
    metadata = dict(response.get("channel_metadata", response.get("platform_data", {})) or {})
    metadata.pop("owner_principal", None)
    if response.get("connection_id"):
        metadata["connection_id"] = response["connection_id"]
    if response.get("session_id"):
        metadata["session_id"] = response["session_id"]
    owner_principal = str(response.get("owner_principal", "") or "")
    if owner_principal:
        metadata["owner_principal"] = owner_principal

    # Mark as AG-UI so the WS router emits `type: "ag_ui"` instead of `type: "response"`
    metadata["response_type"] = "ag_ui"
    metadata["ag_ui_payload"] = ag_ui_payload

    # AG-UI events are mid-turn ephemera — content is the serialized event
    content = json.dumps(ag_ui_payload)

    if channel in ("webchat", "websocket"):
        ws_router.route(content, metadata, task_id)
    elif metadata.get("connection_id"):
        ws_router.route(content, metadata, task_id)
    # AG-UI events only go to WebSocket channels for now
    # (Slack and REST don't need AG-UI granularity)


def _append_response(session_id: str, content: str, task_id: str, now: int,
                     owner_principal: str, session_generation: int) -> bool:
    try:
        sessions_table.update_item(
            Key={"session_id": session_id},
            UpdateExpression=(
                "SET messages = list_append(if_not_exists(messages, :e), :m), "
                "last_response = :r, last_response_task_id = :task, updated_at = :t"
            ),
            ConditionExpression="owner_principal = :owner AND created_at = :generation",
            ExpressionAttributeValues={
                ":m": [{"role": "assistant", "content": content[:10000], "timestamp": Decimal(str(now)), "task_id": task_id}],
                ":e": [], ":r": content[:10000], ":t": now, ":task": task_id,
                ":owner": owner_principal,
                ":generation": session_generation,
            },
        )
        return True
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            logger.warning(
                "OWNERSHIP REFUSED response persistence session=%s task=%s: session owner mismatch",
                session_id, task_id,
            )
        else:
            logger.warning("append_response failed: %s", error)
        return False
    except Exception as error:
        logger.warning("append_response failed: %s", error)
        return False


def _check_thread_and_reenqueue(session_id: str, thread_id: str, original: dict,
                                now: int, owner_principal: str,
                                session_generation: int,
                                response_task_id: str):
    """Re-enqueue only while the response's exact owned session is current."""
    next_task_id = ""
    try:
        resp = sessions_table.get_item(
            Key={"session_id": session_id},
            ProjectionExpression=(
                "threads.#tid, connection_id, channel, owner_principal, "
                "created_at, last_response_task_id"
            ),
            ExpressionAttributeNames={"#tid": thread_id},
            ConsistentRead=True,
        )
        session = resp.get("Item", {})
        if (
            session.get("owner_principal") != owner_principal
            or session.get("created_at") != session_generation
            or session.get("last_response_task_id") != response_task_id
        ):
            logger.warning(
                "OWNERSHIP REFUSED response bookkeeping session=%s task=%s: "
                "owned response state is no longer current",
                session_id, response_task_id,
            )
            return

        thread = session.get("threads", {}).get(thread_id, {})
        if not thread:
            return

        thread_messages = thread.get("messages", [])
        has_pending = any(message.get("role") == "user" for message in thread_messages)

        if has_pending and INPUT_QUEUE_URL:
            next_task_id = str(uuid.uuid4())
            last_user_msg = next(
                (
                    message.get("content", "")
                    for message in reversed(thread_messages)
                    if message.get("role") == "user"
                ),
                "",
            )

            if not _set_thread_processing(
                session_id, thread_id, next_task_id, owner_principal,
                session_generation, response_task_id,
            ):
                return

            task = {
                "task_id": next_task_id,
                "session_id": session_id,
                "thread_id": thread_id,
                "connection_id": session.get("connection_id", original.get("connection_id", "")),
                "channel": session.get("channel", original.get("channel", "webchat")),
                "mode": "chat",
                "agent_type": thread.get("persona", original.get("agent_type", "developer")),
                "message": last_user_msg,
                "channel_metadata": original.get("channel_metadata", {}),
                "owner_principal": owner_principal,
                "session_generation": session_generation,
                "enqueued_at": now,
            }

            send_kwargs = {
                "QueueUrl": INPUT_QUEUE_URL,
                "MessageBody": json.dumps(task),
            }
            if INPUT_QUEUE_URL.endswith(".fifo"):
                send_kwargs["MessageGroupId"] = session_id
                send_kwargs["MessageDeduplicationId"] = next_task_id
            sqs.send_message(**send_kwargs)
            _clear_thread_messages(
                session_id, thread_id, owner_principal, session_generation,
                response_task_id, next_task_id,
            )

            logger.info(
                "Re-enqueued: session=%s thread=%s task=%s",
                session_id, thread_id, next_task_id,
            )
        else:
            _clear_thread_processing(
                session_id, thread_id, owner_principal, session_generation,
                response_task_id, response_task_id,
            )
            logger.info("Thread %s/%s idle", session_id, thread_id)

    except Exception as error:
        logger.warning("Thread re-enqueue failed: %s", error)
        if next_task_id:
            _clear_thread_processing(
                session_id, thread_id, owner_principal, session_generation,
                response_task_id, next_task_id,
            )


def _owned_update_failed(error: Exception, operation: str) -> bool:
    if (
        isinstance(error, ClientError)
        and error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"
    ):
        logger.warning("OWNERSHIP REFUSED %s: owned response state changed", operation)
        return True
    return False


def _set_thread_processing(session_id: str, thread_id: str, task_id: str,
                           owner_principal: str, session_generation: int,
                           response_task_id: str) -> bool:
    try:
        sessions_table.update_item(
            Key={"session_id": session_id},
            UpdateExpression="SET threads.#tid.processing_task_id = :task",
            ConditionExpression=(
                "owner_principal = :owner AND created_at = :generation "
                "AND last_response_task_id = :response_task"
            ),
            ExpressionAttributeNames={"#tid": thread_id},
            ExpressionAttributeValues={
                ":task": task_id,
                ":owner": owner_principal,
                ":generation": session_generation,
                ":response_task": response_task_id,
            },
        )
        return True
    except Exception as error:
        if not _owned_update_failed(error, "set_thread_processing"):
            logger.warning("set_thread_processing failed: %s", error)
        return False


def _clear_thread_processing(session_id: str, thread_id: str,
                             owner_principal: str, session_generation: int,
                             response_task_id: str,
                             expected_processing_task_id: str) -> bool:
    try:
        sessions_table.update_item(
            Key={"session_id": session_id},
            UpdateExpression="SET threads.#tid.processing_task_id = :empty",
            ConditionExpression=(
                "owner_principal = :owner AND created_at = :generation "
                "AND last_response_task_id = :response_task "
                "AND threads.#tid.processing_task_id = :processing_task"
            ),
            ExpressionAttributeNames={"#tid": thread_id},
            ExpressionAttributeValues={
                ":empty": "",
                ":owner": owner_principal,
                ":generation": session_generation,
                ":response_task": response_task_id,
                ":processing_task": expected_processing_task_id,
            },
        )
        return True
    except Exception as error:
        if not _owned_update_failed(error, "clear_thread_processing"):
            logger.warning("clear_thread_processing failed: %s", error)
        return False


def _clear_thread_messages(session_id: str, thread_id: str,
                           owner_principal: str, session_generation: int,
                           response_task_id: str,
                           processing_task_id: str) -> bool:
    """Clear only the buffer claimed by this owned response incarnation."""
    try:
        sessions_table.update_item(
            Key={"session_id": session_id},
            UpdateExpression="SET threads.#tid.messages = :empty",
            ConditionExpression=(
                "owner_principal = :owner AND created_at = :generation "
                "AND last_response_task_id = :response_task "
                "AND threads.#tid.processing_task_id = :processing_task"
            ),
            ExpressionAttributeNames={"#tid": thread_id},
            ExpressionAttributeValues={
                ":empty": [],
                ":owner": owner_principal,
                ":generation": session_generation,
                ":response_task": response_task_id,
                ":processing_task": processing_task_id,
            },
        )
        return True
    except Exception as error:
        if not _owned_update_failed(error, "clear_thread_messages"):
            logger.warning("clear_thread_messages failed: %s", error)
        return False


def _clear_session_processing(session_id: str, owner_principal: str,
                              session_generation: int,
                              response_task_id: str) -> bool:
    """Clear the legacy lock only on the session updated by this response."""
    try:
        sessions_table.update_item(
            Key={"session_id": session_id},
            UpdateExpression="SET processing_task_id = :empty",
            ConditionExpression=(
                "owner_principal = :owner AND created_at = :generation "
                "AND last_response_task_id = :response_task"
            ),
            ExpressionAttributeValues={
                ":empty": "",
                ":owner": owner_principal,
                ":generation": session_generation,
                ":response_task": response_task_id,
            },
        )
        return True
    except Exception as error:
        if not _owned_update_failed(error, "clear_session_processing"):
            logger.warning("clear_session_processing failed: %s", error)
        return False
