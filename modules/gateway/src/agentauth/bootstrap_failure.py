"""Durable failure before a review worker receives its first run credential.

This is a gateway refusal, not a successful worker completion. Keeping the
failed invocation lets the engine reserve a new stage attempt without resetting
history or admitting two workers. Successful credential delivery fences refusal.
"""

from datetime import UTC, datetime

from botocore.exceptions import BotoCoreError, ClientError

from .store import AuthorityStoreError

REASON = "model_policy_snapshot_missing"


def is_bootstrap_failure(raw):
    return bool(
        raw
        and raw.get("status") == {"S": "completed"}
        and raw.get("terminal_outcome") == {"S": "failed"}
        and raw.get("bootstrap_failure_reason") == {"S": REASON}
        and raw.get("orchestration_continuation_receipt")
        and raw.get("bootstrap_failure_request_id", {}).get("S")
        and "bootstrap_authority_issued_at" not in raw
    )


def record_refusal(store, *, record, request_id, events_table, observed_at=None):
    """Called for an actual initial snapshot refusal, never a worker report.

    A historical refusal can be reconciled by an operator from the retained
    gateway request log using that exact request ID, timestamp and pod binding.
    Registration, snapshot attachment, or prior credential delivery refuse the
    reconciliation. No grant, attempt, claim or pod binding is reset.
    """
    if not request_id or not events_table or not record.workload_binding:
        raise AuthorityStoreError("bootstrap refusal evidence unavailable")
    at = observed_at or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    pk = {"S": f"TENANT#{record.tenant_id}"}
    error = "Gateway refused reviewer startup: model-policy snapshot missing; model execution did not start."
    try:
        store.client.transact_write_items(
            TransactItems=[
                {
                    "Update": {
                        "TableName": store.table,
                        "Key": {"pk": pk, "sk": {"S": f"EXEC#{record.invocation_id}"}},
                        "UpdateExpression": (
                            "SET #st = :done, terminal_outcome = :failed, bootstrap_failure_reason = :reason, "
                            "bootstrap_failure_request_id = :request, bootstrap_failed_at = :at"
                        ),
                        "ConditionExpression": (
                            "#st = :active AND current_attempt = :attempt AND current_credential_epoch = :epoch "
                            "AND workload_binding = :pod AND attribute_exists(orchestration_continuation_receipt) "
                            "AND attribute_not_exists(model_policy_snapshot) AND attribute_not_exists(bootstrap_authority_issued_at)"
                        ),
                        "ExpressionAttributeNames": {"#st": "status"},
                        "ExpressionAttributeValues": {
                            ":done": {"S": "completed"},
                            ":failed": {"S": "failed"},
                            ":active": {"S": "active"},
                            ":attempt": {"N": str(record.current_attempt)},
                            ":epoch": {"N": str(record.current_credential_epoch)},
                            ":pod": {"S": record.workload_binding},
                            ":reason": {"S": REASON},
                            ":request": {"S": request_id},
                            ":at": {"S": at},
                        },
                    }
                },
                {
                    "ConditionCheck": {
                        "TableName": store.table,
                        "Key": {"pk": pk, "sk": {"S": f"REG#{record.invocation_id}#{record.current_attempt}"}},
                        "ConditionExpression": "attribute_not_exists(pk)",
                    }
                },
                {
                    "Update": {
                        "TableName": events_table,
                        "Key": {"event_id": {"S": record.invocation_id}, "arrived_at": {"S": record.arrived_at}},
                        "UpdateExpression": "SET #st = :failed, error_message = :error, status_updated_at = :at",
                        "ConditionExpression": "tenant_id = :tenant AND attribute_not_exists(control_registered_at)",
                        "ExpressionAttributeNames": {"#st": "status"},
                        "ExpressionAttributeValues": {
                            ":failed": {"S": "failed"},
                            ":error": {"S": error},
                            ":at": {"S": at},
                            ":tenant": {"S": record.tenant_id},
                        },
                    }
                },
            ]
        )
    except (ClientError, BotoCoreError):
        raw = store._read(f"TENANT#{record.tenant_id}", f"EXEC#{record.invocation_id}")
        if not is_bootstrap_failure(raw) or raw.get("bootstrap_failure_request_id") != {"S": request_id}:
            raise AuthorityStoreError("bootstrap refusal could not be recorded") from None


def record_issuance(store, *, record):
    raw = store._read(f"TENANT#{record.tenant_id}", f"EXEC#{record.invocation_id}") or {}
    if not raw.get("orchestration_continuation_receipt"):
        return
    try:
        store.client.update_item(
            TableName=store.table,
            Key={"pk": {"S": f"TENANT#{record.tenant_id}"}, "sk": {"S": f"EXEC#{record.invocation_id}"}},
            UpdateExpression="SET bootstrap_authority_issued_at = if_not_exists(bootstrap_authority_issued_at, :at)",
            ConditionExpression=(
                "#st = :active AND current_attempt = :attempt AND workload_binding = :pod AND attribute_not_exists(bootstrap_failure_reason)"
            ),
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={
                ":active": {"S": "active"},
                ":attempt": {"N": str(record.current_attempt)},
                ":pod": {"S": record.workload_binding},
                ":at": {"S": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")},
            },
        )
    except (ClientError, BotoCoreError):
        raise AuthorityStoreError("bootstrap issuance could not be recorded") from None
