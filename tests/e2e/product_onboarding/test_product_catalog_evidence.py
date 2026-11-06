"""Offline evidence assembly tests; these fixtures never represent a deployed run."""

from copy import deepcopy

import pytest

from tests.e2e.product_onboarding.product_catalog_evidence import (
    BOLT_ID, CASES, DECISION_PROCEDURE, SCHEMA, build_packet, review_binding,
)


def sample_inputs():
    source = "a" * 40
    requirements = "b" * 40
    artifacts = {"api_image_digest": "sha256:" + "c" * 64,
                 "frontend_manifest_sha256": "d" * 64, "installed_cli_sha256": "e" * 64,
                 "api_workflow_revision": "f" * 40, "browser_workflow_revision": "1" * 40,
                 "cli_workflow_revision": "2" * 40, "schema_head": "catalog_v12"}
    cases = {}
    for criterion, (scenario, selectors) in CASES.items():
        attempts = []
        for surface, selector in selectors.items():
            attempt = {"attempt_id": f"{criterion[-2:]}-{surface}", "surface": surface,
                       "test_ref": selector, "result": "pass", "run_id": "synthetic-run",
                       "requirements_revision": requirements, "source_revision": source,
                       "artifact_ref": f"evidence/{criterion.lower()}-{surface}-report",
                       "readback_ref": f"evidence/{criterion.lower()}-{surface}-readback",
                       "expected": "Owned relationships and original Product identity match",
                       "actual": "Synthetic owned relationships and original Product identity match",
                       "workflow_run_id": 1, "workflow_attempt": 1}
            if surface == "browser":
                attempt.update({"screenshot_ref": "evidence/browser-screen", "trace_ref": "evidence/browser-trace"})
            attempts.append(attempt)
        cases[criterion] = {"scenario_id": scenario, "expected": "Owned case verifies without scope disclosure",
                            "actual": "Synthetic case verifies without scope disclosure", "attempts": attempts}
    return {"run_id": "synthetic-run", "plan_revision": "3" * 40,
            "requirements_revision": requirements, "source_revision": source,
            "artifacts": artifacts,
            "deployment": {**artifacts, "source_revision": source,
                           "receipt_ref": "receipts/release", "observation_ref": "evidence/release-readback"},
            "cli": {"run_id": "synthetic-run", "source_revision": source,
                    "installed_cli_sha256": artifacts["installed_cli_sha256"], "status": "pass", "exit_code": 0,
                    "workflow_revision": artifacts["cli_workflow_revision"], "workflow_run_id": 1, "workflow_attempt": 1,
                    "command": "adp product catalog show --run synthetic-run",
                    "artifact_ref": "evidence/cli-artifact", "readback_ref": "evidence/cli-readback",
                    "stdout_ref": "evidence/cli-stdout",
                    "stderr_ref": "evidence/cli-stderr", "expected": "Installed CLI reports selected release",
                    "actual": "Synthetic CLI reports selected release"},
            "cases": cases,
            "cleanup": {"run_id": "synthetic-run", "status": "verified", "receipt_ref": "receipts/cleanup",
                        "observation_ref": "evidence/cleanup-readback", "owned_absent": True,
                        "unowned_unchanged": True, "evidence_retained": True}}


def test_v12_packet_maps_every_case_and_surface_to_pinned_independent_evidence():
    inputs = sample_inputs()
    packet = build_packet(inputs)
    assert packet["evidence_schema"] == SCHEMA
    assert packet["status"] == "review_ready" and packet["issues"] == []
    assert packet["packet_revision"].startswith("sha256:")
    assert {key: value["scenario_id"] for key, value in packet["cases"].items()} == {
        "AC-01": "SCN-01", "AC-02": "SCN-02", "AC-03": "SCN-03"}
    assert {key: value["status"] for key, value in packet["surfaces"].items()} == {
        "api": "pass", "browser": "pass", "cli": "pass"}
    assert packet["cleanup"]["observation_ref"] != packet["cleanup"]["receipt_ref"]
    assert packet["cases"]["AC-01"]["attempts"][0]["readback_ref"] != packet["deployment"]["receipt_ref"]
    assert build_packet(inputs)["packet_revision"] == packet["packet_revision"]
    assert build_packet({**inputs, "status": "forged", "packet_revision": "forged"}) == packet


def test_v12_packet_requires_installed_cli_and_independent_api_browser_artifacts():
    inputs = sample_inputs()
    inputs["cli"].pop("stdout_ref")
    packet = build_packet(inputs)
    assert packet["status"] == "incomplete" and any("cli.stdout_ref" in issue for issue in packet["issues"])
    inputs = sample_inputs()
    browser = inputs["cases"]["AC-01"]["attempts"][1]
    browser["readback_ref"] = browser["artifact_ref"]
    packet = build_packet(inputs)
    assert packet["status"] == "incomplete"
    assert any("independent matching observations" in issue for issue in packet["issues"])
    assert packet["packet_revision"] != build_packet(sample_inputs())["packet_revision"]


def test_v12_packet_rejects_stale_deployment_schema_and_cli_readback():
    inputs = sample_inputs()
    inputs["deployment"]["schema_head"] = "stale_schema"
    assert "independently observed deployment differs from selected release" in build_packet(inputs)["issues"]
    inputs = sample_inputs()
    inputs["cli"]["readback_ref"] = inputs["cli"]["stdout_ref"]
    assert "installed CLI result does not match the selected run and artifact" in build_packet(inputs)["issues"]


@pytest.mark.parametrize("criterion,surface", [("AC-01", "api"), ("AC-01", "browser"),
                                            ("AC-02", "api"), ("AC-02", "access"), ("AC-03", "api")])
def test_v12_packet_rejects_missing_required_surface(criterion, surface):
    inputs = sample_inputs()
    inputs["cases"][criterion]["attempts"] = [
        item for item in inputs["cases"][criterion]["attempts"] if item["surface"] != surface]
    packet = build_packet(inputs)
    assert packet["status"] == "incomplete"
    assert f"{criterion}.{surface} has no passing final attempt" in packet["issues"]


@pytest.mark.parametrize("result", ["fail", "skip", "not_run", "interrupted"])
@pytest.mark.parametrize("criterion,surface", [("AC-01", "api"), ("AC-01", "browser"),
                                            ("AC-02", "access"), ("AC-03", "api")])
def test_v12_packet_rejects_nonpassing_required_attempts(result, criterion, surface):
    inputs = sample_inputs()
    attempt = next(item for item in inputs["cases"][criterion]["attempts"] if item["surface"] == surface)
    attempt["result"] = result
    packet = build_packet(inputs)
    assert packet["status"] == "incomplete"
    assert f"{criterion}.{surface} has no passing final attempt" in packet["issues"]
    assert any(item["result"] == result for item in packet["cases"][criterion]["attempts"])


@pytest.mark.parametrize("field,value", [("status", "failed"), ("owned_absent", False),
                                          ("unowned_unchanged", False), ("evidence_retained", False),
                                          ("observation_ref", "receipts/cleanup")])
def test_v12_packet_rejects_failed_or_unverified_owned_cleanup(field, value):
    inputs = sample_inputs()
    inputs["cleanup"][field] = value
    packet = build_packet(inputs)
    assert packet["status"] == "incomplete"
    assert "independent owned-only cleanup and evidence retention are unverified" in packet["issues"]


def test_v12_packet_retains_failed_attempt_but_requires_distinct_successful_rerun():
    inputs = sample_inputs()
    attempts = inputs["cases"]["AC-03"]["attempts"]
    failure = deepcopy(attempts[0])
    failure["attempt_id"] = "prior-interruption"
    failure["result"] = "interrupted"
    attempts.insert(0, failure)
    attempts[1]["workflow_attempt"] = 2
    packet = build_packet(inputs)
    assert packet["status"] == "review_ready"
    assert [item["result"] for item in packet["cases"]["AC-03"]["attempts"]] == ["interrupted", "pass"]
    attempts[1]["result"] = "fail"
    failed = build_packet(inputs)
    assert failed["status"] == "incomplete" and failed["packet_revision"] != packet["packet_revision"]


def test_v12_packet_cannot_call_same_workflow_failure_and_success_a_rerun():
    inputs = sample_inputs()
    attempts = inputs["cases"]["AC-03"]["attempts"]
    failure = deepcopy(attempts[0])
    failure["attempt_id"] = "prior-failure"
    failure["result"] = "fail"
    attempts.insert(0, failure)
    packet = build_packet(inputs)
    assert packet["status"] == "incomplete"
    assert "AC-03.api repeats the same workflow attempt" in packet["issues"]


@pytest.mark.parametrize("result", ["skip", "not_run"])
def test_v12_packet_preserves_reason_for_previous_nonexecution(result):
    inputs = sample_inputs()
    attempts = inputs["cases"]["AC-03"]["attempts"]
    earlier = deepcopy(attempts[0])
    earlier.update({"attempt_id": "prior-check", "result": result})
    attempts.insert(0, earlier)
    attempts[1]["workflow_attempt"] = 2
    packet = build_packet(inputs)
    assert packet["status"] == "incomplete"
    assert any("reason" in issue for issue in packet["issues"])
    earlier["reason"] = "Fixture not yet available"
    packet = build_packet(inputs)
    assert packet["status"] == "review_ready"
    assert packet["cases"]["AC-03"]["attempts"][0]["reason"] == earlier["reason"]


def test_v12_packet_links_exact_revision_to_one_pending_b10_review():
    inputs = sample_inputs()
    packet = build_packet(inputs)
    assert packet["review"] == {
        "ac_id": "AC-04", "scenario_id": "SCN-04", "bolt_id": BOLT_ID,
        "procedure_ref": DECISION_PROCEDURE, "plan_revision": inputs["plan_revision"],
        "packet_revision": packet["packet_revision"], "status": "pending",
    }
    assert review_binding(packet, inputs["plan_revision"]) == packet["review"]
    assert review_binding(packet, inputs["plan_revision"]) is not packet["review"]
    assert build_packet(packet) == packet


@pytest.mark.parametrize("fault", ["missing_case", "failed_cleanup", "stale_plan", "stale_revision",
                                  "changed_attempt", "forged_status", "wrong_bolt", "forged_decision"])
def test_v12_b10_binding_rejects_incomplete_changed_or_forged_packet(fault):
    packet = build_packet(sample_inputs())
    plan = packet["plan_revision"]
    if fault == "missing_case":
        del packet["cases"]["AC-03"]
    elif fault == "failed_cleanup":
        packet["cleanup"]["status"] = None
    elif fault == "stale_plan":
        plan = "0" * 40
    elif fault == "stale_revision":
        packet["packet_revision"] = "sha256:" + "0" * 64
    elif fault == "changed_attempt":
        packet["cases"]["AC-01"]["attempts"][0]["result"] = "fail"
    elif fault == "forged_status":
        packet["issues"] = ["private failure"]
    elif fault == "wrong_bolt":
        packet["review"]["bolt_id"] = "B11"
    else:
        packet["review"]["status"] = "accepted"
    with pytest.raises(ValueError, match="B10"):
        review_binding(packet, plan)


def test_v12_incomplete_packet_marks_review_not_ready_without_approval():
    inputs = sample_inputs()
    inputs["cases"]["AC-01"]["attempts"][0]["result"] = "skip"
    packet = build_packet(inputs)
    assert packet["review"]["status"] == "not_ready"
    assert packet["review"]["packet_revision"] == packet["packet_revision"]
    with pytest.raises(ValueError, match="B10"):
        review_binding(packet, inputs["plan_revision"])
