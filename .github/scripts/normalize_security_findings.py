#!/usr/bin/env python3
"""Normalize raw Security Agent findings into one shape (intent #4290, unit U8).

Two scanners feed this module -- the nightly code review and the pentest --
and they are the SAME service, so their findings share one schema
(`findings.schema_fields` in the profile). What differs is only which job
field is populated: `codeReviewJobId` or `pentestJobId`. That is how `source`
is derived, rather than by trusting a caller-passed label.

`code_review_request.write_findings` writes its document verbatim -- "no
normalisation, no dedup, no severity remapping. That is the consuming unit's
job." This module is that job's first half; `dedup_security_findings` is the
second.

Two design rules carry weight here:

**The output is built from an ALLOW-LIST, never by copying-and-deleting.**
`_FIELD_MAP` is the complete set of fields that may leave this module. The
service's schema is an open set (`risk_type_is_open_set: true`, 27 documented
`schema_fields` and no guarantee that is all of them), so a deny-list would
leak any field the service adds later -- including a new exploit-bearing one.
This is the same write-side allow-list reasoning as the ledger's NT-11, applied
at this boundary: `attackScript`, `verificationScript`, `reasoning`,
`codeRemediationTask` and `alignmentRationale` cannot reach an artifact from
here because they are not in the map, not because they are listed for removal.

**Nothing positional enters the comparison key.** See
`dedup_security_findings.fingerprint`; this module's job is to expose both a
keyable signal and a human-readable location as *separate* fields so the
differ cannot accidentally key on the latter.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

# Findings the service itself says are not live work. Dropped here rather than
# in the differ so the counts the differ reports stay a straight raw-vs-new
# comparison. Every drop is COUNTED and reported (`dropped_by_status`): a
# suppression nobody can see is the "fails closed too aggressively" failure
# mode from the issue's impact table, and a silent one is the worst kind.
_NON_ACTIONABLE_STATUSES = frozenset({"RESOLVED", "FALSE_POSITIVE"})

# The complete set of fields that may leave this module, as
# {service field: normalized field}. Adding a row is a reviewable decision.
_FIELD_MAP = {
    "findingId": "finding_id",
    "name": "title",
    "status": "status",
    "riskType": "risk_type",
    "riskLevel": "risk_level",
    "confidence": "confidence",
    "validationStatus": "validation_status",
}

_SOURCE_BY_JOB_FIELD = (
    ("codeReviewJobId", "code-review"),
    ("pentestJobId", "pentest"),
)

_WORD_RE = re.compile(r"[^a-z0-9]+")


class NormalizationError(ValueError):
    """A raw findings document is not the shape this module can consume."""


def load_raw_document(path: Path | str) -> dict:
    """Read one raw findings document.

    Unlike `diff_security_findings.load_json_safe`, a missing or unparseable
    file is an ERROR here, not an empty dict. That module diffs six scanners
    and a missing one means "this tool did not run"; here, an unreadable
    document silently becomes "zero findings tonight", which reads exactly like
    a clean repo. The nightly has already paid for a metered review by this
    point -- failing loudly is the only honest option.
    """
    file_path = Path(path)
    try:
        raw = file_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise NormalizationError(f"cannot read findings document {file_path}: {exc}") from exc
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise NormalizationError(f"{file_path} is not valid JSON: {exc}") from exc
    if not isinstance(document, (dict, list)):
        raise NormalizationError(
            f"{file_path} must hold an object or an array, got {type(document).__name__}"
        )
    return {"findings": document} if isinstance(document, list) else document


def extract_findings(document: dict) -> list[dict]:
    """Pull the findings array out of a raw document."""
    findings = document.get("findings")
    if findings is None:
        return []
    if not isinstance(findings, list):
        raise NormalizationError(
            f"`findings` must be an array, got {type(findings).__name__}"
        )
    return [f for f in findings if isinstance(f, dict)]


def source_of(finding: dict) -> str:
    """Which half reported this finding, derived from the populated job field.

    Falls back to `unknown` rather than guessing: the value is recorded for
    humans and is deliberately NOT part of the comparison key, so an
    unrecognised source cannot split one defect into two work items.
    """
    for field, source in _SOURCE_BY_JOB_FIELD:
        if finding.get(field):
            return source
    return "unknown"


def primary_location(finding: dict) -> dict:
    """The first code location, as {file_path, line_start}.

    `codeLocations` is a list and the service may report several. The first is
    taken for display; the differ keys on a signal derived from ALL of them, so
    this choice cannot affect dedup.
    """
    locations = finding.get("codeLocations")
    if not isinstance(locations, list):
        return {"file_path": "", "line_start": None}
    for location in locations:
        if not isinstance(location, dict):
            continue
        file_path = location.get("filePath")
        if isinstance(file_path, str) and file_path:
            line = location.get("lineStart")
            return {
                "file_path": file_path,
                "line_start": line if isinstance(line, int) and not isinstance(line, bool) else None,
            }
    return {"file_path": "", "line_start": None}


def keyable_files(finding: dict) -> list[str]:
    """The BASENAMES of every file this finding touches, sorted and deduped.

    Basenames, not paths: the issue requires that a MOVED file not resurface a
    known finding, and a full path changes when a file moves. Sorted, because
    the service's ordering within `codeLocations` is not promised to be stable
    and an order-sensitive key would resurface findings on a reshuffle.

    `lineStart` is absent by construction -- it is the other half of the
    positional key the impact table names as a failure mode.
    """
    locations = finding.get("codeLocations")
    if not isinstance(locations, list):
        return []
    names = set()
    for location in locations:
        if not isinstance(location, dict):
            continue
        file_path = location.get("filePath")
        if isinstance(file_path, str) and file_path.strip():
            # PurePosix-style split; findings carry repo-relative POSIX paths.
            names.add(file_path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1])
    return sorted(names)


def prose_signature(text: object) -> str:
    """Reduce LLM-authored prose to a comparable token sequence.

    The spike found "zero identical `name` values" across two runs, so raw
    titles cannot be compared literally. Normalizing to lowercase alphanumeric
    tokens absorbs the punctuation, casing and spacing churn that accounts for
    much of that difference. It does NOT absorb genuine rewording -- see the
    honest limitation recorded in `dedup_security_findings.fingerprint`.
    """
    if not isinstance(text, str):
        return ""
    return " ".join(t for t in _WORD_RE.sub(" ", text.lower()).split() if t)


def normalize_finding(finding: dict) -> dict:
    """One service finding -> one normalized finding.

    Built by projecting through `_FIELD_MAP`; a field absent from the input
    lands as None rather than being omitted, so every normalized finding has
    the same keys and a consumer never has to probe for presence.
    """
    normalized = {out: finding.get(src) for src, out in _FIELD_MAP.items()}
    normalized["source"] = source_of(finding)
    normalized.update(primary_location(finding))
    normalized["key_files"] = keyable_files(finding)
    normalized["title_signature"] = prose_signature(finding.get("name"))
    return normalized


def is_actionable(normalized: dict) -> bool:
    """False for findings the service marked resolved or a false positive."""
    status = normalized.get("status")
    status = status.upper() if isinstance(status, str) else ""
    return status not in _NON_ACTIONABLE_STATUSES


def normalize_document(document: dict) -> dict:
    """Normalize one raw document.

    Returns the actionable normalized findings plus the two counts the caller
    needs to be truthful: how many the scanner actually reported
    (`identified_raw`), and how many were dropped as non-actionable.
    """
    findings = extract_findings(document)
    normalized = [normalize_finding(f) for f in findings]
    actionable = [n for n in normalized if is_actionable(n)]
    return {
        "run_date": document.get("runDate"),
        "identified_raw": len(findings),
        "dropped_by_status": len(normalized) - len(actionable),
        "findings": actionable,
    }


def normalize_documents(paths: list[Path | str]) -> dict:
    """Normalize several raw documents into one result.

    `identified_raw` sums, matching the ledger field's `merge: sum` rule -- the
    two concurrent halves each report their own.
    """
    if not paths:
        raise NormalizationError("no findings documents given")
    merged: dict = {
        "run_date": None,
        "identified_raw": 0,
        "dropped_by_status": 0,
        "findings": [],
    }
    for path in paths:
        result = normalize_document(load_raw_document(path))
        merged["run_date"] = merged["run_date"] or result["run_date"]
        merged["identified_raw"] += result["identified_raw"]
        merged["dropped_by_status"] += result["dropped_by_status"]
        merged["findings"].extend(result["findings"])
    return merged
