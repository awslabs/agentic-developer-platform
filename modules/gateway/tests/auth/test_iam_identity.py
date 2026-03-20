"""
Tests for IAM identity extraction from API Gateway AWS_IAM auth headers.

Issue #260: Dual-Path API Gateway - NONE auth (humans) + AWS_IAM auth (agents)

These tests verify:
1. Parsing assumed-role ARNs to IAM role ARNs
2. DynamoDB agent registry lookup
3. TokenContext building from agent registry entries
4. Middleware IAM identity extraction
"""

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
from starlette.requests import Request

from src.auth.agent_registry import (
    AgentRegistryEntry,
    AgentRegistryService,
    agent_entry_to_token_context,
    parse_assumed_role_arn,
)
from src.auth.middleware import (
    API_GATEWAY_HEADER_CALLER_IDENTITY,
    API_GATEWAY_HEADER_IAM_USER_ARN,
    extract_iam_identity_from_headers,
)
from src.shared.exceptions import UnregisteredServiceAccountError


class TestParseAssumedRoleArn:
    """Tests for parse_assumed_role_arn function."""

    def test_parses_assumed_role_arn(self):
        """Test parsing standard assumed-role ARN."""
        user_arn = "arn:aws:sts::123456789012:assumed-role/my-role/session-name"
        role_arn = parse_assumed_role_arn(user_arn)

        assert role_arn == "arn:aws:iam::123456789012:role/my-role"

    def test_parses_assumed_role_with_complex_name(self):
        """Test parsing assumed-role ARN with complex role name."""
        user_arn = "arn:aws:sts::123456789012:assumed-role/my-app-agent-role-dev/i-abc123"
        role_arn = parse_assumed_role_arn(user_arn)

        assert role_arn == "arn:aws:iam::123456789012:role/my-app-agent-role-dev"

    def test_returns_iam_role_arn_unchanged(self):
        """Test that IAM role ARN is returned unchanged."""
        user_arn = "arn:aws:iam::123456789012:role/my-role"
        role_arn = parse_assumed_role_arn(user_arn)

        assert role_arn == user_arn

    def test_returns_none_for_invalid_arn(self):
        """Test that invalid ARN returns None."""
        assert parse_assumed_role_arn("not-an-arn") is None
        assert parse_assumed_role_arn("arn:aws:s3:::my-bucket") is None
        assert parse_assumed_role_arn("") is None
        assert parse_assumed_role_arn(None) is None

    def test_parses_long_session_names(self):
        """Test parsing ARN with long session names."""
        user_arn = "arn:aws:sts::123456789012:assumed-role/role-name/session-with-long-name-and-timestamp-123456"
        role_arn = parse_assumed_role_arn(user_arn)

        assert role_arn == "arn:aws:iam::123456789012:role/role-name"


class TestAgentEntryToTokenContext:
    """Tests for agent_entry_to_token_context function."""

    def test_converts_entry_to_token_context(self):
        """Test converting agent registry entry to TokenContext."""
        entry: AgentRegistryEntry = {
            "agent_id": "00000000-0000-0000-0000-000000000001",
            "role_arn": "arn:aws:iam::123456789012:role/test-agent",
            "agent_name": "test-agent",
            "org_id": "test-org",
            "team_id": "test-team",
            "owner": "system",
            "scope": "shared",
            "budget_config_id": "",
            "allowed_models": ["claude-sonnet"],
            "status": "active",
            "description": "Test agent",
            "image_uri": "",
            "code_repo": "",
            "workflow_name": "",
            "created_at": "2024-01-01T00:00:00Z",
            "updated_at": "2024-01-01T00:00:00Z",
        }

        context = agent_entry_to_token_context(entry)

        assert context.user_id == "test-agent"
        assert context.org_id == "test-org"
        assert context.team_id == "test-team"
        assert context.department_id == ""
        assert context.account_type == "service"
        assert context.is_admin is False
        assert context.auth_source == "iam"
        assert context.expires_at > datetime.now(UTC)


class TestAgentRegistryService:
    """Tests for AgentRegistryService."""

    @pytest.fixture
    def mock_dynamodb_response(self):
        """Create mock DynamoDB response for agent lookup."""
        return {
            "Items": [
                {
                    "agent_id": {"S": "00000000-0000-0000-0000-000000000001"},
                    "role_arn": {"S": "arn:aws:iam::123456789012:role/test-agent"},
                    "agent_name": {"S": "test-agent"},
                    "org_id": {"S": "test-org"},
                    "team_id": {"S": "test-team"},
                    "owner": {"S": "system"},
                    "scope": {"S": "shared"},
                    "budget_config_id": {"S": ""},
                    "allowed_models": {"SS": ["claude-sonnet", "claude-haiku"]},
                    "status": {"S": "active"},
                    "description": {"S": "Test agent"},
                    "image_uri": {"S": ""},
                    "code_repo": {"S": ""},
                    "workflow_name": {"S": ""},
                    "created_at": {"S": "2024-01-01T00:00:00Z"},
                    "updated_at": {"S": "2024-01-01T00:00:00Z"},
                }
            ]
        }

    def test_get_agent_by_role_arn_success(self, mock_dynamodb_response):
        """Test successful agent lookup by role ARN."""
        with patch("src.auth.agent_registry.get_settings") as mock_settings:
            mock_settings.return_value.agent_registry_table = "test-table"
            mock_settings.return_value.aws_region = "us-east-1"

            with patch("boto3.client") as mock_boto_client:
                mock_dynamodb = MagicMock()
                mock_dynamodb.query.return_value = mock_dynamodb_response
                mock_boto_client.return_value = mock_dynamodb

                service = AgentRegistryService(table_name="test-table")
                entry = service.get_agent_by_role_arn("arn:aws:iam::123456789012:role/test-agent")

                assert entry is not None
                assert entry["agent_name"] == "test-agent"
                assert entry["org_id"] == "test-org"
                assert entry["status"] == "active"

    def test_get_agent_by_role_arn_not_found(self):
        """Test agent lookup when not found."""
        with patch("src.auth.agent_registry.get_settings") as mock_settings:
            mock_settings.return_value.agent_registry_table = "test-table"
            mock_settings.return_value.aws_region = "us-east-1"

            with patch("boto3.client") as mock_boto_client:
                mock_dynamodb = MagicMock()
                mock_dynamodb.query.return_value = {"Items": []}
                mock_boto_client.return_value = mock_dynamodb

                service = AgentRegistryService(table_name="test-table")
                entry = service.get_agent_by_role_arn("arn:aws:iam::123456789012:role/unknown-agent")

                assert entry is None

    def test_get_agent_by_role_arn_inactive(self, mock_dynamodb_response):
        """Test agent lookup returns None for inactive agents."""
        mock_dynamodb_response["Items"][0]["status"]["S"] = "inactive"

        with patch("src.auth.agent_registry.get_settings") as mock_settings:
            mock_settings.return_value.agent_registry_table = "test-table"
            mock_settings.return_value.aws_region = "us-east-1"

            with patch("boto3.client") as mock_boto_client:
                mock_dynamodb = MagicMock()
                mock_dynamodb.query.return_value = mock_dynamodb_response
                mock_boto_client.return_value = mock_dynamodb

                service = AgentRegistryService(table_name="test-table")
                entry = service.get_agent_by_role_arn("arn:aws:iam::123456789012:role/test-agent")

                assert entry is None

    def test_get_agent_by_role_arn_no_table_configured(self):
        """Test agent lookup when table is not configured."""
        with patch("src.auth.agent_registry.get_settings") as mock_settings:
            mock_settings.return_value.agent_registry_table = ""
            mock_settings.return_value.aws_region = "us-east-1"

            service = AgentRegistryService(table_name="")

            entry = service.get_agent_by_role_arn("arn:aws:iam::123456789012:role/test-agent")

            assert entry is None

    def test_cache_prevents_repeated_lookups(self, mock_dynamodb_response):
        """Test that caching prevents repeated DynamoDB lookups."""
        with patch("src.auth.agent_registry.get_settings") as mock_settings:
            mock_settings.return_value.agent_registry_table = "test-table"
            mock_settings.return_value.aws_region = "us-east-1"

            with patch("boto3.client") as mock_boto_client:
                mock_dynamodb = MagicMock()
                mock_dynamodb.query.return_value = mock_dynamodb_response
                mock_boto_client.return_value = mock_dynamodb

                service = AgentRegistryService(table_name="test-table")

                # First lookup
                entry1 = service.get_agent_by_role_arn("arn:aws:iam::123456789012:role/test-agent")

                # Second lookup (should use cache)
                entry2 = service.get_agent_by_role_arn("arn:aws:iam::123456789012:role/test-agent")

                assert entry1 is not None
                assert entry2 is not None
                assert entry1["agent_name"] == entry2["agent_name"]

                # DynamoDB should only be called once
                assert mock_dynamodb.query.call_count == 1


class TestExtractIamIdentityFromHeaders:
    """Tests for extract_iam_identity_from_headers function."""

    def _create_mock_request(self, headers: dict) -> Request:
        """Create a mock request with the given headers."""
        scope = {
            "type": "http",
            "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
            "method": "GET",
            "path": "/v1/messages",
        }
        return Request(scope)

    def test_extracts_identity_from_x_caller_identity(self):
        """Test extracting IAM identity from X-Caller-Identity header."""
        headers = {
            API_GATEWAY_HEADER_CALLER_IDENTITY: "arn:aws:sts::123456789012:assumed-role/test-agent/session",
        }
        request = self._create_mock_request(headers)

        mock_entry: AgentRegistryEntry = {
            "agent_id": "00000000-0000-0000-0000-000000000001",
            "role_arn": "arn:aws:iam::123456789012:role/test-agent",
            "agent_name": "test-agent",
            "org_id": "test-org",
            "team_id": "test-team",
            "owner": "system",
            "scope": "shared",
            "budget_config_id": "",
            "allowed_models": ["claude-sonnet"],
            "status": "active",
            "description": "Test agent",
            "image_uri": "",
            "code_repo": "",
            "workflow_name": "",
            "created_at": "2024-01-01T00:00:00Z",
            "updated_at": "2024-01-01T00:00:00Z",
        }

        with patch("src.auth.agent_registry.get_agent_registry_service") as mock_get_service:
            mock_service = MagicMock()
            mock_service.get_agent_by_role_arn.return_value = mock_entry
            mock_get_service.return_value = mock_service

            context = extract_iam_identity_from_headers(request)

            assert context is not None
            assert context.user_id == "test-agent"
            assert context.org_id == "test-org"
            assert context.auth_source == "iam"

    def test_extracts_identity_from_x_amzn_iam_user_arn(self):
        """Test extracting IAM identity from X-Amzn-Iam-User-Arn header."""
        headers = {
            API_GATEWAY_HEADER_IAM_USER_ARN: "arn:aws:sts::123456789012:assumed-role/test-agent/session",
        }
        request = self._create_mock_request(headers)

        mock_entry: AgentRegistryEntry = {
            "agent_id": "00000000-0000-0000-0000-000000000001",
            "role_arn": "arn:aws:iam::123456789012:role/test-agent",
            "agent_name": "test-agent",
            "org_id": "test-org",
            "team_id": "test-team",
            "owner": "system",
            "scope": "shared",
            "budget_config_id": "",
            "allowed_models": ["claude-sonnet"],
            "status": "active",
            "description": "Test agent",
            "image_uri": "",
            "code_repo": "",
            "workflow_name": "",
            "created_at": "2024-01-01T00:00:00Z",
            "updated_at": "2024-01-01T00:00:00Z",
        }

        with patch("src.auth.agent_registry.get_agent_registry_service") as mock_get_service:
            mock_service = MagicMock()
            mock_service.get_agent_by_role_arn.return_value = mock_entry
            mock_get_service.return_value = mock_service

            context = extract_iam_identity_from_headers(request)

            assert context is not None
            assert context.user_id == "test-agent"

    def test_returns_none_when_no_iam_headers(self):
        """Test returns None when no IAM identity headers present."""
        headers = {"Authorization": "Bearer some-jwt-token"}
        request = self._create_mock_request(headers)

        context = extract_iam_identity_from_headers(request)

        assert context is None

    def test_raises_403_for_unregistered_agent(self):
        """Test raises UnregisteredServiceAccountError when agent is not registered."""
        headers = {
            API_GATEWAY_HEADER_CALLER_IDENTITY: "arn:aws:sts::123456789012:assumed-role/unknown-agent/session",
        }
        request = self._create_mock_request(headers)

        with patch("src.auth.agent_registry.get_agent_registry_service") as mock_get_service:
            mock_service = MagicMock()
            mock_service.get_agent_by_role_arn.return_value = None
            mock_get_service.return_value = mock_service

            with pytest.raises(UnregisteredServiceAccountError) as exc_info:
                extract_iam_identity_from_headers(request)

            assert exc_info.value.status_code == 403
            assert "not registered" in exc_info.value.message.lower()

    def test_returns_none_for_invalid_arn(self):
        """Test returns None when ARN cannot be parsed."""
        headers = {
            API_GATEWAY_HEADER_CALLER_IDENTITY: "not-a-valid-arn",
        }
        request = self._create_mock_request(headers)

        context = extract_iam_identity_from_headers(request)

        assert context is None
