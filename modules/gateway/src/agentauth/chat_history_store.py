"""History reads with separate live-execution and durable-resource authorization."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

from boto3.dynamodb.conditions import Key
from botocore.exceptions import BotoCoreError, ClientError
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from src.agentauth.chat_capability import (
    ChatAuthorizationRefusedError,
    ChatAuthorizationUnavailableError,
    ChatCapabilityService,
    ChatLaunch,
    Operation,
    _decode,
    _encode,
    _launch_json,
)
from src.agentauth.run_credential import CredentialError, _key
from src.orchestration.chat_data_migration import _matches_owner_fields, _owns_context_row

_REFERENCE = re.compile(r"[A-Za-z0-9_.:-]{1,256}\Z")
_ITEM_KEY = re.compile(r"item#[0-9]{8}\Z")
_CURSOR_VERSION = "chat-history-page-v3"


class ChatHistoryExpiredError(Exception):
    """The authorized session is outside its retention window."""


def history_version(header: dict) -> int:
    version = header.get("historyVersion", 0)
    if isinstance(version, bool) or not isinstance(version, int | Decimal) or int(version) != version or not 0 <= version <= 99_999_999:
        raise ChatAuthorizationUnavailableError("chat history version unavailable")
    return int(version)


def timeline_epoch(header: dict) -> int:
    """Bumped only by timeline rewrites (compaction); appends leave it unchanged."""
    epoch = header.get("timelineEpoch", 0)
    if isinstance(epoch, bool) or not isinstance(epoch, int | Decimal) or int(epoch) != epoch or not 0 <= epoch <= 99_999_999:
        raise ChatAuthorizationUnavailableError("chat timeline epoch unavailable")
    return int(epoch)


class _Cursor(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    launch_digest: str
    resource_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    after: str = Field(pattern=r"^item#[0-9]{8}$")
    limit: int = Field(ge=1, le=100)
    issued_at: int
    expires_at: int


class _Message(BaseModel):
    role: Literal["user", "assistant"]
    content: str
    ts: str
    tokens: int = Field(ge=0)
    parts: list[Any] | None = None


class _Summary(BaseModel):
    depth: int = Field(ge=0)
    kind: Literal["leaf", "condensed"]
    content: str
    source_ids: list[str] = Field(alias="sourceIds")
    parent_ids: list[str] | None = Field(default=None, alias="parentIds")
    earliest_at: str = Field(alias="earliestAt")
    latest_at: str = Field(alias="latestAt")
    tokens: int = Field(ge=0)


class _Item(BaseModel):
    ordinal: int = Field(ge=0, le=99_999_999)
    type: Literal["msg", "sum"]
    ref: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,256}$")
    tokens: int | None = Field(default=None, ge=0)


class ChatHistoryStore:
    def __init__(self, table: Any, capabilities: ChatCapabilityService):
        self.table = table
        self.capabilities = capabilities

    def _get(self, session_id: str, sort_key: str) -> dict | None:
        try:
            return self.table.get_item(Key={"PK": f"session#{session_id}", "SK": sort_key}, ConsistentRead=True).get("Item")
        except (BotoCoreError, ClientError):
            raise ChatAuthorizationUnavailableError("chat history unavailable") from None

    def _authorize(self, token: str, run_id: str, session_id: str, operation: Operation, now: int) -> tuple[ChatLaunch, dict]:
        launch = self.capabilities.verify_run(token, run_id=run_id, operation=operation, now=now)
        self._reference(session_id)
        header = self._get(session_id, "header")
        if not header:
            raise ChatAuthorizationRefusedError("chat session unavailable")
        tenant, team, owner = (header.get(field) for field in ("tenantId", "teamId", "ownerUserId"))
        if (
            not isinstance(tenant, str)
            or not tenant
            or not isinstance(owner, str)
            or not owner
            or not isinstance(team, str)
            or header.get("orgId") != tenant
            or not _matches_owner_fields(header, (tenant, team, owner))
        ):
            raise ChatAuthorizationRefusedError("chat session ownership unavailable")
        acl = header.get("aclUserIds", [])
        if not isinstance(acl, list) or any(not isinstance(member, str) or not member for member in acl):
            raise ChatAuthorizationRefusedError("chat session sharing unavailable")
        if session_id != launch.session_id and launch.user_id not in acl:
            raise ChatAuthorizationRefusedError("chat session not explicitly shared")
        self.capabilities.authorize_resource(
            launch, tenant_id=tenant, owner_user_id=owner, team_id=team, acl_user_ids=frozenset(acl), session_id=None, now=now
        )
        ttl = header.get("ttl")
        try:
            if isinstance(ttl, bool) or int(ttl) != ttl:
                raise ValueError
            if int(ttl) <= now:
                raise ChatHistoryExpiredError("chat history expired")
        except (TypeError, ValueError, OverflowError):
            raise ChatAuthorizationUnavailableError("chat retention unavailable") from None
        return launch, header

    def _check_row(self, row: dict, header: dict) -> None:
        if row.get("PK") != header["PK"] or not _owns_context_row(row, (header["tenantId"], header["teamId"], header["ownerUserId"])):
            raise ChatAuthorizationRefusedError("chat record ownership unavailable")

    def _mac(self, body: str) -> str:
        try:
            return _encode(hmac.new(_key(self.capabilities.env), f"{_CURSOR_VERSION}.{body}".encode("ascii"), hashlib.sha256).digest())
        except CredentialError:
            raise ChatAuthorizationUnavailableError("chat cursor signing unavailable") from None

    def _resource_digest(self, header: dict) -> str:
        """Bind a cursor to the resource identity and timeline shape, never to the history version.

        Pages are keyed by ordinal, so a concurrent owner append lands after every
        issued continuation and the next page stays gap- and duplicate-free. A
        compaction rewrites ordinals already handed out and bumps the timeline epoch,
        which does invalidate outstanding cursors. Sharing and membership changes are
        re-checked by ``_authorize`` on every page before the cursor is examined.
        """
        scope = {field: header[field] for field in ("PK", "orgId", "tenantId", "teamId", "ownerUserId")}
        scope["timelineEpoch"] = timeline_epoch(header)
        return hashlib.sha256(json.dumps(scope, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def _cursor(self, launch: ChatLaunch, header: dict, after: str, limit: int, now: int) -> str:
        claims = _Cursor(
            launch_digest=hashlib.sha256(_launch_json(launch).encode()).hexdigest(),
            resource_digest=self._resource_digest(header),
            after=after,
            limit=limit,
            issued_at=now,
            expires_at=min(now + 300, launch.expires_at, int(header["ttl"])),
        )
        body = _encode(claims.model_dump_json().encode())
        return f"{_CURSOR_VERSION}.{body}.{self._mac(body)}"

    def _after(self, cursor: str, launch: ChatLaunch, header: dict, limit: int, now: int) -> str:
        try:
            if not isinstance(cursor, str) or not 1 <= len(cursor) <= 2048:
                raise ValueError
            version, body, signature = cursor.split(".")
            if version != _CURSOR_VERSION or not hmac.compare_digest(self._mac(body), signature):
                raise ValueError
            claims = _Cursor.model_validate_json(_decode(body))
            if (
                claims.launch_digest != hashlib.sha256(_launch_json(launch).encode()).hexdigest()
                or claims.resource_digest != self._resource_digest(header)
                or claims.limit != limit
                or not claims.issued_at <= now < claims.expires_at <= min(claims.issued_at + 300, launch.expires_at)
            ):
                raise ValueError
            return claims.after
        except (ValueError, TypeError, UnicodeError):
            raise ChatAuthorizationRefusedError("chat cursor refused") from None

    def _reference(self, reference: str) -> None:
        if not isinstance(reference, str) or not _REFERENCE.fullmatch(reference):
            raise ChatAuthorizationRefusedError("chat reference refused")

    def _project(self, row: dict, header: dict, model: type[BaseModel]) -> dict:
        self._check_row(row, header)
        try:
            values = dict(row)
            if model is _Message and isinstance(values.get("parts"), str):
                values["parts"] = json.loads(values["parts"])
            result = model.model_validate(values).model_dump(exclude_none=True, by_alias=True)
            if model is _Summary:
                for reference in result["sourceIds"] + result.get("parentIds", []):
                    self._reference(reference)
            return result
        except (ValidationError, ValueError, TypeError):
            raise ChatAuthorizationUnavailableError("chat history record unavailable") from None

    def _result(self, entries: list[dict], *, now: int, cursor: str | None = None, missing: list[str] | None = None) -> dict:
        return {
            "status": "partial" if cursor or missing else "ok" if entries else "empty",
            "entries": entries,
            "next_cursor": cursor,
            "observed_at": datetime.fromtimestamp(now, UTC).isoformat(),
            "coverage": {"source": "session_context", "complete": not bool(cursor or missing), "missing_source_ids": missing or []},
        }

    def read_page(self, token: str, *, run_id: str, session_id: str, now: int, limit: int = 100, cursor: str | None = None) -> dict:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("history page limit must be between 1 and 100")
        launch, header = self._authorize(token, run_id, session_id, "history.read", now)
        query = {
            "KeyConditionExpression": Key("PK").eq(header["PK"]) & Key("SK").begins_with("item#"),
            "ConsistentRead": True,
            "ScanIndexForward": True,
            "Limit": limit,
        }
        if cursor is not None:
            query["ExclusiveStartKey"] = {"PK": header["PK"], "SK": self._after(cursor, launch, header, limit, now)}
        try:
            page = self.table.query(**query)
        except (BotoCoreError, ClientError):
            raise ChatAuthorizationUnavailableError("chat history unavailable") from None
        entries = []
        for row in page.get("Items", []):
            item = self._project(row, header, _Item)
            if row.get("SK") != f"item#{item['ordinal']:08d}":
                raise ChatAuthorizationUnavailableError("chat history ordering unavailable")
            entries.append(item)
        continuation = page.get("LastEvaluatedKey")
        next_cursor = None
        if continuation:
            if continuation.get("PK") != header["PK"] or not _ITEM_KEY.fullmatch(continuation.get("SK", "")):
                raise ChatAuthorizationUnavailableError("chat history page unavailable")
            next_cursor = self._cursor(launch, header, continuation["SK"], limit, now)
        return {**self._result(entries, now=now, cursor=next_cursor), "version": history_version(header)}

    def get_messages(self, token: str, *, run_id: str, session_id: str, ids: list[str], now: int) -> dict:
        if not isinstance(ids, list) or len(ids) > 100:
            raise ValueError("at most 100 message references are allowed")
        _, header = self._authorize(token, run_id, session_id, "history.read", now)
        entries, missing = [], []
        for reference in ids:
            self._reference(reference)
            row = self._get(session_id, f"msg#{reference}")
            if row is None:
                missing.append(reference)
            else:
                entries.append({"ref": reference, "message": self._project(row, header, _Message)})
        return self._result(entries, now=now, missing=missing)

    def get_summary(self, token: str, *, run_id: str, session_id: str, summary_id: str, now: int) -> dict:
        _, header = self._authorize(token, run_id, session_id, "history.expand", now)
        self._reference(summary_id)
        row = self._get(session_id, f"sum#{summary_id}")
        entries = [{"ref": summary_id, "summary": self._project(row, header, _Summary)}] if row is not None else []
        return self._result(entries, now=now, missing=[summary_id] if row is None else [])
