"""Pytest fixtures for activity module tests."""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from src.shared.schemas.auth import TokenContext


@pytest.fixture
def regular_user() -> TokenContext:
    """Create a regular user token context."""
    return TokenContext(
        user_id="user-abc-123",
        org_id="org-tenant-001",
        team_id="team-001",
        department_id="dept-001",
        account_type="human",
        is_admin=False,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


@pytest.fixture
def admin_user() -> TokenContext:
    """Create a platform admin token context."""
    return TokenContext(
        user_id="user-admin-001",
        org_id="org-platform",
        team_id="team-platform",
        department_id="dept-platform",
        account_type="human",
        is_admin=True,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


@pytest.fixture
def org_admin_user() -> TokenContext:
    """Create an org admin token context (non-platform admin, has org)."""
    return TokenContext(
        user_id="user-orgadmin-001",
        org_id="org-tenant-001",
        team_id="team-001",
        department_id="dept-001",
        account_type="human",
        is_admin=False,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


@pytest.fixture
def mock_dynamodb_table():
    """Create a mock DynamoDB table resource."""
    table = MagicMock()
    table.query = MagicMock()
    return table


@pytest.fixture
def mock_dynamodb_resource(mock_dynamodb_table):
    """Create a mock boto3 DynamoDB resource."""
    resource = MagicMock()
    resource.Table.return_value = mock_dynamodb_table
    return resource
