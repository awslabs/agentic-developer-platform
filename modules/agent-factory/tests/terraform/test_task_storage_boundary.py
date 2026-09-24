"""Task persistence infrastructure and deputy boundaries for issue #5794."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
INFRA = ROOT / "webhook-ingress" / "infra"


def _source(name: str) -> str:
    return (INFRA / name).read_text()


def test_existing_request_table_gets_one_sparse_task_work_index() -> None:
    source = _source("dynamodb.tf")
    assert source.count('resource "aws_dynamodb_table" "webhook_events"') == 1
    assert source.count('name            = "task-work-index"') == 1
    assert 'hash_key        = "task_work_shard"' in source
    assert 'range_key       = "task_due"' in source
    assert 'projection_type = "ALL"' in source


def test_gateway_owns_cross_table_task_transactions_without_scan() -> None:
    source = _source("iam.tf")
    policy = source.split('resource "aws_iam_role_policy" "gateway_task_storage"', 1)[1]
    assert '"dynamodb:TransactWriteItems"' in policy
    assert "aws_dynamodb_table.webhook_events.arn" in policy
    assert "aws_dynamodb_table.agent_authority.arn" in policy
    assert 'index/task-work-index' in policy
    assert '"dynamodb:Scan"' not in policy
    locator_delete = policy.split('Sid      = "TaskLocatorRetentionDelete"', 1)[1]
    assert 'Action   = ["dynamodb:DeleteItem"]' in locator_delete
    assert '"TASK_WORK_ID#*"' in locator_delete


def test_worker_mixed_writes_are_denied_for_every_task_namespace() -> None:
    source = _source("scaledjob-iam.tf")
    deny = source.split("agent_task_protection_deny = [", 1)[1].split("]\n}", 1)[0]
    assert '"ForAnyValue:StringLike"' in deny
    assert 'arn:aws:dynamodb:*:*:table/adp-*-webhook-events' in deny
    for action in ("PutItem", "UpdateItem", "DeleteItem", "BatchWriteItem", "TransactWriteItems"):
        assert f'"dynamodb:{action}"' in deny
    for prefix in (
        "TASK#*",
        "TASK_RUN#*",
        "TASK_EVENTS#*",
        "TASK_COMMANDS#*",
        "TASK_TURNS#*",
        "TASK_OPS#*",
        "TASK_IDEMP#*",
        "TASK_WORK#*",
        "TASK_REPORT#*",
        "TASK_ARTIFACT#*",
    ):
        assert f'"{prefix}"' in deny
    assert 'adp-*-chat-artifacts-*/tasks/*' in deny


def test_legacy_ingress_cannot_act_as_task_locator_deputy() -> None:
    source = _source("iam.tf")
    deny = source.split('Sid      = "DenyTaskWorkLocatorWrites"', 1)[1].split("},\n    ]", 1)[0]
    assert 'Effect   = "Deny"' in deny
    assert '"ForAnyValue:StringLike"' in deny
    assert '"TASK_WORK_ID#*"' in deny
    for action in ("PutItem", "UpdateItem", "DeleteItem", "BatchWriteItem", "TransactWriteItems"):
        assert f'"dynamodb:{action}"' in deny
