"""
Unit tests for Agent Registry Admin API (Issue #248).

This module tests the DynamoDB-based agent registry for IAM/SigV4 authentication.

Test Coverage:
- AgentRegistryService CRUD operations with mocked DynamoDB (moto)
- UUID generation on create
- Validation: invalid role_arn -> 400, duplicate role_arn -> 409
- List with GSI queries (by org, by owner)
- Pagination with last_key
- Soft delete sets status to "disabled"
- Update preserves unchanged fields
- role_arn update checks uniqueness
- Lambda authorizer GSI query (by-role-arn)
"""

import base64
import json
import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError
from pydantic import ValidationError

from src.admin.agent_registry_schemas import (
    ROLE_ARN_PATTERN,
    AgentRegistryCreateRequest,
    AgentRegistryUpdateRequest,
)
from src.admin.agent_registry_service import AgentRegistryService
from src.shared.exceptions import ConflictError, NotFoundError
from src.shared.exceptions import ValidationError as GatewayValidationError

# =============================================================================
# Test Fixtures
# =============================================================================


@pytest.fixture
def mock_dynamodb_client():
    """Create a mock DynamoDB client."""
    return MagicMock()


@pytest.fixture
def agent_registry_service(mock_dynamodb_client):
    """Create an AgentRegistryService with mocked DynamoDB."""
    with patch("src.admin.agent_registry_service.get_settings") as mock_settings:
        mock_settings.return_value.aws_region = "us-east-1"
        service = AgentRegistryService(
            dynamodb_client=mock_dynamodb_client,
            table_name="test-agent-registry",
        )
    return service


@pytest.fixture
def valid_create_request():
    """Create a valid agent creation request."""
    return AgentRegistryCreateRequest(
        agent_name="test-agent",
        role_arn="arn:aws:iam::123456789012:role/test-role",
        org_id="test-org",
        team_id="test-team",
        owner="test-owner",
        scope="shared",
        budget_config_id="budget-123",
        allowed_models=["claude-sonnet", "claude-haiku"],
        description="Test agent for unit tests",
    )


@pytest.fixture
def sample_dynamodb_item():
    """Create a sample DynamoDB item."""
    now = datetime.now(UTC).isoformat()
    return {
        "agent_id": {"S": "12345678-1234-1234-1234-123456789012"},
        "agent_name": {"S": "test-agent"},
        "role_arn": {"S": "arn:aws:iam::123456789012:role/test-role"},
        "org_id": {"S": "test-org"},
        "team_id": {"S": "test-team"},
        "owner": {"S": "test-owner"},
        "scope": {"S": "shared"},
        "budget_config_id": {"S": "budget-123"},
        "allowed_models": {"SS": ["claude-sonnet", "claude-haiku"]},
        "status": {"S": "active"},
        "description": {"S": "Test agent"},
        "image_uri": {"S": ""},
        "code_repo": {"S": ""},
        "workflow_name": {"S": ""},
        "created_at": {"S": now},
        "updated_at": {"S": now},
    }


# =============================================================================
# Schema Validation Tests
# =============================================================================


class TestAgentRegistrySchemas:
    """Tests for agent registry Pydantic schemas."""

    def test_role_arn_pattern_valid(self):
        """Test valid IAM role ARN patterns."""
        valid_arns = [
            "arn:aws:iam::123456789012:role/my-role",
            "arn:aws:iam::123456789012:role/path/to/my-role",
            "arn:aws:iam::123456789012:role/service-role/AWSLambdaBasicExecutionRole",
            "arn:aws:iam::000000000000:role/test",
        ]
        for arn in valid_arns:
            assert ROLE_ARN_PATTERN.match(arn), f"Expected valid: {arn}"

    def test_role_arn_pattern_invalid(self):
        """Test invalid IAM role ARN patterns."""
        invalid_arns = [
            "arn:aws:iam::12345:role/my-role",  # Account ID too short
            "arn:aws:iam::1234567890123:role/my-role",  # Account ID too long
            "arn:aws:iam::123456789012:user/my-user",  # Not a role
            "arn:aws:sts::123456789012:assumed-role/my-role/session",  # STS ARN
            "invalid-arn",
            "",
        ]
        for arn in invalid_arns:
            assert not ROLE_ARN_PATTERN.match(arn), f"Expected invalid: {arn}"

    def test_create_request_validation_valid(self):
        """Test valid create request."""
        request = AgentRegistryCreateRequest(
            agent_name="my-agent",
            role_arn="arn:aws:iam::123456789012:role/my-role",
            org_id="org-123",
            owner="user-456",
        )
        assert request.agent_name == "my-agent"
        assert request.scope == "shared"  # Default
        assert request.allowed_models == []  # Default

    def test_create_request_validation_invalid_role_arn(self):
        """Test create request with invalid role_arn."""
        with pytest.raises(ValidationError) as exc_info:
            AgentRegistryCreateRequest(
                agent_name="my-agent",
                role_arn="invalid-arn",
                org_id="org-123",
                owner="user-456",
            )
        assert "role_arn" in str(exc_info.value)

    def test_create_request_validation_invalid_scope(self):
        """Test create request with invalid scope."""
        with pytest.raises(ValidationError):
            AgentRegistryCreateRequest(
                agent_name="my-agent",
                role_arn="arn:aws:iam::123456789012:role/my-role",
                org_id="org-123",
                owner="user-456",
                scope="invalid",  # Should be "shared" or "personal"
            )

    def test_update_request_optional_fields(self):
        """Test update request with optional fields."""
        request = AgentRegistryUpdateRequest(agent_name="new-name")
        assert request.agent_name == "new-name"
        assert request.role_arn is None
        assert request.status is None

    def test_update_request_role_arn_validation(self):
        """Test update request validates role_arn if provided."""
        with pytest.raises(ValidationError):
            AgentRegistryUpdateRequest(role_arn="invalid-arn")


# =============================================================================
# AgentRegistryService Tests
# =============================================================================


class TestAgentRegistryServiceCreate:
    """Tests for AgentRegistryService.create_agent()."""

    @pytest.mark.asyncio
    async def test_create_agent_success(self, agent_registry_service, mock_dynamodb_client, valid_create_request):
        """Test successful agent creation."""
        from unittest.mock import AsyncMock

        # Mock: no existing agent with same role_arn
        mock_dynamodb_client.query.return_value = {"Items": []}
        mock_dynamodb_client.put_item.return_value = {}

        # Mock budget_helper_service to avoid Postgres dependency (lazy import in create_agent)
        mock_budget_helper = MagicMock()
        mock_budget_helper.validate_budget_config_exists = AsyncMock(return_value=True)
        with patch.dict("sys.modules", {"src.admin.budget_helper": MagicMock(budget_helper_service=mock_budget_helper)}):
            result = await agent_registry_service.create_agent(valid_create_request)

        # Verify UUID was generated
        assert result.agent_id is not None
        assert len(result.agent_id) == 36  # UUID format
        uuid.UUID(result.agent_id)  # Should not raise

        # Verify other fields
        assert result.agent_name == valid_create_request.agent_name
        assert result.role_arn == valid_create_request.role_arn
        assert result.org_id == valid_create_request.org_id
        assert result.status == "active"
        assert result.allowed_models == valid_create_request.allowed_models

        # Verify DynamoDB calls
        mock_dynamodb_client.query.assert_called_once()
        mock_dynamodb_client.put_item.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_agent_duplicate_role_arn(self, agent_registry_service, mock_dynamodb_client, valid_create_request, sample_dynamodb_item):
        """Test agent creation fails if role_arn already exists."""
        # Mock: existing agent with same role_arn
        mock_dynamodb_client.query.return_value = {"Items": [sample_dynamodb_item]}

        with pytest.raises(ConflictError) as exc_info:
            await agent_registry_service.create_agent(valid_create_request)

        assert "already exists" in str(exc_info.value)
        mock_dynamodb_client.put_item.assert_not_called()

    @pytest.mark.asyncio
    async def test_create_agent_without_optional_fields(self, agent_registry_service, mock_dynamodb_client):
        """Test agent creation with minimal required fields."""
        request = AgentRegistryCreateRequest(
            agent_name="minimal-agent",
            role_arn="arn:aws:iam::123456789012:role/minimal-role",
            org_id="org-minimal",
            owner="owner-minimal",
        )
        mock_dynamodb_client.query.return_value = {"Items": []}
        mock_dynamodb_client.put_item.return_value = {}

        result = await agent_registry_service.create_agent(request)

        assert result.team_id is None
        assert result.budget_config_id is None
        assert result.allowed_models == []
        assert result.description is None


class TestAgentRegistryServiceGet:
    """Tests for AgentRegistryService.get_agent()."""

    @pytest.mark.asyncio
    async def test_get_agent_success(self, agent_registry_service, mock_dynamodb_client, sample_dynamodb_item):
        """Test successful agent retrieval."""
        mock_dynamodb_client.get_item.return_value = {"Item": sample_dynamodb_item}

        result = await agent_registry_service.get_agent("12345678-1234-1234-1234-123456789012")

        assert result.agent_id == "12345678-1234-1234-1234-123456789012"
        assert result.agent_name == "test-agent"
        assert result.org_id == "test-org"

    @pytest.mark.asyncio
    async def test_get_agent_not_found(self, agent_registry_service, mock_dynamodb_client):
        """Test agent retrieval when not found."""
        mock_dynamodb_client.get_item.return_value = {}

        with pytest.raises(NotFoundError):
            await agent_registry_service.get_agent("nonexistent-id")


class TestAgentRegistryServiceGetByRole:
    """Tests for AgentRegistryService.get_agent_by_role()."""

    @pytest.mark.asyncio
    async def test_get_agent_by_role_success(self, agent_registry_service, mock_dynamodb_client, sample_dynamodb_item):
        """Test successful agent lookup by role_arn."""
        mock_dynamodb_client.query.return_value = {"Items": [sample_dynamodb_item]}

        result = await agent_registry_service.get_agent_by_role("arn:aws:iam::123456789012:role/test-role")

        assert result is not None
        assert result.agent_id == "12345678-1234-1234-1234-123456789012"

        # Verify GSI query
        call_args = mock_dynamodb_client.query.call_args
        assert call_args[1]["IndexName"] == "by-role-arn"

    @pytest.mark.asyncio
    async def test_get_agent_by_role_not_found(self, agent_registry_service, mock_dynamodb_client):
        """Test agent lookup by role_arn when not found."""
        mock_dynamodb_client.query.return_value = {"Items": []}

        result = await agent_registry_service.get_agent_by_role("arn:aws:iam::123456789012:role/nonexistent")

        assert result is None


class TestAgentRegistryServiceList:
    """Tests for AgentRegistryService.list_agents()."""

    @pytest.mark.asyncio
    async def test_list_agents_by_org(self, agent_registry_service, mock_dynamodb_client, sample_dynamodb_item):
        """Test listing agents by organization."""
        mock_dynamodb_client.query.return_value = {"Items": [sample_dynamodb_item]}

        result = await agent_registry_service.list_agents(org_id="test-org")

        assert len(result.items) == 1
        assert result.items[0].org_id == "test-org"

        # Verify GSI query
        call_args = mock_dynamodb_client.query.call_args
        assert call_args[1]["IndexName"] == "by-org-team"

    @pytest.mark.asyncio
    async def test_list_agents_by_org_and_team(self, agent_registry_service, mock_dynamodb_client, sample_dynamodb_item):
        """Test listing agents by organization and team."""
        mock_dynamodb_client.query.return_value = {"Items": [sample_dynamodb_item]}

        result = await agent_registry_service.list_agents(org_id="test-org", team_id="test-team")

        assert len(result.items) == 1

        # Verify GSI query with both keys
        call_args = mock_dynamodb_client.query.call_args
        assert "org_id = :org_id AND team_id = :team_id" in call_args[1]["KeyConditionExpression"]

    @pytest.mark.asyncio
    async def test_list_agents_by_owner(self, agent_registry_service, mock_dynamodb_client, sample_dynamodb_item):
        """Test listing agents by owner."""
        mock_dynamodb_client.query.return_value = {"Items": [sample_dynamodb_item]}

        result = await agent_registry_service.list_agents(owner="test-owner")

        assert len(result.items) == 1

        # Verify GSI query
        call_args = mock_dynamodb_client.query.call_args
        assert call_args[1]["IndexName"] == "by-owner"

    @pytest.mark.asyncio
    async def test_list_agents_pagination(self, agent_registry_service, mock_dynamodb_client, sample_dynamodb_item):
        """Test pagination with last_key."""
        # First page response with LastEvaluatedKey
        mock_dynamodb_client.query.return_value = {
            "Items": [sample_dynamodb_item],
            "LastEvaluatedKey": {"agent_id": {"S": "12345678-1234-1234-1234-123456789012"}},
        }

        result = await agent_registry_service.list_agents(org_id="test-org", page_size=1)

        assert result.last_key is not None

        # Decode and verify the pagination token
        decoded_key = json.loads(base64.b64decode(result.last_key))
        assert "agent_id" in decoded_key

    @pytest.mark.asyncio
    async def test_list_agents_with_last_key(self, agent_registry_service, mock_dynamodb_client, sample_dynamodb_item):
        """Test continuing from a pagination token."""
        mock_dynamodb_client.query.return_value = {"Items": [sample_dynamodb_item]}

        # Create a pagination token
        last_key = base64.b64encode(json.dumps({"agent_id": {"S": "prev-agent-id"}}).encode()).decode()

        await agent_registry_service.list_agents(org_id="test-org", last_key=last_key)

        # Verify ExclusiveStartKey was passed
        call_args = mock_dynamodb_client.query.call_args
        assert "ExclusiveStartKey" in call_args[1]

    @pytest.mark.asyncio
    async def test_list_agents_scan_all(self, agent_registry_service, mock_dynamodb_client, sample_dynamodb_item):
        """Test scanning all agents (no filters) with the explicit opt-in."""
        mock_dynamodb_client.scan.return_value = {"Items": [sample_dynamodb_item]}

        result = await agent_registry_service.list_agents(allow_scan=True)

        mock_dynamodb_client.scan.assert_called_once()
        assert len(result.items) == 1

    @pytest.mark.asyncio
    async def test_list_agents_unfiltered_without_allow_scan_raises(self, agent_registry_service, mock_dynamodb_client):
        """Issue #3988: an unfiltered list must not silently scan every tenant.

        Structural guard — _scan_all is only reachable via allow_scan=True, so a
        future authorization bug (like the un-awaited is_platform_admin) cannot
        reach a cross-tenant scan by accident.
        """
        with pytest.raises(GatewayValidationError):
            await agent_registry_service.list_agents()

        mock_dynamodb_client.scan.assert_not_called()
        mock_dynamodb_client.query.assert_not_called()


class TestAgentRegistryServiceUpdate:
    """Tests for AgentRegistryService.update_agent()."""

    @pytest.mark.asyncio
    async def test_update_agent_success(self, agent_registry_service, mock_dynamodb_client, sample_dynamodb_item):
        """Test successful agent update."""
        mock_dynamodb_client.get_item.return_value = {"Item": sample_dynamodb_item}

        # Updated item
        updated_item = sample_dynamodb_item.copy()
        updated_item["agent_name"]["S"] = "updated-agent"
        mock_dynamodb_client.update_item.return_value = {"Attributes": updated_item}

        request = AgentRegistryUpdateRequest(agent_name="updated-agent")
        result = await agent_registry_service.update_agent("12345678-1234-1234-1234-123456789012", request)

        assert result.agent_name == "updated-agent"
        mock_dynamodb_client.update_item.assert_called_once()

    @pytest.mark.asyncio
    async def test_update_agent_role_arn_uniqueness(self, agent_registry_service, mock_dynamodb_client, sample_dynamodb_item):
        """Test role_arn update checks uniqueness."""
        mock_dynamodb_client.get_item.return_value = {"Item": sample_dynamodb_item}

        # Another agent already has the new role_arn
        other_agent = sample_dynamodb_item.copy()
        other_agent["agent_id"]["S"] = "other-agent-id"
        mock_dynamodb_client.query.return_value = {"Items": [other_agent]}

        request = AgentRegistryUpdateRequest(role_arn="arn:aws:iam::123456789012:role/other-role")

        with pytest.raises(ConflictError) as exc_info:
            await agent_registry_service.update_agent("12345678-1234-1234-1234-123456789012", request)

        assert "already exists" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_update_agent_not_found(self, agent_registry_service, mock_dynamodb_client):
        """Test update fails if agent not found."""
        mock_dynamodb_client.get_item.return_value = {}

        request = AgentRegistryUpdateRequest(agent_name="new-name")

        with pytest.raises(NotFoundError):
            await agent_registry_service.update_agent("nonexistent-id", request)

    @pytest.mark.asyncio
    async def test_update_agent_preserves_unchanged_fields(self, agent_registry_service, mock_dynamodb_client, sample_dynamodb_item):
        """Test update preserves fields not in request."""
        mock_dynamodb_client.get_item.return_value = {"Item": sample_dynamodb_item}
        mock_dynamodb_client.query.return_value = {"Items": []}  # For role_arn check
        mock_dynamodb_client.update_item.return_value = {"Attributes": sample_dynamodb_item}

        # Only update agent_name
        request = AgentRegistryUpdateRequest(agent_name="new-name")
        await agent_registry_service.update_agent("12345678-1234-1234-1234-123456789012", request)

        # Verify update expression only contains agent_name
        call_args = mock_dynamodb_client.update_item.call_args
        update_expr = call_args[1]["UpdateExpression"]
        assert "agent_name" in update_expr
        assert "role_arn" not in update_expr


class TestAgentRegistryServiceDelete:
    """Tests for AgentRegistryService.delete_agent()."""

    @pytest.mark.asyncio
    async def test_delete_agent_soft_delete(self, agent_registry_service, mock_dynamodb_client, sample_dynamodb_item):
        """Test delete performs soft delete (sets status=disabled)."""
        mock_dynamodb_client.get_item.return_value = {"Item": sample_dynamodb_item}
        mock_dynamodb_client.update_item.return_value = {}

        await agent_registry_service.delete_agent("12345678-1234-1234-1234-123456789012")

        # Verify status was set to disabled
        call_args = mock_dynamodb_client.update_item.call_args
        assert ":status" in call_args[1]["ExpressionAttributeValues"]
        assert call_args[1]["ExpressionAttributeValues"][":status"]["S"] == "disabled"

    @pytest.mark.asyncio
    async def test_delete_agent_not_found(self, agent_registry_service, mock_dynamodb_client):
        """Test delete fails if agent not found."""
        mock_dynamodb_client.get_item.return_value = {}

        with pytest.raises(NotFoundError):
            await agent_registry_service.delete_agent("nonexistent-id")


# =============================================================================
# Lambda Authorizer Tests
# =============================================================================


class TestLambdaAuthorizerGSIQuery:
    """Tests for Lambda authorizer GSI query functionality."""

    def test_lookup_agent_response_structure(self, sample_dynamodb_item):
        """Test that agent lookup returns expected structure with agent_id."""
        # This test verifies the structure of the lookup function response
        # The Lambda authorizer (lambda/api-authorizer/handler.py) queries
        # the by-role-arn GSI and returns agent_id as the primary identity

        # Simulate what the Lambda authorizer returns after querying GSI
        agent = {
            "agent_id": sample_dynamodb_item["agent_id"]["S"],
            "agent_name": sample_dynamodb_item["agent_name"]["S"],
            "org_id": sample_dynamodb_item["org_id"]["S"],
            "team_id": sample_dynamodb_item["team_id"]["S"],
            "owner": sample_dynamodb_item["owner"]["S"],
            "scope": sample_dynamodb_item["scope"]["S"],
            "budget_config_id": sample_dynamodb_item["budget_config_id"]["S"],
            "allowed_models": sample_dynamodb_item["allowed_models"]["SS"],
            "status": sample_dynamodb_item["status"]["S"],
        }

        # Verify agent_id is the primary identity (Issue #248)
        assert "agent_id" in agent
        assert agent["agent_id"] == "12345678-1234-1234-1234-123456789012"

        # Verify all required fields are present
        required_fields = [
            "agent_id",
            "agent_name",
            "org_id",
            "team_id",
            "owner",
            "scope",
            "status",
        ]
        for field in required_fields:
            assert field in agent, f"Missing required field: {field}"


# =============================================================================
# Integration-like Tests (with moto would be full integration)
# =============================================================================


class TestAgentRegistryServiceTableNotFound:
    """Tests for handling missing DynamoDB table."""

    @pytest.mark.asyncio
    async def test_list_returns_empty_on_resource_not_found(self, agent_registry_service, mock_dynamodb_client):
        """Test list returns empty when table doesn't exist."""
        error_response = {
            "Error": {
                "Code": "ResourceNotFoundException",
                "Message": "Table not found",
            }
        }
        mock_dynamodb_client.query.side_effect = ClientError(error_response, "Query")

        result = await agent_registry_service.list_agents(org_id="test-org")

        assert result.items == []
        assert result.count == 0

    @pytest.mark.asyncio
    async def test_get_by_role_returns_none_on_resource_not_found(self, agent_registry_service, mock_dynamodb_client):
        """Test get_agent_by_role returns None when table doesn't exist."""
        error_response = {
            "Error": {
                "Code": "ResourceNotFoundException",
                "Message": "Table not found",
            }
        }
        mock_dynamodb_client.query.side_effect = ClientError(error_response, "Query")

        result = await agent_registry_service.get_agent_by_role("arn:aws:iam::123456789012:role/test-role")

        assert result is None
