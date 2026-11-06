"""Assemble private B09 V10 evidence without claiming deferred peer success."""

import hashlib
import json
import re

from tests.e2e.cli_uplift.product_onboarding_full import verify_release_evidence
from tests.e2e.new_ui.product_release import private_capture_directory
from tests.e2e.product_onboarding.product_v02_record import private_json
from tests.e2e.product_onboarding.product_v06_evidence import (
    DIGEST, REFERENCE, REVISION, SUMMARY, build_packet as build_surface_packet,
    review_binding as surface_review_binding, decision_binding as surface_decision_binding,
)

SCHEMA = "product-v10/v1"
BOLT_ID = "B09"
CASES = {
    "AC-01": ("SCN-01", "tests/e2e/product_onboarding/test_v10.py::live_cli_full_journeys",
              "Three onboarding starting points and UI resume agree with independent saved Profile and provider reads"),
    "AC-02": ("SCN-02", "tests/e2e/product_onboarding/test_v10.py::live_cli_peer_unavailable",
              "Context and L5 report unavailable while saved Profile and original lineage remain usable with no peer effects"),
    "AC-03": ("SCN-03", "tests/e2e/product_onboarding/test_v10.py::live_cli_release_evidence",
              "Prior access and replay cases pass with matching artifacts safe rollback and run owned cleanup"),
}
CONTROLS = {
    "AC-01": {"idea": "installed_cli", "existing_assets": "installed_cli",
              "running_product": "installed_cli", "ui_resume": "browser",
              "selected_effects": "independent_provider"},
    "AC-02": {"context_unavailable": "api", "l5_unavailable": "api",
              "denied_scope": "api", "cli_resume": "installed_cli",
              "no_peer_effects": "independent_provider"},
    "AC-03": {"foreign_access": "api", "revoked_access": "api",
              "stale_revision": "api", "replayed_request": "api",
              "rollback": "api", "owned_cleanup": "independent_provider"},
}
DEFERRED = {"context": "unavailable", "l5": "unavailable"}


def build_packet(inputs):
    """Keep failed attempts visible and block review until both B09 stories agree."""
    packet = build_surface_packet(inputs, schema=SCHEMA, bolt_id=BOLT_ID, cases_spec=CASES)
    issues = packet["issues"]

    def reference(value):
        return (isinstance(value, str) and REFERENCE.fullmatch(value)
                and ".." not in value and not re.search(r"[0-9]{12}", value))

    def summary(value):
        return (isinstance(value, str) and 0 < len(value) <= 500 and SUMMARY.fullmatch(value)
                and not re.search(r"https?://|arn:aws|[0-9]{7,}|[A-Za-z0-9_-]{24,}", value, re.I))

    supplied = inputs.get("controls") if isinstance(inputs.get("controls"), dict) else {}
    if set(supplied) != set(CONTROLS):
        issues.append("V10 controls need every journey denial recovery and no effect case")
    controls = {}
    used_ids = set()
    for criterion, required in CONTROLS.items():
        rows = supplied.get(criterion) if isinstance(supplied.get(criterion), dict) else {}
        if set(rows) != set(required):
            issues.append(f"{criterion} control selection is incomplete or unexpected")
        controls[criterion] = {}
        for case, surface in required.items():
            provided = rows.get(case)
            if not isinstance(provided, list) or not 1 <= len(provided) <= 20:
                issues.append(f"{criterion}.{case} needs bounded attempt history")
                provided = []
            controls[criterion][case] = []
            for index, attempt in enumerate(provided):
                entry = attempt if isinstance(attempt, dict) else {}
                attempt_id = entry.get("attempt_id")
                if (not isinstance(attempt_id, str) or not re.fullmatch(r"[A-Za-z0-9_/-]{1,100}", attempt_id)
                        or attempt_id in used_ids):
                    issues.append(f"{criterion}.{case} attempt {index} has invalid identity")
                    attempt_id = None
                used_ids.add(attempt_id)
                artifact = entry.get("artifact_ref")
                readback = entry.get("readback_ref")
                if (entry.get("result") != "pass" or entry.get("surface") != surface
                        or entry.get("run_id") != packet["run_id"]
                        or entry.get("source_revision") != packet["source_revision"]
                        or entry.get("requirements_revision") != packet["requirements_revision"]
                        or entry.get("plan_revision") != packet["plan_revision"]
                        or not reference(artifact) or not reference(readback) or artifact == readback
                        or not summary(entry.get("actual"))):
                    issues.append(f"{criterion}.{case} attempt {index} is failed or lacks independent proof")
                controls[criterion][case].append({
                    "attempt_id": attempt_id, "result": entry.get("result") if entry.get("result") in
                    ("pass", "fail", "skip", "not_run", "interrupted") else None,
                    "surface": surface if entry.get("surface") == surface else None,
                    "run_id": packet["run_id"] if entry.get("run_id") == packet["run_id"] else None,
                    "source_revision": packet["source_revision"] if entry.get("source_revision") == packet["source_revision"] else None,
                    "requirements_revision": packet["requirements_revision"] if entry.get("requirements_revision") == packet["requirements_revision"] else None,
                    "plan_revision": packet["plan_revision"] if entry.get("plan_revision") == packet["plan_revision"] else None,
                    "actual": entry.get("actual") if summary(entry.get("actual")) else None,
                    "artifact_ref": artifact if reference(artifact) else None,
                    "readback_ref": readback if reference(readback) else None,
                })
    packet["controls"] = controls
    for criterion, case in packet["cases"].items():
        if any(attempt["result"] != "pass" for attempt in case["attempts"]):
            issues.append(f"{criterion} has a failed skipped or not run required surface attempt")

    supplied_release = inputs.get("release_record")
    try:
        checked = verify_release_evidence(supplied_release)
        if (checked["run_id"] != packet["run_id"]
                or checked["source_revision"] != packet["source_revision"]
                or checked["requirements_revision"] != packet["requirements_revision"]
                or supplied_release["artifacts"]["api_image_digest"] != packet["artifacts"]["api_image_digest"]
                or supplied_release["artifacts"]["ui_manifest_sha256"] != packet["artifacts"]["ui_manifest_sha256"]
                or supplied_release["artifacts"]["cli_hashes"]["adp"] != packet["artifacts"]["cli_sha256"]
                or supplied_release["artifacts"]["schema_head"] != packet["artifacts"]["schema_head"]
                or supplied_release["artifacts"]["workflow_revision"] != packet["artifacts"]["api_workflow_revision"]
                or supplied_release["cleanup"]["receipt_ref"] != packet["cleanup"]["receipt_ref"]
                or supplied_release["observed_release"]["readback_ref"] != packet["deployment"]["observation_ref"]):
            raise ValueError("V10 release differs from the combined packet artifacts and cleanup")
    except (TypeError, ValueError, KeyError):
        issues.append("V10 release attempts or pinned deployment do not match this packet")
        packet["release_record"] = None
    else:
        packet["release_record"] = supplied_release

    companion = inputs.get("v11_contribution")
    if (not isinstance(companion, dict) or set(companion) != {
            "story", "status", "source_revision", "plan_revision", "packet_revision",
            "report_ref", "readback_ref", "peer_state", "cleanup_verified"}
            or companion.get("story") != "V11" or companion.get("status") != "review_ready"
            or companion.get("source_revision") != packet["source_revision"]
            or companion.get("plan_revision") != packet["plan_revision"]
            or not isinstance(companion.get("packet_revision"), str)
            or not re.fullmatch(r"sha256:[a-f0-9]{64}", companion["packet_revision"])
            or not reference(companion.get("report_ref")) or not reference(companion.get("readback_ref"))
            or companion["report_ref"] == companion["readback_ref"]
            or companion.get("peer_state") != DEFERRED
            or companion.get("cleanup_verified") is not True):
        issues.append("B09 requires a separate verified V11 unavailable peer contribution")
        packet["v11_contribution"] = None
    else:
        packet["v11_contribution"] = companion

    packet["peer_state"] = DEFERRED
    packet["deferred_work"] = ["#8266", "#8149", "#8150"]
    packet["status"] = "review_ready" if not issues else "incomplete"
    packet["packet_revision"] = "sha256:" + hashlib.sha256(
        json.dumps({key: value for key, value in packet.items()
                    if key not in ("packet_revision", "review")}, sort_keys=True,
                   separators=(",", ":"), allow_nan=False).encode(),
    ).hexdigest()
    packet["review"].update(packet_revision=packet["packet_revision"],
                            status="pending" if packet["status"] == "review_ready" else "not_ready")
    return packet


def publish_packet(inputs, directory):
    packet = build_packet(inputs)
    if packet["run_id"] is None:
        raise ValueError("V10 packet needs a valid owned run before publication")
    destination = private_capture_directory(directory) / f"{packet['run_id']}-b09-packet.json"
    return private_json(destination, packet), packet


def review_binding(packet):
    """Identify the one pending B09 decision without claiming human acceptance."""
    return surface_review_binding(packet, bolt_id=BOLT_ID, packet_builder=build_packet)


def decision_binding(packet, decisions):
    """Read the recorded ADP decision for this exact B09 plan and packet."""
    return surface_decision_binding(packet, decisions, bolt_id=BOLT_ID,
                                    packet_builder=build_packet)
