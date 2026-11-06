"""Synthetic V04 packets cannot substitute for deployed observations."""

from copy import deepcopy

import pytest

from tests.e2e.product_onboarding.product_v04_evidence import (
    BOLT_ID, CASES, DECISION_PROCEDURE, ISOLATION_SELECTORS, SCHEMA, SURFACE_SELECTORS,
    build_packet, review_binding,
)


def sample_inputs():
    plan, requirements, source = "a" * 40, "b" * 40, "c" * 40
    artifacts = {
        "api_image_digest": "sha256:" + "d" * 64,
        "frontend_manifest_sha256": "e" * 64,
        "installed_cli_sha256": "f" * 64,
        "api_workflow_revision": "1" * 40,
        "browser_workflow_revision": "2" * 40,
        "cli_workflow_revision": "3" * 40,
        "schema_head": "113_product_scope_guard",
    }
    cases = {}
    for criterion, (scenario, selector, surfaces) in CASES.items():
        attempts = []
        for surface in surfaces:
            reference = criterion.lower().replace("-", "") + "-" + surface
            attempt = {
                "attempt_id": reference + "-one", "run_id": "synthetic-run", "surface": surface,
                "test_ref": SURFACE_SELECTORS.get(criterion, {}).get(surface, selector),
                "result": "pass", "source_revision": source,
                "requirements_revision": requirements, "expected": "Scope isolated", "actual": "Scope isolated",
                "workflow_run_id": 42, "workflow_attempt": 1,
                "artifact_ref": "evidence/" + reference + "-result",
                "readback_ref": "evidence/" + reference + "-independent-api",
            }
            if surface == "browser":
                attempt["screenshot_ref"] = "evidence/" + reference + "-screenshot"
                attempt["trace_ref"] = "evidence/" + reference + "-trace"
            if surface == "cli":
                attempt["stdout_ref"] = "evidence/" + reference + "-stdout"
                attempt["installed_cli_sha256"] = artifacts["installed_cli_sha256"]
                attempt["exit_code"] = 0
            attempts.append(attempt)
        cases[criterion] = {"scenario_id": scenario, "test_ref": selector,
                             "expected": "No scope leak", "actual": "No scope leak", "attempts": attempts}
    return {
        "run_id": "synthetic-run", "plan_revision": plan,
        "requirements_revision": requirements, "source_revision": source,
        "artifacts": artifacts,
        "deployment": {"source_revision": source, **artifacts,
                       "receipt_ref": "receipts/release-one", "observation_ref": "receipts/observed-one"},
        "cases": cases,
        "cleanup": {"run_id": "synthetic-run", "status": "verified",
                    "receipt_ref": "receipts/cleanup-one", "owned_absent": True,
                    "original_projects_preserved": True, "reused_assets_preserved": True},
    }


def test_versioned_packet_maps_all_cases_to_independent_surface_attempts():
    inputs = sample_inputs()
    original = deepcopy(inputs)
    packet = build_packet(inputs)
    assert packet["evidence_schema"] == SCHEMA and packet["status"] == "review_ready"
    assert packet["issues"] == [] and packet["packet_revision"].startswith("sha256:")
    assert packet["review"] == {"ac_id": "AC-04", "scenario_id": "SCN-04", "bolt_id": BOLT_ID,
                                "procedure_ref": DECISION_PROCEDURE, "status": "pending",
                                "plan_revision": inputs["plan_revision"], "packet_revision": packet["packet_revision"]}
    assert review_binding(packet) == packet["review"]
    assert packet["run_id"] == "synthetic-run" and packet["source_revision"] == "c" * 40
    assert set(packet["cases"]) == set(CASES)
    assert {surface: (result["status"], len(result["attempts"]))
            for surface, result in packet["surfaces"].items()} == {
                "api": ("pass", 3), "browser": ("pass", 2), "cli": ("pass", 3),
            }
    for criterion, (scenario, selector, surfaces) in CASES.items():
        case = packet["cases"][criterion]
        assert case["scenario_id"] == scenario and case["test_ref"] == selector
        assert {attempt["surface"] for attempt in case["attempts"]} == set(surfaces)
        assert {attempt["attempt_id"] for attempt in case["attempts"]} == {
            attempt["attempt_id"] for attempt in original["cases"][criterion]["attempts"]
        }
        assert all(attempt["artifact_ref"] != attempt["readback_ref"] for attempt in case["attempts"])
    assert build_packet(deepcopy(inputs))["packet_revision"] == packet["packet_revision"]
    assert inputs == original


def test_packet_preserves_retries_and_drops_unrecognised_private_values():
    inputs = sample_inputs()
    attempt = inputs["cases"]["AC-02"]["attempts"][0]
    prior = {**attempt, "attempt_id": "previous-attempt", "result": "fail", "unexpected_secret": "private-value"}
    inputs["cases"]["AC-02"]["attempts"].insert(0, prior)
    inputs["undocumented_target_url"] = "https://private.example.invalid/"
    inputs["artifacts"]["unexpected_secret"] = "private-value"
    inputs["deployment"]["target_url"] = "https://private.example.invalid/"
    inputs["cleanup"]["unscoped_asset_name"] = "private-value"
    packet = build_packet(inputs)
    assert packet["status"] == "review_ready"
    assert [item["result"] for item in packet["cases"]["AC-02"]["attempts"][:2]] == ["fail", "pass"]
    assert [item["attempt_id"] for item in packet["surfaces"]["api"]["attempts"]
            if item["ac_id"] == "AC-02"] == ["previous-attempt", attempt["attempt_id"]]
    assert "private-value" not in str(packet) and "private.example.invalid" not in str(packet)


@pytest.mark.parametrize(("fault", "problem"), [
    ("missing_case", "inventory"),
    ("wrong_scenario", "binding differs"),
    ("missing_surface", "AC-01.browser has no attempts"),
    ("failed", "final attempt did not pass"),
    ("skipped", "final attempt did not pass"),
    ("not_run", "final attempt did not pass"),
    ("interrupted", "final attempt did not pass"),
    ("stale_source", "another run or revision"),
    ("stale_requirements", "another run or revision"),
    ("foreign_run", "another run or revision"),
    ("wrong_selector", "another run or revision"),
    ("wrong_isolation_surface", "another run or revision"),
    ("wrong_denial_surface", "another run or revision"),
    ("wrong_history_surface", "another run or revision"),
    ("stale_deployment", "differ from pinned artifacts"),
    ("stale_cli", "installed artifact differs"),
    ("bad_cli_exit", "command did not exit successfully"),
    ("missing_browser_trace", "trace_ref needs an opaque"),
    ("missing_api_readback", "readback_ref needs an opaque"),
    ("same_readback", "needs an independent readback"),
    ("leaked_target", "needs an opaque protected"),
    ("leaked_summary", "needs a sanitized outcome summary"),
    ("missing_workflow", "lacks a workflow run"),
    ("missing_cleanup", "cleanup needs an opaque"),
    ("failed_cleanup", "cleanup and original asset preservation"),
    ("asset_deleted", "cleanup and original asset preservation"),
    ("reused_deleted", "cleanup and original asset preservation"),
])
def test_missing_failed_stale_or_unsafe_required_evidence_is_incomplete(fault, problem):
    inputs = sample_inputs()
    complete_revision = build_packet(inputs)["packet_revision"]
    first = inputs["cases"]["AC-01"]["attempts"]
    cli = inputs["cases"]["AC-03"]["attempts"][-1]
    if fault == "missing_case":
        inputs["cases"].pop("AC-03")
    elif fault == "wrong_scenario":
        inputs["cases"]["AC-01"]["scenario_id"] = "SCN-02"
    elif fault == "missing_surface":
        first[:] = [attempt for attempt in first if attempt["surface"] != "browser"]
    elif fault in ("failed", "skipped", "not_run", "interrupted"):
        first[0]["result"] = {"failed": "fail", "skipped": "skip"}.get(fault, fault)
    elif fault == "stale_source":
        first[0]["source_revision"] = "0" * 40
    elif fault == "stale_requirements":
        first[0]["requirements_revision"] = "0" * 40
    elif fault == "foreign_run":
        first[0]["run_id"] = "different-run"
    elif fault == "wrong_selector":
        first[0]["test_ref"] = "case:unselected"
    elif fault == "wrong_isolation_surface":
        first[0]["test_ref"] = ISOLATION_SELECTORS["browser"]
    elif fault == "wrong_denial_surface":
        inputs["cases"]["AC-02"]["attempts"][0]["test_ref"] = SURFACE_SELECTORS["AC-02"]["browser"]
    elif fault == "wrong_history_surface":
        inputs["cases"]["AC-03"]["attempts"][0]["test_ref"] = SURFACE_SELECTORS["AC-02"]["api"]
    elif fault == "stale_deployment":
        inputs["deployment"]["api_image_digest"] = "sha256:" + "0" * 64
    elif fault == "stale_cli":
        cli["installed_cli_sha256"] = "0" * 64
    elif fault == "bad_cli_exit":
        cli["exit_code"] = 5
    elif fault == "missing_browser_trace":
        first[1].pop("trace_ref")
    elif fault == "missing_api_readback":
        first[0].pop("readback_ref")
    elif fault == "same_readback":
        first[0]["readback_ref"] = first[0]["artifact_ref"]
    elif fault == "leaked_target":
        first[0]["artifact_ref"] = "https://private.example.invalid/result"
    elif fault == "leaked_summary":
        first[0]["actual"] = "https://private.example.invalid/result"
    elif fault == "missing_workflow":
        first[0]["workflow_attempt"] = 0
    elif fault == "missing_cleanup":
        inputs["cleanup"].pop("receipt_ref")
    elif fault == "failed_cleanup":
        inputs["cleanup"]["status"] = "failed"
    elif fault == "asset_deleted":
        inputs["cleanup"]["original_projects_preserved"] = False
    else:
        inputs["cleanup"]["reused_assets_preserved"] = False
    packet = build_packet(inputs)
    assert packet["status"] == "incomplete" and any(problem in issue for issue in packet["issues"])
    assert packet["packet_revision"] != complete_revision
    assert packet["review"]["status"] == "not_ready"
    assert "private.example.invalid" not in str(packet)


def test_forged_status_revision_and_human_decision_are_ignored():
    inputs = sample_inputs()
    inputs["cases"]["AC-01"]["attempts"][0]["result"] = "fail"
    inputs.update({"status": "review_ready", "issues": [], "packet_revision": "sha256:forged",
                   "review": {"status": "accepted", "packet_revision": "sha256:forged"}})
    packet = build_packet(inputs)
    assert packet["status"] == "incomplete"
    assert packet["packet_revision"] != "sha256:forged"
    assert packet["review"] == {"ac_id": "AC-04", "scenario_id": "SCN-04", "bolt_id": BOLT_ID,
                                "procedure_ref": DECISION_PROCEDURE, "plan_revision": inputs["plan_revision"],
                                "status": "not_ready", "packet_revision": packet["packet_revision"]}


@pytest.mark.parametrize("fault", ["failed", "missing", "cleanup", "stale_plan", "altered_attempt",
                                  "wrong_revision", "wrong_bolt", "forged_decision"])
def test_scope_decision_binding_rejects_incomplete_or_changed_packets(fault):
    inputs = sample_inputs()
    if fault == "failed":
        inputs["cases"]["AC-02"]["attempts"][0]["result"] = "fail"
    elif fault == "missing":
        inputs["cases"]["AC-03"]["attempts"] = []
    elif fault == "cleanup":
        inputs["cleanup"]["status"] = "failed"
    packet = build_packet(inputs)
    if fault == "stale_plan":
        packet["plan_revision"] = "0" * 40
    elif fault == "altered_attempt":
        packet["cases"]["AC-01"]["attempts"][0]["actual"] = "changed after packet assembly"
    elif fault == "wrong_revision":
        packet["review"]["packet_revision"] = "sha256:" + "0" * 64
    elif fault == "wrong_bolt":
        packet["review"]["bolt_id"] = "B04"
    elif fault == "forged_decision":
        packet["review"]["status"] = "accepted"
    with pytest.raises(ValueError, match="Incomplete scope evidence|binding differs"):
        review_binding(packet)
