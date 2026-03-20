"""
Test fixtures package for BedrockGateway integration and E2E tests.

This package provides:
- Factory functions for creating test entities (factories.py)
- Mock AWS service responses (mock_aws.py)
- Test data seeding utilities (seed_data.py)
"""

from tests.fixtures.factories import (
    create_budget_config,
    create_department,
    create_org,
    create_pool_account,
    create_rate_limit_config,
    create_service_account,
    create_team,
    create_token,
    create_user,
)
from tests.fixtures.mock_aws import (
    MockBedrockClient,
    MockSTSClient,
    mock_bedrock_invoke_response,
    mock_bedrock_streaming_response,
    mock_bedrock_throttling_response,
    mock_sts_caller_identity,
    mock_sts_error_response,
)
from tests.fixtures.seed_data import (
    clear_test_data,
    seed_budget_data,
    seed_rate_limit_data,
    seed_test_database,
)

__all__ = [
    # Factory functions
    "create_org",
    "create_department",
    "create_team",
    "create_user",
    "create_service_account",
    "create_token",
    "create_budget_config",
    "create_rate_limit_config",
    "create_pool_account",
    # Mock AWS
    "MockSTSClient",
    "MockBedrockClient",
    "mock_sts_caller_identity",
    "mock_sts_error_response",
    "mock_bedrock_invoke_response",
    "mock_bedrock_streaming_response",
    "mock_bedrock_throttling_response",
    # Seed data
    "seed_test_database",
    "seed_budget_data",
    "seed_rate_limit_data",
    "clear_test_data",
]
