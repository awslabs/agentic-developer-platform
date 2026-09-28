"""Replay UUID continuations using the conditional write's existing record."""

from copy import deepcopy
from unittest.mock import patch

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from src.orchestration.run_store import EngineRunStore


@pytest.fixture
def replay():
    envelope = {
        "message_id": "97e2207b-1a7c-5a83-ba6c-81f307f32fdd",
        "arrived_at": "2026-09-21T07:16:36Z",
        "tenant_id": "aws-e",
        "actor": {"user_id": "human"},
        "persona": "reviewer",
        "source_ref": {"repo": "aws-e/adp", "issue": 5619, "installation_id": 42},
        "orchestration": {"graph_address": "adp/security/review", "node_id": "node", "attempt": 1},
        "correlation": {"correlation_id": "orch:developer", "root_human_id": "human"},
    }
    with mock_aws():
        table = boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName="events",
            KeySchema=[{"AttributeName": "event_id", "KeyType": "HASH"}, {"AttributeName": "arrived_at", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": name, "AttributeType": "S"} for name in ("event_id", "arrived_at")],
            BillingMode="PAY_PER_REQUEST",
        )
        yield EngineRunStore(table), envelope


def test_uuid_replay_preserves_terminal_record_without_get_permission(replay):
    store, envelope = replay
    store.register(envelope)
    key = {"event_id": envelope["message_id"], "arrived_at": envelope["arrived_at"]}
    existing = store.table.get_item(Key=key)["Item"]
    existing.update(status="complete", transcript_key="review/transcript.md", summary="Review finished")
    store.table.put_item(Item=existing)
    denied = ClientError({"Error": {"Code": "AccessDeniedException"}}, "GetItem")
    with patch.object(store.table, "get_item", side_effect=denied) as get_item:
        store.register(envelope)
        get_item.assert_not_called()
    assert store.table.get_item(Key=key)["Item"] == existing


@pytest.mark.parametrize("field", ["tenant_id", "user_id", "root_human_id", "engine_node_id", "engine_attempt", "repo", "issue_number"])
def test_conflicting_conditional_record_cannot_be_rebound(replay, field):
    store, envelope = replay
    existing = store.build_item(envelope)
    existing[field] = 99 if isinstance(existing[field], int) else "other"
    store.table.put_item(Item=existing)
    with patch.object(store.table, "get_item", side_effect=AssertionError("No separate read")):
        with pytest.raises(RuntimeError, match="identity conflicts"):
            store.register(envelope)
    assert store.table.scan()["Items"] == [existing]


@pytest.mark.parametrize("code", ["ConditionalCheckFailedException", "AccessDeniedException"])
def test_missing_conditional_record_or_denied_write_never_authorizes_replay(replay, code):
    store, envelope = replay
    error = ClientError({"Error": {"Code": code}}, "PutItem")
    before = deepcopy(envelope)
    with patch.object(store.table, "put_item", side_effect=error), patch.object(store.table, "get_item") as get_item:
        with pytest.raises(RuntimeError if code == "ConditionalCheckFailedException" else ClientError):
            store.register(envelope)
        get_item.assert_not_called()
    assert envelope == before
