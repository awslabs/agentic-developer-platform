"""
Shared pytest configuration and fixtures for agent-factory E2E tests.

Provides:
- test_env: resolved TestEnvConfig (unit vs live)
- mock_apigw_event: synthesises API Gateway WebSocket events
- jwt_for_user / jwt_for_agent: Cognito auth helpers
- ws_client: WebSocket context manager (mock in unit, real in live)
- sqs_client: SQS client (moto in unit, real boto3 in live)
- kube_client: Kubernetes client (mock in unit, kubectl wrapper in live)
- mock_dynamodb / mock_sqs / mock_bedrock: moto/botocore stubs
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Generator
from unittest.mock import MagicMock

import boto3
import pytest

from .config import MODULE_ROOT, TestEnvConfig, load_config

# ---------------------------------------------------------------------------
# Config fixture
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def test_env() -> TestEnvConfig:
    """Resolved test environment configuration."""
    return load_config()


# ---------------------------------------------------------------------------
# Pytest markers -- auto-skip live_only in unit mode
# ---------------------------------------------------------------------------


def pytest_collection_modifyitems(config, items):
    """Auto-skip live_only and workflow tests when not in live mode."""
    env_mode = os.environ.get("TEST_ENV", "unit").lower()
    is_live = env_mode not in ("unit", "")

    skip_live = pytest.mark.skip(reason="TEST_ENV is not set to a live environment")

    for item in items:
        if "live_only" in item.keywords and not is_live:
            item.add_marker(skip_live)
        if "live" in item.keywords and not is_live:
            item.add_marker(skip_live)
        if "workflow" in item.keywords and not is_live:
            item.add_marker(skip_live)
        if "kubectl" in item.keywords and not is_live:
            item.add_marker(skip_live)


# ---------------------------------------------------------------------------
# API Gateway WebSocket event factory
# ---------------------------------------------------------------------------


def mock_apigw_event(
    route_key: str = "$default",
    body: dict | str | None = None,
    connection_id: str = "test-conn-abc123",
    token: str | None = None,
    authorizer_claims: dict | None = None,
) -> dict:
    """Synthesise an API Gateway WebSocket event for unit testing the handlers.

    Args:
        route_key: "$connect", "$disconnect", "$default", "sendMessage", etc.
        body: Message body (dict auto-serialized to JSON string).
        connection_id: WebSocket connection ID.
        token: JWT token (placed in queryStringParameters).
        authorizer_claims: Cognito authorizer claims to inject.
    """
    event: dict[str, Any] = {
        "requestContext": {
            "routeKey": route_key,
            "connectionId": connection_id,
            "eventType": "MESSAGE" if route_key not in ("$connect", "$disconnect") else route_key.upper().strip("$"),
            "connectedAt": int(time.time() * 1000),
            "identity": {"sourceIp": "127.0.0.1"},
        },
        "isBase64Encoded": False,
    }

    if authorizer_claims:
        event["requestContext"]["authorizer"] = {"claims": authorizer_claims}

    if token:
        event["queryStringParameters"] = {"token": token}

    if body is not None:
        event["body"] = json.dumps(body) if isinstance(body, dict) else body

    return event


@pytest.fixture
def make_apigw_event():
    """Factory fixture that returns the mock_apigw_event function."""
    return mock_apigw_event


# ---------------------------------------------------------------------------
# JWT helpers
# ---------------------------------------------------------------------------


def _make_fake_jwt(claims: dict, expired: bool = False) -> str:
    """Create a fake JWT for unit tests (not cryptographically valid)."""
    import base64

    header = base64.urlsafe_b64encode(json.dumps({"alg": "RS256", "typ": "JWT"}).encode()).decode().rstrip("=")
    payload_claims = {
        "sub": claims.get("sub", str(uuid.uuid4())),
        "email": claims.get("email", "test@example.com"),
        "cognito:username": claims.get("cognito:username", "testuser"),
        "token_use": claims.get("token_use", "access"),
        "iat": int(time.time()) - 60,
        "exp": int(time.time()) - 3600 if expired else int(time.time()) + 3600,
        "iss": claims.get("iss", "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_FAKE"),
        **claims,
    }
    payload = base64.urlsafe_b64encode(json.dumps(payload_claims).encode()).decode().rstrip("=")
    sig = base64.urlsafe_b64encode(b"fakesig").decode().rstrip("=")
    return f"{header}.{payload}.{sig}"


@pytest.fixture
def jwt_for_user():
    """Return a function that creates a user JWT.

    In live mode: uses Cognito admin-initiate-auth.
    In unit mode: returns a locally-signed fixture JWT.
    """
    mode = os.environ.get("TEST_ENV", "unit").lower()

    def _mint(email: str = "test@example.com") -> str:
        if mode not in ("unit", ""):
            # Live mode: real Cognito auth
            client = boto3.client("cognito-idp", region_name="us-east-1")
            user_pool_id = os.environ.get("COGNITO_USER_POOL_ID", "")
            client_id = os.environ.get("COGNITO_CLIENT_ID", "")
            password = os.environ.get("TEST_USER_PASSWORD", "")
            resp = client.admin_initiate_auth(
                UserPoolId=user_pool_id,
                ClientId=client_id,
                AuthFlow="ADMIN_USER_PASSWORD_AUTH",
                AuthParameters={"USERNAME": email, "PASSWORD": password},
            )
            return resp["AuthenticationResult"]["AccessToken"]
        return _make_fake_jwt({"email": email, "token_use": "access"})

    return _mint


@pytest.fixture
def jwt_for_agent():
    """Return a function that creates an agent JWT (client_credentials flow).

    In live mode: uses Cognito OAuth2 token endpoint.
    In unit mode: returns a fixture JWT with client_credentials grant type.
    """
    mode = os.environ.get("TEST_ENV", "unit").lower()

    def _mint() -> str:
        if mode not in ("unit", ""):
            import urllib.request

            client_id = os.environ.get("COGNITO_AGENT_CLIENT_ID", "")
            user_pool_id = os.environ.get("COGNITO_USER_POOL_ID", "")
            # Derive the Cognito domain from the user pool
            domain = f"https://adp-dev.auth.us-east-1.amazoncognito.com"
            data = f"grant_type=client_credentials&client_id={client_id}".encode()
            req = urllib.request.Request(
                f"{domain}/oauth2/token",
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            resp = urllib.request.urlopen(req, timeout=10)
            return json.loads(resp.read())["access_token"]
        return _make_fake_jwt({"token_use": "access", "grant_type": "client_credentials"})

    return _mint


@pytest.fixture
def expired_jwt() -> str:
    """Return an expired JWT for auth failure tests."""
    return _make_fake_jwt({"email": "expired@example.com"}, expired=True)


# ---------------------------------------------------------------------------
# WebSocket client
# ---------------------------------------------------------------------------


@dataclass
class MockWSClient:
    """Mock WebSocket client for unit tests."""

    url: str = "wss://mock.execute-api.us-east-1.amazonaws.com/v1"
    connected: bool = False
    sent_messages: list = field(default_factory=list)
    _responses: list = field(default_factory=list)

    def add_response(self, data: dict):
        self._responses.append(json.dumps(data))

    async def connect(self):
        self.connected = True

    async def close(self):
        self.connected = False

    async def send(self, data: str):
        self.sent_messages.append(data)

    async def recv(self) -> str:
        if self._responses:
            return self._responses.pop(0)
        return json.dumps({"type": "response", "content": "mock response", "task_id": "mock-task"})


@contextmanager
def _mock_ws_context(url: str = "", token: str = "") -> Generator[MockWSClient, None, None]:
    client = MockWSClient(url=url)
    client.connected = True
    yield client
    client.connected = False


@pytest.fixture
def ws_client(test_env: TestEnvConfig):
    """WebSocket client context manager.

    In unit mode: returns a mock event dispatcher.
    In live mode: returns a real websockets.connect wrapper.
    """
    if test_env.is_unit:
        return _mock_ws_context

    def _live_ws(url: str = "", token: str = ""):
        import websockets

        ws_url = url or test_env.live.ws_url
        if token:
            ws_url = f"{ws_url}?token={token}"
        return websockets.connect(ws_url)

    return _live_ws


# ---------------------------------------------------------------------------
# SQS client (moto in unit, real boto3 in live)
# ---------------------------------------------------------------------------


@pytest.fixture
def sqs_client(test_env: TestEnvConfig):
    """SQS client: moto-backed in unit mode, real boto3 in live mode."""
    if test_env.is_unit:
        from moto import mock_aws

        with mock_aws():
            client = boto3.client("sqs", region_name="us-east-1")
            # Create the queues that the code expects
            input_q = client.create_queue(QueueName="adp-dev-agent-gateway-tasks")
            response_q = client.create_queue(QueueName="adp-dev-agent-gateway-responses")
            yield {
                "client": client,
                "input_queue_url": input_q["QueueUrl"],
                "response_queue_url": response_q["QueueUrl"],
            }
    else:
        yield {
            "client": boto3.client("sqs", region_name="us-east-1"),
            "input_queue_url": test_env.live.tasks_queue_url,
            "response_queue_url": test_env.live.responses_queue_url,
        }


# ---------------------------------------------------------------------------
# DynamoDB (moto in unit)
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_dynamodb():
    """Moto-backed DynamoDB for unit tests."""
    from moto import mock_aws

    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        table = ddb.create_table(
            TableName="adp-dev-agent-gateway-sessions",
            KeySchema=[{"AttributeName": "session_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "session_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield {"resource": ddb, "table": table, "table_name": "adp-dev-agent-gateway-sessions"}


# ---------------------------------------------------------------------------
# Bedrock mock
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_bedrock():
    """Mock Bedrock client that returns canned classifier responses."""

    def _make_response(classification: dict) -> dict:
        body_bytes = json.dumps({
            "content": [{"type": "text", "text": json.dumps(classification)}],
            "usage": {"input_tokens": 100, "output_tokens": 50},
        }).encode()
        import io
        return {"body": io.BytesIO(body_bytes)}

    mock_client = MagicMock()
    mock_client.invoke_model.return_value = _make_response({
        "path": "direct_response",
        "persona": "developer",
        "response": "Hello! How can I help?",
        "thread_action": "none",
        "reasoning": "Simple greeting",
    })
    return mock_client


# ---------------------------------------------------------------------------
# Kubernetes client
# ---------------------------------------------------------------------------


@dataclass
class MockKubeClient:
    """Minimal mock Kubernetes client for unit tests."""

    namespace: str = "adp-gateway-agents"
    _scaledjobs: dict = field(default_factory=dict)
    _pods: dict = field(default_factory=dict)
    _service_accounts: dict = field(default_factory=dict)

    def __post_init__(self):
        # Pre-populate with expected resources
        self._scaledjobs["agent-gateway-worker"] = {
            "metadata": {"name": "agent-gateway-worker", "namespace": self.namespace},
            "spec": {
                "triggers": [{
                    "type": "aws-sqs-queue",
                    "metadata": {
                        "queueURL": "https://sqs.us-east-1.amazonaws.com/123456789012/adp-dev-agent-gateway-tasks",
                        "identityOwner": "operator",
                    },
                }],
                "jobTargetRef": {"activeDeadlineSeconds": 900},
            },
        }
        self._service_accounts["adp-agent"] = {
            "metadata": {
                "name": "adp-agent",
                "namespace": self.namespace,
                "annotations": {
                    "eks.amazonaws.com/role-arn": "arn:aws:iam::123456789012:role/adp-dev-gateway-agent-role",
                },
            },
        }

    def get_scaledjob(self, name: str) -> dict | None:
        return self._scaledjobs.get(name)

    def list_pods(self) -> list[dict]:
        return list(self._pods.values())

    def get_service_account(self, name: str) -> dict | None:
        return self._service_accounts.get(name)


class LiveKubeClient:
    """Thin wrapper around kubectl for live tests."""

    def __init__(self, namespace: str, context: str = ""):
        self.namespace = namespace
        self.context = context

    def _run(self, args: list[str]) -> dict | list | str:
        cmd = ["kubectl"]
        if self.context:
            cmd += ["--context", self.context]
        cmd += ["-n", self.namespace] + args + ["-o", "json"]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            return {}
        return json.loads(result.stdout)

    def get_scaledjob(self, name: str) -> dict | None:
        data = self._run(["get", "scaledjob", name])
        return data if data else None

    def list_pods(self) -> list[dict]:
        data = self._run(["get", "pods"])
        return data.get("items", []) if isinstance(data, dict) else []

    def get_service_account(self, name: str) -> dict | None:
        data = self._run(["get", "serviceaccount", name])
        return data if data else None


@pytest.fixture(scope="session")
def kube_client(test_env: TestEnvConfig):
    """Kubernetes client: mocked in unit mode, real kubectl wrapper in live mode."""
    if test_env.is_unit:
        return MockKubeClient(namespace="adp-gateway-agents")
    return LiveKubeClient(
        namespace="adp-gateway-agents",
        context=test_env.live.kube_context,
    )
