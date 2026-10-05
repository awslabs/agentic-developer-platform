"""Source, workflow and offline state-ownership fixture (no AWS provisioning)."""

import re
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[4]
SHARED = ROOT / "modules/tools/agentcore"
CYBER = ROOT / "modules/domain-apps/cyber/tools"
WORKFLOWS = ROOT / ".github/workflows"


def test_shared_images_exclude_cyber_and_gateway_runtime_source():
    for dockerfile in ("Dockerfile", "Dockerfile.browser"):
        lines = (SHARED / dockerfile).read_text().splitlines()
        copies = [line.split()[1] for line in lines if line.startswith("COPY ")]
        # The exact stdlib maintenance bundle is a build input, not gateway
        # application code or a runtime import dependency.
        security_bundle = "modules/gateway/security/stdlib/"
        assert all(path.startswith("modules/tools/") or path == security_bundle for path in copies)
        if dockerfile == "Dockerfile.browser":
            assert copies.count(security_bundle) == 1
            assert f"COPY {security_bundle} /opt/adp-stdlib-security/" in lines
        assert "modules/tools/adp_tools" in copies or "modules/tools/adp_tools/" in copies
        assert "modules/tools/agentcore/agentcore_tools" in copies or "modules/tools/agentcore/agentcore_tools/" in copies
        assert not any("cyber" in path or ("gateway" in path and path != security_bundle) for path in copies)
        assert "EXPOSE " not in "\n".join(lines)
        ignore = (SHARED / (dockerfile + ".dockerignore")).read_text()
        assert "!modules/domain-apps" not in ignore
        gateway_rules = [line for line in ignore.splitlines() if line.startswith("!modules/gateway")]
        assert gateway_rules == (["!modules/gateway/", "!modules/gateway/security/",
                                  "!modules/gateway/security/stdlib/", "!modules/gateway/security/stdlib/**"]
                                 if dockerfile == "Dockerfile.browser" else [])
    broker = (ROOT / "modules/domain-apps/cyber/browser/Dockerfile").read_text()
    assert "COPY --chown=agent:agent modules/tools/agentcore/agentcore_tools/ /app/agentcore_tools/" in broker
    assert "PYTHONPATH=/app:/app/skills/url-analysis" in broker
    overlay = (ROOT / "modules/domain-apps/cyber/agent/Dockerfile.browser-overlay").read_text()
    assert "modules/tools/agentcore/agentcore_tools/ /app/agentcore_tools/" in overlay
    for source in (SHARED / "agentcore_tools").rglob("*.py"):
        assert not re.search(r"\b(?:import|from)\s+cyber_tools\b", source.read_text()), source
    for script in ("build-image.sh", "deploy.sh"):
        subprocess.run(["bash", "-n", str(SHARED / "infra" / script)], check=True)


def test_shared_workflows_are_manual_apply_only_and_keep_cyber_ci():
    ci = yaml.load((WORKFLOWS / "shared-agentcore-tools-ci.yml").read_text(), Loader=yaml.BaseLoader)
    deploy = yaml.load((WORKFLOWS / "shared-agentcore-tools-deploy.yml").read_text(), Loader=yaml.BaseLoader)
    cyber = yaml.load((WORKFLOWS / "cyber-tools-ci.yml").read_text(), Loader=yaml.BaseLoader)
    assert ci["on"]["pull_request"]["paths"] and "modules/tools/agentcore/**" in ci["on"]["pull_request"]["paths"]
    assert set(deploy["on"]) == {"workflow_dispatch"}
    assert "apply" in deploy["on"]["workflow_dispatch"]["inputs"]["mode"]["options"]
    assert any("Cyber tools" in job.get("name", "") or "modules/domain-apps/cyber/tools/**" in cyber["on"]["pull_request"]["paths"] for job in cyber["jobs"].values())
    source = (WORKFLOWS / "shared-agentcore-tools-deploy.yml").read_text()
    for guard in ("git merge-base --is-ancestor", "get-caller-identity", "tools/agentcore/", "deploy.sh plan", "deploy.sh apply", "source_sha"):
        assert guard in source
    assert "aws_api_gateway_deployment" not in source
    assert "codebuild-run" in source
    assert "docker build" not in (WORKFLOWS / "shared-agentcore-tools-ci.yml").read_text()
    assert "docker build" not in (WORKFLOWS / "cyber-tools-ci.yml").read_text()
    buildspec = yaml.load((SHARED / "infra/buildspec.yml").read_text(), Loader=yaml.BaseLoader)
    assert "build" in buildspec["phases"]


def test_migration_fixture_cannot_delete_or_replace_existing_resources():
    guard = (SHARED / "infra/deploy.sh").read_text()
    assert guard.count('.change.actions | index("delete")') == 2
    assert "aws_api_gateway_deployment" not in "\n".join(path.read_text() for path in (SHARED / "infra").glob("*.tf"))
    cyber_source = "\n".join(path.read_text() for path in (CYBER / "infra").glob("*.tf"))
    for address in ("aws_dynamodb_table\" \"operations", "aws_api_gateway_resource\" \"websearch", "aws_api_gateway_resource\" \"code_interpreter", "aws_sqs_queue\" \"browser"):
        assert address not in cyber_source
