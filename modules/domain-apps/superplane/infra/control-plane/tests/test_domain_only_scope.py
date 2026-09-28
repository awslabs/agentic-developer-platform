"""The basic platform deploy must never apply the optional Superplane root."""

from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[6]
DEPLOY_ALL = ROOT / "platform/scripts/deploy-all.sh"
MODULE_DEPLOY = ROOT / "modules/domain-apps/superplane/deploy.sh"


def test_superplane_is_installed_from_its_module() -> None:
    script = DEPLOY_ALL.read_text()
    assert 'terraform_update_apply "superplane"' not in script
    assert 'step "Step 12/12: Deploy superplane' not in script
    assert MODULE_DEPLOY.is_file()
    assert (ROOT / ".github/workflows/superplane-infra-apply.yml").is_file()


def test_legacy_platform_scope_refuses_before_preflight() -> None:
    result = subprocess.run(
        ["bash", str(DEPLOY_ALL), "--superplane-only"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 2
    assert "modules/domain-apps/superplane" in result.stderr
