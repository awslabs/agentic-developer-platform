"""Synthetic B04 packet fixtures; not deployed evidence."""

from copy import deepcopy

import pytest

from tests.e2e.product_onboarding.product_v05_evidence import (
    ARTIFACTS, BOLT_ID, CASE_DIRECTIONS, DECISION_PROCEDURE, SURFACES,
    build_packet, decision_binding, review_binding,
)


def complete_inputs():
    source, requirements, plan, workflow = ("a" * 40, "b" * 40, "c" * 40, "d" * 40)
    run_id = "product-123-2"
    artifacts = {name: workflow for name in ARTIFACTS}
    artifacts.update({"api_image_digest": "e" * 64, "ui_manifest_sha256": "f" * 64,
                      "cli_sha256": "0" * 64, "schema_head": "product_v05"})
    deployment = {**artifacts, "source_revision": source, "product_onboarding_enabled": True,
                  "receipt_ref": "receipts/deployment", "observation_ref": "evidence/deployment"}
    surfaces = {
        surface: {"run_id": run_id, "status": "pass", "workflow_run_id": 123,
                  "workflow_attempt": 2, "source_revision": source, "workflow_revision": workflow,
                  "report_ref": f"evidence/{surface}-report"}
        for surface in SURFACES
    }
    cases, cleanup = {}, {}
    for criterion, (scenario, selector_name, directions) in CASE_DIRECTIONS.items():
        attempts = []
        for direction, selector in directions.items():
            name = selector.rsplit("::", 1)[-1]
            cleanup[name] = {"receipt_ref": f"receipts/{name}", "owned_absent": True,
                             "shared_unchanged": True}
            for surface in SURFACES:
                attempts.append({
                    "attempt_id": f"{criterion}/{direction}/{surface}/2", "run_id": run_id,
                    "surface": surface, "direction": direction, "test_ref": selector, "result": "pass",
                    "source_revision": source, "requirements_revision": requirements,
                    "workflow_run_id": 123, "workflow_attempt": 2,
                    "artifact_ref": f"evidence/{criterion.lower()}/{direction}/{surface}-artifact",
                    "readback_ref": f"evidence/{criterion.lower()}/{direction}/{surface}-readback",
                    "expected": "Saved state and progress match", "actual": "Observed state and progress match",
                    **({"screenshot_ref": f"evidence/{criterion.lower()}/{direction}/screenshot",
                        "trace_ref": f"evidence/{criterion.lower()}/{direction}/trace"} if surface == "browser" else {}),
                    **({"command": "adp onboard json", "stdout_ref": f"evidence/{criterion.lower()}/{direction}/stdout",
                        "stderr_ref": f"evidence/{criterion.lower()}/{direction}/stderr",
                        "installed_cli_sha256": artifacts["cli_sha256"], "exit_code": 0} if surface == "cli" else {}),
                })
        cases[criterion] = {"scenario_id": scenario,
                            "test_ref": "tests/e2e/product_onboarding/test_v05.py::" + selector_name,
                            "expected": "All reviewed directions match", "actual": "All observed directions match",
                            "attempts": attempts}
    return {"run_id": run_id, "source_revision": source, "requirements_revision": requirements,
            "plan_revision": plan, "artifacts": artifacts, "deployment": deployment,
            "surfaces": surfaces, "cases": cases,
            "cleanup": {"run_id": run_id, "status": "verified", "owned_absent": True,
                        "original_projects_preserved": True, "reused_assets_preserved": True,
                        "receipt_ref": "receipts/cleanup", "cases": cleanup}}


def test_complete_synthetic_packet_preserves_attempts_and_revision():
    inputs = complete_inputs()
    prior = deepcopy(inputs["cases"]["AC-03"]["attempts"][0])
    prior.update(attempt_id="AC-03/recovery/browser/1", result="interrupted")
    inputs["cases"]["AC-03"]["attempts"].insert(0, prior)
    packet = build_packet(inputs)
    assert packet["status"] == "review_ready" and packet["issues"] == []
    assert packet["packet_revision"].startswith("sha256:")
    assert review_binding(packet) == packet["review"]
    assert packet["review"] == {
        "ac_id": "AC-04", "scenario_id": "SCN-04", "bolt_id": BOLT_ID,
        "procedure_ref": DECISION_PROCEDURE, "plan_revision": inputs["plan_revision"],
        "packet_revision": packet["packet_revision"], "status": "pending",
    }
    assert [item["result"] for item in packet["cases"]["AC-03"]["attempts"][:2]] == ["interrupted", "pass"]
    assert len(packet["cases"]["AC-01"]["attempts"]) == 6
    assert build_packet(inputs) == packet
    inputs["cases"]["AC-01"]["attempts"][0]["actual"] = "A different observation"
    assert build_packet(inputs)["packet_revision"] != packet["packet_revision"]


@pytest.mark.parametrize("change", [
    lambda data: data["cases"]["AC-01"]["attempts"].pop(),
    lambda data: data["cases"]["AC-02"]["attempts"][0].update(result="skip"),
    lambda data: data["cases"]["AC-03"]["attempts"][0].update(readback_ref=data["cases"]["AC-03"]["attempts"][0]["artifact_ref"]),
    lambda data: data["cases"]["AC-01"]["attempts"][0].update(requirements_revision="c" * 40),
    lambda data: data["cases"]["AC-01"]["attempts"][0].update(test_ref="tests/unknown.py::test_unknown"),
    lambda data: data["cases"]["AC-01"]["attempts"][0].update(screenshot_ref=None),
    lambda data: data["cases"]["AC-01"]["attempts"][1].update(exit_code=1),
    lambda data: data["surfaces"]["api"].update(status="fail"),
    lambda data: data["deployment"].update(cli_sha256="1" * 64),
    lambda data: data["cleanup"]["cases"]["test_resume_recovery"].update(owned_absent=False),
    lambda data: data["cleanup"].update(original_projects_preserved=False),
    lambda data: data["cases"]["AC-02"].update(actual="https://private.example/test"),
])
def test_incomplete_or_unsafe_inputs_cannot_be_review_ready(change):
    inputs = complete_inputs()
    change(inputs)
    assert build_packet(inputs)["status"] == "incomplete"


def test_unsafe_values_are_not_copied_to_public_packet():
    inputs = complete_inputs()
    raw = inputs["cases"]["AC-01"]["attempts"][1]
    raw["exit_code"] = {"private": "private-marker"}
    raw["command"] = "adp onboard --token private-marker"
    raw["artifact_ref"] = "evidence/../private-marker"
    raw["unexpected"] = "private-marker"
    packet = build_packet(inputs)
    assert packet["status"] == "incomplete"
    assert "private-marker" not in str(packet)
    assert packet["cases"]["AC-01"]["attempts"][1]["exit_code"] is None


def test_missing_surface_and_duplicate_attempt_are_rejected():
    inputs = complete_inputs()
    inputs["surfaces"].pop("api")
    attempts = inputs["cases"]["AC-01"]["attempts"]
    attempts[1]["attempt_id"] = attempts[0]["attempt_id"]
    assert build_packet(inputs)["status"] == "incomplete"


def test_only_one_authoritative_decision_binds_to_current_packet():
    packet = build_packet(complete_inputs())
    previous = {"bolt_id": BOLT_ID, "plan_revision": packet["plan_revision"],
                "packet_revision": "sha256:" + "1" * 64, "decision": "reject",
                "decision_ref": "receipts/previous-review"}
    current = {**previous, "packet_revision": packet["packet_revision"],
               "decision": "accept", "decision_ref": "receipts/current-review"}
    assert decision_binding(packet, [previous, current]) == current
    assert packet["review"]["status"] == "pending"
    assert "decision" not in packet


@pytest.mark.parametrize("fault", [
    "no_decision", "stale_packet", "stale_plan", "wrong_bolt", "duplicate",
    "previous_acceptance", "reused_receipt", "missing_receipt", "leaked_receipt", "unreviewed_case",
    "changed_attempt", "changed_plan", "forged_review", "forged_revision", "injected_status",
])
def test_decision_contract_rejects_missing_stale_duplicate_or_forged_bindings(fault):
    packet = build_packet(complete_inputs())
    decision = {"bolt_id": BOLT_ID, "plan_revision": packet["plan_revision"],
                "packet_revision": packet["packet_revision"], "decision": "reject",
                "decision_ref": "receipts/current-review"}
    records = [decision]
    if fault == "no_decision":
        records = []
    elif fault == "stale_packet":
        decision["packet_revision"] = "sha256:" + "1" * 64
    elif fault == "stale_plan":
        decision["plan_revision"] = "0" * 40
    elif fault == "wrong_bolt":
        decision["bolt_id"] = "B03"
    elif fault == "duplicate":
        records.append(deepcopy(decision))
    elif fault == "previous_acceptance":
        records.insert(0, {**decision, "packet_revision": "sha256:" + "1" * 64, "decision": "accept"})
    elif fault == "reused_receipt":
        records.insert(0, {**decision, "packet_revision": "sha256:" + "1" * 64})
    elif fault == "missing_receipt":
        decision["decision_ref"] = ""
    elif fault == "leaked_receipt":
        decision["decision_ref"] = "https://private.example/decision"
    elif fault == "unreviewed_case":
        packet["cases"]["AC-01"]["attempts"][0]["result"] = "fail"
    elif fault == "changed_attempt":
        packet["cases"]["AC-01"]["attempts"][0]["actual"] = "Edited observation"
    elif fault == "changed_plan":
        packet["plan_revision"] = "0" * 40
    elif fault == "forged_review":
        packet["review"]["status"] = "accepted"
    elif fault == "forged_revision":
        packet["packet_revision"] = "sha256:" + "0" * 64
    elif fault == "injected_status":
        packet["issues"] = []
        packet["status"] = "review_ready"
        packet["decision"] = "accept"
    with pytest.raises(ValueError, match="B04|Incomplete"):
        decision_binding(packet, records)


def test_incomplete_packet_has_no_pending_review_and_cannot_accept_decision():
    inputs = complete_inputs()
    inputs["cleanup"]["owned_absent"] = False
    packet = build_packet(inputs)
    assert packet["review"]["status"] == "not_ready"
    with pytest.raises(ValueError, match="Incomplete"):
        review_binding(packet)
