from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from tools_facade.server import create_app

ROOT = Path(__file__).resolve().parents[4]
MANIFESTS = ROOT / "modules/tools/facade/k8s"


def manifest(name):
    return yaml.safe_load((MANIFESTS / name).read_text())


def test_facade_has_separate_image_account_and_service():
    image = (ROOT / "modules/tools/facade/Dockerfile").read_text()
    assert "COPY modules/tools/facade/pyproject.toml ./" in image
    assert "COPY modules/tools/facade/tools_facade/ ./tools_facade/" in image
    assert "COPY modules/tools/pyproject.toml ./shared/pyproject.toml" in image
    assert "COPY modules/tools/adp_tools ./shared/adp_tools/" in image
    assert "pip install --no-cache-dir ./shared ." in image
    assert "USER 65532:65532" in image
    deployment = manifest("deployment.yaml")
    account = manifest("serviceaccount.yaml")
    service = manifest("service.yaml")
    assert deployment["metadata"]["name"] == "tools-facade"
    assert deployment["spec"]["template"]["spec"]["serviceAccountName"] == "tools-facade"
    assert deployment["spec"]["template"]["spec"]["containers"][0]["image"] == "REPLACE_WITH_TOOLS_FACADE_IMAGE"
    assert service["spec"]["selector"] == deployment["spec"]["selector"]["matchLabels"]
    assert account["metadata"]["annotations"]["eks.amazonaws.com/role-arn"] == "__TOOLS_FACADE_IRSA_ROLE_ARN__"
    assert TestClient(create_app()).get("/health").status_code == 200


def test_deadline_worker_has_its_own_minute_schedule_and_identity():
    job = manifest("reconciler-cronjob.yaml")
    account = manifest("reconciler-serviceaccount.yaml")
    assert job["spec"]["schedule"] == "* * * * *"
    assert job["spec"]["concurrencyPolicy"] == "Forbid"
    pod = job["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == account["metadata"]["name"] == "tools-facade-reconciler"
    assert account["metadata"]["annotations"]["eks.amazonaws.com/role-arn"] == "__TOOLS_RECONCILER_IRSA_ROLE_ARN__"
    assert pod["containers"][0]["command"] == ["python", "-m", "tools_facade.reconciler"]
    facade_image = manifest("deployment.yaml")["spec"]["template"]["spec"]["containers"][0]["image"]
    assert pod["containers"][0]["image"] == facade_image
    values = {entry["name"]: entry["value"] for entry in pod["containers"][0]["env"]}
    assert values["ADP_TOOLS_RECONCILER_KEY_ARN"] == "__TOOLS_RECONCILER_SIGNING_KEY_ARN__"
    assert values["ADP_TOOLS_RECONCILER_ROLE_ARN"] == "__TOOLS_RECONCILER_IRSA_ROLE_ARN__"
    assert values["ADP_TOOLS_CLEANUP_LAMBDA_ARN"] == "__TOOLS_CLEANUP_LAMBDA_ARN__"
    assert values["ADP_TOOLS_CLEANUP_BATCH_SIZE"] == "50"
    assert values["ADP_TOOLS_NATIVE_CLEANUP_URL"] == "__TOOLS_NATIVE_CLEANUP_URL__"
    assert values["ADP_TOOLS_OUTCOME_REPAIR_URL"] == "__TOOLS_OUTCOME_REPAIR_URL__"
    assert values["ADP_TOOLS_ATLASSIAN_READBACK_URL"] == "__TOOLS_ATLASSIAN_READBACK_URL__"
    eks = (ROOT / "platform/infra/modules/eks/main.tf").read_text()
    policy = eks.split('resource "aws_iam_role_policy" "tools_reconciler"', 1)[1].split("resource ", 1)[0]
    assert '"dynamodb:GetItem", "dynamodb:UpdateItem"' in policy
    assert 'Resource = var.tools_execution_table_arn' in policy
    assert '"dynamodb:Scan"' in policy
    facade_policy = (ROOT / "modules/tools/platform/bootstrap/facade.tf").read_text()
    assert '"dynamodb:Scan"' in facade_policy
    assert "bedrock-agentcore:InvokeGateway" not in policy
    bootstrap = (ROOT / "modules/tools/platform/bootstrap/main.tf").read_text()
    scoped = bootstrap.split('resource "aws_iam_role_policy" "reconciler_provider"', 1)[1].split("resource ", 1)[0]
    assert 'Action = "kms:Sign", Resource = aws_kms_key.reconciler[0].arn' in scoped
    assert 'Action = "lambda:InvokeFunction"' in scoped
    assert 'Action = "kms:Sign", Resource = aws_kms_key.assertion[0].arn' not in scoped
    assert '"${local.facade_issuer}:sub" = "system:serviceaccount:adp-gateway:tools-facade"' in (
        ROOT / "modules/tools/platform/bootstrap/facade.tf").read_text()
    release = (ROOT / "modules/tools/facade/deploy.sh").read_text()
    assert 'aws lambda get-function-configuration --function-name "$cleanup_lambda"' in release
    assert '.Environment.Variables.ADP_TOOLS_RECONCILER_KEY_ARN == $key' in release
    assert 's|__TOOLS_RECONCILER_IRSA_ROLE_ARN__|${reconciler_role}|' in release
    assert '__TOOLS_OUTCOME_REPAIR_URL__|${auth_base}/internal/v1/tools/outcome-repair|' in release
    assert '__TOOLS_ATLASSIAN_READBACK_URL__|${auth_base}/internal/v1/tools/atlassian/reconcile|' in release


def test_edge_and_sigv4_agent_routes_preserve_facade_paths():
    ingress = yaml.safe_load((ROOT / "modules/gateway/k8s/ingress.yaml").read_text())
    paths = ingress["spec"]["rules"][0]["http"]["paths"]
    for endpoint in ("/api/tools/adapter-release/transition", "/tools/adapter-release/transition"):
        route = next(path for path in paths if path["path"] == endpoint)
        assert route["pathType"] == "Exact"
        assert route["backend"]["service"] == {"name": "bedrockgateway", "port": {"number": 80}}
    for prefix in ("/api/agent/tools", "/api/tools", "/agent/tools", "/tools"):
        route = next(path for path in paths if path["path"] == prefix)
        assert route["backend"]["service"]["name"] == "tools-facade"
        assert route["pathType"] == "Prefix"
    assert paths[-1]["path"] == "/"
    api = (ROOT / "modules/gateway/infra/modules/api-gateway/main.tf").read_text()
    assert '"/agent/tools/{proxy+}" = {' in api
    assert 'uri                  = "http://${var.internal_alb_dns}/agent/tools/{proxy}"' in api


def test_shared_tools_stack_has_no_old_api_gateway_routes():
    stack = ROOT / "modules/tools/agentcore/infra"
    sources = "\n".join(path.read_text() for path in stack.glob("*.tf"))
    retained = (stack / "retained-legacy.tf").read_text()
    for route in ("browser", "code_interpreter", "websearch"):
        assert f'resource "aws_api_gateway_resource" "{route}"' in retained
        assert f'resource "aws_api_gateway_integration" "{route}"' in retained
        policy = retained.split(f'resource "aws_iam_role_policy" "{route}_worker"', 1)[1].split("resource ", 1)[0]
        assert 'Effect = "Deny", Action = "execute-api:Invoke"' in policy
        assert 'Effect = "Allow"' not in policy
    assert 'agentcore_tools.legacy_retired.lambda_handler' in retained
    assert 'default     = false' in (stack / "retained-legacy-variables.tf").read_text()
    assert 'ADP_TOOLS_WORKER_ROLES' not in sources
    assert 'browser_route_execution_arn' not in sources


def test_only_dedicated_irsa_can_invoke_tools_gateway():
    eks = (ROOT / "platform/infra/modules/eks/main.tf").read_text()
    tools = (ROOT / "modules/tools/platform/infra/main.tf").read_text()
    assert '"${local.oidc_issuer}:sub" = "system:serviceaccount:adp-gateway:tools-facade"' in eks
    policy = eks.split('resource "aws_iam_role_policy" "tools_facade_invoke"', 1)[1].split("resource ", 1)[0]
    assert 'Action   = ["bedrock-agentcore:InvokeGateway"]' in policy
    assert "Resource = var.tools_agentcore_gateway_arn" in policy
    assert 'count = var.tools_agentcore_gateway_arn != "" ? 1 : 0' in policy
    assert "Principal = { AWS = var.tools_facade_role_arn }" in tools
    # The façade executes tools; the engine's runtime-verification roles (the
    # environment's operator connection role plus reviewed extras) may only
    # discover them — the interceptor refuses calls without a façade assertion.
    # Everyone else is denied explicitly.
    assert "gateway_invokers = concat([var.tools_facade_role_arn], local.gateway_verifier_role_arns)" in tools
    assert '["arn:aws:iam::${var.aws_account_id}:role/ADP-Agent-${var.environment}-platform"]' in tools
    assert "Principal = { AWS = local.gateway_verifier_role_arns }" in tools
    assert 'Condition = { ArnNotEquals = { "aws:PrincipalArn" = local.gateway_invokers } }' in tools
    assert "var.adp_gateway_role_arn" not in tools
