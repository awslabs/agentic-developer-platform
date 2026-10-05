"""Superplane build projects are declared by the app with distinct output lanes."""

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[5]
MANIFEST = ROOT / "modules/domain-apps/superplane/codebuild/projects.json"
PROJECTS = {
    "superplane-api": ("api.yml", "adp-superplane-api"),
    "superplane-controller": ("controller.yml", "adp-superplane-controller"),
    "superplane-monitor": ("monitor.yml", "adp-superplane-platform-monitor"),
    "superplane-executor": ("executor.yml", "adp-superplane-executor"),
}


@pytest.mark.parametrize("name", sorted(PROJECTS))
def test_each_superplane_build_has_its_own_spec_and_repository(name: str) -> None:
    projects = json.loads(MANIFEST.read_text())
    spec, repository = PROJECTS[name]
    assert set(projects) == set(PROJECTS)
    assert projects[name]["buildspec"] == f"modules/domain-apps/superplane/releases/buildspecs/{spec}"
    assert (ROOT / projects[name]["buildspec"]).is_file()
    assert projects[name]["ecr_repos"] == [repository]
    assert projects[name]["privileged"] is True
    assert projects[name]["privileged_why"]


def test_superplane_projects_are_selected_only_from_app_manifest() -> None:
    codebuild = (ROOT / "platform/infra/modules/codebuild/main.tf").read_text()
    app_root = (ROOT / "modules/domain-apps/superplane/infra/control-plane/main.tf").read_text()
    assert '"superplane-api" = {' not in codebuild
    assert "modules/domain-apps" not in codebuild
    assert 'module "image_builds"' in app_root
    assert 'codebuild/projects.json' in app_root
