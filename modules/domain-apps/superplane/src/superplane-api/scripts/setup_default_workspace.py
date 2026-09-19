#!/usr/bin/env python3
"""Setup default workspace after bootstrap — US-06.

This script is run once after control plane + data plane bootstrap to:
1. Ensure the platform organization exists (or create one)
2. Register the control plane EKS cluster in the clusters table
3. Create the "default" workspace pointing to that cluster
4. Create a default NodePool with sensible defaults

It is IDEMPOTENT — safe to re-run. If the default workspace already exists,
it updates it to Active status and ensures all fields are correct.

Environment variables:
  DATABASE_URL          - PostgreSQL connection string (required)
  EKS_CLUSTER_NAME      - Control plane EKS cluster name (required)
  EKS_CLUSTER_ENDPOINT  - EKS API server endpoint URL (optional)
  EKS_CLUSTER_ARN       - EKS cluster ARN (optional)
  AWS_REGION            - AWS region (default: us-east-1)
  AWS_ACCOUNT_ID        - AWS account ID (optional)
  NEOCLOUD_CLOUDS       - Comma-separated cloud providers (default: aws)
  USE_IAM_AUTH          - Use IAM auth for Aurora (default: false)

Usage:
  python -m scripts.setup_default_workspace
  # or directly:
  python scripts/setup_default_workspace.py
"""

import logging
import os
import sys
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("setup-default-workspace")

# ---------------------------------------------------------------------------
# Configuration from environment
# ---------------------------------------------------------------------------

DATABASE_URL = os.environ.get("DATABASE_URL", "")
EKS_CLUSTER_NAME = os.environ.get("EKS_CLUSTER_NAME", "")
EKS_CLUSTER_ENDPOINT = os.environ.get("EKS_CLUSTER_ENDPOINT", "")
EKS_CLUSTER_ARN = os.environ.get("EKS_CLUSTER_ARN", "")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
AWS_ACCOUNT_ID = os.environ.get("AWS_ACCOUNT_ID", "")
NEOCLOUD_CLOUDS = os.environ.get("NEOCLOUD_CLOUDS", "aws")

# Default workspace constants
DEFAULT_WORKSPACE_NAME = "default"
DEFAULT_ISOLATION_MODE = "dedicated"
DEFAULT_ORG_NAME = "superplane"
DEFAULT_ORG_BILLING_PLAN = "enterprise"

# Budget guardrails from acceptance criteria
BUDGET_MAX_DAILY_USD = Decimal("500.00")
BUDGET_MAX_HOURLY_USD = Decimal("50.00")
BUDGET_MAX_GPUS = 8

# NodePool defaults
NODEPOOL_NAME = "default-pool"
NODEPOOL_MAX_NODES = 4
NODEPOOL_MIN_NODES = 0
NODEPOOL_SPOT_ENABLED = True


def _to_uuid(value: object) -> uuid.UUID:
    """Convert a value (str or UUID) to uuid.UUID."""
    if isinstance(value, uuid.UUID):
        return value
    return uuid.UUID(str(value))


def get_database_url() -> str:
    """Build the database URL, handling IAM auth if needed."""
    url = DATABASE_URL
    if not url:
        logger.error("DATABASE_URL environment variable is required")
        sys.exit(1)

    # For IAM auth, the sync driver is used (not asyncpg)
    # Convert async URLs to sync if needed
    url = url.replace("postgresql+asyncpg://", "postgresql://")

    return url


def ensure_organization(session: Session) -> uuid.UUID:
    """Ensure the platform organization exists. Returns org_id."""
    result = session.execute(
        text("SELECT id FROM organizations WHERE name = :name"),
        {"name": DEFAULT_ORG_NAME},
    )
    row = result.fetchone()

    if row:
        org_id = _to_uuid(row[0])
        logger.info("Organization '%s' already exists: %s", DEFAULT_ORG_NAME, org_id)
        return org_id

    org_id = uuid.uuid4()
    session.execute(
        text(
            "INSERT INTO organizations (id, name, billing_plan, created_at) "
            "VALUES (:id, :name, :billing_plan, :created_at)"
        ),
        {
            "id": str(org_id),
            "name": DEFAULT_ORG_NAME,
            "billing_plan": DEFAULT_ORG_BILLING_PLAN,
            "created_at": datetime.now(timezone.utc),
        },
    )
    logger.info("Created organization '%s': %s", DEFAULT_ORG_NAME, org_id)
    return org_id


def register_cluster(session: Session, org_id: uuid.UUID) -> uuid.UUID:
    """Register the control plane cluster. Returns cluster_id.

    Idempotent: if a cluster with the same name exists for this org, update it.
    """
    result = session.execute(
        text("SELECT id FROM clusters WHERE org_id = :org_id AND name = :name"),
        {"org_id": str(org_id), "name": EKS_CLUSTER_NAME},
    )
    row = result.fetchone()

    now = datetime.now(timezone.utc)

    if row:
        cluster_id = _to_uuid(row[0])
        logger.info("Cluster '%s' already registered: %s", EKS_CLUSTER_NAME, cluster_id)
        # Update endpoint and health
        session.execute(
            text(
                "UPDATE clusters SET "
                "endpoint = :endpoint, "
                "eks_cluster_arn = :arn, "
                "health_status = 'Healthy', "
                "status = 'Active', "
                "last_heartbeat = :now, "
                "updated_at = :now "
                "WHERE id = :id"
            ),
            {
                "endpoint": EKS_CLUSTER_ENDPOINT,
                "arn": EKS_CLUSTER_ARN,
                "now": now,
                "id": str(cluster_id),
            },
        )
        return cluster_id

    cluster_id = uuid.uuid4()
    session.execute(
        text(
            "INSERT INTO clusters "
            "(id, org_id, name, endpoint, eks_cluster_arn, cloud_provider, "
            " cluster_type, status, health_status, last_heartbeat, created_at, updated_at) "
            "VALUES "
            "(:id, :org_id, :name, :endpoint, :arn, 'aws', "
            " 'eks', 'Active', 'Healthy', :now, :now, :now)"
        ),
        {
            "id": str(cluster_id),
            "org_id": str(org_id),
            "name": EKS_CLUSTER_NAME,
            "endpoint": EKS_CLUSTER_ENDPOINT,
            "arn": EKS_CLUSTER_ARN,
            "now": now,
        },
    )
    logger.info("Registered cluster '%s': %s", EKS_CLUSTER_NAME, cluster_id)
    return cluster_id


def create_default_workspace(
    session: Session, org_id: uuid.UUID, cluster_id: uuid.UUID
) -> uuid.UUID:
    """Create the default workspace. Returns workspace_id.

    Idempotent: if a default workspace already exists, update it.
    """
    # Check for existing default workspace (by is_default flag or name)
    result = session.execute(
        text(
            "SELECT id FROM workspaces "
            "WHERE org_id = :org_id AND (is_default = true OR name = :name)"
        ),
        {"org_id": str(org_id), "name": DEFAULT_WORKSPACE_NAME},
    )
    row = result.fetchone()

    now = datetime.now(timezone.utc)

    if row:
        workspace_id = _to_uuid(row[0])
        logger.info("Default workspace already exists: %s — updating", workspace_id)
        session.execute(
            text(
                "UPDATE workspaces SET "
                "name = :name, "
                "isolation_mode = :isolation_mode, "
                "cluster_id = :cluster_id, "
                "is_default = true, "
                "status = 'Active', "
                "budget_max_daily_usd = :budget_daily, "
                "budget_max_hourly_usd = :budget_hourly, "
                "budget_max_gpus = :budget_gpus, "
                "updated_at = :now "
                "WHERE id = :id"
            ),
            {
                "name": DEFAULT_WORKSPACE_NAME,
                "isolation_mode": DEFAULT_ISOLATION_MODE,
                "cluster_id": str(cluster_id),
                "budget_daily": float(BUDGET_MAX_DAILY_USD),
                "budget_hourly": float(BUDGET_MAX_HOURLY_USD),
                "budget_gpus": BUDGET_MAX_GPUS,
                "now": now,
                "id": str(workspace_id),
            },
        )
        return workspace_id

    workspace_id = uuid.uuid4()
    session.execute(
        text(
            "INSERT INTO workspaces "
            "(id, org_id, name, isolation_mode, cluster_id, is_default, "
            " status, budget_max_daily_usd, budget_max_hourly_usd, budget_max_gpus, "
            " created_at, updated_at) "
            "VALUES "
            "(:id, :org_id, :name, :isolation_mode, :cluster_id, true, "
            " 'Active', :budget_daily, :budget_hourly, :budget_gpus, "
            " :now, :now)"
        ),
        {
            "id": str(workspace_id),
            "org_id": str(org_id),
            "name": DEFAULT_WORKSPACE_NAME,
            "isolation_mode": DEFAULT_ISOLATION_MODE,
            "cluster_id": str(cluster_id),
            "budget_daily": float(BUDGET_MAX_DAILY_USD),
            "budget_hourly": float(BUDGET_MAX_HOURLY_USD),
            "budget_gpus": BUDGET_MAX_GPUS,
            "now": now,
        },
    )
    logger.info("Created default workspace: %s", workspace_id)
    return workspace_id


def create_default_nodepool(
    session: Session, org_id: uuid.UUID, cluster_id: uuid.UUID
) -> uuid.UUID:
    """Create the default node pool for the workspace.

    Idempotent: if a node pool with the same name exists, update it.
    """
    result = session.execute(
        text(
            "SELECT id FROM node_pools WHERE cluster_id = :cluster_id AND name = :name"
        ),
        {"cluster_id": str(cluster_id), "name": NODEPOOL_NAME},
    )
    row = result.fetchone()

    now = datetime.now(timezone.utc)
    clouds = NEOCLOUD_CLOUDS  # e.g., "aws" or "aws,nebius,lambda"

    if row:
        nodepool_id = _to_uuid(row[0])
        logger.info("Default node pool already exists: %s — updating", nodepool_id)
        session.execute(
            text(
                "UPDATE node_pools SET "
                "cloud = :cloud, "
                "spot_enabled = :spot, "
                "autoscale_min = :min_nodes, "
                "autoscale_max = :max_nodes, "
                "status = 'Active', "
                "updated_at = :now "
                "WHERE id = :id"
            ),
            {
                "cloud": clouds,
                "spot": NODEPOOL_SPOT_ENABLED,
                "min_nodes": NODEPOOL_MIN_NODES,
                "max_nodes": NODEPOOL_MAX_NODES,
                "now": now,
                "id": str(nodepool_id),
            },
        )
        return nodepool_id

    nodepool_id = uuid.uuid4()
    session.execute(
        text(
            "INSERT INTO node_pools "
            "(id, cluster_id, org_id, name, cloud, spot_enabled, "
            " autoscale_min, autoscale_max, desired_count, actual_count, "
            " status, created_at, updated_at) "
            "VALUES "
            "(:id, :cluster_id, :org_id, :name, :cloud, :spot, "
            " :min_nodes, :max_nodes, 0, 0, "
            " 'Active', :now, :now)"
        ),
        {
            "id": str(nodepool_id),
            "cluster_id": str(cluster_id),
            "org_id": str(org_id),
            "name": NODEPOOL_NAME,
            "cloud": clouds,
            "spot": NODEPOOL_SPOT_ENABLED,
            "min_nodes": NODEPOOL_MIN_NODES,
            "max_nodes": NODEPOOL_MAX_NODES,
            "now": now,
        },
    )
    logger.info(
        "Created default node pool: %s (clouds=%s, spot=%s, max_nodes=%d)",
        nodepool_id,
        clouds,
        NODEPOOL_SPOT_ENABLED,
        NODEPOOL_MAX_NODES,
    )
    return nodepool_id


def main() -> None:
    """Main entry point — run all setup steps."""
    logger.info("=" * 60)
    logger.info("Default Workspace Setup (US-06)")
    logger.info("=" * 60)
    logger.info("EKS Cluster:     %s", EKS_CLUSTER_NAME or "<not set>")
    logger.info("EKS Endpoint:    %s", EKS_CLUSTER_ENDPOINT or "<not set>")
    logger.info("AWS Region:      %s", AWS_REGION)
    logger.info("Clouds:          %s", NEOCLOUD_CLOUDS)
    logger.info("Budget Daily:    $%s", BUDGET_MAX_DAILY_USD)
    logger.info("Budget Hourly:   $%s", BUDGET_MAX_HOURLY_USD)
    logger.info("Budget Max GPUs: %d", BUDGET_MAX_GPUS)
    logger.info("NodePool Max:    %d nodes", NODEPOOL_MAX_NODES)

    if not EKS_CLUSTER_NAME:
        logger.error("EKS_CLUSTER_NAME is required")
        sys.exit(1)

    db_url = get_database_url()
    engine = create_engine(db_url, echo=False)

    with Session(engine) as session:
        try:
            # Step 1: Ensure organization
            logger.info("")
            logger.info("Step 1: Ensure platform organization...")
            org_id = ensure_organization(session)

            # Step 2: Register control plane cluster
            logger.info("")
            logger.info("Step 2: Register control plane cluster...")
            cluster_id = register_cluster(session, org_id)

            # Step 3: Create default workspace
            logger.info("")
            logger.info("Step 3: Create default workspace...")
            workspace_id = create_default_workspace(session, org_id, cluster_id)

            # Step 4: Create default node pool
            logger.info("")
            logger.info("Step 4: Create default node pool...")
            nodepool_id = create_default_nodepool(session, org_id, cluster_id)

            # Commit all changes
            session.commit()

            logger.info("")
            logger.info("=" * 60)
            logger.info("Default workspace setup complete!")
            logger.info("  Organization: %s (%s)", DEFAULT_ORG_NAME, org_id)
            logger.info("  Cluster:      %s (%s)", EKS_CLUSTER_NAME, cluster_id)
            logger.info("  Workspace:    %s (%s)", DEFAULT_WORKSPACE_NAME, workspace_id)
            logger.info("  NodePool:     %s (%s)", NODEPOOL_NAME, nodepool_id)
            logger.info("  Status:       Active")
            logger.info(
                "  Budget:       $%s/day, $%s/hr, %d GPUs max",
                BUDGET_MAX_DAILY_USD,
                BUDGET_MAX_HOURLY_USD,
                BUDGET_MAX_GPUS,
            )
            logger.info("=" * 60)
            logger.info("")
            logger.info("Users can now run:")
            logger.info("  superplane workspace list")
            logger.info("  superplane workspace use default")
            logger.info("  superplane deploy --model <model-name>")

        except Exception:
            session.rollback()
            logger.exception("Failed to set up default workspace")
            sys.exit(1)
        finally:
            engine.dispose()


if __name__ == "__main__":
    main()
