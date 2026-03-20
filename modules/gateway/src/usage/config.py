"""Usage module configuration."""

from enum import Enum

from pydantic_settings import BaseSettings


class AggregationInterval(str, Enum):
    """Time intervals for usage aggregation."""

    HOURLY = "hourly"
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"


class UsageConfig(BaseSettings):
    """Usage module configuration settings."""

    # Aggregation settings
    default_aggregation_interval: AggregationInterval = AggregationInterval.DAILY

    # Retention settings
    raw_log_retention_days: int = 90
    hourly_aggregation_retention_days: int = 90
    daily_aggregation_retention_days: int = 365
    monthly_aggregation_retention_days: int = 730  # 2 years

    # Query limits
    default_page_size: int = 50
    max_page_size: int = 1000
    max_export_rows: int = 100000

    # Caching
    cache_ttl_seconds: int = 60

    model_config = {"env_prefix": "BG_USAGE_"}


_usage_config: UsageConfig | None = None


def get_usage_config() -> UsageConfig:
    """Get usage configuration singleton."""
    global _usage_config
    if _usage_config is None:
        _usage_config = UsageConfig()
    return _usage_config


def set_usage_config(config: UsageConfig) -> None:
    """Set usage configuration (for testing)."""
    global _usage_config
    _usage_config = config
