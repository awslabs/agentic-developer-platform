"""Versioned summaries with gateway-verified source ownership and provenance."""

import hashlib
import json
import uuid
from datetime import datetime
from typing import Annotated

from pydantic import Field, model_validator

from src.agentauth.chat_capability import ChatAuthorizationUnavailableError
from src.agentauth.chat_history_store import _Message, _Summary, history_version
from src.agentauth.chat_history_write import ChatHistoryConflictError, ChatHistoryWriter, HistoryWrite
from src.chat_logging.scrubber import RegexScrubber
from src.orchestration.chat_data_migration import _owner_fields

Reference = Annotated[str, Field(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9_.:-]+$")]
MAX_SOURCES = 1000


class SummaryAppend(HistoryWrite):
    source_ids: list[Reference] = Field(default_factory=list, max_length=MAX_SOURCES)
    parent_ids: list[Reference] = Field(default_factory=list, max_length=MAX_SOURCES)

    @model_validator(mode="after")
    def distinct_sources(self):
        if not self.source_ids and not self.parent_ids:
            raise ValueError("a summary requires sources")
        if len(set(self.source_ids)) != len(self.source_ids) or len(set(self.parent_ids)) != len(self.parent_ids):
            raise ValueError("summary sources must be distinct")
        if len(self.source_ids) + len(self.parent_ids) > MAX_SOURCES:
            raise ValueError("too many summary sources")
        return self


def _instant(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError
        return parsed
    except ValueError:
        raise ChatAuthorizationUnavailableError("chat source timestamp unavailable") from None


class ChatSummaryWriter(ChatHistoryWriter):
    def _sources(self, session_id: str, header: dict, write: SummaryAppend, *, timeline: list[dict] | None = None) -> dict:
        references = (
            [(item["type"], item["ref"]) for item in timeline]
            if timeline is not None
            else [("msg", reference) for reference in write.source_ids] + [("sum", reference) for reference in write.parent_ids]
        )
        source_ids = {}
        parents = []
        for kind, reference in references:
            if kind == "msg":
                source_ids.setdefault(reference, None)
            else:
                row = self.history._get(session_id, f"sum#{reference}")
                if row is None:
                    raise ChatHistoryConflictError("chat summary source missing")
                parent = self.history._project(row, header, _Summary)
                parents.append(parent)
                source_ids.update(dict.fromkeys(parent["sourceIds"]))
            if len(source_ids) + len(write.parent_ids) > MAX_SOURCES:
                raise ChatHistoryConflictError("chat summary source limit exceeded")
        if not source_ids:
            raise ChatHistoryConflictError("chat summary sources missing")
        timestamps = []
        for reference in source_ids:
            row = self.history._get(session_id, f"msg#{reference}")
            if row is None:
                raise ChatHistoryConflictError("chat message source missing")
            message = self.history._project(row, header, _Message)
            timestamps.append(message["ts"])
        return {
            "sourceIds": list(source_ids),
            "parentIds": write.parent_ids,
            "kind": "condensed" if parents else "leaf",
            "depth": max(parent["depth"] for parent in parents) + 1 if parents else 0,
            "earliestAt": min(timestamps, key=_instant),
            "latestAt": max(timestamps, key=_instant),
        }

    def append_summary(self, token: str, *, run_id: str, session_id: str, write: SummaryAppend, now: int) -> dict:
        launch, header = self._owned(token, run_id, session_id, now)
        digest = hashlib.sha256(json.dumps(write.model_dump(), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        receipt_key = "summary-write#" + hashlib.sha256(write.idempotency_key.encode()).hexdigest()
        receipt = self._receipt(session_id, receipt_key, digest, header, fields=frozenset({"summary_id", "version"}))
        if receipt is not None:
            return receipt
        if history_version(header) != write.expected_version or write.expected_version == 99_999_999:
            raise ChatHistoryConflictError("chat history version changed")
        sources = self._sources(session_id, header, write)
        result = {"summary_id": "sum_" + uuid.uuid4().hex, "version": write.expected_version + 1}
        provenance = {
            **_owner_fields((launch.tenant_id, launch.team_id, launch.user_id)),
            "PK": header["PK"],
            "runId": launch.run_id,
            "leaseGeneration": launch.lease_generation,
        }
        records = [
            {
                **provenance,
                "SK": "sum#" + result["summary_id"],
                **sources,
                "content": RegexScrubber().scrub_text(write.content).content,
                "tokens": write.tokens,
            },
            {**provenance, "SK": receipt_key, "requestDigest": digest, "result": result},
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
            next_ordinal=self._next_ordinal(header),
            now=now,
        )
