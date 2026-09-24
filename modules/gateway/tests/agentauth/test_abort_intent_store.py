"""Durable abort intent in the protected authority store — Issue #3963 (S4).

These tests run against **real DynamoDB semantics** (moto), not a stubbed request
recorder, and that choice is the point of the file. The properties being asserted
are properties of *concurrent conditional writes*:

- the first accepted abort is the durable one, and a second one cannot rewrite it;
- a retry of the same acceptance returns the originally recorded moment rather
  than silently moving it forward;
- an acceptance for a superseded attempt is refused instead of marking a newer
  attempt it never authorized;
- the marker never disturbs ``status``, ``current_attempt`` or the credential
  epoch — because those are what keep the aborting run's own channel alive long
  enough to deliver and finalize the abort.

A ``Stubber``-based test (the pattern in ``test_store.py``) can assert that a
``ConditionExpression`` string was sent, but it cannot demonstrate that the
condition actually *refuses* the second writer — it replays whatever response the
test author scripted. For a "cannot happen twice" claim that is the difference
between checking the lock is mentioned and checking the lock holds.
"""

from __future__ import annotations

from datetime import UTC, datetime

import boto3
import pytest
from moto import mock_aws

from src.agentauth.store import (
    AbortIntentConflictError,
    AgentAuthorityStore,
    AuthorityStoreError,
)

TABLE = "adp-test-agent-authority"
TENANT = "org-tenant-001"
INVOCATION = "inv-developer-7"
COMMAND = "cmd-abort-0001"
DIGEST = "a" * 64
NOW = datetime(2026, 9, 24, 12, 0, 0, tzinfo=UTC)
LATER = datetime(2026, 9, 24, 12, 5, 0, tzinfo=UTC)


@pytest.fixture
def client():
    with mock_aws():
        client = boto3.client(
            "dynamodb",
            region_name="us-east-1",
            aws_access_key_id="testing",
            aws_secret_access_key="testing",
        )
        client.create_table(
            TableName=TABLE,
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield client


@pytest.fixture
def store(client):
    return AgentAuthorityStore(table_name=TABLE, dynamodb_client=client)


def put_execution(client, *, attempt: int = 1, status: str = "active", **extra) -> None:
    item = {
        "pk": {"S": f"TENANT#{TENANT}"},
        "sk": {"S": f"EXEC#{INVOCATION}"},
        "invocation_id": {"S": INVOCATION},
        "tenant_id": {"S": TENANT},
        "current_attempt": {"N": str(attempt)},
        "status": {"S": status},
        "current_credential_epoch": {"N": "2"},
        "min_acceptable_credential_epoch": {"N": "2"},
        "workload_binding": {"S": "pod-uid-1"},
    }
    item.update(extra)
    client.put_item(TableName=TABLE, Item=item)


def read_execution(client) -> dict:
    return client.get_item(
        TableName=TABLE,
        Key={"pk": {"S": f"TENANT#{TENANT}"}, "sk": {"S": f"EXEC#{INVOCATION}"}},
        ConsistentRead=True,
    )["Item"]


class TestRecordAbortIntent:
    def test_records_the_marker_for_the_running_attempt(self, store, client):
        put_execution(client)
        marker = store.record_abort_intent(
            invocation_id=INVOCATION, tenant_id=TENANT, attempt=1,
            command_id=COMMAND, body_digest=DIGEST, now=NOW,
        )
        assert marker == {
            "command_id": COMMAND,
            "body_digest": DIGEST,
            "requested_at": "2026-09-24T12:00:00Z",
        }
        item = read_execution(client)
        assert item["abort_command_id"] == {"S": COMMAND}
        assert item["abort_body_digest"] == {"S": DIGEST}
        assert item["abort_requested_attempt"] == {"N": "1"}

    def test_leaves_the_run_active_so_the_abort_can_still_be_delivered(self, store, client):
        """The constraint that rules out implementing this as a status transition.

        Only an ACTIVE execution authorizes a run credential. If recording intent
        cancelled the execution, the accepted abort could never reach the still
        running task: the worker would lose the channel it needs to apply the
        cancellation and to write the terminal outcome, and the operator would hold
        an "accepted" command with no effect and no report.
        """
        put_execution(client)
        before = read_execution(client)
        store.record_abort_intent(
            invocation_id=INVOCATION, tenant_id=TENANT, attempt=1,
            command_id=COMMAND, body_digest=DIGEST, now=NOW,
        )
        after = read_execution(client)
        for preserved in (
            "status",
            "current_attempt",
            "current_credential_epoch",
            "min_acceptable_credential_epoch",
            "workload_binding",
        ):
            assert after[preserved] == before[preserved], preserved
        assert after["status"] == {"S": "active"}

    def test_repeating_the_same_acceptance_keeps_the_first_recorded_moment(self, store, client):
        """Idempotence that preserves the truth, not just the absence of an error.

        A finalizer may retry. Returning a fresh timestamp would make the record
        say the operator aborted later than they did, and would make two retries
        of one command look like two aborts.
        """
        put_execution(client)
        first = store.record_abort_intent(
            invocation_id=INVOCATION, tenant_id=TENANT, attempt=1,
            command_id=COMMAND, body_digest=DIGEST, now=NOW,
        )
        second = store.record_abort_intent(
            invocation_id=INVOCATION, tenant_id=TENANT, attempt=1,
            command_id=COMMAND, body_digest=DIGEST, now=LATER,
        )
        assert second == first
        assert read_execution(client)["abort_requested_at"] == {"S": "2026-09-24T12:00:00Z"}

    def test_a_second_different_command_cannot_rewrite_the_first_abort(self, store, client):
        """The first accepted abort is the one that stopped the run."""
        put_execution(client)
        store.record_abort_intent(
            invocation_id=INVOCATION, tenant_id=TENANT, attempt=1,
            command_id=COMMAND, body_digest=DIGEST, now=NOW,
        )
        with pytest.raises(AbortIntentConflictError):
            store.record_abort_intent(
                invocation_id=INVOCATION, tenant_id=TENANT, attempt=1,
                command_id="cmd-abort-0002", body_digest="b" * 64, now=LATER,
            )
        item = read_execution(client)
        assert item["abort_command_id"] == {"S": COMMAND}
        assert item["abort_body_digest"] == {"S": DIGEST}

    def test_same_command_with_a_different_body_is_refused(self, store, client):
        """A digest is the binding to the operator's words; it cannot be swapped."""
        put_execution(client)
        store.record_abort_intent(
            invocation_id=INVOCATION, tenant_id=TENANT, attempt=1,
            command_id=COMMAND, body_digest=DIGEST, now=NOW,
        )
        with pytest.raises(AbortIntentConflictError):
            store.record_abort_intent(
                invocation_id=INVOCATION, tenant_id=TENANT, attempt=1,
                command_id=COMMAND, body_digest="c" * 64, now=LATER,
            )

    def test_acceptance_for_a_superseded_attempt_is_refused(self, store, client):
        """A stale acceptance must not mark an attempt it never authorized."""
        put_execution(client, attempt=3)
        with pytest.raises(AbortIntentConflictError):
            store.record_abort_intent(
                invocation_id=INVOCATION, tenant_id=TENANT, attempt=1,
                command_id=COMMAND, body_digest=DIGEST, now=NOW,
            )
        assert "abort_command_id" not in read_execution(client)

    def test_missing_execution_is_refused(self, store):
        with pytest.raises(AbortIntentConflictError):
            store.record_abort_intent(
                invocation_id=INVOCATION, tenant_id=TENANT, attempt=1,
                command_id=COMMAND, body_digest=DIGEST, now=NOW,
            )

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"command_id": ""},
            {"body_digest": ""},
            {"invocation_id": ""},
            {"tenant_id": ""},
            {"attempt": 0},
            {"attempt": True},
            {"attempt": 1.0},
            {"attempt": "1"},
        ],
    )
    def test_incomplete_acceptance_is_refused_before_any_write(self, store, client, kwargs):
        """`True` is an `int` in Python and `1.0 == 1`; neither is an attempt.

        Refused before the write so a malformed acceptance cannot leave a partial
        marker that admission would later read as a real abort.
        """
        put_execution(client)
        call = {
            "invocation_id": INVOCATION, "tenant_id": TENANT, "attempt": 1,
            "command_id": COMMAND, "body_digest": DIGEST, "now": NOW,
        }
        call.update(kwargs)
        with pytest.raises(AuthorityStoreError):
            store.record_abort_intent(**call)
        assert "abort_command_id" not in read_execution(client)


class TestAbortIntent:
    def test_absent_for_a_run_with_no_abort(self, store, client):
        put_execution(client)
        assert store.abort_intent(invocation_id=INVOCATION, tenant_id=TENANT) is None

    def test_absent_for_a_missing_run(self, store):
        assert store.abort_intent(invocation_id=INVOCATION, tenant_id=TENANT) is None

    def test_reads_back_the_recorded_marker(self, store, client):
        put_execution(client)
        store.record_abort_intent(
            invocation_id=INVOCATION, tenant_id=TENANT, attempt=1,
            command_id=COMMAND, body_digest=DIGEST, now=NOW,
        )
        assert store.abort_intent(invocation_id=INVOCATION, tenant_id=TENANT) == {
            "command_id": COMMAND,
            "body_digest": DIGEST,
            "requested_at": "2026-09-24T12:00:00Z",
            "attempt": "1",
        }

    def test_a_half_written_marker_is_not_an_abort(self, store, client):
        """Fail closed toward "no abort" on an incoherent row.

        Admission refusing on a partial marker would be worse than it sounds: a
        stray `abort_command_id` with no timestamp would permanently block a run
        that nobody aborted.
        """
        put_execution(client, abort_command_id={"S": COMMAND})
        assert store.abort_intent(invocation_id=INVOCATION, tenant_id=TENANT) is None

    def test_intent_survives_a_terminal_status_write(self, store, client):
        """The marker is the fact admission trusts, so it must outlive the run.

        This is the crash window the story cares about: the terminal status write
        and the queue acknowledgement can both fail, and the redelivery guard still
        has to refuse. It can only do that if the marker is independent of them.
        """
        put_execution(client)
        store.record_abort_intent(
            invocation_id=INVOCATION, tenant_id=TENANT, attempt=1,
            command_id=COMMAND, body_digest=DIGEST, now=NOW,
        )
        client.update_item(
            TableName=TABLE,
            Key={"pk": {"S": f"TENANT#{TENANT}"}, "sk": {"S": f"EXEC#{INVOCATION}"}},
            UpdateExpression="SET #st = :cancelled",
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={":cancelled": {"S": "cancelled"}},
        )
        assert store.abort_intent(invocation_id=INVOCATION, tenant_id=TENANT)["command_id"] == COMMAND
