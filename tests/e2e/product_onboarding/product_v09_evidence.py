"""Build a revision-pinned B08 financial packet without inventing live results."""

import hashlib
import json
import re

from tests.e2e.product_onboarding.product_v06_evidence import (
    DECISION_PROCEDURE, REFERENCE, SUMMARY, build_packet as build_surface_packet,
    review_binding as surface_review_binding, decision_binding as surface_decision_binding,
)
from tests.e2e.new_ui.product_release import private_capture_directory
from tests.e2e.product_onboarding.product_v02_record import private_json

SCHEMA = "product-v09/v1"
BOLT_ID = "B08"
CASES = {
    "AC-01": ("SCN-01", "tests/e2e/product_onboarding/test_v09.py::live_financial_selective_import",
              "Only selected source date changes while the 75 USD plan and pending proposals persist"),
    "AC-02": ("SCN-02", "tests/e2e/product_onboarding/test_v09.py::live_financial_denials",
              "Stale expired revoked and invalid period inputs preserve values without financial effects"),
    "AC-03": ("SCN-03", "tests/e2e/product_onboarding/test_v09.py::live_financial_resume",
              "Later and NA arithmetic reconcile and lost confirmation resumes once with owned cleanup"),
}
CONTROLS = {
    "AC-01": {"browser_date": ("browser", None), "cli_date": ("installed_cli", None),
              "manual_provenance": ("api", None), "pending_suggestions": ("api", None)},
    "AC-02": {"stale": ("browser", 412), "expired": ("installed_cli", 410),
              "revoked": ("api", 403), "invalid_period": ("browser", 422),
              "no_funding_fx_actuals": ("independent_provider", None)},
    "AC-03": {"later_na": ("browser", None), "lost_response": ("browser", None),
              "single_receipt": ("api", None), "source_unchanged": ("independent_provider", None),
              "owned_cleanup": ("independent_provider", None)},
}


def build_packet(inputs):
    """Preserve attempt history and refuse missing or mismatched financial controls."""
    packet = build_surface_packet(inputs, schema=SCHEMA, bolt_id=BOLT_ID, cases_spec=CASES)
    issues = packet["issues"]
    supplied = inputs.get("controls") if isinstance(inputs.get("controls"), dict) else {}
    if set(supplied) != set(CONTROLS):
        issues.append("Financial control inventory is incomplete or unexpected")
    controls = {}
    used_attempts = set()
    for criterion, required in CONTROLS.items():
        entries = supplied.get(criterion) if isinstance(supplied.get(criterion), dict) else {}
        if set(entries) != set(required):
            issues.append(f"{criterion} is missing a required financial or recovery control")
        controls[criterion] = {}
        for name, (surface, status) in required.items():
            provided = entries.get(name)
            if not isinstance(provided, list) or not 1 <= len(provided) <= 20:
                issues.append(f"{criterion}.{name} needs bounded attempt history")
                provided = []
            attempts = []
            for index, supplied_attempt in enumerate(provided):
                entry = supplied_attempt if isinstance(supplied_attempt, dict) else {}
                attempt_id = entry.get("attempt_id")
                if (not isinstance(attempt_id, str) or not re.fullmatch(r"[A-Za-z0-9_/-]{1,100}", attempt_id)
                        or attempt_id in used_attempts):
                    issues.append(f"{criterion}.{name} attempt {index} has invalid or reused identity")
                    attempt_id = None
                used_attempts.add(attempt_id)
                result = entry.get("result")
                if result not in ("pass", "fail", "skip", "not_run", "interrupted"):
                    issues.append(f"{criterion}.{name} attempt {index} has invalid result")
                    result = None
                refs = {}
                for field in ("artifact_ref", "readback_ref"):
                    value = entry.get(field)
                    if (not isinstance(value, str) or not REFERENCE.fullmatch(value)
                            or ".." in value or re.search(r"[0-9]{12}", value)):
                        issues.append(f"{criterion}.{name} attempt {index} needs protected {field}")
                        value = None
                    refs[field] = value
                if refs["artifact_ref"] == refs["readback_ref"]:
                    issues.append(f"{criterion}.{name} attempt {index} needs independent readback")
                actual = entry.get("actual")
                if (not isinstance(actual, str) or not actual or len(actual) > 500
                        or not SUMMARY.fullmatch(actual)
                        or re.search(r"\b[0-9]{7,}\b|[A-Za-z0-9_-]{24,}", actual)):
                    issues.append(f"{criterion}.{name} attempt {index} needs a sanitized observation")
                    actual = None
                if (entry.get("surface") != surface or entry.get("http_status") != status
                        or entry.get("run_id") != packet["run_id"]
                        or entry.get("source_revision") != packet["source_revision"]
                        or entry.get("requirements_revision") != packet["requirements_revision"]
                        or entry.get("plan_revision") != packet["plan_revision"]):
                    issues.append(f"{criterion}.{name} attempt {index} differs from the pinned run or expected denial")
                exit_code = entry.get("exit_code")
                if surface == "installed_cli" and (type(exit_code) is not int or (exit_code <= 0 if status else exit_code != 0)):
                    issues.append(f"{criterion}.{name} CLI attempt lacks the expected exit code")
                attempts.append({"attempt_id": attempt_id, "result": result, "surface": surface if entry.get("surface") == surface else None,
                                 "http_status": status if entry.get("http_status") == status else None,
                                 "exit_code": exit_code if surface == "installed_cli" and type(exit_code) is int else None,
                                 "actual": actual, **refs,
                                 **{field: packet[field] if entry.get(field) == packet[field] else None
                                    for field in ("run_id", "source_revision", "requirements_revision", "plan_revision")}})
            if not attempts or attempts[-1]["result"] != "pass":
                issues.append(f"{criterion}.{name} has no final passing attempt")
            controls[criterion][name] = attempts
    packet["controls"] = controls
    packet["status"] = "review_ready" if not issues else "incomplete"
    packet.pop("packet_revision")
    packet.pop("review")
    packet["packet_revision"] = "sha256:" + hashlib.sha256(
        json.dumps(packet, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(),
    ).hexdigest()
    packet["review"] = {"ac_id": "AC-04", "scenario_id": "SCN-04", "bolt_id": BOLT_ID,
                        "procedure_ref": DECISION_PROCEDURE, "plan_revision": packet["plan_revision"],
                        "packet_revision": packet["packet_revision"],
                        "status": "pending" if packet["status"] == "review_ready" else "not_ready"}
    return packet


def publish_packet(inputs, directory):
    """Retain the full sanitized attempt map in a private run-owned artifact."""
    packet = build_packet(inputs)
    if packet["run_id"] is None:
        raise ValueError("A packet needs a valid run identity before publication")
    destination = private_capture_directory(directory) / f"{packet['run_id']}-financial-packet.json"
    return private_json(destination, packet), packet


def review_binding(packet):
    """Identify the exact B08 packet eligible for the existing human procedure."""
    return surface_review_binding(packet, bolt_id=BOLT_ID, packet_builder=build_packet)


def decision_binding(packet, decisions):
    """Read an authoritative ADP accept or reject; never create one."""
    return surface_decision_binding(packet, decisions, bolt_id=BOLT_ID, packet_builder=build_packet)
