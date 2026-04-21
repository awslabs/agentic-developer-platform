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
    # Progress frames (status=progress) use `text` as the user-facing
    # message. Regular replies carry the reply body in `result` or `content`.
    content = response.get("text") if status == "progress" else response.get("result", response.get("content", ""))
    channel = response.get("channel", "")
    now = int(time.time())

    logger.info(
        "Response: task=%s session=%s thread=%s channel=%s status=%s",
        task_id, session_id, thread_id, channel, status,
    )

    is_progress = status == "progress"

    # 1. Persist. Skip for progress frames — they're UI ephemera, not
    # conversation history. The chat agent records the final assistant turn
    # via LCM; we don't want progress previews polluting the gateway sessions
    # table's message list.
    if session_id and sessions_table and not is_progress:
        _append_response(session_id, content, task_id, now)

    # 2. Route to channel (progress frames go through the same router path —
    # the UI decides how to render based on `status` / `type`).
    metadata = response.get("channel_metadata", response.get("platform_data", {}))
    if response.get("connection_id"):
        metadata["connection_id"] = response["connection_id"]
    if session_id:
        metadata["session_id"] = session_id
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
    if session_id and thread_id and sessions_table:
        _check_thread_and_reenqueue(session_id, thread_id, response, now)
    elif session_id and sessions_table:
        # Legacy: no thread_id, clear session-level lock
        _clear_session_processing(session_id)


def _append_response(session_id: str, content: str, task_id: str, now: int):
    try:
        sessions_table.update_item(
            Key={"session_id": session_id},
            UpdateExpression=(
                "SET messages = list_append(if_not_exists(messages, :e), :m), "
                "last_response = :r, updated_at = :t"
            ),
            ExpressionAttributeValues={
                ":m": [{"role": "assistant", "content": content[:10000], "timestamp": Decimal(str(now)), "task_id": task_id}],
                ":e": [], ":r": content[:10000], ":t": now,
            },
        )
    except Exception as e:
        logger.warning("append_response failed: %s", e)


def _check_thread_and_reenqueue(session_id: str, thread_id: str, original: dict, now: int):
    """Check if the thread has buffered user messages and re-enqueue if so."""
    try:
        resp = sessions_table.get_item(
            Key={"session_id": session_id},
            ProjectionExpression="threads.#tid, connection_id, channel",
            ExpressionAttributeNames={"#tid": thread_id},
        )
        session = resp.get("Item", {})
        thread = session.get("threads", {}).get(thread_id, {})

        if not thread:
            return

        thread_messages = thread.get("messages", [])

        # Check for user messages in the thread buffer
        has_pending = any(m.get("role") == "user" for m in thread_messages)

        if has_pending and INPUT_QUEUE_URL:
            new_task_id = str(uuid.uuid4())
            last_user_msg = next(
                (m.get("content", "") for m in reversed(thread_messages) if m.get("role") == "user"), ""
            )

            task = {
                "task_id": new_task_id,
                "session_id": session_id,
                "thread_id": thread_id,
                "connection_id": session.get("connection_id", original.get("connection_id", "")),
                "channel": session.get("channel", original.get("channel", "webchat")),
                "mode": "chat",
                "agent_type": thread.get("persona", original.get("agent_type", "developer")),
                "message": last_user_msg,
                "channel_metadata": original.get("channel_metadata", {}),
                "enqueued_at": now,
            }

            # FIFO queues require MessageGroupId + MessageDeduplicationId.
            # Group by session_id (per-session serialization) and dedup by
            # task_id (idempotent re-enqueue if this handler retries).
            send_kwargs = {
                "QueueUrl": INPUT_QUEUE_URL,
                "MessageBody": json.dumps(task),
            }
            if INPUT_QUEUE_URL.endswith(".fifo"):
                send_kwargs["MessageGroupId"] = session_id
                send_kwargs["MessageDeduplicationId"] = new_task_id
            sqs.send_message(**send_kwargs)
            _set_thread_processing(session_id, thread_id, new_task_id)

            # Clear the thread message buffer (they've been consumed)
            _clear_thread_messages(session_id, thread_id)

            logger.info("Re-enqueued: session=%s thread=%s task=%s", session_id, thread_id, new_task_id)
        else:
            _clear_thread_processing(session_id, thread_id)
            logger.info("Thread %s/%s idle", session_id, thread_id)

    except Exception as e:
        logger.warning("Thread re-enqueue failed: %s", e)
        _clear_thread_processing(session_id, thread_id)


def _set_thread_processing(session_id: str, thread_id: str, task_id: str):
    try:
        sessions_table.update_item(
            Key={"session_id": session_id},
            UpdateExpression="SET threads.#tid.processing_task_id = :t",
            ExpressionAttributeNames={"#tid": thread_id},
            ExpressionAttributeValues={":t": task_id},
        )
    except Exception as e:
        logger.warning("set_thread_processing failed: %s", e)


def _clear_thread_processing(session_id: str, thread_id: str):
    try:
        sessions_table.update_item(
            Key={"session_id": session_id},
            UpdateExpression="SET threads.#tid.processing_task_id = :empty",
            ExpressionAttributeNames={"#tid": thread_id},
            ExpressionAttributeValues={":empty": ""},
        )
    except Exception as e:
        logger.warning("clear_thread_processing failed: %s", e)


def _clear_thread_messages(session_id: str, thread_id: str):
    """Clear buffered messages from a thread after re-enqueue."""
    try:
        sessions_table.update_item(
            Key={"session_id": session_id},
            UpdateExpression="SET threads.#tid.messages = :empty",
            ExpressionAttributeNames={"#tid": thread_id},
            ExpressionAttributeValues={":empty": []},
        )
    except Exception as e:
        logger.warning("clear_thread_messages failed: %s", e)


def _clear_session_processing(session_id: str):
    """Legacy: clear session-level lock for backward compatibility."""
    try:
        sessions_table.update_item(
            Key={"session_id": session_id},
            UpdateExpression="SET processing_task_id = :empty",
            ExpressionAttributeValues={":empty": ""},
        )
    except Exception as e:
        logger.warning("clear_session_processing failed: %s", e)
