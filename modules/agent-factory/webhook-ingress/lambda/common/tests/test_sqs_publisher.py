"""Tests for SQS publisher."""

import json
from unittest.mock import MagicMock, patch


class TestPublishEnvelope:
    @patch.dict(
        "os.environ",
        {"SUBMIT_QUEUE_URL": "https://sqs.us-east-1.amazonaws.com/123/queue.fifo"},
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
        assert (
            call_kwargs["QueueUrl"]
            == "https://sqs.us-east-1.amazonaws.com/123/queue.fifo"
        )
        assert call_kwargs["MessageGroupId"] == "tenant-123"
        assert "MessageDeduplicationId" in call_kwargs

        # Verify the body is valid JSON with expected fields
        body = json.loads(call_kwargs["MessageBody"])
        assert body["tenant_id"] == "tenant-123"
        assert body["channel"] == "github"

    @patch.dict("os.environ", {"SUBMIT_QUEUE_URL": ""})
    def test_missing_queue_url_returns_none(self) -> None:
        from common.sqs_publisher import publish_envelope

        result = publish_envelope({"tenant_id": "t1"})
        assert result is None

    @patch.dict(
        "os.environ",
        {"SUBMIT_QUEUE_URL": "https://sqs.us-east-1.amazonaws.com/123/queue.fifo"},
    )
    @patch("common.sqs_publisher._sqs", None)
    @patch("common.sqs_publisher.boto3")
    def test_message_group_is_tenant_id(self, mock_boto3: MagicMock) -> None:
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
        assert call_kwargs["MessageGroupId"] == "unique-tenant"

    @patch.dict(
        "os.environ",
        {"SUBMIT_QUEUE_URL": "https://sqs.us-east-1.amazonaws.com/123/queue.fifo"},
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
