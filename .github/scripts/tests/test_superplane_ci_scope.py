"""Keep CI scope selection and required-check behavior aligned."""

import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "superplane_ci_scope", Path(__file__).parents[1] / "superplane_ci_scope.py"
)
scope = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scope)


def test_catalogue_and_registry_use_lightweight_checks():
    assert scope.classify(list(scope.PERSONA_METADATA)) == "persona"
    assert scope.classify(["docs/agent-catalogue.md"]) == "persona"


def test_core_and_shared_integration_are_not_full_suites():
    assert scope.classify(["platform/scripts/prepare-backends.py"]) == "core"
    for path in (
        "platform/scripts/teardown.py",
        "platform/scripts/deploy-all.sh",
        ".github/workflows/undeploy.yml",
        "modules/gateway/tests/features/test_superplane_deploy_scope.py",
    ):
        assert scope.classify([path]) == "integration"


def test_module_and_shared_dependencies_use_full_checks():
    for path in (
        "modules/domain-apps/superplane/src/superplane-api/app.py",
        "modules/domain-apps/superplane/tests/acceptance/test_worker_image_assets.py",
        "modules/harness/jobs/src/worker.py",
        "modules/gateway/src/domain_proxy/superplane.py",
        "modules/gateway/src/auth/aws_connection_authority.py",
        "modules/gateway/src/features/routes.py",
        "modules/gateway/tests/features/test_superplane_proxy.py",
        "modules/gateway/tests/features/test_superplane_flag.py",
        "modules/gateway/tests/e2e/test_superplane_route_gated.py",
        "modules/gateway/frontend/src/services/features.ts",
        "modules/agent-factory/agent-worker-image/Dockerfile",
        "modules/agent-factory/agent-worker-image/stage-personas.sh",
        ".github/workflows/superplane-domain-ci.yml",
        ".github/scripts/superplane_ci_scope.py",
    ):
        assert scope.classify([path]) == "full"


def test_mixed_changes_select_the_strongest_scope():
    assert (
        scope.classify(["docs/agent-catalogue.md", "platform/scripts/teardown.py"])
        == "integration"
    )
    assert (
        scope.classify(
            ["platform/scripts/teardown.py", "modules/domain-apps/superplane/deploy.sh"]
        )
        == "full"
    )
    assert (
        scope.classify(
            ["docs/agent-catalogue.md", "modules/domain-apps/superplane/deploy.sh"]
        )
        == "full"
    )
    assert (
        scope.classify(["docs/agent-catalogue.md", "docs/renamed-catalogue.md"])
        == "core"
    )
    assert scope.classify([]) == "full"


def test_workflow_emits_required_checks_without_misreporting_full_coverage():
    import yaml

    workflow = yaml.safe_load(
        (Path(__file__).parents[2] / "workflows/superplane-domain-ci.yml").read_text()
    )
    trigger = workflow.get("on", workflow.get(True))
    assert "paths" not in trigger["pull_request"]
    jobs = workflow["jobs"]
    assert jobs["superplane-required-check"]["name"] == "Superplane domain tests"
    assert "scope == 'full'" in jobs["controller-execution-tests"]["if"]
    assert "persona_changed" in jobs["persona-registration-tests"]["if"]
    steps = jobs["superplane-domain-tests"]["steps"]
    summary = next(
        step for step in steps if step.get("name") == "Report selected coverage"
    )
    assert "Full domain/controller/browser suites were not run" in summary["run"]
    focused = next(
        step
        for step in steps
        if step.get("name") == "Verify shared lifecycle integration offline"
    )
    assert "scope == 'integration'" in focused["if"]
    assert "test_teardown.py" in focused["run"]
    assert "test_superplane_deploy_scope.py" in focused["run"]
    assert "--noconftest" in focused["run"]
    install = next(
        step for step in steps if step.get("name") == "Install gateway dependencies"
    )
    assert "scope == 'full'" in install["if"]
    shared_steps = {
        "Setup Python",
        "Install gateway dependencies",
        "Report selected coverage",
        "Install classifier test dependency",
        "Test CI scope selection",
        "Verify shared lifecycle integration offline",
        "Verify no AWS credentials were configured",
        "Verify the AWS SDK cannot discover credentials",
    }
    for step in steps:
        if step.get("name") not in shared_steps and "name" in step:
            assert "scope == 'full'" in step["if"], step["name"]


def test_manual_run_keeps_full_suite(monkeypatch, tmp_path):
    output = tmp_path / "outputs"
    monkeypatch.setenv("CI_EVENT", "workflow_dispatch")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    scope.main()
    assert output.read_text() == "scope=full\npersona_changed=false\n"


def test_pr_diff_uses_changed_paths(monkeypatch, tmp_path):
    import subprocess

    def git(*args):
        return subprocess.check_output(["git", *args], cwd=tmp_path).decode().strip()

    git("init", "-q")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.com")
    git("commit", "--allow-empty", "-qm", "base")
    base = git("rev-parse", "HEAD")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/agent-catalogue.md").write_text("catalogue")
    git("add", "docs")
    git("commit", "-qm", "catalogue")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CI_EVENT", "pull_request")
    monkeypatch.setenv("PR_BASE_SHA", base)
    monkeypatch.setenv("PR_HEAD_SHA", git("rev-parse", "HEAD"))
    output = tmp_path / "outputs"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    scope.main()
    assert output.read_text() == "scope=persona\npersona_changed=true\n"
    (tmp_path / "runtime.py").write_text("change")
    git("add", "runtime.py")
    git("commit", "-qm", "mixed implementation")
    monkeypatch.setenv("PR_HEAD_SHA", git("rev-parse", "HEAD"))
    scope.main()
    assert output.read_text().endswith("scope=core\npersona_changed=true\n")


def test_required_check_rejects_failed_or_missing_selected_coverage():
    import itertools
    import subprocess
    import os
    import yaml

    workflow = yaml.safe_load(
        (Path(__file__).parents[2] / "workflows/superplane-domain-ci.yml").read_text()
    )
    gate = workflow["jobs"]["superplane-required-check"]
    assert "always()" in gate["if"]
    assert set(gate["needs"]) == {
        "change-scope",
        "superplane-domain-tests",
        "persona-registration-tests",
        "controller-execution-tests",
    }
    script = gate["steps"][0]["run"]
    for selected, changed in itertools.product(
        ("core", "integration", "persona", "full", "unknown"), ("true", "false")
    ):
        for result in ("success", "failure", "cancelled", "skipped"):
            for key in (
                "SCOPE_RESULT",
                "DOMAIN_RESULT",
                "PERSONA_RESULT",
                "CONTROLLER_RESULT",
            ):
                env = dict(
                    os.environ,
                    SCOPE=selected,
                    PERSONA_CHANGED=changed,
                    SCOPE_RESULT="success",
                    DOMAIN_RESULT="success",
                    PERSONA_RESULT="success",
                    CONTROLLER_RESULT="success",
                )
                env[key] = result
                required = (
                    key in {"SCOPE_RESULT", "DOMAIN_RESULT"}
                    or (key == "CONTROLLER_RESULT" and selected == "full")
                    or (
                        key == "PERSONA_RESULT"
                        and selected != "full"
                        and changed == "true"
                    )
                )
                expected = selected != "unknown" and (
                    not required or result == "success"
                )
                check = subprocess.run(
                    ["bash", "-e", "-c", script], env=env, capture_output=True
                )
                assert (check.returncode == 0) == expected, (
                    selected,
                    changed,
                    key,
                    result,
                )
