"""Unit tests for registry_reader.py (Issue #2082 Phase 2).

Tests:
- extract_org_repo handles various URL formats
- _get_gateway_connection raises when GATEWAY_DB_NAME not set
- read_registry_assets returns RegistryAsset dataclass instances
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add the ingestion source to sys.path for direct imports
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "images" / "ingestion"))

from registry_reader import extract_org_repo


# ---------------------------------------------------------------------------
# extract_org_repo tests
# ---------------------------------------------------------------------------


class TestExtractOrgRepo:
    """Tests for extract_org_repo."""

    def test_https_url(self):
        assert extract_org_repo("https://github.com/acme/my-service") == "acme/my-service"

    def test_https_url_trailing_slash(self):
        assert extract_org_repo("https://github.com/acme/my-service/") == "acme/my-service"

    def test_https_url_with_git_suffix(self):
        assert extract_org_repo("https://github.com/acme/my-service.git") == "acme/my-service"

    def test_git_at_url(self):
        assert extract_org_repo("git@github.com:acme/my-service.git") == "acme/my-service"

    def test_already_org_repo(self):
        assert extract_org_repo("acme/my-service") == "acme/my-service"

    def test_personal_repo(self):
        assert extract_org_repo("https://github.com/john/dotfiles") == "john/dotfiles"


# ---------------------------------------------------------------------------
# read_registry_assets tests
# ---------------------------------------------------------------------------


class TestReadRegistryAssets:
    """Tests for read_registry_assets with mocked DB connection."""

    @patch("registry_reader._get_gateway_connection")
    def test_returns_assets_from_db(self, mock_conn_fn):
        """Reads rows from gateway DB and returns RegistryAsset list."""
        import uuid

        asset_id = str(uuid.uuid4())
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = [
            (asset_id, "https://github.com/acme/svc", "repo", "tenant-1", None, None, 12345),
        ]
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_conn_fn.return_value = mock_conn

        from registry_reader import read_registry_assets

        assets = read_registry_assets("repo")

        assert len(assets) == 1
        assert assets[0].asset_id == asset_id
        assert assets[0].source_ref == "https://github.com/acme/svc"
        assert assets[0].tenant_id == "tenant-1"
        assert assets[0].installation_id == 12345
        mock_conn.close.assert_called_once()

    @patch("registry_reader._get_gateway_connection")
    def test_empty_result(self, mock_conn_fn):
        """Returns empty list when no assets match."""
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = []
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_conn_fn.return_value = mock_conn

        from registry_reader import read_registry_assets

        assets = read_registry_assets("repo")
        assert assets == []
        mock_conn.close.assert_called_once()

    @patch("registry_reader._get_gateway_connection")
    def test_shared_asset_has_null_tenant(self, mock_conn_fn):
        """Public/shared assets have tenant_id=None."""
        import uuid

        asset_id = str(uuid.uuid4())
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = [
            (asset_id, "https://github.com/public/repo", "repo", None, None, None, None),
        ]
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_conn_fn.return_value = mock_conn

        from registry_reader import read_registry_assets

        assets = read_registry_assets("repo")
        assert len(assets) == 1
        assert assets[0].tenant_id is None
        assert assets[0].installation_id is None

    @patch("registry_reader._get_gateway_connection")
    def test_personal_asset_has_owner_sub(self, mock_conn_fn):
        """Personal assets carry owner_sub."""
        import uuid

        asset_id = str(uuid.uuid4())
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = [
            (asset_id, "https://github.com/user/repo", "repo", "t1", "user-abc", None, 999),
        ]
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_conn_fn.return_value = mock_conn

        from registry_reader import read_registry_assets

        assets = read_registry_assets("repo")
        assert assets[0].owner_sub == "user-abc"
        assert assets[0].tenant_id == "t1"


# ---------------------------------------------------------------------------
# _get_gateway_connection error handling
# ---------------------------------------------------------------------------


class TestGetGatewayConnection:
    """Tests for _get_gateway_connection error cases."""

    def test_raises_when_gateway_db_name_empty(self, monkeypatch):
        """Raises RuntimeError when GATEWAY_DB_NAME is not set."""
        from config import settings

        monkeypatch.setattr(settings, "gateway_db_name", "")
        monkeypatch.setattr(settings, "gateway_db_host", "")
        monkeypatch.delenv("DB_HOST", raising=False)

        from registry_reader import _get_gateway_connection

        with pytest.raises(RuntimeError, match="GATEWAY_DB_NAME not set"):
            _get_gateway_connection()

    def test_raises_when_no_host(self, monkeypatch):
        """Raises RuntimeError when no host is available."""
        from config import settings

        monkeypatch.setattr(settings, "gateway_db_name", "bedrockgateway")
        monkeypatch.setattr(settings, "gateway_db_host", "")
        monkeypatch.delenv("DB_HOST", raising=False)

        from registry_reader import _get_gateway_connection

        with pytest.raises(RuntimeError, match="Neither GATEWAY_DB_HOST nor DB_HOST"):
            _get_gateway_connection()
