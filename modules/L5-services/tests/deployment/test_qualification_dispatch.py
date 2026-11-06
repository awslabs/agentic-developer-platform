"""Offline checks for selected release fixtures and driver boundaries."""

import importlib.util
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/qualification-dispatch.py"
SPEC = importlib.util.spec_from_file_location("qualification_dispatch", SCRIPT)
dispatch = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dispatch)
SOURCE = "a" * 40


@pytest.mark.parametrize("service", ["finops"])
@pytest.mark.parametrize("policy", [None, "", "disabled", "true", "Allowed", "unknown"])
def test_restricted_console_driver_stops_without_private_inputs(service, policy, tmp_path):
    environment = {**os.environ, "RUNNER_TEMP": str(tmp_path)}
    environment.pop("L5_AWS_CONSOLE_AUTOMATION", None)
    if policy is not None:
        environment["L5_AWS_CONSOLE_AUTOMATION"] = policy
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "check-console-policy", service],
        env=environment, capture_output=True, text=True, check=False,
    )
    assert result.returncode != 0
    assert "native_console_automation_disabled" in result.stderr
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("service", ["security", "devops", "transform", "well-architected", "well_architected"])
def test_documented_api_drivers_do_not_require_console_permission(service):
    dispatch.check_console_policy(service, "disabled")


@pytest.mark.parametrize("service", ["devops", "finops"])
def test_explicit_target_policy_allows_existing_console_driver(service):
    dispatch.check_console_policy(service, "allowed")


def test_private_json_materializes_without_logging_content_or_using_a_path_secret(tmp_path):
    payload = json.dumps({"authorization": {"source_sha": SOURCE}, "fixture_owner": "fictional",
                          "release": {"worker": SOURCE}})
    github_env = tmp_path / "github-env"
    github_env.touch()
    location = dispatch.materialize("security", SOURCE, payload, tmp_path, github_env)
    assert location.read_text() == payload
    assert location.stat().st_mode & 0o077 == 0
    assert location.parent.stat().st_mode & 0o077 == 0
    assert (location.parent / "evidence").stat().st_mode & 0o077 == 0
    recorded = github_env.read_text()
    assert f"PRIVATE_FIXTURE={location}\n" in recorded
    assert f"PRIVATE_EVIDENCE_DIR={location.parent / 'evidence'}\n" in recorded
    assert "L5_FIXTURE_DIGEST=" in recorded and "fictional" not in recorded
    with pytest.raises(FileExistsError):
        dispatch.materialize("security", SOURCE, payload, tmp_path, github_env)


@pytest.mark.parametrize("service", ["devops", "finops", "transform", "well_architected", "well-architected"])
def test_unavailable_sibling_driver_fails_before_any_provider_step(service, tmp_path):
    with pytest.raises(ValueError, match="Missing executable native qualification driver"):
        dispatch.check_driver(service, tmp_path)


@pytest.mark.parametrize("payload", [
    "", "not-json", "[]", json.dumps({"release": {"source_sha": "b" * 40}}),
    json.dumps({"release": {"worker": SOURCE, "source_sha": "b" * 40}}),
    json.dumps({"authorized": True}),
])
def test_missing_or_wrong_revision_private_fixture_fails_before_write(tmp_path, payload):
    github_env = tmp_path / "github-env"
    github_env.touch()
    with pytest.raises((ValueError, TypeError)):
        dispatch.materialize("finops", SOURCE, payload, tmp_path, github_env)
    assert not (tmp_path / "l5-services-qualification").exists()
    assert github_env.read_text() == ""


def test_rejects_checkout_or_public_private_fixture_directory(tmp_path):
    github_env = tmp_path / "github-env"
    github_env.touch()
    payload = json.dumps({"source_sha": SOURCE})
    with pytest.raises(ValueError, match="outside the checkout"):
        dispatch.materialize("security", SOURCE, payload, dispatch.ROOT, github_env)
    (tmp_path / "l5-services-qualification").mkdir(mode=0o755)
    with pytest.raises(ValueError, match="unsafe permissions"):
        dispatch.materialize("security", SOURCE, payload, tmp_path, github_env)
    assert github_env.read_text() == ""


def test_selected_driver_and_emitter_must_both_be_local_executables(tmp_path):
    driver = tmp_path / "qualify-devops-release.py"
    driver.write_text("#!/usr/bin/env python3\n")
    driver.chmod(0o700)
    with pytest.raises(ValueError, match="emit-devops-qualification.py"):
        dispatch.check_driver("devops", tmp_path)
    emitter = tmp_path / "emit-devops-qualification.py"
    emitter.write_text("#!/usr/bin/env python3\n")
    emitter.chmod(0o700)
    assert dispatch.check_driver("devops", tmp_path) == (driver.name, emitter.name)
    emitter.unlink()
    emitter.symlink_to(driver)
    with pytest.raises(ValueError, match="Missing executable"):
        dispatch.check_driver("devops", tmp_path)


def test_shared_dispatch_selects_existing_finops_service_entrypoints():
    assert dispatch.check_driver("finops") == ("qualify-finops-release.py", "emit-finops-qualification.py")


def bundle():
    return {
        "bundle_version": 1, "source_sha": SOURCE,
        "fixture": {"source_sha": SOURCE, "session_file": "fixture://clients/session.json",
                    "journal_file": "fixture://evidence/run.jsonl",
                    "cli_executable": "checkout://modules/gateway/cli/adp"},
        "files": {"clients/session.json": {"json": {"session": "fictional-session"}}},
        "directories": ["clients/home"],
        "environment": {"ADP_L5_FIXTURE_PRIMARY_TOKEN": "fictional-session"},
    }


@pytest.mark.parametrize("service", ["devops", "finops"])
def test_portable_sessions_resolve_without_operator_home_or_generated_evidence(tmp_path, capsys, monkeypatch, service):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    github_env = tmp_path / "github-env"
    github_env.touch(mode=0o600)
    location = dispatch.materialize(service, SOURCE, json.dumps(bundle()), tmp_path, github_env)
    fixture = json.loads(location.read_text())
    session = Path(fixture["session_file"])
    assert json.loads(session.read_text()) == {"session": "fictional-session"}
    assert session.is_relative_to(location.parent) and session.stat().st_mode & 0o777 == 0o600
    assert session.parent.stat().st_mode & 0o777 == 0o700
    assert (session.parent / "home").stat().st_mode & 0o777 == 0o700
    assert not Path(fixture["journal_file"]).exists()
    assert Path(fixture["journal_file"]).parent == location.parent / "evidence"
    assert Path(fixture["cli_executable"]) == dispatch.ROOT / "modules/gateway/cli/adp"
    assert f"L5_FIXTURE_DIGEST={hashlib.sha256(location.read_bytes()).hexdigest()}\n" in github_env.read_text()
    assert "ADP_L5_FIXTURE_PRIMARY_TOKEN=fictional-session\n" in github_env.read_text()
    assert capsys.readouterr().out == ""
    before = session.read_bytes()
    with pytest.raises(FileExistsError):
        dispatch.materialize(service, SOURCE, json.dumps(bundle()), tmp_path, github_env)
    assert session.read_bytes() == before


def test_github_masks_individual_session_and_restricts_environment_file(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    github_env = tmp_path / "github-env"
    github_env.touch(mode=0o644)
    value = bundle()
    value["environment"]["ADP_L5_FIXTURE_PRIMARY_TOKEN"] = "fictional%session"
    dispatch.materialize("devops", SOURCE, json.dumps(value), tmp_path, github_env)
    assert capsys.readouterr().out == "::add-mask::fictional%25session\n"
    assert github_env.stat().st_mode & 0o777 == 0o600
    assert "ADP_L5_FIXTURE_PRIMARY_TOKEN=fictional%session\n" in github_env.read_text()


@pytest.mark.parametrize("fault", ["escape", "absolute", "backslash", "overlap", "evidence", "fixture",
                                   "source", "inner_source", "system_env", "env_injection", "checkout_escape"])
def test_invalid_portable_bundle_cannot_write_inputs_or_environment(tmp_path, fault):
    value = bundle()
    if fault in {"escape", "absolute", "backslash", "overlap", "evidence", "fixture"}:
        path = {"escape": "../outside", "absolute": "/tmp/outside", "backslash": "clients\\session.json",
                "overlap": "clients", "evidence": "evidence/success.json", "fixture": "fixture.json/nested"}[fault]
        value["files"][path] = {"json": {}}
    elif fault == "source":
        value["source_sha"] = "b" * 40
    elif fault == "inner_source":
        value["fixture"]["source_sha"] = "b" * 40
    elif fault == "system_env":
        value["environment"] = {"HOME": "/tmp/foreign"}
    elif fault == "env_injection":
        value["environment"]["ADP_L5_FIXTURE_PRIMARY_TOKEN"] = "token\nPATH=/tmp"
    elif fault == "checkout_escape":
        value["fixture"]["cli_executable"] = "checkout://README.md"
    github_env = tmp_path / "github-env"
    github_env.touch()
    with pytest.raises(ValueError):
        dispatch.materialize("devops", SOURCE, json.dumps(value), tmp_path, github_env)
    assert not (tmp_path / "l5-services-qualification").exists()
    assert github_env.read_text() == ""


def test_existing_symlink_cannot_redirect_portable_session(tmp_path):
    directory = tmp_path / "l5-services-qualification/devops"
    directory.parent.mkdir(mode=0o700)
    directory.mkdir(mode=0o700)
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (directory / "clients").symlink_to(foreign, target_is_directory=True)
    github_env = tmp_path / "github-env"
    github_env.touch()
    with pytest.raises(ValueError, match="unsafe permissions"):
        dispatch.materialize("devops", SOURCE, json.dumps(bundle()), tmp_path, github_env)
    assert list(foreign.iterdir()) == []
    assert github_env.read_text() == ""


@pytest.mark.parametrize("service", ["finops"])
@pytest.mark.parametrize("policy", [None, "", "disabled", "true", "Allowed", "unknown"])
def test_full_ui_acceptance_needs_separate_protected_browser_policy(service, policy):
    with pytest.raises(ValueError, match="browser_automation_disabled"):
        dispatch.check_browser_policy(service, policy)
    dispatch.check_browser_policy(service, "allowed")


@pytest.mark.parametrize("policy", [None, "", "disabled", "true", "Allowed", "unknown", "allowed"])
def test_security_dispatch_selects_manual_readbacks_independent_of_browser_policy(policy):
    dispatch.check_browser_policy("security", policy)
    assert dispatch.check_driver("security") == ("qualify-security-readbacks.py", "emit-security-qualification.py")


def test_devops_sso_is_not_classified_as_management_console_automation():
    dispatch.check_console_policy("devops", "disabled")
    dispatch.check_browser_policy("devops", "disabled")
    assert dispatch.check_driver("devops") == ("qualify-devops-release.py", "emit-devops-qualification.py")
