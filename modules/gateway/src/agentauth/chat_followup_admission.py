"""Fresh, fenced root claims for persistent-session follow-ups."""

import hashlib
import json
from datetime import UTC, datetime
from types import SimpleNamespace

from boto3.dynamodb.types import TypeDeserializer
from botocore.exceptions import BotoCoreError, ClientError

from src.agentauth.chat_admission import _encoded, root_identity
from src.agentauth.chat_capability import ChatAuthorizationRefusedError, ChatAuthorizationUnavailableError
from src.agentauth.chat_session_mailbox import AcceptedTurn, ChatSessionMailbox
from src.agentauth.chat_terminal_delivery import verify_terminal_delivery
from src.agentauth.chat_user_turn import consume_user_turn, load_user_turn
from src.agentauth.execution import ExecutionStatus


def previous_delivery_complete(authority, launch):
    store = authority.store
    execution = store._read(f"TENANT#{launch.tenant_id}", f"EXEC#{launch.run_id}") or {}
    if "chat_terminal_delivery_digest" not in execution:
        return True
    if "chat_terminal" not in execution:
        raise ChatAuthorizationUnavailableError("chat terminal delivery inconsistent")
    terminal = TypeDeserializer().deserialize(execution["chat_terminal"])
    delivery = verify_terminal_delivery(store, execution, launch, terminal)
    return delivery is not None and "completion_receipt" in delivery


def pending_mailbox_root(authority, launch, turn, now):
    if turn["turn_id"] == launch.run_id:
        raise ChatAuthorizationRefusedError("chat initial turn already admitted")
    store = authority.store
    pointer = store._read(f"INVOCATION#{turn['turn_id']}", "DISPATCH") or {}
    digest = pointer.get("envelope_digest", {}).get("S")
    if not isinstance(digest, str) or len(digest) != 64:
        raise ChatAuthorizationRefusedError("chat follow-up root unavailable")
    execution, grant, session_id = root_identity(authority, turn["turn_id"], digest, now)
    if (
        execution.status != ExecutionStatus.PENDING
        or execution.tenant_id != launch.tenant_id
        or grant.authority.human_id != launch.user_id
        or session_id != launch.session_id
    ):
        raise ChatAuthorizationRefusedError("chat follow-up authority changed")
    reference = load_user_turn(store, execution, digest)
    if reference is None:
        raise ChatAuthorizationRefusedError("chat follow-up has no protected input")
    root = SimpleNamespace(run_id=turn["turn_id"], tenant_id=launch.tenant_id, user_id=launch.user_id)
    protected, input_delete = consume_user_turn(authority.context_table, root, reference, digest, now)
    if protected["input"]["message"] != turn["message"]:
        raise ChatAuthorizationRefusedError("chat follow-up input changed")
    return pointer, execution, grant, digest, input_delete


def claim_followup(authority, launch, turn, after, now):
    """Fence a fresh turn to the original pod without changing its immutable binding."""
    pointer, execution, grant, digest, input_delete = pending_mailbox_root(authority, launch, turn, now)
    store, table = authority.store, authority.context_table
    mailbox = ChatSessionMailbox(table)
    if (
        mailbox.next_turn(
            session_id=launch.session_id,
            owner=(launch.tenant_id, launch.team_id, launch.user_id),
            run_id=launch.run_id,
            sandbox_uid=launch.sandbox_uid,
            generation=launch.lease_generation,
            after=after,
            now=now,
        )
        != turn
    ):
        raise ChatAuthorizationRefusedError("chat follow-up mailbox changed")
    prefix = f"session#{launch.session_id}"
    rows = [
        table.get_item(Key={"PK": prefix, "SK": key}, ConsistentRead=True).get("Item")
        for key in ("header", f"mailbox#{after:08d}", f"mailbox#{turn['sequence']:08d}", f"mailbox-id#{turn['turn_id']}")
    ]
    if any(row is None for row in rows):
        raise ChatAuthorizationRefusedError("chat follow-up mailbox changed")
    header, previous, entry, receipt = rows
    turn_digest = hashlib.sha256(
        json.dumps(AcceptedTurn(turn_id=turn["turn_id"], message=turn["message"]).model_dump(), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    lease = header.get("chatLease")
    if (
        not isinstance(lease, dict)
        or lease.get("run_id") != launch.run_id
        or lease.get("sandbox_uid") != launch.sandbox_uid
        or lease.get("generation") != launch.lease_generation
        or lease.get("expires_at", 0) <= now
        or header.get("sessionMode") != "persistent"
        or previous.get("status") not in {"completed", "failed"}
        or entry.get("status") != "queued"
        or entry.get("turnId") != turn["turn_id"]
        or entry.get("message") != turn["message"]
        or receipt.get("sequence") != turn["sequence"]
        or receipt.get("digest") != turn_digest
    ):
        raise ChatAuthorizationRefusedError("chat follow-up mailbox changed")
    binding = store._read(f"POD#{launch.sandbox_uid}", "BINDING") or {}
    if (
        binding.get("invocation_id") != {"S": launch.session_run_id or launch.run_id}
        or binding.get("tenant_id") != {"S": launch.tenant_id}
        or binding.get("attempt") != {"N": str(launch.attempt)}
    ):
        raise ChatAuthorizationRefusedError("chat session pod changed")
    metadata = store._read(f"TENANT#{launch.tenant_id}", f"EXEC#{turn['turn_id']}") or {}
    original = store._read(f"TENANT#{launch.tenant_id}", f"EXEC#{launch.run_id}") or {}
    if (
        metadata.get("status") != {"S": "pending"}
        or metadata.get("repo") != {"S": f"chat/{launch.session_id}"}
        or metadata.get("current_attempt") != {"N": str(execution.current_attempt)}
        or "workload_binding" in metadata
        or "abort_command_id" in metadata
        or original.get("status") != {"S": "active"}
        or original.get("workload_binding") != {"S": launch.sandbox_uid}
        or original.get("current_attempt") != {"N": str(launch.attempt)}
        or "abort_command_id" in original
    ):
        raise ChatAuthorizationRefusedError("chat follow-up execution changed")
    record = _encoded(
        {
            "pk": f"CHAT-SESSION-TURN#{turn['turn_id']}",
            "sk": "ADMISSION",
            "session_id": launch.session_id,
            "session_run_id": launch.session_run_id or launch.run_id,
            "previous_run_id": launch.run_id,
            "tenant_id": launch.tenant_id,
            "user_id": launch.user_id,
            "team_id": launch.team_id,
            "sandbox_uid": launch.sandbox_uid,
            "lease_generation": launch.lease_generation,
            "sequence": turn["sequence"],
            "turn_digest": turn_digest,
            "envelope_digest": digest,
            "attempt": execution.current_attempt,
            "grant_id": grant.grant_id,
            "grant_epoch": grant.revocation_epoch,
        }
    )

    def unchanged(table_name, row, keys):
        fields = [field for field in row if field not in keys]
        return {
            "ConditionCheck": {
                "TableName": table_name,
                "Key": {field: row[field] for field in keys},
                "ConditionExpression": " AND ".join(f"#field{index} = :value{index}" for index in range(len(fields))),
                "ExpressionAttributeNames": {f"#field{index}": field for index, field in enumerate(fields)},
                "ExpressionAttributeValues": {f":value{index}": row[field] for index, field in enumerate(fields)},
            }
        }

    instant = datetime.fromtimestamp(now, UTC)
    original_grant = store.live_grant(invocation_id=launch.run_id, tenant_id=launch.tenant_id, attempt=launch.attempt, now=instant)
    if original_grant.grant_id != launch.grant_id or original_grant.revocation_epoch != launch.grant_epoch:
        raise ChatAuthorizationRefusedError("chat session grant changed")
    existing = store._read(record["pk"]["S"], "ADMISSION")
    if existing is not None and existing != record:
        raise ChatAuthorizationRefusedError("chat follow-up already bound")
    metadata_check = unchanged(store.table, metadata, ("pk", "sk"))
    metadata_check["ConditionCheck"]["ConditionExpression"] += (
        " AND attribute_not_exists(workload_binding) AND attribute_not_exists(abort_command_id)"
    )
    original_check = unchanged(store.table, original, ("pk", "sk"))
    original_check["ConditionCheck"]["ConditionExpression"] += " AND attribute_not_exists(abort_command_id)"
    checks = [
        *[unchanged(table.name, _encoded(row), ("PK", "SK")) for row in rows],
        *[unchanged(store.table, row, ("pk", "sk")) for row in (pointer, binding)],
        metadata_check,
        original_check,
        {"ConditionCheck": input_delete["Delete"]},
        store._grant_check(grant, instant),
        store._authority_check(grant),
        *([store._grant_check(original_grant, instant)] if original_grant.principal != grant.principal else []),
        *([store._authority_check(original_grant)] if original_grant.authority.reference_id != grant.authority.reference_id else []),
        unchanged(store.table, existing, ("pk", "sk"))
        if existing is not None
        else {"Put": {"TableName": store.table, "Item": record, "ConditionExpression": "attribute_not_exists(pk)"}},
    ]
    try:
        store.client.transact_write_items(TransactItems=checks)
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") == "TransactionCanceledException":
            raise ChatAuthorizationRefusedError("chat follow-up admission changed") from None
        raise ChatAuthorizationUnavailableError("chat follow-up admission unavailable") from None
    except BotoCoreError:
        raise ChatAuthorizationUnavailableError("chat follow-up admission unavailable") from None
