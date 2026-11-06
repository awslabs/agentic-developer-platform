"""Ordinary tool admission stays one-shot across concurrent façade retries."""

from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from uuid import uuid4

import boto3
import pytest
from moto import mock_aws

from src.internal.tools_admissions import ToolCallAdmission
from src.internal.tools_authorize import ToolsAuthorizationBody


@pytest.fixture
def client():
    with mock_aws():
        dynamodb = boto3.client("dynamodb", region_name="us-east-1")
        dynamodb.create_table(
            TableName="tool-authority",
            BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}, {"AttributeName": "sk", "AttributeType": "S"}],
        )
        yield dynamodb


def admission(client, request_id, *, user_id="owner", team_id="team", run_id="run-one", tool="websearch___search", arguments=None):
    call = ToolsAuthorizationBody(
        schema_version="1.0",
        tool=tool,
        request_id=request_id,
        arguments=arguments if arguments is not None else {"query": "synthetic-private-value"},
    )
    return ToolCallAdmission(
        table="tool-authority",
        client=client,
        tenant_id="tenant",
        user_id=user_id,
        team_id=team_id,
        run_id=run_id,
        workload_id="workload",
        body=call,
    )


def test_concurrent_same_id_has_exactly_one_receipt_without_private_arguments(client):
    request_id = uuid4()
    lock = Lock()

    class AtomicEmulator:
        def __getattr__(self, name):
            return getattr(client, name)

        def put_item(self, **kwargs):
            with lock:
                return client.put_item(**kwargs)

    attempts = [admission(AtomicEmulator(), request_id) for _ in range(12)]
    with ThreadPoolExecutor(max_workers=12) as executor:
        results = list(executor.map(lambda attempt: attempt.claim(), attempts))
    assert results.count(True) == 1
    winner = attempts[results.index(True)]
    winner.finish()
    assert not admission(client, request_id).claim()
    row = client.get_item(TableName="tool-authority", Key=winner.key, ConsistentRead=True)["Item"]
    assert row["receipt_id"]["S"] == winner.receipt_id and row["status"]["S"] == "admitted"
    assert "synthetic-private-value" not in str(row)


@pytest.mark.parametrize(
    "change",
    [
        {"arguments": {"query": "changed"}},
        {"run_id": "run-two"},
        {"team_id": "foreign-team"},
        {"tool": "browser___start"},
    ],
)
def test_same_id_changed_binding_conflicts_without_replacing_original_claim(client, change):
    request_id = uuid4()
    original = admission(client, request_id)
    assert original.claim()
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        admission(client, request_id, **change).claim()
    assert exc.value.status_code == 409
    assert client.get_item(TableName="tool-authority", Key=original.key)["Item"]["receipt_id"]["S"] == original.receipt_id


def test_separate_owner_key_and_known_rate_denial_retry(client):
    request_id = uuid4()
    first = admission(client, request_id)
    assert first.claim()
    assert admission(client, request_id, user_id="foreign").claim()
    first.cancel_before_reservation()
    replacement = admission(client, request_id)
    assert replacement.claim() and replacement.receipt_id != first.receipt_id
