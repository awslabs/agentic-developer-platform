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

from browser_client import investigation_request
from browser_guard import DestinationRefused
from case_contract import Assessment, content_digest, digest, sanitize, utcnow
from evidence_items import validate_inventory
from research_case import (
    CASE_FILE,
    _inconclusive,
    assess_case,
    collection_summary,
    new_case,
    save_case,
    verify_case,
)

MAX_INVESTIGATION_STEPS = 24
MAX_PROFILES = 2


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
    case["assessment"] = _inconclusive("New evidence requires researcher assessment")
    return probe


def _accept(output, case, probe, packet):
    if packet.get("schema_version") != "domain-investigation/1":
        raise ValueError("Incompatible investigation response")
    manifest = packet["manifest"]
    if manifest.get("cleanup_status") not in {"open", "stopped", "failed"}:
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
            not raw.get("screenshot_base64")
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
    if status != "stopped":
        for o in case["observations"]:
            if o["session_id"] == sid:
                o["status"] = "partial"
                o["errors"].append("session_cleanup_unconfirmed")
        case["assessment"] = _inconclusive("Browser cleanup was not confirmed")
    case["browser_view"]["session_open"] = False


def _failure(output, case, probe, error):
    probe.update(status="failed", completed_at=utcnow(), error=type(error).__name__)
    probe["diagnostic"] = sanitize(str(error))[:1000]
    if hasattr(error, "reason_code"):
        probe["reason_code"] = error.reason_code
    case["assessment"] = _inconclusive(f"{probe['id']} failed; retain earlier evidence")
    if not case["observations"]:
        case["stop_reason"] = (
            "Initial collection failed; no observation exists to assess"
        )
        case["assessment"] = _inconclusive(
            f"No page evidence collected ({probe.get('reason_code', probe['error'])}). "
            "Unavailable or blocked collection does not establish safety or maliciousness."
        )
    save_case(output, case)


def start(
    output,
    url,
    objective,
    *,
    profile="desktop",
    scope="host",
    request=investigation_request,
):
    objective = _text(objective, "objective")
    case = new_case(output, url)
    case.update(
        case_kind="domain_investigation",
        objective=objective,
        scope=scope,
        sessions=[],
        reviews=[],
        browser_view={"session_open": False},
    )
    probe = _new_probe(
        case,
        "start",
        {
            "question": objective,
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
    if (
        not case["reviews"]
        or case["reviews"][-1]["probe_id"] != case["probes"][-1]["id"]
    ):
        raise ValueError("Review the latest evidence before selecting the next action")
    if not isinstance(value, dict) or set(value) != {
        "question",
        "reason",
        "expected_signal",
        "evidence_ids",
    }:
        raise ValueError(
            "Decision requires question, reason, expected_signal and evidence_ids"
        )
    return {
        **{k: _text(value[k], k) for k in ("question", "reason", "expected_signal")},
        "evidence_ids": _citations(case, value["evidence_ids"]),
    }


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
        parsed = Assessment.model_validate(assessment)
        parsed.validate_evidence(case["observations"])
        if (
            case["observations"]
            and review_data is None
            and (
                not case["reviews"]
                or case["reviews"][-1]["probe_id"] != case["probes"][-1]["id"]
            )
        ):
            raise ValueError("Review the latest observation before finishing")
    if review_data is not None and case["observations"]:
        review(output, review_data)
    close(output, reason, request=request)
    return assess_case(output, parsed.model_dump())


def assessment_contract(case=None):
    result = {"assessment_schema": Assessment.model_json_schema()}
    if case is not None:
        result.update(
            valid_evidence_ids=[o["id"] for o in case["observations"]],
            evidence_items={
                o["id"]: o.get("evidence_items", []) for o in case["observations"]
            },
            empty_evidence_assessment=_inconclusive(
                "No page observations were captured"
            ),
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
        "objective": case["objective"],
        "collection": collection_summary(case),
        "assessment": case["assessment"],
        "assessment_required": bool(case["observations"]),
        "terminal": bool(case.get("stop_reason")) and not view.get("session_open"),
        "valid_evidence_ids": [o["id"] for o in case["observations"]],
        "evidence_items": latest.get("evidence_items", []),
        "corroboration": case.get("corroboration", []),
        "last_probe": case["probes"][-1],
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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("schema", help="Print the complete assessment JSON Schema")
    p = commands.add_parser("start")
    p.add_argument("url")
    p.add_argument("--objective", required=True)
    p.add_argument("--case", required=True, type=Path)
    p.add_argument("--profile", choices=["desktop", "mobile"], default="desktop")
    p.add_argument("--scope", choices=["host", "observed_external"], default="host")
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
    ):
        p = commands.add_parser(command)
        p.add_argument("--case", required=True, type=Path)
        if command == "review":
            p.add_argument("--review", required=True, type=Path)
        if command in {"step", "profile"}:
            p.add_argument("--decision", required=True, type=Path)
        if command == "step":
            p.add_argument(
                "action", choices=["follow", "expand", "root", "back", "scroll", "wait"]
            )
            p.add_argument("--candidate-id")
            p.add_argument("--seconds", type=int)
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
            if len(case.get("corroboration", [])) + len(records) > 10:
                raise ValueError("Corroboration budget exhausted")
            case.setdefault("corroboration", []).extend(records)
            case["assessment"] = _inconclusive(
                "Review new corroboration alongside the captured evidence"
            )
            save_case(args.case, case)
            print(json.dumps(sanitize(status(case)), indent=2))
        return 0
    if args.command == "start":
        result = start(
            args.case, args.url, args.objective, profile=args.profile, scope=args.scope
        )
    elif args.command == "review":
        result = review(args.case, json.loads(args.review.read_text()))
    elif args.command == "step":
        result = step(
            args.case,
            args.action,
            json.loads(args.decision.read_text()),
            candidate_id=args.candidate_id,
            seconds=args.seconds,
        )
    elif args.command == "close":
        result = close(args.case, args.reason)
    elif args.command == "profile":
        result = profile(args.case, args.name, json.loads(args.decision.read_text()))
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
