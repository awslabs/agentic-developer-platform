from pathlib import Path

INFRA = Path(__file__).resolve().parents[1] / "infra"


def text(name):
    return (INFRA / name).read_text()


def test_recovery_is_a_default_off_one_minute_alias_schedule():
    variables = text("variables.tf")
    recovery = text("task-recovery.tf")
    assert 'variable "task_api_recovery_enabled"' in variables
    block = variables.split('variable "task_api_recovery_enabled"', 1)[1].split("}", 1)[0]
    assert "default     = false" in block
    assert 'name             = "task-recovery"' in recovery
    assert 'schedule_expression = "rate(1 minute)"' in recovery
    assert (
        'state               = var.task_api_recovery_enabled ? "ENABLED" : "DISABLED"' in recovery
    )
    assert "qualifier     = aws_lambda_alias.task_recovery[0].name" in recovery
    assert "source_arn    = aws_cloudwatch_event_rule.task_recovery[0].arn" in recovery


def test_task_routes_and_sparse_index_are_narrowly_granted():
    recovery = text("task-recovery.tf")
    dynamodb = text("dynamodb.tf")
    for route in ("dispatch/claim", "dispatch/settle", "recovery/claim", "recovery/settle"):
        assert f'"{route}"' in recovery
    assert 'name            = "task-work-index"' in dynamodb
    assert 'hash_key        = "task_work_shard"' in dynamodb
    assert 'range_key       = "task_due"' in dynamodb
    assert '"${aws_dynamodb_table.webhook_events.arn}/index/task-work-index"' in recovery
    assert 'Sid      = "TaskWorkAuthority"' in recovery
    assert '"dynamodb:TransactWriteItems"' in recovery
    assert "aws_dynamodb_table.agent_authority.arn" in recovery
    assert "dynamodb:Scan" not in recovery


def test_lambda_cannot_write_protected_work_locators():
    iam = text("iam.tf")
    assert 'Sid      = "DenyTaskWorkLocatorWrites"' in iam
    assert '"dynamodb:LeadingKeys" = ["TASK_WORK_ID#*"]' in iam
    assert (
        'Effect   = "Deny"'
        in iam.split('Sid      = "DenyTaskWorkLocatorWrites"', 1)[1].split("}", 1)[0]
    )


def test_lambda_publishes_a_version_and_receives_task_gateway_root():
    lambdas = text("lambdas.tf")
    assert "publish          = true" in lambdas
    assert "ADP_TASK_GATEWAY_ENDPOINT" in lambdas
    assert "ADP_TASK_API_ADMISSION_ENABLED" in lambdas
    assert "ADP_TASK_API_RECOVERY_ENABLED" in lambdas
