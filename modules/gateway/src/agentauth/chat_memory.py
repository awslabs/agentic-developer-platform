"""Owned memory records with current ACL checks and fenced, idempotent writes."""

import hashlib
import hmac
import json
import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.agentauth.chat_admission import _encoded
from src.agentauth.chat_capability import ChatAuthorizationRefusedError, ChatAuthorizationUnavailableError, Identifier, _decode, _encode, _launch_json
from src.agentauth.chat_history_write import AssistantAppend
from src.agentauth.chat_storage import authority_checks, snapshot_condition
from src.agentauth.run_credential import CredentialError, _key
from src.chat_logging.scrubber import RegexScrubber
from src.orchestration.chat_data_migration import _owner_fields, _owns_context_row

Kind = Literal["preference", "fact", "learning", "draft-learning"]
KIND_TTL = {"preference": None, "fact": 180 * 86400, "learning": 90 * 86400, "draft-learning": 14 * 86400}
MemoryId = Annotated[str, Field(pattern=r"^mem_[a-f0-9]{32}$")]


class ChatMemoryConflictError(Exception):
    """The memory version, receipt or authority fence changed."""


class MemoryLabels(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    component: str | None = Field(default=None, min_length=1, max_length=128)
    persona: str | None = Field(default=None, min_length=1, max_length=128)


class MemoryWrite(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    idempotency_key: Identifier
    expected_version: int = Field(ge=0, le=99_999_998)
    memory_id: MemoryId | None = None
    content: str = Field(min_length=1, max_length=65_536)
    kind: Kind = "fact"
    tags: list[Annotated[str, Field(min_length=1, max_length=128)]] = Field(default_factory=list, max_length=32)
    purpose: str = Field(default="chat-memory", min_length=1, max_length=256)
    labels: MemoryLabels = Field(default_factory=MemoryLabels)

    @field_validator("content")
    @classmethod
    def bounded_content(cls, value):
        return AssistantAppend.bounded_content(value)

    @model_validator(mode="after")
    def creation_version(self):
        if (self.memory_id is None) != (self.expected_version == 0):
            raise ValueError("new memories require version zero; updates require an ID and positive version")
        return self


class MemorySearch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    query: str = Field(default="", max_length=1024)
    kinds: list[Kind] = Field(default_factory=list, max_length=4)
    limit: int = Field(default=20, ge=1, le=100)
    cursor: str | None = Field(default=None, min_length=1, max_length=2048)
    labels: MemoryLabels = Field(default_factory=MemoryLabels)


class _Record(BaseModel):
    id: MemoryId
    content: str
    kind: Kind
    tags: list[str]
    scope: dict[str, str]
    source: dict[str, str]
    created_at: str = Field(alias="createdAt")
    updated_at: str = Field(alias="updatedAt")
    version: int = Field(ge=1, le=99_999_999)
    labels: MemoryLabels = Field(default_factory=MemoryLabels)


class ChatMemoryStore:
    def __init__(self, table, authority, capabilities):
        self.table, self.authority, self.capabilities = table, authority, capabilities

    def _get(self, partition: str, key: str) -> dict | None:
        return self.table.get_item(Key={"PK": partition, "SK": key}, ConsistentRead=True).get("Item")

    def _owner_partition(self, launch) -> str:
        return f"memory-owner#{launch.tenant_id}#{launch.user_id}"

    def _authorize(self, row: dict, launch, now: int) -> None:
        owner = tuple(row.get(field) for field in ("tenantId", "teamId", "ownerUserId"))
        if not all(isinstance(value, str) for value in owner) or not _owns_context_row(row, owner):
            raise ChatAuthorizationRefusedError("memory ownership unavailable")
        acl = row.get("aclUserIds", [])
        if not isinstance(acl, list) or any(not isinstance(member, str) or not member for member in acl):
            raise ChatAuthorizationRefusedError("memory sharing unavailable")
        self.capabilities.authorize_resource(
            launch, tenant_id=owner[0], team_id=owner[1], owner_user_id=owner[2], acl_user_ids=frozenset(acl), session_id=None, now=now
        )

    def _record(self, row: dict) -> dict:
        record = _Record.model_validate(row).model_dump(by_alias=True, exclude_none=True)
        if (
            row.get("PK") != f"memory#{record['id']}"
            or row.get("SK") != "record"
            or record["scope"] != {"tenant": row["tenantId"], "user": row["ownerUserId"]}
        ):
            raise ChatAuthorizationRefusedError("memory provenance unavailable")
        return record

    def _expired(self, row: dict, now: int) -> bool:
        if row["kind"] == "preference" and "ttl" not in row:
            return False
        ttl = row.get("ttl")
        if isinstance(ttl, bool) or ttl is None or int(ttl) != ttl:
            raise ChatAuthorizationUnavailableError("memory retention unavailable")
        return ttl <= now

    def _load(self, memory_id: str, launch, now: int) -> dict | None:
        row = self._get(f"memory#{memory_id}", "record")
        if row is None:
            return None
        self._authorize(row, launch, now)
        self._record(row)
        return None if self._expired(row, now) else row

    def _index_write(self, row: dict) -> dict:
        owner = (row["tenantId"], row["teamId"], row["ownerUserId"])
        key = {"PK": f"memory-owner#{owner[0]}#{owner[2]}", "SK": f"mem#{row['createdAt']}#{row['id']}"}
        previous = self._get(key["PK"], key["SK"])
        if previous is not None and (not _owns_context_row(previous, owner) or previous.get("id") != row["id"]):
            raise ChatAuthorizationRefusedError("memory index ownership unavailable")
        pointer = {**_owner_fields(owner), **key, "id": row["id"], "kind": row["kind"]}
        if "ttl" in row:
            pointer["ttl"] = row["ttl"]
        return {
            "Put": {
                "TableName": self.table.name,
                "Item": _encoded(pointer),
                **(snapshot_condition(previous) if previous is not None else {"ConditionExpression": "attribute_not_exists(PK)"}),
            }
        }

    def _index_expired(self, pointer: dict, now: int) -> bool:
        if "kind" not in pointer and "ttl" not in pointer:
            return False
        if pointer.get("kind") not in KIND_TTL:
            raise ChatAuthorizationUnavailableError("memory index retention unavailable")
        return self._expired(pointer, now)

    def _refresh(self, row: dict, launch, now: int) -> None:
        if row["kind"] not in {"fact", "learning"} or row["ttl"] >= now + KIND_TTL[row["kind"]]:
            return
        refreshed = {**row, "ttl": now + KIND_TTL[row["kind"]]}
        try:
            self.authority.store.client.transact_write_items(
                TransactItems=[
                    {"Put": {"TableName": self.table.name, "Item": _encoded(refreshed), **snapshot_condition(row)}},
                    self._index_write(refreshed),
                ]
                + authority_checks(self.authority, launch, now)
            )
        except ClientError:
            raise ChatAuthorizationUnavailableError("memory retention refresh unavailable") from None

    def _result(self, entries: list, now: int, *, cursor=None, missing=None):
        return {
            "status": "partial" if cursor or missing else "ok" if entries else "empty",
            "entries": entries,
            "next_cursor": cursor,
            "observed_at": datetime.fromtimestamp(now, UTC).isoformat(),
            "coverage": {"source": "owned_memory", "complete": not bool(cursor or missing), "missing_source_ids": missing or []},
        }

    def read(self, token: str, *, run_id: str, memory_id: str, now: int) -> dict:
        launch = self.capabilities.verify_run(token, run_id=run_id, operation="memory.search", now=now)
        row = self._load(memory_id, launch, now)
        if row is None:
            return self._result([], now, missing=[memory_id])
        self._refresh(row, launch, now)
        return self._result([self._record(row)], now)

    def _mac(self, body: str) -> str:
        try:
            return _encode(hmac.new(_key(self.capabilities.env), f"chat-memory-page-v1.{body}".encode(), hashlib.sha256).digest())
        except CredentialError:
            raise ChatAuthorizationUnavailableError("memory cursor signing unavailable") from None

    def search(self, token: str, *, run_id: str, search: MemorySearch, now: int) -> dict:
        launch = self.capabilities.verify_run(token, run_id=run_id, operation="memory.search", now=now)
        partition = self._owner_partition(launch)
        binding = hashlib.sha256((_launch_json(launch) + search.model_dump_json(exclude={"cursor"})).encode()).hexdigest()
        query = {
            "KeyConditionExpression": Key("PK").eq(partition) & Key("SK").begins_with("mem#"),
            "ConsistentRead": True,
            "ScanIndexForward": False,
            "Limit": search.limit,
        }
        if search.cursor:
            try:
                body, signature = search.cursor.split(".")
                if not hmac.compare_digest(self._mac(body), signature):
                    raise ValueError
                claims = json.loads(_decode(body))
                if claims["binding"] != binding or not claims["issued_at"] <= now < claims["expires_at"] <= min(
                    launch.expires_at, claims["issued_at"] + 300
                ):
                    raise ValueError
                if not isinstance(claims["after"], str) or not claims["after"].startswith("mem#"):
                    raise ValueError
                query["ExclusiveStartKey"] = {"PK": partition, "SK": claims["after"]}
            except (ValueError, TypeError, KeyError, UnicodeError):
                raise ChatAuthorizationRefusedError("memory cursor refused") from None
        page = self.table.query(**query)
        entries, missing = [], []
        for pointer in page.get("Items", []):
            if pointer.get("PK") != partition or pointer.get("ownerUserId") != launch.user_id:
                raise ChatAuthorizationRefusedError("memory index ownership unavailable")
            self._authorize(pointer, launch, now)
            row = self._get(f"memory#{pointer['id']}", "record")
            if row is None:
                if not self._index_expired(pointer, now):
                    missing.append(pointer["id"])
                continue
            self._authorize(row, launch, now)
            record = self._record(row)
            if self._expired(row, now):
                continue
            if search.query.lower() not in row["content"].lower() or (search.kinds and row["kind"] not in search.kinds):
                continue
            user_wide_preference = record["kind"] == "preference" and not record["labels"]
            if not user_wide_preference and any(
                record["labels"].get(label) != value for label, value in search.labels.model_dump(exclude_none=True).items()
            ):
                continue
            self._refresh(row, launch, now)
            entries.append(record)
        cursor = None
        if continuation := page.get("LastEvaluatedKey"):
            if continuation.get("PK") != partition or not continuation.get("SK", "").startswith("mem#"):
                raise ChatAuthorizationUnavailableError("memory page unavailable")
            body = _encode(
                json.dumps(
                    {"binding": binding, "after": continuation["SK"], "issued_at": now, "expires_at": min(now + 300, launch.expires_at)}
                ).encode()
            )
            cursor = f"{body}.{self._mac(body)}"
        return self._result(entries, now, cursor=cursor, missing=missing)

    def _receipt(self, key: dict, digest: str, launch, now: int) -> dict | None:
        row = self._get(key["PK"], key["SK"])
        if row is None:
            return None
        self._authorize(row, launch, now)
        if row.get("requestDigest") != digest:
            raise ChatMemoryConflictError("memory idempotency key reused")
        result = row["result"]
        if self._load(result["memory_id"], launch, now) is None:
            raise ChatMemoryConflictError("memory receipt source expired or missing")
        return {"memory_id": result["memory_id"], "version": int(result["version"])}

    def write(self, token: str, *, run_id: str, write: MemoryWrite, now: int) -> dict:
        launch = self.capabilities.verify_run(token, run_id=run_id, operation="memory.write", now=now)
        owner = _owner_fields((launch.tenant_id, launch.team_id, launch.user_id))
        partition = self._owner_partition(launch)
        digest = hashlib.sha256(write.model_dump_json().encode()).hexdigest()
        key = {"PK": partition, "SK": "write#" + hashlib.sha256(f"{run_id}#{write.idempotency_key}".encode()).hexdigest()}
        if receipt := self._receipt(key, digest, launch, now):
            return receipt
        previous = self._load(write.memory_id, launch, now) if write.memory_id else None
        if write.memory_id and (previous is None or previous["ownerUserId"] != launch.user_id):
            raise ChatAuthorizationRefusedError("memory write ownership refused")
        if previous and previous["version"] != write.expected_version:
            raise ChatMemoryConflictError("memory version changed")
        if previous:
            owner = _owner_fields((previous["tenantId"], previous["teamId"], previous["ownerUserId"]))
        memory_id = write.memory_id or "mem_" + uuid.uuid4().hex
        stamp = datetime.fromtimestamp(now, UTC).isoformat()
        scrubber = RegexScrubber()
        record = {
            **owner,
            "PK": f"memory#{memory_id}",
            "SK": "record",
            "id": memory_id,
            "scope": {"tenant": launch.tenant_id, "user": launch.user_id},
            "source": {"sessionId": launch.session_id, "runId": launch.run_id},
            "content": scrubber.scrub_text(write.content).content,
            "kind": write.kind,
            "tags": [scrubber.scrub_text(tag).content for tag in write.tags],
            "purpose": scrubber.scrub_text(write.purpose).content,
            "createdAt": previous["createdAt"] if previous else stamp,
            "updatedAt": stamp,
            "version": write.expected_version + 1,
            "labels": write.labels.model_dump(exclude_none=True),
            "aclUserIds": previous.get("aclUserIds", []) if previous else [],
        }
        if ttl := KIND_TTL[write.kind]:
            record["ttl"] = now + ttl
        result = {"memory_id": memory_id, "version": record["version"]}
        receipt_row = {**owner, **key, "requestDigest": digest, "result": result}
        transaction = [
            {
                "Put": {
                    "TableName": self.table.name,
                    "Item": _encoded(record),
                    **(snapshot_condition(previous) if previous else {"ConditionExpression": "attribute_not_exists(PK)"}),
                }
            },
            {"Put": {"TableName": self.table.name, "Item": _encoded(receipt_row), "ConditionExpression": "attribute_not_exists(PK)"}},
            self._index_write(record),
        ]
        try:
            self.authority.store.client.transact_write_items(TransactItems=transaction + authority_checks(self.authority, launch, now))
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == "TransactionCanceledException" and any(
                reason.get("Code") == "ConditionalCheckFailed" for reason in error.response.get("CancellationReasons", [])
            ):
                self.capabilities.verify_run(token, run_id=run_id, operation="memory.write", now=now)
                if receipt := self._receipt(key, digest, launch, now):
                    return receipt
                raise ChatMemoryConflictError("memory write state changed") from None
            raise ChatAuthorizationUnavailableError("memory write unavailable") from None
        return result
