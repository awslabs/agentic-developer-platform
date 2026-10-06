"""Immutable ingress input materialized under the trusted chat launch transaction."""

import hashlib
from decimal import Decimal
from typing import Annotated

from boto3.dynamodb.conditions import Key
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.agentauth.bootstrap import envelope_digest
from src.agentauth.chat_capability import ChatAuthorizationRefusedError, ChatAuthorizationUnavailableError
from src.agentauth.chat_history_store import history_version
from src.chat_logging.scrubber import RegexScrubber
from src.orchestration.chat_data_migration import _owner_fields, _owns_context_row


class UserTurn(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    message: str = Field(max_length=65_536)
    attachments: list[Annotated[str, Field(pattern=r"^art_[A-Za-z0-9_.:-]{1,128}$")]] = Field(default_factory=list, max_length=32)

    @field_validator("message")
    @classmethod
    def bounded_message(cls, value):
        if len(value.encode("utf-8")) > 131_072:
            raise ValueError("user message exceeds bound")
        return value

    @model_validator(mode="after")
    def valid_turn(self):
        if not self.message and not self.attachments:
            raise ValueError("user turn is empty")
        if len(set(self.attachments)) != len(self.attachments):
            raise ValueError("duplicate attachments")
        return self


def user_message_fields(payload: dict) -> dict:
    content = RegexScrubber().scrub_text(payload["input"]["message"]).content
    return {
        "role": "user",
        "content": content,
        "ts": payload["timestamp"],
        "tokens": (len(content.encode("utf-16-le")) // 2 + 3) // 4,
        "parts": [{"type": "file", "artifactId": attachment} for attachment in payload["input"]["attachments"]],
    }


def protected_user_turn(envelope: dict, *, human_id: str, expires_at: int) -> tuple[dict, dict]:
    turn = UserTurn.model_validate({"message": envelope["message"], "attachments": envelope.get("attachments", [])})
    payload = {"input": turn.model_dump(), "envelope_digest": envelope_digest(envelope), "timestamp": envelope["arrived_at"]}
    reference = {
        "input_digest": envelope_digest(payload),
        "message_digest": envelope_digest(user_message_fields(payload)),
        "expires_at": expires_at,
    }
    staged = {
        "PK": f"chat-input#{envelope['message_id']}",
        "SK": "input",
        "tenantId": envelope["tenant_id"],
        "ownerUserId": human_id,
        "ttl": expires_at,
        "payload": payload,
    }
    return TypeSerializer().serialize(reference), {field: TypeSerializer().serialize(value) for field, value in staged.items()}


def load_user_turn(store, execution, digest: str) -> dict | None:
    metadata = store._read(f"TENANT#{execution.tenant_id}", f"EXEC#{execution.invocation_id}") or {}
    protected = metadata.get("chat_user_turn")
    dispatch = store._read(f"INVOCATION#{execution.invocation_id}", "DISPATCH") or {}
    expected_metadata = {"chat_user_turn": protected} if protected is not None else {}
    if dispatch.get("execution_metadata_digest") != {"S": envelope_digest(expected_metadata)}:
        raise ChatAuthorizationUnavailableError("trusted user turn integrity unavailable")
    if protected is None:
        return None
    try:
        reference = TypeDeserializer().deserialize(protected)
        if set(reference) != {"input_digest", "message_digest", "expires_at"} or metadata.get("envelope_digest") != {"S": digest}:
            raise ValueError
        return reference
    except (ValueError, TypeError, KeyError):
        raise ChatAuthorizationUnavailableError("trusted user turn unavailable") from None


def consume_user_turn(table, launch, reference: dict, digest: str, now: int) -> tuple[dict, dict]:
    key = {"PK": f"chat-input#{launch.run_id}", "SK": "input"}
    staged = table.get_item(Key=key, ConsistentRead=True).get("Item") or {}
    try:
        payload = staged["payload"]
        if (
            staged["ttl"] != reference["expires_at"]
            or staged["ttl"] <= now
            or staged["tenantId"] != launch.tenant_id
            or staged["ownerUserId"] != launch.user_id
            or payload["envelope_digest"] != digest
            or envelope_digest(payload) != reference["input_digest"]
            or envelope_digest(user_message_fields(payload)) != reference["message_digest"]
        ):
            raise ValueError
        UserTurn.model_validate(payload["input"])
    except (ValueError, TypeError, KeyError):
        raise ChatAuthorizationUnavailableError("trusted user input expired or unavailable") from None
    encode = TypeSerializer().serialize
    return payload, {
        "Delete": {
            "TableName": table.name,
            "Key": {field: encode(value) for field, value in key.items()},
            "ConditionExpression": "payload = :payload AND #ttl = :ttl AND #ttl > :now AND tenantId = :tenant AND ownerUserId = :user",
            "ExpressionAttributeNames": {"#ttl": "ttl"},
            "ExpressionAttributeValues": {
                ":payload": encode(payload),
                ":ttl": encode(staged["ttl"]),
                ":now": encode(now),
                ":tenant": encode(launch.tenant_id),
                ":user": encode(launch.user_id),
            },
        }
    }


def user_turn_records(table, header: dict | None, launch, payload: dict) -> tuple[list[dict], int, int]:
    owner = (launch.tenant_id, launch.team_id, launch.user_id)
    if header is None:
        ordinal, version = 1, 0
    else:
        version = history_version(header)
        ordinal = header.get("historyNextOrdinal")
        if ordinal is None:
            page = table.query(
                KeyConditionExpression=Key("PK").eq(header["PK"]) & Key("SK").begins_with("item#"),
                ConsistentRead=True,
                ScanIndexForward=False,
                Limit=1,
            )
            rows = page.get("Items", [])
            previous = rows[0] if rows else None
            if previous is not None:
                previous_ordinal = previous.get("ordinal")
                if (
                    not _owns_context_row(previous, owner)
                    or isinstance(previous_ordinal, bool)
                    or not isinstance(previous_ordinal, int | Decimal)
                    or int(previous_ordinal) != previous_ordinal
                    or not 0 <= previous_ordinal < 99_999_999
                    or previous.get("SK") != f"item#{int(previous_ordinal):08d}"
                ):
                    raise ChatAuthorizationRefusedError("user turn ordering ownership unavailable")
            ordinal = previous["ordinal"] + 1 if previous else 1
    if isinstance(ordinal, bool) or int(ordinal) != ordinal or not 1 <= ordinal < 99_999_999 or version >= 99_999_999:
        raise ChatAuthorizationUnavailableError("user turn ordering unavailable")
    ordinal = int(ordinal)
    reference = "user_" + hashlib.sha256(launch.run_id.encode()).hexdigest()
    provenance = {
        **_owner_fields(owner),
        "PK": f"session#{launch.session_id}",
        "runId": launch.run_id,
        "leaseGeneration": launch.lease_generation,
    }
    message = {
        **provenance,
        "SK": f"msg#{reference}",
        **user_message_fields(payload),
    }
    receipt = {
        **provenance,
        "SK": f"turn#{launch.run_id}",
        "ref": reference,
        "ordinal": ordinal,
        "inputDigest": envelope_digest(payload),
    }
    item = {**provenance, "SK": f"item#{ordinal:08d}", "type": "msg", "ref": reference, "ordinal": ordinal, "tokens": message["tokens"]}
    return [message, item, receipt], version + 1, ordinal + 1


def verify_user_turn(table, launch, reference: dict) -> list[dict]:
    message_ref = "user_" + hashlib.sha256(launch.run_id.encode()).hexdigest()
    records = []
    for sort_key in (f"msg#{message_ref}", f"turn#{launch.run_id}"):
        row = table.get_item(Key={"PK": f"session#{launch.session_id}", "SK": sort_key}, ConsistentRead=True).get("Item")
        if row is None or not _owns_context_row(row, (launch.tenant_id, launch.team_id, launch.user_id)):
            raise ChatAuthorizationUnavailableError("accepted user turn unavailable")
        if row.get("runId") != launch.run_id or row.get("leaseGeneration") != launch.lease_generation:
            raise ChatAuthorizationUnavailableError("accepted user turn changed")
        if sort_key.startswith("msg#"):
            fields = {field: row.get(field) for field in ("role", "content", "ts", "tokens", "parts")}
            try:
                tokens = fields["tokens"]
                fields["tokens"] = int(fields["tokens"])
                valid = not isinstance(tokens, bool) and tokens == fields["tokens"] and envelope_digest(fields) == reference["message_digest"]
            except (TypeError, ValueError):
                valid = False
        else:
            valid = row.get("ref") == message_ref and row.get("inputDigest") == reference["input_digest"]
        if not valid:
            raise ChatAuthorizationUnavailableError("accepted user turn changed")
        records.append(row)
    ordinal = records[1].get("ordinal")
    if isinstance(ordinal, bool) or not isinstance(ordinal, int | Decimal) or int(ordinal) != ordinal or not 1 <= ordinal < 99_999_999:
        raise ChatAuthorizationUnavailableError("accepted user turn ordering unavailable")
    return records
