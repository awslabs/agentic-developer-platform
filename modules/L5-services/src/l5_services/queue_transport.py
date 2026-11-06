"""Transport for SQL-owned worker messages; admission commits before publication."""

import asyncio
import json


def sqs_publisher(queue_url, *, client=None):
    if not queue_url:
        raise ValueError("worker queue URL is required")
    if client is None:
        import boto3

        client = boto3.client("sqs")

    async def publish(envelope, delay):
        if not isinstance(delay, int) or not 0 <= delay <= 900:
            raise ValueError("SQS delay must be between 0 and 900 seconds")
        await asyncio.to_thread(
            client.send_message, QueueUrl=queue_url,
            MessageBody=json.dumps(envelope, separators=(",", ":")), DelaySeconds=delay,
        )

    return publish
