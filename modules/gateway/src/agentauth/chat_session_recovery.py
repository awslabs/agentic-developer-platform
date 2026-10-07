"""Recover persistent launch intent and fence an expired sandbox before removal."""

import hashlib
from datetime import UTC, datetime

from botocore.exceptions import BotoCoreError, ClientError

from src.agentauth.chat_admission import _encoded, root_identity
from src.agentauth.chat_capability import ChatAuthorizationRefusedError, ChatAuthorizationUnavailableError, ChatLaunchStore
from src.agentauth.chat_history_store import ChatHistoryStore
from src.agentauth.chat_history_write import ChatHistoryConflictError, ChatHistoryWriter
from src.agentauth.chat_session_mailbox import ChatSessionMailbox
from src.agentauth.chat_teardown import ChatTeardown


def fence_expired_session(authority, capabilities, launch, *, now):
    if not launch.session_run_id:
        return False
    store = authority.store
    metadata = store._read(f"TENANT#{launch.tenant_id}", f"EXEC#{launch.run_id}") or {}
    binding = _encoded({"run_id": launch.run_id, "sandbox_uid": launch.sandbox_uid, "lease_generation": launch.lease_generation})
    if "chat_session_lost" in metadata:
        if metadata["chat_session_lost"] != {"M": binding}:
            raise ChatAuthorizationRefusedError("chat recovery fence changed")
        return True
    mailbox = ChatSessionMailbox(authority.context_table)
    header = mailbox._header(launch.session_id, (launch.tenant_id, launch.team_id, launch.user_id), now)
    lease = (header or {}).get("chatLease", {})
    if any(lease.get(field) != getattr(launch, field) for field in ("run_id", "sandbox_uid")) or lease.get("generation") != launch.lease_generation:
        return False
    if lease.get("expires_at", 0) > now:
        return False
    if metadata.get("workload_binding") != {"S": launch.sandbox_uid} or metadata.get("current_attempt") != {"N": str(launch.attempt)}:
        raise ChatAuthorizationRefusedError("chat recovery execution changed")
    writer = ChatHistoryWriter(authority, ChatHistoryStore(authority.context_table, capabilities))
    header_update = writer._unchanged(header)["ConditionCheck"]
    header_update["UpdateExpression"] = "SET chatLease.expires_at = :fenced, sessionState = :recovering"
    header_update["ExpressionAttributeValues"].update(_encoded({":fenced": 1, ":recovering": "recovering"}))
    evidence = ChatTeardown(authority, capabilities.launches)
    execution_update = evidence._unchanged(metadata)
    execution_update["ConditionExpression"] += " AND attribute_not_exists(chat_session_lost)"
    if "chat_terminal" not in metadata:
        execution_update["ConditionExpression"] += " AND attribute_not_exists(chat_terminal)"
    execution_update["UpdateExpression"] = "SET chat_session_lost = :lost"
    execution_update["ExpressionAttributeValues"][":lost"] = {"M": binding}
    if "chat_terminal" in metadata:
        execution_update["UpdateExpression"] += ", #recovered_status = :completed"
        execution_update["ExpressionAttributeNames"]["#recovered_status"] = "status"
        execution_update["ExpressionAttributeValues"][":completed"] = {"S": "completed"}
    _transact(
        store,
        [
            {"Update": header_update},
            {"Update": execution_update},
            {"ConditionCheck": evidence._unchanged(ChatLaunchStore.item(launch))},
        ],
    )
    return True


def resume_persistent_creation(authority, capabilities, delivery, pointer, execution, creation, *, now):
    mailbox = ChatSessionMailbox(authority.context_table)
    header = mailbox._header(delivery.session_id, (delivery.tenant_id, delivery.team_id, delivery.user_id), now)
    if not header or mailbox._mode(header) != "persistent":
        return None
    if execution.get("status", {}).get("S") not in {"pending", "active"} or any(
        field in execution for field in ("abort_command_id", "chat_terminal", "chat_pre_admission_cleanup", "chat_pre_admission_terminal")
    ):
        return None
    binding = creation["binding"]["M"]
    if (
        authority.workloads.find_exited_sandbox(
            name=binding["pod_name"]["S"], run_hash=hashlib.sha256(delivery.run_id.encode()).hexdigest(), image_digest=binding["image_digest"]["S"]
        )
        is not None
    ):
        return None
    pod = authority.workloads.find_reserved_sandbox(
        name=binding["pod_name"]["S"],
        run_hash=hashlib.sha256(delivery.run_id.encode()).hexdigest(),
        image_digest=binding["image_digest"]["S"],
        session_hash=hashlib.sha256(delivery.session_id.encode()).hexdigest(),
    )
    store = authority.store
    _, grant, _ = root_identity(authority, delivery.run_id, pointer["envelope_digest"]["S"], now)
    evidence = ChatTeardown(authority, capabilities.launches)
    writer = ChatHistoryWriter(authority, ChatHistoryStore(authority.context_table, capabilities))
    execution_check = evidence._unchanged(execution)
    execution_check["ConditionExpression"] += "".join(
        f" AND attribute_not_exists({field})"
        for field in ("abort_command_id", "chat_terminal", "chat_pre_admission_cleanup", "chat_pre_admission_terminal")
    )
    _transact(
        store,
        [
            *[{"ConditionCheck": evidence._unchanged(item)} for item in (pointer, creation)],
            {"ConditionCheck": execution_check},
            writer._unchanged(header),
            store._authority_check(grant),
            store._grant_check(grant, datetime.fromtimestamp(now, UTC)),
            {
                "ConditionCheck": {
                    "TableName": store.table,
                    "Key": _encoded({"pk": f"CHAT-LAUNCH#{delivery.run_id}", "sk": "LAUNCH"}),
                    "ConditionExpression": "attribute_not_exists(pk)",
                }
            },
        ],
    )
    return {
        "state": "creation_reserved",
        "session_mode": "persistent",
        "pod_name": binding["pod_name"]["S"],
        "image_digest": binding["image_digest"]["S"],
        "attempt": int(binding["attempt"]["N"]),
        **({"sandbox_uid": pod.uid} if pod is not None else {}),
    }


def _transact(store, checks):
    try:
        store.client.transact_write_items(TransactItems=checks)
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") == "TransactionCanceledException":
            raise ChatHistoryConflictError("chat session recovery changed; retry") from None
        raise ChatAuthorizationUnavailableError("chat session recovery unavailable") from None
    except BotoCoreError:
        raise ChatAuthorizationUnavailableError("chat session recovery unavailable") from None
