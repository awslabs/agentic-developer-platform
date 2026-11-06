"""Source coverage a chat turn reports for its answer (#8113).

The frontend already renders an ``AnswerEvidence`` footer (window, freshness, per-source
coverage, links) and already consumes an AG-UI ``CUSTOM`` event named ``answer_evidence``
(``hooks/useAgUiEvents.ts``); nothing produced it until now. The sandbox composes this
structure from the platform tools it called during the turn, sends it with the final
model invocation (so the delivery relay can attach it to the streamed assistant message)
and records it on the turn result (so the audit trail carries which sources an answer
rested on). The shape mirrors the frontend parser's bounds exactly.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

EvidenceStatus = Literal["ok", "empty", "denied", "unavailable", "partial"]
CoverageStatus = Literal["available", "empty", "denied", "unavailable", "partial"]
_INSTANT = re.compile(r"^\d{4}-\d{2}-\d{2}T[0-9:.]+(?:Z|[+-]\d{2}:\d{2})$")


class EvidenceCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    source: str = Field(min_length=1, max_length=100)
    status: CoverageStatus
    reason: str | None = Field(default=None, min_length=1, max_length=160)


class EvidenceSource(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    label: str = Field(min_length=1, max_length=100)
    url: str = Field(min_length=1, max_length=2048)

    @field_validator("url")
    @classmethod
    def safe_url(cls, value: str) -> str:
        # The footer only links https URLs or same-origin paths; anything else is dropped there,
        # so refusing it here keeps the record and the screen in agreement.
        if not (value.startswith("https://") or (value.startswith("/") and not value.startswith("//"))):
            raise ValueError("evidence source url must be https or a same-origin path")
        return value


class AnswerEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True)

    status: EvidenceStatus
    from_: str | None = Field(default=None, alias="from", max_length=64)
    to: str | None = Field(default=None, max_length=64)
    timezone: str | None = Field(default=None, min_length=1, max_length=64)
    observed_at: str | None = Field(default=None, max_length=64)
    coverage: list[EvidenceCoverage] = Field(default_factory=list, max_length=32)
    sources: list[EvidenceSource] = Field(default_factory=list, max_length=32)
    has_more: bool = False

    @field_validator("from_", "to", "observed_at")
    @classmethod
    def instant(cls, value: str | None) -> str | None:
        if value is not None and not _INSTANT.match(value):
            raise ValueError("evidence instants are ISO-8601 with an offset")
        return value

    def document(self) -> dict:
        """The JSON form stored on the turn and sent to the owner's screen."""
        return self.model_dump(by_alias=True, exclude_none=True)


__all__ = ["AnswerEvidence", "EvidenceCoverage", "EvidenceSource"]
