"""Controlled execution faults; synthetic inputs are never live acceptance evidence."""

import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest

from tests.e2e.product_onboarding import product_completion_attempt as attempt_module
from tests.e2e.product_onboarding.product_completion_inventory import Case
from tests.e2e.product_onboarding.product_v02_record import private_json


@pytest.fixture
def attempt(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    environment = {"GITHUB_RUN_ID": "42", "GITHUB_RUN_ATTEMPT": "1",
                   "PRODUCT_EXPECTED_REVISION": "a" * 40, "GITHUB_REPOSITORY": "aws-e/adp",
                   "GITHUB_REPOSITORY_ID": "123", "PRODUCT_BROWSER_WORKFLOW_REVISION": "b" * 40,
                   "PRODUCT_CORRELATION": "execution-1"}
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    cases = {"issue-8250-ac-01": Case("owned", "record", ("case:PR04-C01",)),
             "issue-8268-ac-02": Case("owned", "record", ("tests/e2e/new_ui/test_product_resume.py::test_v11_cross_client_unavailable",))}
    monkeypatch.setattr(attempt_module, "required_cases", lambda: cases)
    monkeypatch.setattr(attempt_module, "verify_fixture_providers", lambda _: None)
    manifest = {"schema_version": 1, "release_id": "product-40-1", "run_id": 40, "run_attempt": 1,
                "repository": "aws-e/adp", "repository_id": 123, "environment": "dev",
                "source_revision": "a" * 40,
                "components": {component: {"verified": True} for component in
                               ("gateway-backend", "gateway-frontend", "installed-cli", "schema")}}
    private_json(tmp_path / "release-manifest.json", manifest)
    digest = hashlib.sha256((tmp_path / "release-manifest.json").read_bytes()).hexdigest()
    private_json(tmp_path / "required-checks.json", {"release_id": "product-40-1", "manifest_sha256": digest})
    state = {"root": tmp_path, "environment": environment, "cases": cases, "calls": [], "fault": None}

    def observations(root, suite, selectors):
        directory = root / suite
        directory.mkdir(mode=0o700)
        report = directory / "junit.xml"
        report.write_text("<testsuite tests='1'><testcase name='controlled'/></testsuite>")
        report.chmod(0o600)
        # Successful commands still retain an empty stderr diagnostic.
        diagnostic = directory / "junit.stderr.txt"
        diagnostic.write_text("")
        diagnostic.chmod(0o600)
        member = {"id": "owner", "org_id": "fixture-tenant", "kind": "member",
                  "resource": {"membership_status": "active", "role": "member", "teams": []}}
        receipt = {"run_id": "product-42-1", "source_revision": "a" * 40, "suite_id": root.name + "-" + suite,
                   "cases": {"owned": {"run_id": "product-42-1", "product_id": root.name + "-product-" + suite,
                                       "project_id": root.name + "-project-" + suite,
                                       "fixture_removed": True, "owned_absent": True, "shared_unchanged": True}},
                   "memberships": {"owner": {"baseline": member, "observed": deepcopy(member)}},
                   "ownership_lock": {"released": True, "version": "observed-version"}}
        cleanup = directory / "product-suite-cleanup.json"
        private_json(cleanup, receipt)
        return [{"selector": selector, "status": "passed", "report_path": str(report),
                 "report_sha256": hashlib.sha256(report.read_bytes()).hexdigest(),
                 "suite_id": receipt["suite_id"], "cleanup_path": str(cleanup),
                 **({"run_id": "product-42-1"} if selector.startswith("case:") else
                    {"tests": 1, "command": ["python", "-m", "pytest", selector]})}
                for selector in selectors]

    def inventory(root, *, namespace_prefix):
        assert root.name == namespace_prefix
        state["calls"].append(namespace_prefix)
        results = {key: observations(root, key, spec.producers) for key, spec in cases.items()}
        if state["fault"]:
            state["fault"](root, results)
        return results

    monkeypatch.setattr(attempt_module, "run_inventory", inventory)
    monkeypatch.setattr(attempt_module, "run_baseline", lambda root, prefix:
                        observations(root, "baseline", attempt_module.BASELINE_SELECTORS))
    return state


def test_two_fresh_passes_publish_story_checks_and_all_four_aggregate_checks(attempt):
    checks = attempt_module.execute_attempt(attempt["root"], attempt["environment"])
    expected = set(attempt["cases"]) | {"baseline-regression", "repeatability", "cleanup", "evidence-index"}
    assert {check["id"] for check in checks} == expected
    assert attempt["calls"] == ["pass-1", "pass-2"]
    index = json.loads((attempt["root"] / "evidence-index.json").read_text())
    assert index["release_id"] == "product-40-1"
    assert {check["id"] for check in index["checks"]} == expected - {"evidence-index"}
    assert all(item["sha256"] for item in index["files"])
    assert any(item["bytes"] == 0 and item["path"].endswith(".stderr.txt") for item in index["files"])
    repeat = json.loads((attempt["root"] / "repeatability.json").read_text())
    first, second = repeat["actual"]["fresh_fixtures"]
    assert set(first["products"]).isdisjoint(second["products"])
    assert set(first["projects"]).isdisjoint(second["projects"])
    with pytest.raises(FileExistsError):
        attempt_module.execute_attempt(attempt["root"], attempt["environment"])
    assert attempt["calls"] == ["pass-1", "pass-2"]


@pytest.mark.parametrize("fault", ["missing_case", "skipped", "changed_report", "missing_cleanup", "wrong_attempt",
                                   "membership_not_restored", "lock_not_released", "reused_fixture", "changed_count"])
def test_incomplete_or_replayed_observations_never_publish_an_index(attempt, fault):
    def corrupt(root, results):
        first = next(iter(results.values()))[0]
        if fault == "missing_case":
            results.pop(next(iter(results)))
        elif fault == "skipped":
            first["status"] = "skipped"
        elif fault == "changed_report":
            Path(first["report_path"]).write_text("changed")
        elif fault == "changed_count":
            if root.name == "pass-2":
                results["issue-8268-ac-02"][0]["tests"] = 2
        else:
            path = Path(first["cleanup_path"])
            if fault == "missing_cleanup":
                path.unlink()
                return
            receipt = json.loads(path.read_text())
            if fault == "wrong_attempt":
                receipt["run_id"] = "product-42-2"
            elif fault == "membership_not_restored":
                receipt["memberships"]["owner"]["observed"]["resource"]["membership_status"] = "revoked"
            elif fault == "lock_not_released":
                receipt["ownership_lock"]["released"] = False
            elif fault == "reused_fixture" and root.name == "pass-2":
                for item in receipt["cases"].values():
                    item["product_id"] = item["product_id"].replace("pass-2", "pass-1")
            path.write_text(json.dumps(receipt))
    attempt["fault"] = corrupt
    with pytest.raises(ValueError):
        attempt_module.execute_attempt(attempt["root"], attempt["environment"])
    assert not (attempt["root"] / "evidence-index.json").exists()


def test_missing_producer_fails_before_a_fixture_is_claimed(attempt, monkeypatch):
    def refuse(_):
        raise ValueError("missing deployed fixture")
    monkeypatch.setattr(attempt_module, "verify_fixture_providers", refuse)
    with pytest.raises(ValueError, match="missing deployed fixture"):
        attempt_module.execute_attempt(attempt["root"], attempt["environment"])
    assert attempt["calls"] == []


def test_standalone_executes_the_same_two_passes_without_engine_transport(attempt, monkeypatch):
    from tests.e2e.product_onboarding import product_completion_regression as regression

    environment = {**attempt["environment"], "GITHUB_REF": "refs/heads/main",
                   "GITHUB_EVENT_NAME": "workflow_dispatch",
                   "GITHUB_WORKFLOW_REF": "aws-e/adp/.github/workflows/product-profile-regression-standalone.yml@refs/heads/main"}
    checks = (*attempt["cases"], "baseline-regression", "repeatability", "cleanup", "evidence-index")
    monkeypatch.setattr(regression, "required_checks", lambda: checks)
    with pytest.raises(ValueError, match="engine transport"):
        attempt_module.publish_standalone(attempt["root"], {**environment, "ADP_CORRELATION": "foreign"})
    assert attempt["calls"] == []
    output = attempt_module.publish_standalone(attempt["root"], environment)
    result = json.loads(Path(output).read_text())
    assert result["mode"] == "standalone" and attempt["calls"] == ["pass-1", "pass-2"]
    assert {check["id"] for check in result["checks"]} == set(checks)
    assert "correlation" not in result and "contract_digest" not in result
