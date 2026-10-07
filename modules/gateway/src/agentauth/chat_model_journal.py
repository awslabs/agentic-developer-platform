"""Durable, lease-fenced journal for delegated chat model operations."""

from __future__ import annotations

import hashlib
import json
import re

from botocore.exceptions import BotoCoreError, ClientError

from src.agentauth.chat_admission import _encoded
from src.agentauth.chat_capability import ChatAuthorizationRefusedError, ChatAuthorizationUnavailableError, ChatLaunch, _launch_json
from src.agentauth.chat_user_turn import load_user_turn, verify_user_turn
from src.agentauth.store import AuthorityStoreError
from src.agentauth.workload import WorkloadUnavailableError
from src.orchestration.chat_data_migration import _owns_context_row

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_OPERATION = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
_MODEL = re.compile(r"[A-Za-z0-9_.:/-]{1,256}\Z")


class ChatModelJournal:
    """Record an operation once; a claim is never permission to dispatch or spend."""

    def __init__(self, authority):
        self.authority = authority

    def _open_turn(self, launch):
        return {
            "ConditionCheck": {
                "TableName": self.authority.store.table,
                "Key": _encoded({"pk": f"TENANT#{launch.tenant_id}", "sk": f"EXEC#{launch.run_id}"}),
                "ConditionExpression": (
                    "#status = :active AND workload_binding = :pod AND current_attempt = :attempt "
                    "AND current_credential_epoch = :epoch AND attribute_not_exists(abort_command_id) "
                    "AND attribute_not_exists(chat_turn_sealed)"
                ),
                "ExpressionAttributeNames": {"#status": "status"},
                "ExpressionAttributeValues": _encoded(
                    {":active": "active", ":pod": launch.sandbox_uid, ":attempt": launch.attempt, ":epoch": launch.credential_epoch}
                ),
            }
        }

    def _read(self, run_id: str, operation_id: str) -> dict | None:
        try:
            item = self.authority.store._read(f"CHAT-MODEL#{run_id}", f"OP#{operation_id}")
        except AuthorityStoreError:
            raise ChatAuthorizationUnavailableError("chat model journal unavailable") from None
        if not item:
            return None
        try:
            record = json.loads(item["document"]["S"])
            if not isinstance(record, dict):
                raise ValueError("chat model journal malformed")
            return record
        except (KeyError, TypeError, ValueError):
            raise ChatAuthorizationUnavailableError("chat model journal unavailable") from None

    def claim(self, launch: ChatLaunch, *, operation_id: str, request_digest: str, model_id: str, now: int) -> dict:
        if (
            not isinstance(operation_id, str)
            or not _OPERATION.fullmatch(operation_id)
            or not isinstance(request_digest, str)
            or not _DIGEST.fullmatch(request_digest)
            or not isinstance(model_id, str)
            or not _MODEL.fullmatch(model_id)
            or isinstance(now, bool)
            or not isinstance(now, int)
            or now >= launch.expires_at
        ):
            raise ChatAuthorizationRefusedError("chat model operation invalid")
        try:
            current = self.authority.current(launch, now)
        except (AuthorityStoreError, WorkloadUnavailableError, BotoCoreError, ClientError):
            raise ChatAuthorizationUnavailableError("chat model lease unavailable") from None
        if not current:
            raise ChatAuthorizationRefusedError("chat model lease unavailable")
        table = self.authority.context_table
        owner = (launch.tenant_id, launch.team_id, launch.user_id)
        receipt_key = {"PK": f"session#{launch.session_id}", "SK": f"turn#{launch.run_id}"}
        try:
            store = self.authority.store
            execution = store.authority.load_execution(invocation_id=launch.run_id, tenant_id=launch.tenant_id)
            dispatch = store._read(f"INVOCATION#{launch.run_id}", "DISPATCH") or {}
            if (
                execution is None
                or execution.current_attempt != launch.attempt
                or execution.repo != f"chat/{launch.session_id}"
                or dispatch.get("tenant_id") != {"S": launch.tenant_id}
            ):
                raise ChatAuthorizationRefusedError("chat model turn unavailable")
            reference = load_user_turn(store, execution, dispatch.get("envelope_digest", {}).get("S", ""))
            if reference is None:
                raise ChatAuthorizationRefusedError("chat model turn unavailable")
            message, receipt = verify_user_turn(table, launch, reference)
            if (
                not receipt
                or not _owns_context_row(receipt, owner)
                or receipt.get("status") != "accepted"
                or receipt.get("runId") != launch.run_id
                or receipt.get("leaseGeneration") != launch.lease_generation
                or receipt.get("ref") != "user_" + hashlib.sha256(launch.run_id.encode()).hexdigest()
                or receipt.get("inputDigest") != reference["input_digest"]
            ):
                raise ChatAuthorizationRefusedError("chat model turn unavailable")
            operation = {
                "run_id": launch.run_id,
                "session_id": launch.session_id,
                "operation_id": operation_id,
                "request_digest": request_digest,
                "model_id": model_id,
                "sandbox_uid": launch.sandbox_uid,
                "lease_generation": launch.lease_generation,
                "status": "pending",
                "handoff": "not_started",
                "reservation_status": "unknown",
                "usage": None,
                "automatic_replay_permitted": False,
            }
            transactions = [
                {
                    "Put": {
                        "TableName": self.authority.store.table,
                        "Item": {
                            "pk": {"S": f"CHAT-MODEL#{launch.run_id}"},
                            "sk": {"S": f"OP#{operation_id}"},
                            "document": {"S": json.dumps(operation, sort_keys=True, separators=(",", ":"))},
                        },
                        "ConditionExpression": "attribute_not_exists(pk)",
                    }
                },
                {
                    "ConditionCheck": {
                        "TableName": self.authority.store.table,
                        "Key": {"pk": {"S": f"CHAT-LAUNCH#{launch.run_id}"}, "sk": {"S": "LAUNCH"}},
                        "ConditionExpression": "#document = :document",
                        "ExpressionAttributeNames": {"#document": "document"},
                        "ExpressionAttributeValues": {":document": {"S": _launch_json(launch)}},
                    }
                },
                {
                    "ConditionCheck": {
                        "TableName": table.name,
                        "Key": _encoded({"PK": receipt_key["PK"], "SK": "header"}),
                        "ConditionExpression": (
                            "#status = :active AND orgId = :tenant AND tenantId = :tenant AND teamId = :team "
                            "AND ownerUserId = :user AND #lease.run_id = :run AND #lease.sandbox_uid = :pod "
                            "AND #lease.generation = :generation AND #lease.expires_at > :now AND #ttl > :now"
                        ),
                        "ExpressionAttributeNames": {"#status": "status", "#lease": "chatLease", "#ttl": "ttl"},
                        "ExpressionAttributeValues": _encoded(
                            {
                                ":active": "active",
                                ":tenant": launch.tenant_id,
                                ":team": launch.team_id,
                                ":user": launch.user_id,
                                ":run": launch.run_id,
                                ":pod": launch.sandbox_uid,
                                ":generation": launch.lease_generation,
                                ":now": now,
                            }
                        ),
                    }
                },
                {
                    "ConditionCheck": {
                        "TableName": table.name,
                        "Key": _encoded(receipt_key),
                        "ConditionExpression": (
                            "#status = :accepted AND orgId = :tenant AND tenantId = :tenant AND teamId = :team "
                            "AND ownerUserId = :user AND runId = :run AND leaseGeneration = :generation "
                            "AND #ref = :ref AND inputDigest = :input_digest"
                        ),
                        "ExpressionAttributeNames": {"#status": "status", "#ref": "ref"},
                        "ExpressionAttributeValues": _encoded(
                            {
                                ":accepted": "accepted",
                                ":tenant": launch.tenant_id,
                                ":team": launch.team_id,
                                ":user": launch.user_id,
                                ":run": launch.run_id,
                                ":generation": launch.lease_generation,
                                ":ref": receipt["ref"],
                                ":input_digest": reference["input_digest"],
                            }
                        ),
                    }
                },
                {
                    "ConditionCheck": {
                        "TableName": table.name,
                        "Key": _encoded({"PK": receipt_key["PK"], "SK": message["SK"]}),
                        "ConditionExpression": (
                            "orgId = :tenant AND tenantId = :tenant AND teamId = :team AND ownerUserId = :user "
                            "AND runId = :run AND leaseGeneration = :generation AND #role = :role "
                            "AND content = :content AND #ts = :ts AND tokens = :tokens AND parts = :parts"
                        ),
                        "ExpressionAttributeNames": {"#role": "role", "#ts": "ts"},
                        "ExpressionAttributeValues": _encoded(
                            {
                                ":tenant": launch.tenant_id,
                                ":team": launch.team_id,
                                ":user": launch.user_id,
                                ":run": launch.run_id,
                                ":generation": launch.lease_generation,
                                ":role": message["role"],
                                ":content": message["content"],
                                ":ts": message["ts"],
                                ":tokens": message["tokens"],
                                ":parts": message["parts"],
                            }
                        ),
                    }
                },
            ]
            transactions.append(self._open_turn(launch))
            self.authority.store.client.transact_write_items(TransactItems=transactions)
            return operation
        except ChatAuthorizationRefusedError:
            raise
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == "TransactionCanceledException":
                previous = self._read(launch.run_id, operation_id)
                binding_fields = ("run_id", "session_id", "operation_id", "request_digest", "model_id", "sandbox_uid", "lease_generation")
                if previous and all(previous.get(field) == operation[field] for field in binding_fields):
                    try:
                        self.authority.store.client.transact_write_items(
                            TransactItems=[
                                {
                                    "ConditionCheck": {
                                        "TableName": self.authority.store.table,
                                        "Key": {"pk": {"S": f"CHAT-MODEL#{launch.run_id}"}, "sk": {"S": f"OP#{operation_id}"}},
                                        "ConditionExpression": "#document = :document",
                                        "ExpressionAttributeNames": {"#document": "document"},
                                        "ExpressionAttributeValues": {
                                            ":document": {"S": json.dumps(previous, sort_keys=True, separators=(",", ":"))}
                                        },
                                    }
                                },
                                *transactions[1:],
                            ]
                        )
                    except ClientError as replay_error:
                        if replay_error.response.get("Error", {}).get("Code") == "TransactionCanceledException":
                            raise ChatAuthorizationRefusedError("chat model operation no longer current") from None
                        raise ChatAuthorizationUnavailableError("chat model journal unavailable") from None
                    return previous
                raise ChatAuthorizationRefusedError("chat model operation changed") from None
            raise ChatAuthorizationUnavailableError("chat model journal unavailable") from None
        except (BotoCoreError, AuthorityStoreError, WorkloadUnavailableError, KeyError, TypeError, ValueError):
            raise ChatAuthorizationUnavailableError("chat model journal unavailable") from None

    def transition(self, launch: ChatLaunch, operation: dict, *, now: int, authorize: bool = False, **updates) -> dict | None:
        if (operation["run_id"], operation["session_id"], operation["sandbox_uid"], operation["lease_generation"]) != (
            launch.run_id,
            launch.session_id,
            launch.sandbox_uid,
            launch.lease_generation,
        ):
            raise ChatAuthorizationRefusedError("chat model operation binding changed")
        updated = {**operation, **updates}
        transactions = [
            {
                "Update": {
                    "TableName": self.authority.store.table,
                    "Key": {"pk": {"S": f"CHAT-MODEL#{launch.run_id}"}, "sk": {"S": f"OP#{operation['operation_id']}"}},
                    "UpdateExpression": "SET #document = :updated",
                    "ConditionExpression": "#document = :previous",
                    "ExpressionAttributeNames": {"#document": "document"},
                    "ExpressionAttributeValues": {
                        ":previous": {"S": json.dumps(operation, sort_keys=True, separators=(",", ":"))},
                        ":updated": {"S": json.dumps(updated, sort_keys=True, separators=(",", ":"))},
                    },
                }
            }
        ]
        try:
            if authorize:
                transactions.append(self._open_turn(launch))
                if now >= launch.expires_at or not self.authority.current(launch, now):
                    raise ChatAuthorizationRefusedError("chat model lease unavailable")
                transactions.extend(
                    [
                        {
                            "ConditionCheck": {
                                "TableName": self.authority.store.table,
                                "Key": {"pk": {"S": f"CHAT-LAUNCH#{launch.run_id}"}, "sk": {"S": "LAUNCH"}},
                                "ConditionExpression": "#document = :document",
                                "ExpressionAttributeNames": {"#document": "document"},
                                "ExpressionAttributeValues": {":document": {"S": _launch_json(launch)}},
                            }
                        },
                        {
                            "ConditionCheck": {
                                "TableName": self.authority.context_table.name,
                                "Key": _encoded({"PK": f"session#{launch.session_id}", "SK": "header"}),
                                "ConditionExpression": (
                                    "#status = :active AND orgId = :tenant AND tenantId = :tenant AND teamId = :team "
                                    "AND ownerUserId = :user AND #lease.run_id = :run AND #lease.sandbox_uid = :pod "
                                    "AND #lease.generation = :generation AND #lease.expires_at > :now AND #ttl > :now"
                                ),
                                "ExpressionAttributeNames": {"#status": "status", "#lease": "chatLease", "#ttl": "ttl"},
                                "ExpressionAttributeValues": _encoded(
                                    {
                                        ":active": "active",
                                        ":tenant": launch.tenant_id,
                                        ":team": launch.team_id,
                                        ":user": launch.user_id,
                                        ":run": launch.run_id,
                                        ":pod": launch.sandbox_uid,
                                        ":generation": launch.lease_generation,
                                        ":now": now,
                                    }
                                ),
                            }
                        },
                        {
                            "ConditionCheck": {
                                "TableName": self.authority.context_table.name,
                                "Key": _encoded({"PK": f"session#{launch.session_id}", "SK": f"turn#{launch.run_id}"}),
                                "ConditionExpression": "#status = :accepted AND runId = :run AND leaseGeneration = :generation",
                                "ExpressionAttributeNames": {"#status": "status"},
                                "ExpressionAttributeValues": _encoded(
                                    {
                                        ":accepted": "accepted",
                                        ":run": launch.run_id,
                                        ":generation": launch.lease_generation,
                                    }
                                ),
                            }
                        },
                    ]
                )
            self.authority.store.client.transact_write_items(TransactItems=transactions)
            return updated
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == "TransactionCanceledException":
                return None
            raise ChatAuthorizationUnavailableError("chat model journal unavailable") from None
        except (BotoCoreError, AuthorityStoreError, WorkloadUnavailableError):
            raise ChatAuthorizationUnavailableError("chat model journal unavailable") from None
