"""
Unit tests for Agent Onboarding Orchestrator (BYO IAM Role).

Issue #250: Epic 11 - US 3 - Unit 1
Issue #262: Refactored - K8s SA creation moved to workflow

Test Coverage:
- Full onboard flow: validate role, register in DynamoDB, return config
- Role doesn't exist -> ValidationError
- Duplicate agent_name -> ConflictError
- Cross-account role ARN -> accepted (format validation only)
- Invalid role ARN format -> ValidationError
- Response includes runs_on_label, scale_set_name, team_namespace
"""

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from botocore.exceptions import ClientError
from pydantic import ValidationError

from src.admin.agent_onboarding_schemas import (
    AgentOnboardRequest,
    AgentOnboardResponse,
)
from src.admin.agent_onboarding_service import AgentOnboardingService
from src.shared.exceptions import ConflictError
from src.shared.exceptions import ValidationError as AppValidationError

# =============================================================================
# Schema Validation Tests
# =============================================================================


class TestAgentOnboardRequestSchema:
    """Tests for AgentOnboardRequest Pydantic schema."""

    def test_valid_request(self):
        """Test valid onboard request."""
        request = AgentOnboardRequest(
            agent_name="my-deployer",
            role_arn="arn:aws:iam::123456789012:role/my-deployer-role",
            org_id="org-123",
            owner="user-456",
        )
        assert request.agent_name == "my-deployer"
        assert request.level == "team"  # Default
        assert request.agent_type == "general"  # Default
        assert request.budget_monthly_usd is None
        assert request.github_scope == "repo"  # Default
        assert request.model == "global.anthropic.claude-sonnet-4-20250514"  # Default

    def test_valid_request_with_all_fields(self):
        """Test valid onboard request with all optional fields."""
        request = AgentOnboardRequest(
            agent_name="data-pipeline",
            role_arn="arn:aws:iam::123456789012:role/data-pipeline-role",
            org_id="org-123",
            team_id="team-456",
            owner="user-789",
            level="org",
            agent_type="data-pipeline",
            budget_monthly_usd=Decimal("100.00"),
            allowed_models=["claude-sonnet", "claude-haiku"],
            app_name="DataPipelineApp",
            description="Data processing pipeline agent",
            image_uri="123456789012.dkr.ecr.us-east-1.amazonaws.com/my-agent:latest",
            code_repo="github.com/org/repo",
            workflow_name="deploy.yml",
            github_scope="org",
            github_repo="my-repo",
            model="global.anthropic.claude-opus-4-20250514",
        )
        assert request.team_id == "team-456"
        assert request.level == "org"
        assert request.budget_monthly_usd == Decimal("100.00")
        assert request.github_scope == "org"
        assert request.github_repo == "my-repo"
        assert request.model == "global.anthropic.claude-opus-4-20250514"

    def test_invalid_role_arn_format(self):
        """Test invalid role ARN format raises ValidationError."""
        with pytest.raises(ValidationError) as exc_info:
            AgentOnboardRequest(
                agent_name="my-agent",
                role_arn="invalid-arn",
                org_id="org-123",
                owner="user-456",
            )
        assert "role_arn" in str(exc_info.value)

    def test_invalid_role_arn_not_a_role(self):
        """Test IAM user ARN (not role) raises ValidationError."""
        with pytest.raises(ValidationError) as exc_info:
            AgentOnboardRequest(
                agent_name="my-agent",
                role_arn="arn:aws:iam::123456789012:user/my-user",
                org_id="org-123",
                owner="user-456",
            )
        assert "role_arn" in str(exc_info.value)

    def test_invalid_agent_name_for_k8s(self):
        """Test agent name with invalid K8s characters raises ValidationError."""
        with pytest.raises(ValidationError) as exc_info:
            AgentOnboardRequest(
                agent_name="My Agent With Spaces",
                role_arn="arn:aws:iam::123456789012:role/my-role",
                org_id="org-123",
                owner="user-456",
            )
        assert "agent_name" in str(exc_info.value)

    def test_agent_name_k8s_valid_with_dashes(self):
        """Test agent name with dashes is valid."""
        request = AgentOnboardRequest(
            agent_name="my-agent-with-dashes",
            role_arn="arn:aws:iam::123456789012:role/my-role",
            org_id="org-123",
            owner="user-456",
        )
        assert request.agent_name == "my-agent-with-dashes"

    def test_invalid_level(self):
        """Test invalid level raises ValidationError."""
        with pytest.raises(ValidationError) as exc_info:
            AgentOnboardRequest(
                agent_name="my-agent",
                role_arn="arn:aws:iam::123456789012:role/my-role",
                org_id="org-123",
                owner="user-456",
                level="invalid-level",
            )
        assert "level" in str(exc_info.value)

    def test_budget_must_be_positive(self):
        """Test negative budget raises ValidationError."""
        with pytest.raises(ValidationError) as exc_info:
            AgentOnboardRequest(
                agent_name="my-agent",
                role_arn="arn:aws:iam::123456789012:role/my-role",
                org_id="org-123",
                owner="user-456",
                budget_monthly_usd=Decimal("-10.00"),
            )
        assert "budget_monthly_usd" in str(exc_info.value)

    def test_cross_account_role_arn_valid(self):
        """Test cross-account role ARN is accepted."""
        request = AgentOnboardRequest(
            agent_name="cross-account-agent",
            role_arn="arn:aws:iam::999888777666:role/cross-account-role",
            org_id="org-123",
            owner="user-456",
        )
        assert request.role_arn == "arn:aws:iam::999888777666:role/cross-account-role"

    def test_invalid_github_scope(self):
        """Test invalid github_scope raises ValidationError."""
        with pytest.raises(ValidationError) as exc_info:
            AgentOnboardRequest(
                agent_name="my-agent",
                role_arn="arn:aws:iam::123456789012:role/my-role",
                org_id="org-123",
                owner="user-456",
                github_scope="invalid",
            )
        assert "github_scope" in str(exc_info.value)


class TestAgentOnboardResponseSchema:
    """Tests for AgentOnboardResponse Pydantic schema."""

    def test_response_structure(self):
        """Test response schema structure."""
        response = AgentOnboardResponse(
            agent_id="uuid-123-456",
            service_account_name="agent-my-deployer-sa",
            runs_on_label="arc-runner-my-deployer",
            scale_set_name="arc-runner-my-deployer",
            team_namespace="arc-runners-my-team",
            api_gateway_invoke_url="https://api.example.com",
            irsa_trust_policy_snippet={
                "Effect": "Allow",
                "Principal": {"Federated": "arn:aws:iam::123456789012:oidc-provider/..."},
                "Action": "sts:AssumeRoleWithWebIdentity",
                "Condition": {"StringEquals": {"...": "..."}},
            },
            budget_config_id="budget-123",
        )
        assert response.agent_id == "uuid-123-456"
        assert response.service_account_name == "agent-my-deployer-sa"
        assert response.runs_on_label == "arc-runner-my-deployer"
        assert response.scale_set_name == "arc-runner-my-deployer"
        assert response.team_namespace == "arc-runners-my-team"
        assert response.budget_config_id == "budget-123"


# =============================================================================
# AgentOnboardingService Tests
# =============================================================================


@pytest.fixture
def mock_iam_client():
    """Create a mock IAM client."""
    client = MagicMock()
    client.get_role.return_value = {"Role": {"RoleName": "test-role", "Arn": "arn:aws:iam::123456789012:role/test-role"}}
    return client


@pytest.fixture
def mock_sts_client():
    """Create a mock STS client."""
    client = MagicMock()
    client.get_caller_identity.return_value = {"Account": "123456789012"}
    return client


@pytest.fixture
def mock_agent_registry_service():
    """Create a mock AgentRegistryService."""
    from datetime import UTC, datetime

    from src.admin.agent_registry_schemas import AgentRegistryResponse

    service = MagicMock()
    service.create_agent = AsyncMock(
        return_value=AgentRegistryResponse(
            agent_id="agent-uuid-123",
            agent_name="test-agent",
            role_arn="arn:aws:iam::123456789012:role/test-role",
            org_id="org-123",
            owner="user-456",
            scope="shared",
            status="active",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
    )
    service.get_agent_by_role = AsyncMock(return_value=None)
    return service


@pytest.fixture
def onboarding_service(mock_iam_client, mock_agent_registry_service):
    """Create an AgentOnboardingService with mocked dependencies."""
    with patch("src.admin.agent_onboarding_service.get_settings") as mock_settings:
        mock_settings.return_value.aws_region = "us-east-1"
        service = AgentOnboardingService(
            iam_client=mock_iam_client,
            agent_registry_service=mock_agent_registry_service,
        )
        service.eks_oidc_provider_arn = "arn:aws:iam::123456789012:oidc-provider/oidc.eks.us-east-1.amazonaws.com/id/TEST"
        service.api_gateway_invoke_url = "https://api.bedrockgateway.example.com"
    return service


class TestAgentOnboardingServiceOnboard:
    """Tests for AgentOnboardingService.onboard_agent()."""

    @pytest.mark.asyncio
    async def test_onboard_success(self, onboarding_service, mock_sts_client):
        """Test successful agent onboarding."""
        with patch("boto3.client") as mock_boto_client:
            mock_boto_client.return_value = mock_sts_client

            request = AgentOnboardRequest(
                agent_name="my-deployer",
                role_arn="arn:aws:iam::123456789012:role/my-deployer-role",
                org_id="org-123",
                owner="user-456",
            )

            result = await onboarding_service.onboard_agent(request)

        assert result.agent_id == "agent-uuid-123"
        assert result.service_account_name == "agent-my-deployer-sa"
        assert result.runs_on_label == "arc-runner-my-deployer"
        assert result.scale_set_name == "arc-runner-my-deployer"
        assert result.team_namespace == "arc-runners-org-123"
        assert "irsa_trust_policy_snippet" in result.model_dump()

    @pytest.mark.asyncio
    async def test_onboard_with_team_uses_team_namespace(self, onboarding_service, mock_sts_client):
        """Test onboarding with team_id uses team in namespace."""
        with patch("boto3.client") as mock_boto_client:
            mock_boto_client.return_value = mock_sts_client

            request = AgentOnboardRequest(
                agent_name="team-agent",
                role_arn="arn:aws:iam::123456789012:role/team-role",
                org_id="org-123",
                team_id="my-team",
                owner="user-456",
            )

            result = await onboarding_service.onboard_agent(request)

        assert result.team_namespace == "arc-runners-my-team"

    @pytest.mark.asyncio
    async def test_onboard_with_budget(self, onboarding_service, mock_sts_client, mock_agent_registry_service):
        """Test onboarding with budget creates budget config."""
        from datetime import UTC, datetime

        from src.admin.agent_registry_schemas import AgentRegistryResponse

        # Update mock to return budget_config_id
        mock_agent_registry_service.create_agent = AsyncMock(
            return_value=AgentRegistryResponse(
                agent_id="agent-uuid-123",
                agent_name="test-agent",
                role_arn="arn:aws:iam::123456789012:role/test-role",
                org_id="org-123",
                owner="user-456",
                scope="shared",
                status="active",
                budget_config_id="budget-config-456",
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )

        with patch("boto3.client") as mock_boto_client:
            mock_boto_client.return_value = mock_sts_client

            request = AgentOnboardRequest(
                agent_name="budget-agent",
                role_arn="arn:aws:iam::123456789012:role/budget-role",
                org_id="org-123",
                owner="user-456",
                budget_monthly_usd=Decimal("50.00"),
            )

            result = await onboarding_service.onboard_agent(request)

        assert result.budget_config_id == "budget-config-456"

    @pytest.mark.asyncio
    async def test_onboard_cross_account_role(self, onboarding_service, mock_sts_client):
        """Test onboarding with cross-account role skips IAM validation."""
        # STS returns different account ID
        mock_sts_client.get_caller_identity.return_value = {"Account": "111111111111"}

        with patch("boto3.client") as mock_boto_client:
            mock_boto_client.return_value = mock_sts_client

            request = AgentOnboardRequest(
                agent_name="cross-account",
                role_arn="arn:aws:iam::999999999999:role/cross-account-role",
                org_id="org-123",
                owner="user-456",
            )

            result = await onboarding_service.onboard_agent(request)

        assert result.agent_id == "agent-uuid-123"
        # IAM get_role should NOT have been called (cross-account)
        onboarding_service.iam_client.get_role.assert_not_called()

    @pytest.mark.asyncio
    async def test_onboard_conflict_error(self, onboarding_service, mock_sts_client, mock_agent_registry_service):
        """Test onboarding with duplicate agent_name raises ConflictError."""
        mock_agent_registry_service.create_agent.side_effect = ConflictError("Agent already exists")

        with patch("boto3.client") as mock_boto_client:
            mock_boto_client.return_value = mock_sts_client

            request = AgentOnboardRequest(
                agent_name="duplicate-agent",
                role_arn="arn:aws:iam::123456789012:role/test-role",
                org_id="org-123",
                owner="user-456",
            )

            with pytest.raises(ConflictError):
                await onboarding_service.onboard_agent(request)


class TestAgentOnboardingServiceValidateRole:
    """Tests for AgentOnboardingService._validate_role()."""

    @pytest.mark.asyncio
    async def test_validate_role_exists(self, onboarding_service, mock_sts_client):
        """Test validation passes when role exists."""
        with patch("boto3.client") as mock_boto_client:
            mock_boto_client.return_value = mock_sts_client

            # Should not raise
            await onboarding_service._validate_role("arn:aws:iam::123456789012:role/test-role")

    @pytest.mark.asyncio
    async def test_validate_role_not_found(self, onboarding_service, mock_sts_client):
        """Test validation fails when role doesn't exist."""
        # IAM returns NoSuchEntity error
        error_response = {"Error": {"Code": "NoSuchEntity", "Message": "Role not found"}}
        onboarding_service.iam_client.get_role.side_effect = ClientError(error_response, "GetRole")

        with patch("boto3.client") as mock_boto_client:
            mock_boto_client.return_value = mock_sts_client

            with pytest.raises(AppValidationError) as exc_info:
                await onboarding_service._validate_role("arn:aws:iam::123456789012:role/nonexistent-role")

            assert "does not exist" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_validate_role_invalid_arn_format(self, onboarding_service):
        """Test validation fails for invalid ARN format."""
        with pytest.raises(AppValidationError) as exc_info:
            await onboarding_service._validate_role("invalid-arn")

        assert "Invalid role ARN format" in str(exc_info.value)


class TestAgentOnboardingServiceIRSATrustPolicy:
    """Tests for IRSA trust policy snippet generation."""

    def test_build_irsa_trust_policy_snippet(self, onboarding_service):
        """Test IRSA trust policy snippet structure."""
        snippet = onboarding_service._build_irsa_trust_policy_snippet("agent-my-deployer-sa", "arc-runners-my-team")

        assert snippet["Effect"] == "Allow"
        assert snippet["Action"] == "sts:AssumeRoleWithWebIdentity"
        assert "Federated" in snippet["Principal"]
        assert "StringEquals" in snippet["Condition"]

    def test_irsa_trust_policy_contains_service_account(self, onboarding_service):
        """Test IRSA trust policy includes service account reference."""
        snippet = onboarding_service._build_irsa_trust_policy_snippet("agent-my-deployer-sa", "arc-runners-my-team")

        # The Condition should reference the service account
        condition_str = str(snippet["Condition"])
        assert "agent-my-deployer-sa" in condition_str
        assert "arc-runners-my-team" in condition_str  # namespace


class TestAgentOnboardingServiceLevelToScope:
    """Tests for level to scope conversion."""

    def test_level_personal_to_personal(self, onboarding_service):
        """Test personal level maps to personal scope."""
        assert onboarding_service._level_to_scope("personal") == "personal"

    def test_level_team_to_shared(self, onboarding_service):
        """Test team level maps to shared scope."""
        assert onboarding_service._level_to_scope("team") == "shared"

    def test_level_org_to_shared(self, onboarding_service):
        """Test org level maps to shared scope."""
        assert onboarding_service._level_to_scope("org") == "shared"
