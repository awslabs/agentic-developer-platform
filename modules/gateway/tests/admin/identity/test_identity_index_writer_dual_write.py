"""Tests for IdentityIndexWriter dual-write behavior.

Issue #537: Sequential write ordering (OLD first, NEW second),
failure propagation, and feature-flag gating.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.admin.identity.identity_index_writer import IdentityIndexWriter
from src.admin.identity.user_identity_index import UserIdentityIndexClient
from src.admin.identity_index import IdentityIndexClient


class TestDualWritePutUserIdentity:
    """Tests for put_user_identity dual-write behavior."""

    @pytest.fixture
    def mock_old_client(self):
        client = MagicMock(spec=IdentityIndexClient)
        client.put_identity = AsyncMock(return_value=True)
        client.delete_identity = AsyncMock(return_value=True)
        return client

    @pytest.fixture
    def mock_new_client(self):
        client = MagicMock(spec=UserIdentityIndexClient)
        client.put_user_identity = AsyncMock(return_value=True)
        client.delete_user_identity = AsyncMock(return_value=True)
        return client

    @pytest.fixture
    def writer(self, mock_old_client, mock_new_client):
        return IdentityIndexWriter(client=mock_old_client, user_identity_client=mock_new_client)

    @pytest.mark.asyncio
    @patch("src.admin.identity.identity_index_writer._v2_write_enabled", return_value=False)
    async def test_put_writes_old_table_only_when_flag_off(self, _mock_flag, writer, mock_old_client, mock_new_client):
        """When V2 write flag is off, only old table is written."""
        result = await writer.put_user_identity(
            provider_user_id="12345",
            user_id="user-001",
            org_id="org-001",
            provider="github",
        )
        assert result is True
        mock_old_client.put_identity.assert_awaited_once()
        mock_new_client.put_user_identity.assert_not_awaited()

    @pytest.mark.asyncio
    @patch("src.admin.identity.identity_index_writer._v2_write_enabled", return_value=True)
    async def test_put_writes_both_tables_when_flag_on(self, _mock_flag, writer, mock_old_client, mock_new_client):
        """When V2 write flag is on, both tables are written sequentially."""
        result = await writer.put_user_identity(
            provider_user_id="12345",
            user_id="user-001",
            org_id="org-001",
            provider="github",
            provider_username="testuser",
        )
        assert result is True
        mock_old_client.put_identity.assert_awaited_once()
        mock_new_client.put_user_identity.assert_awaited_once_with(
            provider="github",
            provider_user_id="12345",
            user_id="user-001",
            org_id="org-001",
            provider_username="testuser",
        )

    @pytest.mark.asyncio
    @patch("src.admin.identity.identity_index_writer._v2_write_enabled", return_value=True)
    async def test_old_write_failure_propagates(self, _mock_flag, writer, mock_old_client, mock_new_client):
        """If OLD table write fails, failure is propagated and NEW write is skipped."""
        mock_old_client.put_identity = AsyncMock(return_value=False)
        result = await writer.put_user_identity(
            provider_user_id="12345",
            user_id="user-001",
            org_id="org-001",
        )
        assert result is False
        mock_new_client.put_user_identity.assert_not_awaited()

    @pytest.mark.asyncio
    @patch("src.admin.identity.identity_index_writer._v2_write_enabled", return_value=True)
    async def test_new_write_failure_does_not_propagate(self, _mock_flag, writer, mock_old_client, mock_new_client):
        """If NEW table write fails, caller still gets True (non-fatal)."""
        mock_new_client.put_user_identity = AsyncMock(return_value=False)
        result = await writer.put_user_identity(
            provider_user_id="12345",
            user_id="user-001",
            org_id="org-001",
        )
        assert result is True

    @pytest.mark.asyncio
    @patch("src.admin.identity.identity_index_writer._v2_write_enabled", return_value=True)
    async def test_new_write_exception_does_not_propagate(self, _mock_flag, writer, mock_old_client, mock_new_client):
        """If NEW table raises exception, caller still gets True (non-fatal)."""
        mock_new_client.put_user_identity = AsyncMock(side_effect=Exception("DDB crash"))
        result = await writer.put_user_identity(
            provider_user_id="12345",
            user_id="user-001",
            org_id="org-001",
        )
        assert result is True

    @pytest.mark.asyncio
    @patch("src.admin.identity.identity_index_writer._v2_write_enabled", return_value=True)
    async def test_put_passes_provider_to_new_table(self, _mock_flag, writer, mock_old_client, mock_new_client):
        """Provider is passed correctly to new table write."""
        await writer.put_user_identity(
            provider_user_id="U012345",
            user_id="user-002",
            org_id="org-001",
            provider="slack",
        )
        mock_new_client.put_user_identity.assert_awaited_once_with(
            provider="slack",
            provider_user_id="U012345",
            user_id="user-002",
            org_id="org-001",
            provider_username=None,
        )

    @pytest.mark.asyncio
    @patch("src.admin.identity.identity_index_writer._v2_write_enabled", return_value=True)
    async def test_put_default_provider_is_github(self, _mock_flag, writer, mock_old_client, mock_new_client):
        """Default provider is 'github' for backward compatibility."""
        await writer.put_user_identity(
            provider_user_id="12345",
            user_id="user-001",
            org_id="org-001",
        )
        mock_new_client.put_user_identity.assert_awaited_once_with(
            provider="github",
            provider_user_id="12345",
            user_id="user-001",
            org_id="org-001",
            provider_username=None,
        )


class TestDualWriteDeleteUserIdentity:
    """Tests for delete_user_identity dual-write behavior."""

    @pytest.fixture
    def mock_old_client(self):
        client = MagicMock(spec=IdentityIndexClient)
        client.put_identity = AsyncMock(return_value=True)
        client.delete_identity = AsyncMock(return_value=True)
        return client

    @pytest.fixture
    def mock_new_client(self):
        client = MagicMock(spec=UserIdentityIndexClient)
        client.put_user_identity = AsyncMock(return_value=True)
        client.delete_user_identity = AsyncMock(return_value=True)
        return client

    @pytest.fixture
    def writer(self, mock_old_client, mock_new_client):
        return IdentityIndexWriter(client=mock_old_client, user_identity_client=mock_new_client)

    @pytest.mark.asyncio
    @patch("src.admin.identity.identity_index_writer._v2_write_enabled", return_value=False)
    async def test_delete_old_table_only_when_flag_off(self, _mock_flag, writer, mock_old_client, mock_new_client):
        """When flag is off, only old table delete fires."""
        result = await writer.delete_user_identity("12345")
        assert result is True
        mock_old_client.delete_identity.assert_awaited_once()
        mock_new_client.delete_user_identity.assert_not_awaited()

    @pytest.mark.asyncio
    @patch("src.admin.identity.identity_index_writer._v2_write_enabled", return_value=True)
    async def test_delete_both_tables_when_flag_on(self, _mock_flag, writer, mock_old_client, mock_new_client):
        """When flag is on, both tables are deleted from."""
        result = await writer.delete_user_identity("12345", provider="github")
        assert result is True
        mock_old_client.delete_identity.assert_awaited_once()
        mock_new_client.delete_user_identity.assert_awaited_once_with(
            provider="github",
            provider_user_id="12345",
        )

    @pytest.mark.asyncio
    @patch("src.admin.identity.identity_index_writer._v2_write_enabled", return_value=True)
    async def test_old_delete_failure_propagates(self, _mock_flag, writer, mock_old_client, mock_new_client):
        """If OLD delete fails, failure propagates and NEW is skipped."""
        mock_old_client.delete_identity = AsyncMock(return_value=False)
        result = await writer.delete_user_identity("12345")
        assert result is False
        mock_new_client.delete_user_identity.assert_not_awaited()

    @pytest.mark.asyncio
    @patch("src.admin.identity.identity_index_writer._v2_write_enabled", return_value=True)
    async def test_new_delete_failure_does_not_propagate(self, _mock_flag, writer, mock_old_client, mock_new_client):
        """If NEW delete fails, caller still gets True."""
        mock_new_client.delete_user_identity = AsyncMock(return_value=False)
        result = await writer.delete_user_identity("12345")
        assert result is True
