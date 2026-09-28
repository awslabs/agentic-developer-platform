"""Tests for default workspace setup and deletion protection (US-06)."""

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.workspace import Workspace
from app.schemas.workspace import WorkspaceResponse


class TestDefaultWorkspaceModel:
    """Test workspace model with is_default field."""

    def test_workspace_model_has_is_default(self):
        """Workspace model should have is_default attribute."""
        ws = Workspace()
        assert hasattr(ws, "is_default")

    def test_workspace_model_default_is_false_or_none(self):
        """is_default should default to False (or None before persistence)."""
        ws = Workspace()
        # Before DB persistence, the Python default may be None; after persistence it's False
        assert ws.is_default in (False, None)

    def test_workspace_model_has_budget_hourly(self):
        """Workspace model should have budget_max_hourly_usd attribute."""
        ws = Workspace()
        assert hasattr(ws, "budget_max_hourly_usd")


class TestDefaultWorkspaceSchema:
    """Test workspace schema includes default workspace fields."""

    def test_workspace_response_includes_is_default(self):
        """WorkspaceResponse should include is_default field."""
        now = datetime.now(timezone.utc)
        resp = WorkspaceResponse(
            id=uuid.uuid4(),
            org_id=uuid.uuid4(),
            name="default",
            isolation_mode="dedicated",
            display_name="default",
            status="Active",
            is_default=True,
            budget_max_daily_usd=Decimal("500.00"),
            budget_max_hourly_usd=Decimal("50.00"),
            budget_max_gpus=8,
            created_at=now,
            updated_at=now,
        )
        assert resp.is_default is True
        assert resp.budget_max_daily_usd == Decimal("500.00")
        assert resp.budget_max_hourly_usd == Decimal("50.00")
        assert resp.budget_max_gpus == 8

    def test_workspace_response_default_is_false(self):
        """is_default should default to False in response."""
        now = datetime.now(timezone.utc)
        resp = WorkspaceResponse(
            id=uuid.uuid4(),
            org_id=uuid.uuid4(),
            name="test",
            isolation_mode="dedicated",
            display_name="test",
            status="Active",
            created_at=now,
            updated_at=now,
        )
        assert resp.is_default is False


class TestDefaultWorkspaceDeletionProtection:
    """Test that default workspaces cannot be deleted."""

    @pytest.mark.asyncio
    async def test_delete_default_workspace_returns_403(self, client):
        """Deleting a default workspace should return 403 Forbidden."""
        from app.middleware.auth import create_access_token

        org_id = uuid.uuid4()
        ws_id = uuid.uuid4()
        token, _ = create_access_token(org_id)
        headers = {"Authorization": f"Bearer {token}"}

        # Mock the database to return a default workspace
        mock_ws = MagicMock(spec=Workspace)
        mock_ws.id = ws_id
        mock_ws.org_id = org_id
        mock_ws.name = "default"
        mock_ws.is_default = True
        mock_ws.status = "Active"

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_ws

        with patch("app.routers.workspaces.get_session") as mock_get_session:
            mock_session = AsyncMock()
            mock_session.execute.return_value = mock_result
            mock_get_session.return_value = mock_session

            # Use dependency override instead
            from app.main import app as fastapi_app
            from app.database import get_session as real_get_session

            async def override_session():
                return mock_session

            fastapi_app.dependency_overrides[real_get_session] = override_session

            try:
                response = await client.delete(f"/workspaces/{ws_id}", headers=headers)
                assert response.status_code == 403
                data = response.json()
                assert (
                    "default workspace" in data["detail"].lower()
                    or "cannot delete" in data["detail"].lower()
                )
            finally:
                # Restore the test conftest's session override
                from tests.conftest import _override_get_session

                fastapi_app.dependency_overrides[real_get_session] = (
                    _override_get_session
                )


class TestDefaultWorkspaceBudgetGuardrails:
    """Test budget guardrail values match acceptance criteria."""

    def test_budget_daily_limit(self):
        """Default workspace should have $500/day limit."""
        from scripts.setup_default_workspace import BUDGET_MAX_DAILY_USD

        assert BUDGET_MAX_DAILY_USD == Decimal("500.00")

    def test_budget_hourly_limit(self):
        """Default workspace should have $50/hr limit."""
        from scripts.setup_default_workspace import BUDGET_MAX_HOURLY_USD

        assert BUDGET_MAX_HOURLY_USD == Decimal("50.00")

    def test_budget_max_gpus(self):
        """Default workspace should have max 8 GPUs."""
        from scripts.setup_default_workspace import BUDGET_MAX_GPUS

        assert BUDGET_MAX_GPUS == 8


class TestDefaultWorkspaceNodePool:
    """Test node pool defaults match acceptance criteria."""

    def test_nodepool_max_nodes(self):
        """Default node pool should have max 4 nodes."""
        from scripts.setup_default_workspace import NODEPOOL_MAX_NODES

        assert NODEPOOL_MAX_NODES == 4

    def test_nodepool_spot_preferred(self):
        """Default node pool should prefer spot instances."""
        from scripts.setup_default_workspace import NODEPOOL_SPOT_ENABLED

        assert NODEPOOL_SPOT_ENABLED is True

    def test_nodepool_name(self):
        """Default node pool should be named 'default-pool'."""
        from scripts.setup_default_workspace import NODEPOOL_NAME

        assert NODEPOOL_NAME == "default-pool"


class TestSetupScriptConfiguration:
    """Test setup script configuration values."""

    def test_default_workspace_name(self):
        """Workspace name should be 'default'."""
        from scripts.setup_default_workspace import DEFAULT_WORKSPACE_NAME

        assert DEFAULT_WORKSPACE_NAME == "default"

    def test_default_isolation_mode(self):
        """Isolation mode should be 'dedicated'."""
        from scripts.setup_default_workspace import DEFAULT_ISOLATION_MODE

        assert DEFAULT_ISOLATION_MODE == "dedicated"

    def test_default_org_name(self):
        """Organization should be 'superplane'."""
        from scripts.setup_default_workspace import DEFAULT_ORG_NAME

        assert DEFAULT_ORG_NAME == "superplane"


class TestSetupScriptFunctions:
    """Test setup script database functions with SQLite in-memory."""

    @pytest.fixture
    def db_engine(self):
        """Create an in-memory SQLite database with required tables."""
        from sqlalchemy import create_engine, text

        engine = create_engine("sqlite:///:memory:")
        with engine.connect() as conn:
            # Create minimal tables for testing
            conn.execute(
                text("""
                CREATE TABLE organizations (
                    id TEXT PRIMARY KEY,
                    name TEXT UNIQUE NOT NULL,
                    billing_plan TEXT NOT NULL DEFAULT 'free',
                    quotas_json TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            )
            conn.execute(
                text("""
                CREATE TABLE clusters (
                    id TEXT PRIMARY KEY,
                    org_id TEXT NOT NULL,
                    workspace_id TEXT,
                    name TEXT NOT NULL,
                    endpoint TEXT,
                    eks_cluster_arn TEXT,
                    cloud_provider TEXT,
                    cluster_type TEXT,
                    status TEXT DEFAULT 'Pending',
                    health_status TEXT,
                    last_heartbeat TIMESTAMP,
                    reconcile_at TIMESTAMP,
                    last_reconciled_at TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            )
            conn.execute(
                text("""
                CREATE TABLE workspaces (
                    id TEXT PRIMARY KEY,
                    org_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    isolation_mode TEXT NOT NULL,
                    aws_account_id TEXT,
                    cluster_id TEXT,
                    shared_cluster_id TEXT,
                    namespace_name TEXT,
                    quotas_json TEXT,
                    budget_max_daily_usd REAL,
                    budget_max_hourly_usd REAL,
                    budget_max_gpus INTEGER,
                    agent_iam_role_arn TEXT,
                    is_default BOOLEAN DEFAULT 0,
                    status TEXT DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            )
            conn.execute(
                text("""
                CREATE TABLE node_pools (
                    id TEXT PRIMARY KEY,
                    cluster_id TEXT NOT NULL,
                    org_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    desired_count INTEGER DEFAULT 0,
                    actual_count INTEGER DEFAULT 0,
                    cloud TEXT,
                    gpu_type TEXT,
                    gpu_count_per_node INTEGER,
                    instance_type TEXT,
                    disk_size_gb INTEGER,
                    autoscale_min INTEGER,
                    autoscale_max INTEGER,
                    spot_enabled BOOLEAN DEFAULT 0,
                    status TEXT DEFAULT 'Pending',
                    reconcile_at TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            )
            conn.commit()
        return engine

    def test_ensure_organization_creates(self, db_engine):
        """ensure_organization creates a new org if none exists."""
        from sqlalchemy.orm import Session
        from scripts.setup_default_workspace import ensure_organization

        with Session(db_engine) as session:
            org_id = ensure_organization(session)
            session.commit()
            assert org_id is not None

    def test_ensure_organization_idempotent(self, db_engine):
        """ensure_organization returns the same org on re-run."""
        from sqlalchemy.orm import Session
        from scripts.setup_default_workspace import ensure_organization

        with Session(db_engine) as session:
            org_id_1 = ensure_organization(session)
            session.commit()

        with Session(db_engine) as session:
            org_id_2 = ensure_organization(session)
            session.commit()

        assert org_id_1 == org_id_2

    def test_register_cluster(self, db_engine):
        """register_cluster inserts a cluster row."""
        import os
        from sqlalchemy.orm import Session
        from sqlalchemy import text

        # Set required env vars
        os.environ["EKS_CLUSTER_NAME"] = "test-cluster"
        os.environ["EKS_CLUSTER_ENDPOINT"] = "https://test.eks.amazonaws.com"
        os.environ["EKS_CLUSTER_ARN"] = (
            "arn:aws:eks:us-east-1:123456789012:cluster/test-cluster"
        )

        # Reload module to pick up env vars
        import importlib
        import scripts.setup_default_workspace as setup_mod

        importlib.reload(setup_mod)

        with Session(db_engine) as session:
            org_id = setup_mod.ensure_organization(session)
            cluster_id = setup_mod.register_cluster(session, org_id)
            session.commit()

            # Verify
            result = session.execute(
                text("SELECT name, status FROM clusters WHERE id = :id"),
                {"id": str(cluster_id)},
            )
            row = result.fetchone()
            assert row is not None
            assert row[0] == "test-cluster"
            assert row[1] == "Active"

    def test_create_default_workspace(self, db_engine):
        """create_default_workspace inserts workspace with correct fields."""
        import os
        from sqlalchemy.orm import Session
        from sqlalchemy import text

        os.environ["EKS_CLUSTER_NAME"] = "test-cluster"

        import importlib
        import scripts.setup_default_workspace as setup_mod

        importlib.reload(setup_mod)

        with Session(db_engine) as session:
            org_id = setup_mod.ensure_organization(session)
            cluster_id = setup_mod.register_cluster(session, org_id)
            ws_id = setup_mod.create_default_workspace(session, org_id, cluster_id)
            session.commit()

            # Verify
            result = session.execute(
                text(
                    "SELECT name, status, is_default, isolation_mode FROM workspaces WHERE id = :id"
                ),
                {"id": str(ws_id)},
            )
            row = result.fetchone()
            assert row is not None
            assert row[0] == "default"
            assert row[1] == "Active"
            assert row[2] == 1  # SQLite boolean
            assert row[3] == "dedicated"

    def test_create_default_nodepool(self, db_engine):
        """create_default_nodepool inserts node pool with correct fields."""
        import os
        from sqlalchemy.orm import Session
        from sqlalchemy import text

        os.environ["EKS_CLUSTER_NAME"] = "test-cluster"
        os.environ["NEOCLOUD_CLOUDS"] = "aws,nebius"

        import importlib
        import scripts.setup_default_workspace as setup_mod

        importlib.reload(setup_mod)

        with Session(db_engine) as session:
            org_id = setup_mod.ensure_organization(session)
            cluster_id = setup_mod.register_cluster(session, org_id)
            np_id = setup_mod.create_default_nodepool(session, org_id, cluster_id)
            session.commit()

            # Verify
            result = session.execute(
                text(
                    "SELECT name, spot_enabled, autoscale_max, cloud FROM node_pools WHERE id = :id"
                ),
                {"id": str(np_id)},
            )
            row = result.fetchone()
            assert row is not None
            assert row[0] == "default-pool"
            assert row[1] == 1  # SQLite boolean
            assert row[2] == 4
            assert "aws" in row[3]

    def test_full_setup_idempotent(self, db_engine):
        """Running the full setup twice produces the same result."""
        import os
        from sqlalchemy.orm import Session
        from sqlalchemy import text

        os.environ["EKS_CLUSTER_NAME"] = "test-cluster"

        import importlib
        import scripts.setup_default_workspace as setup_mod

        importlib.reload(setup_mod)

        # First run
        with Session(db_engine) as session:
            org_id = setup_mod.ensure_organization(session)
            cluster_id = setup_mod.register_cluster(session, org_id)
            ws_id_1 = setup_mod.create_default_workspace(session, org_id, cluster_id)
            np_id_1 = setup_mod.create_default_nodepool(session, org_id, cluster_id)
            session.commit()

        # Second run
        with Session(db_engine) as session:
            org_id = setup_mod.ensure_organization(session)
            cluster_id = setup_mod.register_cluster(session, org_id)
            ws_id_2 = setup_mod.create_default_workspace(session, org_id, cluster_id)
            np_id_2 = setup_mod.create_default_nodepool(session, org_id, cluster_id)
            session.commit()

        assert ws_id_1 == ws_id_2
        assert np_id_1 == np_id_2

        # Verify only one workspace and one node pool exist
        with Session(db_engine) as session:
            ws_count = session.execute(text("SELECT COUNT(*) FROM workspaces")).scalar()
            np_count = session.execute(text("SELECT COUNT(*) FROM node_pools")).scalar()
            assert ws_count == 1
            assert np_count == 1
