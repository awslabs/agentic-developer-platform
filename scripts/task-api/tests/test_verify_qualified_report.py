"""Frozen qualification must reject tampering in every Python mode."""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "verify-qualified-report.py"


def fixture_report(directory, mutation):
    attributes = {"tests": "2", "failures": "0", "errors": "0", "skipped": "0"}
    if mutation in ("failures", "errors", "skipped"):
        attributes[mutation] = "1"
    xml = "<testsuite " + " ".join(f'{k}="{v}"' for k, v in attributes.items()) + "/>"
    artifact = directory / "junit.xml"
    artifact.write_text(xml)
    report = {
        "criteria": {"fixture": {"status": "PASS"}},
        "artifact_sha256": {
            "junit.xml": hashlib.sha256(artifact.read_bytes()).hexdigest()
        },
        "test_runs": [{"report": "junit.xml", "passed": 2}],
    }
    if mutation == "empty_criteria":
        report["criteria"] = {}
    elif mutation == "failed_criterion":
        report["criteria"]["fixture"]["status"] = "FAIL"
    elif mutation == "empty_artifacts":
        report["artifact_sha256"] = {}
    elif mutation == "wrong_digest":
        report["artifact_sha256"]["junit.xml"] = "0" * 64
    elif mutation == "wrong_count":
        report["test_runs"][0]["passed"] = 3
    path = directory / "qualification.json"
    path.write_text(json.dumps(report))
    return path


@pytest.mark.parametrize("optimization", [[], ["-O"], ["-OO"]])
@pytest.mark.parametrize(
    "mutation",
    [
        None,
        "empty_criteria",
        "failed_criterion",
        "empty_artifacts",
        "wrong_digest",
        "wrong_count",
        "failures",
        "errors",
        "skipped",
    ],
)
def test_frozen_report_integrity(tmp_path, optimization, mutation):
    report = fixture_report(tmp_path, mutation)
    result = subprocess.run(
        [sys.executable, *optimization, str(SCRIPT), "--report", str(report)],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if mutation is None:
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["status"] == "PASS"
    else:
        assert result.returncode != 0, "tampered frozen qualification was accepted"
        assert '"status": "PASS"' not in result.stdout
