"""Synthetic B07 packet checks; no deployed or human decision is inferred."""

from copy import deepcopy

import pytest

from tests.e2e.product_onboarding.product_v08_evidence import (
    BOLT_ID, CASES, CONTROLS, SCHEMA, build_packet,
)
from tests.e2e.product_onboarding.test_product_v06_evidence import complete_inputs as v06_inputs


def complete_inputs():
    inputs = v06_inputs()
    for criterion, (scenario, selector, _) in CASES.items():
        case = inputs["cases"][criterion]
        case["scenario_id"] = scenario
        case["test_ref"] = selector
        for attempt in case["attempts"]:
            attempt["test_ref"] = selector
    inputs["controls"] = {
        criterion: {
            name: [{"attempt_id": f"{criterion}/{name}/1", "result": "pass",
                    "artifact_ref": f"evidence/{criterion.lower()}/{name}-attempt",
                    "readback_ref": f"evidence/{criterion.lower()}/{name}-provider",
                    "run_id": inputs["run_id"], "source_revision": inputs["source_revision"],
                    "requirements_revision": inputs["requirements_revision"]}]
            for name in names
        } for criterion, names in CONTROLS.items()
    }
    inputs["producers"] = {
        "gateway_handoff": {"source_revision": inputs["source_revision"],
                            "workflow_revision": "d" * 40, "receipt_ref": "receipts/gateway-handoff"},
    }
    inputs["deployed_producers"] = {
        "gateway_handoff": {"source_revision": inputs["source_revision"],
                            "workflow_revision": "d" * 40, "observation_ref": "evidence/gateway-deployment"},
    }
    return inputs


def test_complete_packet_reconciles_surfaces_controls_producers_and_revision():
    inputs = complete_inputs()
    packet = build_packet(inputs)
    assert packet["bolt_id"] == BOLT_ID and packet["evidence_schema"] == SCHEMA
    assert packet["status"] == "review_ready" and not packet["issues"]
    assert packet["review"] == {
        "ac_id": "AC-04", "scenario_id": "SCN-04", "bolt_id": BOLT_ID,
        "procedure_ref": packet["review"]["procedure_ref"],
        "plan_revision": inputs["plan_revision"], "packet_revision": packet["packet_revision"],
        "status": "pending",
    }
    assert set(packet["cases"]) == set(CASES)
    assert set(packet["controls"]) == set(CONTROLS)
    for criterion, (scenario, selector, expected) in CASES.items():
        case = packet["cases"][criterion]
        assert (case["scenario_id"], case["test_ref"], case["expected"]) == (scenario, selector, expected)
        assert len(case["attempts"]) == 4
        assert all(attempt["expected"] == expected for attempt in case["attempts"])
        assert set(packet["controls"][criterion]) == set(CONTROLS[criterion])
    assert packet["producers"]["gateway_handoff"]["observation_ref"] == "evidence/gateway-deployment"
    assert build_packet(inputs) == packet
    assert build_packet(packet) == packet
    changed = deepcopy(inputs)
    changed["controls"]["AC-02"]["lost_response"][0]["artifact_ref"] = "evidence/another-attempt"
    assert build_packet(changed)["packet_revision"] != packet["packet_revision"]


def test_failed_attempt_remains_visible_without_becoming_a_pass():
    inputs = complete_inputs()
    first = deepcopy(inputs["controls"]["AC-02"]["lost_response"][0])
    first.update(attempt_id="AC-02/lost_response/0", result="interrupted")
    inputs["controls"]["AC-02"]["lost_response"].insert(0, first)
    packet = build_packet(inputs)
    assert packet["status"] == "review_ready"
    assert [entry["result"] for entry in packet["controls"]["AC-02"]["lost_response"]] == ["interrupted", "pass"]
    inputs["controls"]["AC-02"]["lost_response"][-1]["result"] = "fail"
    assert build_packet(inputs)["status"] == "incomplete"


@pytest.mark.parametrize("field,value", (
    ("run_id", "other-run"),
    ("source_revision", "f" * 40),
    ("requirements_revision", "f" * 40),
))
def test_mismatched_control_provenance_remains_incomplete_after_rebuild(field, value):
    from tests.e2e.product_onboarding.product_v08_evidence import review_binding

    inputs = complete_inputs()
    inputs["controls"]["AC-02"]["stale_callback"][0][field] = value
    packet = build_packet(inputs)
    rebuilt = build_packet(packet)
    for candidate in (packet, rebuilt):
        assert candidate["status"] == "incomplete"
        assert "AC-02.stale_callback attempt 0 differs from the pinned run" in candidate["issues"]
        assert candidate["review"]["status"] == "not_ready"
        with pytest.raises(ValueError, match="Incomplete B07 packet"):
            review_binding(candidate)
    assert packet["controls"]["AC-02"]["stale_callback"][0][field] is None
    assert rebuilt == packet


@pytest.mark.parametrize("fault", (
    "missing_control", "missing_attempt", "failed_control", "skipped_control", "duplicate_attempt",
    "stale_run", "stale_source", "stale_requirements", "shared_readback", "unsafe_reference",
    "wrong_scenario", "failed_surface", "missing_deployment", "producer_mismatch",
    "producer_missing", "producer_unobserved", "failed_cleanup", "missing_cleanup_receipt",
    "invalid_result",
))
def test_incomplete_or_mismatched_evidence_never_becomes_review_ready(fault):
    inputs = complete_inputs()
    entry = inputs["controls"]["AC-02"]["stale_callback"][0]
    if fault == "missing_control":
        del inputs["controls"]["AC-02"]["stale_callback"]
    elif fault == "missing_attempt":
        inputs["controls"]["AC-02"]["stale_callback"] = []
    elif fault == "invalid_result":
        entry["result"] = {"unsafe": "value"}
    elif fault == "failed_control":
        entry["result"] = "fail"
    elif fault == "skipped_control":
        entry["result"] = "skip"
    elif fault == "duplicate_attempt":
        inputs["controls"]["AC-02"]["stale_callback"].append(deepcopy(entry))
    elif fault == "stale_run":
        entry["run_id"] = "other-run"
    elif fault == "stale_source":
        entry["source_revision"] = "f" * 40
    elif fault == "stale_requirements":
        entry["requirements_revision"] = "f" * 40
    elif fault == "shared_readback":
        entry["readback_ref"] = entry["artifact_ref"]
    elif fault == "unsafe_reference":
        entry["artifact_ref"] = "https://private.example.invalid/123456789012"
    elif fault == "wrong_scenario":
        inputs["cases"]["AC-02"]["scenario_id"] = "SCN-01"
    elif fault == "failed_surface":
        inputs["surfaces"]["browser"]["status"] = "fail"
    elif fault == "missing_deployment":
        inputs["deployment"]["api_image_digest"] = "1" * 64
    elif fault == "producer_mismatch":
        inputs["deployed_producers"]["gateway_handoff"]["workflow_revision"] = "1" * 40
    elif fault == "producer_missing":
        inputs["producers"] = {}
    elif fault == "producer_unobserved":
        inputs["deployed_producers"]["gateway_handoff"]["observation_ref"] = "receipts/gateway-handoff"
    elif fault == "failed_cleanup":
        inputs["cleanup"]["owned_absent"] = False
    else:
        inputs["cleanup"]["cases"]["AC-03"]["receipt_ref"] = ""
    packet = build_packet(inputs)
    assert packet["status"] == "incomplete" and packet["issues"]
    assert packet["review"]["status"] == "not_ready"
    assert "private.example.invalid" not in str(packet) and "123456789012" not in str(packet)


def test_b07_review_pins_the_existing_one_decision_procedure():
    from pathlib import Path

    from tests.e2e.product_onboarding.product_v08_evidence import DECISION_PROCEDURE, review_binding

    procedure, anchor = DECISION_PROCEDURE.split("#", 1)
    instructions = (Path(__file__).resolve().parents[3] / procedure).read_text()
    assert anchor == "record-one-decision-using-the-existing-verification-stories"
    assert "## Record one decision, using the existing verification stories" in instructions
    assert "B07" in instructions and "exact packet and plan revision" in instructions
    packet = build_packet(complete_inputs())
    assert review_binding(packet) == packet["review"]
    assert "decision" not in packet
    changed = complete_inputs()
    changed["cases"]["AC-01"]["actual"] = "Different result"
    assert build_packet(changed)["packet_revision"] != packet["packet_revision"]


def test_authoritative_review_readback_selects_exact_b07_revision():
    from tests.e2e.product_onboarding.product_v08_evidence import decision_binding

    packet = build_packet(complete_inputs())
    old = {"bolt_id": BOLT_ID, "plan_revision": packet["plan_revision"],
           "packet_revision": "sha256:" + "1" * 64, "decision": "reject",
           "decision_ref": "receipts/previous-review"}
    current = {**old, "packet_revision": packet["packet_revision"], "decision": "accept",
               "decision_ref": "receipts/current-review"}
    assert decision_binding(packet, [old, current]) == current
    assert packet["review"]["status"] == "pending" and "decision" not in packet


@pytest.mark.parametrize("fault", (
    "no_decision", "stale_revision", "wrong_plan", "wrong_bolt", "duplicate_revision",
    "prior_acceptance", "unsafe_receipt", "forged_packet", "incomplete_cleanup",
))
def test_b07_review_rejects_stale_or_forged_decisions(fault):
    from tests.e2e.product_onboarding.product_v08_evidence import decision_binding

    packet = build_packet(complete_inputs())
    current = {"bolt_id": BOLT_ID, "plan_revision": packet["plan_revision"],
               "packet_revision": packet["packet_revision"], "decision": "accept",
               "decision_ref": "receipts/current-review"}
    decisions = [current]
    if fault == "no_decision":
        decisions = []
    elif fault == "stale_revision":
        current["packet_revision"] = "sha256:" + "1" * 64
    elif fault == "wrong_plan":
        current["plan_revision"] = "1" * 40
    elif fault == "wrong_bolt":
        current["bolt_id"] = "B05"
    elif fault == "duplicate_revision":
        decisions.append(deepcopy(current))
    elif fault == "prior_acceptance":
        decisions.insert(0, {**current, "packet_revision": "sha256:" + "1" * 64,
                             "decision_ref": "receipts/prior-review"})
    elif fault == "unsafe_receipt":
        current["decision_ref"] = "https://private.example.invalid/receipt"
    elif fault == "forged_packet":
        packet["controls"]["AC-02"]["stale_callback"][0]["result"] = "fail"
    else:
        incomplete = complete_inputs()
        incomplete["cleanup"]["owned_absent"] = False
        packet = build_packet(incomplete)
    with pytest.raises(ValueError, match="B07|Incomplete"):
        decision_binding(packet, decisions)
