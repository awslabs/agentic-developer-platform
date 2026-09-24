"""Shared, versioned research evidence contract. No browser or AWS access."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from evidence_items import checked_item
from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = "url-research/1"
COLLECTOR_VERSION = "1.3.0"
MAX_RESPONSE_BYTES = 16 * 1024 * 1024


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def digest(value: bytes | str) -> str:
    return hashlib.sha256(
        value.encode() if isinstance(value, str) else value
    ).hexdigest()


def redact_url(value: str) -> str:
    """Preserve the destination, removing userinfo, fragments and ALL query values."""
    try:
        p = urlsplit(value)
        if p.scheme not in {"http", "https"} or not p.hostname:
            return "[non-http URL]"
        host = f"[{p.hostname}]" if ":" in p.hostname else p.hostname
        authority = host + (f":{p.port}" if p.port else "")
        query = urlencode(
            [(k, "REDACTED") for k, _ in parse_qsl(p.query, keep_blank_values=True)]
        )
        return urlunsplit((p.scheme, authority, p.path, query, ""))
    except ValueError:
        return "[invalid URL]"


def sanitize(value):
    """URL redaction, not general PII removal from page text or images."""
    if isinstance(value, dict):
        return {
            k: sanitize(v)
            for k, v in value.items()
            if k.lower()
            not in {"authorization", "cookie", "set-cookie", "postdata", "value"}
        }
    if isinstance(value, list):
        return [sanitize(v) for v in value]
    if isinstance(value, str):
        return re.sub(r"https?://[^\s<>\"']+", lambda m: redact_url(m[0]), value)
    return value


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EvidenceReference(Contract):
    observation_id: str = Field(pattern=r"^obs-[0-9]{3}$")
    item_id: str = Field(
        pattern=r"^(text|form|script|network|redirect|download|screenshot|warning)-[0-9]{3}$"
    )


class Finding(Contract):
    kind: Literal[
        "credential_collection",
        "brand_impersonation",
        "download_offer",
        "redirect",
        "content_variation",
        "benign_context",
        "threat_warning",
        "coverage_limitation",
        "other",
    ]
    statement: str = Field(min_length=1, max_length=2000)
    basis: Literal["observation", "reported", "hypothesis"]
    evidence_ids: list[str] = Field(default_factory=list, max_length=10)
    source_ids: list[str] = Field(default_factory=list, max_length=10)
    evidence_refs: list[EvidenceReference] = Field(default_factory=list, max_length=30)


class IncidentReport(Contract):
    source: str = Field(min_length=1, max_length=300)
    reported_at: str = Field(min_length=1, max_length=100)
    summary: str = Field(min_length=1, max_length=4000)


class ContextFinding(Contract):
    statement: str = Field(min_length=1, max_length=2000)
    source_ids: list[str] = Field(min_length=1, max_length=10)
    basis: Literal["reported", "hypothesis"]


class ContextAssessment(Contract):
    # Read older reports without imposing a second, weaker verdict vocabulary.
    risk: (
        Literal[
            "clean", "suspicious", "malicious", "inconclusive", "no_specific_concern"
        ]
        | None
    ) = None
    findings: list[ContextFinding] = Field(default_factory=list, max_length=20)
    limitations: list[str] = Field(min_length=1, max_length=20)


class Assessment(Contract):
    verdict: Literal[
        "clean",
        "no_specific_concern",
        "no_adverse_behavior_observed",
        "suspicious",
        "malicious",
        "inconclusive",
    ]
    findings: list[Finding] = Field(default_factory=list, max_length=30)
    limitations: list[str] = Field(default_factory=list, max_length=30)
    recommended_actions: list[str] = Field(default_factory=list, max_length=20)
    assessor: str = Field(min_length=1, max_length=200)
    confidence: Literal["high", "medium", "low"] | None = None
    model_version: str = Field(default="", max_length=200)
    context_assessment: ContextAssessment | None = None

    def validate_context(self, records):
        """Check source references only; the model evaluates their meaning and quality."""
        known = {r["id"] for r in records if "id" in r}
        for finding in self.findings:
            if not finding.evidence_ids and not finding.source_ids:
                raise ValueError("Finding requires an observation or source reference")
            if not set(finding.source_ids) <= known:
                raise ValueError("Finding cites an unknown source ID")
        for finding in (
            self.context_assessment.findings if self.context_assessment else []
        ):
            if not set(finding.source_ids) <= known:
                raise ValueError("Context finding cites an unknown source ID")

    def validate_evidence(self, observations: list[dict]) -> None:
        """Check reference existence and integrity without adjudicating the verdict."""
        by_id = {o["id"]: o for o in observations}
        for finding in self.findings:
            if not set(finding.evidence_ids) <= by_id.keys():
                raise ValueError("Finding cites an unknown observation")
            for ref in finding.evidence_refs:
                if ref.observation_id not in finding.evidence_ids:
                    raise ValueError(
                        "Item references must belong to cited observations"
                    )
                checked_item(by_id[ref.observation_id], ref.item_id)


def content_digest(observation: dict) -> str:
    content = {k: observation.get(k) for k in ("visible_text", "forms", "frames")}
    return digest(json.dumps(content, sort_keys=True, ensure_ascii=False))


def assessment_schema():
    """New assessments use four labels; the reader still accepts legacy records."""
    schema = Assessment.model_json_schema()
    schema["properties"]["verdict"]["enum"] = [
        "clean",
        "suspicious",
        "malicious",
        "inconclusive",
    ]
    return schema
