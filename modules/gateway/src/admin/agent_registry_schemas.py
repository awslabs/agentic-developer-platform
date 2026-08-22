"""
Pydantic schemas for Agent Registry (DynamoDB) management.

Issue #248: Admin API for Agent Registry
- Agents authenticate via IAM/SigV4
- Each agent has a UUID-based agent_id as primary identity
- role_arn is a separate updatable field with GSI for Lambda authorizer lookup
- budget_config_id references budget_configs table in Postgres

Issue #249: Per-Agent Budget Assignment and Usage Dashboard
- Added budget_monthly_usd for automatic budget creation
- Added usage response schemas for agent dashboard
"""

import re
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field, field_validator

# Regular expression for validating IAM role ARN
ROLE_ARN_PATTERN = re.compile(r"^arn:aws:iam::\d{12}:role/.+$")


def role_name_from_arn(role_arn: str) -> str:
    """Extract the bare role NAME from an IAM role ARN.

    IAM role ARNs may carry a path (``arn:aws:iam::123:role/some/path/MyRole``);
    the role name is the last segment. Returns an empty string for anything that
    is not a role ARN.
    """
    if ":role/" not in role_arn:
        return ""
    return role_arn.split(":role/", 1)[1].rsplit("/", 1)[-1]


def is_reserved_role_arn(role_arn: str) -> bool:
    """Return True if *role_arn* names a platform-reserved IAM role.

    Issue #3989: an agent-registry row resolves any registered ``role_arn`` to an
    authenticated ``service`` identity in that row's org
    (``src/auth/agent_registry.py``), and ``role_arn`` is unique across the
    table. Registering a platform-owned role would therefore (a) bind the
    platform's own role — e.g. the CI runner role — into an org of the
    registrant's choosing, and (b) squat the ARN so the legitimate row can never
    be created.

    This is a hard denylist: it is enforced on every registry write regardless of
    caller privilege, because there is no legitimate reason to register a
    platform-owned role as a tenant agent. Note that a same-account
    ``iam:GetRole`` existence check would NOT catch this — platform roles are
    precisely the same-account roles that pass it.
    """
    role_name = role_name_from_arn(role_arn).lower()
    if not role_name:
        return False

    from src.shared.config import get_settings

    prefixes = [p.strip().lower() for p in (get_settings().reserved_role_name_prefixes or "").split(",")]
    return any(prefix and role_name.startswith(prefix) for prefix in prefixes)


class AgentRegistryCreateRequest(BaseModel):
    """Request to create a new agent in the DynamoDB registry."""

    agent_name: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="Human-readable name for the agent",
        examples=["my-agent", "data-pipeline-worker"],
    )
    role_arn: str = Field(
        ...,
        description="IAM role ARN for the agent (arn:aws:iam::ACCOUNT:role/ROLE_NAME)",
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
        description="Team ID the agent belongs to (optional)",
    )
    owner: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Owner of the agent (user ID or identifier)",
    )
    scope: str = Field(
        default="shared",
        pattern="^(shared|personal)$",
        description="Agent scope: 'shared' or 'personal'",
    )
    budget_config_id: str | None = Field(
        default=None,
        max_length=255,
        description="Budget configuration ID (references budget_configs in Postgres)",
    )
    budget_monthly_usd: Decimal | None = Field(
        default=None,
        gt=0,
        description="Monthly budget in USD. If provided, auto-creates a budget_config (Issue #249)",
    )
    allowed_models: list[str] = Field(
        default=[],
        description="List of allowed model names for this agent",
    )
    description: str | None = Field(
        default=None,
        max_length=1024,
        description="Description of the agent's purpose",
    )
    image_uri: str | None = Field(
        default=None,
        max_length=2048,
        description="Base container image URI (e.g., ECR)",
    )
    code_repo: str | None = Field(
        default=None,
        max_length=512,
        description="GitHub repo + code path",
    )
    workflow_name: str | None = Field(
        default=None,
        max_length=255,
        description="GitHub Actions workflow name/ID",
    )

    @field_validator("role_arn")
    @classmethod
    def validate_role_arn(cls, v: str) -> str:
        """Validate IAM role ARN format."""
        if not ROLE_ARN_PATTERN.match(v):
            raise ValueError("Invalid role ARN format. Expected: arn:aws:iam::ACCOUNT_ID:role/ROLE_NAME")
        return v


class AgentRegistryResponse(BaseModel):
    """Response containing agent registry information."""

    agent_id: str = Field(..., description="Agent UUID (primary key)")
    agent_name: str = Field(..., description="Agent name")
    role_arn: str = Field(..., description="IAM role ARN")
    org_id: str = Field(..., description="Organization ID")
    team_id: str | None = Field(default=None, description="Team ID")
    owner: str = Field(..., description="Owner identifier")
    scope: str = Field(default="shared", description="Agent scope (shared/personal)")
    budget_config_id: str | None = Field(default=None, description="Budget configuration ID")
    allowed_models: list[str] = Field(default=[], description="Allowed model names")
    status: str = Field(default="active", description="Agent status (active/disabled)")
    description: str | None = Field(default=None, description="Agent description")
    image_uri: str | None = Field(default=None, description="Container image URI")
    code_repo: str | None = Field(default=None, description="GitHub repo + code path")
    workflow_name: str | None = Field(default=None, description="GitHub Actions workflow")
    created_at: datetime = Field(..., description="Creation timestamp")
    updated_at: datetime = Field(..., description="Last update timestamp")


class AgentRegistryUpdateRequest(BaseModel):
    """Request to update an existing agent in the registry."""

    agent_name: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        description="New name for the agent",
    )
    role_arn: str | None = Field(
        default=None,
        description="New IAM role ARN (can be updated if role changes)",
    )
    team_id: str | None = Field(
        default=None,
        max_length=255,
        description="New team ID",
    )
    owner: str | None = Field(
        default=None,
        max_length=255,
        description="New owner",
    )
    scope: str | None = Field(
        default=None,
        pattern="^(shared|personal)$",
        description="New scope (shared/personal)",
    )
    budget_config_id: str | None = Field(
        default=None,
        max_length=255,
        description="New budget configuration ID",
    )
    allowed_models: list[str] | None = Field(
        default=None,
        description="New list of allowed models",
    )
    status: str | None = Field(
        default=None,
        pattern="^(active|disabled)$",
        description="Agent status (active or disabled)",
    )
    description: str | None = Field(
        default=None,
        max_length=1024,
        description="New description",
    )
    image_uri: str | None = Field(
        default=None,
        max_length=2048,
        description="New container image URI",
    )
    code_repo: str | None = Field(
        default=None,
        max_length=512,
        description="New GitHub repo + code path",
    )
    workflow_name: str | None = Field(
        default=None,
        max_length=255,
        description="New GitHub Actions workflow name/ID",
    )

    @field_validator("role_arn")
    @classmethod
    def validate_role_arn(cls, v: str | None) -> str | None:
        """Validate IAM role ARN format if provided."""
        if v is not None and not ROLE_ARN_PATTERN.match(v):
            raise ValueError("Invalid role ARN format. Expected: arn:aws:iam::ACCOUNT_ID:role/ROLE_NAME")
        return v


class AgentRegistryListResponse(BaseModel):
    """Response containing a list of agents from the registry."""

    items: list[AgentRegistryResponse] = Field(default=[], description="List of agents")
    count: int = Field(..., description="Number of agents returned in this page (not total)")
    last_key: str | None = Field(
        default=None,
        description="Pagination token for the next page (base64-encoded DynamoDB key)",
    )


# =============================================================================
# Agent Usage Response Schemas (Issue #249)
# =============================================================================


class AgentUsageByModel(BaseModel):
    """Usage breakdown for a single model."""

    model_id: str = Field(..., description="Model identifier")
    requests: int = Field(..., description="Number of requests")
    input_tokens: int = Field(..., description="Total input tokens")
    output_tokens: int = Field(..., description="Total output tokens")
    cost_usd: Decimal = Field(..., description="Total cost in USD")


class AgentBudgetStatus(BaseModel):
    """Budget status for an agent."""

    monthly_limit_usd: Decimal = Field(..., description="Monthly budget limit in USD")
    used_usd: Decimal = Field(..., description="Amount used in current period")
    remaining_usd: Decimal = Field(..., description="Remaining budget")
    utilization_pct: float = Field(..., description="Budget utilization percentage")


class AgentUsageResponse(BaseModel):
    """Response containing agent usage data.

    Issue #249: Per-Agent Budget Assignment and Usage Dashboard
    """

    agent_id: str = Field(..., description="Agent UUID")
    agent_name: str = Field(..., description="Agent name")
    period: str = Field(..., description="Aggregation period (daily/weekly/monthly)")
    total_requests: int = Field(..., description="Total number of requests")
    total_input_tokens: int = Field(..., description="Total input tokens")
    total_output_tokens: int = Field(..., description="Total output tokens")
    total_cost_usd: Decimal = Field(..., description="Total cost in USD")
    by_model: list[AgentUsageByModel] = Field(default=[], description="Usage breakdown by model")
    budget: AgentBudgetStatus | None = Field(default=None, description="Budget status if configured")
