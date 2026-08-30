"""Issue #4527 — the marked event row is the engine bridge's delivery mechanism.

An `@agent-engine` comment produces no SQS message and no gateway call. The ROW is
how the command travels: this Lambda writes three attributes onto the event row it
already writes, and the gateway-side orchestration tick finds it by Querying the
sparse `engine-command-index`. That makes the row's shape a contract between two
deploy units, and these tests pin the parts of it that are load-bearing:

* **The marker is what makes the row findable.** The index is sparse on
  `engine_command_status`, so a row without that attribute is invisible to the tick
  no matter what else it carries. A body written without a marker is a command that
  is stored and never acted on.
* **The body is what makes it actionable.** A marker without a body would wake the
  tick up to a command it cannot parse. The three attributes are written together or
  not at all.
* **The sender is numeric.** Logins are renameable; a renamed account inheriting
  another user's approvals is an authorization bug, not a display bug.
* **Ordinary rows are untouched.** Marking every webhook would defeat the sparse
  index and put 30 days of deliveries in front of the tick on every wake.
"""

from __future__ import annotations

import boto3
import pytest
from moto import mock_aws

from common.webhook_events import (
    ENGINE_COMMAND_BODY_MAX_CHARS,
    ENGINE_COMMAND_STATUS_CONSUMED,
    ENGINE_COMMAND_STATUS_PENDING,
    WebhookEventLogger,
)

TABLE = "adp-dev-webhook-events"


@pytest.fixture(autouse=True)
def aws_credentials(monkeypatch):
    """Placeholder credentials, so moto never reaches a real account.

    Defined locally rather than taken from `tests/conftest.py`: CI runs these with
    `pytest lambda/`, which does not collect that conftest. Autouse because a test
    here that ran without it would fall back to the ambient environment, which is
    exactly the accident this guards against.
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
    """The table WITH the engine-command index, mirroring `infra/dynamodb.tf`.

    The index is declared here (and not only in the shared `_create_table` helper)
    because the sparse-projection behaviour below is the thing under test: a Query
    that returns a non-engine row would mean the index is not sparse.
    """
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


def _log(logger, **overrides):
    kwargs = {
        "tenant_id": "acme-corp",
        "channel": "github",
        "event_type": "issue_comment",
        "action": "created",
        "repo": "acme-corp/app",
        "issue_number": 4527,
        "status": "no_op",
    }
    kwargs.update(overrides)
    return logger.log_event(**kwargs)


class TestTheMarkedRow:
    @mock_aws
    def test_an_engine_command_writes_all_three_attributes(self, aws_credentials):
        """Marker, body and sender id land together — none is optional."""
        _create_table()
        item = _log(
            WebhookEventLogger(table_name=TABLE),
            event_id="delivery-engine-1",
            arrived_at="2026-08-30T10:00:00Z",
            engine_command=True,
            comment_body="@agent-engine halt",
            sender_github_id="100",
        )

        assert item["engine_command_status"] == ENGINE_COMMAND_STATUS_PENDING
        assert item["engine_command_body"] == "@agent-engine halt"
        assert item["engine_command_sender_github_id"] == "100"

    @mock_aws
    def test_the_row_is_durable_not_merely_returned(self, aws_credentials):
        """The returned dict is not the contract; the stored item is.

        `log_event` swallows write failures by design (a dropped audit row must not
        block a webhook response) and still returns the item it tried to write. So
        asserting on the return value alone would pass even if nothing was stored —
        and for the engine path a dropped row is a lost command, not a lost audit
        line.
        """
        table = _create_table()
        _log(
            WebhookEventLogger(table_name=TABLE),
            event_id="delivery-engine-2",
            arrived_at="2026-08-30T10:00:01Z",
            engine_command=True,
            comment_body="@agent-engine resume",
            sender_github_id="100",
        )

        stored = table.get_item(
            Key={"event_id": "delivery-engine-2", "arrived_at": "2026-08-30T10:00:01Z"}
        )["Item"]
        assert stored["engine_command_status"] == ENGINE_COMMAND_STATUS_PENDING
        assert stored["engine_command_body"] == "@agent-engine resume"

    @mock_aws
    def test_the_tick_finds_it_oldest_first_on_the_sparse_index(self, aws_credentials):
        """The tick's actual access pattern, end to end against the real index.

        Oldest-first matters: commands are a sequence a human typed, and applying
        `halt` after `resume` would leave the plan in the state they asked it to
        leave.
        """
        from boto3.dynamodb.conditions import Key

        table = _create_table()
        logger = WebhookEventLogger(table_name=TABLE)

        _log(
            logger,
            event_id="c-later",
            arrived_at="2026-08-30T12:00:00Z",
            engine_command=True,
            comment_body="@agent-engine resume",
            sender_github_id="100",
        )
        _log(
            logger,
            event_id="c-earlier",
            arrived_at="2026-08-30T11:00:00Z",
            engine_command=True,
            comment_body="@agent-engine halt",
            sender_github_id="100",
        )
        # An ordinary delivery: must not appear in the results at all.
        _log(
            logger,
            event_id="ordinary",
            arrived_at="2026-08-30T11:30:00Z",
            status="webhook_received",
        )

        items = table.query(
            IndexName="engine-command-index",
            KeyConditionExpression=Key("engine_command_status").eq(
                ENGINE_COMMAND_STATUS_PENDING
            ),
            ScanIndexForward=True,
        )["Items"]

        assert [i["event_id"] for i in items] == ["c-earlier", "c-later"]

    @mock_aws
    def test_a_consumed_row_leaves_the_pending_query(self, aws_credentials):
        """Consumption is a status flip, and the flip is what stops re-application.

        Pinned here rather than only on the tick side because the *index* is what
        makes it work: flipping the hash key moves the row out of the pending
        partition, so the next Query cannot see it even though the row (and its
        audit value) is still there.
        """
        from boto3.dynamodb.conditions import Key

        table = _create_table()
        _log(
            WebhookEventLogger(table_name=TABLE),
            event_id="c-consumed",
            arrived_at="2026-08-30T13:00:00Z",
            engine_command=True,
            comment_body="@agent-engine halt",
            sender_github_id="100",
        )

        table.update_item(
            Key={"event_id": "c-consumed", "arrived_at": "2026-08-30T13:00:00Z"},
            UpdateExpression="SET engine_command_status = :consumed",
            ConditionExpression="engine_command_status = :pending",
            ExpressionAttributeValues={
                ":consumed": ENGINE_COMMAND_STATUS_CONSUMED,
                ":pending": ENGINE_COMMAND_STATUS_PENDING,
            },
        )

        pending = table.query(
            IndexName="engine-command-index",
            KeyConditionExpression=Key("engine_command_status").eq(
                ENGINE_COMMAND_STATUS_PENDING
            ),
        )["Items"]
        assert pending == []
        # Still readable by primary key: the audit record survives consumption.
        assert (
            table.get_item(
                Key={"event_id": "c-consumed", "arrived_at": "2026-08-30T13:00:00Z"}
            )["Item"]["engine_command_status"]
            == ENGINE_COMMAND_STATUS_CONSUMED
        )


class TestOrdinaryRowsAreUnchanged:
    @mock_aws
    def test_a_normal_delivery_carries_none_of_the_three_attributes(
        self, aws_credentials
    ):
        """Sparseness is the point of the index, not a side effect.

        The table holds 30 days of every webhook delivery. If a normal row carried
        the marker, the tick would paginate all of it on every wake.
        """
        _create_table()
        item = _log(
            WebhookEventLogger(table_name=TABLE),
            event_id="plain-1",
            status="webhook_received",
        )

        assert "engine_command_status" not in item
        assert "engine_command_body" not in item
        assert "engine_command_sender_github_id" not in item

    @mock_aws
    def test_a_body_without_the_flag_is_not_stored(self, aws_credentials):
        """A comment body is payload content and must not be retained by default.

        `comment_body` is stored ONLY on the engine path, where the tick genuinely
        needs it to parse the command. Passing one without the flag must not put
        webhook payload text on an ordinary audit row.
        """
        _create_table()
        item = _log(
            WebhookEventLogger(table_name=TABLE),
            event_id="plain-2",
            comment_body="@agent-developer please fix this",
            sender_github_id="100",
        )

        assert "engine_command_body" not in item
        assert "engine_command_sender_github_id" not in item


class TestBoundsAndFallbacks:
    @mock_aws
    def test_a_long_body_is_truncated_not_dropped(self, aws_credentials):
        """A body must never be what makes the write fail.

        The row is written from webhook input, so its size is not ours to trust.
        Truncation keeps the command (which is on the first line) while bounding the
        item far below DynamoDB's 400 KB limit — whereas a rejected write would lose
        the command entirely.
        """
        _create_table()
        item = _log(
            WebhookEventLogger(table_name=TABLE),
            event_id="c-long",
            engine_command=True,
            comment_body="@agent-engine replan: " + ("x" * 50_000),
            sender_github_id="100",
        )

        assert len(item["engine_command_body"]) == ENGINE_COMMAND_BODY_MAX_CHARS
        assert item["engine_command_body"].startswith("@agent-engine replan: ")

    @mock_aws
    def test_a_missing_body_or_sender_still_writes_a_findable_row(
        self, aws_credentials
    ):
        """Absent optional values must not become absent ATTRIBUTES.

        DynamoDB has no empty-string-is-null coercion here, and the marker is what
        makes the row findable. Writing the marker while omitting the other two
        would give the tick a row it cannot parse and cannot attribute — better it
        finds a row with empty values, refuses it, and consumes it than that the
        marker be conditional on the body being present.
        """
        _create_table()
        item = _log(
            WebhookEventLogger(table_name=TABLE),
            event_id="c-empty",
            engine_command=True,
            comment_body=None,
            sender_github_id=None,
        )

        assert item["engine_command_status"] == ENGINE_COMMAND_STATUS_PENDING
        assert item["engine_command_body"] == ""
        assert item["engine_command_sender_github_id"] == ""


class TestTheStatusValuesAreTheSharedContract:
    def test_the_two_status_values_are_the_documented_literals(self):
        """The gateway tick re-declares these; separate deploy units cannot import.

        The tick's copy lives in `src/orchestration/engine_commands.py` and is
        asserted equal to these literals by the mirror of this test on that side. The
        literals are spelled out here rather than compared to the constants, so this
        test fails if a value CHANGES — which is the failure that matters, since a
        silent rename on one side makes every command invisible to the other.
        """
        assert ENGINE_COMMAND_STATUS_PENDING == "pending"
        assert ENGINE_COMMAND_STATUS_CONSUMED == "consumed"
        assert ENGINE_COMMAND_STATUS_PENDING != ENGINE_COMMAND_STATUS_CONSUMED
