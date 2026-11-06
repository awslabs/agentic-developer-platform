"""Offline queue transport checks; no AWS resource is created."""

import json
from unittest import IsolatedAsyncioTestCase
from unittest.mock import Mock

from l5_services.queue_transport import sqs_publisher


class QueueTransportTests(IsolatedAsyncioTestCase):
    async def test_immediate_and_delayed_messages_preserve_opaque_envelope(self):
        client = Mock()
        publish = sqs_publisher("https://sqs.example.invalid/queue", client=client)
        envelope = {"schema_version": "1.0", "work_id": "work-1", "generation": 2}

        await publish(envelope, 0)
        await publish(envelope, 600)

        assert client.send_message.call_count == 2
        for index, delay in enumerate((0, 600)):
            sent = client.send_message.call_args_list[index].kwargs
            assert sent["QueueUrl"] == "https://sqs.example.invalid/queue"
            assert sent["DelaySeconds"] == delay
            assert json.loads(sent["MessageBody"]) == envelope
            assert "tenant_id" not in sent["MessageBody"]

    async def test_invalid_delay_and_transport_failure_do_not_report_success(self):
        client = Mock()
        publish = sqs_publisher("https://sqs.example.invalid/queue", client=client)
        for delay in (-1, 901, 1.5):
            with self.assertRaises(ValueError):
                await publish({"work_id": "work-1"}, delay)
        client.send_message.assert_not_called()

        client.send_message.side_effect = TimeoutError("send failed")
        with self.assertRaises(TimeoutError):
            await publish({"work_id": "work-1"}, 0)
        client.send_message.assert_called_once()

    def test_queue_address_is_explicit(self):
        with self.assertRaises(ValueError):
            sqs_publisher("", client=Mock())
