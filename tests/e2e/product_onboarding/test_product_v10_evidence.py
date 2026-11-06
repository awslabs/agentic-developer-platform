"""Synthetic packet inputs exercise gates, not live B09 acceptance."""

import json
import stat
from copy import deepcopy
from pathlib import Path

import pytest

from tests.e2e.cli_uplift.test_product_onboarding_full import release_record
from tests.e2e.product_onboarding.product_v10_evidence import (
    BOLT_ID, CASES, CONTROLS, SCHEMA, build_packet, decision_binding, publish_packet, review_binding,
)
from tests.e2e.product_onboarding.test_product_v06_evidence import complete_inputs as surface_inputs


def complete_inputs():
    inputs = surface_inputs()
    for criterion, (scenario, selector, _expected) in CASES.items():
        inputs["cases"][criterion]["scenario_id"] = scenario
        inputs["cases"][criterion]["test_ref"] = selector
        for attempt in inputs["cases"][criterion]["attempts"]:
            attempt["test_ref"] = selector
            if attempt["surface"] == "installed_cli":
                attempt["command"] = "adp onboard resume --json"
    inputs["controls"] = {
        criterion: {case: [{"attempt_id": f"{criterion}/{case}/1", "result": "pass", "surface": surface,
                            "run_id": inputs["run_id"], "source_revision": inputs["source_revision"],
                            "requirements_revision": inputs["requirements_revision"],
                            "plan_revision": inputs["plan_revision"],
                            "artifact_ref": f"evidence/{criterion.lower()}/{case}-artifact",
                            "readback_ref": f"evidence/{criterion.lower()}/{case}-readback",
                            "actual": "Observed expected behavior with independent readback"}]
                    for case, surface in required.items()}
        for criterion, required in CONTROLS.items()}
    release = release_record()
    release.update(run_id=inputs["run_id"], source_revision=inputs["source_revision"],
                   requirements_revision=inputs["requirements_revision"])
    release["artifacts"].update(api_image_digest=inputs["artifacts"]["api_image_digest"],
                                ui_manifest_sha256=inputs["artifacts"]["ui_manifest_sha256"],
                                schema_head=inputs["artifacts"]["schema_head"],
                                workflow_revision=inputs["artifacts"]["api_workflow_revision"])
    release["artifacts"]["cli_hashes"]["adp"] = inputs["artifacts"]["cli_sha256"]
    release["observed_release"].update(deepcopy(release["artifacts"]))
    release["observed_release"].update(source_revision=inputs["source_revision"],
                                       artifact_ref=inputs["deployment"]["receipt_ref"],
                                       readback_ref=inputs["deployment"]["observation_ref"])
    release["cleanup"].update(run_id=inputs["run_id"], source_revision=inputs["source_revision"],
                              receipt_ref=inputs["cleanup"]["receipt_ref"])
    for criterion, entry in release["attempts"].items():
        entry.update(run_id=inputs["run_id"], source_revision=inputs["source_revision"],
                     requirements_revision=inputs["requirements_revision"])
        for attempt in entry["attempt_history"]:
            attempt["run_id"] = inputs["run_id"]
    for entry in release["regressions"].values():
        entry["run_id"] = inputs["run_id"]
    release["rollback"]["run_id"] = inputs["run_id"]
    inputs["release_record"] = release
    inputs["v11_contribution"] = {
        "story": "V11", "status": "review_ready", "source_revision": inputs["source_revision"],
        "plan_revision": inputs["plan_revision"], "packet_revision": "sha256:" + "9" * 64,
        "report_ref": "evidence/v11/report", "readback_ref": "evidence/v11/readback",
        "peer_state": {"context": "unavailable", "l5": "unavailable"},
        "cleanup_verified": True,
    }
    return inputs


def test_synthetic_combined_packet_preserves_all_surfaces_and_deferred_peer_status():
    inputs = complete_inputs()
    packet = build_packet(inputs)
    assert packet["bolt_id"] == BOLT_ID and packet["evidence_schema"] == SCHEMA
    assert packet["status"] == "review_ready" and packet["issues"] == []
    assert packet["review"]["status"] == "pending"
    assert set(packet["cases"]) == set(packet["controls"]) == set(CASES)
    assert all(len(record["attempts"]) == 4 for record in packet["cases"].values())
    assert packet["peer_state"] == {"context": "unavailable", "l5": "unavailable"}
    assert packet["deferred_work"] == ["#8266", "#8149", "#8150"]
    assert build_packet(packet) == packet
    changed = deepcopy(inputs)
    changed["controls"]["AC-02"]["context_unavailable"][0]["actual"] = "Different observation"
    assert build_packet(changed)["packet_revision"] != packet["packet_revision"]


@pytest.mark.parametrize("fault", (
    "missing_v11", "v11_real_peer_claim", "v11_stale_source", "v11_shared_readback",
    "missing_control", "unselected_control", "failed_control", "skipped_control",
    "duplicate_control", "unsafe_control", "identical_control_ref", "stale_control",
    "failed_surface", "missing_release", "stale_release", "failed_release_attempt",
    "cleanup_failed", "missing_ui", "wrong_cli", "wrong_deployment",
))
def test_packet_refuses_missing_deferred_or_unverified_evidence(fault):
    inputs = complete_inputs()
    control = inputs["controls"]["AC-02"]["context_unavailable"][0]
    if fault == "missing_v11":
        inputs.pop("v11_contribution")
    elif fault == "v11_real_peer_claim":
        inputs["v11_contribution"]["peer_state"]["context"] = "available"
    elif fault == "v11_stale_source":
        inputs["v11_contribution"]["source_revision"] = "f" * 40
    elif fault == "v11_shared_readback":
        inputs["v11_contribution"]["readback_ref"] = inputs["v11_contribution"]["report_ref"]
    elif fault == "missing_control":
        inputs["controls"]["AC-01"].pop("idea")
    elif fault == "unselected_control":
        inputs["controls"]["AC-01"]["fake_peer_result"] = []
    elif fault in ("failed_control", "skipped_control"):
        control["result"] = "fail" if fault == "failed_control" else "skip"
    elif fault == "duplicate_control":
        control["attempt_id"] = inputs["controls"]["AC-01"]["idea"][0]["attempt_id"]
    elif fault == "unsafe_control":
        control["actual"] = "https://private.example.invalid"
    elif fault == "identical_control_ref":
        control["readback_ref"] = control["artifact_ref"]
    elif fault == "stale_control":
        control["source_revision"] = "f" * 40
    elif fault == "failed_surface":
        inputs["cases"]["AC-02"]["attempts"][0]["result"] = "fail"
    elif fault == "missing_release":
        inputs.pop("release_record")
    elif fault == "stale_release":
        inputs["release_record"]["observed_release"]["schema_head"] = "old_schema"
    elif fault == "failed_release_attempt":
        inputs["release_record"]["attempts"]["AC-03"]["result"] = "not_run"
    elif fault == "cleanup_failed":
        inputs["cleanup"]["owned_absent"] = False
    elif fault == "missing_ui":
        inputs["surfaces"].pop("browser")
    elif fault == "wrong_cli":
        inputs["release_record"]["artifacts"]["cli_hashes"]["adp"] = "8" * 64
    else:
        inputs["deployment"]["source_revision"] = "f" * 40
    packet = build_packet(inputs)
    assert packet["status"] == "incomplete" and packet["issues"]
    assert packet["review"]["status"] == "not_ready"


def test_packet_publication_is_private_and_does_not_overwrite_attempt_history(tmp_path):
    path, packet = publish_packet(complete_inputs(), tmp_path)
    assert Path(path).name == "example-run-b09-packet.json"
    assert stat.S_IMODE(Path(path).stat().st_mode) == 0o600
    published = json.loads(Path(path).read_text())
    assert published == packet and review_binding(published) == packet["review"]
    with pytest.raises(FileExistsError):
        publish_packet(complete_inputs(), tmp_path)


def test_single_b09_review_binding_names_exact_pending_plan_and_packet():
    packet = build_packet(complete_inputs())
    assert review_binding(packet) == packet["review"]
    assert packet["review"]["bolt_id"] == "B09"
    assert packet["review"]["scenario_id"] == "SCN-04"
    assert packet["review"]["plan_revision"] == packet["plan_revision"]
    assert packet["review"]["packet_revision"] == packet["packet_revision"]
    assert packet["review"]["status"] == "pending"
    changed = deepcopy(packet)
    changed["controls"]["AC-02"]["context_unavailable"][0]["actual"] = "Changed peer observation"
    with pytest.raises(ValueError, match="verified revision"):
        review_binding(changed)
    changed = deepcopy(packet)
    changed["v11_contribution"] = None
    with pytest.raises(ValueError):
        review_binding(changed)


def test_authoritative_b09_decision_readback_allows_prior_rejection_not_prior_acceptance():
    packet = build_packet(complete_inputs())
    prior = {"bolt_id": BOLT_ID, "plan_revision": packet["plan_revision"],
             "packet_revision": "sha256:" + "0" * 64,
             "decision": "reject", "decision_ref": "receipts/previous-rejection"}
    current = {"bolt_id": BOLT_ID, "plan_revision": packet["plan_revision"],
               "packet_revision": packet["packet_revision"],
               "decision": "accept", "decision_ref": "receipts/current-decision"}
    assert decision_binding(packet, [prior, current]) == current
    assert packet["review"]["status"] == "pending"
    prior["decision"] = "accept"
    with pytest.raises(ValueError, match="already has an accepted"):
        decision_binding(packet, [prior, current])


@pytest.mark.parametrize("fault", ("missing", "stale_packet", "wrong_plan", "duplicate_revision",
                                    "duplicate_receipt", "unsafe_receipt", "wrong_bolt", "extra_field",
                                    "unverified_packet"))
def test_b09_decision_refuses_stale_duplicate_or_incomplete_proof(fault):
    packet = build_packet(complete_inputs())
    decision = {"bolt_id": BOLT_ID, "plan_revision": packet["plan_revision"],
                "packet_revision": packet["packet_revision"],
                "decision": "accept", "decision_ref": "receipts/current-decision"}
    decisions = [decision]
    if fault == "missing":
        decisions = []
    elif fault == "stale_packet":
        decision["packet_revision"] = "sha256:" + "7" * 64
    elif fault == "wrong_plan":
        decision["plan_revision"] = "7" * 40
    elif fault == "duplicate_revision":
        decisions.append(deepcopy(decision))
    elif fault == "duplicate_receipt":
        decisions.insert(0, {**decision, "packet_revision": "sha256:" + "7" * 64,
                             "decision": "reject"})
    elif fault == "unsafe_receipt":
        decision["decision_ref"] = "https://private.example.invalid"
    elif fault == "wrong_bolt":
        decision["bolt_id"] = "B08"
    elif fault == "extra_field":
        decision["another_decision"] = "accept"
    else:
        packet["cleanup"]["owned_absent"] = False
    with pytest.raises(ValueError):
        decision_binding(packet, decisions)
