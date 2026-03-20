"""
Shared SQLAlchemy ORM Models.

All models are automatically created on app startup by importing them
in src/app.py lifespan and calling Base.metadata.create_all().
"""

from src.shared.models.base import Base, TenantMixin, new_uuid, utcnow
from src.shared.models.budget import BudgetConfig, BudgetUsage
from src.shared.models.organization import (
    Department,
    Organization,
    ServiceAccount,
    Team,
    User,
)
from src.shared.models.usage import (
    BedrockPoolAccount,
    ModelAlias,
    ModelPricing,
    RateLimitConfig,
    UsageLog,
)

__all__ = [
    "Base",
    "TenantMixin",
    "new_uuid",
    "utcnow",
    "BudgetConfig",
    "BudgetUsage",
    "Department",
    "Organization",
    "ServiceAccount",
    "Team",
    "User",
    "BedrockPoolAccount",
    "ModelAlias",
    "ModelPricing",
    "RateLimitConfig",
    "UsageLog",
]
