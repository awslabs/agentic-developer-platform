"""Generated handoff cases reject inferred live passes and reversed dependencies."""

import json

import pytest

from tests.e2e.tools.atlassian_handoff import DESIGN_REVISION, MATRIX_PATH, review_handoff
from tests.e2e.tools.atlassian_oauth_probe import ProbeBlocked


REPORT = {
    "design_revision": DESIGN_REVISION,
    "source_revision": "a" * 40,
    "phase": "controlled-development-handoff",
    "live_status": "unproven",
    "scenarios": {"SCN-01": "pass", "SCN-02": "pass", "SCN-03": "pass"},
}


def test_generated_report_keeps_live_acceptance_pending():
    result = review_handoff(REPORT)
    assert result["controlled_status"] == "candidate"
    assert result["live_status"] == "unproven"
    assert set(result["activation_blockers"]) == {"AC-01", "AC-02", "AC-03"}
    assert review_handoff({**REPORT, "scenarios": {**REPORT["scenarios"], "SCN-01": "blocked"}})["controlled_status"] == "blocked"


@pytest.mark.parametrize("changes", [
    {"live_status": "pass"},
    {"source_revision": "branch-name"},
    {"design_revision": "b" * 40},
    {"scenarios": {"SCN-01": "pass", "SCN-02": "pass"}},
    {"scenarios": {"SCN-01": "pass", "SCN-02": "pass", "SCN-03": "live-pass"}},
])
def test_missing_or_misrepresented_case_is_rejected(changes):
    with pytest.raises(ProbeBlocked):
        review_handoff({**REPORT, **changes})


@pytest.mark.parametrize("mutate", [
    lambda matrix: matrix["forward_dependencies"][0].update(after=[8448]),
    lambda matrix: matrix["forward_dependencies"][1].update(deliverable="setup without recovery"),
    lambda matrix: matrix["forward_dependencies"][2].update(after=[8446]),
    lambda matrix: matrix["fixtures"]["licensed_consenting_account"].update(owner=""),
    lambda matrix: matrix["criteria"]["AC-01"].update(live="pass"),
    lambda matrix: matrix["criteria"]["AC-01"].update(scenario="SCN-02"),
])
def test_bad_dependency_fixture_or_live_mapping_is_rejected(tmp_path, mutate):
    matrix = json.loads(MATRIX_PATH.read_text())
    mutate(matrix)
    path = tmp_path / "matrix.json"
    path.write_text(json.dumps(matrix))
    with pytest.raises(ProbeBlocked):
        review_handoff(REPORT, path)
