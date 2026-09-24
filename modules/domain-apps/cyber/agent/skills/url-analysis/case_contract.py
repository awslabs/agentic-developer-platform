"""Shared, versioned research evidence contract. No browser or AWS access."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from evidence_items import (
    checked_item,
    partial_evidence_usable,
    warning_evidence_usable,
)
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
    basis: Literal["observation", "hypothesis"]
    evidence_ids: list[str] = Field(min_length=1, max_length=10)
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
    risk: Literal["suspicious", "inconclusive", "no_specific_concern"]
    findings: list[ContextFinding] = Field(default_factory=list, max_length=20)
    limitations: list[str] = Field(min_length=1, max_length=20)


class EvidenceValidationError(ValueError):
    def __init__(self, message, *, finding_index, observation_id, correction):
        self.detail = {
            "finding_index": finding_index,
            "observation_id": observation_id,
            "correction": correction,
        }
        super().__init__(
            f"Finding {finding_index}, {observation_id}: {message}. {correction}"
        )


class Assessment(Contract):
    verdict: Literal[
        "no_adverse_behavior_observed", "suspicious", "malicious", "inconclusive"
    ]
    findings: list[Finding] = Field(default_factory=list, max_length=30)
    limitations: list[str] = Field(default_factory=list, max_length=30)
    recommended_actions: list[str] = Field(default_factory=list, max_length=20)
    assessor: str = Field(min_length=1, max_length=200)
    model_version: str = Field(default="", max_length=200)
    context_assessment: ContextAssessment | None = None

    def validate_context(self, records):
        if self.context_assessment is None:
            return
        by_id = {r["id"]: r for r in records if "id" in r}
        for finding in self.context_assessment.findings:
            if not set(finding.source_ids) <= by_id.keys():
                raise ValueError("Context finding cites an unknown source ID")
            if finding.basis == "reported" and any(
                by_id[i].get("status") not in {"available", "reported"}
                for i in finding.source_ids
            ):
                raise ValueError(
                    "Unavailable context sources cannot support reported facts"
                )
        if self.context_assessment.risk != "inconclusive" and not any(
            f.basis == "reported" for f in self.context_assessment.findings
        ):
            raise ValueError("Context risk requires a sourced reported finding")

    def validate_evidence(self, observations: list[dict]) -> None:
        by_id = {o["id"]: o for o in observations}
        for finding in self.findings:
            if not set(finding.evidence_ids) <= by_id.keys():
                raise ValueError("Finding cites an unknown observation")
            cited_observations = [by_id[i] for i in finding.evidence_ids]
            for ref in finding.evidence_refs:
                if ref.observation_id not in finding.evidence_ids:
                    raise ValueError(
                        "Item references must belong to cited observations"
                    )
                checked_item(by_id[ref.observation_id], ref.item_id)
            if finding.kind == "threat_warning" and not any(
                warning_evidence_usable(o) for o in cited_observations
            ):
                raise ValueError(
                    "Threat-warning findings require a captured threat warning"
                )
            if finding.kind == "redirect" and not any(
                r.get("kind") in {"http", "navigation"}
                for o in cited_observations
                for r in o.get("redirects", [])
            ):
                raise ValueError(
                    "Redirect findings require observed navigation/redirect evidence; form actions are configuration"
                )
            if finding.kind == "download_offer" and not any(
                o.get("downloads") for o in cited_observations
            ):
                raise ValueError("Download findings require a captured download offer")
            if finding.kind == "content_variation":
                cited = [by_id[i] for i in set(finding.evidence_ids)]
                if len(cited) < 2 or len({o["subject_sha256"] for o in cited}) != 1:
                    raise ValueError(
                        "Variation requires two observations of the same input"
                    )
                if (
                    len(
                        {
                            o.get("content_sha256")
                            for o in cited
                            if o.get("content_sha256")
                        }
                    )
                    < 2
                ):
                    raise ValueError("Variation requires different captured content")
        if self.verdict != "inconclusive":
            if not any(
                f.basis == "observation" and f.kind != "coverage_limitation"
                for f in self.findings
            ):
                raise ValueError("An assessment requires evidence-linked findings")
            if self.verdict == "malicious" and not any(
                f.basis == "observation"
                and f.kind
                not in {"threat_warning", "coverage_limitation", "benign_context"}
                for f in self.findings
            ):
                raise ValueError(
                    "A displayed warning alone cannot establish malicious page behavior; assess suspicion and context separately"
                )
            for index, finding in enumerate(self.findings):
                for observation_id in finding.evidence_ids:
                    observation = by_id[observation_id]
                    if (
                        finding.kind == "coverage_limitation"
                        and self.verdict != "no_adverse_behavior_observed"
                    ):
                        if not self.limitations:
                            raise ValueError(
                                "Coverage findings must also be stated in limitations"
                            )
                        continue
                    if finding.kind == "threat_warning" and warning_evidence_usable(
                        observation
                    ):
                        if not any(
                            r.observation_id == observation_id
                            and r.item_id == "warning-001"
                            for r in finding.evidence_refs
                        ):
                            raise ValueError(
                                "Cite the intact warning-001 item; it does not establish hidden page behavior"
                            )
                        if not self.limitations:
                            raise ValueError(
                                "A threat-warning finding must state its limitations"
                            )
                        continue
                    if observation["status"] == "complete":
                        continue
                    if (
                        self.verdict == "no_adverse_behavior_observed"
                        or not partial_evidence_usable(observation)
                    ):
                        raise EvidenceValidationError(
                            "Incomplete collection cannot support this finding",
                            finding_index=index,
                            observation_id=observation_id,
                            correction="Retain earlier supported findings with their own citations. Describe this view as coverage_limitation; do not change the verdict merely to work around an unsupported citation.",
                        )
                    refs = [
                        r
                        for r in finding.evidence_refs
                        if r.observation_id == observation_id
                    ]
                    if not refs:
                        raise ValueError(
                            "Partial observations require intact, specific evidence_refs"
                        )
                    kinds = {checked_item(observation, r.item_id)["kind"] for r in refs}
                    if finding.kind == "credential_collection" and not kinds & {
                        "form",
                        "script",
                        "network",
                    }:
                        raise ValueError(
                            "Credential findings require form, script, or network evidence"
                        )
                    if finding.kind == "brand_impersonation" and not kinds & {
                        "text",
                        "screenshot",
                    }:
                        raise ValueError(
                            "Brand findings require captured text or screenshot evidence"
                        )
                    if finding.kind in {"redirect", "download_offer"} and (
                        {"redirect": "redirect", "download_offer": "download"}[
                            finding.kind
                        ]
                        not in kinds
                    ):
                        raise ValueError(
                            "Cite the specific redirect or download evidence item"
                        )
                    if not self.limitations:
                        raise ValueError(
                            "An adverse verdict with partial coverage must state limitations"
                        )
        if self.verdict == "no_adverse_behavior_observed" and (
            not observations or any(o["status"] != "complete" for o in observations)
        ):
            raise ValueError("A partial or failed probe cannot support clearance")


def content_digest(observation: dict) -> str:
    content = {k: observation.get(k) for k in ("visible_text", "forms", "frames")}
    return digest(json.dumps(content, sort_keys=True, ensure_ascii=False))
