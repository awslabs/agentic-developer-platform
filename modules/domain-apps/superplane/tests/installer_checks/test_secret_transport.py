"""Credentials survive the SDK pipe transport without entering process output."""

import io
import json
import sys

import boto3
from botocore.stub import Stubber

from installation.database_preparation import create_secret_from_stdin
from installation.runner import Commands


def test_sdk_preserves_secret_and_only_outputs_identifiers(monkeypatch, capsys):
    value = 'private-value-with-quote"-and-newline\n'
    payload = {"Name": "adp/dev/superplane/database", "SecretString": value}
    client = boto3.client(
        "secretsmanager",
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )
    response = {
        "ARN": "arn:aws:secretsmanager:us-east-1:879318057152:secret:test-123456",
        "Name": payload["Name"],
        "VersionId": "a" * 32,
    }
    with Stubber(client) as stub:
        stub.add_response("create_secret", response, payload)
        monkeypatch.setattr(boto3, "client", lambda *a, **kw: client)
        monkeypatch.setattr(
            sys,
            "stdin",
            io.StringIO(json.dumps({"region": "us-east-1", "payload": payload})),
        )
        create_secret_from_stdin()
        stub.assert_no_pending_responses()
    output = capsys.readouterr().out
    assert json.loads(output) == response
    assert "private-value" not in output


def test_child_receives_payload_only_through_stdin(monkeypatch):
    monkeypatch.setenv("SUPERPLANE_DATABASE_ADMIN_URL", "private-admin")
    monkeypatch.setenv("SUPERPLANE_VERIFICATION_TOKEN", "private-access")
    program = (
        "import os,sys,json; value=json.load(sys.stdin); "
        "assert value['secret']=='private-payload'; "
        "assert 'SUPERPLANE_DATABASE_ADMIN_URL' not in os.environ; "
        "assert 'SUPERPLANE_VERIFICATION_TOKEN' not in os.environ; print('ok')"
    )
    result = Commands().call(
        [sys.executable, "-c", program], data=json.dumps({"secret": "private-payload"})
    )
    assert result.stdout.strip() == "ok"
