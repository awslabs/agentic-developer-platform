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


class DenyReason(str, Enum):
    """Why a request was denied — a real cap, or an unreadable ledger.

    Issue #4075: these are very different incidents and must not look alike.
    ``BUDGET_EXCEEDED`` is a genuine cap (non-retryable — more money is not
    coming). ``CHECK_UNAVAILABLE`` means the budget check itself failed, so
    nothing is known to be over budget and the caller should retry shortly.
    Collapsing the two made a database outage present platform-wide as
    "budget exceeded", which sends operators chasing a billing problem.
    """

    BUDGET_EXCEEDED = "budget_exceeded"
    CHECK_UNAVAILABLE = "check_unavailable"


class EnforcementResult(BaseModel):
    allowed: bool
    blocked_reason: str | None = None
    exceeded_entity_type: EntityType | None = None
    exceeded_entity_id: str | None = None
    budget_amount_usd: Decimal | None = None
    current_spend_usd: Decimal | None = None
    enforcement_mode: EnforcementMode | None = None
    warnings: list[str] = []

    # Issue #4075: distinguishes "allowed because in budget" from "allowed
    # because the ledger was unreadable and we are inside the bounded grace
    # window". An allow that is indistinguishable from a healthy allow is just
    # fail-open, so this is what makes the grace window observable.
    grace_engaged: bool = False

    # Issue #4075: set on denials so the middleware can pick the right status
    # code. Defaulted to None so every pre-existing construction site keeps
    # producing the original 402 cap response.
    deny_reason: DenyReason | None = None


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
