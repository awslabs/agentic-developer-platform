"""Atomic, fenced assistant-history writes behind delegated gateway authority."""

import hashlib
import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from boto3.dynamodb.conditions import Key
from botocore.exceptions import BotoCoreError, ClientError
from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.agentauth.chat_admission import _encoded, retention_seconds
from src.agentauth.chat_authority import ChatRuntimeAuthority
from src.agentauth.chat_capability import ChatAuthorizationRefusedError, ChatAuthorizationUnavailableError, Identifier
from src.agentauth.chat_history_store import ChatHistoryStore, history_version, timeline_epoch
from src.agentauth.chat_user_turn import load_user_turn, verify_user_turn
from src.chat_logging.scrubber import RegexScrubber
from src.orchestration.chat_data_migration import _owner_fields


class ChatHistoryConflictError(Exception):
    """The write's version, idempotency key or execution fence changed."""


class HistoryWrite(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    idempotency_key: Identifier
    expected_version: int = Field(ge=0, le=99_999_999)
    content: str = Field(min_length=1, max_length=65_536)
    tokens: int = Field(ge=0, le=1_000_000)

    @field_validator("content")
    @classmethod
    def bounded_content(cls, value: str) -> str:
        if len(value.encode("utf-8")) > 131_072:
            raise ValueError("history content is too large")
        return value


class AssistantAppend(HistoryWrite):
    user_turn_id: Identifier


class ChatHistoryWriter:
    def __init__(self, authority: ChatRuntimeAuthority, history: ChatHistoryStore):
        self.authority = authority
        self.history = history
        self.table = history.table

    def _owned(self, token: str, run_id: str, session_id: str, now: int):
        launch = self.history.capabilities.verify(token, run_id=run_id, session_id=session_id, operation="history.append", now=now)
        _, header = self.history._authorize(token, run_id, session_id, "history.append", now)
        if (header["tenantId"], header["teamId"], header["ownerUserId"]) != (launch.tenant_id, launch.team_id, launch.user_id):
            raise ChatAuthorizationRefusedError("chat write ownership refused")
        return launch, header

    def _accepted(self, launch):
        store = self.authority.store
        execution = store.authority.load_execution(invocation_id=launch.run_id, tenant_id=launch.tenant_id)
        dispatch = store._read(f"INVOCATION#{launch.run_id}", "DISPATCH") or {}
        if execution is None:
            raise ChatAuthorizationRefusedError("chat execution unavailable")
        reference = load_user_turn(store, execution, dispatch.get("envelope_digest", {}).get("S", ""))
        if reference is None:
            raise ChatAuthorizationRefusedError("chat run has no accepted user turn")
        return reference, verify_user_turn(self.table, launch, reference)

    def accepted_turn(self, token: str, *, run_id: str, session_id: str, now: int) -> dict:
        launch, _ = self._owned(token, run_id, session_id, now)
        _, records = self._accepted(launch)
        return {"message_id": records[1]["ref"], "ordinal": int(records[1]["ordinal"])}

    def _receipt(self, session_id: str, key: str, digest: str, header: dict, *, fields=frozenset({"message_id", "ordinal", "version"})):
        receipt = self.history._get(session_id, key)
        if receipt is None:
            return None
        self.history._check_row(receipt, header)
        if receipt.get("requestDigest") != digest:
            raise ChatHistoryConflictError("chat idempotency key reused")
        result = receipt.get("result")
        if not isinstance(result, dict) or set(result) != fields:
            raise ChatAuthorizationUnavailableError("chat write receipt unavailable")
        return {field: result[field] if field.endswith("_id") else int(result[field]) for field in fields}

    def _next_ordinal(self, header: dict) -> int:
        if "historyNextOrdinal" in header:
            ordinal = header["historyNextOrdinal"]
            if isinstance(ordinal, bool) or not isinstance(ordinal, int | Decimal) or int(ordinal) != ordinal or not 1 <= ordinal <= 99_999_999:
                raise ChatAuthorizationUnavailableError("chat ordering unavailable")
            return int(ordinal)
        try:
            page = self.table.query(
                KeyConditionExpression=Key("PK").eq(header["PK"]) & Key("SK").begins_with("item#"),
                ScanIndexForward=False,
                ConsistentRead=True,
                Limit=1,
            )
        except (BotoCoreError, ClientError):
            raise ChatAuthorizationUnavailableError("chat ordering unavailable") from None
        rows = page.get("Items", [])
        if not rows:
            return 1
        row = rows[0]
        self.history._check_row(row, header)
        ordinal = row.get("ordinal")
        if isinstance(ordinal, bool) or not isinstance(ordinal, int | Decimal) or int(ordinal) != ordinal or not 0 <= ordinal < 99_999_999:
            raise ChatAuthorizationUnavailableError("chat ordering unavailable")
        if row.get("SK") != f"item#{int(ordinal):08d}":
            raise ChatAuthorizationUnavailableError("chat ordering unavailable")
        return int(ordinal) + 1

    def append(self, token: str, *, run_id: str, session_id: str, write: AssistantAppend, now: int) -> dict:
        launch, header = self._owned(token, run_id, session_id, now)
        user_turn, accepted_records = self._accepted(launch)
        if write.user_turn_id != accepted_records[1]["ref"]:
            raise ChatAuthorizationRefusedError("chat accepted turn mismatch")
        digest = hashlib.sha256(json.dumps(write.model_dump(), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        receipt_key = "write#" + hashlib.sha256(write.idempotency_key.encode()).hexdigest()
        receipt = self._receipt(session_id, receipt_key, digest, header)
        if receipt is not None:
            return receipt
        if history_version(header) != write.expected_version or write.expected_version == 99_999_999:
            raise ChatHistoryConflictError("chat history version changed")
        ordinal = self._next_ordinal(header)
        if ordinal <= accepted_records[1]["ordinal"]:
            raise ChatAuthorizationUnavailableError("chat accepted turn ordering changed")
        result = {"message_id": "msg_" + uuid.uuid4().hex, "ordinal": ordinal, "version": write.expected_version + 1}
        stamp = datetime.fromtimestamp(now, UTC).isoformat()
        owner = _owner_fields((launch.tenant_id, launch.team_id, launch.user_id))
        provenance = {**owner, "PK": header["PK"], "runId": launch.run_id, "leaseGeneration": launch.lease_generation}
        records = [
            {
                **provenance,
                "SK": "msg#" + result["message_id"],
                "role": "assistant",
                "content": RegexScrubber().scrub_text(write.content).content,
                "ts": stamp,
                "tokens": write.tokens,
                "userTurnId": write.user_turn_id,
            },
            {**provenance, "SK": f"item#{ordinal:08d}", "type": "msg", "ref": result["message_id"], "ordinal": ordinal, "tokens": write.tokens},
            {**provenance, "SK": receipt_key, "requestDigest": digest, "result": result, "userTurnId": write.user_turn_id},
        ]
        return self._commit(
            token,
            launch=launch,
            header=header,
            write=write,
            records=records,
            result=result,
            receipt_key=receipt_key,
            digest=digest,
            next_ordinal=ordinal + 1,
            now=now,
            user_turn=user_turn,
            item_writes=[self._unchanged(record) for record in accepted_records],
        )

    def _unchanged(self, record):
        fields = [field for field in record if field not in {"PK", "SK"}]
        return {
            "ConditionCheck": {
                "TableName": self.table.name,
                "Key": _encoded({"PK": record["PK"], "SK": record["SK"]}),
                "ConditionExpression": " AND ".join(f"#field{index} = :value{index}" for index in range(len(fields))),
                "ExpressionAttributeNames": {f"#field{index}": field for index, field in enumerate(fields)},
                "ExpressionAttributeValues": _encoded({f":value{index}": record[field] for index, field in enumerate(fields)}),
            }
        }

    def _commit(
        self, token, *, launch, header, write, records, result, receipt_key, digest, next_ordinal, now, item_writes=(), user_turn=None, rewrite=False
    ):
        """Commit history records and their receipt under the same version/authority fence.

        ``rewrite`` marks a timeline rewrite (compaction): it bumps the timeline epoch so
        outstanding page cursors are refused, whereas plain appends leave them valid.
        """
        owner = _owner_fields((launch.tenant_id, launch.team_id, launch.user_id))
        names = {f"#{field}": field for field in owner}
        values = {f":{field}": value for field, value in owner.items()}
        required = {"orgId", "tenantId", "teamId", "ownerUserId"}
        conditions = [
            f"#{field} = :{field}" if field in required else f"(attribute_not_exists(#{field}) OR #{field} = :null OR #{field} = :{field})"
            for field in owner
        ]
        conditions.extend(
            [
                "#status = :active",
                "#ttl = :previous_ttl AND #ttl > :now",
                "#lease.run_id = :run AND #lease.sandbox_uid = :pod AND #lease.generation = :generation AND #lease.expires_at > :now",
                "#version = :previous_version" if "historyVersion" in header else "attribute_not_exists(#version)",
            ]
        )
        names.update({"#status": "status", "#ttl": "ttl", "#lease": "chatLease", "#version": "historyVersion"})
        values.update(
            {
                ":null": None,
                ":active": "active",
                ":previous_ttl": header["ttl"],
                ":ttl": max(int(header["ttl"]), now + retention_seconds()),
                ":now": now,
                ":run": launch.run_id,
                ":pod": launch.sandbox_uid,
                ":generation": launch.lease_generation,
                ":version": result["version"],
                ":next": next_ordinal,
                ":stamp": datetime.fromtimestamp(now, UTC).isoformat(),
            }
        )
        if "historyVersion" in header:
            values[":previous_version"] = write.expected_version
        header_update = "SET #version = :version, #ttl = :ttl, lastActivityAt = :stamp, historyNextOrdinal = :next"
        if rewrite:
            header_update += ", timelineEpoch = :epoch"
            values[":epoch"] = timeline_epoch(header) + 1
        store = self.authority.store
        instant = datetime.fromtimestamp(now, UTC)
        grant = store.live_grant(invocation_id=launch.run_id, tenant_id=launch.tenant_id, attempt=launch.attempt, now=instant)
        if grant.grant_id != launch.grant_id or grant.revocation_epoch != launch.grant_epoch:
            raise ChatAuthorizationRefusedError("chat grant changed")
        transaction = [
            {"Put": {"TableName": self.table.name, "Item": _encoded(record), "ConditionExpression": "attribute_not_exists(PK)"}} for record in records
        ]
        transaction.extend(item_writes)
        transaction.extend(
            [
                {
                    "Update": {
                        "TableName": self.table.name,
                        "Key": _encoded({"PK": header["PK"], "SK": "header"}),
                        "UpdateExpression": header_update,
                        "ConditionExpression": " AND ".join(conditions),
                        "ExpressionAttributeNames": names,
                        "ExpressionAttributeValues": _encoded(values),
                    }
                },
                store._authority_check(grant),
                store._grant_check(grant, instant),
                {
                    "ConditionCheck": {
                        "TableName": store.table,
                        "Key": _encoded({"pk": f"TENANT#{launch.tenant_id}", "sk": f"EXEC#{launch.run_id}"}),
                        "ConditionExpression": (
                            "#status = :active AND workload_binding = :pod AND current_attempt = :attempt "
                            "AND current_credential_epoch = :epoch AND attribute_not_exists(abort_command_id)"
                        )
                        + (" AND chat_user_turn = :user_turn" if user_turn is not None else ""),
                        "ExpressionAttributeNames": {"#status": "status"},
                        "ExpressionAttributeValues": _encoded(
                            {
                                ":active": "active",
                                ":pod": launch.sandbox_uid,
                                ":attempt": launch.attempt,
                                ":epoch": launch.credential_epoch,
                                **({":user_turn": user_turn} if user_turn is not None else {}),
                            }
                        ),
                    }
                },
            ]
        )
        try:
            store.client.transact_write_items(TransactItems=transaction)
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == "TransactionCanceledException" and any(
                reason.get("Code") == "ConditionalCheckFailed" for reason in error.response.get("CancellationReasons", [])
            ):
                _, latest = self._owned(token, launch.run_id, launch.session_id, now)
                if user_turn is not None:
                    self._accepted(launch)
                receipt = self._receipt(launch.session_id, receipt_key, digest, latest, fields=frozenset(result))
                if receipt is not None:
                    return receipt
                raise ChatHistoryConflictError("chat write state changed; reread before retry") from None
            raise ChatAuthorizationUnavailableError("chat write unavailable") from None
        except BotoCoreError:
            raise ChatAuthorizationUnavailableError("chat write unavailable") from None
        return result
