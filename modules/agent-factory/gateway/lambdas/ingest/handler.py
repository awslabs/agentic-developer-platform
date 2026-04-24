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
    connection_id = event.get("requestContext", {}).get("connectionId", "")

    if route_key == "$connect":
        # API Gateway WebSocket runs the Cognito authorizer on $connect only;
        # subsequent $default invocations arrive without an authorizer context.
        # Persist the authorized claims keyed by connection_id so we can
        # reinject them on every message — otherwise the webchat adapter
        # drops all messages for lack of a resolvable sub (issue #88).
        _persist_connection_claims(connection_id, event.get("requestContext", {}).get("authorizer", {}))
        return {"statusCode": 200, "body": "Connected"}
    if route_key == "$disconnect":
        _forget_connection(connection_id)
        return {"statusCode": 200, "body": "Disconnected"}

    # For WebSocket message routes, pull the claims we stashed at $connect and
    # inject them back into event.requestContext.authorizer.claims so the
    # adapter's claims.get("sub") path works without re-authenticating.
    if connection_id:
        _restore_connection_claims(event, connection_id)

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


# ─── Connection claims persistence ────────────────────────────
# API Gateway WebSocket only runs the Cognito JWT authorizer on $connect.
# Subsequent $default / sendMessage invocations arrive without the authorizer
# context, so we stash the authorized sub/email/etc on $connect and rehydrate
# them on each message.  Keyed by connection_id (short-lived) with a TTL so
# abandoned connections get cleaned up.

CONNECTION_CLAIMS_TTL_SECONDS = 24 * 3600


def _persist_connection_claims(connection_id: str, authorizer_ctx: dict) -> None:
    if not connection_id:
        return
    # The gateway's custom authorizer puts claims under X-Agent-* context keys;
    # also accept Cognito-JWT-native "claims" dict as a fallback.
    sub = (
        authorizer_ctx.get("X-Agent-UserId")
        or authorizer_ctx.get("principalId")
        or authorizer_ctx.get("claims", {}).get("sub", "")
    )
    email = authorizer_ctx.get("X-Agent-Email") or authorizer_ctx.get("claims", {}).get("email", "")
    tenant_id = authorizer_ctx.get("X-Agent-Tenant") or authorizer_ctx.get("claims", {}).get("custom:tenant_id", "")
    if not sub:
        logger.warning(
            "Connection %s authorized but no sub/X-Agent-UserId in authorizer context; "
            "downstream messages will be dropped by the adapter.",
            connection_id,
        )
        return
    try:
        sessions_table.put_item(
            Item={
                "session_id": f"conn#{connection_id}",
                "kind": "connection_claims",
                "sub": sub,
                "email": email,
                "tenant_id": tenant_id,
                "expires_at": int(time.time()) + CONNECTION_CLAIMS_TTL_SECONDS,
            }
        )
        logger.info("Persisted connection claims for %s (sub=%s)", connection_id, sub)
    except Exception as e:
        logger.error("Failed to persist connection claims for %s: %s", connection_id, e)


def _forget_connection(connection_id: str) -> None:
    if not connection_id:
        return
    try:
        sessions_table.delete_item(Key={"session_id": f"conn#{connection_id}"})
    except Exception as e:
        logger.warning("Failed to clean up connection claims for %s: %s", connection_id, e)


def _restore_connection_claims(event: dict, connection_id: str) -> None:
    """Re-inject persisted claims into event.requestContext.authorizer.claims
    so adapters can use the normal claims.get("sub") path."""
    try:
        resp = sessions_table.get_item(Key={"session_id": f"conn#{connection_id}"})
        item = resp.get("Item") or {}
        if not item.get("sub"):
            return
        request_context = event.setdefault("requestContext", {})
        authorizer = request_context.setdefault("authorizer", {})
        claims = authorizer.setdefault("claims", {})
        # setdefault: don't overwrite if a real authorizer context is somehow
        # already present on this invocation.
        claims.setdefault("sub", item["sub"])
        if item.get("email"):
            claims.setdefault("email", item["email"])
        if item.get("tenant_id"):
            claims.setdefault("custom:tenant_id", item["tenant_id"])
    except Exception as e:
        logger.warning("Failed to restore connection claims for %s: %s", connection_id, e)


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

    # FIFO queues require MessageGroupId + MessageDeduplicationId. Group by
    # session_id so per-session turns serialize; different sessions stay
    # parallel. Dedup by task_id makes re-deliveries idempotent.
    send_kwargs = dict(
        QueueUrl=INPUT_QUEUE_URL,
        MessageBody=json.dumps({
            "task_id": task_id, "session_id": session_id, "thread_id": thread_id,
            "connection_id": connection_id, "channel": message.channel.value,
            "mode": "chat", "agent_type": classification.persona,
            "user_id": message.user_id,
            "repo_owner": (classification.repo or "").split("/")[0] if classification.repo and "/" in classification.repo else "",
            "repo_name": (classification.repo or "").split("/")[1] if classification.repo and "/" in classification.repo else "",
            "message": message.text, "platform_data": message.platform_data, "enqueued_at": now,
        }),
    )
    if INPUT_QUEUE_URL.endswith(".fifo"):
        send_kwargs["MessageGroupId"] = session_id
        send_kwargs["MessageDeduplicationId"] = task_id
    sqs.send_message(**send_kwargs)

    # Always send an acknowledgement. The classifier prompt asks for
    # escalation_note on non-direct paths, but LLMs occasionally omit it —
    # fall back so the user never stares at a silent "sent" message while the
    # long_running agent spins up.
    notify = classification.escalation_note or "On it — working on this now. I'll reply here when it's ready."
    send_notification(session_id, task_id, connection_id, message, notify, now)

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
    # Guard: reject empty / whitespace-only content
    if not content or not content.strip():
        logger.info("append_message: skipping empty content for session=%s role=%s", session_id, role)
        return

    # Dedupe: don't append if the last message is identical (same role + content
    # within a 5-second window).  Prevents the double-ack that happens when
    # both handle_direct_response and send_notification fire for the same
    # classification.
    try:
        resp = sessions_table.get_item(
            Key={"session_id": session_id},
            ProjectionExpression="messages",
        )
        messages = resp.get("Item", {}).get("messages", [])
        if messages:
            last = messages[-1]
            last_ts = float(last.get("timestamp", 0))
            if (
                last.get("role") == role
                and last.get("content") == content[:10000]
                and abs(ts - last_ts) < 5
            ):
                logger.info("append_message: deduped identical %s message for session=%s", role, session_id)
                return
    except Exception as e:
        logger.debug("append_message dedupe check failed (proceeding): %s", e)

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
