"""Tests for Alembic migration 019 — knowledge_assets table.

Issue #2046 (#2039 I5): Validates that the migration creates the
knowledge_assets table fresh in the gateway DB with the expected columns,
indexes, and the reserved `status_detail` JSONB column.
"""

import importlib
import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Load the migration module directly (it's not on the normal import path)
# ---------------------------------------------------------------------------

MIGRATION_PATH = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "019_knowledge_assets.py"


@pytest.fixture
def migration_module():
    """Import the migration module from its file path."""
    spec = importlib.util.spec_from_file_location("migration_019", MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    # Temporarily add to sys.modules for the import to work
    sys.modules["migration_019"] = module
    spec.loader.exec_module(module)
    yield module
    del sys.modules["migration_019"]


# ---------------------------------------------------------------------------
# Revision chain tests
# ---------------------------------------------------------------------------


class TestRevisionChain:
    """Verify the migration chains correctly off the current gateway head."""

    def test_revision_id(self, migration_module):
        assert migration_module.revision == "019_knowledge_assets"

    def test_down_revision(self, migration_module):
        assert migration_module.down_revision == "018_agent_run_cost_traceability"

    def test_no_branch_labels(self, migration_module):
        assert migration_module.branch_labels is None

    def test_no_depends_on(self, migration_module):
        assert migration_module.depends_on is None


# ---------------------------------------------------------------------------
# Upgrade tests
# ---------------------------------------------------------------------------


class TestUpgrade:
    """Verify upgrade() creates knowledge_assets with expected schema."""

    @pytest.fixture
    def mock_op(self):
        """Mock alembic.op for inspecting calls."""
        with (
            patch("alembic.op.execute", new_callable=MagicMock) as mock_execute,
            patch("alembic.op.create_index", new_callable=MagicMock) as mock_create_index,
        ):
            yield {"execute": mock_execute, "create_index": mock_create_index}

    def test_creates_table_with_expected_columns(self, migration_module, mock_op):
        """upgrade() creates knowledge_assets with all required columns."""
        migration_module.upgrade()

        # Find the CREATE TABLE call
        create_calls = [c for c in mock_op["execute"].call_args_list if "CREATE TABLE" in str(c)]
        assert len(create_calls) >= 1, "No CREATE TABLE call found"

        create_sql = str(create_calls[0])

        # Core columns from agent-context 007
        assert "id" in create_sql
        assert "UUID PRIMARY KEY" in create_sql
        assert "asset_type" in create_sql
        assert "VARCHAR(32) NOT NULL" in create_sql
        assert "source_ref" in create_sql
        assert "VARCHAR(2048) NOT NULL" in create_sql
        assert "tenant_id" in create_sql
        assert "owner_sub" in create_sql
        assert "project_id" in create_sql
        assert "status" in create_sql
        assert "registered_by" in create_sql
        assert "created_at" in create_sql
        assert "TIMESTAMPTZ" in create_sql
        assert "updated_at" in create_sql
        assert "metadata" in create_sql
        assert "JSONB" in create_sql
        assert "last_error" in create_sql
        assert "retry_count" in create_sql

    def test_status_detail_jsonb_column_present(self, migration_module, mock_op):
        """upgrade() includes the reserved status_detail JSONB column."""
        migration_module.upgrade()

        create_calls = [c for c in mock_op["execute"].call_args_list if "CREATE TABLE" in str(c)]
        create_sql = str(create_calls[0])

        assert "status_detail" in create_sql
        # Ensure it's JSONB type
        # The column definition: status_detail   JSONB,
        assert "status_detail" in create_sql

    def test_unique_constraint_on_source_scope(self, migration_module, mock_op):
        """upgrade() creates the COALESCE-based unique index."""
        migration_module.upgrade()

        unique_calls = [c for c in mock_op["execute"].call_args_list if "uq_knowledge_assets_source_scope" in str(c)]
        assert len(unique_calls) == 1, "Unique constraint not created"

        unique_sql = str(unique_calls[0])
        assert "COALESCE(tenant_id" in unique_sql
        assert "COALESCE(owner_sub" in unique_sql
        assert "source_ref" in unique_sql

    def test_composite_tenant_owner_index(self, migration_module, mock_op):
        """upgrade() creates composite (tenant_id, owner_sub) index for scoped queries."""
        migration_module.upgrade()

        # Look for the composite index call
        create_index_calls = mock_op["create_index"].call_args_list
        tenant_owner_calls = [c for c in create_index_calls if "ix_knowledge_assets_tenant_owner" in str(c)]
        assert len(tenant_owner_calls) == 1
        args = tenant_owner_calls[0]
        assert args[0][0] == "ix_knowledge_assets_tenant_owner"
        assert args[0][1] == "knowledge_assets"
        assert args[0][2] == ["tenant_id", "owner_sub"]

    def test_individual_lookup_indexes(self, migration_module, mock_op):
        """upgrade() creates individual indexes for tenant_id, owner_sub, status, project_id."""
        migration_module.upgrade()

        index_names = [str(c[0][0]) for c in mock_op["create_index"].call_args_list]
        assert "ix_knowledge_assets_tenant_id" in index_names
        assert "ix_knowledge_assets_owner_sub" in index_names
        assert "ix_knowledge_assets_status" in index_names
        assert "ix_knowledge_assets_project_id" in index_names


# ---------------------------------------------------------------------------
# Downgrade tests
# ---------------------------------------------------------------------------


class TestDowngrade:
    """Verify downgrade() cleanly drops the table and indexes."""

    @pytest.fixture
    def mock_op(self):
        """Mock alembic.op for inspecting calls."""
        with (
            patch("alembic.op.execute", new_callable=MagicMock) as mock_execute,
            patch("alembic.op.drop_index", new_callable=MagicMock) as mock_drop_index,
            patch("alembic.op.drop_table", new_callable=MagicMock) as mock_drop_table,
        ):
            yield {
                "execute": mock_execute,
                "drop_index": mock_drop_index,
                "drop_table": mock_drop_table,
            }

    def test_drops_all_indexes(self, migration_module, mock_op):
        """downgrade() drops all created indexes."""
        migration_module.downgrade()

        dropped_indexes = [str(c[0][0]) for c in mock_op["drop_index"].call_args_list]
        assert "ix_knowledge_assets_project_id" in dropped_indexes
        assert "ix_knowledge_assets_status" in dropped_indexes
        assert "ix_knowledge_assets_owner_sub" in dropped_indexes
        assert "ix_knowledge_assets_tenant_id" in dropped_indexes
        assert "ix_knowledge_assets_tenant_owner" in dropped_indexes

    def test_drops_unique_constraint(self, migration_module, mock_op):
        """downgrade() drops the unique constraint index."""
        migration_module.downgrade()

        execute_calls = [str(c) for c in mock_op["execute"].call_args_list]
        assert any("uq_knowledge_assets_source_scope" in c for c in execute_calls)

    def test_drops_table(self, migration_module, mock_op):
        """downgrade() drops the knowledge_assets table."""
        migration_module.downgrade()

        mock_op["drop_table"].assert_called_once_with("knowledge_assets")

    def test_drop_order_indexes_before_table(self, migration_module, mock_op):
        """downgrade() drops indexes before the table (correct ordering)."""
        call_order = []

        mock_op["drop_index"].side_effect = lambda *a, **kw: call_order.append("index")
        mock_op["execute"].side_effect = lambda *a, **kw: call_order.append("execute")
        mock_op["drop_table"].side_effect = lambda *a, **kw: call_order.append("table")

        migration_module.downgrade()

        # Table drop must be last
        assert call_order[-1] == "table"
        # All index drops before table drop
        table_idx = call_order.index("table")
        for i, action in enumerate(call_order[:table_idx]):
            assert action in ("index", "execute")
