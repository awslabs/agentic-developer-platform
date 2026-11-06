import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUNBOOK = ROOT / "docs/tools/deployment.md"


def test_tools_runbook_keeps_owner_and_protected_environment_boundary():
    text = RUNBOOK.read_text()
    assert "not evidence of a deployment" in text
    assert "only `main`" in text
    assert "`adp-tools-deploy-dev`" in text
    assert "`enable_tools_deployment = false`" in text
    assert "`ADP_TOOLS_DEPLOY_TFVARS_JSON`" in text
    assert "environment **secret**" in text
    assert "prod remains dispatch-only with a human reviewer" in text
    assert "approved private target configuration" in text
    assert "the source ARN shape alone has not" in text
    for workflow in ("tools-platform-deploy.yml", "tools-interceptor-deploy.yml", "tools-facade-deploy.yml"):
        assert workflow in text


def test_tools_runbook_requires_state_separation_and_real_interceptor_before_targets():
    text = RUNBOOK.read_text()
    assert "tools/bootstrap/dev.tfstate" in text
    assert "tools/platform/dev.tfstate" in text
    assert "two states" in text
    assert "403-only placeholder" in text
    assert "privileged" in text
    phases = text.split("## Ordered dev verification after merge and approved apply", 1)[1]
    assert phases.index("1. Publish the **real**") < phases.index("2. Before platform/target activation")
    assert phases.index("2. Before platform/target activation") < phases.index("3. Run `tools-platform-deploy.yml`")
    assert phases.index("3. Run `tools-platform-deploy.yml`") < phases.index("4. Run `tools-facade-deploy.yml`")
    assert phases.index("4. Run `tools-facade-deploy.yml`") < phases.index("5. The tools owner resumes #7253")
    for field in ("interceptor_version", "interceptor_code_sha256", "interceptor_lambda_arn"):
        assert f"`{field}`" in phases
    for criterion in range(1, 6):
        assert f"AC-{criterion:02}" in text
        assert f"SCN-{criterion:02}" in text
    assert "SCN-06" in text
    assert "**not run**" in text


def test_tools_runbook_rollback_preserves_state_and_previous_receipts():
    text = re.sub(r"\s+", " ", RUNBOOK.read_text().split("## Recovery and stop conditions", 1)[1].replace("**", ""))
    for prerequisite in (
        "rollback_to_version", "prior verified numbered version", "rollback_to_digest",
        "prior verified", "reviewed state plan", "Preserve the previous", "never destroy keys",
        "selected source package", "retain state and receipts",
    ):
        assert prerequisite in text


def test_tools_runbook_is_indexed_and_local_links_exist():
    assert "[deployment.md](deployment.md)" in (ROOT / "docs/tools/README.md").read_text()
    links = re.findall(r"\]\((\.\.?/[^)#]+)", RUNBOOK.read_text())
    assert links
    for target in links:
        assert (RUNBOOK.parent / target).resolve().is_file(), target
