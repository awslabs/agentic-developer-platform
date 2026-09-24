"""Evidence-led, multi-page investigation tools for the existing cyber agent.

The agent chooses and reviews actions. This CLI neither crawls automatically nor
instantiates a model. Browser capabilities are kept outside the artifact bundle.
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import json
import os
from contextlib import contextmanager
from pathlib import Path

from analyst_context import SOURCES, context_records, incident_records, lookup
from browser_client import investigation_request
from browser_guard import DestinationRefused
from case_contract import (
    Assessment,
    assessment_schema,
    content_digest,
    digest,
    sanitize,
    utcnow,
)
from evidence_items import evidence_coverage, validate_inventory
from research_case import (
    CASE_FILE,
    _pending,
    assess_case,
    collection_summary,
    new_case,
    save_case,
    verify_case,
)

MAX_INVESTIGATION_STEPS = 24
MAX_PROFILES = 2
MAX_ARCHIVE_PAGES = 8


def _lease_path(output):
    root = Path("/tmp/adp-url-browser-leases")
    if root.is_symlink():
        raise ValueError("Invalid private lease directory")
    root.mkdir(mode=0o700, exist_ok=True)
    os.chmod(root, 0o700)
    return root / (digest(str(output.resolve())) + ".json")


def _private_write(path, value):
    temp = path.with_suffix(".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


@contextmanager
def _case(output):
    with (output / ".case.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        verify_case(output)
        case = json.loads((output / CASE_FILE).read_text())
        if case.get("case_kind") != "domain_investigation":
            raise ValueError("This is not a domain investigation")
        yield case


def _text(value, name):
    if not isinstance(value, str) or not value.strip() or len(value) > 2000:
        raise ValueError(f"{name} must contain 1–2000 characters")
    return sanitize(value)


def _citations(case, ids, *, latest=True):
    if not isinstance(ids, list) or not ids or not all(isinstance(i, str) for i in ids):
        raise ValueError("Evidence references are required")
    known = {o["id"] for o in case["observations"]}
    if not set(ids) <= known or (latest and case["observations"][-1]["id"] not in ids):
        raise ValueError("Cite actual evidence, including the latest observation")
    return ids


def _new_probe(case, action, decision):
    if len(case["probes"]) >= MAX_INVESTIGATION_STEPS:
        raise ValueError(
            "Investigation step budget exhausted; close and report coverage"
        )
    probe = {
        "id": f"probe-{len(case['probes']) + 1:03d}",
        "action": action,
        "status": "running",
        "started_at": utcnow(),
        "decision": sanitize(decision),
    }
    case["probes"].append(probe)
    case["assessment"] = _pending("New evidence requires researcher assessment")
    return probe


def _accept(output, case, probe, packet):
    if packet.get("schema_version") != "domain-investigation/1":
        raise ValueError("Incompatible investigation response")
    manifest = packet["manifest"]
    if manifest.get("cleanup_status") not in {"open", "stopped", "failed", "unknown"}:
        raise ValueError("Missing browser lifecycle evidence")
    observations = packet.get("observations", [])
    if not 1 <= len(observations) <= 2:
        raise ValueError("Invalid observation count")
    sid = manifest["session_id"]
    case["unconfirmed_browser_start"] = False
    if not any(s["id"] == sid for s in case["sessions"]):
        case["sessions"].append(
            {
                "id": sid,
                "profile": manifest["profile"],
                "cleanup_status": manifest["cleanup_status"],
            }
        )
    previous_id = case["observations"][-1]["id"] if case["observations"] else None
    for raw in observations:
        validate_inventory(raw)
        if raw.get("session_id") != sid or raw.get("content_sha256") != content_digest(
            raw
        ):
            raise ValueError("Observation provenance or content hash differs")
        if raw.get("status") not in {"complete", "partial", "failed"}:
            raise ValueError("Invalid observation status")
        if raw["status"] == "complete" and (
            (
                not raw.get("screenshot_base64")
                and raw.get("screenshot_status") != "not_requested"
            )
            or not raw.get("dom_snapshot")
            or not raw.get("visible_text", "").strip()
            or raw.get("errors")
            or raw.get("blocked_requests")
            or not raw.get("network_requests")
            or type(raw.get("http_status")) is not int
            or not 200 <= raw["http_status"] < 400
        ):
            raise ValueError("Completeness requires captured evidence")
        o = dict(raw)
        o["id"] = f"obs-{len(case['observations']) + 1:03d}"
        o["probe_id"] = probe["id"]
        image = o.pop("screenshot_base64", "")
        if image:
            body = base64.b64decode(image, validate=True)
            if (
                not body.startswith(b"\x89PNG")
                or len(body) > 5 * 1024 * 1024
                or digest(body) != o["screenshot_sha256"]
            ):
                raise ValueError("Screenshot integrity check failed")
            o["screenshot"] = o["id"] + ".png"
            (output / o["screenshot"]).write_bytes(body)
        dom = o.pop("dom_snapshot", "")
        if dom:
            o["dom_snapshot"] = o["id"] + "-dom.txt"
            o["dom_sha256"] = digest(dom)
            (output / o["dom_snapshot"]).write_text(dom)
        case["observations"].append(sanitize(o))
        case.setdefault("navigation_graph", []).append(
            {
                "from_observation": previous_id,
                "to_observation": o["id"],
                "action": probe["action"],
                "probe_id": probe["id"],
                "url": o["final_url"],
                "session_id": sid,
            }
        )
        previous_id = o["id"]
    for lead in packet.get("external_leads", []):
        case.setdefault("external_leads", []).append(
            {**lead, "observation_id": previous_id}
        )
    probe["manifest"] = sanitize(manifest)
    probe["status"] = (
        "complete"
        if all(o["status"] == "complete" for o in observations)
        else "partial"
    )
    probe["completed_at"] = utcnow()
    case["browser_view"] = {
        k: packet[k]
        for k in (
            "view_id",
            "choices",
            "external_leads",
            "session_open",
            "steps_used",
            "max_steps",
        )
    }
    if not packet["session_open"]:
        _record_close(
            case, {"session_id": sid, "cleanup_status": manifest["cleanup_status"]}
        )
    save_case(output, case)


def _record_close(case, result):
    status = result.get("cleanup_status", "unknown")
    sid = result.get("session_id")
    for session in case["sessions"]:
        if session["id"] == sid:
            session.update(cleanup_status=status, closed_at=utcnow())
    for probe in case["probes"]:
        if probe.get("manifest", {}).get("session_id") == sid:
            probe["manifest"]["cleanup_status"] = status
    case["browser_view"]["session_open"] = False


def _failure(output, case, probe, error):
    probe.update(status="failed", completed_at=utcnow(), error=type(error).__name__)
    probe["diagnostic"] = sanitize(str(error))[:1000]
    if getattr(error, "code", None):
        probe["broker_error_code"] = error.code
        probe["retry_after_seconds"] = getattr(error, "retry_after", None)
    if getattr(error, "browser_start_unattempted", False) is True:
        case["unconfirmed_browser_start"] = False
    cleanup = getattr(error, "cleanup", None)
    if cleanup and cleanup.get("session_id"):
        if not any(s["id"] == cleanup["session_id"] for s in case["sessions"]):
            case["sessions"].append({"id": cleanup["session_id"], "profile": "unknown"})
        _record_close(case, cleanup)
        if cleanup.get("cleanup_status") == "stopped":
            case["unconfirmed_browser_start"] = False
    if hasattr(error, "reason_code"):
        probe["reason_code"] = error.reason_code
        if (
            not case["observations"]
            and getattr(error, "browser_start_unattempted", False) is True
        ):
            case["unconfirmed_browser_start"] = False
    case["assessment"] = _pending(f"{probe['id']} failed; retain earlier evidence")
    if not case["observations"]:
        case["stop_reason"] = (
            "Initial collection failed; no observation exists to assess"
        )
        case["assessment"] = _pending(
            f"No page evidence collected ({probe.get('reason_code', probe['error'])}). "
            "Unavailable or blocked collection does not establish safety or maliciousness."
        )
    save_case(output, case)


def _initialize(
    output,
    url,
    objective,
    *,
    profile="desktop",
    scope="observed_external",
    incident_context=None,
    brand_references=None,
):
    objective = _text(objective, "objective")
    reports = incident_records(incident_context or [])
    from corroboration import BrandReference, compare_brand

    references = [
        BrandReference.model_validate(r).model_dump(mode="json")
        for r in (brand_references or [])
    ]
    if len(references) > 10:
        raise ValueError("At most ten brand references are allowed")
    case = new_case(output, url)
    case.update(
        case_kind="domain_investigation",
        objective=objective,
        scope=scope,
        sessions=[],
        reviews=[],
        browser_view={"session_open": False},
        incident_context=reports,
        corroboration=[],
    )
    for reference in references:
        _append_context(case, {**compare_brand(case, reference), "status": "available"})
    _private_write(
        _lease_path(output), {"url": url, "scope": scope, "profile": profile}
    )
    save_case(output, case)
    return case


def prepare(output, url, objective, *, lookup_fn=lookup, **options):
    """Collect historical context without starting a browser or consuming a lease."""
    case = _initialize(output, url, objective, **options)
    _append_context(
        case,
        {
            **lookup_fn("common_crawl", url),
            "requested_source": "common_crawl",
            "request_reason": "Establish historical coverage before the initial hypothesis",
        },
    )
    case["workflow"] = "archive_then_browser"
    save_case(output, case)
    return case


def hypothesize(output, value):
    with _case(output) as case:
        if case["probes"] or case.get("workflow") != "archive_then_browser":
            raise ValueError("Initial hypothesis belongs to a prepared, unopened case")
        if not isinstance(value, dict) or set(value) != {
            "hypothesis",
            "source_ids",
            "limitations",
            "next_question",
        }:
            raise ValueError(
                "Supply hypothesis, source_ids, limitations and next_question"
            )
        known = {r["id"] for r in context_records(case)}
        ids = value["source_ids"]
        if (
            not isinstance(ids, list)
            or not ids
            or not all(isinstance(i, str) and i in known for i in ids)
        ):
            raise ValueError(
                "Cite the recorded context, including unavailable coverage when appropriate"
            )
        if (
            not isinstance(value["limitations"], list)
            or not 1 <= len(value["limitations"]) <= 10
        ):
            raise ValueError("Record one to ten historical-coverage limitations")
        case["initial_hypothesis"] = {
            "hypothesis": _text(value["hypothesis"], "hypothesis"),
            "next_question": _text(value["next_question"], "next_question"),
            "source_ids": ids,
            "limitations": [_text(v, "limitation") for v in value["limitations"]],
            "recorded_at": utcnow(),
            "basis": "hypothesis_not_a_verdict",
        }
        save_case(output, case)
        return case


def browse(output, *, request=investigation_request):
    with _case(output) as case:
        if case["probes"] or not case.get("initial_hypothesis"):
            raise ValueError(
                "Prepare and record an initial hypothesis before opening the browser; do not replay starts"
            )
        lease = json.loads(_lease_path(output).read_text())
        return _open_initial(
            output, case, lease["url"], lease["profile"], lease["scope"], request
        )


def start(
    output,
    url,
    objective,
    *,
    profile="desktop",
    scope="observed_external",
    incident_context=None,
    brand_references=None,
    request=investigation_request,
):
    """Compatibility entry point for existing browser-only adapters."""
    case = _initialize(
        output,
        url,
        objective,
        profile=profile,
        scope=scope,
        incident_context=incident_context,
        brand_references=brand_references,
    )
    return _open_initial(output, case, url, profile, scope, request)


def _open_initial(output, case, url, profile, scope, request):
    probe = _new_probe(
        case,
        "start",
        {
            "question": case.get("initial_hypothesis", {}).get(
                "next_question", case["objective"]
            ),
            "reason": "Inspect the supplied seed before choosing an investigation path",
        },
    )
    save_case(output, case)
    packet = None
    case["unconfirmed_browser_start"] = True
    save_case(output, case)
    try:
        packet = request("start", {"url": url, "profile": profile, "scope": scope})
        lease = {
            "session_token": packet.pop("session_token"),
            "url": url,
            "scope": scope,
        }
        _private_write(_lease_path(output), lease)
        _accept(output, case, probe, packet)
    except Exception as error:
        _failure(output, case, probe, error)
        if packet and "lease" in locals():
            _close_after_failure(output, case, lease, request)
        if (
            isinstance(error, DestinationRefused)
            and error.reason_code == "resolution_failed"
        ):
            # A recorded unavailable result is terminal; do not spend model turns
            # creating findings or reviews for observations that do not exist.
            return case
        raise
    return case


def review(output, finding):
    with _case(output) as case:
        if not isinstance(finding, dict) or set(finding) != {
            "hypothesis",
            "outcome",
            "explanation",
            "evidence_ids",
            "next_question",
        }:
            raise ValueError(
                "Review requires hypothesis, outcome, explanation, evidence_ids and next_question"
            )
        if finding["outcome"] not in {"supported", "refuted", "revised", "unresolved"}:
            raise ValueError("Invalid hypothesis outcome")
        if case["probes"][-1]["status"] == "running" or not case["observations"]:
            raise ValueError("There is no completed observation to review")
        value = {
            k: _text(finding[k], k)
            for k in ("hypothesis", "explanation", "next_question")
        }
        value.update(
            outcome=finding["outcome"],
            evidence_ids=_citations(case, finding["evidence_ids"]),
            probe_id=case["probes"][-1]["id"],
            recorded_at=utcnow(),
        )
        if len(case["reviews"]) >= 2 * MAX_INVESTIGATION_STEPS:
            raise ValueError("Review budget exhausted")
        case["reviews"].append(value)
        save_case(output, case)
        return case


def _decision(case, value):
    # One concise rationale is sufficient; separate review files remain optional.
    if isinstance(value, str):
        return {"reason": _text(value, "reason"), "evidence_ids": []}
    if (
        not isinstance(value, dict)
        or "reason" not in value
        or set(value)
        - {"question", "reason", "expected_signal", "evidence_ids", "source_ids"}
    ):
        raise ValueError(
            "Decision requires a reason and optional evidence/source references"
        )
    result = {
        k: _text(value[k], k)
        for k in ("question", "reason", "expected_signal")
        if k in value
    }
    result["evidence_ids"] = (
        _citations(case, value["evidence_ids"], latest=False)
        if value.get("evidence_ids")
        else []
    )
    ids = value.get("source_ids", [])
    known = {r["id"] for r in context_records(case)}
    if not isinstance(ids, list) or not all(
        isinstance(i, str) and i in known for i in ids
    ):
        raise ValueError("Decision cites an unknown source")
    result["source_ids"] = ids
    return result


def _close_after_failure(output, case, lease, request):
    try:
        result = request("close", {"session_token": lease["session_token"]})
    except Exception:
        result = {
            "session_id": case["sessions"][-1]["id"] if case["sessions"] else None,
            "cleanup_status": "unknown",
        }
    _record_close(case, result)
    save_case(output, case)
    if result["cleanup_status"] == "stopped":
        case["unconfirmed_browser_start"] = False
        save_case(output, case)
        _lease_path(output).unlink(missing_ok=True)


def step(
    output,
    action,
    decision,
    *,
    candidate_id=None,
    seconds=None,
    url=None,
    request=investigation_request,
):
    with _case(output) as case:
        decision = _decision(case, decision)
        if not case["browser_view"]["session_open"]:
            raise ValueError(
                "Browser context ended; assess coverage or start a separate profile"
            )
        lease = json.loads(_lease_path(output).read_text())
        payload = {
            "session_token": lease["session_token"],
            "view_id": case["browser_view"]["view_id"],
            "action": action,
        }
        if candidate_id is not None:
            payload["candidate_id"] = candidate_id
        if seconds is not None:
            payload["seconds"] = seconds
        if url is not None:
            from research_case import _validate_input

            _validate_input(url)
            payload["url"] = url
        probe = _new_probe(
            case,
            action,
            {**decision, **{k: v for k, v in payload.items() if k != "session_token"}},
        )
        save_case(output, case)
        try:
            _accept(output, case, probe, request("step", payload))
        except Exception as error:
            _failure(output, case, probe, error)
            _close_after_failure(output, case, lease, request)
            raise
        return case


def retained_findings(case):
    """Retain normalized, referenced findings from the latest rejected assessment."""
    retained = []
    for attempt in case.get("assessment_attempts", [])[-1:]:
        candidate = attempt["assessment"]
        if not isinstance(candidate, dict) or not isinstance(
            candidate.get("findings", []), list
        ):
            continue
        for finding in candidate.get("findings", []):
            try:
                checked = Assessment.model_validate(
                    {**candidate, "findings": [finding], "context_assessment": None}
                )
                checked.validate_evidence(case["observations"])
                checked.validate_context(context_records(case))
                normalized = checked.findings[0].model_dump()
                if normalized not in retained:
                    retained.append(normalized)
            except (ValueError, TypeError):
                continue
    return retained[:30]


def close(output, reason, *, request=investigation_request):
    reason = _text(reason, "stop reason")
    with _case(output) as case:
        case["stop_reason"] = reason
        if case["browser_view"]["session_open"] or any(
            s["cleanup_status"] != "stopped" for s in case["sessions"]
        ):
            lease = json.loads(_lease_path(output).read_text())
            try:
                result = request("close", {"session_token": lease["session_token"]})
                _record_close(case, result)
            except Exception:
                _record_close(
                    case,
                    {
                        "session_id": case["sessions"][-1]["id"],
                        "cleanup_status": "unknown",
                    },
                )
                save_case(output, case)
                raise
        case["stop_reason"] = reason
        if case["assessment"]["assessor"] == "collection-system" and case.get(
            "assessment_attempts"
        ):
            case["assessment"] = {
                **_pending(
                    "Assessment did not validate; preserve individually supported findings and rejected attempts"
                ),
                "findings": retained_findings(case),
            }
        save_case(output, case)
        if all(s["cleanup_status"] == "stopped" for s in case["sessions"]):
            # Keep the seed privately until the caller has chosen whether to branch.
            lease_path = _lease_path(output)
            if lease_path.exists():
                lease = json.loads(lease_path.read_text())
                lease.pop("session_token", None)
                _private_write(lease_path, lease)
        return case


def profile(output, name, decision, *, request=investigation_request):
    if name not in {"desktop", "mobile"}:
        raise ValueError("Choose desktop or mobile")
    with _case(output) as case:
        decision = _decision(case, decision)
        if len(case["sessions"]) >= MAX_PROFILES:
            raise ValueError("Profile budget exhausted")
        lease = json.loads(_lease_path(output).read_text())
    close(
        output,
        "Close current context before the agent-selected profile comparison",
        request=request,
    )
    with _case(output) as case:
        probe = _new_probe(case, "profile", {**decision, "profile": name})
        lease.pop("session_token", None)
        case["unconfirmed_browser_start"] = True
        save_case(output, case)
        try:
            packet = request(
                "start", {"url": lease["url"], "scope": lease["scope"], "profile": name}
            )
            lease["session_token"] = packet.pop("session_token")
            _private_write(_lease_path(output), lease)
            _accept(output, case, probe, packet)
        except Exception as error:
            _failure(output, case, probe, error)
            if lease.get("session_token"):
                _close_after_failure(output, case, lease, request)
            raise
    return case


def finish(
    output, assessment, reason, review_data=None, *, request=investigation_request
):
    """Validate before closing, so a malformed report cannot destroy the context."""
    with _case(output) as case:
        try:
            parsed = Assessment.model_validate(assessment)
            parsed.validate_evidence(case["observations"])
            parsed.validate_context(context_records(case))
        except ValueError as error:
            attempts = case.setdefault("assessment_attempts", [])
            attempts.append(
                {
                    "assessment": sanitize(assessment),
                    "error": str(error)[:2000],
                    "validation": getattr(error, "detail", None),
                }
            )
            case["assessment_attempts"] = attempts[-10:]
            save_case(output, case)
            raise
    if review_data is not None and case["observations"]:
        review(output, review_data)
    # Cleanup is always attempted. Failure is operational evidence, not a verdict veto.
    try:
        close(output, reason, request=request)
    except Exception as error:
        with _case(output) as case:
            case["stop_reason"] = _text(reason, "stop reason")
            case.setdefault("operational_errors", []).append(
                {
                    "operation": "close",
                    "error": type(error).__name__,
                    "diagnostic": sanitize(str(error))[:1000],
                }
            )
            save_case(output, case)
    return assess_case(output, parsed.model_dump())


def assessment_contract(case=None):
    result = {"assessment_schema": assessment_schema()}
    if case is not None:
        result.update(
            valid_evidence_ids=[o["id"] for o in case["observations"]],
            evidence_items={
                o["id"]: o.get("evidence_items", []) for o in case["observations"]
            },
            evidence_coverage={
                o["id"]: evidence_coverage(o) for o in case["observations"]
            },
            valid_context_ids=[r["id"] for r in context_records(case) if "id" in r],
            archive_candidates=archive_candidates(case),
            assessment_status="pending"
            if case["assessment"].get("verdict") is None
            else "complete",
        )
    return result


def status(case):
    latest = case["observations"][-1] if case["observations"] else {}
    latest = {**latest, "visible_text": latest.get("visible_text", "")[:4000]}
    view = {
        **case["browser_view"],
        "choices": [
            {**c, "url": c.get("url", "")[:500]}
            for c in case["browser_view"].get("choices", [])
        ],
    }
    return {
        "case_id": case["case_id"],
        "target_url": case["target_url"],
        "objective": case["objective"],
        "collection": collection_summary(case),
        "browser_cleanup": case.get("browser_cleanup", "unknown"),
        "assessment": case["assessment"]
        if case["assessment"].get("verdict") is not None
        else None,
        "assessment_status": "pending"
        if case["assessment"].get("verdict") is None
        else "complete",
        "assessment_required": bool(case["observations"] or context_records(case)),
        "terminal": bool(case.get("stop_reason")) and not view.get("session_open"),
        "valid_evidence_ids": [o["id"] for o in case["observations"]],
        "evidence_items": latest.get("evidence_items", []),
        "corroboration": case.get("corroboration", []),
        "incident_context": case.get("incident_context", []),
        "context_records": context_records(case),
        "archive_candidates": archive_candidates(case),
        "enrichment_available": True,
        "last_probe": case["probes"][-1] if case["probes"] else None,
        "initial_hypothesis": case.get("initial_hypothesis"),
        "next_operation": (
            "hypothesize"
            if case.get("workflow") == "archive_then_browser"
            and not case.get("initial_hypothesis")
            else ("browse" if not case["probes"] else None)
        ),
        "latest_observation": {
            k: latest.get(k)
            for k in (
                "id",
                "final_url",
                "page_title",
                "visible_text",
                "forms",
                "screenshot",
                "interaction",
                "status",
                "errors",
            )
        },
        "browser": view,
        "latest_review": case["reviews"][-1] if case["reviews"] else None,
        "sessions": case["sessions"],
        "steps_used": len(case["probes"]),
        "step_limit": MAX_INVESTIGATION_STEPS,
    }


def _append_context(case, record):
    records = case.setdefault("corroboration", [])
    if len(records) >= 48:
        raise ValueError("Combined source budget exhausted")
    records.append({**sanitize(record), "id": f"corroboration-{len(records) + 1:03d}"})
    if record.get("status") in {"available", "reported", "fetching"}:
        case["assessment"] = _pending(
            "New source evidence requires researcher assessment"
        )


def archive_candidates(case):
    """Expose recorded page choices; the model selects relevance, not a URL rule."""
    return [
        {
            "source_id": source["id"],
            "capture_id": capture.get("capture_id", f"capture-{i + 1:03d}"),
            **{
                key: capture.get(key)
                for key in (
                    "url",
                    "crawl",
                    "fetch_time",
                    "fetch_status",
                    "content_mime_type",
                )
            },
        }
        for source in context_records(case)
        if source.get("kind") == "archive_index" and source.get("status") == "available"
        for i, capture in enumerate(source.get("captures", []))
    ]


def archive(output, source_id, capture_id, reason, *, fetch_fn=None):
    """Fetch a model-selected archived page; preserve bytes before extracting them."""
    from archive_content import coordinates, extract_content, fetch_record, parse_record

    reason = _text(reason, "archive selection reason")
    with _case(output) as case:
        source = next(
            (
                s
                for s in context_records(case)
                if s.get("id") == source_id
                and s.get("kind") == "archive_index"
                and s.get("status") == "available"
            ),
            None,
        )
        if source is None:
            raise ValueError("Select a recorded Common Crawl index source")
        capture = next(
            (
                c
                for i, c in enumerate(source.get("captures", []))
                if c.get("capture_id", f"capture-{i + 1:03d}") == capture_id
            ),
            None,
        )
        if capture is None:
            raise ValueError("Select an existing archive capture ID")
        prior = [r for r in context_records(case) if r.get("kind") == "archived_page"]
        for page in prior:
            if (
                page["index_source_id"] == source_id
                and page["capture_id"] == capture_id
            ):
                return case  # Reuse the preserved result, including a recorded failure.
        if len(prior) >= MAX_ARCHIVE_PAGES:
            raise ValueError(
                "Archive page budget exhausted; assess the preserved content"
            )
        _append_context(
            case,
            {
                "kind": "archived_page",
                "source": "common_crawl_warc",
                "status": "fetching",
                "verdict_effect": "model_assessed",
                "index_source_id": source_id,
                "capture_id": capture_id,
                "selection_reason": reason,
                "checked_at": utcnow(),
                "url": capture.get("url"),
                "crawl": capture.get("crawl"),
                "index_fetch_time": capture.get("fetch_time"),
                "limitations": [
                    "Historical archive content; current behavior may differ."
                ],
            },
        )
        record = case["corroboration"][-1]
        stem = f"archive-{len(prior) + 1:03d}"
        save_case(output, case)
        try:
            key, offset, length = coordinates(capture)
            raw = (fetch_fn or fetch_record)(capture)
            if len(raw) != length:
                raise ValueError("Archive range length differs from the index")
            archive_file = stem + ".warc.gz"
            (output / archive_file).write_bytes(raw)
            record.update(
                warc_filename=key,
                warc_record_offset=offset,
                warc_record_length=length,
                archive_file=archive_file,
                archive_sha256=digest(raw),
            )
            save_case(output, case)
            payload, metadata = parse_record(raw, capture)
            # Keep original response bytes inert. Do not replace them with redacted text.
            payload_file = stem + "-payload.bin"
            (output / payload_file).write_bytes(payload)
            record.update(
                **metadata, payload_file=payload_file, payload_sha256=digest(payload)
            )
            save_case(output, case)
            content = extract_content(payload, metadata)
            content_file = stem + "-content.json"
            (output / content_file).write_text(json.dumps(content, indent=2) + "\n")
            record.update(
                status="available",
                content_file=content_file,
                content_sha256=digest((output / content_file).read_bytes()),
                content_preview={
                    "title": content["title"],
                    "text": content["text"][:6000],
                    "forms": content["forms"],
                    "links": content["links"][:30],
                    "scripts": [
                        {**s, "inline": s["inline"][:1000]} for s in content["scripts"]
                    ],
                    "extraction_truncated": content["extraction_truncated"],
                    "preview_notice": "Preview only; read content_file for the full retained extraction.",
                },
                limitations=record["limitations"] + content["limitations"],
            )
        except Exception as error:
            record.update(
                status="unavailable",
                error_type=type(error).__name__,
                reason="Selected archive content could not be extracted; any downloaded bytes remain preserved.",
            )
            # Parser errors are fixed diagnostics; network errors may carry provider details.
            if isinstance(error, ValueError):
                record["diagnostic"] = sanitize(str(error))[:1000]
        save_case(output, case)
        return case


def discover(output, reason, *, match="host", crawls=None, lookup_fn=None):
    """Model-selected archive query; keep failed and successful attempts distinct."""
    from dataclasses import replace
    from common_crawl import CrawlConfig, lookup_common_crawl

    with _case(output) as case:
        if sum(r.get("kind") == "archive_index" for r in context_records(case)) >= 8:
            raise ValueError("Archive discovery budget exhausted")
        config = CrawlConfig.from_env()
        if crawls:
            if config is None or not set(crawls) <= set(config.crawls):
                raise ValueError(
                    "Select historical partitions from configured archive coverage"
                )
            config = replace(config, crawls=tuple(crawls))
        lease = json.loads(_lease_path(output).read_text())
        record = (lookup_fn or lookup_common_crawl)(
            lease["url"], config=config, match=match
        )
        _append_context(case, {**record, "request_reason": _text(reason, "reason")})
        save_case(output, case)
        return case


def import_evidence(output, source, reason):
    """Copy verified same-run observations as sourced context, never verdicts."""
    import copy

    output, source = Path(output).resolve(), Path(source).resolve()
    if output == source or output.parent != source.parent:
        raise ValueError(
            "Evidence imports must be from another case in the same run directory"
        )
    verify_case(source)
    original = json.loads((source / CASE_FILE).read_text())
    source_manifest = json.loads((source / "manifest.json").read_text())
    with _case(output) as case:
        if any(
            r.get("source_case_id") == original["case_id"]
            for r in context_records(case)
        ):
            raise ValueError("This case's observations were already imported")
        if (
            len(original["observations"])
            + sum(
                r.get("kind") == "imported_observation" for r in context_records(case)
            )
            > 24
        ):
            raise ValueError("Same-run observation import budget exhausted")
        if len(case.get("corroboration", [])) + len(original["observations"]) > 48:
            raise ValueError("Combined source budget exhausted")
        for observation in original["observations"]:
            validate_inventory(observation)
            retained = copy.deepcopy(observation)
            stem = f"import-{len(case.get('corroboration', [])) + 1:03d}"
            for field in ("screenshot", "dom_snapshot"):
                name = retained.get(field)
                if not name:
                    continue
                if name not in source_manifest["files"] or Path(name).name != name:
                    raise ValueError(
                        "Imported observation file is not in the verified manifest"
                    )
                copied = stem + "-" + name
                (output / copied).write_bytes((source / name).read_bytes())
                retained[field] = copied
            _append_context(
                case,
                {
                    "kind": "imported_observation",
                    "source": "same_run_browser_evidence",
                    "status": "available",
                    "checked_at": utcnow(),
                    "source_case_id": original["case_id"],
                    "source_created_at": original["created_at"],
                    "source_case_sha256": source_manifest["files"][CASE_FILE]["sha256"],
                    "reason": _text(reason, "reason"),
                    "observation": retained,
                },
            )
        save_case(output, case)
        return case


def enrich(output, source, reason, *, lookup_fn=lookup):
    """One model-selected provider lookup for the seed; browser state is preserved."""
    reason = _text(reason, "enrichment reason")
    if source not in SOURCES:
        raise ValueError("Unsupported enrichment source")
    with _case(output) as case:
        if (
            sum(bool(r.get("requested_source")) for r in case.get("corroboration", []))
            >= 10
        ):
            raise ValueError("Corroboration budget exhausted")
        if any(
            r.get("requested_source") == source for r in case.get("corroboration", [])
        ):
            raise ValueError("This source was already queried; use its recorded result")
        lease = json.loads(_lease_path(output).read_text())
        record = lookup_fn(
            source,
            lease["url"],
            api_key=os.environ.get(
                "CYBER_URLHAUS_AUTH_KEY" if source == "urlhaus" else "CYBER_VT_API_KEY"
            ),
        )
        _append_context(case, {**record, "requested_source": source, "reason": reason})
        save_case(output, case)
        return case


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("schema", help="Print the complete assessment JSON Schema")
    for command in ("start", "prepare"):
        p = commands.add_parser(command)
        p.add_argument("url")
        p.add_argument("--objective", required=True)
        p.add_argument("--case", required=True, type=Path)
        p.add_argument("--profile", choices=["desktop", "mobile"], default="desktop")
        p.add_argument(
            "--scope",
            choices=["host", "observed_external"],
            default="observed_external",
        )
        p.add_argument("--incident-context", type=Path)
        p.add_argument("--brand-references", type=Path)
    for command in (
        "review",
        "step",
        "close",
        "profile",
        "status",
        "assess",
        "verify",
        "finish",
        "contract",
        "corroborate",
        "enrich",
        "hypothesize",
        "browse",
        "archive",
        "discover",
        "import-evidence",
    ):
        p = commands.add_parser(command)
        p.add_argument("--case", required=True, type=Path)
        if command == "review":
            p.add_argument("--review", required=True, type=Path)
        if command == "hypothesize":
            p.add_argument("--hypothesis", required=True, type=Path)
        if command in {"step", "profile"}:
            choice = p.add_mutually_exclusive_group(required=True)
            choice.add_argument("--decision", type=Path)
            choice.add_argument("--reason")
        if command == "step":
            p.add_argument(
                "action",
                choices=[
                    "follow",
                    "expand",
                    "root",
                    "back",
                    "scroll",
                    "wait",
                    "navigate",
                    "screenshot",
                ],
            )
            p.add_argument("--candidate-id")
            p.add_argument("--seconds", type=int)
            p.add_argument("--url")
        if command == "profile":
            p.add_argument("name", choices=["desktop", "mobile"])
        if command in {"close", "finish"}:
            p.add_argument("--reason", required=True)
        if command in {"assess", "finish"}:
            p.add_argument("--assessment", required=True, type=Path)
        if command == "finish":
            p.add_argument("--review", type=Path)
        if command == "corroborate":
            p.add_argument("--brand-reference", type=Path)
            p.add_argument("--virustotal-url")
        if command == "enrich":
            p.add_argument("--source", choices=SOURCES, required=True)
            p.add_argument("--reason", required=True)
        if command == "discover":
            p.add_argument("--match", choices=["host", "exact"], default="host")
            p.add_argument("--crawls", help="Comma-separated configured partitions")
            p.add_argument("--reason", required=True)
        if command == "import-evidence":
            p.add_argument("--from-case", required=True, type=Path)
            p.add_argument("--reason", required=True)
        if command == "archive":
            p.add_argument("--source-id", required=True)
            p.add_argument("--capture-id", required=True)
            p.add_argument("--reason", required=True)
    args = parser.parse_args(argv)
    if args.command == "schema":
        print(json.dumps(assessment_contract(), indent=2))
        return 0
    if args.command == "contract":
        with _case(args.case) as case:
            print(json.dumps(assessment_contract(case), indent=2))
        return 0
    if args.command == "corroborate":
        from corroboration import compare_brand, lookup_virustotal
        from research_case import _validate_input

        if not args.brand_reference and not args.virustotal_url:
            raise ValueError(
                "Supply a researcher-verified brand reference or an explicit reputation lookup URL"
            )
        with _case(args.case) as case:
            records = []
            if args.brand_reference:
                records.append(
                    compare_brand(case, json.loads(args.brand_reference.read_text()))
                )
            if args.virustotal_url:
                _validate_input(args.virustotal_url)
                if digest(args.virustotal_url) != case["subject_sha256"]:
                    raise ValueError("Reputation lookup must use the exact case seed")
                records.append(
                    lookup_virustotal(
                        args.virustotal_url, os.environ.get("CYBER_VT_API_KEY")
                    )
                )
            if (
                sum(
                    r.get("kind") != "archived_page"
                    for r in case.get("corroboration", [])
                )
                + len(records)
                > 10
            ):
                raise ValueError("Corroboration budget exhausted")
            for record in records:
                _append_context(case, {"status": "available", **record})
            # A skipped lookup must not erase accepted findings. Context is
            # appended for the next assessment, as in the maintained enrich path.
            save_case(args.case, case)
            print(json.dumps(sanitize(status(case)), indent=2))
        return 0
    if args.command in {"start", "prepare"}:
        result = (prepare if args.command == "prepare" else start)(
            args.case,
            args.url,
            args.objective,
            profile=args.profile,
            scope=args.scope,
            incident_context=(
                json.loads(args.incident_context.read_text())
                if args.incident_context
                else None
            ),
            brand_references=(
                json.loads(args.brand_references.read_text())
                if args.brand_references
                else None
            ),
        )
    elif args.command == "hypothesize":
        result = hypothesize(args.case, json.loads(args.hypothesis.read_text()))
    elif args.command == "browse":
        result = browse(args.case)
    elif args.command == "discover":
        result = discover(
            args.case,
            args.reason,
            match=args.match,
            crawls=args.crawls.split(",") if args.crawls else None,
        )
    elif args.command == "import-evidence":
        result = import_evidence(args.case, args.from_case, args.reason)
    elif args.command == "enrich":
        result = enrich(args.case, args.source, args.reason)
    elif args.command == "archive":
        result = archive(args.case, args.source_id, args.capture_id, args.reason)
    elif args.command == "review":
        result = review(args.case, json.loads(args.review.read_text()))
    elif args.command == "step":
        result = step(
            args.case,
            args.action,
            json.loads(args.decision.read_text()) if args.decision else args.reason,
            candidate_id=args.candidate_id,
            seconds=args.seconds,
            url=args.url,
        )
    elif args.command == "close":
        result = close(args.case, args.reason)
    elif args.command == "profile":
        result = profile(
            args.case,
            args.name,
            json.loads(args.decision.read_text()) if args.decision else args.reason,
        )
    elif args.command == "assess":
        result = assess_case(args.case, json.loads(args.assessment.read_text()))
    elif args.command == "finish":
        result = finish(
            args.case,
            json.loads(args.assessment.read_text()),
            args.reason,
            json.loads(args.review.read_text()) if args.review else None,
        )
    elif args.command == "verify":
        print(json.dumps({"verified_files": verify_case(args.case)}))
        return 0
    else:
        verify_case(args.case)
        result = json.loads((args.case / CASE_FILE).read_text())
    print(json.dumps(sanitize(status(result)), indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, RuntimeError, DestinationRefused) as error:
        print(json.dumps({"error": type(error).__name__, "message": str(error)[:500]}))
        raise SystemExit(1)
