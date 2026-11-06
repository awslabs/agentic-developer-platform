"""Offline recovery scenario contracts; synthetic attempts are not live evidence."""

import json
from copy import deepcopy

import pytest

from tests.e2e.product_onboarding.test_v06 import (
    LIVE_SURFACES,
    RECOVERY_PHASES,
    RECOVERY_SELECTORS,
    live_source_recovery,
    pinned_source_recovery_case,
    verify_live_source_recovery,
)

START = "2026-10-20T00:00:00Z"


def recovery_fixture():
    case = pinned_source_recovery_case("a" * 40, "b" * 40, "example-run", START, ["owned-1"], "preserved-1")
    attempts = {}
    for index, phase in enumerate(RECOVERY_PHASES):
        attempts[phase] = {
            "status": "passed", "run_id": case["run_id"], "source_revision": case["source_revision"],
            "observed_at": f"2026-10-20T00:00:{index + 1:02d}Z",
            "artifacts": {surface: f"{phase}-{surface}.json" for surface in LIVE_SURFACES},
        }
    attempts["interrupted"].update({"resource_cursor": "2", "resource_ids": ["35"], "page_count": 1})
    attempts["resumed"].update({"requested_cursor": "2", "resource_ids": ["36"], "page_count": 2})
    attempts["reverified"].update({"binding_id": "owned-1", "binding_ids": ["owned-1", "preserved-1"],
                                   "selected_native_id": "36", "binding_native_id": "36",
                                   "verify_receipt_ids": ["operation-1", "operation-1"], "verify_attempts": 2})
    attempts["unlinked"].update({"unlinked_binding_ids": ["owned-1"], "remaining_binding_ids": ["preserved-1"],
                                 "confirmed_values_before": {"purpose": "Coordinate small releases"},
                                 "confirmed_values_after": {"purpose": "Coordinate small releases"},
                                 "cleanup_attempts": 1})
    report = {
        "run_id": case["run_id"], "source_revision": case["source_revision"],
        "requirements_revision": case["requirements_revision"], "owned_binding_ids": case["owned_binding_ids"],
        "preserved_binding_id": case["preserved_binding_id"], "started_at": START,
        "finished_at": "2026-10-20T00:00:05Z", "attempts": attempts,
        "independent_provider": {"artifact_ref": "provider-readback.json", "before": {"repository_revision": "r1"},
                                 "after": {"repository_revision": "r1"}, "calls": ["GET repository", "GET repository"]},
        "cleanup": {"run_id": case["run_id"], "source_revision": case["source_revision"],
                    "unlinked_binding_ids": ["owned-1"], "remaining_owned_binding_ids": [],
                    "independent_provider_unchanged": True,
                    "recovery_ref": "protected-recovery-receipt.json", "owned_readback_ref": "protected-owned-readback.json"},
    }
    return case, report


def test_recovery_case_pins_bounded_run_and_reviewed_selectors():
    case, report = recovery_fixture()
    assert case["scenario_id"] == "SCN-03" and case["selectors"] == RECOVERY_SELECTORS
    assert case["selector_fixture_type"]["browser_mock"] == "mocked_ui"
    assert case["attempts"] == dict.fromkeys(RECOVERY_PHASES, "not_run")
    assert case["max_pages"] == 2 and case["max_attempts"] == 3 and case["deadline_seconds"] == 300
    assert verify_live_source_recovery(case, report)
    for owned, preserved, clock in (([], "preserved-1", START), (["owned-1"], "owned-1", START),
                                    (["owned-1"], "preserved-1", "not-utc")):
        with pytest.raises(ValueError):
            pinned_source_recovery_case("a" * 40, "b" * 40, "example-run", clock, owned, preserved)
    with pytest.raises(ValueError, match="reviewed local"):
        pinned_source_recovery_case("a" * 40, "b" * 40, "example-run", START, ["owned-1"], "preserved-1",
                                    {**RECOVERY_SELECTORS, "api": "wrong"})


@pytest.mark.parametrize("change", ("missing_browser", "skipped_phase", "wrong_run", "late_finish", "reverse_clock",
                                         "duplicate_resource", "bad_cursor", "extra_page", "duplicate_binding",
                                         "changed_receipt", "shared_removed", "confirmed_lost", "missing_owned_cleanup",
                                         "write_provider", "provider_changed", "missing_recovery_receipt", "too_many_retries"))
def test_recovery_report_rejects_incomplete_or_false_evidence(change):
    case, original = recovery_fixture()
    report = deepcopy(original)
    first, resumed, verified, unlinked = (report["attempts"][phase] for phase in RECOVERY_PHASES)
    if change == "missing_browser":
        del first["artifacts"]["browser"]
    elif change == "skipped_phase":
        resumed["status"] = "not_run"
    elif change == "wrong_run":
        verified["run_id"] = "other-run"
    elif change == "late_finish":
        report["finished_at"] = "2026-10-20T00:06:00Z"
    elif change == "reverse_clock":
        resumed["observed_at"] = START
    elif change == "duplicate_resource":
        resumed["resource_ids"] = ["35"]
    elif change == "bad_cursor":
        resumed["requested_cursor"] = "wrong"
    elif change == "extra_page":
        resumed["page_count"] = 3
    elif change == "duplicate_binding":
        verified["binding_ids"] = ["owned-1", "owned-1", "preserved-1"]
    elif change == "changed_receipt":
        verified["verify_receipt_ids"][1] = "operation-2"
    elif change == "shared_removed":
        unlinked["remaining_binding_ids"] = []
    elif change == "confirmed_lost":
        unlinked["confirmed_values_after"] = {}
    elif change == "missing_owned_cleanup":
        report["cleanup"]["remaining_owned_binding_ids"] = ["owned-1"]
    elif change == "write_provider":
        report["independent_provider"]["calls"] = ["POST repository"]
    elif change == "provider_changed":
        report["independent_provider"]["after"] = {"repository_revision": "r2"}
    elif change == "missing_recovery_receipt":
        report["cleanup"]["recovery_ref"] = ""
    else:
        unlinked["cleanup_attempts"] = 4
    with pytest.raises((ValueError, AssertionError)):
        verify_live_source_recovery(case, report)


@pytest.mark.parametrize("change", ("shared_artifact", "unhashable_resource", "wrong_native_id", "non_numeric_cleanup"))
def test_recovery_rejects_malformed_or_crossed_evidence(change):
    case, report = recovery_fixture()
    attempts = report["attempts"]
    if change == "shared_artifact":
        attempts["interrupted"]["artifacts"]["browser"] = attempts["interrupted"]["artifacts"]["api"]
    elif change == "unhashable_resource":
        attempts["resumed"]["resource_ids"] = [{"native_id": "36"}]
    elif change == "wrong_native_id":
        attempts["reverified"]["binding_native_id"] = "not-discovered"
    else:
        attempts["unlinked"]["cleanup_attempts"] = "1"
    with pytest.raises(ValueError):
        verify_live_source_recovery(case, report)


def test_live_recovery_selector_requires_protected_deployed_report(tmp_path, monkeypatch):
    monkeypatch.delenv("PRODUCT_SOURCE_RECOVERY_LIVE_ENABLED", raising=False)
    with pytest.raises(pytest.skip.Exception, match="deployed recovery"):
        live_source_recovery(tmp_path)
    assert not (tmp_path / "v06-source-recovery-case.json").exists()
    _, report = recovery_fixture()
    path = tmp_path / "unprotected.json"
    path.write_text(json.dumps(report))
    path.chmod(0o644)
    monkeypatch.setenv("PRODUCT_SOURCE_RECOVERY_LIVE_ENABLED", "1")
    monkeypatch.setenv("PRODUCT_SOURCE_RECOVERY_REPORT_PATH", str(path))
    monkeypatch.setenv("PRODUCT_SOURCES_RUN_ID", "example-run")
    with pytest.raises(ValueError, match="not be group or world readable"):
        live_source_recovery(tmp_path)
