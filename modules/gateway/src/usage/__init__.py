"""Usage module for tracking and reporting API usage."""

from .config import AggregationInterval, UsageConfig, get_usage_config, set_usage_config
from .routes import router as usage_router
from .service import UsageService

__all__ = [
    # Configuration
    "UsageConfig",
    "AggregationInterval",
    "get_usage_config",
    "set_usage_config",
    # Service
    "UsageService",
    # Routes
    "usage_router",
]

# Export router for FastAPI auto-discovery
router = usage_router
