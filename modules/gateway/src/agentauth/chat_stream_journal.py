"""Fence provisional event commits with the same lease and grant as model output."""

from datetime import UTC, datetime

from botocore.exceptions import BotoCoreError, ClientError

from src.agentauth.chat_capability import ChatAuthorizationRefusedError, ChatAuthorizationUnavailableError, ChatLaunchStore
from src.agentauth.chat_event_journal import ChatEventJournal
from src.agentauth.chat_history_store import ChatHistoryStore
from src.agentauth.chat_history_write import ChatHistoryWriter
from src.agentauth.chat_teardown import ChatTeardown
from src.orchestration.chat_data_migration import _owns_context_row


def append_stream_event(authority, launch, delivery, event_id, payload, *, now):
    if any(getattr(delivery, field) != getattr(launch, field) for field in ("tenant_id", "team_id", "user_id", "session_id", "run_id")):
        raise ChatAuthorizationRefusedError("chat stream delivery changed")
    store = authority.store
    owner = (launch.tenant_id, launch.team_id, launch.user_id)
    writer = ChatHistoryWriter(authority, ChatHistoryStore(authority.context_table, None))
    evidence = ChatTeardown(authority, None)
    for _attempt in range(5):
        header = authority.context_table.get_item(Key={"PK": f"session#{launch.session_id}", "SK": "header"}, ConsistentRead=True).get("Item", {})
        lease = header.get("chatLease", {})
        metadata = store._read(f"TENANT#{launch.tenant_id}", f"EXEC#{launch.run_id}") or {}
        instant = datetime.fromtimestamp(now, UTC)
        grant = store.live_grant(invocation_id=launch.run_id, tenant_id=launch.tenant_id, attempt=launch.attempt, now=instant)
        if (
            not _owns_context_row(header, owner)
            or header.get("ttl", 0) <= now
            or header.get("status") != "active"
            or lease.get("run_id") != launch.run_id
            or lease.get("sandbox_uid") != launch.sandbox_uid
            or lease.get("generation") != launch.lease_generation
            or lease.get("expires_at", 0) <= now
            or metadata.get("status") != {"S": "active"}
            or metadata.get("workload_binding") != {"S": launch.sandbox_uid}
            or metadata.get("current_attempt") != {"N": str(launch.attempt)}
            or metadata.get("current_credential_epoch") != {"N": str(launch.credential_epoch)}
            or any(field in metadata for field in ("abort_command_id", "chat_turn_sealed", "chat_terminal"))
            or grant.grant_id != launch.grant_id
            or grant.revocation_epoch != launch.grant_epoch
        ):
            raise ChatAuthorizationRefusedError("chat stream authority changed")
        event, writes = ChatEventJournal(authority.context_table).prepare(delivery, event_id, "ag_ui", payload, now=now)
        execution_check = evidence._unchanged(metadata)
        execution_check["ConditionExpression"] += (
            " AND attribute_not_exists(abort_command_id) AND attribute_not_exists(chat_turn_sealed) AND attribute_not_exists(chat_terminal)"
        )
        try:
            store.client.transact_write_items(
                TransactItems=[
                    *writes,
                    writer._unchanged(header),
                    {"ConditionCheck": execution_check},
                    {"ConditionCheck": evidence._unchanged(ChatLaunchStore.item(launch))},
                    store._authority_check(grant),
                    store._grant_check(grant, instant),
                ]
            )
            return event
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") != "TransactionCanceledException":
                raise ChatAuthorizationUnavailableError("chat stream journal unavailable") from None
        except BotoCoreError:
            raise ChatAuthorizationUnavailableError("chat stream journal unavailable") from None
    raise ChatAuthorizationUnavailableError("chat stream journal contention")
