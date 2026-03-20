"""Bedrock Pool module for cross-account client distribution.

This module provides:
- PoolService: Main service implementing IPoolService interface
- Round-robin distribution of requests across AWS accounts (US-5.1)
- Health tracking with cooldown for failed accounts
- Proper error handling when all accounts are unhealthy (US-9.4)
"""

from src.pool.config import BedrockAccountConfig, PoolConfig, PoolSettings, get_pool_settings
from src.pool.exceptions import (
    AllAccountsUnhealthyError,
    ClientCreationError,
    CredentialExpiredError,
    HealthCheckError,
    NoAccountsConfiguredError,
    PoolError,
    PoolExhaustedError,
    RoleAssumptionError,
)
from src.pool.health_tracker import HealthTracker
from src.pool.models import AssumedRoleCredentials, BedrockAccount, HealthStatus, PoolClient, PoolStatus
from src.pool.selector import RoundRobinSelector
from src.pool.service import PoolService
from src.pool.sts_client import STSClient

__all__ = [
    # Main service
    "PoolService",
    # Configuration
    "BedrockAccountConfig",
    "PoolConfig",
    "PoolSettings",
    "get_pool_settings",
    # Models
    "AssumedRoleCredentials",
    "BedrockAccount",
    "HealthStatus",
    "PoolClient",
    "PoolStatus",
    # Components
    "HealthTracker",
    "RoundRobinSelector",
    "STSClient",
    # Exceptions
    "AllAccountsUnhealthyError",
    "ClientCreationError",
    "CredentialExpiredError",
    "HealthCheckError",
    "NoAccountsConfiguredError",
    "PoolError",
    "PoolExhaustedError",
    "RoleAssumptionError",
]
