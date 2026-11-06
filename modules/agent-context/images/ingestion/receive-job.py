"""Trusted init container: receive one message, admit it, then exit before parsing."""

import json
import os
from pathlib import Path

import boto3

from job_client import control_url, invoke


def receive():
    queue = os.environ["SQS_QUEUE_URL"]
    sqs = boto3.client("sqs", region_name=os.environ.get("AWS_REGION", "us-east-1"))
    response = sqs.receive_message(
        QueueUrl=queue, MaxNumberOfMessages=1, WaitTimeSeconds=20, VisibilityTimeout=4200
    )
    messages = response.get("Messages", [])
    target = Path(os.environ.get("INGESTION_JOB_FILE", "/admission/job.json"))
    if not messages:
        value = {"empty": True}
    else:
        message = messages[0]
        envelope = json.loads(message["Body"])
        # Source, owner, installation, steps and tags from SQS are not trusted.
        # Only the signed attempt is presented; the server rebuilds the job.
        value = invoke(
            control_url(),
            envelope.get("callback_grant", ""),
            "bootstrap",
            {"receipt_handle": message["ReceiptHandle"]},
        )
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o400)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream)


if __name__ == "__main__":
    receive()
