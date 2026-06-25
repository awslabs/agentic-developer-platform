"""Unit tests for src.shared.identity — Cognito sub to canonical users.id resolver.

Covers:
- Happy path: known cognito_sub resolves to the canonical users.id UUID.
- Fallback: unknown cognito_sub returns the raw sub value (graceful degradation).
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.shared.identity import resolve_canonical_user_id

KNOWN_COGNITO_SUB = "cognito-sub-abc-123"
CANONICAL_USER_ID = "uuid-canonical-user-999"
UNKNOWN_COGNITO_SUB = "cognito-sub-unknown-xyz"


@pytest.fixture
def mock_db_with_user():
    """Mock AsyncSession that resolves a known cognito_sub to canonical id."""
    db = MagicMock()
    db.scalar = AsyncMock(return_value=CANONICAL_USER_ID)
    return db


@pytest.fixture
def mock_db_no_user():
    """Mock AsyncSession where no users row matches the cognito_sub."""
    db = MagicMock()
    db.scalar = AsyncMock(return_value=None)
    return db


class TestResolveCanonicalUserId:
    """Tests for resolve_canonical_user_id."""

    @pytest.mark.asyncio
    async def test_returns_canonical_id_for_known_sub(self, mock_db_with_user):
        """When a users row exists for the cognito_sub, return its id (UUID)."""
        result = await resolve_canonical_user_id(mock_db_with_user, KNOWN_COGNITO_SUB)

        assert result == CANONICAL_USER_ID
        mock_db_with_user.scalar.assert_called_once()

    @pytest.mark.asyncio
    async def test_falls_back_to_raw_sub_for_unknown(self, mock_db_no_user):
        """When no users row matches, fall back to the raw cognito_sub value."""
        result = await resolve_canonical_user_id(mock_db_no_user, UNKNOWN_COGNITO_SUB)

        assert result == UNKNOWN_COGNITO_SUB
        mock_db_no_user.scalar.assert_called_once()

    @pytest.mark.asyncio
    async def test_does_not_return_none_for_unknown_sub(self, mock_db_no_user):
        """The fallback must return a string, never None — callers depend on this."""
        result = await resolve_canonical_user_id(mock_db_no_user, UNKNOWN_COGNITO_SUB)

        assert result is not None
        assert isinstance(result, str)
