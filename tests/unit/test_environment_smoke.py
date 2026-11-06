"""Offline commissioning selection, wrong-target rejection and orchestration checks."""

import ast
import json
from pathlib import Path

import pytest
import yaml

from tests.e2e.cli_uplift import cases, config
from tests.regression import environment_smoke as smoke

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def target():
    return {
        "account_id": "000000000101",
        "region": "eu-west-1",
        "resource_environment": "commissioning",
        "gateway_url": "https://gateway.example.com/api",
        "dashboard_url": "https://gateway.example.com",
        "cognito_user_pool_id": "eu-west-1_Example",
        "chat_enabled": False,
        "new_ui_enabled": False,
    }


@pytest.fixture
def context(target):
    return {
        "AWS_REGION": target["region"],
        "ENVIRONMENT": target["resource_environment"],
        "SMOKE_TARGET_ENVIRONMENT": "staging",
        "CLI_UPLIFT_EVAL_EXPECTED_REVISION": "a" * 40,
    }


def test_core_uses_existing_cases_without_paid_or_mutating_lifecycles():
    plan = smoke.selection()
    assert {"E01", "C01", "E20", "E23", "E29", "E38"} <= set(plan["cli_cases"])
    assert not {"E08", "E27", "E42", "E50", "D02", "D03", "D06"} & set(
        plan["cli_cases"]
    )
    assert {
        case.id for case in cases.resolve_suites(plan["cli_scope"].split(","))
    } == set(plan["cli_cases"])
    assert "MOD-016" not in plan["modules"]


def test_focused_selection_adds_foundation_and_rejects_unsupported_modules():
    plan = smoke.selection("Agent Models")
    assert plan["modules"] == ["MOD-001", "MOD-005", "MOD-023", "MOD-024"]
    assert "case:E38" in plan["cli_scope"]
    assert "case:E29" not in plan["cli_scope"]
    with pytest.raises(ValueError, match="No smoke group"):
        smoke.selection("MOD-034")
    with pytest.raises(ValueError):
        smoke.selection("MOD-001;echo unsafe")


def test_all_means_defined_smoke_groups_and_both_chat_modes_have_real_tests():
    for enabled in (True, False):
        plan = smoke.selection("all", enabled)
        assert len(plan["modules"]) == 12
        selectors = plan["browser_selectors"]
        assert any("test_persistence" in s for s in selectors) is enabled
        assert any("test_disabled" in s for s in selectors) is not enabled
        for selector in selectors:
            path, *names = selector.split("::")
            tree = ast.parse((ROOT / path).read_text())
            for name in names:
                tree = next(
                    node
                    for node in tree.body
                    if isinstance(node, (ast.ClassDef, ast.FunctionDef))
                    and node.name == name
                )
        for case in plan["cli_cases"]:
            assert case in cases.BY_ID


@pytest.mark.parametrize(
    "field,value",
    [
        ("account_id", "wrong"),
        ("region", "us-east-1\n"),
        ("resource_environment", "../dev"),
        ("dashboard_url", "http://gateway.example.com"),
        ("dashboard_url", "https://other.example.com"),
        ("gateway_url", "https://gateway.example.com/api?token=x"),
        ("gateway_url", "https://user:password@gateway.example.com/api"),
        ("chat_enabled", "false"),
        ("cognito_user_pool_id", "us-east-1_Example"),
    ],
)
def test_bad_target_is_rejected(target, field, value):
    target[field] = value
    with pytest.raises(ValueError):
        smoke.target(json.dumps(target))


def test_no_implicit_target_or_unknown_secret_fields(target):
    for raw in ("", "{}", "[]", json.dumps({**target, "password": "unsafe"})):
        with pytest.raises(ValueError):
            smoke.target(raw)


def test_target_digest_and_non_dev_region_are_checked(target, context):
    smoke.validate_context(smoke.target(json.dumps(target)), context)
    context["SMOKE_TARGET_DIGEST"] = smoke.fingerprint(target)
    smoke.validate_context(target, context)
    with pytest.raises(ValueError, match="differs"):
        smoke.validate_context({**target, "new_ui_enabled": True}, context)
    with pytest.raises(ValueError, match="region/resource"):
        smoke.validate_context(target, {**context, "AWS_REGION": "us-east-1"})
    with pytest.raises(ValueError, match="region/resource"):
        smoke.validate_context(target, {**context, "ENVIRONMENT": "dev"})
    with pytest.raises(ValueError, match="SHA"):
        smoke.validate_context(
            target, {**context, "CLI_UPLIFT_EVAL_EXPECTED_REVISION": "main"}
        )


def test_missing_private_identity_and_effective_override_cannot_use_examples(target):
    cfg = json.loads(config.EXAMPLE_PATH.read_text())
    cfg.update(
        platform_account=target["account_id"],
        region=target["region"],
        gateway_url=target["gateway_url"],
        cognito_user_pool_id=target["cognito_user_pool_id"],
        gateway_deployment="staging",
        instance_security_group_id="sg-00000000000000000",
        credential_secret_name="test-fixture",
        state_bucket="example-recovery",
        evaluation_target="adp-eval-staging-example",
    )
    smoke.validate_cli(target, cfg, cfg)
    with pytest.raises(ValueError, match="explicitly"):
        smoke.validate_cli(target, cfg, {})
    with pytest.raises(ValueError, match="differs"):
        smoke.validate_cli(
            target, {**cfg, "gateway_url": "https://other.example.com/api"}, cfg
        )


def workflow(name):
    return yaml.safe_load((ROOT / ".github/workflows" / name).read_text())


def test_missing_role_keeps_preflight_evidence_without_live_access(tmp_path):
    import os
    import subprocess

    steps = workflow("_environment-smoke-snapshot.yml")["jobs"]["snapshot"]["steps"]
    assert steps[0]["uses"].startswith("actions/checkout@")
    guard = steps[1]
    result = subprocess.run(
        ["bash", "-c", guard["run"]],
        cwd=tmp_path,
        env={**os.environ, "GITHUB_REF": "refs/heads/main", "ROLE_ARN": ""},
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert (
        "Missing evaluator role"
        in (tmp_path / "test-results/environment-smoke-preflight.txt").read_text()
    )
    assert steps[-1]["with"]["path"] == "test-results/"


def test_workflow_is_explicit_dispatch_or_reuse_not_regular_deploy_ci():
    doc = workflow("adp-environment-smoke.yml")
    triggers = doc.get("on", doc.get(True))
    assert set(triggers) == {"workflow_dispatch", "workflow_call"}
    for event in triggers.values():
        for field in (
            "target_environment",
            "environment",
            "region",
            "expected_revision",
        ):
            assert event["inputs"][field]["required"] is True
    jobs = doc["jobs"]
    assert jobs["cli"]["uses"].endswith("eval-cli-uplift.yml")
    assert (
        jobs["cli"]["with"]["smoke_target_digest"]
        == "${{ needs.before.outputs.target_digest }}"
    )
    assert "cleanup_ok == 'true'" in jobs["browser"]["if"]
    assert (
        jobs["after"]["with"]["target_digest"]
        == "${{ needs.before.outputs.target_digest }}"
    )
    assert (
        jobs["after"]["with"]["expected_revision"] == "${{ inputs.expected_revision }}"
    )
    assert set(jobs["summary"]["needs"]) == {"before", "cli", "browser", "after"}
    assert jobs["summary"]["if"] == "always()"
    assert doc["concurrency"]["group"] != jobs["browser"]["concurrency"]["group"]


def test_evaluation_and_recovery_guard_the_same_target_before_mutation():
    doc = workflow("eval-cli-uplift.yml")
    for name in ("evaluate", "recover"):
        job = doc["jobs"][name]
        assert job["env"]["SMOKE_TARGET_DIGEST"] == "${{ inputs.smoke_target_digest }}"
        assert "inputs.region" in job["env"]["AWS_REGION"]
        assert (
            "inputs.smoke_target_digest == ''" in job["env"]["CLI_UPLIFT_EVAL_BINDINGS"]
        )
        steps = job["steps"]
        guard = next(
            i
            for i, s in enumerate(steps)
            if s.get("run") == "python -m tests.regression.environment_smoke cli-guard"
        )
        build = next(
            i
            for i, s in enumerate(steps)
            if "python -m tests.e2e.cli_uplift.build_run_config" in s.get("run", "")
        )
        assert guard < build
        for step in steps:
            if step.get("uses", "").startswith("aws-actions/configure-aws-credentials"):
                assert "inputs.region" in step["with"]["aws-region"]


def test_wrong_role_account_stops_before_loading_cli_or_running_tests(
    target, context, monkeypatch
):
    from types import SimpleNamespace
    import sys

    for key, value in context.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("ENVIRONMENT_SMOKE_TARGET_JSON", json.dumps(target))
    monkeypatch.setenv("SMOKE_TARGET_DIGEST", smoke.fingerprint(target))
    monkeypatch.setitem(
        sys.modules,
        "boto3",
        SimpleNamespace(
            client=lambda name: SimpleNamespace(
                get_caller_identity=lambda: {"Account": "000000000999"}
            )
        ),
    )
    with pytest.raises(ValueError, match="Assumed role account"):
        smoke.main(["browser"])


@pytest.mark.parametrize("enabled", [True, False])
def test_curated_browser_collection_keeps_every_requested_definition(tmp_path, enabled):
    import os
    import subprocess
    import sys

    plan = smoke.selection("all", enabled)
    inventory = tmp_path / "inventory.json"
    env = {
        **os.environ,
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "PYTHONPATH": str(ROOT / ".github/scripts") + os.pathsep + str(ROOT),
        "REGRESSION_INVENTORY": str(inventory),
        "REGRESSION_MODULES": "",
        "REGRESSION_KIND": "all",
        "E2E_CHAT_ENABLED": "1",
        "E2E_NEW_UI_ENABLED": "1",
        "E2E_CHAT_EXPECTED_FLAG": "on" if enabled else "off",
        "E2E_NEW_UI_EXPECTED_FLAG": "off",
    }
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-p",
            "regression_pytest",
            *plan["browser_selectors"],
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    collected = json.loads(inventory.read_text())
    assert {node.split("[", 1)[0] for node in collected} == set(
        plan["browser_selectors"]
    )
