"""
Unit tests for Vault Phase 5 (#138): user identity resolution.

Tests:
- Resolver HTTP call happy path (200 → ResolvedUser)
- Resolver 404 → UnresolvedUser with magic_link_url
- Cache TTL behavior (hit, miss after expiry)
- Feature flag off → resolver bypassed
- Slack adapter provider identity extraction
- WebChat adapter provider identity extraction
- Handler integration: resolved user injects identity
- Handler integration: unresolved user returns magic-link, no enqueue
"""

from __future__ import annotations

import io
import json
import os
import sys
import time
from unittest.mock import MagicMock, patch

import boto3
import pytest
from moto import mock_aws

from tests.conftest import mock_apigw_event

# ---------------------------------------------------------------------------
# Helpers to import the handler with mocked env / boto3
# ---------------------------------------------------------------------------

HANDLER_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "gateway", "lambdas", "ingest"
)


@pytest.fixture(autouse=True)
def _patch_sys_path():
    """Add the ingest Lambda directory to sys.path so handler.py can be imported."""
    original = sys.path.copy()
    sys.path.insert(0, HANDLER_DIR)
    yield
    sys.path = original


@pytest.fixture
def mock_env(monkeypatch):
    """Set required environment variables for the ingest handler."""
    monkeypatch.setenv("INPUT_QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/adp-dev-agent-gateway-tasks")
    monkeypatch.setenv("RESPONSE_QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/adp-dev-agent-gateway-responses.fifo")
    monkeypatch.setenv("SESSIONS_TABLE_NAME", "adp-dev-agent-gateway-sessions")
    monkeypatch.setenv("AWS_REGION_NAME", "us-east-1")
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "")
    monkeypatch.setenv("SLACK_BOT_USER_ID", "")


@pytest.fixture
def mock_env_with_resolver(monkeypatch, mock_env):
    """Environment with user identity resolution enabled."""
    monkeypatch.setenv("ENABLE_USER_IDENTITIES", "true")
    monkeypatch.setenv("RESOLVER_BASE_URL", "http://gateway.internal:8080")
    monkeypatch.setenv("BG_INTERNAL_API_KEY", "test-secret-key")


def _make_bedrock_response(classification: dict) -> dict:
    """Create a mock Bedrock invoke_model return value."""
    body_bytes = json.dumps({
        "content": [{"type": "text", "text": json.dumps(classification)}],
        "usage": {"input_tokens": 100, "output_tokens": 50},
    }).encode()
    return {"body": io.BytesIO(body_bytes)}


@pytest.fixture
def mocked_aws_services(mock_env):
    """Spin up moto DynamoDB + SQS, patch boto3 clients used by the handler."""
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        table = ddb.create_table(
            TableName="adp-dev-agent-gateway-sessions",
            KeySchema=[{"AttributeName": "session_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "session_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )

        sqs_client = boto3.client("sqs", region_name="us-east-1")
        sqs_client.create_queue(QueueName="adp-dev-agent-gateway-tasks")
        sqs_client.create_queue(
            QueueName="adp-dev-agent-gateway-responses.fifo",
            Attributes={"FifoQueue": "true"},
        )

        yield {"ddb": ddb, "table": table, "sqs": sqs_client}


def _import_fresh(mock_bedrock=None):
    """Import the handler module fresh (after env/path setup)."""
    for mod_name in list(sys.modules.keys()):
        if mod_name in ("handler", "classifier", "channels", "channels.base",
                        "channels.webchat", "channels.slack", "github_dispatch",
                        "user_resolver"):
            del sys.modules[mod_name]

    import handler

    if mock_bedrock is not None:
        import classifier
        classifier._bedrock_client = mock_bedrock

    return handler


# ===========================================================================
# Tests: user_resolver module standalone
# ===========================================================================


class TestResolverModule:
    """Unit tests for user_resolver.py in isolation."""

    def test_resolve_user_success(self, mock_env_with_resolver):
        """200 response returns ResolvedUser."""
        _import_fresh()  # ensure env vars are loaded
        import user_resolver
        user_resolver.cache_clear()
        # Force the feature flag on (env was set before import)
        user_resolver.ENABLE_USER_IDENTITIES = True

        mock_response = json.dumps({
            "user_id": "usr-123",
            "org_id": "org-abc",
            "team_id": "team-xyz",
            "is_shadow": False,
        }).encode()

        with patch("user_resolver.urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.read.return_value = mock_response
            mock_resp.__enter__ = MagicMock(return_value=mock_resp)
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = mock_resp

            result = user_resolver.resolve_user("slack", "T01ABC:U987")

        assert isinstance(result, user_resolver.ResolvedUser)
        assert result.user_id == "usr-123"
        assert result.org_id == "org-abc"
        assert result.team_id == "team-xyz"
        assert result.is_shadow is False

    def test_resolve_user_404_returns_unresolved(self, mock_env_with_resolver):
        """404 response returns UnresolvedUser with magic_link_url."""
        _import_fresh()
        import user_resolver
        user_resolver.cache_clear()
        user_resolver.ENABLE_USER_IDENTITIES = True

        import urllib.error
        error_body = json.dumps({"magic_link_url": "https://gw.example.com/auth/link/magic?token=abc"}).encode()
        http_error = urllib.error.HTTPError(
            url="http://gateway.internal:8080/internal/v1/resolve-user",
            code=404,
            msg="Not Found",
            hdrs={},
            fp=io.BytesIO(error_body),
        )

        with patch("user_resolver.urllib.request.urlopen", side_effect=http_error):
            result = user_resolver.resolve_user("slack", "T01ABC:U999")

        assert isinstance(result, user_resolver.UnresolvedUser)
        assert "magic?token=abc" in result.magic_link_url

    def test_cache_hit_avoids_http_call(self, mock_env_with_resolver):
        """Second call for same provider identity uses cache, no HTTP."""
        _import_fresh()
        import user_resolver
        user_resolver.cache_clear()
        user_resolver.ENABLE_USER_IDENTITIES = True

        mock_response = json.dumps({
            "user_id": "usr-cached",
            "org_id": "org-c",
            "team_id": "team-c",
            "is_shadow": False,
        }).encode()

        with patch("user_resolver.urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.read.return_value = mock_response
            mock_resp.__enter__ = MagicMock(return_value=mock_resp)
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = mock_resp

            # First call — HTTP
            user_resolver.resolve_user("slack", "T01:U111")
            # Second call — cache
            result2 = user_resolver.resolve_user("slack", "T01:U111")

        assert mock_urlopen.call_count == 1
        assert isinstance(result2, user_resolver.ResolvedUser)
        assert result2.user_id == "usr-cached"

    def test_cache_expires_after_ttl(self, mock_env_with_resolver):
        """Cache entries expire after TTL, triggering a new HTTP call."""
        _import_fresh()
        import user_resolver
        user_resolver.cache_clear()
        user_resolver.ENABLE_USER_IDENTITIES = True

        mock_response = json.dumps({
            "user_id": "usr-ttl",
            "org_id": "org-t",
            "team_id": "team-t",
            "is_shadow": False,
        }).encode()

        with patch("user_resolver.urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.read.return_value = mock_response
            mock_resp.__enter__ = MagicMock(return_value=mock_resp)
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = mock_resp

            # First call
            user_resolver.resolve_user("slack", "T01:U222")

            # Manually expire the cache entry
            key = ("slack", "T01:U222")
            entry = user_resolver._cache[key]
            user_resolver._cache[key] = (entry[0], time.time() - 1)

            # Second call — should hit HTTP again
            user_resolver.resolve_user("slack", "T01:U222")

        assert mock_urlopen.call_count == 2

    def test_feature_flag_off_returns_none(self, mock_env):
        """When ENABLE_USER_IDENTITIES is not set, resolve returns None."""
        _import_fresh()
        import user_resolver
        user_resolver.cache_clear()
        user_resolver.ENABLE_USER_IDENTITIES = False

        result = user_resolver.resolve_user("slack", "T01:U333")
        assert result is None

    def test_missing_base_url_returns_none(self, mock_env_with_resolver):
        """When RESOLVER_BASE_URL is empty, resolve returns None."""
        _import_fresh()
        import user_resolver
        user_resolver.cache_clear()
        user_resolver.ENABLE_USER_IDENTITIES = True
        user_resolver.RESOLVER_BASE_URL = ""

        result = user_resolver.resolve_user("slack", "T01:U444")
        assert result is None


# ===========================================================================
# Tests: Slack adapter identity extraction
# ===========================================================================


class TestSlackProviderIdentity:
    """Slack adapter sets provider + provider_user_id on UnifiedMessage."""

    def test_message_event_sets_provider(self, mock_env):
        _import_fresh()
        from channels.slack import SlackAdapter

        adapter = SlackAdapter(signing_secret="", bot_user_id="BOT")
        payload = {
            "type": "event_callback",
            "team_id": "T01WORKSPACE",
            "event_id": "Ev01",
            "event": {
                "type": "message",
                "user": "U_SENDER",
                "text": "hello",
                "ts": "1234567890.123456",
                "channel": "C01",
                "channel_type": "im",
            },
        }
        msg = adapter.parse_event(payload)
        assert msg is not None
        assert msg.provider == "slack"
        assert msg.provider_user_id == "T01WORKSPACE:U_SENDER"

    def test_mention_event_sets_provider(self, mock_env):
        _import_fresh()
        from channels.slack import SlackAdapter

        adapter = SlackAdapter(signing_secret="", bot_user_id="BOT")
        payload = {
            "type": "event_callback",
            "team_id": "T02",
            "event_id": "Ev02",
            "event": {
                "type": "app_mention",
                "user": "U_MENTIONER",
                "text": "<@BOT> do something",
                "ts": "1234567890.111",
                "channel": "C02",
            },
        }
        msg = adapter.parse_event(payload)
        assert msg is not None
        assert msg.provider == "slack"
        assert msg.provider_user_id == "T02:U_MENTIONER"


# ===========================================================================
# Tests: WebChat adapter identity extraction
# ===========================================================================


class TestWebChatProviderIdentity:
    """WebChat adapter sets provider=cognito."""

    def test_webchat_sets_cognito_provider(self, mock_env):
        _import_fresh()
        from channels.webchat import WebChatAdapter

        adapter = WebChatAdapter()
        event = {
            "requestContext": {
                "routeKey": "$default",
                "connectionId": "conn-123",
                "authorizer": {"claims": {"sub": "cognito-sub-abc", "email": "a@b.com"}},
                "identity": {"sourceIp": "127.0.0.1"},
                "connectedAt": 1000,
            },
            "body": json.dumps({"action": "message", "text": "hi", "session_id": "s1"}),
        }
        msg = adapter.parse_event(event)
        assert msg is not None
        assert msg.provider == "cognito"
        assert msg.provider_user_id == "cognito-sub-abc"


# ===========================================================================
# Tests: Handler integration with resolver
# ===========================================================================


class TestHandlerResolverIntegration:
    """End-to-end handler tests with resolver enabled."""

    def test_resolved_user_injects_identity(self, mocked_aws_services, mock_env_with_resolver):
        """Resolved user identity is injected into SQS message."""
        mock_bedrock = MagicMock()
        mock_bedrock.invoke_model.return_value = _make_bedrock_response({
            "path": "long_running",
            "persona": "developer",
            "response": None,
            "thread_action": "new",
            "reasoning": "Work needed",
        })

        resolver_response = json.dumps({
            "user_id": "internal-user-42",
            "org_id": "org-resolved",
            "team_id": "team-resolved",
            "is_shadow": False,
        }).encode()

        with patch("user_resolver.urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.read.return_value = resolver_response
            mock_resp.__enter__ = MagicMock(return_value=mock_resp)
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = mock_resp

            handler = _import_fresh(mock_bedrock=mock_bedrock)
            # Force module-level flag on after fresh import
            import user_resolver
            user_resolver.ENABLE_USER_IDENTITIES = True
            user_resolver.RESOLVER_BASE_URL = "http://gateway.internal:8080"
            user_resolver.cache_clear()

            # Simulate a Slack event (non-WebSocket)
            event = {
                "headers": {"x-slack-signature": "v0=fake", "x-slack-request-timestamp": str(int(time.time()))},
                "body": json.dumps({
                    "type": "event_callback",
                    "team_id": "T_TEAM",
                    "event_id": "Ev01",
                    "event": {
                        "type": "message",
                        "user": "U_SLACK_USER",
                        "text": "deploy please",
                        "ts": "1234567890.123",
                        "channel": "C01",
                        "channel_type": "im",
                    },
                }),
            }

            # Bypass slack signature verification for test
            handler.ADAPTERS["slack"].verify_request = lambda *a, **kw: True
            result = handler.lambda_handler(event, None)

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["status"] == "processing"

        # Verify SQS message has resolved identity
        sqs = mocked_aws_services["sqs"]
        resp = sqs.receive_message(
            QueueUrl="https://sqs.us-east-1.amazonaws.com/123/adp-dev-agent-gateway-tasks",
            MaxNumberOfMessages=1,
            WaitTimeSeconds=0,
        )
        messages = resp.get("Messages", [])
        assert len(messages) >= 1
        task = json.loads(messages[0]["Body"])
        assert task["user_id"] == "internal-user-42"
        assert task["org_id"] == "org-resolved"
        assert task["team_id"] == "team-resolved"

    def test_unresolved_user_returns_magic_link_no_enqueue(self, mocked_aws_services, mock_env_with_resolver):
        """Unresolved user gets magic-link response, message is NOT enqueued."""
        import urllib.error

        error_body = json.dumps({"magic_link_url": "https://gw.example.com/auth/link/magic?token=xyz"}).encode()
        http_error = urllib.error.HTTPError(
            url="http://gateway.internal:8080/internal/v1/resolve-user",
            code=404,
            msg="Not Found",
            hdrs={},
            fp=io.BytesIO(error_body),
        )

        with patch("user_resolver.urllib.request.urlopen", side_effect=http_error):
            handler = _import_fresh()
            import user_resolver
            user_resolver.ENABLE_USER_IDENTITIES = True
            user_resolver.RESOLVER_BASE_URL = "http://gateway.internal:8080"
            user_resolver.cache_clear()

            event = {
                "headers": {"x-slack-signature": "v0=fake", "x-slack-request-timestamp": str(int(time.time()))},
                "body": json.dumps({
                    "type": "event_callback",
                    "team_id": "T_TEAM",
                    "event_id": "Ev02",
                    "event": {
                        "type": "message",
                        "user": "U_UNKNOWN",
                        "text": "help me",
                        "ts": "1234567890.456",
                        "channel": "C02",
                        "channel_type": "im",
                    },
                }),
            }

            handler.ADAPTERS["slack"].verify_request = lambda *a, **kw: True
            result = handler.lambda_handler(event, None)

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["status"] == "unresolved_user"
        assert "magic?token=xyz" in body["magic_link_url"]

        # Verify NO message was enqueued
        sqs = mocked_aws_services["sqs"]
        resp = sqs.receive_message(
            QueueUrl="https://sqs.us-east-1.amazonaws.com/123/adp-dev-agent-gateway-tasks",
            MaxNumberOfMessages=1,
            WaitTimeSeconds=0,
        )
        messages = resp.get("Messages", [])
        assert len(messages) == 0

    def test_webchat_skips_resolver(self, mocked_aws_services, mock_env_with_resolver):
        """WebChat (provider=cognito) does NOT call resolver — existing flow works."""
        mock_bedrock = MagicMock()
        mock_bedrock.invoke_model.return_value = _make_bedrock_response({
            "path": "direct_response",
            "persona": "developer",
            "response": "Hello!",
            "thread_action": "none",
            "reasoning": "Greeting",
        })

        with patch("user_resolver.urllib.request.urlopen") as mock_urlopen:
            handler = _import_fresh(mock_bedrock=mock_bedrock)
            import user_resolver
            user_resolver.ENABLE_USER_IDENTITIES = True
            user_resolver.RESOLVER_BASE_URL = "http://gateway.internal:8080"
            user_resolver.cache_clear()

            event = mock_apigw_event(
                route_key="$default",
                body={"action": "message", "text": "Hello!", "session_id": "sess-wc"},
                connection_id="conn-wc",
                authorizer_claims={"sub": "cognito-user-1", "email": "u@e.com"},
            )
            result = handler.lambda_handler(event, None)

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["status"] == "completed"
        # Resolver was NOT called for cognito
        mock_urlopen.assert_not_called()

    def test_feature_flag_off_skips_resolver(self, mocked_aws_services, mock_env):
        """When ENABLE_USER_IDENTITIES is off, Slack messages proceed without resolver."""
        mock_bedrock = MagicMock()
        mock_bedrock.invoke_model.return_value = _make_bedrock_response({
            "path": "long_running",
            "persona": "developer",
            "response": None,
            "thread_action": "new",
            "reasoning": "Work",
        })

        with patch("user_resolver.urllib.request.urlopen") as mock_urlopen:
            handler = _import_fresh(mock_bedrock=mock_bedrock)
            import user_resolver
            user_resolver.ENABLE_USER_IDENTITIES = False
            user_resolver.cache_clear()

            event = {
                "headers": {"x-slack-signature": "v0=fake", "x-slack-request-timestamp": str(int(time.time()))},
                "body": json.dumps({
                    "type": "event_callback",
                    "team_id": "T_TEAM",
                    "event_id": "Ev03",
                    "event": {
                        "type": "message",
                        "user": "U_ANY",
                        "text": "do stuff",
                        "ts": "1234567890.789",
                        "channel": "C03",
                        "channel_type": "im",
                    },
                }),
            }

            handler.ADAPTERS["slack"].verify_request = lambda *a, **kw: True
            result = handler.lambda_handler(event, None)

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["status"] == "processing"
        # Resolver was NOT called
        mock_urlopen.assert_not_called()
