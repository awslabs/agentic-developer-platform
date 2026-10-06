"""Keep missing, skipped or unsuccessful database evidence from passing CI."""

import importlib.util
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / ".github/scripts/superplane_postgres_gate.py"
WORKFLOW = ROOT / ".github/workflows/superplane-domain-ci.yml"
spec = importlib.util.spec_from_file_location("superplane_postgres_gate", SCRIPT)
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


def report(tmp_path, *, missing=None, changed=None, status=None, prefix=""):
    root = ET.Element("testsuites")
    suite = ET.SubElement(root, "testsuite")
    for name in sorted(gate.REQUIRED_SUITES):
        if name == missing:
            continue
        case = ET.SubElement(
            suite,
            "testcase",
            classname=f"{prefix}workspace_provisioning.tests.{name}",
            name="test_authorized_retirement",
        )
        if name == changed:
            ET.SubElement(case, status)
    path = tmp_path / "domain.xml"
    ET.ElementTree(root).write(path)
    return path


@pytest.mark.parametrize("prefix", ["", "modules.domain-apps.superplane."])
def test_complete_postgres_evidence_passes(tmp_path, prefix):
    assert gate.require_retirement_postgres(report(tmp_path, prefix=prefix)) == 4


@pytest.mark.parametrize("missing", sorted(gate.REQUIRED_SUITES))
def test_each_required_suite_must_be_present(tmp_path, missing):
    with pytest.raises(
        ValueError, match=f"Missing retirement PostgreSQL suite: {missing}"
    ):
        gate.require_retirement_postgres(report(tmp_path, missing=missing))


@pytest.mark.parametrize("changed", sorted(gate.REQUIRED_SUITES))
@pytest.mark.parametrize("status", ["skipped", "failure", "error"])
def test_each_required_case_must_execute_successfully(tmp_path, changed, status):
    with pytest.raises(ValueError, match=f"did not pass: {changed}"):
        gate.require_retirement_postgres(
            report(tmp_path, changed=changed, status=status)
        )


def test_unrelated_results_do_not_substitute_for_retirement(tmp_path):
    path = report(tmp_path)
    path.write_text(
        path.read_text().replace("workspace_provisioning", "another_package")
    )
    with pytest.raises(ValueError, match="Missing retirement PostgreSQL suite"):
        gate.require_retirement_postgres(path)


def test_successful_pytest_exit_with_skipped_database_fixture_is_refused(tmp_path):
    name = "test_retirement_managed_access_postgres"
    fixture = tmp_path / f"{name}.py"
    fixture.write_text(
        "import pytest\n"
        "@pytest.fixture\n"
        "def database():\n"
        "    pytest.skip('database prerequisite unavailable')\n"
        "def test_cleanup(database):\n"
        "    raise AssertionError('skipped fixture must not run')\n"
    )
    actual = tmp_path / "actual.xml"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-c",
            "/dev/null",
            "--noconftest",
            f"--rootdir={tmp_path}",
            f"--junitxml={actual}",
            "--junit-prefix=workspace_provisioning.tests",
            str(fixture),
            "-q",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    path = report(tmp_path, missing=name)
    combined = ET.parse(path)
    combined.getroot().extend(ET.parse(actual).getroot())
    combined.write(path)
    with pytest.raises(ValueError, match=f"did not pass: {name}"):
        gate.require_retirement_postgres(path)


@pytest.mark.parametrize("content", [None, "not XML", "<testsuites/>"])
def test_command_fails_for_missing_malformed_or_empty_evidence(tmp_path, content):
    path = tmp_path / "domain.xml"
    if content is not None:
        path.write_text(content)
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(path)], capture_output=True, text=True
    )
    assert result.returncode != 0
    assert "passed, zero skipped" not in result.stdout


def test_workflow_requires_postgres_and_checks_the_same_report():
    lane = yaml.safe_load(WORKFLOW.read_text())["jobs"]["superplane-domain-tests"]
    steps = {step.get("name"): step for step in lane["steps"]}
    run = steps["Run domain module tests"]
    assert run["env"]["WORKSPACE_PROVISIONING_REQUIRE_POSTGRES"] == "1"
    assert '--junitxml="$RUNNER_TEMP/superplane-domain.xml"' in run["run"]
    check = steps["Require retirement PostgreSQL evidence"]
    assert check["if"] == run["if"] == "${{ matrix.component == 'domain' }}"
    assert check.get("continue-on-error", False) is False
    assert check["run"].strip() == (
        'python3 .github/scripts/superplane_postgres_gate.py "$RUNNER_TEMP/superplane-domain.xml"'
    )
    assert lane["steps"].index(check) > lane["steps"].index(run)
    assert "test_superplane_postgres_gate.py" in steps["Test CI scope selection"]["run"]
