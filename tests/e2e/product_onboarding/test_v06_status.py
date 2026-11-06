"""Offline SCN-02 controls; these synthetic reports are not deployed evidence."""

import json
from copy import deepcopy

import pytest

from tests.e2e.product_onboarding.test_v06 import (
    SOURCE_STATUS_SELECTORS,
    STATUS_ARTIFACTS,
    STATUS_PHASES,
    pinned_source_status_case,
    verify_live_source_status,
)

OBSERVED = "2026-10-20T00:00:00Z"
EXPIRED = "2026-10-20T00:15:00Z"


def status_fixture():
    case = pinned_source_status_case("a" * 40, "b" * 40, "example-run", OBSERVED, EXPIRED)
    source = {"coverage": "partial", "health": "healthy", "verified_at": OBSERVED,
              "resources": [{"native_id": "us-east-1", "observed_at": OBSERVED}]}
    attempts = {}
    for name, phase in STATUS_PHASES.items():
        restricted = phase == "restricted"
        check = {"progress": {"checks": [{"id": "environment", "status": (
            "missing" if restricted else "stale" if phase == "stale" else "complete"
        )}]}}
        attempts[name] = {
            "status": "passed", "run_id": case["run_id"], "source_revision": case["source_revision"],
            "clock": EXPIRED if name == "at_expiry" else OBSERVED,
            "artifacts": {surface: surface + ".json" for surface in STATUS_ARTIFACTS},
            "surfaces": {"source": {"coverage": "unavailable", "health": "unverified"} if restricted else source,
                         "binding": {"status": "restricted"} if restricted else {
                             "status": "degraded" if phase == "stale" else "healthy", "observed_at": OBSERVED,
                         },
                         "profile": deepcopy(check), "revision": None if restricted else deepcopy(check),
                         "session": deepcopy(check), "native_id": "us-east-1"},
        }
    report = {"run_id": case["run_id"], "source_revision": case["source_revision"],
              "requirements_revision": case["requirements_revision"], "owned_binding_ids": [],
              "observed_at": OBSERVED, "stale_after": EXPIRED,
              "provider_identity": {"account_id": "000000000000", "role_ref": "role/example"},
              "attempts": attempts,
              "cleanup": {"run_id": case["run_id"], "source_revision": case["source_revision"],
                          "unlinked_binding_ids": [], "remaining_owned_binding_ids": [],
                          "independent_provider_unchanged": True}}
    return case, report


def test_source_status_case_pins_observation_boundary_and_reviewed_selectors():
    case, report = status_fixture()
    assert case["scenario_id"] == "SCN-02" and case["selectors"] == SOURCE_STATUS_SELECTORS
    assert set(case["attempts"]) == set(STATUS_PHASES)
    assert all(status == "not_run" for status in case["attempts"].values())
    assert case["selector_fixture_type"]["browser_mock"] == "mocked_ui"
    assert verify_live_source_status(case, report)
    for observed, stale in (("2026-10-20T00:00:00+01:00", EXPIRED), (OBSERVED, OBSERVED),
                            (OBSERVED, "missing")):
        with pytest.raises(ValueError):
            pinned_source_status_case("a" * 40, "b" * 40, "example-run", observed, stale)
    with pytest.raises(ValueError, match="reviewed local"):
        pinned_source_status_case("a" * 40, "b" * 40, "example-run", OBSERVED, EXPIRED,
                                  {**SOURCE_STATUS_SELECTORS, "api": "wrong"})


@pytest.mark.parametrize("change", ("missing_browser", "skipped_revoked", "wrong_clock", "stale_ready",
                                         "leaked_role", "wrong_revision", "failed_cleanup"))
def test_source_status_report_refuses_incomplete_or_false_evidence(change):
    case, original = status_fixture()
    report = json.loads(json.dumps(original))
    if change == "missing_browser":
        del report["attempts"]["partial"]["artifacts"]["browser"]
    elif change == "skipped_revoked":
        report["attempts"]["revoked"]["status"] = "not_run"
    elif change == "wrong_clock":
        report["attempts"]["at_expiry"]["clock"] = OBSERVED
    elif change == "stale_ready":
        report["attempts"]["at_expiry"]["surfaces"]["profile"]["progress"]["checks"][0]["status"] = "complete"
    elif change == "leaked_role":
        report["attempts"]["revoked"]["surfaces"]["session"]["verified_environment"] = {"role_ref": "role/example"}
    elif change == "wrong_revision":
        report["source_revision"] = "c" * 40
    else:
        report["cleanup"]["remaining_owned_binding_ids"] = ["binding-left-behind"]
    with pytest.raises((ValueError, AssertionError)):
        verify_live_source_status(case, report)
