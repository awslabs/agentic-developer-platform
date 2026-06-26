"""Tests for Alembic migration 020 — add display_name, tags, installation_id.

Issue #2084 (#2082 Phase-1 story 1): Validates that the migration adds
display_name, tags, and installation_id columns to the knowledge_assets
table and that downgrade removes them cleanly.
"""

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Load the migration module directly (it's not on the normal import path)
# ---------------------------------------------------------------------------

MIGRATION_PATH = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "020_knowledge_assets_display_name_tags_installation_id.py"


@pytest.fixture
def migration_module():
    """Import the migration module from its file path."""
    spec = importlib.util.spec_from_file_location("migration_020", MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["migration_020"] = module
    spec.loader.exec_module(module)
    yield module
    del sys.modules["migration_020"]


# ---------------------------------------------------------------------------
# Revision chain tests
# ---------------------------------------------------------------------------


class TestRevisionChain:
    """Verify the migration chains correctly off 019."""

    def test_revision_id(self, migration_module):
        assert migration_module.revision == "020_knowledge_assets_display_name_tags_installation_id"

    def test_down_revision(self, migration_module):
        assert migration_module.down_revision == "019_knowledge_assets"

    def test_no_branch_labels(self, migration_module):
        assert migration_module.branch_labels is None

    def test_no_depends_on(self, migration_module):
        assert migration_module.depends_on is None


# ---------------------------------------------------------------------------
# Upgrade tests
# ---------------------------------------------------------------------------


class TestUpgrade:
    """Verify upgrade() adds the three columns with correct types."""

    @pytest.fixture
    def mock_op(self):
        """Mock alembic.op for inspecting calls."""
        with patch("alembic.op.execute", new_callable=MagicMock) as mock_execute:
            yield {"execute": mock_execute}

    def test_alter_table_adds_display_name(self, migration_module, mock_op):
        """upgrade() adds display_name VARCHAR(512) column."""
        migration_module.upgrade()

        alter_calls = [c for c in mock_op["execute"].call_args_list if "ALTER TABLE" in str(c)]
        assert len(alter_calls) == 1, "Expected exactly one ALTER TABLE call"

        alter_sql = str(alter_calls[0])
        assert "display_name" in alter_sql
        assert "VARCHAR(512)" in alter_sql

    def test_alter_table_adds_tags_jsonb(self, migration_module, mock_op):
        """upgrade() adds tags JSONB column with empty object default."""
        migration_module.upgrade()

        alter_calls = [c for c in mock_op["execute"].call_args_list if "ALTER TABLE" in str(c)]
        alter_sql = str(alter_calls[0])

        assert "tags" in alter_sql
        assert "JSONB" in alter_sql
        assert "'{}'::jsonb" in alter_sql

    def test_alter_table_adds_installation_id_bigint(self, migration_module, mock_op):
        """upgrade() adds installation_id as BIGINT (not INTEGER)."""
        migration_module.upgrade()

        alter_calls = [c for c in mock_op["execute"].call_args_list if "ALTER TABLE" in str(c)]
        alter_sql = str(alter_calls[0])

        assert "installation_id" in alter_sql
        assert "BIGINT" in alter_sql

    def test_columns_are_nullable(self, migration_module, mock_op):
        """upgrade() does not add NOT NULL constraints (columns are nullable)."""
        migration_module.upgrade()

        alter_calls = [c for c in mock_op["execute"].call_args_list if "ALTER TABLE" in str(c)]
        alter_sql = str(alter_calls[0])

        # NOT NULL should not appear for any of the new columns
        assert "NOT NULL" not in alter_sql


# ---------------------------------------------------------------------------
# Downgrade tests
# ---------------------------------------------------------------------------


class TestDowngrade:
    """Verify downgrade() drops all three columns cleanly."""

    @pytest.fixture
    def mock_op(self):
        """Mock alembic.op for inspecting calls."""
        with patch("alembic.op.drop_column", new_callable=MagicMock) as mock_drop_column:
            yield {"drop_column": mock_drop_column}

    def test_drops_all_three_columns(self, migration_module, mock_op):
        """downgrade() drops display_name, tags, and installation_id."""
        migration_module.downgrade()

        dropped = [str(c[0][1]) for c in mock_op["drop_column"].call_args_list]
        assert "display_name" in dropped
        assert "tags" in dropped
        assert "installation_id" in dropped

    def test_drops_exactly_three_columns(self, migration_module, mock_op):
        """downgrade() drops exactly 3 columns (no extras)."""
        migration_module.downgrade()

        assert mock_op["drop_column"].call_count == 3

    def test_all_drops_target_knowledge_assets_table(self, migration_module, mock_op):
        """downgrade() targets the knowledge_assets table for all drops."""
        migration_module.downgrade()

        for call in mock_op["drop_column"].call_args_list:
            assert call[0][0] == "knowledge_assets"
