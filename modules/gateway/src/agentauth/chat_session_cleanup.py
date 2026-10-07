"""Recover session cleanup from the admitted sandbox's durable notification."""

import hashlib
import json
from datetime import datetime

from botocore.exceptions import BotoCoreError, ClientError

from src.agentauth.chat_capability import ChatAuthorizationRefusedError, ChatAuthorizationUnavailableError
from src.agentauth.chat_delivery import load_registered_delivery
from src.agentauth.chat_pending_handoff import input_transport
from src.agentauth.chat_session_mailbox import ChatSessionMailbox
from src.agentauth.chat_session_recovery import fence_expired_session
from src.orchestration.chat_data_migration import _owns_context_row

IDLE_SECONDS = 300
RETRY_SECONDS = 30


def request_session_end(table, *, session_id, owner, now, reason, expected_lease=None, expected_idle_snapshot=None):
    if reason not in {"user", "mode_switch", "idle", "authority"}:
        raise ChatAuthorizationRefusedError("chat end reason unavailable")
    key = {"PK": f"session#{session_id}", "SK": "header"}
    header = table.get_item(Key=key, ConsistentRead=True).get("Item") or {}
    if not _owns_context_row(header, owner) or header.get("status") != "active" or header.get("sessionMode") != "persistent":
        raise ChatAuthorizationRefusedError("chat session end refused")
    if expected_lease is not None and header.get("chatLease") != expected_lease:
        raise ChatAuthorizationRefusedError("chat session lease changed")
    if header.get("sessionState") in {"ending", "recovering", "ended"}:
        return header
    lease = header.get("chatLease")
    if not isinstance(lease, dict) or not lease.get("run_id") or not lease.get("sandbox_uid"):
        raise ChatAuthorizationRefusedError("chat session lease unavailable")
    try:
        table.update_item(
            Key=key,
            UpdateExpression="SET chatLease.expires_at = :fenced, sessionState = :ending, sessionEndReason = :reason, sessionCleanupStartedAt = :now",
            ConditionExpression=(
                "tenantId = :tenant AND teamId = :team AND ownerUserId = :user AND #status = :active "
                "AND #mode = :persistent AND #lease = :lease AND #state = :previous"
                + (" AND lastActivityAt = :activity AND sessionTurnSequence = :sequence" if expected_idle_snapshot is not None else "")
            ),
            ExpressionAttributeNames={"#status": "status", "#mode": "sessionMode", "#lease": "chatLease", "#state": "sessionState"},
            ExpressionAttributeValues={
                ":tenant": owner[0],
                ":team": owner[1],
                ":user": owner[2],
                ":active": "active",
                ":persistent": "persistent",
                ":lease": lease,
                ":previous": header.get("sessionState"),
                ":fenced": 1,
                ":ending": "ending",
                ":reason": reason,
                ":now": now,
                **({":activity": expected_idle_snapshot[0], ":sequence": expected_idle_snapshot[1]} if expected_idle_snapshot is not None else {}),
            },
        )
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            raise ChatAuthorizationRefusedError("chat session end changed") from None
        raise ChatAuthorizationUnavailableError("chat session end unavailable") from None
    except BotoCoreError:
        raise ChatAuthorizationUnavailableError("chat session end unavailable") from None

    return {**header, "chatLease": {**lease, "expires_at": 1}, "sessionState": "ending", "sessionEndReason": reason, "sessionCleanupStartedAt": now}


def publish_session_cleanup(authority, capabilities, item, *, now, transport=input_transport):
    if item.get("PK") != "chat-notifications" or not item.get("SK", "").startswith("cleanup#"):
        raise ChatAuthorizationRefusedError("chat cleanup key invalid")
    run_id = item["SK"].removeprefix("cleanup#")
    launch = capabilities.launches.load(run_id)
    delivery = load_registered_delivery(authority, run_id, launch.tenant_id)
    owner = (launch.tenant_id, launch.team_id, launch.user_id)
    if (
        not launch.session_run_id
        or not _owns_context_row(item, owner)
        or item.get("sessionId") != launch.session_id
        or item.get("sandboxUid") != launch.sandbox_uid
        or item.get("leaseGeneration") != launch.lease_generation
        or delivery.session_id != launch.session_id
        or delivery.tenant_id != launch.tenant_id
    ):
        raise ChatAuthorizationRefusedError("chat cleanup binding changed")
    store = authority.store
    teardown = store._read(f"CHAT-LAUNCH#{run_id}", "TEARDOWN") or {}
    terminal = store._read(f"CHAT-DELIVERY#{run_id}", "TERMINAL") or {}
    table = authority.context_table
    key = {"PK": item["PK"], "SK": item["SK"]}
    if "removed_at" in teardown and "completion_receipt" in terminal:
        header = table.get_item(Key={"PK": f"session#{launch.session_id}", "SK": "header"}, ConsistentRead=True).get("Item") or {}
        lease = header.get("chatLease", {})
        if (lease.get("run_id"), lease.get("sandbox_uid"), lease.get("generation")) == (
            run_id,
            launch.sandbox_uid,
            launch.lease_generation,
        ) and header.get("sessionState") in {"ending", "recovering"}:
            switching = header.get("sessionPendingMode") == "ephemeral"
            table.update_item(
                Key={"PK": f"session#{launch.session_id}", "SK": "header"},
                UpdateExpression=(
                    "SET sessionState = :idle, sessionMode = :ephemeral, sessionCleanupFinishedAt = :now "
                    "REMOVE sessionPendingMode, chatLease, sessionEndReason"
                    if switching
                    else "SET sessionState = :ended, sessionCleanupFinishedAt = :now"
                ),
                ConditionExpression=(
                    "chatLease = :lease AND sessionState = :previous AND tenantId = :tenant AND teamId = :team AND ownerUserId = :user"
                    + (" AND sessionPendingMode = :pending" if switching else " AND attribute_not_exists(sessionPendingMode)")
                ),
                ExpressionAttributeValues={
                    ":lease": lease,
                    ":previous": header["sessionState"],
                    **({":idle": "idle", ":ephemeral": "ephemeral", ":pending": "ephemeral"} if switching else {":ended": "ended"}),
                    ":now": now,
                    ":tenant": owner[0],
                    ":team": owner[1],
                    ":user": owner[2],
                },
            )
        table.delete_item(
            Key=key,
            ConditionExpression="sandboxUid = :uid AND leaseGeneration = :generation",
            ExpressionAttributeValues={":uid": launch.sandbox_uid, ":generation": launch.lease_generation},
        )
        return "removed"
    header = table.get_item(Key={"PK": f"session#{launch.session_id}", "SK": "header"}, ConsistentRead=True).get("Item") or {}
    if not _owns_context_row(header, owner):
        raise ChatAuthorizationRefusedError("chat cleanup owner changed")
    lease = header.get("chatLease", {})
    same_lease = (
        lease.get("run_id") == run_id and lease.get("sandbox_uid") == launch.sandbox_uid and lease.get("generation") == launch.lease_generation
    )
    if same_lease and header.get("sessionState") not in {"ending", "recovering", "ended"}:
        activity = datetime.fromisoformat(header["lastActivityAt"])
        if activity.tzinfo is None:
            raise ChatAuthorizationRefusedError("chat cleanup activity invalid")
        idle = activity.timestamp() + IDLE_SECONDS <= now
        sequence = ChatSessionMailbox._sequence(header)
        latest = table.get_item(Key={"PK": f"session#{launch.session_id}", "SK": f"mailbox#{sequence:08d}"}, ConsistentRead=True).get("Item", {})
        authority_expired = now >= launch.expires_at or not authority.current(launch, now)
        try:
            capabilities._member(launch.tenant_id, launch.user_id, launch.team_id)
        except ChatAuthorizationRefusedError:
            authority_expired = True
        finished = latest.get("status") in {"completed", "failed"}
        switch_ready = header.get("sessionPendingMode") == "ephemeral" and finished
        if not authority_expired and lease.get("expires_at", 0) > now and not (switch_ready or (idle and finished)):
            return "active"
        reason = "authority" if authority_expired else "mode_switch" if switch_ready else "idle"
        request_session_end(
            table,
            session_id=launch.session_id,
            owner=owner,
            now=now,
            reason=reason,
            expected_lease=lease,
            expected_idle_snapshot=(header["lastActivityAt"], sequence) if reason == "idle" and lease.get("expires_at", 0) > now else None,
        )
    if same_lease:
        fence_expired_session(authority, capabilities, launch, now=now)
    if item.get("publishedAt", 0) + RETRY_SECONDS > now:
        return "waiting"
    pointer = store._read(f"INVOCATION#{run_id}", "DISPATCH") or {}
    digest = pointer.get("envelope_digest", {}).get("S")
    if not isinstance(digest, str) or len(digest) != 64:
        raise ChatAuthorizationRefusedError("chat cleanup dispatch changed")
    message = {
        "notification_version": 1,
        "message_id": run_id,
        "session_id": launch.session_id,
        "task_id": delivery.task_id,
        "session_generation": delivery.session_generation,
        "session_mode": "persistent",
        "envelope_digest": digest,
    }
    client, queue = transport()
    response = client.send_message(
        QueueUrl=queue,
        MessageGroupId=launch.session_id,
        MessageDeduplicationId=hashlib.sha256(f"cleanup:{run_id}:{now // RETRY_SECONDS}".encode()).hexdigest(),
        MessageBody=json.dumps(message, sort_keys=True, separators=(",", ":")),
    )
    if not isinstance(response.get("MessageId"), str) or not response["MessageId"]:
        raise ChatAuthorizationRefusedError("chat cleanup dispatch unconfirmed")
    table.update_item(
        Key=key,
        UpdateExpression="SET publishedAt = :now ADD dispatchCount :one",
        ConditionExpression="sandboxUid = :uid AND leaseGeneration = :generation",
        ExpressionAttributeValues={":now": now, ":one": 1, ":uid": launch.sandbox_uid, ":generation": launch.lease_generation},
    )
    return "notified"
