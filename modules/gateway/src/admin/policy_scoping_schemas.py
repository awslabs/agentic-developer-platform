"""Pydantic schemas for the Policy Scoping Service API.

Issue #255: These schemas define the request/response models for the policy
preview endpoint and other policy-related operations.
"""

from typing import Literal

from pydantic import BaseModel, Field


class PolicyPreviewRequest(BaseModel):
    """Request schema for previewing generated IAM policies.

    This request contains all the attributes needed to generate the three-layer
    policy artifacts: permission boundary, agent policy, and trust policy.
    """

    level: Literal["platform", "org", "team", "app", "user"] = Field(
        ...,
        description="Hierarchy level for the agent",
    )
    agent_type: Literal["infra", "app-dev", "monitoring", "security", "general"] = Field(
        ...,
        description="Type of agent determining AWS action permissions",
    )
    hierarchy: dict[str, str] = Field(
        ...,
        description="Hierarchy values. Required keys depend on level. "
        "Example for app level: {platform: 'bedrockgw', org: 'engineering', team: 'platform', app: 'payments'}",
        examples=[
            {
                "platform": "bedrockgw",
                "org": "engineering",
                "team": "platform",
                "app": "payments",
            }
        ],
    )
    region: str = Field(
        default="us-east-1",
        description="AWS region for resource ARNs",
    )
    account_id: str = Field(
        ...,
        pattern=r"^\d{12}$",
        description="12-digit AWS account ID",
    )
    api_gateway_arn: str = Field(
        ...,
        description="API Gateway ARN for gateway access statement. Example: arn:aws:execute-api:us-east-1:123456789012:abc123/*",
    )
    # Optional IRSA configuration
    oidc_provider_arn: str | None = Field(
        None,
        description="OIDC provider ARN for IRSA trust policy (optional)",
    )
    oidc_issuer: str | None = Field(
        None,
        description="OIDC issuer URL without https:// (optional)",
    )
    namespace: str | None = Field(
        None,
        description="Kubernetes namespace for IRSA (optional)",
    )
    service_account_name: str | None = Field(
        None,
        description="Kubernetes ServiceAccount name for IRSA (optional)",
    )


class PolicyPreviewResponse(BaseModel):
    """Response schema for policy preview endpoint.

    Contains all three policy artifacts that would be generated for an agent
    with the given attributes.
    """

    resource_prefix: str = Field(
        ...,
        description="Generated resource prefix from hierarchy",
    )
    permission_boundary: dict = Field(
        ...,
        description="IAM Permission Boundary policy document (hard ceiling)",
    )
    agent_policy: dict = Field(
        ...,
        description="IAM policy document with agent-specific permissions",
    )
    trust_policy: dict | None = Field(
        None,
        description="IRSA trust policy document (only if OIDC config provided)",
    )
    required_tags: dict[str, str] = Field(
        ...,
        description="Tags that must be applied to resources created by the agent",
    )
    tag_enforcement_statements: list[dict] = Field(
        ...,
        description="IAM policy statements for tag enforcement (to be combined with agent policy)",
    )


class ResourcePrefixResponse(BaseModel):
    """Response schema for resource prefix generation.

    Simple response for testing prefix generation without full policy output.
    """

    prefix: str = Field(
        ...,
        description="Generated resource prefix",
        examples=["bgw-eng-plat-pay-"],
    )
    level: str = Field(
        ...,
        description="Hierarchy level used",
    )
    hierarchy: dict[str, str] = Field(
        ...,
        description="Hierarchy values used",
    )


class AgentTypeActionsResponse(BaseModel):
    """Response schema for listing available agent types and their actions.

    Useful for UI that needs to display what permissions each agent type has.
    """

    agent_type: str = Field(
        ...,
        description="Agent type name",
    )
    actions: list[str] = Field(
        ...,
        description="List of AWS actions allowed for this agent type",
    )
    description: str = Field(
        ...,
        description="Description of what this agent type is for",
    )


class AgentTypesListResponse(BaseModel):
    """Response schema for listing all agent types."""

    agent_types: list[AgentTypeActionsResponse] = Field(
        ...,
        description="List of all available agent types with their actions",
    )
