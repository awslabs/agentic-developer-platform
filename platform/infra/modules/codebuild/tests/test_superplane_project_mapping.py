"""Superplane CodeBuild projects must each map to their own buildspec and ECR repository.

The defect this guards against: copy-pasting a sibling project entry and forgetting to
update the buildspec path or ECR repository name. With four near-identical entries
(api, controller, monitor, executor), a wrong buildspec means the executor lane builds
the API image, and a wrong ECR repository means the executor image overwrites a sibling's
repository — or two projects share write access, which the module's existing uniqueness
assertion would also catch, but only at plan time rather than in the test suite.

This test reads the Terraform source directly so it runs without `terraform init` and
catches mapping errors before any plan or apply.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

MODULE_DIR = Path(__file__).resolve().parent.parent
MAIN_TF = MODULE_DIR / "main.tf"
BUILDSPECS_DIR = (
    Path(__file__).resolve().parents[5]
    / "modules"
    / "domain-apps"
    / "superplane"
    / "releases"
    / "buildspecs"
)

# Every superplane component that must have a dedicated CodeBuild project,
# its expected buildspec filename and its expected ECR repository name.
SUPERPLANE_PROJECTS = {
    "superplane-api": {
        "buildspec": "modules/domain-apps/superplane/releases/buildspecs/api.yml",
        "ecr_repo": "adp-superplane-api",
    },
    "superplane-controller": {
        "buildspec": "modules/domain-apps/superplane/releases/buildspecs/controller.yml",
        "ecr_repo": "adp-superplane-controller",
    },
    "superplane-monitor": {
        "buildspec": "modules/domain-apps/superplane/releases/buildspecs/monitor.yml",
        "ecr_repo": "adp-superplane-platform-monitor",
    },
    "superplane-executor": {
        "buildspec": "modules/domain-apps/superplane/releases/buildspecs/executor.yml",
        "ecr_repo": "adp-superplane-executor",
    },
}


def _parse_core_projects() -> dict[str, str]:
    """Extract the core_projects map entries from main.tf as raw text blocks.

    Returns a dict keyed by project name (e.g. "superplane-executor") whose values
    are the raw HCL text of that project's block — enough to assert field values
    without a full HCL parser.
    """
    text = MAIN_TF.read_text(encoding="utf-8")
    # Match each "key" = { ... } block inside core_projects.  The block ends at
    # a closing brace at the same indent level (4 spaces).
    pattern = re.compile(
        r'^    "([^"]+)"\s*=\s*\{(.*?)\n    \}',
        re.MULTILINE | re.DOTALL,
    )
    return {m.group(1): m.group(2) for m in pattern.finditer(text)}


@pytest.fixture(scope="module")
def core_projects() -> dict[str, str]:
    return _parse_core_projects()


class TestSuperplaneProjectsExist:
    """Every expected superplane component has a core_projects entry."""

    @pytest.mark.parametrize("project_key", sorted(SUPERPLANE_PROJECTS))
    def test_project_present(
        self, core_projects: dict[str, str], project_key: str
    ) -> None:
        assert project_key in core_projects, (
            f"{project_key} is missing from core_projects in main.tf. "
            f"The executor (or another superplane component) has no CodeBuild lane."
        )


class TestSuperplaneBuildspecMapping:
    """Each superplane project references its own component's buildspec, not a sibling's."""

    @pytest.mark.parametrize("project_key", sorted(SUPERPLANE_PROJECTS))
    def test_buildspec_matches_own_component(
        self, core_projects: dict[str, str], project_key: str
    ) -> None:
        block = core_projects.get(project_key, "")
        expected = SUPERPLANE_PROJECTS[project_key]["buildspec"]
        declared = re.search(r'\bbuildspec\s*=\s*("[^"\n]+")', block)
        assert declared and json.loads(declared.group(1)) == expected, (
            f'{project_key} does not reference buildspec "{expected}". '
            f"It may be pointing at a sibling's buildspec."
        )

    @pytest.mark.parametrize("project_key", sorted(SUPERPLANE_PROJECTS))
    def test_buildspec_file_exists(self, project_key: str) -> None:
        buildspec = SUPERPLANE_PROJECTS[project_key]["buildspec"]
        full_path = Path(__file__).resolve().parents[5] / buildspec
        assert full_path.exists(), (
            f'The buildspec "{buildspec}" referenced by {project_key} does not exist on disk.'
        )


class TestSuperplaneEcrMapping:
    """Each superplane project declares only its own ECR repository."""

    @pytest.mark.parametrize("project_key", sorted(SUPERPLANE_PROJECTS))
    def test_ecr_repo_matches_own_component(
        self, core_projects: dict[str, str], project_key: str
    ) -> None:
        block = core_projects.get(project_key, "")
        expected = SUPERPLANE_PROJECTS[project_key]["ecr_repo"]
        declared = re.search(r"\becr_repos\s*=\s*(\[[^\]]*\])", block)
        assert declared and json.loads(declared.group(1)) == [expected], (
            f'{project_key} does not declare ECR repository "{expected}". '
            f"It may be pushing to a sibling's repository."
        )

    @pytest.mark.parametrize("project_key", sorted(SUPERPLANE_PROJECTS))
    def test_no_sibling_ecr_repo(
        self, core_projects: dict[str, str], project_key: str
    ) -> None:
        block = core_projects.get(project_key, "")
        sibling_repos = [
            spec["ecr_repo"]
            for key, spec in SUPERPLANE_PROJECTS.items()
            if key != project_key
        ]
        for repo in sibling_repos:
            assert f'"{repo}"' not in block, (
                f'{project_key} references sibling ECR repository "{repo}". '
                f"Each project must push only to its own repository."
            )
