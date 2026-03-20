from decimal import Decimal

from pydantic_settings import BaseSettings


class BudgetConfig(BaseSettings):
    """Configuration for budget service including model pricing."""

    # Enforcement settings
    default_enforcement_mode: str = "hard"
    budget_check_enabled: bool = True
    cost_calculation_enabled: bool = True

    # Fail mode: "open" (default) allows requests on errors, "closed" blocks them
    # "open" ensures transient DB/IAM errors don't block all traffic
    budget_fail_mode: str = "open"

    # Grace period settings (in seconds)
    soft_enforcement_grace_period: int = 300  # 5 minutes
    budget_exceeded_notification_cooldown: int = 3600  # 1 hour

    # Alert thresholds
    budget_warning_threshold_percent: float = 80.0
    budget_critical_threshold_percent: float = 95.0

    # Model pricing (cost per 1000 tokens in USD)
    model_pricing: dict[str, dict[str, Decimal]] = {
        # Claude models
        "claude-3-5-sonnet-20241022": {
            "input": Decimal("0.003"),
            "output": Decimal("0.015"),
        },
        "claude-3-5-sonnet-20240620": {
            "input": Decimal("0.003"),
            "output": Decimal("0.015"),
        },
        "claude-3-5-haiku-20241022": {
            "input": Decimal("0.0008"),
            "output": Decimal("0.004"),
        },
        "claude-3-opus-20240229": {
            "input": Decimal("0.015"),
            "output": Decimal("0.075"),
        },
        "claude-3-sonnet-20240229": {
            "input": Decimal("0.003"),
            "output": Decimal("0.015"),
        },
        "claude-3-haiku-20240307": {
            "input": Decimal("0.00025"),
            "output": Decimal("0.00125"),
        },
        # Legacy Claude models
        "claude-2.1": {
            "input": Decimal("0.008"),
            "output": Decimal("0.024"),
        },
        "claude-2.0": {
            "input": Decimal("0.008"),
            "output": Decimal("0.024"),
        },
        "claude-instant-1.2": {
            "input": Decimal("0.0008"),
            "output": Decimal("0.0024"),
        },
        # Default pricing for unknown models
        "default": {
            "input": Decimal("0.003"),
            "output": Decimal("0.015"),
        },
    }

    # Database settings
    budget_usage_batch_size: int = 100
    budget_cleanup_days: int = 90  # Keep budget usage data for 90 days

    class Config:
        env_prefix = "BG_BUDGET_"
        case_sensitive = False


# Global config instance
budget_config = BudgetConfig()
