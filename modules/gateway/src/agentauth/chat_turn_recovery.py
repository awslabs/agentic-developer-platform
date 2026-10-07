"""Trusted reconciliation of a lost chat worker against its accepted turn."""

from decimal import Decimal

from botocore.exceptions import BotoCoreError, ClientError

from src.agentauth.chat_admission import _encoded
from src.agentauth.chat_authority import ChatSessionLease
from src.agentauth.chat_capability import ChatAuthorizationRefusedError, ChatAuthorizationUnavailableError, ChatLaunchStore
from src.agentauth.chat_history_store import history_version
from src.orchestration.chat_data_migration import _owns_context_row


def reconcile_lost_turn(authority, *, run_id: str, now: int, worker_terminated: bool = False) -> dict:
    """Fence a trusted lost worker; retryable never permits automatic model replay."""
    launch = ChatLaunchStore(authority.store).load(run_id)
    table = authority.context_table
    header_key = {"PK": f"session#{launch.session_id}", "SK": "header"}
    receipt_key = {"PK": header_key["PK"], "SK": f"turn#{run_id}"}
    owner = (launch.tenant_id, launch.team_id, launch.user_id)
    try:
        header = table.get_item(Key=header_key, ConsistentRead=True).get("Item")
        receipt = table.get_item(Key=receipt_key, ConsistentRead=True).get("Item")
        if (
            not header
            or not receipt
            or not _owns_context_row(header, owner)
            or not _owns_context_row(receipt, owner)
            or header.get("status") != "active"
            or receipt.get("runId") != launch.run_id
            or receipt.get("leaseGeneration") != launch.lease_generation
            or receipt.get("status") not in {"accepted", "interrupted"}
        ):
            raise ChatAuthorizationRefusedError("chat turn cannot be reconciled")
        lease = ChatSessionLease.model_validate(header.get("chatLease"))
        if (lease.run_id, lease.sandbox_uid, lease.generation) != (launch.run_id, launch.sandbox_uid, launch.lease_generation):
            raise ChatAuthorizationRefusedError("chat lease changed")
        if receipt["status"] == "interrupted":
            return {"status": "interrupted", "retryable": receipt.get("retryable") is True}
        if lease.expires_at > now and not worker_terminated:
            return {"status": "running", "retryable": False}
        accepted_version = receipt.get("acceptedHistoryVersion")
        accepted_next = receipt.get("acceptedNextOrdinal")
        if (
            any(isinstance(value, bool) or not isinstance(value, int | Decimal) or int(value) != value for value in (accepted_version, accepted_next))
            or accepted_version < 1
            or accepted_next < 2
        ):
            raise ChatAuthorizationUnavailableError("chat accepted turn version unavailable")
        current_next = header.get("historyNextOrdinal")
        if (
            isinstance(current_next, bool)
            or not isinstance(current_next, int | Decimal)
            or int(current_next) != current_next
            or current_next < accepted_next
        ):
            raise ChatAuthorizationUnavailableError("chat history ordering unavailable")
        current_version = history_version(header)
        retryable = current_version == accepted_version and current_next == accepted_next
        authority.store.client.transact_write_items(
            TransactItems=[
                {
                    "Update": {
                        "TableName": table.name,
                        "Key": _encoded(header_key),
                        "UpdateExpression": "SET #lease.expires_at = :fenced",
                        "ConditionExpression": (
                            "#status = :active AND orgId = :tenant AND tenantId = :tenant AND teamId = :team AND ownerUserId = :user "
                            "AND #lease.run_id = :run AND #lease.sandbox_uid = :pod AND #lease.generation = :generation "
                            "AND #lease.expires_at = :previous_expiry AND historyVersion = :version AND historyNextOrdinal = :next"
                        ),
                        "ExpressionAttributeNames": {"#status": "status", "#lease": "chatLease"},
                        "ExpressionAttributeValues": _encoded(
                            {
                                ":fenced": 1,
                                ":active": "active",
                                ":tenant": launch.tenant_id,
                                ":team": launch.team_id,
                                ":user": launch.user_id,
                                ":run": launch.run_id,
                                ":pod": launch.sandbox_uid,
                                ":generation": launch.lease_generation,
                                ":previous_expiry": lease.expires_at,
                                ":version": current_version,
                                ":next": current_next,
                            }
                        ),
                    }
                },
                {
                    "Update": {
                        "TableName": table.name,
                        "Key": _encoded(receipt_key),
                        "UpdateExpression": "SET #status = :interrupted, retryable = :retryable, automaticReplayPermitted = :no_replay",
                        "ConditionExpression": (
                            "#status = :accepted AND tenantId = :tenant AND teamId = :team AND ownerUserId = :user "
                            "AND runId = :run AND leaseGeneration = :generation AND acceptedHistoryVersion = :accepted_version "
                            "AND acceptedNextOrdinal = :accepted_next"
                        ),
                        "ExpressionAttributeNames": {"#status": "status"},
                        "ExpressionAttributeValues": _encoded(
                            {
                                ":interrupted": "interrupted",
                                ":retryable": retryable,
                                ":no_replay": False,
                                ":accepted": "accepted",
                                ":tenant": launch.tenant_id,
                                ":team": launch.team_id,
                                ":user": launch.user_id,
                                ":run": launch.run_id,
                                ":generation": launch.lease_generation,
                                ":accepted_version": accepted_version,
                                ":accepted_next": accepted_next,
                            }
                        ),
                    }
                },
            ]
        )
        return {"status": "interrupted", "retryable": retryable}
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") == "TransactionCanceledException":
            raise ChatAuthorizationRefusedError("chat turn reconciliation changed") from None
        raise ChatAuthorizationUnavailableError("chat turn reconciliation unavailable") from None
    except BotoCoreError:
        raise ChatAuthorizationUnavailableError("chat turn reconciliation unavailable") from None
