"""Offline producer/reader checks for generated-only Identity proof reports."""

import base64
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path
from xml.sax.saxutils import quoteattr

import pytest
import yaml

from tests.e2e.tools.atlassian_phase import CASES, CLEANUP_CASE, phase_report, readback, write_report


SOURCE, DEFINITION = "a" * 40, "b" * 40
PINS = dict(
    source=SOURCE, definition=DEFINITION, gateway=SOURCE, run_id="314", attempt="2"
)
REPOSITORY, ACCOUNT = "aws-e/adp", "000000000000"


@pytest.mark.parametrize("failed", [False, True])
def test_extracted_identity_driver_reports_only_after_every_selected_test_succeeds(tmp_path, failed):
    log = tmp_path / "commands.jsonl"
    runner = tmp_path / "python"
    runner.write_text(
        f"#!{sys.executable}\nimport json,os,sys\n"
        "with open(os.environ['COMMAND_LOG'], 'a') as output: output.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "raise SystemExit(1 if os.environ['FAIL_TESTS'] == 'true' and sys.argv[1:3] == ['-m', 'pytest'] else 0)\n"
    )
    runner.chmod(0o700)
    result = subprocess.run(
        ["bash", str(Path(__file__).with_name("atlassian_identity_ci.sh")), "run"],
        env={**os.environ, "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"], "RUNNER_TEMP": str(tmp_path),
             "COMMAND_LOG": str(log), "FAIL_TESTS": str(failed).lower()}, capture_output=True, timeout=10,
    )
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert (result.returncode == 0) is not failed
    assert calls[0][:2] == ["-m", "pytest"]
    assert len([arg for arg in calls[0] if arg.startswith("tests/e2e/tools/test_atlassian_")]) == 10
    assert len(calls) == (1 if failed else 2)
    if not failed:
        assert calls[1][:3] == ["-m", "tests.e2e.tools.atlassian_phase", "report"]


def junit(*, omit=None, fail=None, cleanup=None):
    cases = {**CASES, "cleanup": CLEANUP_CASE}
    observation = {"phase": "verified", "uncertain": False, "remaining_resources": []} if cleanup is None else cleanup
    properties = '<properties><property name="adp_generated_cleanup" value=' + quoteattr(json.dumps(observation)) + '/></properties>'
    tests = "".join(
        f'<testcase name="{name}">{properties if scenario == "cleanup" else ""}{"<failure/>" if scenario == fail else ""}</testcase>'
        for scenario, name in cases.items()
        if scenario != omit
    )
    return f'<testsuites><testsuite tests="{len(cases)}">{tests}</testsuite></testsuites>'.encode()


def retained(tmp_path, *, report_change=None, junit_change=None, manifest_change=None):
    directory = tmp_path / "identity"
    directory.mkdir()
    (directory / "junit.xml").write_bytes(
        junit() if junit_change is None else junit_change
    )
    report = write_report(directory / "evidence.json", directory / "junit.xml", **PINS)
    if report_change:
        report_change(report)
        (directory / "evidence.json").write_text(json.dumps(report))
    files = {
        name: (directory / name).read_bytes() for name in ("evidence.json", "junit.xml")
    }
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w:gz") as packed:
        for name, content in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            packed.addfile(info, io.BytesIO(content))
    content = archive.getvalue()
    bucket = f"adp-dev-ci-evidence-{ACCOUNT}"
    prefix = f"artifacts/14/{REPOSITORY}/314/2/identity-proof/atlassian-identity-proof-314-2/"
    archive_sha = hashlib.sha256(content).hexdigest()
    artifact_key = prefix + archive_sha + ".tar.gz"
    manifest = {
        "schema_version": 1,
        "repository": REPOSITORY,
        "run_id": "314",
        "run_attempt": "2",
        "job": "identity-proof",
        "name": "atlassian-identity-proof-314-2",
        "retention_days": 14,
        "checked_out_revision": DEFINITION,
        "workflow_revision": DEFINITION,
        "artifact": {
            "bucket": bucket,
            "key": artifact_key,
            "sha256": archive_sha,
            "size": len(content),
            "version_id": "v1",
        },
        "files": [
            {
                "path": name,
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
            for name, data in files.items()
        ],
    }
    if manifest_change:
        manifest_change(manifest)
    manifest_bytes = json.dumps(manifest, sort_keys=True).encode()
    manifest_key = (
        prefix + hashlib.sha256(manifest_bytes).hexdigest() + ".manifest.json"
    )
    objects = {artifact_key: content, manifest_key: manifest_bytes}

    def fetch(actual_bucket, key):
        assert actual_bucket == bucket
        data = objects[key]
        return data, {
            "VersionId": "v1",
            "ContentLength": len(data),
            "ChecksumSHA256": base64.b64encode(hashlib.sha256(data).digest()).decode(),
        }

    return f"s3://{bucket}/{manifest_key}", fetch, objects


def verify(uri, fetch, **changes):
    return readback(
        uri,
        "v1",
        repository=REPOSITORY,
        account=ACCOUNT,
        fetch=fetch,
        **{**PINS, **changes},
    )


def test_controlled_report_has_explicit_provenance_and_unproven_live_status(tmp_path):
    uri, fetch, _ = retained(tmp_path)
    report = verify(uri, fetch)
    assert report["phase"] == "stage.identity-proof"
    assert report["scenarios"] == {scenario: "pass" for scenario in CASES}
    assert report["issue_number"] == 8446 and report["generated_cleanup"] == "verified"
    assert report["unresolved_operations"] == []
    assert report["cleanup_observation"] == {"phase": "verified", "uncertain": False, "remaining_resources": []}
    assert (
        report["controlled_status"] == "candidate"
        and report["live_status"] == "unproven"
    )
    assert report["source_revision"] == report["gateway_revision"] == SOURCE
    assert report["definition_revision"] == DEFINITION
    assert len(report["contract_sha256"]) == len(report["readiness_sha256"]) == 64


@pytest.mark.parametrize(
    "change",
    [
        lambda report: report.update(live_status="pass"),
        lambda report: report.update(controlled_status="pass"),
        lambda report: report.update(definition_revision=SOURCE),
        lambda report: report.update(gateway_revision=DEFINITION),
        lambda report: report["scenarios"].pop("SCN-03"),
        lambda report: report.update(contract_sha256="0" * 64),
        lambda report: report.update(generated_cleanup="pending"),
        lambda report: report.update(issue_number=8445),
    ],
)
def test_reader_refuses_tampered_or_promoted_report(tmp_path, change):
    uri, fetch, _ = retained(tmp_path, report_change=change)
    with pytest.raises(ValueError):
        verify(uri, fetch)


@pytest.mark.parametrize(
    "change",
    [
        lambda manifest: manifest.update(job="evaluate"),
        lambda manifest: manifest.update(workflow_revision=SOURCE),
        lambda manifest: manifest["files"].pop(),
        lambda manifest: manifest.update(missing_path_count=1),
    ],
)
def test_reader_refuses_wrong_manifest_or_missing_evidence(tmp_path, change):
    uri, fetch, _ = retained(tmp_path, manifest_change=change)
    with pytest.raises(ValueError):
        verify(uri, fetch)


@pytest.mark.parametrize(
    "change",
    [
        {"source": DEFINITION},
        {"definition": SOURCE},
        {"gateway": DEFINITION},
        {"run_id": "99"},
    ],
)
def test_reader_refuses_different_server_selected_revision_or_run(tmp_path, change):
    uri, fetch, _ = retained(tmp_path)
    with pytest.raises(ValueError):
        verify(uri, fetch, **change)


@pytest.mark.parametrize(
    "content",
    [
        junit(omit="SCN-02"),
        junit(omit="cleanup"),
        junit(fail="SCN-01"),
        junit(fail="cleanup"),
        junit().replace(
            b'<testsuite tests="4">', b'<testsuite tests="4" failures="1">'
        ),
        b"<broken",
    ],
)
def test_missing_failed_or_invalid_procedure_cannot_produce_report(tmp_path, content):
    with pytest.raises(ValueError):
        phase_report(content, **PINS)


@pytest.mark.parametrize("observation", [
    {}, {"phase": "verified", "uncertain": True, "remaining_resources": []},
    {"phase": "verified", "uncertain": False, "remaining_resources": ["provider"]},
    {"phase": "pending", "uncertain": False, "remaining_resources": []},
    {"phase": "verified", "uncertain": 0, "remaining_resources": []},
])
def test_passing_cleanup_case_requires_observed_absence_and_no_uncertainty(observation):
    with pytest.raises(ValueError):
        phase_report(junit(cleanup=observation), **PINS)


def test_passing_cleanup_case_without_observation_is_not_evidence():
    from xml.etree import ElementTree

    root = ElementTree.fromstring(junit())
    case = next(item for item in root.iter("testcase") if item.get("name") == CLEANUP_CASE)
    case.remove(case.find("properties"))
    with pytest.raises(ValueError):
        phase_report(ElementTree.tostring(root), **PINS)


def test_reader_refuses_changed_private_archive_or_version(tmp_path):
    uri, fetch, objects = retained(tmp_path)
    artifact_key = next(key for key in objects if key.endswith(".tar.gz"))
    objects[artifact_key] += b"changed"
    with pytest.raises(ValueError):
        verify(uri, fetch)
    with pytest.raises(ValueError):
        readback(uri, "v2", repository=REPOSITORY, account=ACCOUNT, fetch=fetch, **PINS)


def test_workflow_runs_generated_phase_separately_and_reads_private_result():
    path = Path(__file__).resolve().parents[3] / ".github/workflows/gateway-deploy.yml"
    workflow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
    job = workflow["jobs"]["identity-proof"]
    assert job["runs-on"] == "arc-runner-deploy"
    assert "refs/heads/main" in job["if"] and "stage.identity-proof" in job["if"]
    assert "needs.deploy-backend.result == 'success'" in job["if"]
    assert (
        "inputs.environment == 'dev'" in job["if"]
        and "inputs.product_cases == 'none'" in job["if"]
    )
    assert job["needs"] == ["deploy-backend"]
    assert (
        workflow["on"]["workflow_dispatch"]["inputs"]["proof_phase"]["default"]
        == "none"
    )
    steps = job["steps"]
    bound = next(
        step
        for step in steps
        if step.get("name") == "Require bound proof source and workflow definition"
    )
    identity = next(
        step for step in steps if step.get("name") == "Preserve runner identity for private readback"
    )
    assert 'test -n "$AWS_ROLE_ARN"' in identity["run"]
    assert 'test -f "$AWS_WEB_IDENTITY_TOKEN_FILE"' in identity["run"]
    assert "CI_EVIDENCE_RUNNER_ROLE_ARN" in identity["run"]
    suite = next(
        step
        for step in steps
        if step.get("name")
        == "Exercise the three controlled procedures and safety regressions"
    )
    upload = next(step for step in steps if step.get("id") == "publish-proof")
    reader = next(
        step
        for step in steps
        if step.get("name") == "Independently read back the bound controlled report"
    )
    assert (
        "git merge-base --is-ancestor" in bound["run"]
        and '"$SOURCE_REVISION" == "$GATEWAY_REVISION"' in bound["run"]
    )
    assert 'git diff --quiet "$SOURCE_REVISION" HEAD --' in bound["run"]
    assert (
        '"$ADP_CORRELATION" =~' in bound["run"]
        and '-z "$MANUAL_SOURCE"' in bound["run"]
    )
    assert ".github/workflows/gateway-deploy.yml" in bound["run"]
    assert suite["run"] == "bash tests/e2e/tools/atlassian_identity_ci.sh run"
    assert "tests/e2e/tools/atlassian_*" in bound["run"]
    driver = Path(__file__).with_name("atlassian_identity_ci.sh").read_text()
    assert all(
        name in driver
        for name in (
            "test_atlassian_lifecycle_procedure.py",
            "test_atlassian_capability_procedure.py",
            "test_atlassian_handoff_procedure.py",
        )
    )
    assert (
        steps.index(bound)
        < steps.index(suite)
        < steps.index(upload)
        < steps.index(reader)
    )
    assert upload["if"] == reader["if"] == "always()"
    assert upload["with"]["if-no-files-found"] == "error"
    assert (
        reader["env"]["CI_EVIDENCE_MANIFEST_URI"]
        == "${{ steps.publish-proof.outputs.manifest-uri }}"
    )
    assert "atlassian_phase readback" in reader["run"]
