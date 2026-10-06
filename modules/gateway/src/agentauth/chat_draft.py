"""Versioned whole-draft replacement behind current session authorization."""

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated

from botocore.exceptions import ClientError
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.agentauth.chat_admission import _encoded, retention_seconds
from src.agentauth.chat_capability import ChatAuthorizationRefusedError, ChatAuthorizationUnavailableError, Identifier
from src.agentauth.chat_history_store import ChatHistoryStore
from src.agentauth.chat_storage import authority_checks, snapshot_condition
from src.chat_logging.scrubber import RegexScrubber
from src.orchestration.chat_data_migration import _owner_fields

DraftText = Annotated[str, Field(max_length=2000)]
DraftList = Annotated[list[DraftText], Field(max_length=20)]


class ChatDraftConflictError(Exception):
    """The draft version, retry key or live authority changed."""


class WaveDisplay(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    title: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=500)

    @field_validator("title", "description")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("display text must not be blank")
        return value.strip()


class EpicDisplay(WaveDisplay):
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=3000)


class IntentDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    intent: DraftText | None = None
    motivation: DraftText | None = None
    outcomes: DraftList | None = None
    constraints: DraftList | None = None
    open_questions: DraftList | None = Field(default=None, alias="openQuestions")
    wave_display: WaveDisplay | None = Field(default=None, alias="waveDisplay")
    epic_display: EpicDisplay | None = Field(default=None, alias="epicDisplay")

    @model_validator(mode="after")
    def bounded_payload(self):
        if len(self.model_dump_json(by_alias=True, exclude_none=True).encode()) > 131_072:
            raise ValueError("draft payload is too large")
        return self


class _StoredDraft(IntentDraft):
    updated_at: str = Field(alias="updatedAt")

    @field_validator("updated_at")
    @classmethod
    def timestamp(cls, value: str) -> str:
        if datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is None:
            raise ValueError("draft timestamp requires a timezone")
        return value


class DraftWrite(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    idempotency_key: Identifier
    expected_version: int = Field(ge=0, le=99_999_998)
    draft: IntentDraft


def _version(row: dict) -> int:
    version = row.get("draftVersion", 0)
    if isinstance(version, bool) or not isinstance(version, int | Decimal) or int(version) != version or not 0 <= version <= 99_999_999:
        raise ChatAuthorizationUnavailableError("draft version unavailable")
    return int(version)


def _scrub(value, scrubber: RegexScrubber):
    if isinstance(value, str):
        return scrubber.scrub_text(value).content
    if isinstance(value, list):
        return [_scrub(item, scrubber) for item in value]
    return {field: _scrub(item, scrubber) for field, item in value.items()}


class ChatDraftStore:
    def __init__(self, authority, capabilities):
        self.authority = authority
        self.history = ChatHistoryStore(authority.context_table, capabilities)
        self.table = authority.context_table

    def _owned(self, token: str, run_id: str, session_id: str, now: int):
        launch = self.history.capabilities.verify(token, run_id=run_id, session_id=session_id, operation="draft.write", now=now)
        _, header = self.history._authorize(token, run_id, session_id, "draft.write", now)
        if (header["tenantId"], header["teamId"], header["ownerUserId"]) != (launch.tenant_id, launch.team_id, launch.user_id):
            raise ChatAuthorizationRefusedError("draft write ownership refused")
        return launch, header

    def _row(self, session_id: str, header: dict) -> dict | None:
        row = self.history._get(session_id, "draft")
        if row is not None:
            self.history._check_row(row, header)
            _StoredDraft.model_validate(row["draft"])
            _version(row)
        return row

    def read(self, token: str, *, run_id: str, session_id: str, now: int) -> dict:
        _, header = self.history._authorize(token, run_id, session_id, "draft.read", now)
        row = self._row(session_id, header)
        entries = [{"draft": _StoredDraft.model_validate(row["draft"]).model_dump(by_alias=True, exclude_none=True)}] if row else []
        result = self.history._result(entries, now=now)
        result["coverage"]["source"] = "session_draft"
        return {**result, "version": _version(row) if row else 0}

    def _receipt(self, session_id: str, header: dict, receipt_key: str, digest: str) -> dict | None:
        row = self.history._get(session_id, receipt_key)
        if row is None:
            return None
        self.history._check_row(row, header)
        if row.get("requestDigest") != digest:
            raise ChatDraftConflictError("draft idempotency key reused")
        result = row.get("result")
        if not isinstance(result, dict) or set(result) != {"draft", "version"}:
            raise ChatAuthorizationUnavailableError("draft receipt unavailable")
        version = _version({"draftVersion": result["version"]})
        if version == 0:
            raise ChatAuthorizationUnavailableError("draft receipt unavailable")
        return {"draft": _StoredDraft.model_validate(result["draft"]).model_dump(by_alias=True, exclude_none=True), "version": version}

    def _checks(self, launch, header: dict, now: int) -> list[dict]:
        checks = authority_checks(self.authority, launch, now)
        session = checks[0]["ConditionCheck"]
        session["ConditionExpression"] += " AND #ttl = :previous_ttl"
        session["ExpressionAttributeValues"].update(
            _encoded(
                {
                    ":previous_ttl": header["ttl"],
                    ":new_ttl": max(int(header["ttl"]), now + retention_seconds()),
                    ":stamp": datetime.fromtimestamp(now, UTC).isoformat(),
                }
            )
        )
        checks[0] = {"Update": {**session, "UpdateExpression": "SET #ttl = :new_ttl, lastActivityAt = :stamp"}}
        return checks

    def write(self, token: str, *, run_id: str, session_id: str, write: DraftWrite, now: int) -> dict:
        launch, header = self._owned(token, run_id, session_id, now)
        payload = write.model_dump(by_alias=True, exclude_none=True)
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        receipt_key = "draft-write#" + hashlib.sha256(write.idempotency_key.encode()).hexdigest()
        if receipt := self._receipt(session_id, header, receipt_key, digest):
            return receipt
        previous = self._row(session_id, header)
        if (_version(previous) if previous else 0) != write.expected_version:
            raise ChatDraftConflictError("draft version changed")
        stored = _StoredDraft.model_validate(
            {**_scrub(payload["draft"], RegexScrubber()), "updatedAt": datetime.fromtimestamp(now, UTC).isoformat()}
        ).model_dump(by_alias=True, exclude_none=True)
        result = {"draft": stored, "version": write.expected_version + 1}
        provenance = {
            **_owner_fields((launch.tenant_id, launch.team_id, launch.user_id)),
            "PK": header["PK"],
            "runId": launch.run_id,
            "leaseGeneration": launch.lease_generation,
        }
        row = {**provenance, "SK": "draft", "draft": stored, "draftVersion": result["version"]}
        receipt = {**provenance, "SK": receipt_key, "requestDigest": digest, "result": result}
        transaction = [
            {
                "Put": {
                    "TableName": self.table.name,
                    "Item": _encoded(row),
                    **(snapshot_condition(previous) if previous else {"ConditionExpression": "attribute_not_exists(PK)"}),
                }
            },
            {"Put": {"TableName": self.table.name, "Item": _encoded(receipt), "ConditionExpression": "attribute_not_exists(PK)"}},
        ]
        try:
            self.authority.store.client.transact_write_items(TransactItems=transaction + self._checks(launch, header, now))
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == "TransactionCanceledException" and any(
                reason.get("Code") == "ConditionalCheckFailed" for reason in error.response.get("CancellationReasons", [])
            ):
                _, latest = self._owned(token, run_id, session_id, now)
                if receipt := self._receipt(session_id, latest, receipt_key, digest):
                    return receipt
                raise ChatDraftConflictError("draft write state changed; reread before retry") from None
            raise ChatAuthorizationUnavailableError("draft write unavailable") from None
        return result
