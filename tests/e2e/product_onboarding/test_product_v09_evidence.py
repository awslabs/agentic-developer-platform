"""Synthetic V09 packet inputs never claim a deployed run or human decision."""

from copy import deepcopy

import pytest

from tests.e2e.product_onboarding.product_v09_evidence import (
    BOLT_ID, CASES, CONTROLS, SCHEMA, build_packet, decision_binding, publish_packet, review_binding,
)
from tests.e2e.product_onboarding.test_product_v06_evidence import complete_inputs as surface_inputs


def complete_inputs():
    inputs = surface_inputs()
    for criterion, (scenario, selector, _) in CASES.items():
        inputs["cases"][criterion]["scenario_id"] = scenario
        inputs["cases"][criterion]["test_ref"] = selector
        for attempt in inputs["cases"][criterion]["attempts"]:
            attempt["test_ref"] = selector
    inputs["controls"] = {
        criterion: {name: [{"attempt_id": f"{criterion}/{name}/1", "result": "pass", "surface": surface,
                            "http_status": status, "exit_code": 5 if status and surface == "installed_cli" else 0,
                            "actual": "Observed expected state with independent readback",
                            "artifact_ref": f"evidence/{criterion.lower()}/{name}-client",
                            "readback_ref": f"evidence/{criterion.lower()}/{name}-observer",
                            "run_id": inputs["run_id"], "source_revision": inputs["source_revision"],
                            "requirements_revision": inputs["requirements_revision"],
                            "plan_revision": inputs["plan_revision"]}]
                    for name, (surface, status) in names.items()}
        for criterion, names in CONTROLS.items()
    }
    return inputs


def test_complete_synthetic_packet_links_scenarios_surfaces_controls_and_revisions():
    inputs = complete_inputs()
    packet = build_packet(inputs)
    assert packet["status"] == "review_ready" and packet["issues"] == []
    assert packet["bolt_id"] == BOLT_ID and packet["evidence_schema"] == SCHEMA
    assert packet["review"]["status"] == "pending" and packet["review"]["packet_revision"] == packet["packet_revision"]
    assert packet["review"]["plan_revision"] == inputs["plan_revision"]
    assert set(packet["cases"]) == set(CASES) and set(packet["controls"]) == set(CONTROLS)
    assert all(len(case["attempts"]) == 4 for case in packet["cases"].values())
    assert build_packet(inputs) == packet == build_packet(packet)
    changed = deepcopy(inputs)
    changed["controls"]["AC-02"]["expired"][0]["actual"] = "Observed an updated source"
    assert build_packet(changed)["packet_revision"] != packet["packet_revision"]


def test_failed_attempt_is_preserved_but_requires_a_later_pass():
    inputs = complete_inputs()
    first = deepcopy(inputs["controls"]["AC-03"]["lost_response"][0])
    first.update(attempt_id="AC-03/lost_response/0", result="interrupted", actual="Connection dropped after commit")
    inputs["controls"]["AC-03"]["lost_response"].insert(0, first)
    packet = build_packet(inputs)
    assert packet["status"] == "review_ready"
    assert [item["result"] for item in packet["controls"]["AC-03"]["lost_response"]] == ["interrupted", "pass"]
    inputs["controls"]["AC-03"]["lost_response"][-1]["result"] = "skip"
    assert build_packet(inputs)["status"] == "incomplete"


def test_packet_publication_preserves_attempts_in_private_file(tmp_path):
    import json
    import stat
    from pathlib import Path

    path, packet = publish_packet(complete_inputs(), tmp_path)
    assert path == str(tmp_path / "example-run-financial-packet.json")
    assert stat.S_IMODE(Path(path).stat().st_mode) == 0o600
    assert json.loads(Path(path).read_text()) == packet
    with pytest.raises(FileExistsError):
        publish_packet(complete_inputs(), tmp_path)


def test_review_binding_identifies_exact_packet_without_pretending_to_accept():
    packet = build_packet(complete_inputs())
    assert review_binding(packet) == packet["review"]
    assert packet["review"]["bolt_id"] == "B08" and packet["review"]["status"] == "pending"
    assert packet["review"]["procedure_ref"].endswith("#record-one-decision-using-the-existing-verification-stories")
    changed = deepcopy(packet)
    changed["controls"]["AC-01"]["browser_date"][0]["actual"] = "Different observation"
    with pytest.raises(ValueError, match="differs from its verified revision"):
        review_binding(changed)
    changed = deepcopy(packet)
    changed["status"] = "incomplete"
    with pytest.raises(ValueError, match="Incomplete B08 packet"):
        review_binding(changed)


def test_decision_readback_accepts_one_exact_revision_after_a_rejected_attempt():
    packet = build_packet(complete_inputs())
    previous = {"bolt_id": BOLT_ID, "plan_revision": packet["plan_revision"],
                "packet_revision": "sha256:" + "0" * 64, "decision": "reject", "decision_ref": "receipts/previous-rejection"}
    current = {"bolt_id": BOLT_ID, "plan_revision": packet["plan_revision"],
               "packet_revision": packet["packet_revision"], "decision": "accept", "decision_ref": "receipts/current-decision"}
    assert decision_binding(packet, [previous, current]) == current
    assert packet["review"]["status"] == "pending"


@pytest.mark.parametrize("fault", ("missing", "wrong_plan", "stale_packet", "duplicate_packet",
                                   "prior_acceptance", "unsafe_receipt", "unverified_packet"))
def test_decision_binding_refuses_stale_duplicate_or_unverified_evidence(fault):
    packet = build_packet(complete_inputs())
    current = {"bolt_id": BOLT_ID, "plan_revision": packet["plan_revision"],
               "packet_revision": packet["packet_revision"], "decision": "accept", "decision_ref": "receipts/current-decision"}
    decisions = [current]
    if fault == "missing":
        decisions = []
    elif fault == "wrong_plan":
        current["plan_revision"] = "f" * 40
    elif fault == "stale_packet":
        current["packet_revision"] = "sha256:" + "f" * 64
    elif fault == "duplicate_packet":
        decisions.append(deepcopy(current))
    elif fault == "prior_acceptance":
        decisions.insert(0, {**current, "packet_revision": "sha256:" + "f" * 64,
                             "decision_ref": "receipts/prior-decision"})
    elif fault == "unsafe_receipt":
        current["decision_ref"] = "https://example.invalid/private"
    else:
        packet["cleanup"]["owned_absent"] = False
    with pytest.raises(ValueError):
        decision_binding(packet, decisions)


@pytest.mark.parametrize("fault", (
    "missing_control", "failed_denial", "wrong_http_status", "accepted_cli_denial", "wrong_source",
    "wrong_plan", "duplicate_attempt", "shared_readback", "unsafe_ref", "unsafe_actual", "missing_browser_report", "failed_cleanup",
))
def test_packet_refuses_missing_or_unproven_financial_evidence(fault):
    inputs = complete_inputs()
    expired = inputs["controls"]["AC-02"]["expired"][0]
    if fault == "missing_control":
        del inputs["controls"]["AC-02"]["revoked"]
    elif fault == "failed_denial":
        expired["result"] = "fail"
    elif fault == "wrong_http_status":
        expired["http_status"] = 200
    elif fault == "accepted_cli_denial":
        expired["exit_code"] = 0
    elif fault == "wrong_source":
        expired["source_revision"] = "0" * 40
    elif fault == "wrong_plan":
        expired["plan_revision"] = "0" * 40
    elif fault == "duplicate_attempt":
        inputs["controls"]["AC-03"]["single_receipt"][0]["attempt_id"] = expired["attempt_id"]
    elif fault == "shared_readback":
        expired["readback_ref"] = expired["artifact_ref"]
    elif fault == "unsafe_ref":
        expired["artifact_ref"] = "https://example.invalid/private"
    elif fault == "unsafe_actual":
        expired["actual"] = "Private source token abcdefghijklmnopqrstuvwxyz12345"
    elif fault == "missing_browser_report":
        del inputs["surfaces"]["browser"]
    else:
        inputs["cleanup"]["cases"]["AC-02"]["owned_absent"] = False
    packet = build_packet(inputs)
    assert packet["status"] == "incomplete" and packet["issues"]
    assert packet["review"]["status"] == "not_ready"
    rebuilt = build_packet(packet)
    assert rebuilt["status"] == "incomplete" and rebuilt["review"]["status"] == "not_ready"
