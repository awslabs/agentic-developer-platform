"""Offline packet tests; these synthetic references are not live B02 evidence."""

from copy import deepcopy

import pytest

from tests.e2e.product_onboarding.product_v02_evidence import CASES, SCHEMA, build_packet

SOURCE = "a" * 40
WORKFLOW = "b" * 40
PLAN = "c" * 40
REQUIREMENTS = "d" * 40
FRONTEND = "e" * 64
CLI = "f" * 64
IMAGE = "sha256:" + "1" * 64


def sample_packet():
    run_id = "product-42-1"
    refs = {name: "private/api/" + name for name in CASES}
    cases = {}
    for name, (criterion, scenario, test_ref) in CASES.items():
        cases[name] = {
            "ac_id": criterion, "scenario_id": scenario, "test_ref": test_ref,
            "run_id": run_id, "status": "pass", "expected": "Product result and permission assertions",
            "actual": "API comparison and browser assertion passed", "evidence_ref": "private/ui/" + name,
            "readback_ref": refs[name], "cleanup_ref": "private/cleanup",
            "attempts": [{
                "attempt_id": "42/1/" + name, "workflow_run_id": 42, "workflow_attempt": 1,
                "result": "pass", "expected": "Observed Product result",
                "actual": "Browser and API checks passed", "evidence_ref": "private/attempt/" + name,
            }],
        }
    return {
        "run_id": run_id, "plan_revision": PLAN, "report_artifact": "private/b02/report",
        "identities": {
            "requirements_revision": REQUIREMENTS, "source_revision": SOURCE,
            "browser_code_revision": SOURCE, "browser_workflow_revision": WORKFLOW, "api_workflow_revision": "2" * 40,
            "frontend_workflow_revision": "3" * 40, "schema_head": "profile-head",
            "api_image_digest": IMAGE, "frontend_release_sha256": FRONTEND,
            "frontend_release_ref": "private/frontend/verified-release", "cli_checksum": CLI,
        },
        "deployment": {
            "receipt_ref": "private/deployment/signed", "observation_ref": "private/deployment/readback",
            "observed": {
                "source_revision": SOURCE, "api_workflow_revision": "2" * 40,
                "frontend_workflow_revision": "3" * 40, "schema_head": "profile-head", "api_image_digest": IMAGE,
                "frontend_release_sha256": FRONTEND, "frontend_release_ref": "private/frontend/verified-release",
                "cli_checksum": CLI, "product_profile_enabled": True,
            },
        },
        "surfaces": {
            "ui": {
                "status": "pass", "run_id": run_id, "workflow_run_id": 42, "workflow_attempt": 1,
                "source_revision": SOURCE, "browser_workflow_revision": WORKFLOW, "frontend_release_ref": "private/frontend/verified-release",
                "artifact_ref": "private/ui/screenshots", "report_ref": "private/ui/junit",
                "expected": "Three-field create and edit; no false saves or disclosure",
                "actual": "Browser cases passed", "viewports": {
                    width: {"screenshot_ref": "private/screenshot/" + width,
                            "trace_ref": "private/trace/" + width, "overflow": False, "keyboard": "pass"}
                    for width in ("390", "1440")
                },
            },
            "api": {
                "status": "pass", "run_id": run_id, "workflow_run_id": 42, "workflow_attempt": 1,
                "source_revision": SOURCE, "browser_workflow_revision": WORKFLOW, "artifact_ref": "private/api/result",
                "report_ref": "private/api/report", "readback_ref": "private/api/readback",
                "case_readback_refs": refs, "expected": "Owned values and denied access",
                "actual": "Independent Product API checks passed",
            },
            "cli": {
                "status": "artifact_only", "source_revision": SOURCE,
                "cli_checksum": CLI, "artifact_ref": "private/cli/release-identity",
            },
        },
        "cases": cases,
        "cleanup": {
            "status": "verified", "run_id": run_id, "receipt_ref": "private/cleanup",
            "product_id": "owned-product", "project_id": "owned-project",
            "fixture_removed": True, "owned_absent": True, "shared_unchanged": True,
        },
    }


def test_complete_synthetic_inventory_is_stable_and_does_not_mutate_inputs():
    inputs = sample_packet()
    original = deepcopy(inputs)
    packet = build_packet(inputs)
    assert packet["evidence_schema"] == SCHEMA
    assert packet["status"] == "review_ready"
    assert packet["issues"] == []
    assert packet["packet_revision"].startswith("sha256:")
    assert build_packet(deepcopy(inputs))["packet_revision"] == packet["packet_revision"]
    assert inputs == original
    assert set(packet["cases"]) == set(CASES)


@pytest.mark.parametrize("change,fragment", [
    (lambda packet: packet["cases"].pop("live_browser_evidence"), "inventory"),
    (lambda packet: packet["cases"]["live_browser_create"].update(test_ref="wrong"), "mapping"),
    (lambda packet: packet["identities"].update(source_revision="stale"), "source_revision"),
    (lambda packet: packet["deployment"]["observed"].update(api_image_digest="sha256:" + "0" * 64), "api_image_digest"),
    (lambda packet: packet["deployment"]["observed"].update(frontend_workflow_revision="0" * 40), "frontend_workflow_revision"),
    (lambda packet: packet["surfaces"]["ui"].update(browser_workflow_revision="0" * 40), "runner revision"),
    (lambda packet: packet["deployment"]["observed"].update(product_profile_enabled=False), "does not admit"),
    (lambda packet: packet["deployment"].update(observation_ref="private/deployment/signed"), "independent"),
    (lambda packet: packet["surfaces"]["ui"]["viewports"].pop("390"), "390"),
    (lambda packet: packet["surfaces"]["ui"]["viewports"]["1440"].update(trace_ref=""), "1440"),
    (lambda packet: packet["surfaces"]["api"].update(status="skip"), "api has no passing"),
    (lambda packet: packet["surfaces"]["api"].update(workflow_attempt=2), "different attempts"),
    (lambda packet: packet["surfaces"]["cli"].update(cli_checksum="0" * 64), "CLI artifact"),
    (lambda packet: packet["cleanup"].update(owned_absent=False), "cleanup"),
    (lambda packet: packet["cleanup"].update(receipt_ref=""), "cleanup"),
    (lambda packet: packet["cases"]["live_browser_create"].update(readback_ref="private/ui/junit"), "readback"),
    (lambda packet: packet["cases"]["live_browser_create"]["attempts"].clear(), "attempts"),
    (lambda packet: packet["cases"]["live_browser_create"]["attempts"][-1].update(result="not_run", reason="fixture closed"), "last attempt"),
    (lambda packet: packet["cases"]["live_browser_create"]["attempts"][-1].update(workflow_run_id=7), "final attempt"),
    (lambda packet: packet["cases"]["live_browser_create"]["attempts"][-1].update(evidence_ref=""), "evidence_ref"),
])
def test_missing_failed_or_stale_evidence_cannot_be_review_ready(change, fragment):
    inputs = sample_packet()
    change(inputs)
    packet = build_packet(inputs)
    assert packet["status"] == "incomplete"
    assert any(fragment in problem for problem in packet["issues"]), packet["issues"]
    assert packet["packet_revision"].startswith("sha256:")


def test_prior_failed_and_skipped_attempts_are_preserved_after_verified_retry():
    inputs = sample_packet()
    case = inputs["cases"]["live_browser_create"]
    case["attempts"][:0] = [
        {"attempt_id": "40/1/create", "workflow_run_id": 40, "workflow_attempt": 1,
         "result": "fail", "expected": "Saved Product", "actual": "API refused write",
         "evidence_ref": "private/attempt/failed"},
        {"attempt_id": "41/1/create", "workflow_run_id": 41, "workflow_attempt": 1,
         "result": "skip", "expected": "Served browser", "actual": "Fixture unavailable",
         "reason": "Protected fixture not provisioned", "evidence_ref": "private/attempt/skipped"},
    ]
    packet = build_packet(inputs)
    assert packet["status"] == "review_ready"
    assert [entry["result"] for entry in packet["cases"]["live_browser_create"]["attempts"]] == ["fail", "skip", "pass"]
    case["attempts"][1].pop("reason")
    assert build_packet(inputs)["status"] == "incomplete"


def test_input_cannot_set_status_or_revision_or_hide_missing_evidence():
    inputs = sample_packet()
    inputs.update(status="review_ready", packet_revision="sha256:forged", reviewer_decision="accept")
    inputs["surfaces"].pop("ui")
    packet = build_packet(inputs)
    assert packet["status"] == "incomplete"
    assert packet["packet_revision"] != "sha256:forged"
    assert "reviewer_decision" not in packet


def test_non_mapping_inputs_rejected():
    with pytest.raises(TypeError):
        build_packet([])
