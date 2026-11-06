"""SCN-06: owner-scoped dry-run and replay-safe migration journal."""

from types import SimpleNamespace
from unittest.mock import Mock

import boto3
import pytest
from moto import mock_aws

from src.agentauth import chat_data_routes
from src.agentauth.agentcore_migration import LegacyMemoryMigration
from src.agentauth.agentcore_recall import AgentCoreRecall
from src.agentauth.chat_capability import ChatAuthorizationRefusedError
from src.orchestration.chat_data_migration import _owner_fields
from tests.agentauth.test_agentcore_management_route import mounted as mounted_fixture

mounted = mounted_fixture


async def test_mounted_dry_run_is_read_only_and_owner_scoped(mounted):
    client, app, user, provider, _, context = mounted
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        memory = table(resource, "memory")
        owner = SimpleNamespace(tenant_id=user.org_id, team_id=user.team_id, user_id=user.user_id, session_id="session-a")
        seed(context, memory, owner, 1)
        app.dependency_overrides[chat_data_routes.memory_table] = lambda: memory
        response = await client.get("/chat/sessions/session-a/agentcore-memory/migration?limit=1")
        assert response.status_code == 200, response.text
        assert response.json() == {"counts": {"eligible": 1, "quarantined": 0, "expired": 0}, "next_cursor": None, "status": "dry_run"}
        assert response.headers["cache-control"] == "no-store"
        provider.client.create_event.assert_not_called()
        assert len(memory.scan()["Items"]) == 2


def table(resource, name):
    return resource.create_table(
        TableName=name,
        BillingMode="PAY_PER_REQUEST",
        KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"}, {"AttributeName": "SK", "AttributeType": "S"}],
    )


def seed(context, memory, owner, reference, *, source=True, pointer_owner=None, ttl=3000000000):
    fields = _owner_fields((owner.tenant_id, owner.team_id, owner.user_id))
    if source:
        context.put_item(Item={"PK": f"session#{owner.session_id}", "SK": "header", **fields, "memoryPrivacy": "shared", "memoryConsent": True})
    memory_id = f"mem_{reference:032x}"
    created = "2026-01-01T00:00:00+00:00"
    row = {
        "PK": f"memory#{memory_id}",
        "SK": "record",
        "id": memory_id,
        **fields,
        "scope": {"tenant": owner.tenant_id, "user": owner.user_id},
        "source": {"sessionId": owner.session_id, "runId": "run-a"},
        "content": "Remember a reviewed preference",
        "kind": "preference",
        "tags": [],
        "createdAt": created,
        "updatedAt": created,
        "version": 2,
        "ttl": ttl,
    }
    memory.put_item(Item=row)
    index_owner = pointer_owner or owner
    memory.put_item(
        Item={
            "PK": f"memory-owner#{index_owner.tenant_id}#{index_owner.user_id}",
            "SK": f"mem#{created}#{memory_id}",
            "id": memory_id,
            **fields,
        }
    )
    return memory_id


def test_scoped_inventory_quarantines_conflicts_and_expired_without_disclosing_contents():
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        context, memory = table(resource, "context"), table(resource, "memory")
        owner = SimpleNamespace(tenant_id="tenant-a", team_id="team-a", user_id="owner-a", session_id="session-a")
        foreign = SimpleNamespace(tenant_id="tenant-a", team_id="team-a", user_id="owner-b", session_id="session-b")
        valid = seed(context, memory, owner, 1)
        seed(context, memory, owner, 2, ttl=1)
        seed(context, memory, foreign, 3, pointer_owner=owner)
        seed(context, memory, foreign, 4)
        provider = AgentCoreRecall(Mock(), memory_id="configured", strategy_id="semantic", namespace_key=b"k" * 32)
        migration = LegacyMemoryMigration(memory, context, provider, Mock())
        report = migration.inventory(owner, now=100)
        assert report["counts"] == {"eligible": 1, "quarantined": 1, "expired": 1}
        assert [row["id"] for row in report["candidates"]] == [valid]
        assert "Remember" not in str(report["counts"])
        assert migration.inventory(foreign, now=100)["counts"]["eligible"] == 1
        provider.client.create_event.assert_not_called()


def test_dry_run_pages_count_only_the_requested_owner():
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        context, memory = table(resource, "context"), table(resource, "memory")
        owner = SimpleNamespace(tenant_id="tenant-a", team_id="team-a", user_id="owner-a", session_id="session-a")
        seed(context, memory, owner, 1)
        seed(context, memory, owner, 2)
        provider = AgentCoreRecall(Mock(), memory_id="configured", strategy_id="semantic", namespace_key=b"k" * 32)
        migration = LegacyMemoryMigration(memory, context, provider, Mock())
        first = migration.inventory(owner, now=100, limit=1)
        second = migration.inventory(owner, now=100, limit=1, cursor=first["next_cursor"])
        assert first["counts"]["eligible"] == second["counts"]["eligible"] == 1
        assert first["next_cursor"] and second["next_cursor"] is None
        with pytest.raises(ChatAuthorizationRefusedError):
            migration.inventory(owner, now=100, cursor="foreign")


def test_migration_journal_retries_once_and_rollback_fences_late_service_results():
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        context, memory = table(resource, "context"), table(resource, "memory")
        owner = SimpleNamespace(tenant_id="tenant-a", team_id="team-a", user_id="owner-a", session_id="session-a")
        seed(context, memory, owner, 1)
        provider = AgentCoreRecall(Mock(), memory_id="configured", strategy_id="semantic", namespace_key=b"k" * 32)
        namespace = provider.owner_namespace(owner)
        provider.client.create_event.return_value = {
            "event": {"eventId": "service-event", "memoryId": "configured", "actorId": namespace.rsplit("/", 1)[-1]}
        }
        migration = LegacyMemoryMigration(memory, context, provider, Mock())
        initial = migration.migrate(owner, now=100)
        assert initial["counts"]["eligible"] == 1
        assert initial["receipts"][0]["status"] == "pending"
        assert migration.migrate(owner, now=100) == initial
        provider.client.create_event.assert_called_once()
        assert provider.client.create_event.call_args.kwargs["metadata"]["adp_legacy_source"]["stringValue"].endswith(":2")
        receipt = initial["receipts"][0]["receipt_id"]
        journal = context.get_item(Key={"PK": f"memory-migration#{receipt}", "SK": "journal"})["Item"]
        assert journal["serviceEventId"] == "service-event"
        assert migration.rollback(owner, receipt_id=receipt)["status"] == "rollback_pending_service_cleanup"
        provider.client.retrieve_memory_records.return_value = {
            "memoryRecordSummaries": [
                {
                    "memoryRecordId": "late-record",
                    "namespaces": [namespace],
                    "memoryStrategyId": "semantic",
                    "metadata": {"adp_epoch": {"stringValue": "0"}},
                    "content": {"text": "Remember a reviewed preference"},
                }
            ]
        }
        capabilities = Mock()
        capabilities.verify_run.return_value = owner
        recalled = provider.retrieve(
            capabilities, "token", authority=SimpleNamespace(context_table=context), run_id="run-a", query="preference", now=100
        )
        assert recalled["record_ids"] == []
        assert recalled["status"] == "partial"
        assert migration.inventory(owner, now=100)["counts"] == {"eligible": 0, "quarantined": 1, "expired": 0}
        assert migration.migrate(owner, now=100)["receipts"] == []
        provider.client.create_event.assert_called_once()
        assert migration.rollback(owner, receipt_id=receipt)["status"] == "rollback_pending_service_cleanup"
        foreign = SimpleNamespace(tenant_id="tenant-a", team_id="team-a", user_id="owner-b", session_id="session-b")
        seed(context, memory, foreign, 2)
        with pytest.raises(ChatAuthorizationRefusedError):
            migration.rollback(foreign, receipt_id=receipt)


def test_migration_never_accepts_foreign_or_unshared_sources():
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        context, memory = table(resource, "context"), table(resource, "memory")
        owner = SimpleNamespace(tenant_id="tenant-a", team_id="team-a", user_id="owner-a", session_id="session-a")
        foreign = SimpleNamespace(tenant_id="tenant-a", team_id="team-a", user_id="owner-b", session_id="session-b")
        seed(context, memory, owner, 1, source=False)
        seed(context, memory, foreign, 2, pointer_owner=owner)
        provider = AgentCoreRecall(Mock(), memory_id="configured", strategy_id="semantic", namespace_key=b"k" * 32)
        capabilities = Mock()
        migration = LegacyMemoryMigration(memory, context, provider, capabilities)
        with pytest.raises(ChatAuthorizationRefusedError):
            migration.inventory(owner, now=100)
        seed(context, memory, owner, 1)
        context.update_item(
            Key={"PK": "session#session-a", "SK": "header"},
            UpdateExpression="SET memoryPrivacy = :privacy",
            ExpressionAttributeValues={":privacy": "private"},
        )
        with pytest.raises(ChatAuthorizationRefusedError):
            migration.inventory(owner, now=100)
        context.update_item(
            Key={"PK": "session#session-a", "SK": "header"},
            UpdateExpression="SET memoryPrivacy = :privacy",
            ExpressionAttributeValues={":privacy": "shared"},
        )
        capabilities._member.side_effect = ChatAuthorizationRefusedError("membership revoked")
        with pytest.raises(ChatAuthorizationRefusedError):
            migration.migrate(owner, now=100)
        provider.client.create_event.assert_not_called()
