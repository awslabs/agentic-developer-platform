"""Task publication attributes and outcome honesty, against the T0 fixtures.

Fixtures come from docs/task-api/contracts/v1/fixtures and are consumed
unchanged: the evaluation manifest makes T0 their owner, so a test that edits
one to pass is not evidence of anything.

Covers T3-AC01 (ambiguous sends stay recoverable) and T3-AC02 (transport IDs
never become run IDs).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from common import task_publisher
from common.task_publisher import (
    TaskPublicationError,
    digest_components,
    message_group_id,
    publish_attributes,
    publish_task_envelope,
    serialize_envelope,
    validate_envelope,
)

FIXTURES = (
    Path(__file__).resolve().parents[6]
    / "docs"
    / "task-api"
    / "contracts"
    / "v1"
    / "fixtures"
)
QUEUE = "https://sqs.us-east-1.amazonaws.com/123456789012/adp-dev-agent-submit.fifo"


def fixture(relative: str) -> dict:
    body = json.loads((FIXTURES / relative).read_text())
    body.pop("$fixture", None)
    return body


@pytest.fixture(autouse=True)
def _no_real_sqs(monkeypatch):
    monkeypatch.setattr(task_publisher, "_sqs", None)


@pytest.fixture
def sqs(monkeypatch):
    client = MagicMock()
    client.send_message.return_value = {"MessageId": "transport-message-id"}
    monkeypatch.setattr(task_publisher, "_sqs", client)
    return client


def test_t0_dispatch_envelope_fixture_publishes_with_the_contract_attributes(sqs):
    """The committed envelope publishes, and the attributes match the contract.

    The expected attribute fixture is compared field by field rather than by
    shape so a change to either rule fails here.
    """
    envelope = fixture("valid/envelope-dispatch.json")
    expected = fixture("valid/sqs-publish-attributes.json")

    result = publish_task_envelope(envelope, queue_url=QUEUE)

    assert result == {
        "publication_outcome": "confirmed",
        "sqs_message_id": "transport-message-id",
    }
    sent = sqs.send_message.call_args.kwargs
    assert sent["MessageDeduplicationId"] == expected["MessageDeduplicationId"]
    assert sent["MessageDeduplicationId"] == envelope["dispatch_id"]
    assert len(sent["MessageGroupId"]) == 64


def test_dedup_key_is_the_dispatch_not_the_invocation(sqs):
    """The invalid fixture's mistake must not be reproducible from our code.

    Keying dedup on the invocation would let SQS swallow a recovery republish of
    the same run under a new dispatch ID -- no message, no error, a stalled task.
    """
    envelope = fixture("valid/envelope-dispatch.json")
    wrong = fixture("invalid/sqs-dedup-id-is-invocation.json")

    attributes = publish_attributes(envelope)

    assert attributes["MessageDeduplicationId"] != wrong["MessageDeduplicationId"]
    assert attributes["MessageDeduplicationId"] != envelope["invocation_id"]
    assert attributes["MessageDeduplicationId"] == envelope["dispatch_id"]


def test_recovery_republish_keeps_the_same_dispatch_dedup_key(sqs):
    """Recovery retries the exact immutable envelope and dispatch identity."""
    first = fixture("valid/envelope-dispatch.json")
    retry = dict(first)

    a = publish_attributes(first)
    b = publish_attributes(retry)

    assert a == b
    assert a["MessageDeduplicationId"] == first["dispatch_id"]


def test_group_id_isolates_tasks_and_tenants():
    """Per-task grouping: a stuck task must not block its tenant."""
    first_task = "tsk_aaaaaaaa-0000-4000-8000-000000000001"
    one = message_group_id("t-4821", first_task)
    two = message_group_id("t-4821", "tsk_aaaaaaaa-0000-4000-8000-000000000002")
    other_tenant = message_group_id("t-9999", first_task)

    assert one != two
    assert one != other_tenant


def test_digest_components_are_length_delimited_not_concatenated():
    """Ambiguous concatenation would let one pair collide with another."""
    assert digest_components("ab", "c") != digest_components("a", "bc")
    assert digest_components("ab", "c") == digest_components("ab", "c")


def test_transport_identifiers_are_refused_in_the_envelope(sqs):
    """T3-AC02: a receipt handle or SQS message ID is never a run handle."""
    envelope = fixture("invalid/envelope-transport-id-as-run-id.json")

    with pytest.raises(TaskPublicationError) as raised:
        validate_envelope(envelope)

    assert raised.value.code == "forbidden_envelope_field"
    assert not sqs.send_message.called


def test_message_id_must_equal_the_invocation_id(sqs):
    """Two divergent identifiers is how one run is reported as two."""
    envelope = fixture("invalid/envelope-message-id-not-invocation.json")

    with pytest.raises(TaskPublicationError) as raised:
        validate_envelope(envelope)

    assert raised.value.code == "message_id_not_invocation"
    assert not sqs.send_message.called


def test_confirmed_publication_does_not_put_the_transport_id_in_the_body(sqs):
    envelope = fixture("valid/envelope-dispatch.json")

    publish_task_envelope(envelope, queue_url=QUEUE)

    body = json.loads(sqs.send_message.call_args.kwargs["MessageBody"])
    assert "sqs_message_id" not in body
    assert "receipt_handle" not in body
    assert body["message_id"] == envelope["invocation_id"]


def test_ambiguous_send_reports_unknown_rather_than_failed(monkeypatch):
    """T3-AC01: a send that may have landed must not be called a failure.

    Calling it failed would authorize a fresh dispatch, and a fresh dispatch of
    a message that did land is a second execution.
    """
    client = MagicMock()
    client.send_message.side_effect = ConnectionResetError("reset")
    monkeypatch.setattr(task_publisher, "_sqs", client)

    with pytest.raises(TaskPublicationError) as raised:
        publish_task_envelope(fixture("valid/envelope-dispatch.json"), queue_url=QUEUE)

    assert raised.value.outcome == "unknown"
    assert raised.value.code == "send_ambiguous"


def test_success_response_without_a_message_id_is_not_confirmed(monkeypatch):
    client = MagicMock()
    client.send_message.return_value = {}
    monkeypatch.setattr(task_publisher, "_sqs", client)

    with pytest.raises(TaskPublicationError) as raised:
        publish_task_envelope(fixture("valid/envelope-dispatch.json"), queue_url=QUEUE)

    assert raised.value.outcome == "unknown"


def test_missing_queue_is_a_failure_before_any_send(sqs):
    """Nothing was sent, so this one IS definitively failed."""
    with pytest.raises(TaskPublicationError) as raised:
        publish_task_envelope(fixture("valid/envelope-dispatch.json"), queue_url="")

    assert raised.value.outcome == "failed"
    assert not sqs.send_message.called


def test_serialization_is_stable_so_the_committed_digest_still_matches():
    """The gateway persists the envelope digest BEFORE publication."""
    envelope = fixture("valid/envelope-dispatch.json")

    assert serialize_envelope(envelope) == serialize_envelope(
        json.loads(json.dumps(dict(reversed(list(envelope.items())))))
    )


def test_oversize_envelope_fails_loudly(sqs):
    envelope = fixture("valid/envelope-dispatch.json")
    envelope["persona"] = "a" * (64 * 1024)

    with pytest.raises(TaskPublicationError) as raised:
        publish_task_envelope(envelope, queue_url=QUEUE)

    assert raised.value.code == "envelope_too_large"
    assert not sqs.send_message.called


def test_legacy_publisher_is_untouched_by_the_task_path(monkeypatch):
    """The legacy envelope keeps its own grouping and dedup rules.

    Both paths share a queue. This asserts the task module did not change the
    legacy attributes, which is the regression that would break all current
    traffic.
    """
    from common import sqs_publisher

    monkeypatch.setenv("AGENT_AUTHORITY_ENABLED", "false")
    monkeypatch.setenv("ADP_WORK_CLAIMS_ENABLED", "false")
    monkeypatch.setattr(sqs_publisher, "SUBMIT_QUEUE_URL", QUEUE)
    client = MagicMock()
    client.send_message.return_value = {"MessageId": "legacy"}
    monkeypatch.setattr(sqs_publisher, "_sqs", client)

    sqs_publisher.publish_envelope(
        {
            "channel": "github",
            "message_id": "run-1",
            "tenant_id": "org",
            "arrived_at": "2026-09-24T00:00:00Z",
            "source_ref": {"repo": "org/repo", "issue": 7},
        }
    )

    sent = client.send_message.call_args.kwargs
    assert sent["MessageGroupId"] == "org#org/repo#7"
    assert sent["MessageDeduplicationId"] == "2026-09-24T00:00:00Z_org/repo_7"
