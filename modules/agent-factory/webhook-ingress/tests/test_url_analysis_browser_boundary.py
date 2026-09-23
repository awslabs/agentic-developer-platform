"""Structural regression checks for the URL-analysis browser trust boundary."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
INFRA = ROOT / "modules/agent-factory/webhook-ingress/infra"
CYBER_INFRA = ROOT / "modules/domain-apps/cyber/infra"
SKILL = ROOT / "modules/domain-apps/cyber/agent/skills/url-analysis"


def test_reasoning_worker_explicitly_denies_all_direct_browser_apis() -> None:
    worker_policy = (INFRA / "scaledjob-iam.tf").read_text()
    assert 'Sid      = "DenyDirectAgentCoreBrowser"' in worker_policy
    assert 'Effect   = "Deny"' in worker_policy
    assert 'Action   = ["bedrock-agentcore:*"]' in worker_policy
    assert 'Sid    = "BedrockAgentCoreBrowser"' not in worker_policy


def test_cyber_worker_explicitly_denies_all_direct_browser_apis() -> None:
    worker_policy = (CYBER_INFRA / "url_analysis.tf").read_text()
    assert "role = aws_iam_role.cyber_worker.id" in worker_policy
    assert 'Sid      = "DenyDirectAgentCoreBrowser"' in worker_policy
    assert 'Effect   = "Deny"' in worker_policy
    assert 'Action   = ["bedrock-agentcore:*"]' in worker_policy
    assert 'Effect = "Allow"' not in worker_policy


def test_broker_has_distinct_identity_and_no_invoke_browser_permission() -> None:
    broker = (INFRA / "url-analysis-browser-broker.tf").read_text()
    assert "url-analysis-browser-broker-sa" in broker
    assert "permissions_boundary" in broker
    assert '"bedrock-agentcore:ConnectBrowserAutomationStream"' in broker
    assert '"bedrock-agentcore:StartBrowserSession"' in broker
    assert "bedrock-agentcore:InvokeBrowser" not in broker
    assert 'args    = ["/app/skills/url-analysis/browser_broker.py"]' in broker


def test_broker_uses_a_dedicated_port_consistently() -> None:
    broker_infra = (INFRA / "url-analysis-browser-broker.tf").read_text()
    worker_netpol = (INFRA / "scaledjob-netpol.tf").read_text()
    broker_server = (SKILL / "browser_broker.py").read_text()
    broker_client = (SKILL / "browser_client.py").read_text()

    assert broker_infra.count("8765") == 5
    assert "port     = 8765" in worker_netpol
    assert 'URL_ANALYSIS_BROKER_PORT", "8765"' in broker_server
    assert "url-analysis-browser-broker.adp-agents.svc.cluster.local:8765" in (broker_client)
    assert "8080" not in broker_infra
    assert '"8080"' not in broker_server
    assert ":8080" not in broker_client
    assert "port     = 8080" not in worker_netpol


def test_browser_deny_can_deploy_without_worker_authority_migration() -> None:
    broker = (INFRA / "url-analysis-browser-broker.tf").read_text()
    start = broker.index('resource "aws_iam_role_policy" "agent_scaledjob_browser_deny"')
    policy = broker[start:broker.index('\nresource ', start + 1)]
    assert 'role = aws_iam_role.agent_scaledjob.id' in policy
    assert 'Effect   = "Deny"' in policy
    assert 'Action   = ["bedrock-agentcore:*"]' in policy
    assert 'Resource = "*"' in policy
    assert 'Effect   = "Allow"' not in policy
    assert 'agent_authority' not in policy


def test_orchestration_cannot_select_raw_browser_path() -> None:
    for example in sorted((SKILL / "examples").glob("*.py")):
        tree = ast.parse(example.read_text())
        imports = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        imports.update(
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        )
        assert "browser_client" in imports
        assert not imports & {"boto3", "playwright", "bedrock_agentcore"}
