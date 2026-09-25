from datetime import UTC, datetime
from decimal import Decimal

import boto3
import pytest
from moto import mock_aws

from src.admin.persona_models.schemas import TaskPolicyResponse
from src.agentauth.task_service_policy import TaskServicePolicyError, TaskServicePolicyStore

TENANT = "tenant-1"
PRINCIPAL = "principal-1"
NOW = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)


def policy(**overrides):
    value = {
        "status": "active",
        "allowed_personas": ["agent-task-investigator"],
        "task_scopes": ["submit", "read", "input", "cancel", "artifacts"],
        "model_policy_version": "models-v1",
        "limits": {
            "max_duration_minutes": 30,
            "max_turns": 8,
            "max_output_tokens_per_turn": 4096,
            "max_usd_per_task": Decimal("1"),
        },
    }
    value.update(overrides)
    return value


@pytest.fixture
def store():
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(
            TableName="authority",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}, {"AttributeName": "sk", "AttributeType": "S"}],
            ProvisionedThroughput={"ReadCapacityUnits": 5, "WriteCapacityUnits": 5},
        )
        yield TaskServicePolicyStore(table_name="authority", client=client, clock=lambda: NOW), client


def test_policy_is_absent_by_default_and_services_cannot_inherit_authority(store):
    repository, _ = store
    assert repository.get(tenant_id=TENANT, canonical_principal_id=PRINCIPAL) is None


def test_human_admin_write_is_version_fenced_and_audited_atomically(store):
    repository, client = store
    created = repository.put(
        tenant_id=TENANT,
        canonical_principal_id=PRINCIPAL,
        expected_version=0,
        policy=policy(),
        updated_by="human-admin-1",
    )
    assert created["version"] == 1
    assert repository.get(tenant_id=TENANT, canonical_principal_id=PRINCIPAL) == created
    assert TaskPolicyResponse.model_validate(created).canonical_principal_id == PRINCIPAL
    audit = client.get_item(
        TableName="authority",
        Key={
            "pk": {"S": f"TENANT#{TENANT}"},
            "sk": {"S": f"TASK_POLICY_AUDIT#{PRINCIPAL}#VERSION#0000000001"},
        },
        ConsistentRead=True,
    )["Item"]
    assert audit["updated_by"] == {"S": "human-admin-1"}
    assert len(audit["policy_digest"]["S"]) == 64


def test_stale_expected_version_cannot_overwrite_current_policy(store):
    repository, _ = store
    repository.put(
        tenant_id=TENANT,
        canonical_principal_id=PRINCIPAL,
        expected_version=0,
        policy=policy(),
        updated_by="human-admin-1",
    )
    with pytest.raises(TaskServicePolicyError, match="version_conflict"):
        repository.put(
            tenant_id=TENANT,
            canonical_principal_id=PRINCIPAL,
            expected_version=0,
            policy=policy(status="disabled"),
            updated_by="human-admin-2",
        )
    assert repository.get(tenant_id=TENANT, canonical_principal_id=PRINCIPAL)["status"] == "active"


@pytest.mark.parametrize(
    "invalid",
    [
        policy(allowed_personas=[]),
        policy(task_scopes=["admin"]),
        policy(
            limits={
                "max_duration_minutes": 31,
                "max_turns": 8,
                "max_output_tokens_per_turn": 4096,
                "max_usd_per_task": Decimal("1"),
            }
        ),
        policy(
            limits={
                "max_duration_minutes": 30,
                "max_turns": 9,
                "max_output_tokens_per_turn": 4096,
                "max_usd_per_task": Decimal("1"),
            }
        ),
        policy(
            limits={
                "max_duration_minutes": 30,
                "max_turns": 8,
                "max_output_tokens_per_turn": 4096,
                "max_usd_per_task": Decimal("1.01"),
            }
        ),
    ],
)
def test_platform_ceilings_fail_closed(store, invalid):
    repository, _ = store
    with pytest.raises(TaskServicePolicyError, match="invalid_policy"):
        repository.put(
            tenant_id=TENANT,
            canonical_principal_id=PRINCIPAL,
            expected_version=0,
            policy=invalid,
            updated_by="human-admin-1",
        )


def test_corrupt_policy_binding_fails_closed(store):
    repository, client = store
    repository.put(
        tenant_id=TENANT,
        canonical_principal_id=PRINCIPAL,
        expected_version=0,
        policy=policy(),
        updated_by="human-admin-1",
    )
    client.update_item(
        TableName="authority",
        Key=repository.key(TENANT, PRINCIPAL),
        UpdateExpression="SET canonical_principal_id = :other",
        ExpressionAttributeValues={":other": {"S": "other-principal"}},
    )
    with pytest.raises(TaskServicePolicyError, match="corrupt_policy"):
        repository.get(tenant_id=TENANT, canonical_principal_id=PRINCIPAL)
