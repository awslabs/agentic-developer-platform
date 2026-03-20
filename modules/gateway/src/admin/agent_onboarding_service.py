"""
Agent Onboarding Service for BYO IAM Role.

Issue #250: Epic 11 - US 3 - Unit 1 - Agent Onboarding Orchestrator
Issue #262: Refactored to remove K8s SA creation (moved to onboarding workflow)

The backend service only handles:
1. Validates the IAM role exists in AWS
2. Registers the agent in DynamoDB
3. Creates budget config in Postgres (if budget_monthly_usd provided)
4. Returns configuration for the workflow to create K8s resources

K8s operations (namespace, ServiceAccount, Helm install) are handled by the
onboarding workflow (.github/workflows/agent-onboard.yml) following the
`github-actions-runner/scripts/onboard-repo.sh` pattern.
"""

import asyncio
import logging
import os
import re
from typing import Any

import boto3
from botocore.exceptions import ClientError

from src.admin.agent_onboarding_schemas import AgentOnboardRequest, AgentOnboardResponse
from src.admin.agent_registry_schemas import AgentRegistryCreateRequest
from src.admin.agent_registry_service import AgentRegistryService
from src.shared.config import get_settings
from src.shared.exceptions import ValidationError

logger = logging.getLogger(__name__)


class AgentOnboardingService:
    """
    Service for orchestrating agent onboarding with BYO IAM Role.

    The platform does NOT create IAM roles. Developers bring their own roles
    via Terraform or the AWS console. The platform validates the role exists,
    registers in DynamoDB, and sets up budget config.

    K8s operations (ServiceAccount, runner scale set) are handled by the
    onboarding workflow, not this service.
    """

    def __init__(
        self,
        iam_client=None,
        agent_registry_service: AgentRegistryService | None = None,
    ):
        """
        Initialize the agent onboarding service.

        Args:
            iam_client: Boto3 IAM client (optional, for mocking)
            agent_registry_service: AgentRegistryService instance (optional, for mocking)
        """
        settings = get_settings()
        self.region = settings.aws_region

        # IAM client for role validation
        self.iam_client = iam_client or boto3.client("iam", region_name=self.region)

        # Agent registry service for DynamoDB operations
        self.agent_registry_service = agent_registry_service or AgentRegistryService()

        # EKS OIDC provider ARN (for IRSA trust policy snippet)
        self.eks_oidc_provider_arn = os.environ.get("EKS_OIDC_PROVIDER_ARN", "")

        # API Gateway invoke URL
        self.api_gateway_invoke_url = os.environ.get(
            "API_GATEWAY_INVOKE_URL",
            "https://api.bedrockgateway.example.com",
        )

    async def onboard_agent(self, request: AgentOnboardRequest) -> AgentOnboardResponse:
        """
        Onboard a new agent to the platform.

        This is the main orchestrator method that:
        1. Validates the IAM role exists
        2. Creates budget config in Postgres (if budget_monthly_usd provided)
        3. Registers in DynamoDB
        4. Returns configuration for workflow to create K8s resources

        K8s operations (namespace, ServiceAccount, Helm install) are handled
        by the onboarding workflow, not this service.

        Args:
            request: AgentOnboardRequest with agent details

        Returns:
            AgentOnboardResponse with agent_id, runs_on_label, etc.

        Raises:
            ValidationError: If role doesn't exist or validation fails
            ConflictError: If agent with same name or role_arn already exists
        """
        logger.info(f"Starting agent onboarding: {request.agent_name} for org: {request.org_id}")

        # Step 1: Validate the IAM role exists
        await self._validate_role(request.role_arn)

        # Generate K8s ServiceAccount name and runs-on label for workflow
        sa_name = f"agent-{request.agent_name.lower()}-sa"
        runs_on_label = f"arc-runner-{request.agent_name.lower()}"
        scale_set_name = f"arc-runner-{request.agent_name.lower()}"
        team_namespace = f"arc-runners-{(request.team_id or request.org_id).lower().replace('_', '-')}"

        # Step 2 & 3: Register in DynamoDB via AgentRegistryService
        # Budget creation (if budget_monthly_usd provided) is also handled with rollback
        registry_request = AgentRegistryCreateRequest(
            agent_name=request.agent_name,
            role_arn=request.role_arn,
            org_id=request.org_id,
            team_id=request.team_id,
            owner=request.owner,
            scope=self._level_to_scope(request.level),
            budget_monthly_usd=request.budget_monthly_usd,
            allowed_models=request.allowed_models,
            description=request.description,
            image_uri=request.image_uri,
            code_repo=request.code_repo,
            workflow_name=request.workflow_name,
        )

        agent_response = await self.agent_registry_service.create_agent(registry_request)
        logger.info(f"Registered agent in DynamoDB: {agent_response.agent_id}")

        # Build IRSA trust policy snippet for the developer
        irsa_trust_policy = self._build_irsa_trust_policy_snippet(sa_name, team_namespace)

        return AgentOnboardResponse(
            agent_id=agent_response.agent_id,
            service_account_name=sa_name,
            runs_on_label=runs_on_label,
            scale_set_name=scale_set_name,
            team_namespace=team_namespace,
            api_gateway_invoke_url=self.api_gateway_invoke_url,
            irsa_trust_policy_snippet=irsa_trust_policy,
            budget_config_id=agent_response.budget_config_id,
            message="Agent registered. Workflow will create K8s resources (namespace, ServiceAccount, runner scale set).",
        )

    async def _validate_role(self, role_arn: str) -> None:
        """
        Validate that the IAM role exists.

        For cross-account roles, we just validate the ARN format and trust
        the developer. Auth will fail at runtime if the role is invalid.

        Args:
            role_arn: IAM role ARN to validate

        Raises:
            ValidationError: If role doesn't exist (same account) or ARN format is invalid
        """
        # Extract role name from ARN
        # Format: arn:aws:iam::ACCOUNT_ID:role/ROLE_NAME or arn:aws:iam::ACCOUNT_ID:role/path/to/ROLE_NAME
        match = re.match(r"^arn:aws:iam::(\d{12}):role/(.+)$", role_arn)
        if not match:
            raise ValidationError(f"Invalid role ARN format: {role_arn}")

        account_id = match.group(1)
        role_name = match.group(2)

        # Get current account ID to check if this is a cross-account role
        try:
            sts_client = boto3.client("sts", region_name=self.region)
            current_identity = await asyncio.to_thread(sts_client.get_caller_identity)
            current_account_id = current_identity["Account"]
        except Exception as e:
            logger.warning(f"Could not get current account ID: {e}")
            # If we can't get current account, skip validation (trust the ARN)
            return

        # For cross-account roles, accept the format and let auth fail at runtime if invalid
        if account_id != current_account_id:
            logger.info(f"Cross-account role detected (account: {account_id}), skipping IAM validation")
            return

        # For same-account roles, validate the role exists
        try:
            await asyncio.to_thread(self.iam_client.get_role, RoleName=role_name)
            logger.info(f"Validated IAM role exists: {role_name}")
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            if error_code == "NoSuchEntity":
                raise ValidationError(f"IAM role does not exist: {role_name}")
            # For other errors (permissions, etc.), let auth fail at runtime
            logger.warning(f"Could not validate IAM role {role_name}: {e}")

    def _build_irsa_trust_policy_snippet(self, sa_name: str, namespace: str) -> dict[str, Any]:
        """
        Build the IRSA trust policy snippet for the developer.

        The developer needs to add this to their IAM role's trust policy
        to allow the K8s ServiceAccount to assume the role.

        Args:
            sa_name: ServiceAccount name
            namespace: K8s namespace where the ServiceAccount will be created

        Returns:
            dict: IRSA trust policy statement
        """
        # Extract OIDC provider URL from ARN
        # ARN format: arn:aws:iam::ACCOUNT:oidc-provider/oidc.eks.REGION.amazonaws.com/id/CLUSTER_ID
        oidc_provider_url = ""
        if self.eks_oidc_provider_arn:
            match = re.match(r"^arn:aws:iam::\d+:oidc-provider/(.+)$", self.eks_oidc_provider_arn)
            if match:
                oidc_provider_url = match.group(1)

        # Default to a placeholder if not configured
        if not oidc_provider_url:
            oidc_provider_url = "oidc.eks.us-east-1.amazonaws.com/id/EXAMPLE_CLUSTER_ID"

        if not self.eks_oidc_provider_arn:
            # Use a placeholder ARN
            self.eks_oidc_provider_arn = f"arn:aws:iam::000000000000:oidc-provider/{oidc_provider_url}"

        return {
            "Effect": "Allow",
            "Principal": {
                "Federated": self.eks_oidc_provider_arn,
            },
            "Action": "sts:AssumeRoleWithWebIdentity",
            "Condition": {
                "StringEquals": {
                    f"{oidc_provider_url}:sub": f"system:serviceaccount:{namespace}:{sa_name}",
                    f"{oidc_provider_url}:aud": "sts.amazonaws.com",
                },
            },
        }

    def _level_to_scope(self, level: str) -> str:
        """
        Convert agent level to scope for DynamoDB.

        Args:
            level: Agent level (org, team, personal)

        Returns:
            Scope value (shared or personal)
        """
        if level == "personal":
            return "personal"
        return "shared"  # org and team both map to shared
