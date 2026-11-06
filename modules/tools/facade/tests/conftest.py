import boto3
import pytest
from moto import mock_aws


@pytest.fixture(autouse=True)
def execution_table(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("ADP_TOOLS_EXECUTION_TABLE", "adp-tools-dev-executions")
    with mock_aws():
        boto3.client("dynamodb", region_name="us-east-1").create_table(
            TableName="adp-tools-dev-executions", BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "execution_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "execution_id", "AttributeType": "S"},
                                  {"AttributeName": "cleanup_partition", "AttributeType": "S"},
                                  {"AttributeName": "next_cleanup_at", "AttributeType": "N"}],
            GlobalSecondaryIndexes=[{"IndexName": "cleanup-due",
                                     "KeySchema": [{"AttributeName": "cleanup_partition", "KeyType": "HASH"},
                                                   {"AttributeName": "next_cleanup_at", "KeyType": "RANGE"}],
                                     "Projection": {"ProjectionType": "ALL"}}],
        )
        yield
