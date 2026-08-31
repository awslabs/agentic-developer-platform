# =============================================================================
# Unit Tests for API Gateway Lambda Authorizer (Issue #239)
# =============================================================================
# Tests JWT validation and IAM-based agent authentication.
# Uses moto for DynamoDB mocking and unittest.mock for JWT mocking.
# =============================================================================

import os
import sys
from unittest import mock

import pytest
from botocore.exceptions import ClientError

# Set environment variables before importing handler
os.environ["COGNITO_USER_POOL_ID"] = "us-east-1_TestPool"
os.environ["COGNITO_REGION"] = "us-east-1"
os.environ["AGENT_REGISTRY_TABLE"] = "test-agent-registry"

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(__file__))

import handler  # noqa: E402
from handler import (  # noqa: E402
    extract_bearer_token,
    generate_policy,
    lambda_handler,
    lookup_agent_in_registry,
    parse_role_arn_from_user_arn,
    source_ip_allowed,
)

# =============================================================================
# Test Fixtures
# =============================================================================


@pytest.fixture
def mock_dynamodb():
    """Mock DynamoDB client with moto."""
    try:
        from moto import mock_aws
    except ImportError:
        pytest.skip("moto not installed")

    with mock_aws():
        import boto3

        # Create the table
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(
            TableName="test-agent-registry",
            KeySchema=[{"AttributeName": "role_arn", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "role_arn", "AttributeType": "S"},
                {"AttributeName": "org_id", "AttributeType": "S"},
                {"AttributeName": "team_id", "AttributeType": "S"},
                {"AttributeName": "owner", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "by-org-team",
                    "KeySchema": [
                        {"AttributeName": "org_id", "KeyType": "HASH"},
                        {"AttributeName": "team_id", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
                {
                    "IndexName": "by-owner",
                    "KeySchema": [{"AttributeName": "owner", "KeyType": "HASH"}],
                    "Projection": {"ProjectionType": "ALL"},
                },
            ],
            BillingMode="PAY_PER_REQUEST",
        )

        # Add test agent
        client.put_item(
            TableName="test-agent-registry",
            Item={
                "role_arn": {"S": "arn:aws:iam::123456789012:role/test-agent"},
                "agent_name": {"S": "test-agent"},
                "org_id": {"S": "default"},
                "team_id": {"S": "platform"},
                "owner": {"S": "system"},
                "scope": {"S": "shared"},
                "budget_config_id": {"S": "budget-123"},
                "allowed_models": {"SS": ["claude-sonnet", "claude-haiku"]},
                "status": {"S": "active"},
            },
        )

        # Add disabled agent
        client.put_item(
            TableName="test-agent-registry",
            Item={
                "role_arn": {"S": "arn:aws:iam::123456789012:role/disabled-agent"},
                "agent_name": {"S": "disabled-agent"},
                "org_id": {"S": "default"},
                "team_id": {"S": "platform"},
                "owner": {"S": "system"},
                "scope": {"S": "shared"},
                "budget_config_id": {"S": ""},
                "allowed_models": {"SS": ["claude-sonnet"]},
                "status": {"S": "disabled"},
            },
        )

        # Reset the global DynamoDB client in the handler module
        import handler

        handler._dynamodb_client = None

        yield client


@pytest.fixture
def valid_jwt_claims():
    """Sample valid JWT claims."""
    return {
        "sub": "user-123",
        "iss": "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_TestPool",
        "exp": 9999999999,
        "iat": 1000000000,
        "custom:org_id": "test-org",
        "custom:team_id": "test-team",
        "custom:account_type": "user",
        "custom:scope": "personal",
        "custom:budget_config_id": "budget-456",
    }


@pytest.fixture
def api_gateway_event():
    """Base API Gateway event."""
    return {
        "type": "TOKEN",
        "methodArn": "arn:aws:execute-api:us-east-1:123456789012:abc123/dev/GET/test",
        "headers": {},
        "requestContext": {
            "identity": {},
        },
    }


# =============================================================================
# Test: extract_bearer_token
# =============================================================================


def test_extract_bearer_token_valid():
    """Test extracting valid Bearer token."""
    token = extract_bearer_token("Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.test")
    assert token == "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.test"  # nosec B105


def test_extract_bearer_token_case_insensitive():
    """Test Bearer token extraction is case-insensitive."""
    token = extract_bearer_token("bearer eyJtest")
    assert token == "eyJtest"  # nosec B105


def test_extract_bearer_token_none():
    """Test extracting from None header."""
    assert extract_bearer_token(None) is None


def test_extract_bearer_token_empty():
    """Test extracting from empty header."""
    assert extract_bearer_token("") is None


def test_extract_bearer_token_malformed():
    """Test extracting from malformed header."""
    assert extract_bearer_token("Basic abc123") is None
    assert extract_bearer_token("Bearer") is None
    assert extract_bearer_token("Bearer token extra") is None


# =============================================================================
# Test: parse_role_arn_from_user_arn
# =============================================================================


def test_parse_role_arn_assumed_role():
    """Test parsing role ARN from assumed-role userArn."""
    user_arn = "arn:aws:sts::123456789012:assumed-role/my-role/session-name"
    role_arn = parse_role_arn_from_user_arn(user_arn)
    assert role_arn == "arn:aws:iam::123456789012:role/my-role"


def test_parse_role_arn_direct_role():
    """Test parsing direct role ARN."""
    user_arn = "arn:aws:iam::123456789012:role/my-role"
    role_arn = parse_role_arn_from_user_arn(user_arn)
    assert role_arn == "arn:aws:iam::123456789012:role/my-role"


def test_parse_role_arn_with_path():
    """Test parsing role ARN with path."""
    user_arn = "arn:aws:sts::123456789012:assumed-role/path/to/my-role/session"
    role_arn = parse_role_arn_from_user_arn(user_arn)
    # The regex only captures the first path component after assumed-role/
    assert role_arn == "arn:aws:iam::123456789012:role/path"


def test_parse_role_arn_empty():
    """Test parsing empty userArn."""
    assert parse_role_arn_from_user_arn("") is None
    assert parse_role_arn_from_user_arn(None) is None


def test_parse_role_arn_invalid():
    """Test parsing invalid userArn."""
    assert parse_role_arn_from_user_arn("not-an-arn") is None
    assert parse_role_arn_from_user_arn("arn:aws:s3:::my-bucket") is None


# =============================================================================
# Test: generate_policy
# =============================================================================


def test_generate_policy_allow():
    """Test generating allow policy."""
    policy = generate_policy(
        principal_id="user-123",
        effect="Allow",
        resource="arn:aws:execute-api:us-east-1:123456789012:abc/*/GET/*",
    )
    assert policy["principalId"] == "user-123"
    assert policy["policyDocument"]["Statement"][0]["Effect"] == "Allow"
    assert "context" not in policy


def test_generate_policy_deny():
    """Test generating deny policy."""
    policy = generate_policy(
        principal_id="unauthorized",
        effect="Deny",
        resource="*",
    )
    assert policy["principalId"] == "unauthorized"
    assert policy["policyDocument"]["Statement"][0]["Effect"] == "Deny"


def test_generate_policy_with_context():
    """Test generating policy with context."""
    context = {"X-Auth-Source": "jwt", "X-Agent-Id": "user-123"}
    policy = generate_policy(
        principal_id="user-123",
        effect="Allow",
        resource="*",
        context=context,
    )
    assert policy["context"] == context


# =============================================================================
# Test: lookup_agent_in_registry
# =============================================================================


def test_lookup_agent_found(mock_dynamodb):
    """Test looking up an existing active agent."""
    agent = lookup_agent_in_registry("arn:aws:iam::123456789012:role/test-agent")
    assert agent is not None
    assert agent["agent_name"] == "test-agent"
    assert agent["org_id"] == "default"
    assert agent["team_id"] == "platform"
    assert agent["owner"] == "system"
    assert agent["scope"] == "shared"
    assert agent["budget_config_id"] == "budget-123"
    assert "claude-sonnet" in agent["allowed_models"]
    assert "claude-haiku" in agent["allowed_models"]


def test_lookup_agent_not_found(mock_dynamodb):
    """Test looking up non-existent agent."""
    agent = lookup_agent_in_registry("arn:aws:iam::123456789012:role/unknown-agent")
    assert agent is None


def test_lookup_agent_disabled(mock_dynamodb):
    """Test looking up disabled agent returns None."""
    agent = lookup_agent_in_registry("arn:aws:iam::123456789012:role/disabled-agent")
    assert agent is None


def test_lookup_agent_no_table():
    """Test lookup when table is not configured."""
    original_table = os.environ.get("AGENT_REGISTRY_TABLE")
    os.environ["AGENT_REGISTRY_TABLE"] = ""
    try:
        import handler

        handler._dynamodb_client = None
        agent = lookup_agent_in_registry("arn:aws:iam::123456789012:role/test-agent")
        assert agent is None
    finally:
        os.environ["AGENT_REGISTRY_TABLE"] = original_table


# =============================================================================
# Test: lambda_handler - JWT Authentication
# =============================================================================


def test_handler_valid_jwt(api_gateway_event, valid_jwt_claims):
    """Test handler with valid JWT token."""
    api_gateway_event["headers"]["Authorization"] = "Bearer valid-token"

    with mock.patch("handler.validate_jwt") as mock_validate:
        mock_validate.return_value = valid_jwt_claims

        result = lambda_handler(api_gateway_event, None)

        assert result["policyDocument"]["Statement"][0]["Effect"] == "Allow"
        assert result["principalId"] == "user-123"
        assert result["context"]["X-Auth-Source"] == "jwt"
        assert result["context"]["X-Agent-Id"] == "user-123"
        assert result["context"]["X-Agent-OrgId"] == "test-org"
        assert result["context"]["X-Agent-TeamId"] == "test-team"
        assert result["context"]["X-Agent-AccountType"] == "user"


def test_handler_expired_jwt(api_gateway_event):
    """Test handler with expired JWT token returns deny."""
    api_gateway_event["headers"]["Authorization"] = "Bearer expired-token"

    with mock.patch("handler.validate_jwt") as mock_validate:
        mock_validate.return_value = None  # Validation failed

        result = lambda_handler(api_gateway_event, None)

        assert result["policyDocument"]["Statement"][0]["Effect"] == "Deny"
        assert result["principalId"] == "unauthorized"


def test_handler_malformed_auth_header(api_gateway_event):
    """Test handler with malformed Authorization header."""
    api_gateway_event["headers"]["Authorization"] = "NotBearer token"
    api_gateway_event["requestContext"]["identity"]["userArn"] = None

    result = lambda_handler(api_gateway_event, None)

    assert result["policyDocument"]["Statement"][0]["Effect"] == "Deny"


# =============================================================================
# Test: lambda_handler - IAM Authentication
# =============================================================================


def test_handler_valid_iam_agent(api_gateway_event, mock_dynamodb):
    """Test handler with valid IAM agent."""
    api_gateway_event["requestContext"]["identity"]["userArn"] = "arn:aws:sts::123456789012:assumed-role/test-agent/session"

    result = lambda_handler(api_gateway_event, None)

    assert result["policyDocument"]["Statement"][0]["Effect"] == "Allow"
    assert result["principalId"] == "test-agent"
    assert result["context"]["X-Auth-Source"] == "iam"
    assert result["context"]["X-Agent-Id"] == "test-agent"
    assert result["context"]["X-Agent-OrgId"] == "default"
    assert result["context"]["X-Agent-TeamId"] == "platform"
    assert result["context"]["X-Agent-AccountType"] == "service"
    assert result["context"]["X-Agent-Scope"] == "shared"
    assert result["context"]["X-Agent-BudgetConfigId"] == "budget-123"
    assert "claude-sonnet" in result["context"]["X-Agent-AllowedModels"]


def test_handler_iam_agent_not_in_registry(api_gateway_event, mock_dynamodb):
    """Test handler with IAM agent not in registry returns deny."""
    api_gateway_event["requestContext"]["identity"]["userArn"] = "arn:aws:sts::123456789012:assumed-role/unknown-agent/session"

    result = lambda_handler(api_gateway_event, None)

    assert result["policyDocument"]["Statement"][0]["Effect"] == "Deny"


def test_handler_iam_agent_disabled(api_gateway_event, mock_dynamodb):
    """Test handler with disabled IAM agent returns deny."""
    api_gateway_event["requestContext"]["identity"]["userArn"] = "arn:aws:sts::123456789012:assumed-role/disabled-agent/session"

    result = lambda_handler(api_gateway_event, None)

    assert result["policyDocument"]["Statement"][0]["Effect"] == "Deny"


def test_handler_no_credentials(api_gateway_event):
    """Test handler with no credentials returns deny."""
    result = lambda_handler(api_gateway_event, None)

    assert result["policyDocument"]["Statement"][0]["Effect"] == "Deny"


def test_handler_unparseable_user_arn(api_gateway_event):
    """Test handler with unparseable userArn returns deny."""
    api_gateway_event["requestContext"]["identity"]["userArn"] = "invalid-arn"

    result = lambda_handler(api_gateway_event, None)

    assert result["policyDocument"]["Statement"][0]["Effect"] == "Deny"


# =============================================================================
# Test: lambda_handler - Edge Cases
# =============================================================================


def test_handler_lowercase_authorization_header(api_gateway_event, valid_jwt_claims):
    """Test handler handles lowercase Authorization header."""
    api_gateway_event["headers"]["authorization"] = "Bearer valid-token"

    with mock.patch("handler.validate_jwt") as mock_validate:
        mock_validate.return_value = valid_jwt_claims

        result = lambda_handler(api_gateway_event, None)

        assert result["policyDocument"]["Statement"][0]["Effect"] == "Allow"


def test_handler_empty_headers(api_gateway_event):
    """Test handler with None headers dict."""
    api_gateway_event["headers"] = None

    result = lambda_handler(api_gateway_event, None)

    # Should fall through to IAM auth, but no userArn = deny
    assert result["policyDocument"]["Statement"][0]["Effect"] == "Deny"


def test_handler_jwt_defaults_for_missing_claims(api_gateway_event):
    """Test handler uses defaults for missing JWT claims."""
    api_gateway_event["headers"]["Authorization"] = "Bearer valid-token"

    minimal_claims = {
        "sub": "user-456",
        "iss": "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_TestPool",
        "exp": 9999999999,
        "iat": 1000000000,
    }

    with mock.patch("handler.validate_jwt") as mock_validate:
        mock_validate.return_value = minimal_claims

        result = lambda_handler(api_gateway_event, None)

        assert result["policyDocument"]["Statement"][0]["Effect"] == "Allow"
        assert result["context"]["X-Agent-OrgId"] == "default"
        assert result["context"]["X-Agent-TeamId"] == ""
        assert result["context"]["X-Agent-AccountType"] == "user"


# =============================================================================
# Test: source_ip_allowed / optional IP allowlist
# =============================================================================


@pytest.fixture
def ip_allowlist(monkeypatch):
    """
    Configure the optional IP allowlist and reset its cache.

    Yields a setter taking the allowlist string as SSM would return it; the
    cache is cleared before and after so tests don't leak state into each other.
    """

    def _configure(value: str | None):
        handler._ip_allowlist_cache = None
        handler._ip_allowlist_cached_at = 0.0
        if value is None:
            monkeypatch.setattr(handler, "IP_ALLOWLIST_SSM_PARAM", "")
            return None
        monkeypatch.setattr(handler, "IP_ALLOWLIST_SSM_PARAM", "/adp/test/ws-ip-allowlist")
        fake_ssm = mock.Mock()
        fake_ssm.get_parameter.return_value = {"Parameter": {"Value": value}}
        monkeypatch.setattr(handler, "get_ssm_client", lambda: fake_ssm)
        return fake_ssm

    yield _configure
    handler._ip_allowlist_cache = None
    handler._ip_allowlist_cached_at = 0.0


def _event_from(event, source_ip):
    event["requestContext"]["identity"]["sourceIp"] = source_ip
    return event


def test_source_ip_allowed_when_not_configured(api_gateway_event, ip_allowlist):
    """No allowlist parameter configured means every source IP is allowed."""
    ip_allowlist(None)
    assert source_ip_allowed(_event_from(api_gateway_event, "203.0.113.9")) is True


def test_source_ip_allowed_in_range(api_gateway_event, ip_allowlist):
    """An address inside a configured CIDR is allowed."""
    ip_allowlist("52.3.162.184/32,10.0.0.0/8")
    assert source_ip_allowed(_event_from(api_gateway_event, "52.3.162.184")) is True


def test_source_ip_denied_out_of_range(api_gateway_event, ip_allowlist):
    """An address outside every configured CIDR is denied."""
    ip_allowlist("52.3.162.184/32")
    assert source_ip_allowed(_event_from(api_gateway_event, "203.0.113.9")) is False


def test_source_ip_denied_when_missing(api_gateway_event, ip_allowlist):
    """A configured allowlist with no sourceIp in the event fails closed."""
    ip_allowlist("52.3.162.184/32")
    assert source_ip_allowed(api_gateway_event) is False


def test_source_ip_allowed_when_ssm_unreadable(api_gateway_event, ip_allowlist, monkeypatch):
    """
    An unreadable parameter is treated as unconfigured, not as deny-all.

    This is the property that makes the change safe to adopt without the
    matching IAM grant: taking the code alone must not break authorization.
    """
    ip_allowlist("52.3.162.184/32")
    failing = mock.Mock()
    failing.get_parameter.side_effect = ClientError({"Error": {"Code": "AccessDeniedException", "Message": "denied"}}, "GetParameter")
    monkeypatch.setattr(handler, "get_ssm_client", lambda: failing)
    handler._ip_allowlist_cache = None
    handler._ip_allowlist_cached_at = 0.0

    assert source_ip_allowed(_event_from(api_gateway_event, "203.0.113.9")) is True


def test_handler_jwt_denied_from_disallowed_ip(api_gateway_event, valid_jwt_claims, ip_allowlist):
    """A valid JWT from outside the allowlist is denied."""
    ip_allowlist("52.3.162.184/32")
    api_gateway_event["headers"]["Authorization"] = "Bearer valid-token"
    _event_from(api_gateway_event, "203.0.113.9")

    with mock.patch("handler.validate_jwt") as mock_validate:
        mock_validate.return_value = valid_jwt_claims
        result = lambda_handler(api_gateway_event, None)

    assert result["policyDocument"]["Statement"][0]["Effect"] == "Deny"


def test_handler_jwt_allowed_from_allowed_ip(api_gateway_event, valid_jwt_claims, ip_allowlist):
    """A valid JWT from inside the allowlist still succeeds."""
    ip_allowlist("52.3.162.184/32")
    api_gateway_event["headers"]["Authorization"] = "Bearer valid-token"
    _event_from(api_gateway_event, "52.3.162.184")

    with mock.patch("handler.validate_jwt") as mock_validate:
        mock_validate.return_value = valid_jwt_claims
        result = lambda_handler(api_gateway_event, None)

    assert result["policyDocument"]["Statement"][0]["Effect"] == "Allow"


def test_handler_iam_path_ignores_ip_allowlist(api_gateway_event, ip_allowlist):
    """
    The IAM branch is never subject to the allowlist.

    Agents and in-cluster callers arrive from a VPC endpoint or NAT address,
    never a corporate egress IP, so a blanket check would break them. This is
    the regression guard for that scoping decision.

    The registry lookup is mocked rather than using the moto fixture so this
    asserts only the scoping property.
    """
    ip_allowlist("52.3.162.184/32")
    api_gateway_event["requestContext"]["identity"]["userArn"] = "arn:aws:sts::123456789012:assumed-role/test-agent-role/session"
    _event_from(api_gateway_event, "10.0.42.7")

    with mock.patch("handler.lookup_agent_in_registry") as mock_lookup:
        mock_lookup.return_value = {
            "agent_id": {"S": "test-agent"},
            "enabled": {"BOOL": True},
            "org_id": {"S": "test-org"},
        }
        result = lambda_handler(api_gateway_event, None)

    assert result["policyDocument"]["Statement"][0]["Effect"] == "Allow"
    assert result["context"]["X-Auth-Source"] == "iam"


def test_handler_iam_path_does_not_read_allowlist(api_gateway_event, ip_allowlist, monkeypatch):
    """The allowlist is not even consulted on the IAM branch."""
    ip_allowlist("52.3.162.184/32")
    api_gateway_event["requestContext"]["identity"]["userArn"] = "arn:aws:sts::123456789012:assumed-role/test-agent-role/session"
    _event_from(api_gateway_event, "10.0.42.7")

    spy = mock.Mock(return_value=False)
    monkeypatch.setattr(handler, "source_ip_allowed", spy)

    with mock.patch("handler.lookup_agent_in_registry") as mock_lookup:
        mock_lookup.return_value = {"agent_id": {"S": "a"}, "enabled": {"BOOL": True}}
        lambda_handler(api_gateway_event, None)

    spy.assert_not_called()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
