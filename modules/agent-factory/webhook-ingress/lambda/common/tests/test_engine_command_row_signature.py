"""The attribution signature is written atomically with the marker (issue #4539).

The marked row is the engine bridge's delivery mechanism (#4527), which makes it
the carrier of *who asked for what, on which plan*. Those authority fields are
ordinary mutable attributes; GitHub's HMAC is verified on the delivery and nothing
carried that verification forward, so anything able to write the row could choose
the acting identity and routing target of a human approval.

`test_command_signing.py` pins the signature's cryptographic properties. This file
pins the *row shape* those properties depend on — the ways the mechanism could be
cryptographically sound and still fail to protect anything:

* **Stored, not merely returned.** The verifier reads DynamoDB, not the dict this
  Lambda returns. A signature that never lands is a command the tick refuses.
* **Reproducible from the row alone.** The stored payload plus the stored key id
  must be sufficient to recompute the signature byte-for-byte. This is the test
  that would catch the `Decimal` round-trip: a payload stored as a map comes back
  with `Decimal('4539')` where `4539` was signed.
* **All-or-nothing, in both directions.** No signature without a marker (dead
  weight the sparse index never surfaces, suggesting a verified command exists
  where none does), and no marker "half-signed" — a signature with no key id is
  unverifiable-but-signed-looking, and a verifier that read a missing key id as
  "use the active key" would let an attacker choose which key checks their forgery.
* **An unsigned marker is a legitimate, visible state.** It is what signing failure
  produces. The row must still be written (it is the audit record of a command
  somebody really sent) and the failure must be counted, because the tick's refusal
  alone cannot say whether the key was never seeded.
"""

from __future__ import annotations

import json

import boto3
import pytest
from moto import mock_aws

from common.command_signing import compute_signature
from common.webhook_events import (
    ENGINE_COMMAND_KEY_ID_ATTR,
    ENGINE_COMMAND_PROTOCOL,
    ENGINE_COMMAND_PROTOCOL_VERSION_ATTR,
    ENGINE_COMMAND_SIGNATURE_ATTR,
    ENGINE_COMMAND_SIGNED_PAYLOAD_ATTR,
    ENGINE_COMMAND_STATUS_PENDING,
    WebhookEventLogger,
)

TABLE = "adp-dev-webhook-events"

#: Test-only key material. Never a real key: these tests assert the arithmetic of
#: "the stored payload under the stored key id reproduces the stored signature",
#: which is independent of the key's value.
_TEST_KEY = b"row-shape-test-key-not-a-real-signing-key"


@pytest.fixture(autouse=True)
def aws_credentials(monkeypatch):
    """Placeholder credentials, so moto never reaches a real account.

    Defined locally rather than in a conftest for the same reason
    `test_engine_command_row_4527.py` does: CI runs these with `pytest lambda/`,
    which does not collect `tests/conftest.py`. Autouse, so a test here can never
    silently fall back to the ambient environment.
    """
    for name in (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SECURITY_TOKEN",
        "AWS_SESSION_TOKEN",
    ):
        monkeypatch.setenv(name, "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


def _create_table():
    """The table with the sparse engine-command index, mirroring `infra/dynamodb.tf`."""
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    ddb.create_table(
        TableName=TABLE,
        KeySchema=[
            {"AttributeName": "event_id", "KeyType": "HASH"},
            {"AttributeName": "arrived_at", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "event_id", "AttributeType": "S"},
            {"AttributeName": "arrived_at", "AttributeType": "S"},
            {"AttributeName": "engine_command_status", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "engine-command-index",
                "KeySchema": [
                    {"AttributeName": "engine_command_status", "KeyType": "HASH"},
                    {"AttributeName": "arrived_at", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    return ddb.Table(TABLE)


def _envelope(**overrides) -> dict:
    """A spec-valid signed envelope, as `sign_command` would return it."""
    envelope = {
        "protocol_version": ENGINE_COMMAND_PROTOCOL,
        "key_id": "2026-09",
        "provider": "github",
        "delivery_id": "d-1",
        "event_type": "issue_comment",
        "event_id": "delivery-signed-1",
        "arrived_at": "2026-09-15T10:00:00Z",
        "tenant_id": "acme-corp",
        "installation_id": "99887766",
        "repo_id": 55501,
        "repo": "acme-corp/app",
        "issue_number": 4539,
        "sender_github_id": "1042",
        "sender_type": "User",
        "command_body": "@agent-engine halt",
        "signed_at": "2026-09-15T10:00:01Z",
    }
    envelope.update(overrides)
    return envelope


def _log_signed(logger, *, envelope=None, key=_TEST_KEY, **overrides):
    """Write a marked, signed row the way the handler will.

    The canonical payload is produced by the signer itself rather than hand-written,
    so this helper cannot accidentally assert against a shape the signer would never
    emit.
    """
    from common.command_signing import canonical_bytes

    envelope = envelope or _envelope()
    kwargs = {
        "event_id": envelope["event_id"],
        "arrived_at": envelope["arrived_at"],
        "tenant_id": envelope["tenant_id"],
        "channel": "github",
        "event_type": "issue_comment",
        "action": "created",
        "repo": envelope["repo"],
        "issue_number": envelope["issue_number"],
        "installation_id": envelope["installation_id"],
        "status": "no_op",
        "engine_command": True,
        "comment_body": envelope["command_body"],
        "sender_github_id": envelope["sender_github_id"],
        "engine_command_signature": compute_signature(key, envelope),
        "engine_command_signing_key_id": envelope["key_id"],
        "engine_command_signed_payload": canonical_bytes(envelope).decode("utf-8"),
    }
    kwargs.update(overrides)
    return logger.log_event(**kwargs)


class TestTheSignatureIsStoredWithTheCommand:
    @mock_aws
    def test_all_four_attributes_are_stored(self, aws_credentials):
        """The verifier reads the table, so the table is the contract."""
        table = _create_table()
        _log_signed(WebhookEventLogger(table_name=TABLE))

        stored = table.get_item(
            Key={"event_id": "delivery-signed-1", "arrived_at": "2026-09-15T10:00:00Z"}
        )["Item"]

        assert stored["engine_command_status"] == ENGINE_COMMAND_STATUS_PENDING
        assert stored[ENGINE_COMMAND_SIGNATURE_ATTR]
        assert stored[ENGINE_COMMAND_KEY_ID_ATTR] == "2026-09"
        assert stored[ENGINE_COMMAND_SIGNED_PAYLOAD_ATTR]
        assert stored[ENGINE_COMMAND_PROTOCOL_VERSION_ATTR] == ENGINE_COMMAND_PROTOCOL

    @mock_aws
    def test_the_stored_payload_reproduces_the_stored_signature(self, aws_credentials):
        """The property the whole mechanism rests on, asserted after a round-trip.

        Not a restatement of the signer's own tests: this recomputes from what
        DynamoDB *returned*, which is the only thing the verifier will ever see. It
        is the test that fails if the payload is ever stored as a map — boto3's
        resource layer would return `Decimal('4539')` where `4539` was signed, and
        the recomputation would diverge.
        """
        table = _create_table()
        _log_signed(WebhookEventLogger(table_name=TABLE))

        stored = table.get_item(
            Key={"event_id": "delivery-signed-1", "arrived_at": "2026-09-15T10:00:00Z"}
        )["Item"]

        payload = stored[ENGINE_COMMAND_SIGNED_PAYLOAD_ATTR]
        assert isinstance(payload, str), (
            "the signed payload must survive DynamoDB as a STRING; a map would "
            "round-trip its numbers through Decimal and change the signed bytes"
        )
        recomputed = compute_signature(_TEST_KEY, dict(json.loads(payload)))
        assert recomputed == stored[ENGINE_COMMAND_SIGNATURE_ATTR]

    @mock_aws
    def test_numeric_fields_survive_as_json_numbers(self, aws_credentials):
        """`4539`, not `Decimal('4539')` and not `"4539"`.

        Called out separately because the failure is invisible at the row level —
        the attribute is present and looks right — and only shows up as every
        command being refused in production.
        """
        table = _create_table()
        _log_signed(WebhookEventLogger(table_name=TABLE))

        stored = table.get_item(
            Key={"event_id": "delivery-signed-1", "arrived_at": "2026-09-15T10:00:00Z"}
        )["Item"]
        parsed = dict(json.loads(stored[ENGINE_COMMAND_SIGNED_PAYLOAD_ATTR]))

        assert parsed["issue_number"] == 4539
        assert isinstance(parsed["issue_number"], int)
        assert isinstance(parsed["repo_id"], int)

    @mock_aws
    def test_a_unicode_body_round_trips_intact(self, aws_credentials):
        """The signature must still reproduce for the bodies humans actually type."""
        table = _create_table()
        body = "@agent-engine replan: Änderung 日本語\nzeile"
        envelope = _envelope(command_body=body)
        _log_signed(WebhookEventLogger(table_name=TABLE), envelope=envelope)

        stored = table.get_item(
            Key={"event_id": "delivery-signed-1", "arrived_at": "2026-09-15T10:00:00Z"}
        )["Item"]
        payload = stored[ENGINE_COMMAND_SIGNED_PAYLOAD_ATTR]

        assert (
            compute_signature(_TEST_KEY, dict(json.loads(payload)))
            == (stored[ENGINE_COMMAND_SIGNATURE_ATTR])
        )
        assert "Änderung" in payload


class TestAllOrNothing:
    @mock_aws
    def test_no_signature_is_written_without_a_marker(self, aws_credentials):
        """A signature on an unmarked row would suggest a command that does not exist.

        The sparse index never surfaces such a row, so nothing would ever verify it —
        but its presence would make an operator (or a future reader) believe a
        verified command was recorded.
        """
        table = _create_table()
        WebhookEventLogger(table_name=TABLE).log_event(
            event_id="ordinary-1",
            arrived_at="2026-09-15T11:00:00Z",
            tenant_id="acme-corp",
            channel="github",
            event_type="issue_comment",
            action="created",
            status="no_op",
            engine_command=False,
            engine_command_signature="c2ln",
            engine_command_signing_key_id="2026-09",
            engine_command_signed_payload='[["protocol_version","1"]]',
        )

        stored = table.get_item(
            Key={"event_id": "ordinary-1", "arrived_at": "2026-09-15T11:00:00Z"}
        )["Item"]

        assert "engine_command_status" not in stored
        assert ENGINE_COMMAND_SIGNATURE_ATTR not in stored
        assert ENGINE_COMMAND_KEY_ID_ATTR not in stored
        assert ENGINE_COMMAND_SIGNED_PAYLOAD_ATTR not in stored

    @pytest.mark.parametrize(
        ("omit", "why"),
        [
            (
                "engine_command_signature",
                "a key id and payload with no signature verifies nothing",
            ),
            (
                "engine_command_signing_key_id",
                "a verifier with no key id would have to GUESS which key to check "
                "against, handing that choice to whoever wrote the row",
            ),
            (
                "engine_command_signed_payload",
                "a signature with nothing to recompute over cannot be checked",
            ),
        ],
    )
    @mock_aws
    def test_a_partial_signature_is_stored_as_no_signature(
        self, aws_credentials, omit, why
    ):
        """Partial input degrades to unsigned, never to partially trusted."""
        table = _create_table()
        _log_signed(WebhookEventLogger(table_name=TABLE), **{omit: None})

        stored = table.get_item(
            Key={"event_id": "delivery-signed-1", "arrived_at": "2026-09-15T10:00:00Z"}
        )["Item"]

        assert stored["engine_command_status"] == ENGINE_COMMAND_STATUS_PENDING, (
            "the marker and the audit row must survive — a command somebody really "
            "sent is not erased because it could not be signed"
        )
        for attr in (
            ENGINE_COMMAND_SIGNATURE_ATTR,
            ENGINE_COMMAND_KEY_ID_ATTR,
            ENGINE_COMMAND_SIGNED_PAYLOAD_ATTR,
            ENGINE_COMMAND_PROTOCOL_VERSION_ATTR,
        ):
            assert attr not in stored, f"{attr} survived a partial signature: {why}"

    @mock_aws
    def test_an_empty_string_signature_is_not_a_signature(self, aws_credentials):
        """Whitespace or empty is absent, so no `compare_digest` ever sees it."""
        table = _create_table()
        _log_signed(
            WebhookEventLogger(table_name=TABLE),
            engine_command_signature="   ",
        )

        stored = table.get_item(
            Key={"event_id": "delivery-signed-1", "arrived_at": "2026-09-15T10:00:00Z"}
        )["Item"]
        assert ENGINE_COMMAND_SIGNATURE_ATTR not in stored


class TestUnsignedIsVisible:
    """Signing failure must be countable, not merely refusable."""

    @mock_aws
    def test_an_unsigned_marker_still_writes_the_row(self, aws_credentials):
        """The audit record of a real command is never the thing we drop.

        Dropping it would mean an environment with no key seeded has no evidence
        anybody ever tried to command the engine.
        """
        table = _create_table()
        _log_signed(
            WebhookEventLogger(table_name=TABLE),
            engine_command_signature=None,
            engine_command_signing_key_id=None,
            engine_command_signed_payload=None,
        )

        stored = table.get_item(
            Key={"event_id": "delivery-signed-1", "arrived_at": "2026-09-15T10:00:00Z"}
        )["Item"]
        assert stored["engine_command_status"] == ENGINE_COMMAND_STATUS_PENDING
        assert stored["engine_command_body"] == "@agent-engine halt"
        assert ENGINE_COMMAND_SIGNATURE_ATTR not in stored

    @mock_aws
    def test_an_unsigned_marker_emits_the_metric(self, aws_credentials, monkeypatch):
        """`absent` — signing raised, e.g. the key was never seeded."""
        emitted = []
        import common.webhook_events as we

        monkeypatch.setattr(we, "_emit_engine_command_unsigned", emitted.append)
        _create_table()
        _log_signed(
            WebhookEventLogger(table_name=TABLE),
            engine_command_signature=None,
            engine_command_signing_key_id=None,
            engine_command_signed_payload=None,
        )

        assert emitted == ["absent"]

    @mock_aws
    def test_a_partial_signature_is_counted_separately(
        self, aws_credentials, monkeypatch
    ):
        """`incomplete` — a caller passed a subset, which is a code defect.

        Distinguished from `absent` because the two need different responses: seed a
        key, versus fix a call site. One dimension value for both would make the
        code defect indistinguishable from a normal unseeded environment.
        """
        emitted = []
        import common.webhook_events as we

        monkeypatch.setattr(we, "_emit_engine_command_unsigned", emitted.append)
        _create_table()
        _log_signed(
            WebhookEventLogger(table_name=TABLE),
            engine_command_signing_key_id=None,
        )

        assert emitted == ["incomplete"]

    @mock_aws
    def test_a_signed_marker_emits_nothing(self, aws_credentials, monkeypatch):
        """Positive control: no per-command metric cost on the happy path."""
        emitted = []
        import common.webhook_events as we

        monkeypatch.setattr(we, "_emit_engine_command_unsigned", emitted.append)
        _create_table()
        _log_signed(WebhookEventLogger(table_name=TABLE))

        assert emitted == []

    @mock_aws
    def test_an_ordinary_row_emits_nothing(self, aws_credentials, monkeypatch):
        """Non-engine deliveries are the overwhelming majority of traffic."""
        emitted = []
        import common.webhook_events as we

        monkeypatch.setattr(we, "_emit_engine_command_unsigned", emitted.append)
        _create_table()
        WebhookEventLogger(table_name=TABLE).log_event(
            event_id="ordinary-2",
            arrived_at="2026-09-15T11:00:01Z",
            tenant_id="acme-corp",
            channel="github",
            event_type="push",
            action="",
            status="no_op",
        )

        assert emitted == []
