"""
Agent Gateway — Ingest Lambda

Universal front door with thread-aware concurrency:
  direct_response → answer immediately, no thread
  long_running    → per-thread serialization via SQS/KEDA
  github_actions  → create/label GitHub issue, always parallel
"""

import json
import logging
import os
import time
import uuid
from decimal import Decimal
from typing import Any

import boto3

from channels.base import ChannelAdapter, ChannelType, UnifiedMessage
from channels.slack import SlackAdapter
from channels.webchat import WebChatAdapter
from classifier import classify_message
from github_dispatch import create_issue_and_dispatch, label_existing_issue

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

INPUT_QUEUE_URL = os.environ["INPUT_QUEUE_URL"]
RESPONSE_QUEUE_URL = os.environ.get("RESPONSE_QUEUE_URL", "")
SESSIONS_TABLE = os.environ["SESSIONS_TABLE_NAME"]
REGION = os.environ.get("AWS_REGION_NAME", "us-east-1")
SLACK_SIGNING_SECRET = os.environ.get("SLACK_SIGNING_SECRET", "")
SLACK_BOT_USER_ID = os.environ.get("SLACK_BOT_USER_ID", "")

sqs = boto3.client("sqs", region_name=REGION)
dynamodb = boto3.resource("dynamodb", region_name=REGION)
sessions_table = dynamodb.Table(SESSIONS_TABLE)

ADAPTERS: dict[str, ChannelAdapter] = {
    "webchat": WebChatAdapter(),
    "slack": SlackAdapter(signing_secret=SLACK_SIGNING_SECRET, bot_user_id=SLACK_BOT_USER_ID),
}


def lambda_handler(event, context):
    route_key = event.get("requestContext", {}).get("routeKey", "")
    if route_key == "$connect":
        return {"statusCode": 200, "body": "Connected"}
    if route_key == "$disconnect":
        return {"statusCode": 200, "body": "Disconnected"}

    channel_name, adapter = detect_channel(event)

    if channel_name == "slack":
        payload = parse_body(event)
        if payload.get("type") == "url_verification":
            return {"statusCode": 200, "body": json.dumps(SlackAdapter.handle_url_verification(payload))}
        if not adapter.verify_request(event.get("headers", {}), event.get("body", "").encode("utf-8")):
            return {"statusCode": 401, "body": "Invalid signature"}

    message = adapter.parse_event(event) if channel_name == "webchat" else adapter.parse_event(parse_body(event))
    if message is None:
        return {"statusCode": 200, "body": "OK"}

    return handle_unified_message(message)


def handle_unified_message(message: UnifiedMessage) -> dict:
    now = int(time.time())
    task_id = str(uuid.uuid4())
    connection_id = message.platform_data.get("connection_id", "")
    session_id = message.thread_id or message.session_key

    # Ensure session exists
    session = get_or_create_session(session_id, connection_id, message, now)
    threads = session.get("threads", {})

    # Load recent history and active thread summaries for classifier
    history = load_recent_history(session_id, limit=10)
    active_threads = [
        {
            "thread_id": tid,
            "topic": t.get("topic", ""),
            "path": t.get("path", ""),
            "status": "processing" if t.get("processing_task_id") else "idle",
            "github_issue_url": t.get("github_issue_url", ""),
        }
        for tid, t in threads.items()
        if t.get("processing_task_id")  # Only show active threads
    ]

    # Classify with thread awareness
    classification = classify_message(
        message=message.text,
        conversation_history=history,
        active_threads=active_threads,
        channel=message.channel.value,
        user_name=message.user_name,
    )

    logger.info("Route: path=%s thread_action=%s persona=%s", classification.path, classification.thread_action, classification.persona)

    # Append message to session history (always, for all paths)
    append_message(session_id, "user", message.text, now)

    # --- Route ---

    if classification.path == "direct_response" and classification.response:
        return handle_direct_response(session_id, task_id, connection_id, message, classification, now)

    if classification.path == "github_actions":
        return handle_github_dispatch(session_id, task_id, connection_id, message, classification, threads, now)

    # long_running
    return handle_long_running(session_id, task_id, connection_id, message, classification, threads, now)


# ─── Path Handlers ────────────────────────────────────────────

def handle_direct_response(session_id, task_id, connection_id, message, classification, now):
    append_message(session_id, "assistant", classification.response, now)
    if RESPONSE_QUEUE_URL:
        sqs.send_message(QueueUrl=RESPONSE_QUEUE_URL, MessageBody=json.dumps({
            "task_id": task_id, "session_id": session_id, "connection_id": connection_id,
            "channel": message.channel.value, "channel_metadata": message.platform_data,
            "result": classification.response, "status": "completed", "completed_at": now,
        }))
    return {"statusCode": 200, "body": json.dumps({"task_id": task_id, "session_id": session_id, "status": "completed"})}


def handle_github_dispatch(session_id, task_id, connection_id, message, classification, threads, now):
    """Always dispatch — github_actions tasks are independent, never blocked."""
    repo_parts = (classification.repo or "").split("/", 1)
    repo_owner = repo_parts[0] if len(repo_parts) > 1 else ""
    repo_name = repo_parts[1] if len(repo_parts) > 1 else repo_parts[0] if repo_parts else ""

    # Follow-up to existing github thread → post comment on the issue
    if classification.thread_action == "follow_up" and classification.follow_up_thread_id:
        thread = threads.get(classification.follow_up_thread_id, {})
        issue_url = thread.get("github_issue_url", "")
        if issue_url and thread.get("github_issue_number"):
            from github_dispatch import _get_installation_token, _post_comment
            token = _get_installation_token(repo_owner)
            if token:
                _post_comment(token, repo_owner, repo_name, thread["github_issue_number"],
                              f"**Follow-up from {message.channel.value}** ({message.user_name}):\n\n{message.text}")
            notify = f"📝 Added your follow-up to {issue_url}"
            send_notification(session_id, task_id, connection_id, message, notify, now)
            return {"statusCode": 200, "body": json.dumps({"task_id": task_id, "session_id": session_id, "status": "comment_added", "issue_url": issue_url})}

    # New github task → create issue
    thread_id = str(uuid.uuid4())[:8]

    if classification.issue_number and not classification.create_issue:
        result = label_existing_issue(repo_owner, repo_name, classification.issue_number, classification.persona, classification.enriched_message or "")
    else:
        title = classification.issue_title or f"[{classification.persona}] {message.text[:80]}"
        body = classification.enriched_message or message.text
        result = create_issue_and_dispatch(repo_owner, repo_name, title, body, classification.persona, session_id, message.channel.value, message.user_name)

    if result.get("dispatched"):
        issue_url = result.get("issue_url", "")
        issue_number = result.get("issue_number", 0)

        # Create thread record
        create_thread(session_id, thread_id, task_id, classification, issue_number, issue_url)

        escalation = f"🔧 I've escalated this to a code task.\n📋 Tracking: {issue_url}\nThe @agent-{classification.persona} is working on it."
        if classification.escalation_note:
            escalation = f"{classification.escalation_note}\n📋 {issue_url}"
        send_notification(session_id, task_id, connection_id, message, escalation, now)

        return {"statusCode": 200, "body": json.dumps({"task_id": task_id, "session_id": session_id, "status": "dispatched_github", "issue_url": issue_url, "thread_id": thread_id})}

    # Fallback to long_running
    return handle_long_running(session_id, task_id, connection_id, message, classification, threads, now)


def handle_long_running(session_id, task_id, connection_id, message, classification, threads, now):
    """Per-thread serialization for long_running tasks."""

    if classification.thread_action == "follow_up" and classification.follow_up_thread_id:
        thread_id = classification.follow_up_thread_id
        thread = threads.get(thread_id, {})

        if thread.get("processing_task_id"):
            # Thread is busy — buffer message, it'll be picked up on completion
            append_thread_message(session_id, thread_id, "user", message.text, now)
            notify = classification.escalation_note or "Your message has been queued. I'll address it once the current task completes."
            send_notification(session_id, task_id, connection_id, message, notify, now)
            return {"statusCode": 200, "body": json.dumps({"task_id": None, "session_id": session_id, "thread_id": thread_id, "status": "queued"})}
    else:
        # New thread
        thread_id = str(uuid.uuid4())[:8]

    # Enqueue to SQS
    create_thread(session_id, thread_id, task_id, classification)
    set_thread_processing(session_id, thread_id, task_id)

    sqs.send_message(QueueUrl=INPUT_QUEUE_URL, MessageBody=json.dumps({
        "task_id": task_id, "session_id": session_id, "thread_id": thread_id,
        "connection_id": connection_id, "channel": message.channel.value,
        "mode": "chat", "agent_type": classification.persona,
        "repo_owner": (classification.repo or "").split("/")[0] if classification.repo and "/" in classification.repo else "",
        "repo_name": (classification.repo or "").split("/")[1] if classification.repo and "/" in classification.repo else "",
        "message": message.text, "platform_data": message.platform_data, "enqueued_at": now,
    }))

    if classification.escalation_note:
        send_notification(session_id, task_id, connection_id, message, classification.escalation_note, now)

    return {"statusCode": 200, "body": json.dumps({"task_id": task_id, "session_id": session_id, "thread_id": thread_id, "status": "processing"})}


# ─── Helpers ──────────────────────────────────────────────────

def send_notification(session_id, task_id, connection_id, message, text, now):
    append_message(session_id, "assistant", text, now)
    if RESPONSE_QUEUE_URL:
        sqs.send_message(QueueUrl=RESPONSE_QUEUE_URL, MessageBody=json.dumps({
            "task_id": task_id, "session_id": session_id, "connection_id": connection_id,
            "channel": message.channel.value, "channel_metadata": message.platform_data,
            "result": text, "status": "notification", "completed_at": now,
        }))


def detect_channel(event):
    if event.get("requestContext", {}).get("connectionId"):
        return "webchat", ADAPTERS["webchat"]
    headers = event.get("headers", {})
    if headers.get("x-slack-signature") or headers.get("X-Slack-Signature"):
        return "slack", ADAPTERS["slack"]
    body = parse_body(event)
    if body.get("type") in ("url_verification", "event_callback"):
        return "slack", ADAPTERS["slack"]
    return "webchat", ADAPTERS["webchat"]


def parse_body(event):
    body = event.get("body", {})
    if isinstance(body, str):
        try: return json.loads(body)
        except: return {}
    return body if isinstance(body, dict) else {}


# ─── Session & Thread DynamoDB Operations ─────────────────────

def get_or_create_session(session_id, connection_id, message, now):
    try:
        resp = sessions_table.get_item(Key={"session_id": session_id})
        item = resp.get("Item")
        if item:
            sessions_table.update_item(Key={"session_id": session_id},
                UpdateExpression="SET connection_id = :c, updated_at = :t, expires_at = :e",
                ExpressionAttributeValues={":c": connection_id, ":t": now, ":e": now + 86400})
            return item
    except Exception as e:
        logger.warning("get_session failed: %s", e)

    sessions_table.put_item(Item={
        "session_id": session_id,
        "user_workspace": f"{message.user_id}#{message.channel.value}",
        "connection_id": connection_id, "channel": message.channel.value,
        "messages": [], "threads": {}, "created_at": now, "updated_at": now, "expires_at": now + 86400,
    })
    return {"threads": {}}


def append_message(session_id, role, content, ts):
    try:
        sessions_table.update_item(Key={"session_id": session_id},
            UpdateExpression="SET messages = list_append(if_not_exists(messages, :e), :m), updated_at = :t",
            ExpressionAttributeValues={":m": [{"role": role, "content": content[:10000], "timestamp": Decimal(str(ts))}], ":e": [], ":t": ts})
    except Exception as e:
        logger.warning("append_message failed: %s", e)


def create_thread(session_id, thread_id, task_id, classification, issue_number=None, issue_url=None):
    thread_data = {
        "topic": classification.issue_title or classification.reasoning[:100],
        "path": classification.path,
        "persona": classification.persona,
        "processing_task_id": task_id,
        "messages": [],
        "created_at": int(time.time()),
    }
    if issue_number:
        thread_data["github_issue_number"] = issue_number
    if issue_url:
        thread_data["github_issue_url"] = issue_url

    try:
        sessions_table.update_item(Key={"session_id": session_id},
            UpdateExpression="SET threads.#tid = :td",
            ExpressionAttributeNames={"#tid": thread_id},
            ExpressionAttributeValues={":td": thread_data})
    except Exception as e:
        logger.warning("create_thread failed: %s", e)


def set_thread_processing(session_id, thread_id, task_id):
    try:
        sessions_table.update_item(Key={"session_id": session_id},
            UpdateExpression="SET threads.#tid.processing_task_id = :t",
            ExpressionAttributeNames={"#tid": thread_id},
            ExpressionAttributeValues={":t": task_id})
    except Exception as e:
        logger.warning("set_thread_processing failed: %s", e)


def append_thread_message(session_id, thread_id, role, content, ts):
    try:
        sessions_table.update_item(Key={"session_id": session_id},
            UpdateExpression="SET threads.#tid.messages = list_append(if_not_exists(threads.#tid.messages, :e), :m)",
            ExpressionAttributeNames={"#tid": thread_id},
            ExpressionAttributeValues={":m": [{"role": role, "content": content[:10000], "timestamp": Decimal(str(ts))}], ":e": []})
    except Exception as e:
        logger.warning("append_thread_message failed: %s", e)


def load_recent_history(session_id, limit=10):
    try:
        resp = sessions_table.get_item(Key={"session_id": session_id}, ProjectionExpression="messages")
        msgs = resp.get("Item", {}).get("messages", [])
        return [{"role": m.get("role", "user"), "content": m.get("content", "")} for m in msgs[-limit:]]
    except: return []
