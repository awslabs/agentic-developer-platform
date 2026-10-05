"""Exercise component selection, the real git diff and required-check failures."""

import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

spec = importlib.util.spec_from_file_location(
    "superplane_ci_scope", Path(__file__).parents[1] / "superplane_ci_scope.py"
)
scope = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scope)
ROOT = Path(__file__).parents[3]
WORKFLOW = ROOT / ".github/workflows/superplane-domain-ci.yml"


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("platform/scripts/prepare-backends.py", set()),
        ("platform/scripts/deploy-all.sh", {"lifecycle"}),
        ("platform/scripts/teardown.py", {"lifecycle"}),
        (
            "modules/gateway/tests/features/test_superplane_deploy_scope.py",
            {"lifecycle"},
        ),
        ("modules/agent-factory/agent-worker-image/Dockerfile", {"worker"}),
        ("modules/agent-factory/agent-worker-image/stage-personas.sh", {"worker"}),
        ("modules/gateway/security/stdlib/manifest.json", {"worker"}),
        (
            "modules/domain-apps/superplane/agent/personas/example.md",
            {"worker", "persona"},
        ),
        (
            "modules/domain-apps/superplane/tests/acceptance/test_worker_image_assets.py",
            {"worker"},
        ),
        ("modules/domain-apps/superplane/src/superplane-api/app/main.py", {"api"}),
        (
            "modules/domain-apps/superplane/src/superplane-controller/go.mod",
            {"controller"},
        ),
        (
            "modules/domain-apps/superplane/src/superplane-platform-monitor/go.sum",
            {"monitor"},
        ),
        ("modules/domain-apps/superplane/migration/placement.py", {"domain"}),
        ("modules/domain-apps/superplane/deploy.sh", {"domain"}),
        (
            "modules/domain-apps/superplane/auth/superplane_auth/policy.py",
            {"api", "domain", "gateway"},
        ),
        (
            "modules/harness/jobs/harness_jobs/execution.py",
            {"api", "domain", "controller", "worker"},
        ),
        (
            "modules/domain-apps/superplane/infra/account-provisioning/runner.py",
            {"api", "domain"},
        ),
        ("modules/gateway/src/auth/dependencies.py", {"gateway"}),
        ("modules/gateway/src/domain_proxy/routes.py", {"gateway"}),
        ("modules/gateway/frontend/src/App.tsx", {"gateway", "ui"}),
        ("modules/domain-apps/superplane/ui/index.ts", {"ui"}),
        ("modules/gateway/frontend/package-lock.json", {"ui"}),
        (".github/workflows/superplane-ui-browser-ci.yml", {"ui"}),
        (
            "modules/domain-apps/superplane/pyproject.toml",
            {"domain", "api", "controller", "worker"},
        ),
        ("modules/gateway/pyproject.toml", {"domain", "gateway"}),
        ("docs/agent-catalogue.md", {"persona"}),
    ],
)
def test_component_consumers(path, expected):
    assert scope.classify([path]) == expected


@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/superplane-domain-ci.yml",
        ".github/scripts/superplane_ci_scope.py",
        "modules/domain-apps/superplane/contracts/schema.json",
        "modules/domain-apps/superplane/releases/transfer-constraints.txt",
        "modules/domain-apps/superplane/src/new-component/app.py",
    ],
)
def test_shared_contracts_and_unknown_components_keep_all_consumers(path):
    assert scope.classify([path]) == set(scope.COMPONENTS) | {"persona"}
    assert scope.classify([]) == set(scope.COMPONENTS) | {"persona"}


def test_mixed_changes_union_consumers_without_escalating_everything():
    assert scope.classify(
        [
            "platform/scripts/deploy-all.sh",
            "modules/agent-factory/agent-worker-image/Dockerfile",
            "modules/gateway/tests/features/test_superplane_deploy_scope.py",
        ]
    ) == {"worker", "lifecycle"}
    assert scope.classify(
        [
            "modules/domain-apps/superplane/src/superplane-api/app.py",
            "modules/domain-apps/superplane/src/superplane-platform-monitor/main.go",
            "docs/agent-catalogue.md",
        ]
    ) == {"api", "monitor", "persona"}


def outputs(path):
    return dict(line.split("=", 1) for line in path.read_text().splitlines())


def test_manual_run_keeps_all_components(monkeypatch, tmp_path):
    target = tmp_path / "output"
    monkeypatch.setenv("CI_EVENT", "workflow_dispatch")
    monkeypatch.setenv("GITHUB_OUTPUT", str(target))
    scope.main()
    result = outputs(target)
    assert set(json.loads(result["components"])) == scope.COMPONENTS
    assert result["controller_changed"] == result["persona_changed"] == "true"


def test_actual_git_rename_and_delete_keep_both_component_consumers(
    monkeypatch, tmp_path
):
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=tmp_path).decode().strip()

    git("init", "-q")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.com")
    old = tmp_path / (scope.MODULE + "src/superplane-api/example.py")
    old.parent.mkdir(parents=True)
    old.write_text("example")
    git("add", ".")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    new = tmp_path / (scope.MODULE + "src/superplane-controller/example.py")
    new.parent.mkdir(parents=True)
    git("mv", str(old), str(new))
    git("commit", "-qm", "move component")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CI_EVENT", "pull_request")
    monkeypatch.setenv("PR_BASE_SHA", base)
    monkeypatch.setenv("PR_HEAD_SHA", git("rev-parse", "HEAD"))
    target = tmp_path / "outputs"
    monkeypatch.setenv("GITHUB_OUTPUT", str(target))
    scope.main()
    assert json.loads(outputs(target)["components"]) == ["api", "controller"]
    base = git("rev-parse", "HEAD")
    git("rm", str(new))
    git("commit", "-qm", "remove component")
    monkeypatch.setenv("PR_BASE_SHA", base)
    monkeypatch.setenv("PR_HEAD_SHA", git("rev-parse", "HEAD"))
    scope.main()
    assert json.loads(outputs(target)["components"]) == ["controller"]


def test_workflow_runs_independent_component_jobs_and_preserves_check_name():
    workflow = yaml.safe_load(WORKFLOW.read_text())
    assert "paths" not in workflow.get("on", workflow.get(True))["pull_request"]
    jobs = workflow["jobs"]
    lane = jobs["superplane-domain-tests"]
    assert lane["strategy"]["fail-fast"] is False
    assert "fromJSON" in lane["strategy"]["matrix"]["component"]
    assert jobs["superplane-required-check"]["name"] == "Superplane domain tests"
    assert "controller_changed" in jobs["controller-execution-tests"]["if"]
    assert "persona_changed" in jobs["persona-registration-tests"]["if"]
    expected = {
        "Run transferred Superplane API tests": "api",
        "Enforce coverage on the observation receiver": "api",
        "Enforce coverage on provider-handle persistence": "api",
        "Run transferred Superplane controller tests": "controller",
        "Run transferred Superplane platform monitor tests": "monitor",
        "Enforce coverage on the observation client": "monitor",
        "Run domain module tests": "domain",
        "Test Superplane UI": "ui",
        "Verify Superplane worker packaging": "worker",
        "Verify shared lifecycle integration offline": "lifecycle",
        "Run gate and registration tests": "gateway",
    }
    steps = {s.get("name"): s for s in lane["steps"]}
    for name, component in expected.items():
        assert steps[name]["if"] == "${{ matrix.component == '" + component + "' }}"
    assert (
        "test_worker_image_assets.py"
        in steps["Verify Superplane worker packaging"]["run"]
    )
    assert (
        "Unselected component suites were not run"
        in steps["Report selected coverage"]["run"]
    )
    # No old full-scope condition may silently stop an existing test step running.
    assert "outputs.scope" not in WORKFLOW.read_text()
    assert set(expected.values()) == scope.COMPONENTS


@pytest.mark.parametrize("component", ["core", *sorted(scope.COMPONENTS)])
@pytest.mark.parametrize("personas", ["true", "false"])
def test_required_check_refuses_failure_cancellation_and_selected_skips(
    component, personas
):
    gate = yaml.safe_load(WORKFLOW.read_text())["jobs"]["superplane-required-check"]
    assert "always()" in gate["if"]
    assert set(gate["needs"]) == {
        "change-scope",
        "superplane-domain-tests",
        "persona-registration-tests",
        "controller-execution-tests",
        "superplane-browser",
    }
    for key in (
        "SCOPE_RESULT",
        "DOMAIN_RESULT",
        "CONTROLLER_RESULT",
        "PERSONA_RESULT",
        "BROWSER_RESULT",
    ):
        for value in ("success", "failure", "cancelled", "skipped"):
            env = dict(
                os.environ,
                COMPONENTS=json.dumps([component]),
                CONTROLLER_CHANGED=str(component == "controller").lower(),
                PERSONA_CHANGED=personas,
                SCOPE_RESULT="success",
                DOMAIN_RESULT="success",
                CONTROLLER_RESULT="success",
                PERSONA_RESULT="success",
                BROWSER_RESULT="success",
            )
            env[key] = value
            required = (
                key in ("SCOPE_RESULT", "DOMAIN_RESULT")
                or (key == "CONTROLLER_RESULT" and component == "controller")
                or (key == "PERSONA_RESULT" and personas == "true")
                or (key == "BROWSER_RESULT" and component == "ui")
            )
            result = subprocess.run(
                ["bash", "-e", "-c", gate["steps"][0]["run"]],
                env=env,
                check=False,
                capture_output=True,
            )
            assert (result.returncode == 0) == (not required or value == "success")


@pytest.mark.parametrize(
    "components", [[], ["unknown"], ["core", "api"], ["api", "api"], "api"]
)
def test_required_check_rejects_invalid_matrix(components):
    gate = yaml.safe_load(WORKFLOW.read_text())["jobs"]["superplane-required-check"]
    env = dict(
        os.environ,
        COMPONENTS=json.dumps(components),
        CONTROLLER_CHANGED="false",
        PERSONA_CHANGED="false",
        SCOPE_RESULT="success",
        DOMAIN_RESULT="success",
        CONTROLLER_RESULT="success",
        PERSONA_RESULT="success",
        BROWSER_RESULT="success",
    )
    result = subprocess.run(
        ["bash", "-e", "-c", gate["steps"][0]["run"]],
        env=env,
        check=False,
        capture_output=True,
    )
    assert result.returncode != 0


def test_browser_checks_are_scoped_and_owned_by_superplane():
    jobs = yaml.safe_load(WORKFLOW.read_text())["jobs"]
    browser = jobs["superplane-browser"]
    assert (
        "contains(fromJSON(needs.change-scope.outputs.components), 'ui')"
        in browser["if"]
    )
    assert browser["uses"] == "./.github/workflows/superplane-ui-browser-ci.yml"
    gateway = yaml.safe_load((ROOT / ".github/workflows/gateway-ci.yml").read_text())
    assert "superplane-browser" not in gateway["jobs"]


@pytest.mark.parametrize(
    "paths,expected,persona",
    [
        (["platform/infra/main.tf"], ["core"], "false"),
        (["docs/agent-catalogue.md"], ["core"], "true"),
        (
            [
                "platform/scripts/deploy-all.sh",
                "modules/agent-factory/agent-worker-image/Dockerfile",
            ],
            ["lifecycle", "worker"],
            "false",
        ),
    ],
)
def test_pr_outputs_for_platform_and_persona_changes(
    monkeypatch, tmp_path, paths, expected, persona
):
    monkeypatch.setenv("CI_EVENT", "pull_request")
    monkeypatch.setenv("PR_BASE_SHA", "a" * 40)
    monkeypatch.setenv("PR_HEAD_SHA", "b" * 40)
    target = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(target))
    monkeypatch.setattr(
        scope.subprocess,
        "check_output",
        lambda args: ("\0".join(paths) + "\0").encode(),
    )
    scope.main()
    result = outputs(target)
    assert json.loads(result["components"]) == expected
    assert result["persona_changed"] == persona
    assert result["controller_changed"] == "false"


def test_invalid_pr_sha_fails_without_emitting_a_passing_selection(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("CI_EVENT", "pull_request")
    monkeypatch.setenv("PR_BASE_SHA", "--invalid")
    monkeypatch.setenv("PR_HEAD_SHA", "b" * 40)
    target = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(target))
    with pytest.raises(ValueError, match="full PR commit SHAs"):
        scope.main()
    assert not target.exists()
