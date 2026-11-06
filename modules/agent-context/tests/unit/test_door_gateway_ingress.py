"""The released Door ingress stays internal, TLS-only and limited to MCP."""

import os
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/deploy-door-gateway-ingress.sh"


def deploy(tmp_path, overrides=None):
    kubectl = tmp_path / "kubectl"
    kubectl.write_text('#!/bin/sh\ncat > "$RENDERED_INGRESS"\n')
    kubectl.chmod(0o700)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "RENDERED_INGRESS": str(tmp_path / "ingress.yaml"),
        "NAMESPACE": "agent-context",
        "AWS_REGION": "us-east-1",
        "ACCOUNT_ID": "123456789012",
        **(overrides or {}),
    }
    result = subprocess.run(["bash", str(SCRIPT)], cwd=ROOT, env=env, capture_output=True, text=True)
    return result, tmp_path / "ingress.yaml"


def test_private_door_mcp_ingress_is_opt_in(tmp_path):
    result, manifest = deploy(tmp_path, {key: "" for key in (
        "DOOR_MCP_HOST", "DOOR_MCP_CERTIFICATE_ARN", "DOOR_MCP_ALB_SOURCE_CIDR"
    )})
    assert result.returncode == 0, result.stderr
    assert not manifest.exists()


def test_private_door_mcp_ingress_routes_to_existing_service(tmp_path):
    result, manifest = deploy(tmp_path, {
        "DOOR_MCP_HOST": "door.example.invalid",
        "DOOR_MCP_CERTIFICATE_ARN": "arn:aws:acm:us-east-1:123456789012:certificate/11111111-1111-1111-1111-111111111111",
        "DOOR_MCP_ALB_SOURCE_CIDR": "10.20.0.0/16",
    })
    assert result.returncode == 0, result.stderr
    ingress, policy = yaml.safe_load_all(manifest.read_text())
    assert ingress["kind"] == "Ingress"
    assert ingress["metadata"]["annotations"]["alb.ingress.kubernetes.io/scheme"] == "internal"
    assert ingress["metadata"]["annotations"]["alb.ingress.kubernetes.io/listen-ports"] == '[{"HTTPS": 443}]'
    assert ingress["spec"]["rules"][0]["host"] == "door.example.invalid"
    path = ingress["spec"]["rules"][0]["http"]["paths"][0]
    assert path["path"] == "/mcp" and path["pathType"] == "Prefix"
    assert path["backend"]["service"] == {"name": "context-mcp", "port": {"number": 5100}}
    assert policy["spec"]["ingress"][0]["from"] == [{"ipBlock": {"cidr": "10.20.0.0/16"}}]
    assert policy["spec"]["ingress"][0]["ports"] == [{"port": 5100, "protocol": "TCP"}]
    assert "scripts/deploy-door-gateway-ingress.sh" in (ROOT / "deploy.sh").read_text()
    workflow = (ROOT.parents[1] / ".github/workflows/agent-context-deploy.yml").read_text()
    assert "scripts/deploy-door-gateway-ingress.sh" in workflow
    for name in ("DOOR_MCP_HOST", "DOOR_MCP_CERTIFICATE_ARN", "DOOR_MCP_ALB_SOURCE_CIDR"):
        assert f"{name}: ${{{{ vars.ADP_{name} }}}}" in workflow


def test_door_mcp_ingress_refuses_missing_or_public_inputs(tmp_path):
    valid = {
        "DOOR_MCP_HOST": "door.example.invalid",
        "DOOR_MCP_CERTIFICATE_ARN": "arn:aws:acm:us-east-1:123456789012:certificate/11111111-1111-1111-1111-111111111111",
        "DOOR_MCP_ALB_SOURCE_CIDR": "10.20.0.0/16",
    }
    for change in (
        {"DOOR_MCP_HOST": ""},
        {"DOOR_MCP_HOST": "*.example.invalid"},
        {"DOOR_MCP_CERTIFICATE_ARN": ""},
        {"DOOR_MCP_ALB_SOURCE_CIDR": "0.0.0.0/0"},
    ):
        result, manifest = deploy(tmp_path, {**valid, **change})
        assert result.returncode != 0
        assert not manifest.exists()
