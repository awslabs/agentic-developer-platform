"""Release selector controls run offline; live artifacts remain unverified."""

import subprocess
import sys
from copy import deepcopy
from types import SimpleNamespace

import pytest

from tests.e2e.cli_uplift.product_onboarding_full import REGRESSION_CASES
from tests.e2e.cli_uplift.test_product_onboarding_full import release_record
from tests.e2e.product_onboarding import test_v10

SOURCE = "a" * 40
REQUIREMENTS = "b" * 40


def selected():
    record = release_record()
    events = []
    failures = []

    def observe_release():
        events.append("observe_release")
        return deepcopy(record["observed_release"])

    def load_ac_attempts():
        events.append("load_ac_attempts")
        return {case: deepcopy(record["attempts"][case]) for case in ("AC-01", "AC-02")}

    def run_regression(case):
        events.append(case)
        return {"case_id": case, **deepcopy(record["regressions"][case])}

    def disable_admissions():
        events.append("disable_admissions")
        return {"status": "disabled", "receipt_ref": "receipts/admission-disable"}

    def rollback_smoke(disabled):
        assert disabled["status"] == "disabled"
        events.append("rollback_smoke")
        return deepcopy(record["rollback"])

    def cleanup():
        events.append("cleanup")
        return deepcopy(record["cleanup"])

    def capture_ac03_attempt(_regressions, _rollback, _cleanup):
        events.append("capture_ac03_attempt")
        return deepcopy(record["attempts"]["AC-03"])

    run = {"fixture": {"owned_fixture": True, "source_revision": SOURCE,
                       "run_id": record["run_id"]},
           "artifacts": record["artifacts"],
           "expected_cli_hashes": record["artifacts"]["cli_hashes"],
           "deployment_source_revision": SOURCE, "requirements_revision": REQUIREMENTS,
           "selected_regressions": list(REGRESSION_CASES),
           "observe_release": observe_release, "load_ac_attempts": load_ac_attempts,
           "run_regression": run_regression, "disable_admissions": disable_admissions,
           "rollback_smoke": rollback_smoke, "cleanup": cleanup,
           "capture_ac03_attempt": capture_ac03_attempt,
           "record_failure": lambda scenario, failure: failures.append((scenario, failure))}
    return run, record, events, failures


def test_release_selector_runs_named_regressions_then_safe_rollback_and_cleanup():
    run, record, events, failures = selected()
    result = test_v10.run_cli_release_evidence(run, SOURCE, REQUIREMENTS)
    assert result["status"] == "passed" and result["cleanup"] == record["cleanup"]
    assert result["regression_cases"] == list(REGRESSION_CASES)
    assert events == ["observe_release", "load_ac_attempts", *REGRESSION_CASES,
                      "disable_admissions", "rollback_smoke", "cleanup", "capture_ac03_attempt"]
    assert failures == []


@pytest.mark.parametrize("fault", ("unknown_case", "missing_case", "stale_source", "wrong_hash"))
def test_release_selector_rejects_unsupported_or_stale_dispatch_before_side_effects(fault):
    run, _record, events, failures = selected()
    if fault == "unknown_case":
        run["selected_regressions"].append("unreviewed_write")
    elif fault == "missing_case":
        run["selected_regressions"].pop()
    elif fault == "stale_source":
        run["deployment_source_revision"] = "f" * 40
    else:
        run["expected_cli_hashes"] = {"adp": "f" * 64}
    with pytest.raises(ValueError, match="pinned artifacts"):
        test_v10.run_cli_release_evidence(run, SOURCE, REQUIREMENTS)
    assert events == failures == []


@pytest.mark.parametrize("fault", ("missing_prior", "failed_regression", "wrong_case",
                                    "admissions_open", "rollback_failed", "cleanup_failed",
                                    "stale_deployment", "failed_ac_attempt", "missing_ac03"))
def test_release_selector_records_failure_and_cleans_up(fault):
    run, record, events, failures = selected()
    if fault == "missing_prior":
        run["load_ac_attempts"] = lambda: events.append("load_ac_attempts") or {}
    elif fault in ("failed_regression", "wrong_case"):
        run["run_regression"] = lambda case: events.append(case) or {
            "case_id": "other" if fault == "wrong_case" else case,
            "run_id": record["run_id"], "result": "fail" if fault == "failed_regression" else "pass"}
    elif fault == "admissions_open":
        run["disable_admissions"] = lambda: events.append("disable_admissions") or {"status": "open"}
    elif fault == "rollback_failed":
        record["rollback"]["history_retained"] = False
    elif fault == "cleanup_failed":
        record["cleanup"]["owned_absent"] = False
    elif fault == "stale_deployment":
        record["observed_release"]["schema_head"] = "old_schema"
    elif fault == "failed_ac_attempt":
        record["attempts"]["AC-01"]["result"] = "skip"
    else:
        run["capture_ac03_attempt"] = lambda *_args: events.append("capture_ac03_attempt") or None
    with pytest.raises((ValueError, AssertionError)):
        test_v10.run_cli_release_evidence(run, SOURCE, REQUIREMENTS)
    assert events.count("cleanup") == 1 and len(failures) == 1
    assert failures[0][0] == "SCN-03"
    if fault in ("missing_prior", "failed_regression", "wrong_case", "admissions_open"):
        assert "rollback_smoke" not in events


def test_release_selector_is_collectable_and_requires_authorized_fixture(monkeypatch):
    selector = "tests/e2e/product_onboarding/test_v10.py::live_cli_release_evidence"
    result = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q", selector],
                            capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines().count(selector) == 1
    monkeypatch.setenv("PRODUCT_V10_LIVE_ENABLED", "1")
    monkeypatch.setenv("PRODUCT_V10_SOURCE_REVISION", SOURCE)
    monkeypatch.setenv("PRODUCT_V10_REQUIREMENTS_REVISION", REQUIREMENTS)
    request = SimpleNamespace(getfixturevalue=lambda _: pytest.fail("No authorized release fixture"))
    with pytest.raises(pytest.fail.Exception, match="No authorized release fixture"):
        test_v10.live_cli_release_evidence(request)
