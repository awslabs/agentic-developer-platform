"""Deployment wiring for authenticated Task API submission."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INFRA = ROOT / "infra"


def test_task_admission_is_default_off_and_reaches_the_main_gateway():
    variables = (INFRA / "variables.tf").read_text()
    start = variables.index('variable "task_api_admission_enabled"')
    block = variables[start : variables.index("}", start)]
    assert "default     = false" in block

    lambdas = (INFRA / "lambdas.tf").read_text()
    assert re.search(
        r"ADP_TASK_API_ADMISSION_ENABLED\s*=\s*tostring\(var.task_api_admission_enabled\)", lambdas
    )
    assert re.search(
        r"ADP_TASK_ADMIT_ENDPOINT\s*=\s*data.aws_ssm_parameter.gateway_apigw_invoke_url.value",
        lambdas,
    )


def test_task_admission_permission_is_exact_and_enabled_with_the_handler():
    task_api = (INFRA / "task-api.tf").read_text()

    assert "count = var.task_api_admission_enabled ? 1 : 0" in task_api
    assert 'Action   = ["execute-api:Invoke"]' in task_api
    assert "Resource = [local.task_api_admit_arn]" in task_api
    assert ("/${local.work_claim_gateway[2]}/POST/internal/v1/tasks/admit") in task_api
    assert "/*" not in task_api


def test_task_admission_permission_is_bound_to_the_deployment_region():
    task_api = (INFRA / "task-api.tf").read_text()

    assert "local.work_claim_gateway[1] == var.aws_region" in task_api
    assert "Task API admission endpoint must be the gateway" in task_api


def test_gateway_stack_can_consume_the_lambda_integration_uri():
    outputs = (INFRA / "outputs.tf").read_text()
    start = outputs.index('output "lambda_invoke_arn"')
    block = outputs[start : outputs.index("}", start)]

    assert "aws_lambda_function.github_webhook.invoke_arn" in block
