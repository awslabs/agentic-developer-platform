"""Tests for SQS publisher."""

import json
from unittest.mock import MagicMock, patch

import pytest

# The module-level SUBMIT_QUEUE_URL in sqs_publisher is evaluated at import
# time, so patching os.environ alone is insufficient when another test file
# imports the module first with a different URL.  We patch the module-level
# constant directly alongside os.environ to guarantee test isolation.
_TEST_QUEUE_URL = "https://sqs.us-east-1.amazonaws.com/123/queue.fifo"


@pytest.mark.parametrize(
    "authority, admitted, expected",
    [(False, True, False), (True, False, False), (True, True, True)],
)
def test_claim_admission_precedes_queue_publication(
    monkeypatch, authority, admitted, expected
):
    from common import gateway_client, sqs_publisher

    monkeypatch.setenv("ADP_WORK_CLAIMS_ENABLED", "true")
    monkeypatch.setenv("AGENT_AUTHORITY_ENABLED", str(authority).lower())
    monkeypatch.setattr(sqs_publisher, "SUBMIT_QUEUE_URL", _TEST_QUEUE_URL)
    producer = MagicMock()
    producer.send_message.return_value = {"MessageId": "sqs-id"}
    monkeypatch.setattr(sqs_publisher, "_sqs", producer)
    admit = MagicMock(return_value=admitted)
    monkeypatch.setattr(gateway_client, "admit_issue_work", admit)
    result = sqs_publisher.publish_envelope(
        {
            "channel": "github",
            "message_id": "run",
            "tenant_id": "org",
            "source_ref": {"repo": "org/repo", "issue": 1},
        }
    )
    assert producer.send_message.called is expected
    assert bool(result) is expected
    assert admit.called is authority


class TestPublishEnvelope:
    @patch("common.sqs_publisher.SUBMIT_QUEUE_URL", _TEST_QUEUE_URL)
    @patch.dict(
        "os.environ",
        {"SUBMIT_QUEUE_URL": _TEST_QUEUE_URL},
    )
    @patch("common.sqs_publisher._sqs", None)
    @patch("common.sqs_publisher.boto3")
    def test_publish_success(self, mock_boto3: MagicMock) -> None:
        from common.sqs_publisher import publish_envelope

        mock_sqs = MagicMock()
        mock_boto3.client.return_value = mock_sqs
        mock_sqs.send_message.return_value = {"MessageId": "msg-xyz"}

        envelope = {
            "tenant_id": "tenant-123",
            "channel": "github",
            "arrived_at": "2026-01-01T00:00:00Z",
            "source_ref": {"repo": "org/repo", "issue": 5},
            "payload": {"action": "labeled"},
        }
        result = publish_envelope(envelope)

        assert result == "msg-xyz"
        mock_sqs.send_message.assert_called_once()
        call_kwargs = mock_sqs.send_message.call_args[1]
        assert call_kwargs["QueueUrl"] == _TEST_QUEUE_URL
        # Per-run group ID (not per-tenant) to avoid head-of-line blocking.
        assert call_kwargs["MessageGroupId"] == "tenant-123#org/repo#5"
        assert "MessageDeduplicationId" in call_kwargs

        # Verify the body is valid JSON with expected fields
        body = json.loads(call_kwargs["MessageBody"])
        assert body["tenant_id"] == "tenant-123"
        assert body["channel"] == "github"

    @patch("common.sqs_publisher.SUBMIT_QUEUE_URL", "")
    @patch.dict("os.environ", {"SUBMIT_QUEUE_URL": ""})
    def test_missing_queue_url_returns_none(self) -> None:
        from common.sqs_publisher import publish_envelope

        result = publish_envelope({"tenant_id": "t1"})
        assert result is None

    @patch("common.sqs_publisher.SUBMIT_QUEUE_URL", _TEST_QUEUE_URL)
    @patch.dict(
        "os.environ",
        {"SUBMIT_QUEUE_URL": _TEST_QUEUE_URL},
    )
    @patch("common.sqs_publisher._sqs", None)
    @patch("common.sqs_publisher.boto3")
    def test_message_group_is_scoped_per_run(self, mock_boto3: MagicMock) -> None:
        """Group ID must be tenant#repo#issue, not just tenant, to avoid
        head-of-line blocking when one run's message gets stuck."""
        from common.sqs_publisher import publish_envelope

        mock_sqs = MagicMock()
        mock_boto3.client.return_value = mock_sqs
        mock_sqs.send_message.return_value = {"MessageId": "m1"}

        envelope = {
            "tenant_id": "unique-tenant",
            "arrived_at": "2026-01-01T00:00:00Z",
            "source_ref": {"repo": "o/r", "issue": 1},
        }
        publish_envelope(envelope)

        call_kwargs = mock_sqs.send_message.call_args[1]
        assert call_kwargs["MessageGroupId"] == "unique-tenant#o/r#1"

    @patch("common.sqs_publisher.SUBMIT_QUEUE_URL", _TEST_QUEUE_URL)
    @patch.dict(
        "os.environ",
        {"SUBMIT_QUEUE_URL": _TEST_QUEUE_URL},
    )
    @patch("common.sqs_publisher._sqs", None)
    @patch("common.sqs_publisher.boto3")
    def test_two_runs_different_issues_dont_share_group(
        self, mock_boto3: MagicMock
    ) -> None:
        """Two runs for different issues in the same tenant must use
        different MessageGroupIds so one stuck run doesn't block the other."""
        from common.sqs_publisher import publish_envelope

        mock_sqs = MagicMock()
        mock_boto3.client.return_value = mock_sqs
        mock_sqs.send_message.return_value = {"MessageId": "m1"}

        publish_envelope(
            {
                "tenant_id": "t",
                "arrived_at": "2026-01-01T00:00:00Z",
                "source_ref": {"repo": "o/r", "issue": 1},
            }
        )
        publish_envelope(
            {
                "tenant_id": "t",
                "arrived_at": "2026-01-01T00:00:01Z",
                "source_ref": {"repo": "o/r", "issue": 2},
            }
        )

        calls = mock_sqs.send_message.call_args_list
        assert calls[0][1]["MessageGroupId"] != calls[1][1]["MessageGroupId"]
        assert calls[0][1]["MessageGroupId"] == "t#o/r#1"
        assert calls[1][1]["MessageGroupId"] == "t#o/r#2"

    @patch("common.sqs_publisher.SUBMIT_QUEUE_URL", _TEST_QUEUE_URL)
    @patch.dict(
        "os.environ",
        {"SUBMIT_QUEUE_URL": _TEST_QUEUE_URL},
    )
    @patch("common.sqs_publisher._sqs", None)
    @patch("common.sqs_publisher.boto3")
    def test_sqs_error_returns_none(self, mock_boto3: MagicMock) -> None:
        from common.sqs_publisher import publish_envelope

        mock_sqs = MagicMock()
        mock_boto3.client.return_value = mock_sqs
        mock_sqs.send_message.side_effect = Exception("SQS timeout")

        envelope = {
            "tenant_id": "t1",
            "arrived_at": "x",
            "source_ref": {"repo": "a", "issue": 1},
        }
        result = publish_envelope(envelope)
        assert result is None
