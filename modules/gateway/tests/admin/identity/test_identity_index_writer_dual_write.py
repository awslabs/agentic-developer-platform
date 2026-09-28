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
    """Tests for put_user_identity dual-write behavior.

    Issue #3134 fix: When member_org_ids is None, put_user_identity now uses
    UpdateItem (update_user_identity_core / update_user_core_attrs) to preserve
    existing member_org_ids attrs. When member_org_ids IS provided, it still
    uses PutItem (full overwrite) to set the complete state.
    """

    @pytest.fixture
    def mock_old_client(self):
        client = MagicMock(spec=IdentityIndexClient)
        client.put_identity = AsyncMock(return_value=True)
        client.update_user_identity_core = AsyncMock(return_value=True)
        client.delete_identity = AsyncMock(return_value=True)
        return client

    @pytest.fixture
    def mock_new_client(self):
        client = MagicMock(spec=UserIdentityIndexClient)
        client.put_user_identity = AsyncMock(return_value=True)
        client.update_user_core_attrs = AsyncMock(return_value=True)
        client.delete_user_identity = AsyncMock(return_value=True)
        return client

    @pytest.fixture
    def writer(self, mock_old_client, mock_new_client):
        return IdentityIndexWriter(client=mock_old_client, user_identity_client=mock_new_client)

    @pytest.mark.asyncio
    @patch("src.admin.identity.identity_index_writer._v2_write_enabled", return_value=False)
    async def test_put_without_member_org_ids_uses_update(self, _mock_flag, writer, mock_old_client, mock_new_client):
        """When member_org_ids is None, uses UpdateItem to preserve existing membership attrs."""
        result = await writer.put_user_identity(
            provider_user_id="12345",
            user_id="user-001",
            org_id="org-001",
            provider="github",
        )
        assert result is True
        # UpdateItem path (preserves member_org_ids)
        mock_old_client.update_user_identity_core.assert_awaited_once_with(
            identity_value="12345",
            user_id="user-001",
            org_id="org-001",
            provider_username=None,
        )
        # PutItem NOT called (would wipe member_org_ids)
        mock_old_client.put_identity.assert_not_awaited()
        # V2 flag off → new table not written
        mock_new_client.update_user_core_attrs.assert_not_awaited()
        mock_new_client.put_user_identity.assert_not_awaited()

    @pytest.mark.asyncio
    @patch("src.admin.identity.identity_index_writer._v2_write_enabled", return_value=True)
    async def test_put_without_member_org_ids_dual_write_uses_update(self, _mock_flag, writer, mock_old_client, mock_new_client):
        """When member_org_ids is None + V2 flag on, both tables use UpdateItem."""
        result = await writer.put_user_identity(
            provider_user_id="12345",
            user_id="user-001",
            org_id="org-001",
            provider="github",
            provider_username="testuser",
            member_org_ids=None,
        )
        assert result is True
        # Old table: UpdateItem
        mock_old_client.update_user_identity_core.assert_awaited_once_with(
            identity_value="12345",
            user_id="user-001",
            org_id="org-001",
            provider_username="testuser",
        )
        mock_old_client.put_identity.assert_not_awaited()
        # New table: UpdateItem (preserves member_org_ids)
        mock_new_client.update_user_core_attrs.assert_awaited_once_with(
            provider="github",
            provider_user_id="12345",
            user_id="user-001",
            org_id="org-001",
            provider_username="testuser",
        )
        mock_new_client.put_user_identity.assert_not_awaited()

    @pytest.mark.asyncio
    @patch("src.admin.identity.identity_index_writer._v2_write_enabled", return_value=True)
    async def test_put_with_member_org_ids_uses_put_item(self, _mock_flag, writer, mock_old_client, mock_new_client):
        """When member_org_ids IS provided, uses PutItem to set complete state."""
        result = await writer.put_user_identity(
            provider_user_id="12345",
            user_id="user-001",
            org_id="org-001",
            provider="github",
            provider_username="testuser",
            member_org_ids=["org-001", "org-002"],
        )
        assert result is True
        # Old table: PutItem (full overwrite with member_org_ids)
        mock_old_client.put_identity.assert_awaited_once()
        mock_old_client.update_user_identity_core.assert_not_awaited()
        # New table: PutItem (full overwrite with member_org_ids)
        mock_new_client.put_user_identity.assert_awaited_once_with(
            provider="github",
            provider_user_id="12345",
            user_id="user-001",
            org_id="org-001",
            provider_username="testuser",
            member_org_ids=["org-001", "org-002"],
        )
        mock_new_client.update_user_core_attrs.assert_not_awaited()

    @pytest.mark.asyncio
    @patch("src.admin.identity.identity_index_writer._v2_write_enabled", return_value=True)
    async def test_old_write_failure_propagates(self, _mock_flag, writer, mock_old_client, mock_new_client):
        """If OLD table write fails, failure is propagated and NEW write is skipped."""
        mock_old_client.update_user_identity_core = AsyncMock(return_value=False)
        result = await writer.put_user_identity(
            provider_user_id="12345",
            user_id="user-001",
            org_id="org-001",
        )
        assert result is False
        mock_new_client.update_user_core_attrs.assert_not_awaited()
        mock_new_client.put_user_identity.assert_not_awaited()

    @pytest.mark.asyncio
    @patch("src.admin.identity.identity_index_writer._v2_write_enabled", return_value=True)
    async def test_new_write_failure_does_not_propagate(self, _mock_flag, writer, mock_old_client, mock_new_client):
        """If NEW table write fails, caller still gets True (non-fatal)."""
        mock_new_client.update_user_core_attrs = AsyncMock(return_value=False)
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
        mock_new_client.update_user_core_attrs = AsyncMock(side_effect=Exception("DDB crash"))
        result = await writer.put_user_identity(
            provider_user_id="12345",
            user_id="user-001",
            org_id="org-001",
        )
        assert result is True

    @pytest.mark.asyncio
    @patch("src.admin.identity.identity_index_writer._v2_write_enabled", return_value=True)
    async def test_put_passes_provider_to_new_table(self, _mock_flag, writer, mock_old_client, mock_new_client):
        """Provider is passed correctly to new table write (UpdateItem path)."""
        await writer.put_user_identity(
            provider_user_id="U012345",
            user_id="user-002",
            org_id="org-001",
            provider="slack",
        )
        mock_new_client.update_user_core_attrs.assert_awaited_once_with(
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
        mock_new_client.update_user_core_attrs.assert_awaited_once_with(
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
