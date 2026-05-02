"""
Tests for Agent (Cognito App Client) Management Service.

Issue #119: Unified Cognito JWT Auth
"""

from unittest.mock import MagicMock, Mock, patch

import pytest
from botocore.exceptions import ClientError

from src.admin.agent_schemas import (
    AgentCreateRequest,
    AgentUpdateRequest,
)
from src.admin.agent_service import AgentService
from src.shared.exceptions import NotFoundError, ValidationError


@pytest.fixture
def mock_settings():
    """Mock settings for tests."""
    with patch("src.admin.agent_service.get_settings") as mock:
        settings = Mock()
        settings.aws_region = "us-east-1"
        settings.cognito_user_pool_id = "us-east-1_testpool"
        settings.cognito_domain = "test-domain"
        mock.return_value = settings
        yield settings


@pytest.fixture
def mock_cognito_client():
    """Mock boto3 Cognito IDP client."""
    mock = MagicMock()
    return mock


@pytest.fixture
def mock_dynamodb():
    """Mock boto3 DynamoDB resource."""
    mock = MagicMock()
    mock_table = MagicMock()
    mock.Table.return_value = mock_table
    return mock


@pytest.fixture
def agent_service(mock_settings, mock_cognito_client, mock_dynamodb):
    """Create an AgentService instance with mocked dependencies."""
    with patch.dict("os.environ", {"IDENTITY_TABLE": "test-identity"}):
        service = AgentService(
            cognito_client=mock_cognito_client,
            dynamodb_resource=mock_dynamodb,
            user_pool_id="us-east-1_testpool",
            table_name="test-identity",
        )
        return service


class TestAgentServiceInit:
    """Tests for AgentService initialization."""

    def test_init_with_explicit_values(self, mock_settings, mock_cognito_client, mock_dynamodb):
        """Test initialization with explicit values."""
        service = AgentService(
            cognito_client=mock_cognito_client,
            dynamodb_resource=mock_dynamodb,
            user_pool_id="us-east-1_custom",
            table_name="custom-table",
        )

        assert service.user_pool_id == "us-east-1_custom"
        assert service.table_name == "custom-table"

    def test_init_builds_token_endpoint(self, mock_settings, mock_cognito_client, mock_dynamodb):
        """Test that initialization builds correct token endpoint."""
        service = AgentService(
            cognito_client=mock_cognito_client,
            dynamodb_resource=mock_dynamodb,
            user_pool_id="us-east-1_testpool",
        )

        expected = "https://test-domain.auth.us-east-1.amazoncognito.com/oauth2/token"
        assert service.token_endpoint == expected


class TestCreateAgent:
    """Tests for create_agent method."""

    @pytest.mark.asyncio
    async def test_create_agent_success(self, agent_service, mock_cognito_client, mock_dynamodb):
        """Test successful agent creation."""
        # Mock Cognito response
        mock_cognito_client.create_user_pool_client.return_value = {
            "UserPoolClient": {
                "ClientId": "new-agent-client-id",
            }
        }

        request = AgentCreateRequest(
            name="my-agent",
            org_id="org-123",
            team_id="team-456",
            description="Test agent",
            scopes=["bedrockgw/invoke"],
        )

        result = await agent_service.create_agent(request)

        # Verify Cognito client was called correctly
        mock_cognito_client.create_user_pool_client.assert_called_once()
        call_kwargs = mock_cognito_client.create_user_pool_client.call_args.kwargs

        assert call_kwargs["UserPoolId"] == "us-east-1_testpool"
        assert call_kwargs["GenerateSecret"] is True
        assert call_kwargs["AllowedOAuthFlows"] == ["client_credentials"]
        assert "bedrockgw/invoke" in call_kwargs["AllowedOAuthScopes"]

        # Verify DynamoDB was updated
        mock_dynamodb.Table.return_value.put_item.assert_called_once()

        # Verify response
        assert result.client_id == "new-agent-client-id"
        assert result.name == "my-agent"
        assert result.org_id == "org-123"

    @pytest.mark.asyncio
    async def test_create_agent_without_user_pool_id(self, mock_cognito_client, mock_dynamodb):
        """Test that creation fails without user pool ID."""
        # Mock settings with empty user pool ID
        with patch("src.admin.agent_service.get_settings") as mock_get_settings:
            mock_settings = Mock()
            mock_settings.aws_region = "us-east-1"
            mock_settings.cognito_user_pool_id = ""
            mock_settings.cognito_domain = "test-domain"
            mock_get_settings.return_value = mock_settings

            service = AgentService(
                cognito_client=mock_cognito_client,
                dynamodb_resource=mock_dynamodb,
                user_pool_id=None,  # Pass None to use settings value
                table_name="test-table",
            )

            request = AgentCreateRequest(
                name="my-agent",
                org_id="org-123",
            )

            with pytest.raises(ValidationError, match="User Pool ID not configured"):
                await service.create_agent(request)

    @pytest.mark.asyncio
    async def test_create_agent_cognito_error(self, agent_service, mock_cognito_client):
        """Test handling of Cognito errors during creation."""
        mock_cognito_client.create_user_pool_client.side_effect = ClientError(
            {"Error": {"Code": "InvalidParameterException", "Message": "Invalid"}},
            "CreateUserPoolClient",
        )

        request = AgentCreateRequest(
            name="my-agent",
            org_id="org-123",
        )

        with pytest.raises(ValidationError):
            await agent_service.create_agent(request)


class TestGetAgent:
    """Tests for get_agent method."""

    @pytest.mark.asyncio
    async def test_get_agent_success(self, agent_service, mock_dynamodb):
        """Test successful agent retrieval."""
        mock_dynamodb.Table.return_value.get_item.return_value = {
            "Item": {
                "client_id": "agent-client-123",
                "name": "my-agent",
                "org_id": "org-123",
                "team_id": "team-456",
                "scopes": ["bedrockgw/invoke"],
                "status": "active",
                "created_at": "2024-01-01T00:00:00+00:00",
                "updated_at": "2024-01-01T00:00:00+00:00",
            }
        }

        result = await agent_service.get_agent("agent-client-123", "org-123")

        assert result.client_id == "agent-client-123"
        assert result.name == "my-agent"
        assert result.org_id == "org-123"

    @pytest.mark.asyncio
    async def test_get_agent_not_found(self, agent_service, mock_dynamodb):
        """Test handling of agent not found."""
        mock_dynamodb.Table.return_value.get_item.return_value = {}

        with pytest.raises(NotFoundError, match="Agent not found"):
            await agent_service.get_agent("nonexistent", "org-123")

    @pytest.mark.asyncio
    async def test_get_agent_wrong_org(self, agent_service, mock_dynamodb):
        """Test that agents from other orgs are not returned."""
        mock_dynamodb.Table.return_value.get_item.return_value = {
            "Item": {
                "client_id": "agent-client-123",
                "name": "my-agent",
                "org_id": "org-other",  # Different org
                "status": "active",
                "created_at": "2024-01-01T00:00:00+00:00",
            }
        }

        with pytest.raises(NotFoundError, match="Agent not found"):
            await agent_service.get_agent("agent-client-123", "org-123")


class TestListAgents:
    """Tests for list_agents method."""

    @pytest.mark.asyncio
    async def test_list_agents_success(self, agent_service, mock_dynamodb):
        """Test successful agent listing."""
        mock_dynamodb.Table.return_value.query.return_value = {
            "Items": [
                {
                    "client_id": "agent-1",
                    "name": "agent-one",
                    "org_id": "org-123",
                    "status": "active",
                    "scopes": [],
                    "created_at": "2024-01-01T00:00:00+00:00",
                },
                {
                    "client_id": "agent-2",
                    "name": "agent-two",
                    "org_id": "org-123",
                    "status": "active",
                    "scopes": [],
                    "created_at": "2024-01-02T00:00:00+00:00",
                },
            ]
        }

        result = await agent_service.list_agents("org-123")

        assert len(result.items) == 2
        assert result.total == 2
        assert result.items[0].client_id == "agent-1"

    @pytest.mark.asyncio
    async def test_list_agents_empty(self, agent_service, mock_dynamodb):
        """Test listing when no agents exist."""
        mock_dynamodb.Table.return_value.query.return_value = {"Items": []}

        result = await agent_service.list_agents("org-123")

        assert len(result.items) == 0
        assert result.total == 0

    @pytest.mark.asyncio
    async def test_list_agents_pagination(self, agent_service, mock_dynamodb):
        """Test pagination of agent listing."""
        # Create 5 mock agents
        items = [
            {
                "client_id": f"agent-{i}",
                "name": f"agent-{i}",
                "org_id": "org-123",
                "status": "active",
                "scopes": [],
                "created_at": "2024-01-01T00:00:00+00:00",
            }
            for i in range(5)
        ]
        mock_dynamodb.Table.return_value.query.return_value = {"Items": items}

        # Request page 1 with page_size 2
        result = await agent_service.list_agents("org-123", page=1, page_size=2)

        assert len(result.items) == 2
        assert result.total == 5
        assert result.has_more is True


class TestGetAgentCredentials:
    """Tests for get_agent_credentials method."""

    @pytest.mark.asyncio
    async def test_get_credentials_success(self, agent_service, mock_cognito_client, mock_dynamodb):
        """Test successful credentials retrieval."""
        # Mock DynamoDB agent record
        mock_dynamodb.Table.return_value.get_item.return_value = {
            "Item": {
                "client_id": "agent-client-123",
                "name": "my-agent",
                "org_id": "org-123",
                "scopes": ["bedrockgw/invoke"],
                "status": "active",
                "created_at": "2024-01-01T00:00:00+00:00",
            }
        }

        # Mock Cognito describe response
        mock_cognito_client.describe_user_pool_client.return_value = {
            "UserPoolClient": {
                "ClientId": "agent-client-123",
                "ClientSecret": "super-secret-value",
            }
        }

        result = await agent_service.get_agent_credentials("agent-client-123", "org-123")

        assert result.client_id == "agent-client-123"
        assert result.client_secret == "super-secret-value"
        assert "oauth2/token" in result.token_endpoint
        assert "bedrockgw/invoke" in result.scopes


class TestUpdateAgent:
    """Tests for update_agent method."""

    @pytest.mark.asyncio
    async def test_update_agent_success(self, agent_service, mock_dynamodb):
        """Test successful agent update."""
        # Mock initial get
        mock_dynamodb.Table.return_value.get_item.return_value = {
            "Item": {
                "client_id": "agent-client-123",
                "name": "my-agent",
                "org_id": "org-123",
                "status": "active",
                "scopes": [],
                "created_at": "2024-01-01T00:00:00+00:00",
            }
        }

        # Mock update response
        mock_dynamodb.Table.return_value.update_item.return_value = {
            "Attributes": {
                "client_id": "agent-client-123",
                "name": "updated-agent",
                "org_id": "org-123",
                "status": "active",
                "scopes": [],
                "created_at": "2024-01-01T00:00:00+00:00",
                "updated_at": "2024-01-02T00:00:00+00:00",
            }
        }

        request = AgentUpdateRequest(name="updated-agent")
        result = await agent_service.update_agent("agent-client-123", "org-123", request)

        assert result.name == "updated-agent"


class TestDeleteAgent:
    """Tests for delete_agent method."""

    @pytest.mark.asyncio
    async def test_delete_agent_success(self, agent_service, mock_cognito_client, mock_dynamodb):
        """Test successful agent deletion."""
        # Mock agent exists
        mock_dynamodb.Table.return_value.get_item.return_value = {
            "Item": {
                "client_id": "agent-client-123",
                "name": "my-agent",
                "org_id": "org-123",
                "status": "active",
                "scopes": [],
                "created_at": "2024-01-01T00:00:00+00:00",
            }
        }

        await agent_service.delete_agent("agent-client-123", "org-123")

        # Verify Cognito client was deleted
        mock_cognito_client.delete_user_pool_client.assert_called_once_with(
            UserPoolId="us-east-1_testpool",
            ClientId="agent-client-123",
        )

        # Verify DynamoDB record was deleted
        mock_dynamodb.Table.return_value.delete_item.assert_called_once_with(Key={"client_id": "agent-client-123"})

    @pytest.mark.asyncio
    async def test_delete_agent_not_found(self, agent_service, mock_dynamodb):
        """Test deleting non-existent agent."""
        mock_dynamodb.Table.return_value.get_item.return_value = {}

        with pytest.raises(NotFoundError):
            await agent_service.delete_agent("nonexistent", "org-123")
