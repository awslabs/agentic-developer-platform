"""Basic platform builds exclude domain apps; existing installations retain them."""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "platform/scripts/resolve-domain-builds.py"
MANIFESTS = ROOT / "modules/domain-apps"


def select(state: str = "", explicit: str = "", explicit_set: bool = False,
           superplane: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--manifest-root", str(MANIFESTS),
         "--explicit", explicit, "--explicit-set", "x" if explicit_set else "",
         "--superplane-enabled", "true" if superplane else "false",
         "--superplane-only", "false", "--skip-superplane", "false"],
        input=state, text=True, capture_output=True,
    )


def test_fresh_base_has_no_domain_build_projects() -> None:
    result = select()
    assert result.returncode == 0
    assert json.loads(result.stdout) == []


def test_existing_domain_projects_are_retained_until_explicitly_removed() -> None:
    state = '\n'.join([
        'module.codebuild.aws_iam_role.project["cyber-browser"]',
        'module.codebuild.aws_codebuild_project.main["superplane-api"]',
    ])
    assert json.loads(select(state).stdout) == ["cyber", "superplane"]
    assert json.loads(select(state, explicit="none", explicit_set=True).stdout) == []


def test_superplane_install_selects_its_builds_and_rejects_missing_manifest() -> None:
    assert json.loads(select(superplane=True).stdout) == ["superplane"]
    assert select(explicit_set=True, superplane=True).returncode != 0
    assert select(explicit="unknown", explicit_set=True).returncode != 0
