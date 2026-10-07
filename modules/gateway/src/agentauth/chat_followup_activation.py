"""Atomically exchange a sealed turn for fresh authority on the same sandbox."""

from datetime import UTC, datetime

from src.agentauth.chat_admission import OPERATIONS, PERSISTENT_LEASE_SECONDS, _encoded
from src.agentauth.chat_capability import ChatAuthorizationRefusedError, ChatLaunch, ChatLaunchStore, require_sandbox_run
from src.agentauth.chat_followup_admission import claim_followup, pending_mailbox_root
from src.agentauth.chat_history_store import ChatHistoryStore
from src.agentauth.chat_session_mailbox import ChatSessionMailbox
from src.agentauth.chat_teardown import ChatTeardown
from src.agentauth.chat_terminal_delivery import verify_terminal_delivery
from src.agentauth.chat_turn_finalization import ChatTurnFinalizer
from src.agentauth.chat_user_turn import consume_user_turn, load_user_turn, user_turn_records
from src.orchestration.chat_data_migration import _owner_fields


def activate_followup(authority, capabilities, launch, *, after, now):
    capabilities._live(launch, now)
    if not launch.session_run_id:
        raise ChatAuthorizationRefusedError("chat persistent binding required")
    table, store = authority.context_table, authority.store
    mailbox = ChatSessionMailbox(table)
    writer = ChatTurnFinalizer(authority, ChatHistoryStore(table, capabilities))
    _, (previous_message, previous_receipt) = writer._accepted(launch)
    sequence = mailbox.initial_cursor(
        session_id=launch.session_id,
        owner=(launch.tenant_id, launch.team_id, launch.user_id),
        run_id=launch.run_id,
        sandbox_uid=launch.sandbox_uid,
        generation=launch.lease_generation,
        message=previous_message["content"],
        now=now,
    )
    if after != sequence:
        raise ChatAuthorizationRefusedError("chat follow-up cursor changed")
    turn = mailbox.next_turn(
        session_id=launch.session_id,
        owner=(launch.tenant_id, launch.team_id, launch.user_id),
        run_id=launch.run_id,
        sandbox_uid=launch.sandbox_uid,
        generation=launch.lease_generation,
        after=after,
        now=now,
    )
    if turn is None:
        raise ChatAuthorizationRefusedError("chat follow-up unavailable")
    claim_followup(authority, launch, turn, after, now)
    pointer, execution, grant, digest, _ = pending_mailbox_root(authority, launch, turn, now)
    previous = store._read(f"TENANT#{launch.tenant_id}", f"EXEC#{launch.run_id}") or {}
    admission = store._read(f"CHAT-SESSION-TURN#{turn['turn_id']}", "ADMISSION")
    metadata = store._read(f"TENANT#{launch.tenant_id}", f"EXEC#{turn['turn_id']}") or {}
    terminal = previous_receipt.get("terminal_result")
    if (
        previous.get("status") != {"S": "active"}
        or previous.get("chat_turn_sealed") != {"BOOL": True}
        or not isinstance(terminal, dict)
        or terminal.get("outcome") not in {"completed", "failed"}
        or terminal.get("accounting_status") not in {"not_used", "settled"}
        or previous.get("chat_terminal") != {"M": _encoded(terminal)}
        or "abort_command_id" in previous
        or metadata.get("status") != {"S": "pending"}
        or "workload_binding" in metadata
        or "abort_command_id" in metadata
        or admission is None
    ):
        raise ChatAuthorizationRefusedError("chat previous turn not committed")
    delivery = verify_terminal_delivery(store, previous, launch, terminal)
    if delivery is not None and "completion_receipt" not in delivery:
        raise ChatAuthorizationRefusedError("chat previous delivery not committed")
    header = writer.history._get(launch.session_id, "header")
    lease = (header or {}).get("chatLease", {})
    if (
        header is None
        or lease.get("run_id") != launch.run_id
        or lease.get("sandbox_uid") != launch.sandbox_uid
        or lease.get("generation") != launch.lease_generation
        or lease.get("expires_at", 0) <= now
    ):
        raise ChatAuthorizationRefusedError("chat session changed")
    pending_entry = writer.history._get(launch.session_id, f"mailbox#{turn['sequence']:08d}")
    pending_receipt = writer.history._get(launch.session_id, f"mailbox-id#{turn['turn_id']}")
    if (
        pending_entry is None
        or pending_receipt is None
        or pending_entry.get("status") != "queued"
        or pending_entry.get("turnId") != turn["turn_id"]
        or pending_entry.get("message") != turn["message"]
        or pending_receipt.get("sequence") != turn["sequence"]
        or pending_receipt.get("digest") != admission["turn_digest"]["S"]
    ):
        raise ChatAuthorizationRefusedError("chat follow-up changed")
    writer.history._check_row(pending_entry, header)
    writer.history._check_row(pending_receipt, header)
    pod = authority.workloads.verify_bound(name=previous["pod_name"]["S"], uid=launch.sandbox_uid)
    require_sandbox_run(pod, launch.session_run_id)
    binding = store._read(f"POD#{launch.sandbox_uid}", "BINDING") or {}
    if (
        pod.uid != launch.sandbox_uid
        or pod.image_digest != launch.image_digest
        or binding.get("invocation_id") != {"S": launch.session_run_id}
        or binding.get("tenant_id") != {"S": launch.tenant_id}
    ):
        raise ChatAuthorizationRefusedError("chat persistent pod changed")
    expires_at = int(grant.expires_at.timestamp())
    if pod.deadline_at:
        expires_at = min(expires_at, int(datetime.fromisoformat(pod.deadline_at.replace("Z", "+00:00")).timestamp()))
    following = ChatLaunch(
        run_id=turn["turn_id"],
        tenant_id=launch.tenant_id,
        user_id=launch.user_id,
        team_id=launch.team_id,
        session_id=launch.session_id,
        session_run_id=launch.session_run_id,
        sandbox_uid=launch.sandbox_uid,
        image_digest=launch.image_digest,
        attempt=execution.current_attempt,
        credential_epoch=execution.current_credential_epoch,
        lease_generation=launch.lease_generation + 1,
        grant_id=grant.grant_id,
        grant_epoch=grant.revocation_epoch,
        operations=OPERATIONS,
        expires_at=expires_at,
    )
    reference = load_user_turn(store, execution, digest)
    payload, input_delete = consume_user_turn(table, following, reference, digest, now)
    records, version, ordinal = user_turn_records(table, header, following, payload)
    header_update = writer._unchanged(header)["ConditionCheck"]
    header_update["UpdateExpression"] = "SET chatLease = :lease, historyVersion = :version, historyNextOrdinal = :ordinal"
    header_update["ExpressionAttributeValues"].update(
        _encoded(
            {
                ":lease": {
                    "run_id": following.run_id,
                    "sandbox_uid": following.sandbox_uid,
                    "generation": following.lease_generation,
                    "expires_at": min(now + PERSISTENT_LEASE_SECONDS, expires_at),
                },
                ":version": version,
                ":ordinal": ordinal,
            }
        )
    )
    evidence = ChatTeardown(authority, capabilities.launches)
    previous_update = evidence._unchanged(previous)
    previous_update["ConditionExpression"] += " AND attribute_not_exists(abort_command_id)"
    previous_update["UpdateExpression"] = "SET #completed = :completed"
    previous_update["ExpressionAttributeNames"]["#completed"] = "status"
    previous_update["ExpressionAttributeValues"][":completed"] = {"S": "completed"}
    execution_update = evidence._unchanged(metadata)
    execution_update["ConditionExpression"] += " AND attribute_not_exists(workload_binding) AND attribute_not_exists(abort_command_id)"
    execution_update["UpdateExpression"] = "SET #active = :active, workload_binding = :pod, pod_name = :name"
    execution_update["ExpressionAttributeNames"]["#active"] = "status"
    execution_update["ExpressionAttributeValues"].update(_encoded({":active": "active", ":pod": launch.sandbox_uid, ":name": pod.name}))
    receipt, entry = mailbox.finalized_entry(
        session_id=launch.session_id,
        owner=(launch.tenant_id, launch.team_id, launch.user_id),
        run_id=launch.run_id,
        message=previous_message["content"],
        outcome=terminal["outcome"],
        finalized_at=terminal["finalized_at"],
    )
    instant = datetime.fromtimestamp(now, UTC)
    previous_grant = store.live_grant(invocation_id=launch.run_id, tenant_id=launch.tenant_id, attempt=launch.attempt, now=instant)
    if previous_grant.grant_id != launch.grant_id or previous_grant.revocation_epoch != launch.grant_epoch:
        raise ChatAuthorizationRefusedError("chat previous grant changed")
    checks = [
        {"Update": header_update},
        {"Update": previous_update},
        {"Update": execution_update},
        {
            "Delete": {
                "TableName": table.name,
                "Key": _encoded({"PK": "chat-notifications", "SK": f"cleanup#{launch.run_id}"}),
                "ConditionExpression": (
                    "tenantId = :tenant AND teamId = :team AND ownerUserId = :user AND sessionId = :session "
                    "AND sandboxUid = :pod AND leaseGeneration = :generation"
                ),
                "ExpressionAttributeValues": _encoded(
                    {
                        ":tenant": launch.tenant_id,
                        ":team": launch.team_id,
                        ":user": launch.user_id,
                        ":session": launch.session_id,
                        ":pod": launch.sandbox_uid,
                        ":generation": launch.lease_generation,
                    }
                ),
            }
        },
        {
            "Put": {
                "TableName": table.name,
                "Item": _encoded(
                    {
                        "PK": "chat-notifications",
                        "SK": f"cleanup#{following.run_id}",
                        **_owner_fields((following.tenant_id, following.team_id, following.user_id)),
                        "sessionId": following.session_id,
                        "sandboxUid": following.sandbox_uid,
                        "leaseGeneration": following.lease_generation,
                        "ttl": header["ttl"],
                    }
                ),
                "ConditionExpression": "attribute_not_exists(PK)",
            }
        },
        input_delete,
        writer._unchanged(previous_receipt),
        writer._unchanged(receipt),
        writer._unchanged(entry),
        writer._unchanged(previous_message),
        writer._unchanged(pending_entry),
        writer._unchanged(pending_receipt),
        {"ConditionCheck": evidence._unchanged(pointer)},
        {"ConditionCheck": evidence._unchanged(admission)},
        {"ConditionCheck": evidence._unchanged(binding)},
        store._grant_check(grant, instant),
        store._authority_check(grant),
        store._grant_check(previous_grant, instant),
        *([store._authority_check(previous_grant)] if previous_grant.authority.reference_id != grant.authority.reference_id else []),
        {"Put": {"TableName": store.table, "Item": ChatLaunchStore.item(following), "ConditionExpression": "attribute_not_exists(pk)"}},
        *[{"Put": {"TableName": table.name, "Item": _encoded(record), "ConditionExpression": "attribute_not_exists(PK)"}} for record in records],
        *([{"ConditionCheck": evidence._unchanged(delivery)}] if delivery is not None else []),
    ]
    writer._transact(checks)
    return following
