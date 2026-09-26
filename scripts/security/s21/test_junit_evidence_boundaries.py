"""JUnit evidence consumers refuse DTDs and qualification evidence substitution."""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
QUALIFY = ROOT / "scripts/task-api/verify-qualified-report.py"
COLLECT = ROOT / "platform/scripts/operator/wave3/collect_fixture_pr.py"
VALID = '<testsuite tests="1"><testcase name="fixture"/></testsuite>'


@pytest.mark.parametrize("consumer", ["qualification", "collector"])
@pytest.mark.parametrize(
    "declaration",
    [
        None,
        '<!DOCTYPE testsuite [<!ENTITY name "fixture">]>',
        '<!DOCTYPE testsuite SYSTEM "http://127.0.0.1:1/never">',
    ],
)
def test_junit_consumers_reject_declarations(tmp_path, consumer, declaration):
    xml = VALID if declaration is None else declaration + VALID
    if declaration and "ENTITY" in declaration:
        xml = xml.replace('name="fixture"', 'name="&name;"')
    path = tmp_path / "junit.xml"
    path.write_text(xml)
    if consumer == "collector":
        command = [
            sys.executable,
            "-c",
            "import runpy,sys; print(runpy.run_path(sys.argv[1])['junit_passes'](sys.argv[2]))",
            str(COLLECT),
            str(path),
        ]
    else:
        report = tmp_path / "qualification.json"
        report.write_text(
            json.dumps(
                {
                    "criteria": {"fixture": {"status": "PASS"}},
                    "artifact_sha256": {
                        "junit.xml": hashlib.sha256(path.read_bytes()).hexdigest()
                    },
                    "test_runs": [{"report": "junit.xml", "passed": 1}],
                }
            )
        )
        command = [sys.executable, str(QUALIFY), "--report", str(report)]
    result = subprocess.run(
        command, capture_output=True, text=True, timeout=10, check=False
    )
    assert (result.returncode == 0) == (declaration is None), (
        result.stdout + result.stderr
    )
    if declaration is not None:
        assert "DTD declarations are not permitted" in result.stderr


@pytest.mark.parametrize("reference", ["unbound.xml", "../outside.xml"])
def test_qualification_junit_must_be_bound_by_validated_artifact_digest(
    tmp_path, reference
):
    directory = tmp_path / "report"
    directory.mkdir()
    original = directory / "retained.txt"
    original.write_text("unrelated retained evidence")
    candidate = directory / reference
    candidate.write_text(VALID)
    report = directory / "qualification.json"
    report.write_text(
        json.dumps(
            {
                "criteria": {"fixture": {"status": "PASS"}},
                "artifact_sha256": {
                    "retained.txt": hashlib.sha256(original.read_bytes()).hexdigest()
                },
                "test_runs": [{"report": reference, "passed": 1}],
            }
        )
    )
    result = subprocess.run(
        [sys.executable, str(QUALIFY), "--report", str(report)],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode != 0, "unbound JUnit evidence was accepted"
    assert '"status": "PASS"' not in result.stdout
