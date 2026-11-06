"""Slack must authenticate requests even when its signing secret is absent."""

import hashlib
import hmac
import importlib.util
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def ingest(monkeypatch):
    directory = Path(__file__).parents[2] / "gateway" / "lambdas" / "ingest"
    monkeypatch.syspath_prepend(str(directory))
    for key, value in {
        "INPUT_QUEUE_URL": "https://sqs.us-east-1.amazonaws.com/123/test-input",
        "RESPONSE_QUEUE_URL": "https://sqs.us-east-1.amazonaws.com/123/test-response",
        "SESSIONS_TABLE_NAME": "test-sessions",
        "AWS_REGION_NAME": "us-east-1",
        "WEBHOOK_EVENTS_TABLE": "test-events",
        "SLACK_SIGNING_SECRET": "",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr("boto3.client", MagicMock())
    monkeypatch.setattr("boto3.resource", MagicMock())
    spec = importlib.util.spec_from_file_location(
        "slack_ingest_auth_test", directory / "handler.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ENABLE_USER_IDENTITIES", False)
    monkeypatch.setattr(
        module, "handle_unified_message", MagicMock(return_value={"statusCode": 200})
    )
    monkeypatch.setattr("channels.slack.time.time", lambda: 1000)
    return module


def slack_event(secret, *, challenge=False, timestamp=1000):
    payload = (
        {"type": "url_verification", "challenge": "test-challenge"}
        if challenge
        else {
            "type": "event_callback",
            "team_id": "T123",
            "event": {
                "type": "app_mention",
                "user": "U123",
                "channel": "C123",
                "text": "hello",
                "ts": "1000.1",
            },
        }
    )
    body = json.dumps(payload)
    signature = hmac.new(
        secret.encode(), f"v0:{timestamp}:{body}".encode(), hashlib.sha256
    ).hexdigest()
    return {
        "headers": {
            "x-slack-request-timestamp": str(timestamp),
            "x-slack-signature": "v0=" + signature,
        },
        "body": body,
    }


@pytest.mark.parametrize("secret", ["", "   "])
@pytest.mark.parametrize("challenge", [False, True])
def test_unconfigured_slack_rejects_attacker_computable_signature(ingest, secret, challenge):
    ingest.ADAPTERS["slack"] = ingest.SlackAdapter(secret)
    assert (
        ingest.lambda_handler(slack_event(secret, challenge=challenge), None)["statusCode"] == 401
    )
    ingest.handle_unified_message.assert_not_called()


@pytest.mark.parametrize(
    "event",
    [slack_event(""), slack_event("wrong-secret"), slack_event("test-secret", timestamp=699)],
)
def test_configured_slack_rejects_wrong_key_or_expired_request(ingest, event):
    ingest.ADAPTERS["slack"] = ingest.SlackAdapter("test-secret")
    assert ingest.lambda_handler(event, None)["statusCode"] == 401
    ingest.handle_unified_message.assert_not_called()


def test_configured_slack_still_accepts_signed_message(ingest):
    ingest.ADAPTERS["slack"] = ingest.SlackAdapter("test-secret")
    assert ingest.lambda_handler(slack_event("test-secret"), None)["statusCode"] == 200
    message = ingest.handle_unified_message.call_args.args[0]
    assert message.text == "hello"
    assert message.provider_user_id == "T123:U123"


def test_configured_slack_still_accepts_signed_url_verification(ingest):
    ingest.ADAPTERS["slack"] = ingest.SlackAdapter("test-secret")
    response = ingest.lambda_handler(slack_event("test-secret", challenge=True), None)
    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == {"challenge": "test-challenge"}
    ingest.handle_unified_message.assert_not_called()
