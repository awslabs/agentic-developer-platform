"""Installed WA07 case selectors require independent private release readbacks."""

import json
import os
import re
import runpy
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic.script import ScriptDirectory

from l5_services import migrations
from l5_services.wa_native_evidence import verify_portable_native_reviews
from l5_services.wa_reuse_evidence import verify_wa_reuse
from l5_services.wa_safety_evidence import verify_wa_safety
from l5_services.installed_release_probe import (
    InstalledFixture, ReleaseProofError, require, verify_installed_release,
)
from l5_services.runtime_names import functions, queue_scope


def verify_wa_release(fixture, observed, *, now=None):
    scripts = ScriptDirectory(str(Path(migrations.__file__).parent))
    known_revisions = {revision.revision for revision in scripts.walk_revisions()}
    require(fixture.database_head in known_revisions, "wa_release_schema_head_invalid")
    require(any(revision.revision == "l5_wa_usage"
                for revision in scripts.walk_revisions(head=fixture.database_head)),
            "wa_release_schema_head_invalid")
    verify_installed_release(fixture, observed, now=now)
    factory = observed["worker"].get("worker_factory")
    require(isinstance(factory, str) and re.fullmatch(
        r"[a-zA-Z_][a-zA-Z_0-9.]*:[a-zA-Z_][a-zA-Z_0-9]*", factory),
        "wa_release_factory_missing")
    for name in ("worker", "repair"):
        require(observed[name].get("worker_factory") == factory, "wa_release_factory_changed")
        require(observed[name].get("function_url_status") == "not_found", "wa_release_public_url_unverified")
    receipt = observed.get("wa_release")
    require(isinstance(receipt, dict) and receipt.get("source") == "authenticated-runtime"
            and isinstance(receipt.get("evidence_ref"), str) and receipt["evidence_ref"],
            "wa_release_readback_missing")
    rows = receipt.get("prior_service_rows")
    require(isinstance(rows, list) and len(rows) == 2
            and all(isinstance(row, dict) and isinstance(row.get("service_id"), str) for row in rows)
            and {row["service_id"] for row in rows} == {"security", "devops"},
            "wa_release_prior_rows_missing")
    require(all(isinstance(row.get("before_sha256"), str)
                and re.fullmatch(r"[0-9a-f]{64}", row["before_sha256"])
                and row["before_sha256"] == row.get("after_sha256") for row in rows),
            "wa_release_prior_rows_changed")
    rollback = receipt.get("rollback")
    require(isinstance(rollback, dict) and rollback.get("admissions_disabled") is True
            and isinstance(rollback.get("retained_sha256"), str)
            and re.fullmatch(r"[0-9a-f]{64}", rollback["retained_sha256"])
            and rollback.get("retained_sha256") == rollback.get("readback_sha256"),
            "wa_release_rollback_unverified")


def _aws_read(label, *arguments):
    result = subprocess.run(
        ["adp-cred", "assume", "--service", "aws", "--label", label,
         "--exec", "aws", *arguments, "--output", "json"],
        capture_output=True, text=True, timeout=30, check=False,
    )
    if result.returncode != 0:
        raise ReleaseProofError("wa_release_provider_readback_failed")
    try:
        readback = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise ReleaseProofError("wa_release_provider_readback_invalid") from error
    require(isinstance(readback, dict), "wa_release_provider_readback_invalid")
    return readback


def _live_worker_readback(fixture, observed, label):
    identity = _aws_read(label, "sts", "get-caller-identity")
    role_name = fixture.role_arn.rsplit("/", 1)[-1]
    require(identity.get("Account") == fixture.account_id
            and isinstance(identity.get("Arn"), str)
            and identity["Arn"].startswith(
                f"arn:aws:sts::{fixture.account_id}:assumed-role/{role_name}/"),
            "wa_release_selected_role_changed")
    region, account, environment, service = queue_scope(fixture.queue_arn)
    require(account == fixture.account_id and service == "well_architected", "wa_release_selected_service_changed")
    for name, function in zip(("worker", "repair"), functions(environment, service), strict=True):
        arn = f"arn:aws:lambda:{region}:{fixture.account_id}:function:{function}"
        config = _aws_read(label, "lambda", "get-function-configuration", "--function-name", arn)
        part = observed.get(name)
        require(isinstance(part, dict) and all((
            config.get("FunctionArn") == arn,
            config.get("CodeSha256") == fixture.worker_code_sha256 == part.get("code_sha256"),
            config.get("RevisionId") == part.get("revision_id"),
            config.get("Role") == part.get("role_arn"),
            config.get("State") == part.get("state"),
            config.get("LastUpdateStatus") == part.get("last_update_status"),
            (config.get("Environment") or {}).get("Variables", {}).get("L5_WORKER_FACTORY") == part.get("worker_factory"),
        )), "wa_release_installed_worker_changed")
        url = subprocess.run(
            ["adp-cred", "assume", "--service", "aws", "--label", label,
             "--exec", "aws", "lambda", "get-function-url-config", "--function-name", arn],
            capture_output=True, text=True, timeout=30, check=False,
        )
        require(url.returncode != 0 and "ResourceNotFoundException" in url.stderr,
                "wa_release_public_url_unverified")


def test_ac_01():
    manifest = os.environ.get("L5_WA07_RELEASE_MANIFEST")
    readback = os.environ.get("L5_WA07_RELEASE_READBACK")
    if not manifest and not readback:
        pytest.skip("deployed-target: authorized private installed release evidence required")
    if not manifest or not readback:
        pytest.fail("both WA07 private release manifest and readback are required")
    label = os.environ.get("L5_WA07_AWS_CONNECTION")
    if not label:
        pytest.fail("selected WA07 AWS connection label is required for installed proof")
    fixture = InstalledFixture.from_manifest(Path(manifest))
    observed = json.loads(Path(readback).read_text())
    _live_worker_readback(fixture, observed, label)
    verify_wa_release(fixture, observed)


@pytest.fixture
def synthetic_observations(monkeypatch):
    monkeypatch.setattr(sys.modules[__name__], "verify_installed_release", lambda *args, **kwargs: None)
    return {
        "worker": {"worker_factory": "private.worker:factory", "function_url_status": "not_found"},
        "repair": {"worker_factory": "private.worker:factory", "function_url_status": "not_found"},
        "wa_release": {"source": "authenticated-runtime", "evidence_ref": "private/wa-release",
                       "prior_service_rows": [
                           {"service_id": service, "before_sha256": "a" * 64, "after_sha256": "a" * 64}
                           for service in ("security", "devops")],
                       "rollback": {"admissions_disabled": True,
                                    "retained_sha256": "b" * 64, "readback_sha256": "b" * 64}},
    }


def test_release_selector_requires_wa_specific_readback(synthetic_observations):
    fixture = SimpleNamespace(database_head="l5_wa_usage")
    verify_wa_release(fixture, synthetic_observations)
    synthetic_observations["repair"]["worker_factory"] = "foreign:factory"
    with pytest.raises(ReleaseProofError, match="wa_release_factory_changed"):
        verify_wa_release(fixture, synthetic_observations)
    synthetic_observations["repair"]["worker_factory"] = "private.worker:factory"
    synthetic_observations["worker"]["function_url_status"] = "present"
    with pytest.raises(ReleaseProofError, match="wa_release_public_url_unverified"):
        verify_wa_release(fixture, synthetic_observations)
    synthetic_observations["worker"]["function_url_status"] = "not_found"
    synthetic_observations["wa_release"]["prior_service_rows"][0]["after_sha256"] = "c" * 64
    with pytest.raises(ReleaseProofError, match="wa_release_prior_rows_changed"):
        verify_wa_release(fixture, synthetic_observations)
    synthetic_observations["wa_release"]["prior_service_rows"][0]["after_sha256"] = "a" * 64
    synthetic_observations["wa_release"]["rollback"]["readback_sha256"] = "c" * 64
    with pytest.raises(ReleaseProofError, match="wa_release_rollback_unverified"):
        verify_wa_release(fixture, synthetic_observations)


@pytest.mark.parametrize("head", ["l5_worker_repair", "l5_wa_collection_exclusions", "unknown", "head", "l5_wa_us"])
def test_release_selector_requires_correct_schema_head(synthetic_observations, head):
    with pytest.raises(ReleaseProofError, match="wa_release_schema_head_invalid"):
        verify_wa_release(SimpleNamespace(database_head=head), synthetic_observations)


def test_release_selector_accepts_current_migration_head(synthetic_observations):
    scripts = ScriptDirectory(str(Path(migrations.__file__).parent))
    head = scripts.get_current_head()
    assert "l5_wa_usage" in {revision.revision for revision in scripts.walk_revisions(head=head)}
    verify_wa_release(SimpleNamespace(database_head=head), synthetic_observations)


def test_release_selector_accepts_compatible_merge_but_not_its_sibling_branch(
        synthetic_observations, monkeypatch, tmp_path):
    directory = shutil.copytree(Path(migrations.__file__).parent, tmp_path / "migrations")
    head = ScriptDirectory(str(directory)).get_current_head()
    (directory / "versions" / "wa07_test_sibling.py").write_text(
        "revision = 'wa07_test_sibling'\ndown_revision = 'l5_worker_repair'\n")
    (directory / "versions" / "wa07_test_merge.py").write_text(
        f"revision = 'wa07_test_merge'\ndown_revision = ({head!r}, 'wa07_test_sibling')\n")
    monkeypatch.setattr(migrations, "__file__", str(directory / "__init__.py"))
    scripts = ScriptDirectory(str(directory))
    assert scripts.get_current_head() == "wa07_test_merge"
    verify_wa_release(SimpleNamespace(database_head=scripts.get_current_head()), synthetic_observations)
    with pytest.raises(ReleaseProofError, match="wa_release_schema_head_invalid"):
        verify_wa_release(SimpleNamespace(database_head="wa07_test_sibling"), synthetic_observations)


@pytest.mark.parametrize("changed_heads", [("database_heads",), ("image_heads",), ("database_heads", "image_heads")])
def test_release_selector_preserves_database_image_head_check(
        synthetic_observations, monkeypatch, tmp_path, changed_heads):
    from l5_services.installed_release_probe import verify_installed_release as verify_release

    monkeypatch.setattr(sys.modules[__name__], "verify_installed_release", verify_release)
    helpers = runpy.run_path(str(Path(__file__).parents[1] / "deployment" / "test_installed_probe.py"))
    head = ScriptDirectory(str(Path(migrations.__file__).parent)).get_current_head()
    data = {**helpers["fixture_data"](), "database_head": head}
    fixture = helpers["fixture"](tmp_path, data)
    observed = helpers["observations"]()
    observed["schema"].update(database_heads=[head], image_heads=[head])
    for name in ("worker", "repair"):
        observed[name].update(synthetic_observations[name])
    observed["wa_release"] = synthetic_observations["wa_release"]
    now = datetime.fromisoformat(observed["schema"]["observed_at"])
    verify_wa_release(fixture, observed, now=now)
    for field in changed_heads:
        observed["schema"][field] = ["l5_worker_repair"]
    with pytest.raises(ReleaseProofError, match="release_schema_changed"):
        verify_wa_release(fixture, observed, now=now)


def test_live_selector_does_not_pass_without_both_private_inputs(monkeypatch):
    monkeypatch.delenv("L5_WA07_RELEASE_MANIFEST", raising=False)
    monkeypatch.delenv("L5_WA07_RELEASE_READBACK", raising=False)
    with pytest.raises(pytest.skip.Exception, match="deployed-target"):
        test_ac_01()
    monkeypatch.setenv("L5_WA07_RELEASE_MANIFEST", "/private/nonexistent")
    with pytest.raises(pytest.fail.Exception, match="both WA07 private"):
        test_ac_01()
    monkeypatch.setenv("L5_WA07_RELEASE_READBACK", "/private/nonexistent")
    monkeypatch.delenv("L5_WA07_AWS_CONNECTION", raising=False)
    with pytest.raises(pytest.fail.Exception, match="selected WA07 AWS connection"):
        test_ac_01()


def test_live_readback_checks_selected_role_before_worker_access(monkeypatch):
    fixture = SimpleNamespace(account_id="000000000000",
                              role_arn="arn:aws:iam::000000000000:role/selected-role",
                              queue_arn="arn:aws:sqs:us-east-1:000000000000:adp-dev-l5-work",
                              worker_code_sha256="A" * 43 + "=")
    calls = []

    def provider_read(label, *arguments):
        calls.append(arguments)
        return {"Account": "000000000000", "Arn": "arn:aws:sts::000000000000:assumed-role/foreign/session"}

    monkeypatch.setattr(sys.modules[__name__], "_aws_read", provider_read)
    with pytest.raises(ReleaseProofError, match="wa_release_selected_role_changed"):
        _live_worker_readback(fixture, {}, "selected-connection")
    assert calls == [("sts", "get-caller-identity")]


def test_live_readback_requires_matching_code_and_absent_urls(monkeypatch):
    fixture = SimpleNamespace(account_id="000000000000",
                              role_arn="arn:aws:iam::000000000000:role/selected-role",
                              queue_arn="arn:aws:sqs:us-east-1:000000000000:adp-dev-l5-well-architected-work",
                              worker_code_sha256="A" * 43 + "=")
    calls = []

    def provider_read(label, *arguments):
        calls.append(arguments)
        if arguments[0] == "sts":
            return {"Account": fixture.account_id,
                    "Arn": f"arn:aws:sts::{fixture.account_id}:assumed-role/selected-role/session"}
        arn = arguments[-1]
        name = arn.rsplit("-", 1)[-1]
        return {"FunctionArn": arn, "CodeSha256": fixture.worker_code_sha256,
                "RevisionId": f"{name}-revision", "Role": f"role-{name}",
                "State": "Active", "LastUpdateStatus": "Successful",
                "Environment": {"Variables": {"L5_WORKER_FACTORY": "private.worker:factory"}}}

    def url_read(*args, **kwargs):
        return SimpleNamespace(returncode=254, stderr="ResourceNotFoundException")

    monkeypatch.setattr(sys.modules[__name__], "_aws_read", provider_read)
    monkeypatch.setattr(subprocess, "run", url_read)
    observed = {name: {"code_sha256": fixture.worker_code_sha256,
                       "revision_id": f"{name}-revision", "role_arn": f"role-{name}",
                       "state": "Active", "last_update_status": "Successful",
                       "worker_factory": "private.worker:factory"} for name in ("worker", "repair")}
    _live_worker_readback(fixture, observed, "selected-connection")
    assert len(calls) == 3
    observed["repair"]["code_sha256"] = "changed"
    with pytest.raises(ReleaseProofError, match="wa_release_installed_worker_changed"):
        _live_worker_readback(fixture, observed, "selected-connection")
    observed["repair"]["code_sha256"] = fixture.worker_code_sha256
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=254, stderr="AccessDenied"))
    with pytest.raises(ReleaseProofError, match="wa_release_public_url_unverified"):
        _live_worker_readback(fixture, observed, "selected-connection")


def test_ac_02():
    manifest = os.environ.get("L5_WA07_RELEASE_MANIFEST")
    readback = os.environ.get("L5_WA07_RELEASE_READBACK")
    portable = os.environ.get("L5_WA07_PORTABLE_CASES")
    if not manifest and not readback and not portable:
        pytest.skip("deployed-target: two authorized private native review fixtures required")
    if not all((manifest, readback, portable)):
        pytest.fail("release manifest, installed readback and two private native cases are required")
    label = os.environ.get("L5_WA07_AWS_CONNECTION")
    if not label:
        pytest.fail("selected WA07 AWS connection label is required for native proof")
    fixture = InstalledFixture.from_manifest(Path(manifest))
    observed = json.loads(Path(readback).read_text())
    cases = json.loads(Path(portable).read_text())
    _live_worker_readback(fixture, observed, label)
    verify_wa_release(fixture, observed)
    from l5_services.adapters.well_architected.source_evidence import read_broker_archive

    read = lambda *args: _aws_read(label, *args)
    verify_portable_native_reviews(cases, fixture, read,
                                  read_archive=lambda uri, reference: read_broker_archive(read, uri, reference))


def _aws_absent(label, *arguments):
    result = subprocess.run(
        ["adp-cred", "assume", "--service", "aws", "--label", label,
         "--exec", "aws", *arguments, "--output", "json"],
        capture_output=True, text=True, timeout=30, check=False,
    )
    return result.returncode != 0 and "ResourceNotFoundException" in result.stderr


def test_ac_03():
    manifest = os.environ.get("L5_WA07_RELEASE_MANIFEST")
    readback = os.environ.get("L5_WA07_RELEASE_READBACK")
    safety = os.environ.get("L5_WA07_SAFETY_CASE")
    if not manifest and not readback and not safety:
        pytest.skip("deployed-target: authorized private installed safety receipts required")
    if not all((manifest, readback, safety)):
        pytest.fail("release manifest, installed readback and private safety case are required")
    label = os.environ.get("L5_WA07_AWS_CONNECTION")
    if not label:
        pytest.fail("selected WA07 AWS connection label is required for safety proof")
    fixture = InstalledFixture.from_manifest(Path(manifest))
    observed = json.loads(Path(readback).read_text())
    case = json.loads(Path(safety).read_text())
    _live_worker_readback(fixture, observed, label)
    verify_wa_release(fixture, observed)
    verify_wa_safety(case, fixture, lambda *args: _aws_read(label, *args),
                     lambda *args: _aws_absent(label, *args))


def test_ac_04():
    manifest = os.environ.get("L5_WA07_RELEASE_MANIFEST")
    readback = os.environ.get("L5_WA07_RELEASE_READBACK")
    safety = os.environ.get("L5_WA07_SAFETY_CASE")
    reuse = os.environ.get("L5_WA07_REUSE_CASE")
    if not any((manifest, readback, safety, reuse)):
        pytest.skip("deployed-target: installed client and peer receipts required")
    if not all((manifest, readback, safety, reuse)):
        pytest.fail("release manifest, installed readback, safety and reuse cases are required")
    label = os.environ.get("L5_WA07_AWS_CONNECTION")
    if not label:
        pytest.fail("selected WA07 AWS connection label is required for reuse proof")
    fixture = InstalledFixture.from_manifest(Path(manifest))
    observed = json.loads(Path(readback).read_text())
    safety_case = json.loads(Path(safety).read_text())
    reuse_case = json.loads(Path(reuse).read_text())
    _live_worker_readback(fixture, observed, label)
    verify_wa_release(fixture, observed)
    verify_wa_safety(safety_case, fixture, lambda *args: _aws_read(label, *args),
                     lambda *args: _aws_absent(label, *args))
    verify_wa_reuse(reuse_case, fixture, safety_case)
