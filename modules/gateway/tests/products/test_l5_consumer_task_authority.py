"""Later-result use resolves real Task ownership and standing policy on each read."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import boto3
import pytest
from boto3.dynamodb.types import TypeSerializer
from moto import mock_aws

from src.agentauth.task_service_policy import TaskServicePolicyStore
from src.l5_consumer_authority import task_authorized
from src.shared.models.organization import Organization, User
from src.shared.schemas.auth import TokenContext
from src.tasks import routes
from src.tasks.dynamo_read_store import DynamoTaskReadStore
from src.tasks.records import META_SORT_KEY, task_partition
from src.tasks.store import TaskStore


@pytest.mark.parametrize("revocation", ["owner", "tenant", "product", "project", "policy", "persona", "missing", "earlier", "causal"])
async def test_real_task_store_and_policy_deny_foreign_or_revoked_later_use(db_session, monkeypatch, revocation):
    now = datetime.now(UTC)
    user_id, tenant_id = str(uuid4()), str(uuid4())
    actor = TokenContext(
        user_id=user_id,
        org_id=tenant_id,
        team_id="team",
        department_id="department",
        account_type="human",
        auth_source="jwt",
        expires_at=now + timedelta(hours=1),
    )
    db_session.add(Organization(id=tenant_id, name="Consumer task fixture"))
    db_session.add(User(id=user_id, org_id=tenant_id, team_id="team", user_kind="human", email="consumer-task@example.test"))
    await db_session.commit()
    run = SimpleNamespace(
        run_id=str(uuid4()),
        trigger_id="manual",
        tenant_id=tenant_id,
        product_id="product-1",
        project_id="project-1",
        input={},
        admission={"admitted_at": (now - timedelta(hours=1)).isoformat()},
    )
    task_id = "tsk_" + str(uuid4())
    principal = "human:" + user_id
    monkeypatch.setenv("ADP_TASK_API_HUMAN_ENABLED", "true")
    monkeypatch.setenv("AGENT_AUTHORITY_TABLE", "consumer-task-authority")
    serializer = TypeSerializer()
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        for table, partition, sort in (("consumer-tasks", "event_id", "arrived_at"), ("consumer-task-authority", "pk", "sk")):
            client.create_table(
                TableName=table,
                BillingMode="PAY_PER_REQUEST",
                AttributeDefinitions=[{"AttributeName": name, "AttributeType": "S"} for name in (partition, sort)],
                KeySchema=[{"AttributeName": partition, "KeyType": "HASH"}, {"AttributeName": sort, "KeyType": "RANGE"}],
            )
        policy = {
            "status": "active",
            "allowed_personas": ["agent-task-investigator"],
            "task_scopes": ["read"],
            "model_policy_version": "1",
            "limits": {"max_duration_minutes": 5, "max_turns": 2, "max_output_tokens_per_turn": 100, "max_usd_per_task": 1},
        }
        policies = TaskServicePolicyStore(table_name="consumer-task-authority", client=client)
        policies.put(tenant_id=tenant_id, canonical_principal_id=principal, expected_version=0, policy=policy, updated_by=user_id)
        row = {
            "event_id": task_partition(task_id),
            "arrived_at": META_SORT_KEY,
            "task_id": task_id,
            "invocation_id": str(uuid4()),
            "scope": {"tenant": tenant_id, "canonical_principal": principal},
            "persona": "agent-task-investigator",
            "state": "completed",
            "version": 1,
            "generation": 1,
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
            "deadline_at": (now + timedelta(minutes=5)).isoformat(),
            "product_id": run.product_id,
            "project_id": run.project_id,
        }

        def write():
            client.put_item(TableName="consumer-tasks", Item={key: serializer.serialize(value) for key, value in row.items()})

        write()
        store = DynamoTaskReadStore(
            TaskStore(table_name="consumer-tasks", authority_table_name="consumer-task-authority", dynamodb_client=client),
            s3_client=boto3.client("s3", region_name="us-east-1"),
            artifact_bucket="consumer-task-artifacts",
        )
        monkeypatch.setattr(routes, "get_store", lambda: store)
        assert await task_authorized(db_session, actor, run, task_id)
        assert not await task_authorized(db_session, actor, run, "arbitrary-task-string")
        if revocation == "policy":
            policies.put(
                tenant_id=tenant_id, canonical_principal_id=principal, expected_version=1, policy={**policy, "status": "disabled"}, updated_by=user_id
            )
        elif revocation == "missing":
            client.delete_item(TableName="consumer-tasks", Key={"event_id": {"S": row["event_id"]}, "arrived_at": {"S": META_SORT_KEY}})
        else:
            if revocation == "owner":
                row["scope"]["canonical_principal"] = "human:" + str(uuid4())
            elif revocation == "tenant":
                row["scope"]["tenant"] = "foreign"
            elif revocation in {"product", "project"}:
                row[revocation + "_id"] = "foreign"
            elif revocation == "persona":
                row["persona"] = "ungranted-persona"
            elif revocation == "earlier":
                row["created_at"] = (now - timedelta(days=1)).isoformat()
            else:
                row["invocation_id"] = run.run_id
            write()
        assert not await task_authorized(db_session, actor, run, task_id)
