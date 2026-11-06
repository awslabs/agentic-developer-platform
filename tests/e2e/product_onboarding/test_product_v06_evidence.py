"""Synthetic B05 evidence fixtures; no deployed result is claimed."""

from copy import deepcopy
from pathlib import Path

import pytest

from tests.e2e.product_onboarding.product_v06_evidence import (
    ARTIFACTS, BOLT_ID, CASES, DECISION_PROCEDURE, SURFACES, build_packet, decision_binding, review_binding,
)


def complete_inputs():
    source, requirements, plan, workflow = ("a" * 40, "b" * 40, "c" * 40, "d" * 40)
    run_id = "example-run"
    artifacts = {field: workflow for field in ARTIFACTS}
    artifacts.update({"api_image_digest": "e" * 64, "ui_manifest_sha256": "f" * 64,
                      "cli_sha256": "0" * 64, "schema_head": "product_v06"})
    deployment = {**artifacts, "source_revision": source,
                  "receipt_ref": "receipts/deployment", "observation_ref": "evidence/deployment-readback"}
    cases = {}
    for criterion, (scenario, selector, _) in CASES.items():
        attempts = []
        for surface in SURFACES:
            attempt = {
                "attempt_id": f"{criterion}/{surface}/1", "run_id": run_id, "surface": surface,
                "test_ref": selector, "result": "pass", "source_revision": source,
                "requirements_revision": requirements, "workflow_run_id": 123,
                "workflow_attempt": 1, "artifact_ref": f"evidence/{criterion.lower()}/{surface}-artifact",
                "readback_ref": f"evidence/{criterion.lower()}/{surface}-readback",
                "actual": "Observed expected source behavior",
            }
            if surface == "browser":
                attempt.update(screenshot_ref=f"evidence/{criterion.lower()}/screenshot",
                               trace_ref=f"evidence/{criterion.lower()}/trace")
            if surface == "installed_cli":
                attempt.update(command="adp product sources --json", stdout_ref=f"evidence/{criterion.lower()}/stdout",
                               stderr_ref=f"evidence/{criterion.lower()}/stderr", installed_cli_sha256=artifacts["cli_sha256"],
                               exit_code=0)
            attempts.append(attempt)
        cases[criterion] = {"scenario_id": scenario, "test_ref": selector, "actual": "All source checks passed",
                            "report_ref": f"evidence/{criterion.lower()}/scenario-report", "attempts": attempts}
    reports = {surface: {"report_ref": f"evidence/{surface}-report", "run_id": run_id,
                         "source_revision": source, "status": "pass", "workflow_run_id": 123,
                         "workflow_revision": artifacts[{"api": "api_workflow_revision", "browser": "browser_workflow_revision",
                                                         "installed_cli": "cli_workflow_revision"}.get(surface, "api_workflow_revision")],
                         "workflow_attempt": 1} for surface in SURFACES}
    cleanup = {"run_id": run_id, "status": "verified", "owned_absent": True,
               "reused_assets_preserved": True, "receipt_ref": "receipts/cleanup",
               "cases": {criterion: {"receipt_ref": f"receipts/{criterion.lower()}/cleanup", "owned_absent": True,
                                     "shared_unchanged": True} for criterion in CASES}}
    return {"run_id": run_id, "source_revision": source, "requirements_revision": requirements,
            "plan_revision": plan, "artifacts": artifacts, "deployment": deployment,
            "surfaces": reports, "cases": cases, "cleanup": cleanup}


def test_complete_synthetic_packet_pins_every_surface_and_attempt():
    inputs = complete_inputs()
    packet = build_packet(inputs)
    assert packet["status"] == "review_ready" and packet["issues"] == []
    assert packet["bolt_id"] == BOLT_ID and packet["packet_revision"].startswith("sha256:")
    assert set(packet["cases"]) == set(CASES)
    for criterion, (scenario, selector, expected) in CASES.items():
        record = packet["cases"][criterion]
        assert record["scenario_id"] == scenario and record["test_ref"] == selector
        assert record["expected"] == expected
        assert {attempt["surface"] for attempt in record["attempts"]} == set(SURFACES)
        assert all(attempt["expected"] == expected for attempt in record["attempts"])
    assert packet["deployment"]["source_revision"] == inputs["source_revision"]
    assert build_packet(inputs) == packet
    inputs["cases"]["AC-01"]["attempts"][0]["actual"] = "Different observed result"
    assert build_packet(inputs)["packet_revision"] != packet["packet_revision"]


def test_prior_failed_attempt_is_preserved_but_final_success_is_required():
    inputs = complete_inputs()
    first = deepcopy(inputs["cases"]["AC-03"]["attempts"][0])
    first.update(attempt_id="AC-03/api/0", result="interrupted", actual="Response lost before readback")
    inputs["cases"]["AC-03"]["attempts"].insert(0, first)
    packet = build_packet(inputs)
    assert packet["status"] == "review_ready"
    assert [attempt["result"] for attempt in packet["cases"]["AC-03"]["attempts"][:2]] == ["interrupted", "pass"]
    inputs["cases"]["AC-03"]["attempts"][1]["result"] = "not_run"
    assert build_packet(inputs)["status"] == "incomplete"


@pytest.mark.parametrize("fault", ("missing", "shared", "failed", "other_run"))
def test_separate_surface_reports_must_match_the_pinned_run(fault):
    inputs = complete_inputs()
    reports = inputs["surfaces"]
    if fault == "missing":
        del reports["browser"]
    elif fault == "shared":
        reports["installed_cli"]["report_ref"] = reports["browser"]["report_ref"]
    elif fault == "failed":
        reports["api"]["status"] = "skip"
    else:
        reports["independent_provider"]["run_id"] = "other-run"
    packet = build_packet(inputs)
    assert packet["status"] == "incomplete" and packet["issues"]
    assert set(packet["surfaces"]) == set(SURFACES)


@pytest.mark.parametrize("field", ("scenario_id", "test_ref"))
def test_packet_requires_the_reviewed_acceptance_to_scenario_mapping(field):
    inputs = complete_inputs()
    inputs["cases"]["AC-01"][field] = "unreviewed"
    packet = build_packet(inputs)
    assert packet["status"] == "incomplete"
    assert any("scenario selector" in issue for issue in packet["issues"])
    assert packet["cases"]["AC-01"]["test_ref"] == CASES["AC-01"][1]


@pytest.mark.parametrize("fault", ("stale_workflow", "unmatched_attempt", "duplicate_artifact",
                                   "private_reference", "private_run"))
def test_packet_rejects_crossed_revisions_and_private_identifiers(fault):
    inputs = complete_inputs()
    first = inputs["cases"]["AC-01"]["attempts"]
    if fault == "stale_workflow":
        inputs["surfaces"]["api"]["workflow_revision"] = "9" * 40
    elif fault == "unmatched_attempt":
        first[0]["workflow_run_id"] = 999
    elif fault == "duplicate_artifact":
        first[1]["artifact_ref"] = first[0]["artifact_ref"]
    elif fault == "private_reference":
        first[0]["artifact_ref"] = "evidence/123456789012"
    else:
        inputs["run_id"] = "123456789012"
    packet = build_packet(inputs)
    assert packet["status"] == "incomplete" and packet["issues"]
    assert "123456789012" not in str(packet)


@pytest.mark.parametrize("fault", [
    "missing_case", "missing_browser", "failed", "skipped", "not_run", "wrong_revision", "stale_deployment",
    "wrong_schema", "wrong_cli_checksum", "cli_exit", "missing_stdout", "missing_screenshot", "missing_api_readback",
    "same_readback", "failed_cleanup", "missing_cleanup_receipt", "shared_provider_changed", "missing_report",
    "leaked_target", "leaked_actual",
])
def test_missing_failed_stale_or_unsafe_evidence_cannot_be_review_ready(fault):
    inputs = complete_inputs()
    attempts = inputs["cases"]["AC-01"]["attempts"]
    if fault == "missing_case":
        inputs["cases"].pop("AC-02")
    elif fault == "missing_browser":
        attempts[:] = [attempt for attempt in attempts if attempt["surface"] != "browser"]
    elif fault in ("failed", "skipped", "not_run"):
        attempts[0]["result"] = {"failed": "fail", "skipped": "skip", "not_run": "not_run"}[fault]
    elif fault == "wrong_revision":
        attempts[0]["source_revision"] = "0" * 40
    elif fault == "stale_deployment":
        inputs["deployment"]["api_image_digest"] = "0" * 64
    elif fault == "wrong_schema":
        inputs["deployment"]["schema_head"] = "old_schema"
    elif fault == "wrong_cli_checksum":
        attempts[1]["installed_cli_sha256"] = "1" * 64
    elif fault == "cli_exit":
        attempts[1]["exit_code"] = 1
    elif fault == "missing_stdout":
        del attempts[1]["stdout_ref"]
    elif fault == "missing_screenshot":
        del attempts[2]["screenshot_ref"]
    elif fault == "missing_api_readback":
        del attempts[0]["readback_ref"]
    elif fault == "same_readback":
        attempts[0]["readback_ref"] = attempts[0]["artifact_ref"]
    elif fault == "failed_cleanup":
        inputs["cleanup"]["owned_absent"] = False
    elif fault == "missing_cleanup_receipt":
        inputs["cleanup"]["cases"]["AC-03"]["receipt_ref"] = ""
    elif fault == "shared_provider_changed":
        inputs["cleanup"]["cases"]["AC-01"]["shared_unchanged"] = False
    elif fault == "missing_report":
        inputs["cases"]["AC-01"]["report_ref"] = ""
    elif fault == "leaked_target":
        attempts[0]["artifact_ref"] = "https://private.example.invalid/result"
    else:
        attempts[0]["actual"] = "Account 123456789012 observed"
    packet = build_packet(inputs)
    assert packet["status"] == "incomplete" and packet["issues"]
    assert packet["packet_revision"] != build_packet(complete_inputs())["packet_revision"]
    assert "123456789012" not in str(packet) and "private.example.invalid" not in str(packet)


def test_review_target_pins_b05_packet_and_plan_without_recording_a_decision():
    procedure, anchor = DECISION_PROCEDURE.split("#", 1)
    instructions = (Path(__file__).resolve().parents[3] / procedure).read_text()
    assert "B05" in instructions
    assert anchor == "record-one-decision-using-the-existing-verification-stories"
    assert "## Record one decision, using the existing verification stories" in instructions
    packet = build_packet(complete_inputs())
    assert review_binding(packet) == packet["review"] == {
        "ac_id": "AC-04", "scenario_id": "SCN-04", "bolt_id": BOLT_ID,
        "procedure_ref": DECISION_PROCEDURE, "plan_revision": packet["plan_revision"],
        "packet_revision": packet["packet_revision"], "status": "pending",
    }
    assert "decision" not in packet
    assert build_packet(packet) == packet
    changed = complete_inputs()
    changed["cases"]["AC-01"]["actual"] = "Different result"
    assert packet["review"]["packet_revision"] != build_packet(changed)["packet_revision"]


def test_synthetic_decision_readback_requires_exact_current_packet():
    packet = build_packet(complete_inputs())
    previous = {"bolt_id": BOLT_ID, "plan_revision": packet["plan_revision"],
                "packet_revision": "sha256:" + "1" * 64, "decision": "reject",
                "decision_ref": "receipts/previous-review"}
    current = {**previous, "packet_revision": packet["packet_revision"], "decision": "accept",
               "decision_ref": "receipts/current-review"}
    assert decision_binding(packet, [previous, current]) == current
    assert packet["review"]["status"] == "pending" and "decision" not in packet


@pytest.mark.parametrize("fault", (
    "no_decision", "stale_packet", "wrong_plan", "wrong_bolt", "prior_acceptance", "duplicate_receipt",
    "duplicate_revision", "missing_receipt", "unsafe_receipt", "private_receipt", "forged_attempt",
    "forged_review", "forged_hash", "forged_plan", "incomplete",
))
def test_synthetic_decision_contract_rejects_missing_stale_or_forged_readback(fault):
    packet = build_packet(complete_inputs())
    decision = {"bolt_id": BOLT_ID, "plan_revision": packet["plan_revision"],
                "packet_revision": packet["packet_revision"], "decision": "reject",
                "decision_ref": "receipts/current-review"}
    decisions = [decision]
    if fault == "no_decision":
        decisions = []
    elif fault == "stale_packet":
        decision["packet_revision"] = "sha256:" + "1" * 64
    elif fault == "wrong_plan":
        decision["plan_revision"] = "0" * 40
    elif fault == "wrong_bolt":
        decision["bolt_id"] = "B04"
    elif fault == "prior_acceptance":
        decisions.insert(0, {**decision, "packet_revision": "sha256:" + "1" * 64,
                             "decision": "accept", "decision_ref": "receipts/previous-review"})
    elif fault == "duplicate_receipt":
        decisions.insert(0, {**decision, "packet_revision": "sha256:" + "1" * 64})
    elif fault == "duplicate_revision":
        decisions.append(deepcopy(decision))
    elif fault == "missing_receipt":
        decision["decision_ref"] = ""
    elif fault == "unsafe_receipt":
        decision["decision_ref"] = "https://private.example.invalid/decision"
    elif fault == "private_receipt":
        decision["decision_ref"] = "receipts/123456789012"
    elif fault == "forged_attempt":
        packet["cases"]["AC-01"]["attempts"][0]["result"] = "fail"
    elif fault == "forged_review":
        packet["review"]["status"] = "accepted"
    elif fault == "forged_hash":
        packet["packet_revision"] = "sha256:" + "0" * 64
    elif fault == "forged_plan":
        packet["plan_revision"] = "0" * 40
    else:
        inputs = complete_inputs()
        inputs["cleanup"]["owned_absent"] = False
        packet = build_packet(inputs)
        assert packet["review"]["status"] == "not_ready"
    with pytest.raises(ValueError, match="B05|Incomplete"):
        decision_binding(packet, decisions)
