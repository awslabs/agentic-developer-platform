"""Export source-bound private reports without treating readbacks as live acceptance."""

from hashlib import sha256
import json
from pathlib import Path
import runpy
import sys
from types import SimpleNamespace

import pytest

import driver as live
from test_driver import case as driver_case, release_environment, source_bound_observations


case = driver_case
SCRIPTS = Path(__file__).resolve().parents[3] / "scripts"


@pytest.fixture
def release_case(case, monkeypatch):
    manifest, directory, client, run, result, config, binding, published, seen = case
    source_bound_observations(manifest, run, result)
    fixture = directory / "fixture.json"
    fixture.write_text(json.dumps(manifest))
    fixture.chmod(0o600)
    evidence = directory / "evidence"
    evidence.mkdir(mode=0o700)
    release_environment(monkeypatch, fixture, manifest["release_sha"])
    monkeypatch.setattr(live.httpx, "Client", lambda **kwargs: client)
    monkeypatch.setattr(client, "close", lambda: None)

    def external(command, **kwargs):
        if len(command) > 2 and command[2].endswith("adp_deployments.py"):
            body = {"deployment": "example", "gateway_url": "https://gateway.example.org/api"}
        elif command[0] == "node":
            body = {"result_id": result["result_id"], "revision": result["revision"],
                    "native_artifact": result["native"]["answer_artifact"],
                    "billing_pages": result["oracle"]["pages"], "answer_text": "Original example native answer"}
        else:
            body = {"status": "ok", "detail": result}
        return SimpleNamespace(returncode=0, stdout=json.dumps(body).encode())

    monkeypatch.setattr(live.subprocess, "run", external)
    arguments = [manifest["release_sha"], str(fixture), str(evidence)]
    monkeypatch.setattr(sys, "argv", ["qualify-finops-release", "run", *arguments])
    runpy.run_path(str(SCRIPTS / "qualify-finops-release.py"), run_name="__main__")
    monkeypatch.setattr(sys, "argv", ["emit-finops-qualification", *arguments])
    return SimpleNamespace(directory=evidence, fixture=fixture, seen=seen, result=result,
                           observation=next(evidence.glob("*-qualification.json")),
                           report=evidence / "qualification.json")


def emit():
    runpy.run_path(str(SCRIPTS / "emit-finops-qualification.py"), run_name="__main__")


def test_export_binds_readbacks_and_leaves_unobserved_acceptance_unresolved(release_case, capsys):
    state = release_case
    before = len(state.seen)
    emit()
    report = json.loads(state.report.read_text())
    assert report["component"] == "l5-finops" and report["service"] == "finops"
    assert report["source_revision"] == "a" * 40 and report["definition_revision"] == "b" * 40
    assert report["workflow_path"] == ".github/workflows/l5-services-release.yml"
    assert report["repository_id"] == 101 and report["environment"] == "test"
    assert (report["run_id"], report["run_attempt"]) == ("202", "3")
    assert report["correlation_id"] == "orch:00000000-0000-0000-0000-000000000001"
    assert report["contract_digest"] == "c" * 64 and report["requirements_revision"] == "F1"
    assert report["fixture_sha256"] == sha256(state.fixture.read_bytes()).hexdigest()
    assert report["native_observation"] == {
        "artifact": state.observation.name, "sha256": sha256(state.observation.read_bytes()).hexdigest()}
    assert {item["id"] for item in report["epic_criteria"]} == {f"EC-{index:02}" for index in range(1, 9)}
    assert "FO07/AC-04" in {item["id"] for item in report["story_criteria"]}
    assert "FO07/SCN-04" in {item["id"] for item in report["scenarios"]}
    assert "FO07/cleanup_reuse" in {item["id"] for item in report["live_checks"]}
    for section in ("epic_criteria", "story_criteria", "scenarios", "live_checks"):
        assert report[section]
        for item in report[section]:
            assert item["outcome"] == "incomplete" and item["evidence_verified"] is False
            assert item["unresolved_reasons"]
            assert f"{section}/{item['id']}:not-evaluated" in report["unresolved"]
    assert report["outcome"] == "incomplete" and report["native_ready"] is False
    assert report["validation"] == "schema_only" and report["live_acceptance"] == "not_evaluated"
    assert report["native_console_automation"] == "prohibited"
    assert "aws_native_console_automation:prohibited_security_restriction" in report["unresolved"]
    assert "documented_native_inquiry_api:not-qualified" in report["unresolved"]
    assert report["gateway_revision"] is None and "installed_release:not-observed" in report["unresolved"]
    assert {item["id"] for item in report["readbacks"]} == {
        "original_run", "native_answer", "billing", "clients", "publication"}
    assert all(item["outcome"] == "passed" and item["evidence_verified"] is True
               and item["verification_layer"] == "schema-only" for item in report["readbacks"])
    assert report["cleanup"]["status"] == "retained" and report["cleanup"]["evidence_verified"] is True
    assert report["cleanup"]["scope"] == "original_run_retention"
    assert report["product_run_id"] == report["cleanup"]["run_id"] == "run-example"
    assert state.report.stat().st_mode & 0o777 == 0o600
    assert state.directory.stat().st_mode & 0o777 == 0o700
    assert not (state.directory / "delivery-evidence.json").exists()
    output = state.report.read_text() + capsys.readouterr().out
    assert all(secret not in output for secret in (
        "Original example native answer", "submit-private", "read-private", "mcp-private"))
    assert not any(request.method == "POST" and request.url.path.endswith(("/runs", "/cancel"))
                   for request in state.seen[before:])
    original = state.report.read_bytes()
    with pytest.raises(FileExistsError):
        emit()
    assert state.report.read_bytes() == original


@pytest.mark.parametrize("key,value", [
    ("L5_SERVICE", "security"), ("L5_ENVIRONMENT", "unknown"),
    ("ADP_SOURCE_REVISION", "f" * 40), ("ADP_DEFINITION_REVISION", "f" * 40),
    ("GITHUB_WORKFLOW_REF", "example/adp/.github/workflows/other.yml@refs/heads/main"),
    ("GITHUB_RUN_ID", "0"), ("GITHUB_RUN_ATTEMPT", "0"),
    ("L5_FIXTURE_DIGEST", "f" * 64), ("ADP_CONTRACT_DIGEST", ""),
])
def test_export_rejects_wrong_release_identity_before_readback(release_case, monkeypatch, key, value):
    monkeypatch.setenv(key, value)
    before = len(release_case.seen)
    with pytest.raises(ValueError):
        emit()
    assert not release_case.report.exists() and len(release_case.seen) == before


@pytest.mark.parametrize("fault", ("native", "billing", "clients", "publication", "revision", "validation", "scope"))
def test_export_rejects_rehashed_falsified_observations(release_case, fault):
    state = release_case
    record = json.loads(state.observation.read_text())
    if fault == "native":
        record["native"]["conversation_id"] = "foreign-conversation"
    elif fault == "billing":
        record["billing"]["pages"][0]["version"] = "foreign-version"
    elif fault == "clients":
        record["clients"]["cli"] = "f" * 64
    elif fault == "publication":
        record["publication"]["product-context"]["receipt_id"] = "foreign-receipt"
    elif fault == "revision":
        record["revision"] += 1
    elif fault == "validation":
        record["validation"] = "independent-live"
    else:
        record["scope"]["product_id"] = "foreign-product"
    record.pop("sha256")
    record["sha256"] = sha256(json.dumps(record, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    state.observation.write_text(json.dumps(record))
    with pytest.raises(ValueError):
        emit()
    assert not state.report.exists()
    assert len([request for request in state.seen if request.method == "POST"
                and request.url.path.endswith("/runs")]) == 1


def test_export_rejects_changed_authoritative_result(release_case):
    release_case.result["revision"] += 1
    with pytest.raises(ValueError):
        emit()
    assert not release_case.report.exists()


def test_export_does_not_follow_existing_output_symlink(release_case, tmp_path):
    other = tmp_path / "other.json"
    other.write_text("unchanged")
    release_case.report.symlink_to(other)
    with pytest.raises(FileExistsError):
        emit()
    assert other.read_text() == "unchanged"
