from datetime import date, datetime
from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, Field


class PeriodType(str, Enum):
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    # Issue #4187: a run/chain cap is scoped to a *lifetime*, not a calendar
    # window — it must accumulate for as long as the run does and reset only
    # when a new run starts. `get_period_start_end` deliberately rejects this
    # value: there is no calendar period to compute, and the run/chain id in the
    # reservation key is already what separates one run's ledger from the next.
    RUN = "run"


class EntityType(str, Enum):
    ORGANIZATION = "org"
    DEPARTMENT = "department"
    TEAM = "team"
    USER = "user"
    SERVICE_ACCOUNT = "service_account"
    AGENT = "agent"  # IAM-authenticated agents (Issue #249)
    # Issue #4187: the two spend-cap scopes. Deliberately NOT "attempt" — that
    # word has no referent in this codebase, whereas both of these do:
    #   RUN   -> one agent run, keyed on the webhook-events `event_id`
    #   CHAIN -> a run and every run it spawned, keyed on `correlation_id`
    # A fan-out is a parent plus N children, so when best-of-N lands its cap IS
    # CHAIN with no new vocabulary. Both ids are resolved SERVER-SIDE from the
    # run's registry row (src/budget/run_binding.py) — never from a header.
    RUN = "run"
    CHAIN = "chain"
    # Issue #4300: the human who set a chain in motion. Deliberately NOT a reuse
    # of USER: that entity holds the *authenticated caller*, which for a hosted
    # run is the agent's own service account, and its ids are Cognito `sub`s.
    # ROOT_USER holds a canonical `users.id` — the namespace the lineage plane
    # writes `root_human_id` in. Two id namespaces sharing one entity_type under
    # a UniqueConstraint is an identifier collision waiting to happen, so they
    # get separate values.
    #
    # Resolved SERVER-SIDE off the run's registry row (src/budget/run_binding.py),
    # never from a header. Unlike RUN/CHAIN this line is CUMULATIVE per calendar
    # period: it has a settled Postgres ledger (the budget-usage-tracker Lambda
    # writes a "root_user" row), so it belongs in the hierarchy, not _scope_targets.
    ROOT_USER = "root_user"


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

    # Issue #4187: which spend-cap scope stopped this request — "run" or
    # "chain". Surfaced in the 402 body's `details` so the worker can map the
    # denial to a `budget_stopped` terminal reason instead of reporting a
    # phantom crash, and so the stop is attributable after the fact.
    #
    # None on the hierarchy scopes (user/team/dept/org), which keeps every
    # pre-#4187 denial byte-identical.
    scope: str | None = None

    # Issue #4187: the cap that was hit, reported alongside `scope` so the 402
    # says how much the run was allowed rather than only that it ran out.
    # Deliberately NOT paired with a spend figure: `current_spend_usd` is the
    # *settled* Postgres total, and run/chain scopes have no settled ledger at
    # all (the tracker Lambda writes no run rows), so there is no honest number
    # to put beside it.
    scope_cap_usd: Decimal | None = None


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
