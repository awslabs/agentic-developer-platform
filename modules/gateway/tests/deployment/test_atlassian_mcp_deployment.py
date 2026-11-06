"""The real deployment paths retain protected MCP settings and a recovery consumer."""

from pathlib import Path

import yaml

GATEWAY = Path(__file__).resolve().parents[2]
ROOT = GATEWAY.parents[1]
SETTINGS = (GATEWAY / "k8s/configmap.yaml").read_text()
INFRA = (GATEWAY / "infra/atlassian-mcp-config.tf").read_text()
WORKFLOW = (ROOT / ".github/workflows/gateway-deploy.yml").read_text()
LAUNCHER = (ROOT / "platform/scripts/deploy-all.sh").read_text()


def test_private_setup_storage_requires_qualified_metadata_and_preserves_dcr_state():
    assert 'name                    = "adp/${var.environment}/connections/atlassian-mcp/config"' in INFRA
    assert 'name                    = "adp/${var.environment}/connections/atlassian-mcp/client"' in INFRA
    assert 'secret_string = jsonencode({ state = "unregistered" })' in INFRA
    assert "ignore_changes = [secret_string]" in INFRA
    assert 'resource "aws_secretsmanager_secret_version" "atlassian_mcp_config"' not in INFRA
    assert '(var.atlassian_mcp_contract_revision == "") == (var.atlassian_mcp_metadata_sha256 == "")' in INFRA
    for field in ("config-ref", "contract-revision", "metadata-sha256"):
        assert f'"/adp/${{var.environment}}/gateway/atlassian-mcp-{field}"' in INFRA
    assert 'default     = ""' in INFRA


def test_both_gateway_renderers_bind_only_nonsecret_settings():
    fields = {"CONFIG_REF": "config-ref", "CONTRACT_REVISION": "contract-revision", "METADATA_SHA256": "metadata-sha256"}
    for variable, parameter in fields.items():
        assert SETTINGS.count(f'BG_ATLASSIAN_{variable}: "__ATLASSIAN_{variable}__"') == 1
        for renderer in (WORKFLOW, LAUNCHER):
            assert f'ATLASSIAN_{variable}=$(get_ssm "/adp/${{ENVIRONMENT}}/gateway/atlassian-mcp-{parameter}" "")' in renderer or (
                f'ATLASSIAN_{variable}=$(_get_ssm "/adp/${{ENVIRONMENT}}/gateway/atlassian-mcp-{parameter}" "")' in renderer
            )
            assert f"s|__ATLASSIAN_{variable}__|${{ATLASSIAN_{variable}}}|g" in renderer
    assert "BG_ATLASSIAN_APP_SECRET" not in SETTINGS
    assert "client_secret" not in SETTINGS
    assert "Incomplete Atlassian MCP provider plan binding" in WORKFLOW
    assert "Incomplete Atlassian MCP provider plan binding" in LAUNCHER
    rendered = SETTINGS.replace(
        "__ATLASSIAN_CONFIG_REF__",
        "arn:aws:secretsmanager:us-east-1:123456789012:secret:adp/dev/connections/atlassian-mcp/config-ABC123",
    )
    rendered = rendered.replace("__ATLASSIAN_CONTRACT_REVISION__", "reviewed-v1")
    rendered = rendered.replace("__ATLASSIAN_METADATA_SHA256__", "a" * 64)
    data = yaml.safe_load(rendered)["data"]
    assert data["BG_ATLASSIAN_CONFIG_REF"].endswith("/config-ABC123")
    assert data["BG_ATLASSIAN_CONTRACT_REVISION"] == "reviewed-v1"
    assert data["BG_ATLASSIAN_METADATA_SHA256"] == "a" * 64


def test_one_tick_recovery_uses_same_release_image_and_gateway_settings():
    worker = yaml.safe_load((GATEWAY / "k8s/atlassian-mcp-recovery.yaml").read_text())
    assert worker["kind"] == "CronJob"
    spec = worker["spec"]
    assert spec["concurrencyPolicy"] == "Forbid"
    assert spec["jobTemplate"]["spec"]["activeDeadlineSeconds"] == 240
    pod = spec["jobTemplate"]["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == "gateway-service"
    container = pod["containers"][0]
    assert container["image"] == "REPLACE_WITH_GATEWAY_IMAGE"
    assert container["command"] == ["python", "-m", "src.shared.identity.oauth_setup_worker", "--once"]
    assert container["envFrom"] == [{"configMapRef": {"name": "bedrockgateway-config"}}]
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    for renderer, image in ((WORKFLOW, "RELEASE_IMAGE"), (LAUNCHER, "GATEWAY_IMAGE")):
        assert f"s|REPLACE_WITH_GATEWAY_IMAGE|${{{image}}}|g" in renderer
        assert "atlassian-mcp-recovery.yaml | kubectl apply" in renderer
        assert "deployment.yaml|atlassian-mcp-recovery.yaml" in renderer
