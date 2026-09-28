"""The analyst's task credential process must not become Browser identity."""

import os
import sys
from pathlib import Path

import pytest
from botocore.config import Config

from isolated_browser import ProcessActor, stop_session
from native_identity import browser_environment, cleanup_client


@pytest.fixture
def task_identity(monkeypatch, tmp_path):
    config = tmp_path / "task.config"
    config.write_text(
        "[default]\ncredential_process = missing-task-credential-process\n"
    )
    values = {
        "ADP_AGENT_AUTHORITY_ENABLED": "true",
        "ADP_WORKER_IRSA_ROLE_ARN": "arn:aws:iam::123456789012:role/protected-worker",
        "ADP_WORKER_IRSA_TOKEN_FILE": str(tmp_path / "projected-token"),
        "ADP_WORKER_AWS_REGION": "us-east-1",
        "AWS_CONFIG_FILE": str(config),
        "AWS_ACCESS_KEY_ID": "customer-fixture-key",
        "AWS_SECRET_ACCESS_KEY": "customer-fixture-secret",
        "AWS_SESSION_TOKEN": "customer-fixture-token",
        "AWS_REGION": "eu-west-1",
        "NODE_OPTIONS": "--require /missing-preload.js",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("AWS_ROLE_ARN", raising=False)
    monkeypatch.delenv("AWS_WEB_IDENTITY_TOKEN_FILE", raising=False)
    return values


def test_native_child_uses_platform_identity_for_lifecycle_and_cdp(task_identity):
    # Exercise the real process boundary and both credential chains used by the
    # AgentCore SDK. No AWS request or credential materialization is performed.
    script = """import sys,json,os,boto3
from bedrock_agentcore.tools.browser_client import BrowserClient
sys.stdin.readline()
client=BrowserClient(region=os.environ['AWS_REGION'])
result={
 'lifecycle':client.data_plane_client._request_signer._credentials.method,
 'cdp':boto3.Session().get_credentials().method,
 'region':client.region,
 'preload':os.environ.get('NODE_OPTIONS'),
}
print(json.dumps({'event':'result','id':'start','result':result}),flush=True)
sys.stdin.readline()
"""
    actor = ProcessActor(
        {}, None, lambda _: None, command=[sys.executable, "-c", script, "--native"]
    )
    try:
        assert actor.initial() == {
            "lifecycle": "assume-role-with-web-identity",
            "cdp": "assume-role-with-web-identity",
            "region": "us-east-1",
            "preload": None,
        }
        assert all(os.environ[key] == value for key, value in task_identity.items())
    finally:
        actor.abort()


def test_cleanup_uses_refreshable_platform_identity(task_identity, monkeypatch):
    worker = (
        Path(__file__).resolve().parents[6] / "agent-factory" / "agent-worker-image"
    )
    monkeypatch.syspath_prepend(str(worker))
    client = cleanup_client(Config(connect_timeout=2))
    assert client._request_signer._credentials.method == "assume-role-with-web-identity"
    assert client.meta.region_name == "us-east-1"
    assert all(os.environ[key] == value for key, value in task_identity.items())


@pytest.mark.parametrize(
    "missing", ["ADP_WORKER_IRSA_ROLE_ARN", "ADP_WORKER_IRSA_TOKEN_FILE"]
)
def test_missing_platform_identity_never_falls_back_to_customer(
    task_identity, monkeypatch, missing
):
    monkeypatch.delenv(missing)
    with pytest.raises(RuntimeError, match="IRSA identity unavailable"):
        browser_environment(os.environ)


def test_native_emergency_stop_uses_native_client(monkeypatch):
    from unittest.mock import Mock

    client = Mock()
    client.get_browser_session.return_value = {"status": "TERMINATED"}
    monkeypatch.setattr("native_identity.cleanup_client", lambda _: client)
    assert stop_session("fixture-session", native=True)
    client.stop_browser_session.assert_called_once_with(
        browserIdentifier="aws.browser.v1", sessionId="fixture-session"
    )
