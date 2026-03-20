"""
Pydantic schemas for Agent (Cognito App Client) management.

Issue #119: Unified Cognito JWT Auth
- Agents authenticate via client_credentials flow
- Each agent is a Cognito App Client with a client_id and client_secret
- Agent metadata (org_id, team_id) is stored in DynamoDB for the Pre Token Generation Lambda
"""

from datetime import datetime

from pydantic import BaseModel, Field


class AgentCreateRequest(BaseModel):
    """Request to create a new agent (Cognito App Client)."""

    name: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="Human-readable name for the agent",
        examples=["my-agent", "data-pipeline-worker"],
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
        description="Team ID the agent belongs to (optional)",
    )
    department_id: str | None = Field(
        default=None,
        max_length=255,
        description="Department ID the agent belongs to (optional)",
    )
    description: str | None = Field(
        default=None,
        max_length=512,
        description="Description of the agent's purpose",
    )
    scopes: list[str] = Field(
        default=["bedrockgw/invoke"],
        description="OAuth2 scopes for the agent (default: invoke only)",
    )


class AgentResponse(BaseModel):
    """Response containing agent information."""

    client_id: str = Field(..., description="Cognito App Client ID")
    name: str = Field(..., description="Agent name")
    org_id: str = Field(..., description="Organization ID")
    team_id: str | None = Field(default=None, description="Team ID")
    department_id: str | None = Field(default=None, description="Department ID")
    description: str | None = Field(default=None, description="Agent description")
    scopes: list[str] = Field(default=[], description="Allowed OAuth2 scopes")
    created_at: datetime = Field(..., description="Creation timestamp")
    updated_at: datetime | None = Field(default=None, description="Last update timestamp")
    status: str = Field(default="active", description="Agent status (active, disabled)")


class AgentCredentialsResponse(BaseModel):
    """Response containing agent credentials (one-time view)."""

    client_id: str = Field(..., description="Cognito App Client ID")
    client_secret: str = Field(..., description="Cognito App Client Secret (store securely)")
    token_endpoint: str = Field(
        ...,
        description="OAuth2 token endpoint for client_credentials flow",
    )
    scopes: list[str] = Field(..., description="Allowed OAuth2 scopes")

    # Example usage
    example_curl: str = Field(
        ...,
        description="Example curl command to obtain a token",
    )


class AgentListResponse(BaseModel):
    """Response containing a list of agents."""

    items: list[AgentResponse] = Field(default=[], description="List of agents")
    total: int = Field(..., description="Total number of agents")
    page: int = Field(default=1, description="Current page number")
    page_size: int = Field(default=50, description="Items per page")
    has_more: bool = Field(default=False, description="Whether more items exist")


class AgentUpdateRequest(BaseModel):
    """Request to update an existing agent."""

    name: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        description="New name for the agent",
    )
    team_id: str | None = Field(
        default=None,
        max_length=255,
        description="New team ID",
    )
    department_id: str | None = Field(
        default=None,
        max_length=255,
        description="New department ID",
    )
    description: str | None = Field(
        default=None,
        max_length=512,
        description="New description",
    )
    status: str | None = Field(
        default=None,
        pattern="^(active|disabled)$",
        description="Agent status (active or disabled)",
    )
