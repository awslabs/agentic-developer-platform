"""
M1 stub. Receives a single SQS message pointing at an S3 sample, writes a
placeholder row to DDB, acks the message. Real static-analysis logic is a
follow-up issue -- but the plumbing (IAM, queues, table, networking) is
exercised here.
"""

import json
import os
import time

import boto3

QUEUE_URL = os.environ["INPUT_QUEUE_URL"]
RESPONSE_QUEUE_URL = os.environ["RESPONSE_QUEUE_URL"]
RESULTS_TABLE = os.environ["RESULTS_TABLE"]


def run() -> None:
    sqs = boto3.client("sqs")
    ddb = boto3.resource("dynamodb").Table(RESULTS_TABLE)

    resp = sqs.receive_message(
        QueueUrl=QUEUE_URL,
        MaxNumberOfMessages=1,
        WaitTimeSeconds=20,
        MessageAttributeNames=["All"],
    )
    msgs = resp.get("Messages", [])
    if not msgs:
        print("No message, exiting.")
        return

    msg = msgs[0]
    body = json.loads(msg["Body"])
    artifact_id = body["artifact_id"]
    ts = int(time.time())

    ddb.put_item(
        Item={
            "artifact_id": artifact_id,
            "SK": f"static#{ts}",
            "status": "stub-ok",
            "stage": "static",
            "image_tag": os.environ.get("IMAGE_TAG", "unknown"),
        }
    )

    sqs.send_message(
        QueueUrl=RESPONSE_QUEUE_URL,
        MessageBody=json.dumps(
            {"artifact_id": artifact_id, "stage": "static", "status": "stub-ok"}
        ),
        MessageGroupId=artifact_id,
        MessageDeduplicationId=f"{artifact_id}-static-{ts}",
    )

    sqs.delete_message(QueueUrl=QUEUE_URL, ReceiptHandle=msg["ReceiptHandle"])
    print(f"static stub handled {artifact_id}")
