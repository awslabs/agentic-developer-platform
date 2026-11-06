"""V05 recording tests use synthetic records, not a deployed Product."""

import json
import stat
from pathlib import Path

from tests.e2e.product_onboarding.product_v05_record import CASES, ResumeEvidence


def test_v05_records_all_surfaces_without_reusing_b02_inventory(tmp_path):
    source = "a" * 40
    recorder = ResumeEvidence(tmp_path, {
        "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "2", "GITHUB_SHA": source,
        "PRODUCT_EXPECTED_REVISION": source, "PRODUCT_BROWSER_WORKFLOW_REVISION": "b" * 40,
    })
    assert len(CASES) == 4
    for name, selector in CASES.items():
        record = recorder.case(selector)
        assert record is recorder.records[name]
        record.phases = {"setup": "passed", "call": "passed", "teardown": "passed"}
        record.reads.append({"status": 200, "body": {"id": "synthetic-session"}})
        record.cli_events.append({"exit_code": 0, "stdout": "synthetic", "stderr": ""})
        record.cleanup.append({"fixture_removed": True, "owned_absent": True})
    assert recorder.case("tests/e2e/new_ui/test_product_profile.py::test_product_create") is None
    report_path = Path(recorder.write())
    assert stat.S_IMODE(report_path.stat().st_mode) == 0o600
    report = json.loads(report_path.read_text())
    assert report["run_id"] == "product-123-2"
    for name in CASES:
        case = report["cases"][name]
        assert (case["read_count"], case["cli_count"], case["cleanup_count"]) == (1, 1, 1)
        assert case["status"] == "pass"
        for artifact in case["artifacts"].values():
            assert stat.S_IMODE(Path(artifact).stat().st_mode) == 0o600
