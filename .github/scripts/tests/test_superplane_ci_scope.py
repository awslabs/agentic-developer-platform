"""Ensure metadata-only selection cannot hide implementation changes."""

import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "superplane_ci_scope", Path(__file__).parents[1] / "superplane_ci_scope.py"
)
scope = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scope)


def test_catalogue_and_registry_use_lightweight_checks():
    assert scope.persona_only(list(scope.PERSONA_METADATA))
    assert scope.persona_only(["docs/agent-catalogue.md"])


def test_mixed_change_requires_full_checks():
    assert not scope.persona_only(
        [
            "docs/agent-catalogue.md",
            "modules/domain-apps/superplane/src/superplane-api/app.py",
        ]
    )


def test_staging_and_workflow_changes_require_full_checks():
    for path in [
        "modules/agent-factory/agent-worker-image/stage-personas.sh",
        ".github/workflows/superplane-domain-ci.yml",
        ".github/scripts/superplane_ci_scope.py",
        "modules/gateway/src/domain_proxy/routes.py",
    ]:
        assert not scope.persona_only([path])


def test_empty_diff_requires_full_checks():
    assert not scope.persona_only([])


def test_rename_out_of_metadata_requires_full_checks():
    assert not scope.persona_only(
        ["docs/agent-catalogue.md", "docs/renamed-catalogue.md"]
    )


def test_manual_run_keeps_full_suite(monkeypatch, tmp_path):
    output = tmp_path / "outputs"
    monkeypatch.setenv("CI_EVENT", "workflow_dispatch")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    scope.main()
    assert output.read_text() == "persona_only=false\n"


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
    assert output.read_text() == "persona_only=true\n"
    (tmp_path / "runtime.py").write_text("change")
    git("add", "runtime.py")
    git("commit", "-qm", "mixed implementation")
    monkeypatch.setenv("PR_HEAD_SHA", git("rev-parse", "HEAD"))
    scope.main()
    assert output.read_text().endswith("persona_only=false\n")
