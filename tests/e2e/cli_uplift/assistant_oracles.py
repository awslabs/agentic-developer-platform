"""Offline evidence checks for assistant streams and source-grounded answers.

The event vocabulary is a test contract, not proof that a deployed gateway emits
it. A case cannot pass until a remote driver records real events and invokes it.
"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .remote.common import REDACTED
from .remote.common import redact_canary as redact_canary


class EvidenceError(ValueError):
    pass


def require(condition, description):
    if not condition:
        raise EvidenceError(description)


def require_canary_check(canary, canary_check):
    require(
        canary_check in (None, "passed"),
        "Assistant pre-redaction canary check failed or is invalid",
    )
    require(
        (isinstance(canary, str) and canary and canary != REDACTED)
        or (canary is None and canary_check == "passed"),
        "A synthetic canary or successful pre-redaction check is required",
    )


ISOLATION_OWNER_PROBES = (
    "session_read",
    "session_write",
    "message_read",
    "message_write",
    "summary_read",
    "summary_write",
    "artifact_read",
    "artifact_write",
    "memory_read",
    "memory_write",
    "cursor_read",
    "object_read",
    "direct_url_read",
)
ISOLATION_CROSS_PROBES = (
    "session_read",
    "session_write",
    "message_read",
    "message_write",
    "summary_read",
    "summary_write",
    "artifact_read",
    "artifact_write",
    "memory_read",
    "memory_write",
    "cursor_read",
    "object_read",
    "direct_url_read",
    "forged_principal",
    "forged_run",
    "forged_session",
    "forged_generation",
    "forged_user_header",
    "forged_tenant_header",
)
ISOLATION_CREDENTIAL_PROBES = (
    "environment",
    "proc",
    "projected_files",
    "aws_provider",
    "aws_cli",
    "sts_identity",
    "s3_read",
    "dynamodb_read",
    "secrets_read",
    "metadata",
    "kubernetes_api",
    "network_routes",
)


def isolation_expectations():
    expected = {}
    for probe in ISOLATION_OWNER_PROBES:
        expected[(probe, "a1")] = ("a1", "allowed")
    for actor in ("a2", "b1"):
        for probe in ISOLATION_CROSS_PROBES:
            expected[(probe, actor)] = ("a1", "denied")
        for probe in ("residue_file", "residue_process"):
            expected[(probe, actor)] = ("a1", "absent")
    for actor in ("a1", "a2", "b1"):
        expected[("sandbox_identity", actor)] = (actor, "distinct")
        for probe in ISOLATION_CREDENTIAL_PROBES:
            expected[(probe, actor)] = (
                actor,
                "absent"
                if probe in {"environment", "proc", "projected_files"}
                else "denied",
            )
    return expected


def isolation(rows, *, canary, canary_check=None):
    """Verify executed sandbox-tool observations, not model text or HTTP denial."""
    require_canary_check(canary, canary_check)
    expected = isolation_expectations()
    require(isinstance(rows, list), "Assistant isolation probe rows are missing")
    seen = set()
    sandbox_identities = {}
    residue_identities = {}
    tool_calls = set()
    evidence_refs = set()
    for row in rows:
        require(isinstance(row, dict), "Assistant isolation probe row is malformed")
        key = (row.get("probe"), row.get("actor"))
        label = "/".join(str(part) for part in key)
        require(
            key in expected and key not in seen,
            f"Unexpected or repeated isolation probe {label}",
        )
        seen.add(key)
        owner, outcome = expected[key]
        require(
            row.get("target_owner") == owner and row.get("expected") == outcome,
            f"Isolation probe {label} has the wrong target or expectation",
        )
        observed = row.get("observed")
        require(
            isinstance(observed, dict)
            and observed.get("source") == "sandbox_tool"
            and observed.get("executed") is True
            and isinstance(observed.get("tool_call_id"), str)
            and bool(observed["tool_call_id"])
            and isinstance(row.get("evidence_ref"), str)
            and bool(row["evidence_ref"]),
            f"Isolation probe {label} has no correlated sandbox tool execution",
        )
        require(
            observed.get("outcome") == outcome,
            f"Isolation probe {label} expected {outcome}, observed {observed.get('outcome')}",
        )
        require(
            observed["tool_call_id"] not in tool_calls
            and row["evidence_ref"] not in evidence_refs,
            f"Isolation probe {label} reused a tool call or evidence reference",
        )
        tool_calls.add(observed["tool_call_id"])
        evidence_refs.add(row["evidence_ref"])
        if key[0] == "sandbox_identity":
            identity = observed.get("sandbox_uid")
            require(
                isinstance(identity, str) and identity,
                f"Isolation probe {label} has no sandbox identity",
            )
            sandbox_identities[key[1]] = identity
        if key[0] in {"residue_file", "residue_process"}:
            residue_identities[key] = observed.get("sandbox_uid")
    missing = set(expected) - seen
    if missing:
        raise EvidenceError(f"Missing isolation probe {min(missing)}")
    require(
        len(set(sandbox_identities.values())) == 3,
        "Isolation sandbox identities were reused across actors",
    )
    for key, identity in residue_identities.items():
        require(
            identity == sandbox_identities[key[1]],
            f"Isolation residue probe {key} used the wrong sandbox",
        )
    return {
        "probe_count": len(seen),
        "probes": sorted(f"{probe}/{actor}" for probe, actor in seen),
    }


def isolation_receipts(evidence, users, evaluation_id, *, now):
    from .remote.assistant_isolation_evidence import verify

    try:
        verified = verify(evidence, users, evaluation_id, now=now)
        result = isolation(verified["rows"], canary=None, canary_check="passed")
        return {
            **result,
            "rows": verified["rows"],
            "image_digest": verified["image_digest"],
            "trusted_receipts_verified": True,
        }
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        raise EvidenceError(f"Invalid qualification evidence: {error}") from None


def stream(events, *, request_id, canary=None, canary_check=None):
    require_canary_check(canary, canary_check)
    require(
        isinstance(request_id, str) and 0 < len(request_id.strip()) <= 256,
        "Assistant stream requires a nonempty request identifier",
    )
    require(isinstance(events, list) and events, "Assistant events are missing")
    require(len(events) <= 1000, "Assistant event evidence exceeds limit")
    stages = (
        "acknowledged",
        "queued",
        "starting",
        "running",
        "tool",
        "answer",
        "completed",
    )
    positions = []
    seen = set()
    tool_ids = set()
    session_id = None
    previous_cursor = -1
    for event in events:
        require(isinstance(event, dict), "Invalid assistant event")
        require(
            event.get("request_id") == request_id,
            "Assistant event changed turn identity",
        )
        cursor = event.get("cursor")
        require(
            type(cursor) is int and cursor > previous_cursor,
            "Assistant event cursor repeated or out of order",
        )
        previous_cursor = cursor
        kind = event.get("type")
        require(kind in stages, "Assistant event kind is unknown")
        if kind not in {"tool", "answer"}:
            require(kind not in seen, "Assistant lifecycle event was duplicated")
        seen.add(kind)
        positions.append(stages.index(kind))
        require(
            isinstance(event.get("session_id"), str)
            and 0 < len(event["session_id"]) <= 256,
            "Assistant event has no session identity",
        )
        session_id = session_id or event["session_id"]
        require(event["session_id"] == session_id, "Assistant event crossed sessions")
        require(
            canary is None or canary not in str(event),
            "Synthetic secret leaked in assistant event",
        )
        if kind == "acknowledged":
            require(
                event.get("durable") is True, "Assistant acknowledgement is not durable"
            )
        if kind == "tool":
            tool_id = event.get("tool_id")
            require(
                isinstance(tool_id, str)
                and 0 < len(tool_id) <= 256
                and tool_id not in tool_ids,
                "Assistant tool event is missing or duplicated",
            )
            tool_ids.add(tool_id)
        if kind == "answer":
            require(bool(event.get("text")), "Assistant answer is empty")
    require(
        set(stages) == seen
        and positions == sorted(positions)
        and positions[-1] == len(stages) - 1,
        "Assistant event order or completion is incomplete",
    )
    return {
        "request_id": request_id,
        "event_count": len(events),
        "last_cursor": previous_cursor,
        "session_id": session_id,
        "events": [
            {
                key: event[key]
                for key in ("cursor", "type", "tool_id", "durable")
                if key in event
            }
            for event in events
        ],
    }


def instant(value):
    try:
        stamp = datetime.fromisoformat(value)
        require(stamp.tzinfo is not None, "Assistant source has no timezone")
        return stamp.astimezone(timezone.utc)
    except (TypeError, ValueError):
        raise EvidenceError("Assistant source lacks a valid timestamp") from None


def sources(
    pages,
    *,
    expected_ids,
    allowed_ids,
    expected_timestamps,
    window,
    canary=None,
    canary_check=None,
):
    require_canary_check(canary, canary_check)
    require(isinstance(pages, list) and pages, "Assistant source pages are missing")
    require(len(pages) <= 1000, "Assistant source evidence exceeds limit")
    require(isinstance(window, dict), "Assistant source query window is missing")
    try:
        ZoneInfo(window.get("timezone"))
    except (TypeError, ValueError, ZoneInfoNotFoundError):
        raise EvidenceError("Assistant source query timezone is invalid") from None
    start, end = instant(window.get("start")), instant(window.get("end"))
    require(start < end, "Assistant source query window is invalid")
    require(
        isinstance(expected_timestamps, dict)
        and set(expected_timestamps) == set(expected_ids),
        "Assistant source timestamp fixture is missing or incomplete",
    )
    expected = {
        identifier: instant(stamp) for identifier, stamp in expected_timestamps.items()
    }
    require(
        all(start <= stamp < end for stamp in expected.values()),
        "Assistant source fixture is outside query window",
    )
    found = set()
    citations = set()
    observations = []
    previous_cursor = None
    seen_cursors = set()
    for page_index, page in enumerate(pages):
        require(
            isinstance(page, dict)
            and isinstance(page.get("status"), str)
            and page.get("status")
            in {
                "complete",
                "empty",
                "partial",
                "denied",
                "rate_limited",
                "unavailable",
            },
            "Assistant source coverage is unknown",
        )
        require(
            page["status"] in {"complete", "empty"},
            "Assistant source coverage is incomplete",
        )
        require(
            (page_index == 0 or previous_cursor is not None)
            and page.get("cursor") == previous_cursor,
            "Assistant source pagination is broken",
        )
        previous_cursor = page.get("next_cursor")
        require(
            previous_cursor is None
            or (isinstance(previous_cursor, str) and previous_cursor.strip()),
            "Assistant source pagination cursor is invalid",
        )
        if previous_cursor is not None:
            require(
                previous_cursor not in seen_cursors,
                "Assistant source pagination cursor was reused",
            )
            seen_cursors.add(previous_cursor)
        require(
            canary is None or canary not in str(page),
            "Synthetic secret leaked in assistant source",
        )
        records = page.get("records", [])
        page_citations = page.get("citations", [])
        require(isinstance(records, list), "Assistant source records must be a list")
        require(
            isinstance(page_citations, list)
            and all(
                isinstance(citation, str) and 0 < len(citation) <= 256
                for citation in page_citations
            ),
            "Assistant source citations must be a list of source IDs",
        )
        require(
            page["status"] != "empty" or not records,
            "Assistant empty source page contains records",
        )
        for record in records:
            require(
                isinstance(record, dict)
                and isinstance(record.get("id"), str)
                and 0 < len(record["id"]) <= 256,
                "Assistant source ID is invalid or exceeds limit",
            )
            require(
                record["id"] in allowed_ids,
                "Unauthorized or unknown assistant source ID",
            )
            require(record["id"] not in found, "Assistant source was duplicated")
            found.add(record["id"])
            require(len(found) <= 1000, "Assistant source evidence exceeds limit")
            stamp = instant(record.get("timestamp"))
            require(
                stamp == expected.get(record["id"]),
                "Assistant source timestamp differs from known fixture",
            )
            require(start <= stamp < end, "Assistant source is outside query window")
            observations.append({"id": record["id"], "timestamp": stamp.isoformat()})
        citations.update(page_citations)
    require(previous_cursor is None, "Assistant source pagination did not finish")
    require(
        found == set(expected_ids),
        "Assistant source coverage differs from known fixture",
    )
    require(
        citations == found,
        "Assistant citations do not identify exactly the visible sources",
    )
    return {
        "source_count": len(found),
        "citation_count": len(citations),
        "page_count": len(pages),
        "records": observations,
        "citations": sorted(citations),
        "window": {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "timezone": window["timezone"],
        },
    }
