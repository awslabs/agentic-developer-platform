"""Executable evidence and orchestration contracts; never access live targets."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def module(name):
    spec = importlib.util.spec_from_file_location(
        name, ROOT / ".github/scripts" / f"{name}.py"
    )
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


report = module("regression_report")
gateway = module("regression_gateway_config")


def workflow(name):
    return yaml.safe_load((ROOT / ".github/workflows" / name).read_text())


def triggers(doc):
    return doc.get("on", doc.get(True, {}))


@pytest.mark.parametrize(
    "schedule,selected", [(report.DAILY_CRON, "daily"), (report.WEEKLY_CRON, "weekly")]
)
def test_delayed_schedule_uses_event_expression_not_current_weekday(schedule, selected):
    assert report.profile("schedule", schedule, "ignored") == selected


def test_unknown_profile_and_schedule_are_rejected():
    with pytest.raises(ValueError):
        report.profile("schedule", "unrecognized")
    with pytest.raises(ValueError):
        report.profile("workflow_dispatch", "", "unrecognized")


@pytest.mark.parametrize("state", ["failure", "skipped", "cancelled", "missing", None])
def test_required_failure_or_absence_cannot_be_hidden(state):
    assert (
        report.aggregate(
            {"api": {"result": state}, "ui": {"result": "success"}}, ["api", "ui"]
        )[1]
        == 1
    )
    assert report.aggregate({}, ["api"])[1] == 1
    assert report.aggregate({}, [])[1] == 1


def evidence(tmp_path, expected, results):
    inventory = tmp_path / "inventory.json"
    inventory.write_text(json.dumps(expected))
    root = ET.Element("testsuites", tests="100", failures="0")
    suite = ET.SubElement(root, "testsuite")
    for case_id, outcome in results:
        case = ET.SubElement(suite, "testcase", name=case_id)
        props = ET.SubElement(case, "properties")
        ET.SubElement(props, "property", name="adp_case_id", value=case_id)
        if outcome:
            ET.SubElement(case, outcome)
    path = tmp_path / "junit.xml"
    ET.ElementTree(root).write(path)
    return path, inventory


@pytest.mark.parametrize(
    "results",
    [
        [],
        [("a", "skipped")],
        [("a", "failure")],
        [("a", "error")],
        [("other", "")],
        [("a", ""), ("a", "")],
    ],
)
def test_incomplete_junit_cannot_pass_even_with_successful_xml_totals(
    tmp_path, results
):
    path, inventory = evidence(tmp_path, ["a"], results)
    assert report.junit(path, inventory)["status"] == "incomplete"


def test_complete_junit_passes(tmp_path):
    path, inventory = evidence(tmp_path, ["a", "b"], [("a", ""), ("b", "")])
    result = report.junit(path, inventory)
    assert result["status"] == "pass"
    assert result["executed"] == 2


@pytest.mark.parametrize("expected", [[], ["a", "a"], {}])
def test_invalid_inventory_cannot_pass(tmp_path, expected):
    path, inventory = evidence(tmp_path, expected, [])
    with pytest.raises(ValueError):
        report.junit(path, inventory)


def test_real_pytest_plugin_records_selection_and_detects_skipped_execution(tmp_path):
    (tmp_path / "test_fixture.py").write_text(
        "import pytest\ndef test_pass(): pass\n"
        "@pytest.mark.skip(reason='missing fixture')\ndef test_blocked(): pass\n"
    )
    inventory = tmp_path / "inventory.json"
    junit = tmp_path / "junit.xml"
    env = {
        **os.environ,
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "PYTHONPATH": str(ROOT / ".github/scripts"),
        "REGRESSION_INVENTORY": str(inventory),
    }
    process = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "regression_pytest",
            "--junitxml",
            str(junit),
            "-q",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert process.returncode == 0, process.stdout + process.stderr
    result = report.junit(junit, inventory)
    assert result["status"] == "incomplete"
    assert (result["expected"], result["executed"], result["skipped"]) == (2, 1, 1)


def bindings():
    return {
        "account_id": "000000000101",
        "gateway_url": "https://gateway.example.com",
        "api_gateway_url": "https://api.example.com/v1",
        "m2m_secret_name": "adp/test/gateway/test-m2m",
        "organization_id": "owned-test-org",
        "organization_name": "eval-regression-fixture",
    }


@pytest.mark.parametrize(
    "override",
    [
        {"account_id": "000000000102"},
        {"gateway_url": "http://example.com"},
        {"organization_name": "production"},
        {"organization_id": "a\nINJECTED=value"},
        {"api_gateway_url": "https://user:password@example.com"},
    ],
)
def test_gateway_binding_rejects_wrong_target_and_unowned_fixture(override):
    with pytest.raises(ValueError):
        gateway.resolve(json.dumps(bindings() | override), "000000000101")


def test_gateway_bindings_export_distinct_transport_targets():
    result = gateway.resolve(json.dumps(bindings()), "000000000101")
    assert result["GATEWAY_URL"] == "https://gateway.example.com"
    assert result["API_GATEWAY_URL"] == "https://api.example.com/v1"
    assert "password" not in result


def test_one_scheduler_and_no_child_schedule():
    coordinator = workflow("adp-regression.yml")
    assert triggers(coordinator)["schedule"] == [
        {"cron": report.DAILY_CRON},
        {"cron": report.WEEKLY_CRON},
    ]
    for name in [
        "nightly-cli-regression.yml",
        "e2e-chat-playwright.yml",
        "e2e-new-ui-playwright.yml",
        "gateway-live-tests.yml",
        "credential-binding-adversarial-e2e.yml",
    ]:
        child = workflow(name)
        assert "schedule" not in triggers(child)
        assert "workflow_call" in triggers(child)
        assert child["concurrency"]["group"] != coordinator["concurrency"]["group"]
    for name in [
        "eval-bedrock-routing.yml",
        "orchestration-live-tests.yml",
        "security-agent-nightly.yml",
    ]:
        assert "schedule" not in triggers(workflow(name))


def test_daily_and_weekly_preserve_nightly_cases_and_full_gate():
    from tests.e2e.cli_uplift.cases import resolve_suites

    profiles = json.loads((ROOT / ".github/regression-profiles.json").read_text())
    daily = {
        case.id for case in resolve_suites(profiles["daily"]["cli_scope"].split(","))
    }
    weekly = {
        case.id for case in resolve_suites(profiles["weekly"]["cli_scope"].split(","))
    }
    assert {"E01", "C01", "E27", "E42"} <= daily
    assert daily < weekly
    assert "full" not in profiles["weekly"]["cli_scope"].split(",")


def test_offline_reuse_preserves_required_pr_jobs_and_disables_cloud_build():
    ci = workflow("adp-ci.yml")
    assert ci["permissions"] == {"contents": "read"}
    for job in ci["jobs"].values():
        if "uses" in job:
            child = yaml.safe_load((ROOT / job["uses"]).read_text())
            assert "workflow_call" in triggers(child)
            if job["uses"] != "./.github/workflows/_regression-security.yml":
                assert "pull_request" in triggers(child)
    assert ci["jobs"]["gateway-ci"]["with"]["offline_only"] is True
    assert (
        workflow("gateway-ci.yml")["jobs"]["build"]["if"]
        == "${{ !inputs.offline_only }}"
    )


def test_live_wrappers_forward_oidc_and_aggregate_every_required_lane():
    for name in ["adp-regression.yml", "adp-deploy-check.yml"]:
        jobs = workflow(name)["jobs"]
        assert set(jobs["summary"]["needs"]) == set(jobs) - {"summary"}
        for key, job in jobs.items():
            if "uses" in job and key != "offline":
                assert job["permissions"]["id-token"] == "write"
                assert "continue-on-error" not in job
    assert (
        workflow("adp-regression.yml")["jobs"]["chat"]["with"]["require_interactions"]
        is True
    )


def test_readiness_observer_follows_coordinator_schedule():
    source = (ROOT / "platform/scripts/credential-binding-readiness.py").read_text()
    assert '"adp-regression.yml"' in source
    assert '"credential-binding-adversarial-e2e.yml"' not in source


def test_coordinators_require_smoke_token_instead_of_optional_skip(tmp_path):
    for name in ["adp-regression.yml", "adp-deploy-check.yml"]:
        assert workflow(name)["jobs"]["smoke"]["with"]["require_token"] is True
    steps = workflow("gateway-smoke.yml")["jobs"]["smoke"]["steps"]
    token = next(step for step in steps if step.get("id") == "token")
    output = tmp_path / "output"
    result = subprocess.run(
        ["bash", "-e", "-c", token["run"]],
        capture_output=True,
        env={**os.environ, "SMOKE_REFRESH_TOKEN_ARN": "", "GITHUB_OUTPUT": str(output)},
    )
    assert result.returncode == 0
    assert output.read_text().strip() == "token_available=false"
    gate = next(
        step
        for step in steps
        if step.get("name") == "Require smoke coverage for coordinated runs"
    )
    assert (
        gate["if"]
        == "${{ inputs.require_token && steps.token.outputs.token_available != 'true' }}"
    )
    assert (
        subprocess.run(
            ["bash", "-e", "-c", gate["run"]], capture_output=True
        ).returncode
        == 1
    )


def test_revision_drift_and_missing_revision_fail_even_when_jobs_pass(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("REGRESSION_JOBS", json.dumps({"api": {"result": "success"}}))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary"))
    monkeypatch.setenv("REGRESSION_REVISION_BEFORE", "a" * 40)
    monkeypatch.setenv("REGRESSION_REVISION_AFTER", "b" * 40)
    assert report.main(["aggregate", "--required", "api"]) == 1
    assert "**PASS**" not in (tmp_path / "summary").read_text()
    monkeypatch.setenv("REGRESSION_REVISION_AFTER", "a" * 40)
    assert report.main(["aggregate", "--required", "api"]) == 0
    monkeypatch.setenv("REGRESSION_REVISION_BEFORE", "")
    assert report.main(["aggregate", "--required", "api"]) == 1


def test_private_snapshot_binding_is_required_before_aws(monkeypatch):
    from tests.e2e.cli_regression import prepare

    monkeypatch.delenv("CLI_UPLIFT_EVAL_BINDINGS_JSON", raising=False)
    with pytest.raises(ValueError, match="private"):
        prepare.main([])


def test_scanner_empty_or_failed_output_cannot_pass(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / ".github/scripts"))
    scanner = module("regression_security")
    for tool in scanner.STEPS:
        with pytest.raises(ValueError):
            scanner.validate(tool, {})
    valid = {"runs": [{"tool": {"driver": {"name": "test"}}, "results": []}]}
    scanner.validate("bandit", valid)
    valid["runs"][0]["invocations"] = [{"executionSuccessful": False}]
    with pytest.raises(ValueError):
        scanner.validate("bandit", valid)


def test_gateway_cleanup_failure_is_not_swallowed_and_other_intents_are_swept():
    spec = importlib.util.spec_from_file_location(
        "gateway_live_fixture",
        ROOT / "modules/gateway/tests/integration/test_live_api.py",
    )
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    tracker = fixture._TestDataTracker()
    tracker.add_budget("owned", "team", "synthetic", "monthly")
    tracker.add_ratelimit("owned", "team", "synthetic")
    calls = []

    class Client:
        def delete(self, url):
            calls.append(url)
            raise RuntimeError("unavailable")

    cleanup = fixture.cleanup_test_data.__wrapped__(Client(), tracker)
    next(cleanup)
    with pytest.raises(pytest.fail.Exception, match="cleanup unavailable"):
        next(cleanup)
    assert len(calls) == 2


@pytest.mark.parametrize(
    "outcome,expected",
    [
        ("success", "true"),
        ("skipped", "true"),
        ("failure", "false"),
        ("cancelled", "false"),
        ("", "false"),
    ],
)
def test_live_mutation_boundary_requires_known_outcome(tmp_path, outcome, expected):
    for name in ["gateway-live-tests.yml", "gitlab-integration-tests.yml"]:
        for job in workflow(name)["jobs"].values():
            for step in job.get("steps", []):
                if step.get("id") != "boundary":
                    continue
                output = tmp_path / "output"
                output.write_text("")
                subprocess.run(
                    ["bash", "-e", "-c", step["run"]],
                    check=True,
                    env={
                        **os.environ,
                        "TEST_OUTCOME": outcome,
                        "GITHUB_OUTPUT": str(output),
                    },
                )
                assert output.read_text().strip() == f"cleanup_ok={expected}"


@pytest.mark.parametrize("expected", ["a" * 40, "b" * 40, "invalid"])
def test_snapshot_requires_triggering_gateway_revision(tmp_path, monkeypatch, expected):
    from tests.e2e.cli_regression import prepare

    monkeypatch.setenv("CLI_UPLIFT_EVAL_BINDINGS_JSON", "{}")
    monkeypatch.setenv("REGRESSION_EXPECTED_REVISION", expected)
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "output"))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary"))
    monkeypatch.setattr(prepare.config, "from_environment", lambda env: {})
    monkeypatch.setattr(
        prepare.ports, "default_ports", lambda cfg: {"aws": None, "http": None}
    )
    monkeypatch.setattr(prepare, "snapshot", lambda *args: ("a" * 40, "test"))
    if expected == "a" * 40:
        assert prepare.main([]) == 0
    else:
        with pytest.raises(ValueError, match="triggering deployment"):
            prepare.main([])
        assert not (tmp_path / "output").exists()


@pytest.mark.parametrize("baselined", [False, True])
def test_npm_driver_executes_source_command_and_preserves_baseline_namespace(
    tmp_path, monkeypatch, baselined
):
    monkeypatch.syspath_prepend(str(ROOT / ".github/scripts"))
    scanner = module("regression_security")
    package = "modules/gateway/frontend"
    (tmp_path / package).mkdir(parents=True)
    config_dir = tmp_path / ".github/security"
    config_dir.mkdir(parents=True)
    workflow_dir = tmp_path / ".github/workflows"
    workflow_dir.mkdir(parents=True)
    (workflow_dir / "security-scan.yml").write_text(
        (ROOT / ".github/workflows/security-scan.yml").read_text()
    )
    baseline = (
        {"vulnerabilities": {"modules-gateway-frontend:example": {"severity": "high"}}}
        if baselined
        else {}
    )
    (config_dir / "npm-audit-baseline.json").write_text(json.dumps(baseline))
    executable = tmp_path / "bin/npm"
    executable.parent.mkdir()
    executable.write_text(
        "#!/bin/sh\n"
        'echo \'{"auditReportVersion":2,"vulnerabilities":{"example":{"severity":"high"}}}\'\n'
        "exit 1\n"
    )
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", f"{executable.parent}:{os.environ['PATH']}")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["scanner", "npm-audit", "--package", package])
    assert scanner.main() == (0 if baselined else 1)
    gate = json.loads(
        (
            tmp_path / "test-results/security/modules-gateway-frontend/gate.json"
        ).read_text()
    )
    assert gate["new_count"] == (0 if baselined else 1)
