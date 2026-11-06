"""Early failures must explain configuration gaps without revealing private inputs."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "scripts/check-release-prerequisites.py"
SPEC = importlib.util.spec_from_file_location("release_prerequisites", SCRIPT)
prerequisites = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(prerequisites)
SOURCE = "a" * 40


def environment(service="devops", fixture=None):
    return {
        "L5_BROWSER_AUTOMATION": "allowed",
        "PRIVATE_FIXTURE_JSON": json.dumps(fixture or {"source_sha": SOURCE}),
        "ADP_L5_INFRA_CONFIG_JSON": json.dumps({"services": {service: {"worker_factory": "example:factory"}}}),
    }


@pytest.mark.parametrize("service", ["security", "devops", "finops", "transform", "well_architected", "well-architected"])
def test_all_missing_inputs_are_listed_together(service):
    report = prerequisites.check(service, SOURCE, {})
    inputs = {issue["input"] for issue in report["blockers"]}
    name = service.replace("-", "_").upper()
    assert inputs == {f"ADP_L5_{name}_LIVE_CONFIG_JSON", "ADP_L5_INFRA_CONFIG_JSON"} | (
        {"ADP_L5_SECURITY_LIVE_CREDENTIALS_JSON"} if service == "security" else
        {"ADP_L5_TRANSFORM_IMAGE_CONFIG_JSON"} if service == "transform" else set())
    assert report["outcome"] == "blocked"
    assert all(issue["code"] == "missing_protected_input" for issue in report["blockers"])
    assert all("before retrying" in issue["next_action"] for issue in report["blockers"])


@pytest.mark.parametrize("fixture", [
    {"source_sha": SOURCE}, {"release_sha": SOURCE},
    {"authorization": {"source_sha": SOURCE}, "release": {"worker": SOURCE}},
    {"source_sha": SOURCE, "installed_release": {"source_revision": SOURCE}},
    {"bundle_version": 1, "source_sha": SOURCE, "fixture": {"release": {"worker": SOURCE}}},
])
def test_supported_source_bound_fixtures_do_not_require_unselected_service_secrets(fixture):
    report = prerequisites.check("devops", SOURCE, environment(fixture=fixture))
    assert report["outcome"] == "configured"
    assert report["live_acceptance"] == "not_evaluated"


@pytest.mark.parametrize("fixture", [
    {"authorization": {"owner": "private-person"}},
    {"source_sha": SOURCE, "release_sha": "b" * 40},
    {"authorization": {"source_sha": "b" * 40}, "release": {"worker": SOURCE}},
    {"source_sha": SOURCE, "installed_release": {"source_revision": "b" * 40}},
    {"bundle_version": 1, "source_sha": SOURCE, "fixture": {"source_sha": "b" * 40}},
    {"bundle_version": 1, "source_sha": "b" * 40, "fixture": {"source_sha": SOURCE}},
    {"bundle_version": True, "source_sha": SOURCE, "fixture": {"source_sha": SOURCE}},
])
def test_source_mismatch_never_repins_authorization(fixture):
    selected = environment(fixture=fixture)
    before = dict(selected)
    report = prerequisites.check("devops", SOURCE, selected)
    assert [item["code"] for item in report["blockers"]] == ["fixture_source_mismatch"]
    assert selected == before


@pytest.mark.parametrize("raw", ["private invalid JSON", "[]", "{}", '"private-value"', "x" * 262145])
def test_invalid_json_is_bounded_and_not_reflected_in_diagnostics(raw):
    selected = environment()
    selected["PRIVATE_FIXTURE_JSON"] = raw
    report = prerequisites.check("devops", SOURCE, selected)
    assert [item["code"] for item in report["blockers"]] == ["invalid_protected_input"]
    assert "private-value" not in json.dumps(report) and "private invalid JSON" not in json.dumps(report)


def test_selected_service_must_exist_in_infrastructure_config():
    report = prerequisites.check("well-architected", SOURCE, environment("security"))
    assert [item["code"] for item in report["blockers"]] == ["missing_service_infrastructure"]


@pytest.mark.parametrize("credentials", [{"PRIVATE_TOKEN": "private-token-value"}, {"PRIVATE_TOKEN": None}, {"bad name": "private-token-value"}])
def test_security_requires_a_private_credential_map(credentials):
    selected = environment("security")
    selected["ADP_L5_SECURITY_LIVE_CREDENTIALS_JSON"] = json.dumps(credentials)
    report = prerequisites.check("security", SOURCE, selected)
    assert report["outcome"] == ("configured" if credentials == {"PRIVATE_TOKEN": "private-token-value"} else "blocked")
    assert "private-token-value" not in json.dumps(report)


def test_cli_fails_before_any_provider_work_and_never_prints_private_input(tmp_path):
    selected = {**os.environ, **environment()}
    selected["PRIVATE_FIXTURE_JSON"] = '{"source_sha":"private-secret-value"}'
    result = subprocess.run([sys.executable, str(SCRIPT), "devops", SOURCE],
        cwd=tmp_path, env=selected, capture_output=True, text=True, check=False)
    assert result.returncode == 1
    assert "fixture_source_mismatch" in result.stdout
    assert "ADP_L5_DEVOPS_LIVE_CONFIG_JSON" in result.stdout
    assert "before artifact publication" in result.stdout
    assert "private-secret-value" not in result.stdout + result.stderr
    assert result.stderr == "" and list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("service", ["security", "devops", "well_architected"])
def test_standalone_install_needs_infrastructure_but_no_native_sessions(service):
    selected = environment(service)
    selected.pop("PRIVATE_FIXTURE_JSON")
    report = prerequisites.check(service, SOURCE, selected, operation="install")
    assert report["outcome"] == "configured" and report["live_acceptance"] == "not_evaluated"
    assert prerequisites.check(service, SOURCE, {}, operation="install")["outcome"] == "blocked"


def test_install_still_requires_real_transform_image_inputs():
    report = prerequisites.check("transform", SOURCE, environment("transform"), operation="install")
    assert [item["input"] for item in report["blockers"]] == ["ADP_L5_TRANSFORM_IMAGE_CONFIG_JSON"]


@pytest.mark.parametrize("service", sorted(prerequisites.SERVICES))
def test_publication_needs_no_runtime_or_native_fixture(service):
    selected = {"ADP_L5_TRANSFORM_IMAGE_CONFIG_JSON": '{"selected":true}'} if service == "transform" else {}
    report = prerequisites.check(service, SOURCE, selected, operation="publish")
    assert report["outcome"] == "configured" and report["live_acceptance"] == "not_evaluated"
    if service == "transform":
        missing = prerequisites.check(service, SOURCE, {}, operation="publish")
        assert [item["input"] for item in missing["blockers"]] == ["ADP_L5_TRANSFORM_IMAGE_CONFIG_JSON"]


@pytest.mark.parametrize("key", ["ADP_CORRELATION", "ADP_SOURCE_REVISION", "ADP_DEFINITION_REVISION", "ADP_CONTRACT_DIGEST"])
def test_publication_rejects_each_engine_authority_field(key):
    report = prerequisites.check("devops", SOURCE, {key: "supplied"}, operation="publish")
    assert [item["code"] for item in report["blockers"]] == ["publication_cannot_complete_delivery"]


@pytest.mark.parametrize("key", ["ADP_CORRELATION", "ADP_SOURCE_REVISION", "ADP_DEFINITION_REVISION", "ADP_CONTRACT_DIGEST"])
def test_install_cannot_be_used_as_engine_delivery(key):
    report = prerequisites.check("devops", SOURCE, {**environment(), key: "supplied"}, operation="install")
    assert [item["code"] for item in report["blockers"]] == ["installation_cannot_complete_delivery"]


def test_install_cli_accepts_operation_without_native_fixture(tmp_path):
    selected = {**os.environ, **environment("security")}
    selected.pop("PRIVATE_FIXTURE_JSON")
    for key in ("ADP_CORRELATION", "ADP_SOURCE_REVISION", "ADP_DEFINITION_REVISION", "ADP_CONTRACT_DIGEST"):
        selected.pop(key, None)
    result = subprocess.run([sys.executable, str(SCRIPT), "security", SOURCE, "--operation", "install"],
        cwd=tmp_path, env=selected, capture_output=True, text=True, check=False)
    assert result.returncode == 0 and "remain to be verified" in result.stdout
    assert list(tmp_path.iterdir()) == []


def test_devops_disabled_browser_requires_explicit_human_path_before_publication():
    selected = environment()
    selected["L5_BROWSER_AUTOMATION"] = "disabled"
    report = prerequisites.check("devops", SOURCE, selected)
    assert [item["code"] for item in report["blockers"]] == ["devops_human_evidence_required"]
    selected["PRIVATE_FIXTURE_JSON"] = json.dumps({"source_sha": SOURCE, "manual_ui": {"mode": "authenticated-human"}})
    assert prerequisites.check("devops", SOURCE, selected)["outcome"] == "configured"
