"""
Unit tests for cyber worker handlers (updated for real handlers).

Uses moto to mock SQS + DynamoDB + S3. Exercises:
- WORKER_ROLE=triage  -> processes SQS msg -> downloads from S3 -> DDB row -> response
- WORKER_ROLE=static  -> same shape, different stage value
- WORKER_ROLE="" (unset) -> entrypoint exits with code 2
"""

import json
import os
import subprocess
import sys

import boto3
import pytest
from moto import mock_aws

WORKERS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

EICAR_BYTES = b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"


@pytest.fixture()
def aws_env():
    """Set up mocked AWS resources: 2 SQS queues + 1 DynamoDB table + S3."""
    with mock_aws():
        region = "us-east-1"
        sqs = boto3.client("sqs", region_name=region)
        ddb = boto3.client("dynamodb", region_name=region)
        s3 = boto3.client("s3", region_name=region)

        # Create input queue (standard)
        input_q = sqs.create_queue(QueueName="cyber-triage-input")
        input_url = input_q["QueueUrl"]

        # Create response queue (FIFO)
        resp_q = sqs.create_queue(
            QueueName="cyber-response.fifo",
            Attributes={
                "FifoQueue": "true",
                "ContentBasedDeduplication": "false",
            },
        )
        resp_url = resp_q["QueueUrl"]

        # Create results table with correct schema (stage_timestamp as range key)
        ddb.create_table(
            TableName="cyber-results",
            KeySchema=[
                {"AttributeName": "artifact_id", "KeyType": "HASH"},
                {"AttributeName": "stage_timestamp", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "artifact_id", "AttributeType": "S"},
                {"AttributeName": "stage_timestamp", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )

        # Create S3 bucket with test sample
        s3.create_bucket(Bucket="test-samples")
        s3.put_object(Bucket="test-samples", Key="samples/test.bin", Body=EICAR_BYTES)

        # Set env vars the handlers expect
        os.environ["INPUT_QUEUE_URL"] = input_url
        os.environ["RESPONSE_QUEUE_URL"] = resp_url
        os.environ["RESULTS_TABLE"] = "cyber-results"
        os.environ["IMAGE_TAG"] = "test-sha"
        os.environ["AWS_DEFAULT_REGION"] = region

        yield {
            "sqs": sqs,
            "s3": s3,
            "ddb": boto3.resource("dynamodb", region_name=region),
            "input_url": input_url,
            "resp_url": resp_url,
            "region": region,
        }


def _send_sample_message(sqs_client, queue_url: str, artifact_id: str) -> None:
    """Send a sample message to the input queue (uses real handler format)."""
    sqs_client.send_message(
        QueueUrl=queue_url,
        MessageBody=json.dumps({
            "artifact_id": artifact_id,
            "sample_s3_uri": "s3://test-samples/samples/test.bin",
        }),
    )


class TestTriageHandler:
    def test_processes_message_and_writes_ddb(self, aws_env):
        """Triage handler reads SQS, writes DDB row, sends response, deletes receipt."""
        _send_sample_message(aws_env["sqs"], aws_env["input_url"], "art-001")

        from triage.handler import run

        run()

        # Verify DDB row
        table = aws_env["ddb"].Table("cyber-results")
        items = table.scan(
            FilterExpression=boto3.dynamodb.conditions.Attr("artifact_id").eq("art-001")
        )["Items"]
        assert len(items) == 1
        assert items[0]["stage"] == "triage"
        assert items[0]["status"] == "ok"
        assert items[0]["image_tag"] == "test-sha"

        # Verify findings contain real hashes
        findings = json.loads(items[0]["findings"])
        assert "hashes" in findings
        assert len(findings["hashes"]["sha256"]) == 64

        # Verify response queue received a message
        resp_msgs = aws_env["sqs"].receive_message(
            QueueUrl=aws_env["resp_url"], MaxNumberOfMessages=1
        )
        assert len(resp_msgs.get("Messages", [])) == 1
        resp_body = json.loads(resp_msgs["Messages"][0]["Body"])
        assert resp_body["artifact_id"] == "art-001"
        assert resp_body["stage"] == 1
        assert resp_body["stage_name"] == "triage"

        # Verify input queue is now empty (message was deleted)
        remaining = aws_env["sqs"].receive_message(
            QueueUrl=aws_env["input_url"],
            MaxNumberOfMessages=1,
            WaitTimeSeconds=0,
        )
        assert len(remaining.get("Messages", [])) == 0

    def test_no_message_exits_cleanly(self, aws_env, capsys):
        """Empty queue -> prints 'No message' and returns without error."""
        from triage.handler import run

        run()

        captured = capsys.readouterr()
        assert "No message" in captured.out


class TestStaticHandler:
    def test_processes_message_and_writes_ddb(self, aws_env):
        """Static handler reads SQS, writes DDB row with stage=static."""
        _send_sample_message(aws_env["sqs"], aws_env["input_url"], "art-002")

        from static.handler import run

        run()

        table = aws_env["ddb"].Table("cyber-results")
        items = table.scan(
            FilterExpression=boto3.dynamodb.conditions.Attr("artifact_id").eq("art-002")
        )["Items"]
        assert len(items) == 1
        assert items[0]["stage"] == "static"
        assert items[0]["status"] == "ok"

        # Verify findings contain real analysis
        findings = json.loads(items[0]["findings"])
        assert findings["mode"] == "rule-driven"

        resp_msgs = aws_env["sqs"].receive_message(
            QueueUrl=aws_env["resp_url"], MaxNumberOfMessages=1
        )
        resp_body = json.loads(resp_msgs["Messages"][0]["Body"])
        assert resp_body["stage"] == 3
        assert resp_body["stage_name"] == "static"


class TestEntrypoint:
    def test_invalid_role_exits_2(self):
        """Unset/invalid WORKER_ROLE causes exit code 2."""
        result = subprocess.run(
            [sys.executable, os.path.join(WORKERS_DIR, "entrypoint.py")],
            env={**os.environ, "WORKER_ROLE": ""},
            capture_output=True,
            text=True,
        )
        assert result.returncode == 2
        assert "WORKER_ROLE must be" in result.stderr

    def test_invalid_role_value_exits_2(self):
        """Random WORKER_ROLE value causes exit code 2."""
        result = subprocess.run(
            [sys.executable, os.path.join(WORKERS_DIR, "entrypoint.py")],
            env={**os.environ, "WORKER_ROLE": "bogus"},
            capture_output=True,
            text=True,
        )
        assert result.returncode == 2
        assert "bogus" in result.stderr
