"""Recover retained abort pods independently of SQL work-claim ownership."""
from __future__ import annotations

import logging
from datetime import UTC, datetime

from botocore.exceptions import BotoCoreError, ClientError

from src.activity.liveness import OBSERVED_TERMINAL_STATUSES
from src.agentauth.abort_reconciliation import repair_aborted_terminal_status
from src.agentauth.exit_retention import ExitRetentionError
from src.agentauth.registration import CONTROL_ATTRIBUTES
from src.agentauth.store import AuthorityStoreError

logger = logging.getLogger(__name__)


def _retire_execution(store, *, raw, tenant, invocation, event, events_table):
    """Fence authority retirement against both the observed report and attempt.

    A crash between report repair and this transaction is retryable: the event
    stays terminal, and discovery still finds the retained pod. Normal terminal
    reports win a race without being overwritten; the next pass reads their state.
    """
    if raw.get("status") != {"S": "active"}:
        if raw.get("status", {}).get("S") not in {"completed", "cancelled", "revoked"}:
            raise AuthorityStoreError("unexpected retained execution state")
        return
    store.client.transact_write_items(TransactItems=[
        {"ConditionCheck": {
            "TableName": events_table,
            "Key": {"event_id": {"S": invocation}, "arrived_at": raw["arrived_at"]},
            "ConditionExpression": "tenant_id = :tenant AND #s = :outcome",
            "ExpressionAttributeNames": {"#s": "status"},
            "ExpressionAttributeValues": {":tenant": {"S": tenant}, ":outcome": event["status"]},
        }},
        {"Update": {
            "TableName": store.table,
            "Key": {"pk": {"S": f"TENANT#{tenant}"}, "sk": {"S": f"EXEC#{invocation}"}},
            "ConditionExpression": ("#s = :active AND tenant_id = :tenant AND workload_binding = :uid "
                                    "AND current_attempt = :attempt AND abort_command_id = :command"),
            "UpdateExpression": "SET #s = :complete, terminal_outcome = :outcome, terminal_reconciled_by = :source",
            "ExpressionAttributeNames": {"#s": "status"},
            "ExpressionAttributeValues": {
                ":active": {"S": "active"}, ":complete": {"S": "completed"},
                ":tenant": {"S": tenant}, ":uid": raw["workload_binding"],
                ":attempt": raw["current_attempt"], ":command": raw["abort_command_id"],
                ":outcome": event["status"], ":source": {"S": "retained_abort_recovery"},
            },
        }},
    ])


def _recover_interrupted_acceptance(store, *, raw, tenant, invocation, events_table):
    """Atomically report an exited run while fencing a delayed abort acceptance.

    No marker means no accepted abort. If the run has no terminal report, report
    failed execution instead. A concurrent marker or normal terminal report makes
    the entire transaction fail; discovery retries from fresh protected state.
    """
    if not events_table or not raw.get("arrived_at"):
        raise AuthorityStoreError("event row key unavailable")
    key = {"event_id": {"S": invocation}, "arrived_at": raw["arrived_at"]}
    event = store.client.get_item(TableName=events_table, Key=key, ConsistentRead=True).get("Item", {})
    if event.get("tenant_id") != {"S": tenant}:
        raise AuthorityStoreError("event row unavailable or tenant mismatch")
    prior = event.get("status")
    terminal = (prior or {}).get("S") in OBSERVED_TERMINAL_STATUSES
    if raw.get("status") != {"S": "active"}:
        if raw.get("status", {}).get("S") not in {"completed", "cancelled", "revoked"} or not terminal:
            raise AuthorityStoreError("terminal reporting remains unresolved")
        return
    outcome = prior if terminal else {"S": "failed"}
    values = {":tenant": {"S": tenant}}
    condition = "tenant_id = :tenant AND "
    if prior is None:
        condition += "attribute_not_exists(#s)"
    else:
        condition += "#s = :prior"
        values[":prior"] = prior
    update = "REMOVE " + ", ".join(CONTROL_ATTRIBUTES)
    if not terminal:
        values.update({":failed": outcome, ":now": {"S": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")},
                       ":reason": {"S": "worker_exited_without_terminal_report"}})
        update = "SET #s = :failed, status_updated_at = :now, stop_reason = :reason " + update
    store.client.transact_write_items(TransactItems=[
        {"Update": {
            "TableName": events_table, "Key": key,
            "ConditionExpression": condition, "UpdateExpression": update,
            "ExpressionAttributeNames": {"#s": "status"}, "ExpressionAttributeValues": values,
        }},
        {"Update": {
            "TableName": store.table,
            "Key": {"pk": {"S": f"TENANT#{tenant}"}, "sk": {"S": f"EXEC#{invocation}"}},
            "ConditionExpression": ("#s = :active AND tenant_id = :tenant AND workload_binding = :uid "
                                    "AND current_attempt = :attempt AND attribute_not_exists(abort_command_id)"),
            "UpdateExpression": "SET #s = :complete, terminal_outcome = :outcome, terminal_reconciled_by = :source",
            "ExpressionAttributeNames": {"#s": "status"},
            "ExpressionAttributeValues": {
                ":active": {"S": "active"}, ":complete": {"S": "completed"},
                ":tenant": {"S": tenant}, ":uid": raw["workload_binding"], ":attempt": raw["current_attempt"],
                ":outcome": outcome, ":source": {"S": "interrupted_abort_acceptance_recovery"},
            },
        }},
    ])


def recover_retained_abort_pods(*, store, workloads, events_table: str, cursor: str = "") -> tuple[int, str]:
    """Process one bounded page; any unresolved pod keeps its evidence finalizer.

    Run this even when work claims are disabled. Pod annotations only locate a
    candidate: the protected execution must bind the same tenant, run, name and
    UID, and the workload verifier must observe the actual terminated container.
    Neither a deletion timestamp nor a missing pod establishes termination.
    """
    retention = workloads.exit_retention
    candidates, next_cursor = retention.discover(cursor=cursor)
    released = 0
    for hint in candidates:
        try:
            invocation, tenant = hint["invocation_id"], hint["tenant_id"]
            raw = store._read(f"TENANT#{tenant}", f"EXEC#{invocation}") or {}
            if any(raw.get(key) != {"S": value} for key, value in (
                ("tenant_id", tenant), ("invocation_id", invocation),
                ("pod_name", hint["name"]), ("workload_binding", hint["uid"]),
            )):
                continue
            if not workloads.has_exited(name=hint["name"], uid=hint["uid"]):
                continue
            if "abort_command_id" in raw:
                result = repair_aborted_terminal_status(
                    authority_client=store.client, events_table=events_table,
                    execution=raw, invocation_id=invocation, tenant_id=tenant,
                )
                if not result.repaired and result.reason != "already_terminal":
                    continue
                event = store.client.get_item(
                    TableName=events_table,
                    Key={"event_id": {"S": invocation}, "arrived_at": raw["arrived_at"]},
                    ConsistentRead=True,
                ).get("Item", {})
                if event.get("tenant_id") != {"S": tenant} or event.get("status", {}).get("S") not in OBSERVED_TERMINAL_STATUSES:
                    continue
                _retire_execution(store, raw=raw, tenant=tenant, invocation=invocation, event=event, events_table=events_table)
            else:
                _recover_interrupted_acceptance(store, raw=raw, tenant=tenant, invocation=invocation, events_table=events_table)
            grant = raw.get("parent_grant_id", {}).get("S")
            reservation = raw.get("dispatch_reservation_id", {}).get("S")
            continuation = raw.get("orchestration_continuation_receipt")
            if bool(grant) != bool(reservation) and not (continuation and grant and not reservation):
                raise AuthorityStoreError("incomplete dispatch reservation binding")
            if grant and reservation:
                store.authority.release_dispatch(tenant_id=tenant, grant_id=grant, reservation_id=reservation)
            retention.release(**hint)
            released += 1
        except (ClientError, BotoCoreError, AuthorityStoreError, ExitRetentionError, KeyError, TypeError, ValueError):
            logger.exception("retained abort recovery deferred; pod evidence remains")
    return released, next_cursor
