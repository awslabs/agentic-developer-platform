"""
Pydantic schemas for Agent Onboarding Orchestrator.

Issue #250: Epic 11 - US 3 - Unit 1 - Agent Onboarding Orchestrator (BYO IAM Role)
Issue #262: Refactored to support workflow-created K8s resources

The onboarding orchestrator takes a developer-provided IAM role ARN and:
1. Validates the role exists in AWS
2. Registers the agent in DynamoDB
3. Creates budget config in Postgres (if budget_monthly_usd provided)
4. Returns configuration for the workflow to create K8s resources

K8s operations (namespace, ServiceAccount, Helm install) are handled by
the onboarding workflow (.github/workflows/agent-onboard.yml).
"""

from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field, field_validator

from src.admin.agent_registry_schemas import ROLE_ARN_PATTERN


class AgentOnboardRequest(BaseModel):
    """Request to onboard a new agent to the platform."""

    agent_name: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="Human-readable name for the agent (used in K8s ServiceAccount name and runs-on label)",
        examples=["my-deployer", "data-pipeline-worker"],
    )
    role_arn: str = Field(
        ...,
        description="Developer-provided IAM role ARN (Bring Your Own Role)",
        examples=["arn:aws:iam::123456789012:role/my-agent-role"],
    )
    org_id: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Organization ID the agent belongs to",
    )
    team_id: str | None = Field(
        default=None,
        max_length=255,
        description="Team ID the agent belongs to (used for namespace: arc-runners-{team})",
    )
    owner: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Owner of the agent (user ID or identifier)",
    )
    level: str = Field(
        default="team",
        pattern="^(org|team|personal)$",
        description="Agent level: 'org', 'team', or 'personal'",
    )
    agent_type: str = Field(
        default="general",
        max_length=64,
        description="Type of agent (e.g., 'deployer', 'ci-runner', 'data-pipeline')",
    )
    budget_monthly_usd: Decimal | None = Field(
        default=None,
        gt=0,
        description="Monthly budget in USD. If provided, creates a budget_config in Postgres",
    )
    allowed_models: list[str] = Field(
        default=[],
        description="List of allowed model names for this agent",
    )
    app_name: str | None = Field(
        default=None,
        max_length=255,
        description="Application name this agent serves (optional)",
    )
    description: str | None = Field(
        default=None,
        max_length=1024,
        description="Description of the agent's purpose",
    )
    image_uri: str | None = Field(
        default=None,
        max_length=2048,
        description="Container image URI (e.g., ECR) (optional)",
    )
    code_repo: str | None = Field(
        default=None,
        max_length=512,
        description="GitHub repo + code path (optional)",
    )
    workflow_name: str | None = Field(
        default=None,
        max_length=255,
        description="GitHub Actions workflow name/ID (optional)",
    )
    # New fields for Issue #262: GitHub scope configuration
    github_scope: str = Field(
        default="repo",
        pattern="^(repo|org)$",
        description="Runner scope: 'repo' (single repo) or 'org' (all repos in org)",
    )
    github_repo: str | None = Field(
        default=None,
        max_length=255,
        description="Target repo for repo-scoped runners (e.g., 'my-repo')",
    )
    model: str = Field(
        default="global.anthropic.claude-sonnet-4-20250514",
        max_length=255,
        description="Default model for the agent",
    )

    @field_validator("role_arn")
    @classmethod
    def validate_role_arn(cls, v: str) -> str:
        """Validate IAM role ARN format."""
        if not ROLE_ARN_PATTERN.match(v):
            raise ValueError("Invalid role ARN format. Expected: arn:aws:iam::ACCOUNT_ID:role/ROLE_NAME")
        return v

    @field_validator("agent_name")
    @classmethod
    def validate_agent_name_k8s_safe(cls, v: str) -> str:
        """Validate agent_name is safe for K8s ServiceAccount naming."""
        import re

        # K8s names must be lowercase, alphanumeric, and may contain '-'
        k8s_name_pattern = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
        if not k8s_name_pattern.match(v.lower()):
            raise ValueError("agent_name must be valid for K8s naming: lowercase, alphanumeric, may contain '-', must start/end with alphanumeric")
        return v


class IRSATrustPolicySnippet(BaseModel):
    """IRSA trust policy snippet for the developer to add to their IAM role."""

    Effect: str = Field(default="Allow", description="IAM policy effect")
    Principal: dict[str, str] = Field(..., description="Federated principal (EKS OIDC provider)")
    Action: str = Field(default="sts:AssumeRoleWithWebIdentity", description="STS action")
    Condition: dict[str, dict[str, str]] = Field(..., description="StringEquals condition for service account")


class AgentOnboardResponse(BaseModel):
    """Response from successful agent onboarding.

    Issue #262: Added runs_on_label, scale_set_name, team_namespace for
    workflow-created K8s resources.
    """

    agent_id: str = Field(..., description="Generated agent UUID (primary key in DynamoDB)")
    service_account_name: str = Field(
        ...,
        description="K8s ServiceAccount name (e.g., 'agent-my-deployer-sa')",
    )
    runs_on_label: str = Field(
        ...,
        description="GitHub Actions runs-on label (e.g., 'arc-runner-my-deployer')",
    )
    scale_set_name: str = Field(
        ...,
        description="Helm release name for the runner scale set (e.g., 'arc-runner-my-deployer')",
    )
    team_namespace: str = Field(
        ...,
        description="K8s namespace for the team's runners (e.g., 'arc-runners-my-team')",
    )
    api_gateway_invoke_url: str = Field(
        ...,
        description="API Gateway invoke URL for the agent to use",
    )
    irsa_trust_policy_snippet: dict[str, Any] = Field(
        ...,
        description="IRSA trust policy snippet the developer needs to add to their IAM role",
    )
    budget_config_id: str | None = Field(
        default=None,
        description="Budget config ID if budget_monthly_usd was provided",
    )
    message: str = Field(
        default="Agent onboarded successfully",
        description="Human-readable status message",
    )
