"""Evaluate production Terraform expressions without providers or live state."""

import json
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


def evaluate(tmp_path, source, variables, expression):
    (tmp_path / "main.tf").write_text(source)
    (tmp_path / "terraform.tfvars.json").write_text(json.dumps(variables))
    result = subprocess.run(
        ["terraform", "console", "-no-color"],
        cwd=tmp_path,
        input=f"jsonencode({expression})\n",
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(json.loads(result.stdout))


@pytest.mark.parametrize("environment", ["dev", "staging", "prod"])
@pytest.mark.parametrize("active", [False, True])
def test_tick_wiring_is_environment_scoped_and_preparation_preserves_dispatch(
    tmp_path,
    environment,
    active,
):
    source = (ROOT / "modules/gateway/infra/worker-runtime-wiring.tf").read_text()
    source = re.sub(
        r'data "aws_ssm_parameters_by_path" "worker_runtime" \{.*?^\}',
        "",
        source,
        flags=re.S | re.M,
    )
    source = source.replace(
        "data.aws_ssm_parameters_by_path.worker_runtime.names", "keys(var.discovered)"
    )
    source = source.replace(
        "data.aws_ssm_parameters_by_path.worker_runtime.values",
        "values(var.discovered)",
    )
    variables = {
        "environment": environment,
        "orchestration_agent_authority_enabled": active,
        "orchestration_webhook_events_table": "",
        "orchestration_webhook_events_kms_key_arn": "",
        "orchestration_dispatch_queue_arn": "",
        "orchestration_dispatch_queue_url": "",
    }
    source += '\nvariable "discovered" { type = map(string) }\n'
    for name, value in variables.items():
        source += f'variable "{name}" {{ type = {"bool" if isinstance(value, bool) else "string"} }}\n'
    main = (
        (ROOT / "modules/gateway/infra/main.tf")
        .read_text()
        .split('module "orchestration_tick"', 1)[1]
    )
    fields = [
        "agent_submit_queue_url",
        "webhook_events_table_name",
        "agent_authority_prepared",
    ]
    source += "\nlocals {\n"
    for field in fields:
        expression = re.search(rf"^  {field}\s*=\s*(.+)$", main, re.M).group(1)
        source += f"  observed_{field} = {expression}\n"
    source += "}\n"
    wiring = {
        "webhook_events_table": f"adp-{environment}-webhook-events",
        "webhook_events_kms_key_arn": "arn:aws:kms:eu-west-1:123456789012:key/webhook",
        "dispatch_queue_arn": f"arn:aws:sqs:eu-west-1:123456789012:adp-{environment}-agent-submit.fifo",
        "dispatch_queue_url": f"https://sqs.eu-west-1.amazonaws.com/123456789012/adp-{environment}-agent-submit.fifo",
    }
    variables["discovered"] = {
        f"/adp/{environment}/webhook-ingress/worker-runtime/wiring": json.dumps(wiring),
        "/adp/foreign/webhook-ingress/worker-runtime/wiring": json.dumps(
            {"dispatch_queue_url": "foreign"}
        ),
    }
    result = evaluate(
        tmp_path,
        source,
        variables,
        "{queue=local.observed_agent_submit_queue_url, table=local.observed_webhook_events_table_name, prepared=local.observed_agent_authority_prepared}",
    )
    assert result == {
        "queue": wiring["dispatch_queue_url"] if active else "",
        "table": wiring["webhook_events_table"] if active else "",
        "prepared": True,
    }
    variables["discovered"] = {}
    assert (
        evaluate(tmp_path, source, variables, "local.observed_agent_authority_prepared")
        is False
    )


@pytest.mark.parametrize("retired", [False, True])
@pytest.mark.parametrize(
    "deployer",
    ["Admin", "adp-prod-agent-authority-worker-role", "adp-prod-agent-scaledjob-role"],
)
def test_platform_never_grants_protected_worker_admin_and_retires_legacy(
    tmp_path,
    retired,
    deployer,
):
    source = (ROOT / "platform/infra/main.tf").read_text()
    expression = re.search(
        r"  cluster_admin_principal_arns = (\[.*?\n  \])", source, re.S
    ).group(1)
    expression = expression.replace(
        "data.aws_iam_roles.ci_runner.arns", "[local.ci_runner_role_arn]"
    )
    expression = expression.replace(
        "data.aws_caller_identity.current.account_id", '"123456789012"'
    )
    prefix = "arn:aws:iam::123456789012:role/"
    source = (
        """
variable "agent_legacy_worker_admin_retired" { type = bool }
variable "manage_ci_runner_cluster_admin" { default = true }
variable "extra_cluster_admin_principal_arns" { type = list(string) }
variable "deployer_role_arn" { type = string }
locals {
  name_prefix = "adp-prod"
  deployer_role_arn = var.deployer_role_arn
  ci_runner_role_arn = "arn:aws:iam::123456789012:role/adp-prod-agent-runner-role"
  cluster_admin_principal_arns = """
        + expression
        + "\n}\n"
    )
    result = evaluate(
        tmp_path,
        source,
        {
            "agent_legacy_worker_admin_retired": retired,
            "deployer_role_arn": prefix + deployer,
            "extra_cluster_admin_principal_arns": [
                prefix + "Admin",
                prefix + "adp-prod-agent-scaledjob-role",
                prefix + "adp-prod-agent-authority-worker-role",
            ],
        },
        "local.cluster_admin_principal_arns",
    )
    assert prefix + "Admin" in result
    assert prefix + "adp-prod-agent-authority-worker-role" not in result
    assert (prefix + "adp-prod-agent-scaledjob-role" in result) is not retired
