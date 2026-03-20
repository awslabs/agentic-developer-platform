from datetime import date, datetime
from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, Field


class PeriodType(str, Enum):
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"


class EntityType(str, Enum):
    ORGANIZATION = "org"
    DEPARTMENT = "department"
    TEAM = "team"
    USER = "user"
    SERVICE_ACCOUNT = "service_account"
    AGENT = "agent"  # IAM-authenticated agents (Issue #249)


class EnforcementMode(str, Enum):
    SOFT = "soft"  # Allow requests to exceed budget with warnings
    HARD = "hard"  # Block requests that would exceed budget


class BudgetCreateRequest(BaseModel):
    entity_type: EntityType
    entity_id: str
    period_type: PeriodType
    budget_amount_usd: Decimal = Field(gt=0, decimal_places=2)
    enforcement_mode: EnforcementMode = EnforcementMode.HARD


class BudgetUpdateRequest(BaseModel):
    budget_amount_usd: Decimal | None = Field(None, gt=0, decimal_places=2)
    enforcement_mode: EnforcementMode | None = None


class BudgetResponse(BaseModel):
    id: str
    entity_type: EntityType
    entity_id: str
    period_type: PeriodType
    budget_amount_usd: Decimal
    enforcement_mode: EnforcementMode
    org_id: str
    updated_at: datetime

    class Config:
        from_attributes = True


class CostRecordRequest(BaseModel):
    entity_type: EntityType
    entity_id: str
    model_name: str
    tokens_in: int = Field(ge=0)
    tokens_out: int = Field(ge=0)
    request_cost_usd: Decimal | None = Field(None, decimal_places=4)


class BudgetUsageResponse(BaseModel):
    id: str
    entity_type: EntityType
    entity_id: str
    period_start: date
    period_type: PeriodType
    total_cost_usd: Decimal
    total_tokens: int
    request_count: int
    org_id: str

    class Config:
        from_attributes = True


class BudgetStatusResponse(BaseModel):
    budget_amount_usd: Decimal
    current_spend_usd: Decimal
    remaining_budget_usd: Decimal
    budget_utilization_percent: float
    period_start: date
    period_end: date
    period_type: PeriodType
    enforcement_mode: EnforcementMode
    budget_exceeded: bool
    warnings: list[str] = []


class EnforcementResult(BaseModel):
    allowed: bool
    blocked_reason: str | None = None
    exceeded_entity_type: EntityType | None = None
    exceeded_entity_id: str | None = None
    budget_amount_usd: Decimal | None = None
    current_spend_usd: Decimal | None = None
    enforcement_mode: EnforcementMode | None = None
    warnings: list[str] = []


class CostCalculationRequest(BaseModel):
    model_name: str
    tokens_in: int = Field(ge=0)
    tokens_out: int = Field(ge=0)


class CostCalculationResponse(BaseModel):
    model_name: str
    tokens_in: int
    tokens_out: int
    cost_usd: Decimal
    input_cost_per_1k_tokens: Decimal
    output_cost_per_1k_tokens: Decimal
