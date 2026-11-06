"""Admit verified completed human turns to gateway-owned memory processing."""

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal

from src.agentauth.agentcore_authority import shared_owner_header
from src.agentauth.agentcore_governance import owner_state
from src.agentauth.agentcore_recall import RECALLED_CONTEXT_END, RECALLED_CONTEXT_START
from src.agentauth.bootstrap import BootstrapRefusedError
from src.agentauth.chat_admission import _encoded
from src.agentauth.chat_capability import ChatAuthorizationRefusedError
from src.orchestration.chat_data_migration import _owns_context_row

DOCUMENT_MARKERS = ("<document", "```document")


def eligible_capture(writer, launch, terminal, *, now):
    if terminal.get("outcome") != "completed" or not terminal.get("message_id"):
        return None
    header = shared_owner_header(writer.table, launch, require_shared=False)
    if header is None:
        return None
    memory_epoch = header.get("memoryEpoch", 0)
    if isinstance(memory_epoch, bool) or not isinstance(memory_epoch, int | Decimal) or memory_epoch < 0 or int(memory_epoch) != memory_epoch:
        return None
    owner = (launch.tenant_id, launch.team_id, launch.user_id)
    store = writer.authority.store
    try:
        writer.history.capabilities._member(launch.tenant_id, launch.user_id, launch.team_id)
        grant = store.live_grant(
            invocation_id=launch.run_id,
            tenant_id=launch.tenant_id,
            attempt=launch.attempt,
            now=datetime.fromtimestamp(now, UTC),
        )
    except (BootstrapRefusedError, ChatAuthorizationRefusedError):
        return None
    if (
        grant.grant_id != launch.grant_id
        or grant.revocation_epoch != launch.grant_epoch
        or grant.authority.kind != "chat_event"
        or grant.authority.human_id != launch.user_id
        or f"chat/{launch.session_id}" not in grant.repo_scope
    ):
        return None
    _, accepted = writer._accepted(launch)
    user_message, turn = accepted
    owner_epoch = turn.get("ownerEpoch")
    if (
        isinstance(owner_epoch, bool)
        or not isinstance(owner_epoch, int | Decimal)
        or owner_epoch < 0
        or int(owner_epoch) != owner_epoch
        or owner_epoch != owner_state(writer.table, launch)["epoch"]
    ):
        return None
    assistant = writer.history._get(launch.session_id, f"msg#{terminal['message_id']}")
    if (
        not assistant
        or not _owns_context_row(assistant, owner)
        or assistant.get("role") != "assistant"
        or assistant.get("runId") != launch.run_id
        or assistant.get("leaseGeneration") != launch.lease_generation
        or assistant.get("userTurnId") != turn["ref"]
        or user_message.get("parts")
        or assistant.get("parts")
    ):
        return None
    prompt, response = user_message.get("content"), assistant.get("content")
    if not all(isinstance(text, str) and text.strip() and len(text.encode()) <= 8192 for text in (prompt, response)):
        return None
    if any(marker.casefold() in prompt.casefold() for marker in (*DOCUMENT_MARKERS, RECALLED_CONTEXT_START, RECALLED_CONTEXT_END)):
        return None
    canonical = json.dumps(
        [launch.tenant_id, launch.user_id, launch.session_id, launch.run_id, turn["inputDigest"], terminal["message_id"]],
        separators=(",", ":"),
    )
    receipt_id = hashlib.sha256(canonical.encode()).hexdigest()
    record = {
        "PK": f"memory-capture#{receipt_id}",
        "SK": "capture",
        "tenantId": launch.tenant_id,
        "teamId": launch.team_id,
        "ownerUserId": launch.user_id,
        "sessionId": launch.session_id,
        "runId": launch.run_id,
        "attempt": launch.attempt,
        "grantId": launch.grant_id,
        "grantEpoch": launch.grant_epoch,
        "messageId": terminal["message_id"],
        "receiptId": receipt_id,
        "requestDigest": turn["inputDigest"],
        "userText": prompt,
        "status": "pending",
        "capturedAt": now,
        "memoryEpoch": int(memory_epoch),
        "ownerEpoch": int(owner_epoch),
    }
    return (
        [
            store._authority_check(grant),
            store._grant_check(grant, datetime.fromtimestamp(now, UTC)),
            writer._unchanged(user_message),
            writer._unchanged(turn),
            writer._unchanged(assistant),
            {"Put": {"TableName": writer.table.name, "Item": _encoded(record), "ConditionExpression": "attribute_not_exists(PK)"}},
        ],
        {"status": "captured_for_processing", "receipt_id": receipt_id},
    )
