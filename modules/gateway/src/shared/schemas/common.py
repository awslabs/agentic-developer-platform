from pydantic import BaseModel


class ErrorResponse(BaseModel):
    error: str
    message: str
    details: dict | None = None


class HealthResponse(BaseModel):
    status: str  # "healthy" or "unhealthy"


class BudgetCheckResult(BaseModel):
    allowed: bool
    exceeded_level: str | None = None  # org/department/team/user
    exceeded_entity: str | None = None
    budget_usd: float | None = None
    spent_usd: float | None = None
    period: str | None = None
    resets_at: str | None = None
    enforcement_mode: str | None = None  # soft/hard
    warnings: list[str] = []


class RateLimitCheckResult(BaseModel):
    allowed: bool
    limit_type: str | None = None  # rpm/tpm/concurrent
    limit: int | None = None
    remaining: int | None = None
    retry_after_seconds: int | None = None
