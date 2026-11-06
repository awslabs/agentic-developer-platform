"""V05 scenario selectors; collecting a case is not deployed execution."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
REQUIREMENTS = "docs/product-onboarding/delivery/verification-contract.md"
RESUME_SELECTORS = {
    "browser_ui_to_cli": "tests/e2e/new_ui/test_product_resume.py::test_resume_ui_to_cli",
    "browser_cli_to_ui": "tests/e2e/new_ui/test_product_resume.py::test_resume_cli_to_ui",
    "gateway_contract": "modules/gateway/tests/products/test_i07_cli.py::test_cli_resumes_ui_session_through_gateway",
}
PROGRESS_SELECTORS = {
    "browser": "tests/e2e/new_ui/test_product_resume.py::test_progress_math",
    "gateway_contract": "modules/gateway/tests/products/test_i07_progress.py::test_minimal_idea_and_later_optional_na_readback",
}
RECOVERY_SELECTORS = {
    "browser": "tests/e2e/new_ui/test_product_resume.py::test_resume_recovery",
    "gateway_contract": "modules/gateway/tests/products/test_i07_api.py::test_start_read_update_step_and_replay",
    "recovery_contract": "tests/e2e/product_onboarding/test_product_resume_recovery.py::test_recovery_refuses_duplicate_mutations_and_wrong_ownership",
}


def pinned_case(source_revision, requirements_revision, scenario_id, expected, selectors=None):
    if any(not isinstance(revision, str) or not re.fullmatch(r"[a-f0-9]{40}", revision)
           for revision in (source_revision, requirements_revision)):
        raise ValueError("Scenario source and requirements revisions must be full commit SHAs")
    if selectors is not None and selectors != expected:
        raise ValueError("Scenario must select the exact reviewed browser and Gateway cases")
    return {"scenario_id": scenario_id, "source_revision": source_revision,
            "requirements_revision": requirements_revision, "selectors": dict(expected),
            "status": "not_run"}


def pinned_resume_case(source_revision, requirements_revision, selectors=None):
    return pinned_case(source_revision, requirements_revision, "SCN-01", RESUME_SELECTORS, selectors)


def pinned_progress_case(source_revision, requirements_revision, selectors=None):
    return pinned_case(source_revision, requirements_revision, "SCN-02", PROGRESS_SELECTORS, selectors)


def pinned_recovery_case(source_revision, requirements_revision, selectors=None):
    return pinned_case(source_revision, requirements_revision, "SCN-03", RECOVERY_SELECTORS, selectors)


def collect_pinned_case(tmp_path, pin):
    source_revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    requirements_revision = subprocess.check_output(
        ["git", "log", "-1", "--format=%H", "--", REQUIREMENTS], cwd=ROOT, text=True,
    ).strip()
    record = pin(source_revision, requirements_revision)
    for selector in record["selectors"].values():
        result = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q", selector],
                                cwd=ROOT, text=True, capture_output=True, timeout=30, check=False)
        assert result.returncode == 0, result.stdout + result.stderr
        collected = selector.removeprefix("modules/gateway/")
        assert result.stdout.splitlines().count(collected) == 1, result.stdout
    path = tmp_path / (record["scenario_id"] + "-case.json")
    path.write_text(json.dumps(record, sort_keys=True) + "\n")
    assert json.loads(path.read_text()) == record
    return record


def collect_pinned_resume_case(tmp_path):
    return collect_pinned_case(tmp_path, pinned_resume_case)


def collect_pinned_progress_case(tmp_path):
    return collect_pinned_case(tmp_path, pinned_progress_case)


def collect_pinned_recovery_case(tmp_path):
    return collect_pinned_case(tmp_path, pinned_recovery_case)


def live_bidirectional_resume(tmp_path):
    """Pin SCN-01 selections; separate live jobs must produce the actual evidence."""
    assert collect_pinned_resume_case(tmp_path)["status"] == "not_run"


def live_progress_math(tmp_path):
    """Pin SCN-02 selections; this is not a live progress result."""
    assert collect_pinned_progress_case(tmp_path)["status"] == "not_run"


def live_resume_recovery(tmp_path):
    """Pin SCN-03 selections; collection cannot prove a deployed recovery."""
    assert collect_pinned_recovery_case(tmp_path)["status"] == "not_run"


def test_resume_scenario_collects_only_reviewed_surfaces(tmp_path):
    record = collect_pinned_resume_case(tmp_path)
    assert record["status"] == "not_run"
    assert record["selectors"] == RESUME_SELECTORS
    for surface in RESUME_SELECTORS:
        with pytest.raises(ValueError, match="exact reviewed browser and Gateway"):
            pinned_resume_case(record["source_revision"], record["requirements_revision"],
                               {**RESUME_SELECTORS, surface: "tests/unknown.py::test_missing"})
    with pytest.raises(ValueError, match="full commit SHAs"):
        pinned_resume_case(record["source_revision"][:-1], record["requirements_revision"])
    with pytest.raises(ValueError, match="full commit SHAs"):
        pinned_resume_case(record["source_revision"], "missing")


def test_progress_scenario_collects_only_reviewed_surfaces(tmp_path):
    record = collect_pinned_progress_case(tmp_path)
    assert record["scenario_id"] == "SCN-02" and record["status"] == "not_run"
    assert record["selectors"] == PROGRESS_SELECTORS
    for surface in PROGRESS_SELECTORS:
        with pytest.raises(ValueError, match="exact reviewed browser and Gateway"):
            pinned_progress_case(record["source_revision"], record["requirements_revision"],
                                 {**PROGRESS_SELECTORS, surface: "tests/unknown.py::test_missing"})
    with pytest.raises(ValueError, match="full commit SHAs"):
        pinned_progress_case(record["source_revision"], record["requirements_revision"][:-1])


def test_recovery_scenario_collects_only_reviewed_surfaces(tmp_path):
    record = collect_pinned_recovery_case(tmp_path)
    assert record["scenario_id"] == "SCN-03" and record["status"] == "not_run"
    assert record["selectors"] == RECOVERY_SELECTORS
    for surface in RECOVERY_SELECTORS:
        with pytest.raises(ValueError, match="exact reviewed browser and Gateway"):
            pinned_recovery_case(record["source_revision"], record["requirements_revision"],
                                 {**RECOVERY_SELECTORS, surface: "tests/unknown.py::test_missing"})
    with pytest.raises(ValueError, match="full commit SHAs"):
        pinned_recovery_case("unknown", record["requirements_revision"])
