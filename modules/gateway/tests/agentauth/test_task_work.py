import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from src.agentauth.task_work import (
    DISPATCH_SORT_PREFIX,
    DUE_ATTRIBUTE,
    SHARD_ATTRIBUTE,
    TASK_LOCATOR_PREFIX,
    TASK_WORK_INDEX,
    TaskWorkError,
    TaskWorkStore,
    work_shard,
)

TASK = "tsk_3d5f8a10-2b4c-4e6f-9a81-7c3e5d9f1b20"
DISPATCH = "b5e9835b-fc24-4231-96f2-e8b8ca3681be"
INVOCATION = "5e7a9c31-4d6f-4813-ba25-9c1e3f5a7d40"
TENANT = "t-4821"
REQUEST_TABLE = "requests"
AUTHORITY_TABLE = "authority"
NOW = datetime(2026, 9, 24, 14, 42, 3, tzinfo=UTC)


def envelope():
    return {
        "kind": "adp.task",
        "schema_version": "1.0",
        "task_id": TASK,
        "invocation_id": INVOCATION,
        "message_id": INVOCATION,
        "persona": "agent-task-investigator",
        "dispatch_id": DISPATCH,
        "request_digest": "9" * 64,
        "input_ref": {"record_type": "TASK", "input_digest": "1" * 64},
        "assignment_ref": {
            "grant_pk": f"TENANT#{TENANT}",
            "grant_sk": f"TASK_RUN#{INVOCATION}#GEN#0000000001",
            "generation": 1,
        },
    }


def scoped(**values):
    return {
        **values,
        "task_id": {"S": TASK},
        "invocation_id": {"S": INVOCATION},
        "generation": {"N": "1"},
        "scope": {"M": {"tenant_id": {"S": TENANT}}},
    }


@pytest.fixture
def repository():
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(
            TableName=REQUEST_TABLE,
            KeySchema=[
                {"AttributeName": "event_id", "KeyType": "HASH"},
                {"AttributeName": "arrived_at", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "event_id", "AttributeType": "S"},
                {"AttributeName": "arrived_at", "AttributeType": "S"},
                {"AttributeName": SHARD_ATTRIBUTE, "AttributeType": "S"},
                {"AttributeName": DUE_ATTRIBUTE, "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[{
                "IndexName": TASK_WORK_INDEX,
                "KeySchema": [
                    {"AttributeName": SHARD_ATTRIBUTE, "KeyType": "HASH"},
                    {"AttributeName": DUE_ATTRIBUTE, "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
                "ProvisionedThroughput": {"ReadCapacityUnits": 5, "WriteCapacityUnits": 5},
            }],
            ProvisionedThroughput={"ReadCapacityUnits": 5, "WriteCapacityUnits": 5},
        )
        client.create_table(
            TableName=AUTHORITY_TABLE,
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}, {"AttributeName": "sk", "AttributeType": "S"}],
            ProvisionedThroughput={"ReadCapacityUnits": 5, "WriteCapacityUnits": 5},
        )
        clock = [NOW.timestamp()]
        store = TaskWorkStore(
            dynamodb_client=client,
            table_name=REQUEST_TABLE,
            authority_table_name=AUTHORITY_TABLE,
            clock=lambda: clock[0],
        )
        client.put_item(
            TableName=REQUEST_TABLE,
            Item=scoped(
                event_id={"S": f"TASK#{TASK}"}, arrived_at={"S": "META"},
                record_type={"S": "TASK"}, status={"S": "accepted"}, version={"S": "task-v1"},
            ),
        )
        grant_sk = f"TASK_RUN#{INVOCATION}#GEN#0000000001"
        client.put_item(
            TableName=AUTHORITY_TABLE,
            Item=scoped(
                pk={"S": f"TENANT#{TENANT}"}, sk={"S": grant_sk}, status={"S": "active"},
                canonical_principal_id={"S": "service-principal-1"},
                task_policy_sk={"S": "TASK_POLICY#service-principal-1"},
                task_policy_version={"N": "1"},
            ),
        )
        client.put_item(
            TableName=AUTHORITY_TABLE,
            Item=scoped(pk={"S": f"TENANT#{TENANT}"}, sk={"S": f"TASK#{TASK}"}, status={"S": "active"}),
        )
        client.put_item(
            TableName=AUTHORITY_TABLE,
            Item={
                "pk": {"S": f"TENANT#{TENANT}"}, "sk": {"S": "TASK_POLICY#service-principal-1"},
                "record_type": {"S": "TASK_SERVICE_POLICY"}, "status": {"S": "active"},
                "canonical_principal_id": {"S": "service-principal-1"}, "version": {"N": "1"},
                "allowed_personas": {"L": [{"S": "agent-task-investigator"}]},
                "task_scopes": {"L": [{"S": "submit"}, {"S": "read"}]},
                "scope": {"M": {"tenant_id": {"S": TENANT}}},
            },
        )
        store.put_work(
            task_id=TASK, kind="dispatch", tenant_id=TENANT,
            deadline_at=NOW + timedelta(minutes=30), envelope=envelope(),
        )
        yield store, client, clock


def test_versioned_storage_fixture_records_frozen_locator_rules():
    fixture = json.loads((Path(__file__).parent / "fixtures/task-work-storage-v1.json").read_text())
    assert fixture["fixture_version"] == "1.0"
    assert fixture["authority_locator"]["pk"] == f"TASK_WORK_ID#{DISPATCH}"
    assert fixture["request_work"]["publication_and_recovery_leases_are_distinct"] is True
    assert fixture["replacement_rules"] == {
        "reconcile_sort_key": "RECONCILE", "new_work_id_required": True,
        "old_locator_retargeted": False, "old_leases_retained": False,
    }


def test_acceptance_commits_full_envelope_and_protected_locator_atomically(repository):
    store, client, _ = repository
    bound = store.resolve(DISPATCH)
    assert bound.envelope == envelope()
    assert bound.work["arrived_at"] == {"S": f"{DISPATCH_SORT_PREFIX}{DISPATCH}"}
    locator = client.get_item(
        TableName=AUTHORITY_TABLE,
        Key={"pk": {"S": f"{TASK_LOCATOR_PREFIX}{DISPATCH}"}, "sk": {"S": "BINDING"}},
        ConsistentRead=True,
    )["Item"]
    assert locator["work_event_id"] == {"S": f"TASK_WORK#{TASK}"}
    assert locator["work_arrived_at"] == {"S": f"DISPATCH#{DISPATCH}"}
    assert locator["protected_digest"] == bound.work["envelope_digest"]


def test_aborted_acceptance_exposes_neither_work_nor_locator(repository):
    store, client, _ = repository
    other = envelope() | {"dispatch_id": "08bc87f5-2f05-41e4-9a92-0a774e41e618"}
    work_id, items = store.transaction_items(
        task_id=TASK, kind="dispatch", tenant_id=TENANT,
        deadline_at=NOW + timedelta(minutes=30), envelope=other,
    )
    items.append({"ConditionCheck": {
        "TableName": REQUEST_TABLE,
        "Key": {"event_id": {"S": "MISSING"}, "arrived_at": {"S": "META"}},
        "ConditionExpression": "attribute_exists(event_id)",
    }})
    with pytest.raises(ClientError):
        client.transact_write_items(TransactItems=items)
    assert client.get_item(
        TableName=AUTHORITY_TABLE,
        Key={"pk": {"S": f"TASK_WORK_ID#{work_id}"}, "sk": {"S": "BINDING"}},
    ).get("Item") is None
    assert client.get_item(
        TableName=REQUEST_TABLE,
        Key={"event_id": {"S": f"TASK_WORK#{TASK}"}, "arrived_at": {"S": f"DISPATCH#{work_id}"}},
    ).get("Item") is None


def test_duplicate_and_competing_publication_claims_have_one_owner(repository):
    store, _, _ = repository
    first = store.claim_publication(DISPATCH)
    with pytest.raises(TaskWorkError, match="leased"):
        store.claim_publication(DISPATCH)
    assert first.envelope["message_id"] == INVOCATION
    assert first.work["publication_lease_token"]["S"]


def test_publication_lease_expiry_reclaims_but_stale_token_cannot_settle(repository):
    store, _, clock = repository
    first = store.claim_publication(DISPATCH)
    clock[0] += 46
    second = store.claim_publication(DISPATCH)
    assert second.work["publication_lease_token"] != first.work["publication_lease_token"]
    with pytest.raises(TaskWorkError, match="stale_lease"):
        store.settle_publication(
            dispatch_id=DISPATCH,
            lease_token=first.work["publication_lease_token"]["S"],
            publication_outcome="confirmed",
            sqs_message_id="late-message",
        )


def test_unknown_send_retries_identical_envelope_and_dispatch_id(repository):
    store, _, _ = repository
    first = store.claim_publication(DISPATCH)
    store.settle_publication(
        dispatch_id=DISPATCH,
        lease_token=first.work["publication_lease_token"]["S"],
        publication_outcome="unknown",
        sqs_message_id=None,
    )
    retry = store.claim_publication(DISPATCH)
    assert retry.envelope == first.envelope == envelope()
    assert retry.work["work_id"] == {"S": DISPATCH}


def test_recovery_claim_is_a_distinct_45_second_lease(repository):
    store, _, clock = repository
    publication = store.claim_publication(DISPATCH)
    claimed, _ = store.claim_recovery(shard=work_shard(TASK), limit=100)
    assert len(claimed) == 1
    assert claimed[0].work["recovery_lease_token"]
    assert claimed[0].work["publication_lease_token"] == publication.work["publication_lease_token"]
    assert store.claim_recovery(shard=work_shard(TASK), limit=100)[0] == []
    old_token = claimed[0].work["recovery_lease_token"]["S"]
    clock[0] += 46
    reclaimed = store.claim_recovery(shard=work_shard(TASK), limit=100)[0][0]
    assert reclaimed.work["recovery_lease_token"]["S"] != old_token
    with pytest.raises(TaskWorkError, match="stale_lease"):
        store.settle_recovery(
            work_id=DISPATCH, lease_token=old_token, evidence_kind="publication",
            observed=False, observed_at=NOW,
        )


def test_recovery_boolean_cannot_fabricate_publication(repository):
    store, _, _ = repository
    claimed = store.claim_recovery(shard=work_shard(TASK), limit=100)[0][0]
    status, updated = store.settle_recovery(
        work_id=DISPATCH,
        lease_token=claimed.work["recovery_lease_token"]["S"],
        evidence_kind="publication", observed=True, observed_at=NOW,
    )
    assert status == "rejected"
    assert updated.work["publication_outcome"] == {"S": "pending"}
    assert store.task_status(TASK) == "accepted"


def test_confirmed_publication_then_recovery_settlement(repository):
    store, _, _ = repository
    recovery = store.claim_recovery(shard=work_shard(TASK), limit=100)[0][0]
    publication = store.claim_publication(DISPATCH)
    store.settle_publication(
        dispatch_id=DISPATCH,
        lease_token=publication.work["publication_lease_token"]["S"],
        publication_outcome="confirmed", sqs_message_id="sqs-1",
    )
    status, settled = store.settle_recovery(
        work_id=DISPATCH,
        lease_token=recovery.work["recovery_lease_token"]["S"],
        evidence_kind="publication", observed=True, observed_at=NOW,
    )
    assert status == "confirmed"
    assert SHARD_ATTRIBUTE not in settled.work
    assert store.task_status(TASK) == "queued"


def test_missing_or_mismatched_locator_fails_closed(repository):
    store, client, _ = repository
    key = {"pk": {"S": f"TASK_WORK_ID#{DISPATCH}"}, "sk": {"S": "BINDING"}}
    original = client.get_item(TableName=AUTHORITY_TABLE, Key=key)["Item"]
    client.delete_item(TableName=AUTHORITY_TABLE, Key=key)
    with pytest.raises(TaskWorkError, match="not_found"):
        store.claim_publication(DISPATCH)
    original["task_id"] = {"S": "tsk_aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"}
    client.put_item(TableName=AUTHORITY_TABLE, Item=original)
    with pytest.raises(TaskWorkError, match="binding_mismatch"):
        store.claim_publication(DISPATCH)


def test_stale_generation_is_rejected(repository):
    store, client, _ = repository
    client.update_item(
        TableName=REQUEST_TABLE,
        Key={"event_id": {"S": f"TASK#{TASK}"}, "arrived_at": {"S": "META"}},
        UpdateExpression="SET generation = :generation",
        ExpressionAttributeValues={":generation": {"N": "2"}},
    )
    with pytest.raises(TaskWorkError, match="binding_mismatch"):
        store.claim_publication(DISPATCH)


def test_recovery_work_revalidates_generation_and_grant(repository):
    store, client, _ = repository
    work_id = store.put_work(
        task_id=TASK, kind="execution", tenant_id=TENANT,
        deadline_at=NOW + timedelta(minutes=30), invocation_id=INVOCATION, generation=1,
    )
    client.update_item(
        TableName=REQUEST_TABLE,
        Key={"event_id": {"S": f"TASK#{TASK}"}, "arrived_at": {"S": "META"}},
        UpdateExpression="SET generation = :generation",
        ExpressionAttributeValues={":generation": {"N": "2"}},
    )
    with pytest.raises(TaskWorkError, match="binding_mismatch"):
        store.resolve(work_id)


def test_deadline_exhaustion_fails_a_queued_task(repository):
    store, client, clock = repository
    client.update_item(
        TableName=REQUEST_TABLE,
        Key={"event_id": {"S": f"TASK#{TASK}"}, "arrived_at": {"S": "META"}},
        UpdateExpression="SET #status = :queued",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={":queued": {"S": "queued"}},
    )
    clock[0] = (NOW + timedelta(minutes=31)).timestamp()
    with pytest.raises(TaskWorkError, match="exhausted"):
        store.claim_publication(DISPATCH)
    assert store.task_status(TASK) == "failed"


def test_terminal_task_cannot_be_published(repository):
    store, client, _ = repository
    client.update_item(
        TableName=REQUEST_TABLE,
        Key={"event_id": {"S": f"TASK#{TASK}"}, "arrived_at": {"S": "META"}},
        UpdateExpression="SET #status = :cancelled",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={":cancelled": {"S": "cancelled"}},
    )
    with pytest.raises(TaskWorkError, match="task_not_publishable"):
        store.claim_publication(DISPATCH)


def test_late_settlement_records_evidence_without_regressing_running(repository):
    store, client, _ = repository
    claim = store.claim_publication(DISPATCH)
    client.update_item(
        TableName=REQUEST_TABLE,
        Key={"event_id": {"S": f"TASK#{TASK}"}, "arrived_at": {"S": "META"}},
        UpdateExpression="SET #status = :running, #version = :version",
        ExpressionAttributeNames={"#status": "status", "#version": "version"},
        ExpressionAttributeValues={":running": {"S": "running"}, ":version": {"S": "task-v2"}},
    )
    settled = store.settle_publication(
        dispatch_id=DISPATCH,
        lease_token=claim.work["publication_lease_token"]["S"],
        publication_outcome="confirmed", sqs_message_id="sqs-late",
    )
    assert settled.work["sqs_message_id"] == {"S": "sqs-late"}
    assert store.task_status(TASK) == "running"


def test_reconcile_replacement_gets_new_identity_and_invalidates_old_locator(repository):
    store, client, _ = repository
    old_id = store.put_work(
        task_id=TASK, kind="execution", tenant_id=TENANT,
        deadline_at=NOW + timedelta(minutes=30), invocation_id=INVOCATION, generation=1,
    )
    old_claim = store.claim_recovery(shard=work_shard(TASK), limit=100)[0]
    old_recovery = next(item for item in old_claim if item.work_id == old_id)
    new_id, transaction = store.replacement_transaction_items(
        previous_work_id=old_id, task_id=TASK, kind="execution", tenant_id=TENANT,
        deadline_at=NOW + timedelta(minutes=30), invocation_id=INVOCATION, generation=1,
    )
    client.transact_write_items(TransactItems=transaction)

    with pytest.raises(TaskWorkError, match="binding_mismatch"):
        store.resolve(old_id)
    replacement = store.resolve(new_id)
    assert replacement.work_id != old_recovery.work_id
    assert "recovery_lease_token" not in replacement.work


def test_revoked_or_mismatched_task_policy_blocks_publication(repository):
    store, client, _ = repository
    key = {"pk": {"S": f"TENANT#{TENANT}"}, "sk": {"S": "TASK_POLICY#service-principal-1"}}
    client.update_item(
        TableName=AUTHORITY_TABLE, Key=key,
        UpdateExpression="SET #status = :disabled",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={":disabled": {"S": "disabled"}},
    )
    with pytest.raises(TaskWorkError, match="authority_refused"):
        store.claim_publication(DISPATCH)
    client.update_item(
        TableName=AUTHORITY_TABLE, Key=key,
        UpdateExpression="SET #status = :active, allowed_personas = :personas",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={
            ":active": {"S": "active"},
            ":personas": {"L": [{"S": "different-persona"}]},
        },
    )
    with pytest.raises(TaskWorkError, match="authority_refused"):
        store.claim_publication(DISPATCH)


def test_policy_without_submit_scope_blocks_publication(repository):
    store, client, _ = repository
    key = {"pk": {"S": f"TENANT#{TENANT}"}, "sk": {"S": "TASK_POLICY#service-principal-1"}}
    client.update_item(
        TableName=AUTHORITY_TABLE, Key=key,
        UpdateExpression="SET task_scopes = :scopes",
        ExpressionAttributeValues={":scopes": {"L": [{"S": "read"}]}},
    )
    with pytest.raises(TaskWorkError, match="authority_refused"):
        store.claim_publication(DISPATCH)
