"""Fail-closed B07 packet for deployed Product handoff and repository acceptance."""

import hashlib
import json
import re

from tests.e2e.product_onboarding.product_v06_evidence import (
    DECISION_PROCEDURE,
    REFERENCE,
    REVISION,
    decision_binding as surface_decision_binding,
    review_binding as surface_review_binding,
    build_packet as build_surface_packet,
)

SCHEMA = "product-v08/v1"
BOLT_ID = "B07"
CASES = {
    "AC-01": ("SCN-01", "tests/e2e/product_onboarding/test_v08.py::live_admin_handoff",
              "Admin completion resumes the original scoped session with an observed callback and no exposed credentials"),
    "AC-02": ("SCN-02", "tests/e2e/product_onboarding/test_v08.py::live_repository_recovery",
              "Confirmed creation and reconciliation leave one protected repository with the planned visibility"),
    "AC-03": ("SCN-03", "tests/e2e/product_onboarding/test_v08.py::live_provider_cleanup",
              "Adoption preserves the existing provider baseline and deletes only the run owned repository"),
}
CONTROLS = {
    "AC-01": ("admin_effect", "wrong_scope"),
    "AC-02": ("lost_response", "duplicate_callback", "stale_callback", "wrong_scope"),
    "AC-03": ("adoption", "unlink", "owned_cleanup"),
}
PRODUCER_IDS = {"gateway_handoff", "github_auth_broker", "webhook_ingress"}
OUTCOMES = {"pass", "fail", "skip", "not_run", "interrupted"}


def build_packet(inputs):
    """Keep every attempt, but only completed independently observed work is review-ready."""
    packet = build_surface_packet(inputs, schema=SCHEMA, bolt_id=BOLT_ID, cases_spec=CASES)
    issues = packet["issues"]

    def reference(value, label):
        if (not isinstance(value, str) or not REFERENCE.fullmatch(value)
                or ".." in value or re.search(r"[0-9]{12}", value)):
            issues.append(f"{label} needs a protected reference")
            return None
        return value

    supplied_controls = inputs.get("controls") if isinstance(inputs.get("controls"), dict) else {}
    if set(supplied_controls) != set(CONTROLS):
        issues.append("V08 control inventory is incomplete or unexpected")
    controls = {}
    for criterion, required in CONTROLS.items():
        supplied = supplied_controls.get(criterion)
        supplied = supplied if isinstance(supplied, dict) else {}
        if set(supplied) != set(required):
            issues.append(f"{criterion} is missing a denial, recovery or cleanup control")
        controls[criterion] = {}
        for name in required:
            entries = supplied.get(name)
            if not isinstance(entries, list) or not 1 <= len(entries) <= 20:
                issues.append(f"{criterion}.{name} has no bounded attempt history")
                entries = []
            attempts = []
            seen = set()
            for index, attempt in enumerate(entries):
                entry = attempt if isinstance(attempt, dict) else {}
                attempt_id = entry.get("attempt_id")
                if (not isinstance(attempt_id, str) or not re.fullmatch(r"[A-Za-z0-9_/-]{1,100}", attempt_id)
                        or attempt_id in seen):
                    issues.append(f"{criterion}.{name} attempt {index} has invalid identity")
                    attempt_id = None
                seen.add(attempt_id)
                result = entry.get("result")
                if not isinstance(result, str) or result not in OUTCOMES:
                    issues.append(f"{criterion}.{name} attempt {index} has invalid result")
                    result = None
                artifact = reference(entry.get("artifact_ref"), f"{criterion}.{name} attempt {index} artifact")
                readback = reference(entry.get("readback_ref"), f"{criterion}.{name} attempt {index} readback")
                if artifact == readback:
                    issues.append(f"{criterion}.{name} attempt {index} needs independent readback")
                if (entry.get("run_id") != packet["run_id"]
                        or entry.get("source_revision") != packet["source_revision"]
                        or entry.get("requirements_revision") != packet["requirements_revision"]):
                    issues.append(f"{criterion}.{name} attempt {index} differs from the pinned run")
                attempts.append({"attempt_id": attempt_id, "result": result, "artifact_ref": artifact,
                                 "readback_ref": readback,
                                 "run_id": packet["run_id"] if entry.get("run_id") == packet["run_id"] else None,
                                 "source_revision": packet["source_revision"] if entry.get("source_revision") == packet["source_revision"] else None,
                                 "requirements_revision": packet["requirements_revision"] if entry.get("requirements_revision") == packet["requirements_revision"] else None})
            if not attempts or attempts[-1]["result"] != "pass":
                issues.append(f"{criterion}.{name} has no final passing attempt")
            controls[criterion][name] = attempts

    observed_producers = inputs.get("deployed_producers")
    selected_producers = inputs.get("producers")
    if (not isinstance(observed_producers, dict) or not isinstance(selected_producers, dict)
            or "gateway_handoff" not in observed_producers
            or set(observed_producers) != set(selected_producers)
            or not set(observed_producers) <= PRODUCER_IDS):
        issues.append("Producer selection differs from the independently observed deployment")
        observed_producers, selected_producers = {}, {}
    producers = {}
    deployments = {}
    for name, receipt in selected_producers.items():
        deployment = observed_producers[name]
        if not isinstance(receipt, dict) or not isinstance(deployment, dict):
            issues.append(f"{name} producer receipt or deployment is unavailable")
            continue
        source = receipt.get("source_revision")
        workflow = receipt.get("workflow_revision")
        if (source != packet["source_revision"] or not isinstance(workflow, str)
                or not REVISION.fullmatch(workflow) or deployment.get("source_revision") != source
                or deployment.get("workflow_revision") != workflow):
            issues.append(f"{name} producer differs from the pinned consumer or deployed workflow")
        receipt_ref = reference(receipt.get("receipt_ref"), f"{name} producer receipt")
        observed_ref = reference(deployment.get("observation_ref"), f"{name} producer observation")
        if receipt_ref == observed_ref:
            issues.append(f"{name} producer needs a separate deployed observation")
        deployments[name] = {"source_revision": source if deployment.get("source_revision") == source else None,
                             "workflow_revision": workflow if deployment.get("workflow_revision") == workflow else None,
                             "observation_ref": observed_ref}
        producers[name] = {"source_revision": source if source == packet["source_revision"] else None,
                           "workflow_revision": workflow if isinstance(workflow, str) and REVISION.fullmatch(workflow) else None,
                           "receipt_ref": receipt_ref, "observation_ref": observed_ref}

    packet["controls"] = controls
    packet["producers"] = producers
    packet["deployed_producers"] = deployments
    packet["issues"] = issues
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


def review_binding(packet):
    """Locate the exact B07 packet awaiting the existing human review."""
    return surface_review_binding(packet, bolt_id=BOLT_ID, packet_builder=build_packet)


def decision_binding(packet, decisions):
    """Read an existing ADP decision without making or inferring one."""
    return surface_decision_binding(packet, decisions, bolt_id=BOLT_ID, packet_builder=build_packet)
