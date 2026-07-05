"""
Issue #2918: Verify that Base.metadata.create_all is gated behind BG_DB_AUTO_CREATE.

When BG_DB_AUTO_CREATE is false (the deployed-environment default), the lifespan
must NOT call create_all — alembic migrations are the single source of truth.
When BG_DB_AUTO_CREATE is true (local dev via docker-compose), create_all runs
so tables exist without requiring alembic.
"""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_create_all_skipped_when_db_auto_create_false():
    """create_all must not be called when BG_DB_AUTO_CREATE=false (default)."""
    env = {**os.environ, "BG_DB_AUTO_CREATE": "false"}
    with patch.dict(os.environ, env, clear=False):
        # Force re-evaluation of settings
        with patch("src.app.get_settings") as mock_settings:
            settings = MagicMock()
            settings.db_auto_create = False
            mock_settings.return_value = settings

            # Mock the database engine so we can detect create_all calls
            mock_engine = MagicMock()
            mock_conn = MagicMock()
            mock_conn.run_sync = AsyncMock()

            # Use an async context manager mock
            mock_begin = AsyncMock()
            mock_begin.__aenter__ = AsyncMock(return_value=mock_conn)
            mock_begin.__aexit__ = AsyncMock(return_value=False)
            mock_engine.begin = MagicMock(return_value=mock_begin)

            with patch("src.shared.database.get_engine", return_value=mock_engine):
                from src.app import lifespan

                app = MagicMock()
                async with lifespan(app):
                    pass

                # create_all should NOT have been called
                mock_conn.run_sync.assert_not_called()


@pytest.mark.asyncio
async def test_create_all_runs_when_db_auto_create_true():
    """create_all must run when BG_DB_AUTO_CREATE=true (local dev)."""
    env = {**os.environ, "BG_DB_AUTO_CREATE": "true"}
    with patch.dict(os.environ, env, clear=False):
        with patch("src.app.get_settings") as mock_settings:
            settings = MagicMock()
            settings.db_auto_create = True
            # Also mock other settings accessed during lifespan
            settings.mantle_enabled = False
            mock_settings.return_value = settings

            mock_engine = MagicMock()
            mock_conn = MagicMock()
            mock_conn.run_sync = AsyncMock()

            mock_begin = AsyncMock()
            mock_begin.__aenter__ = AsyncMock(return_value=mock_conn)
            mock_begin.__aexit__ = AsyncMock(return_value=False)
            mock_engine.begin = MagicMock(return_value=mock_begin)

            with patch("src.shared.database.get_engine", return_value=mock_engine):
                from src.app import lifespan

                app = MagicMock()
                async with lifespan(app):
                    pass

                # create_all SHOULD have been called
                mock_conn.run_sync.assert_called_once()
