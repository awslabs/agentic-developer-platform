"""List readiness includes only a workspace's own organization's observations."""

import json
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker
from app.models.organization import Organization
from app.models.cluster import Cluster
from app.models.cloud_account import CloudAccount
from app.models.workspace import Workspace
from app.routers.workspaces import (
    list_workspaces,
    get_workspace,
    workspace_provider_authority,
)
from tests.conftest import engine_test


@pytest.mark.asyncio
async def test_list_and_detail_share_cluster_observation_and_account():
    sessions = async_sessionmaker(engine_test, expire_on_commit=False)
    org_id, other_org = uuid.uuid4(), uuid.uuid4()
    async with sessions() as db:
        db.add_all(
            [
                Organization(id=org_id, name="own"),
                Organization(id=other_org, name="other"),
            ]
        )
        await db.flush()
        account = CloudAccount(
            org_id=org_id,
            provider="aws",
            account_identifier="123456789012",
            friendly_name="demo",
            provisioning_mode="customer_onboarded",
            adp_credential_ids_json=json.dumps(
                ["spda1:12345678-1234-1234-1234-123456789012"]
            ),
        )
        cluster = Cluster(
            org_id=org_id,
            name="actual",
            cloud_provider="aws",
            eks_cluster_arn="arn:aws:eks:us-east-1:123456789012:cluster/actual",
            health_status="Healthy",
            last_heartbeat=datetime.now(UTC),
        )
        foreign = Cluster(
            org_id=other_org,
            name="private",
            health_status="Healthy",
            last_heartbeat=datetime.now(UTC),
        )
        db.add_all([account, cluster, foreign])
        await db.flush()
        workspace = Workspace(
            org_id=org_id,
            name="demo",
            isolation_mode="dedicated",
            status="Active",
            aws_account_id=account.id,
            cluster_id=cluster.id,
            operation_request_json=json.dumps({"region": "us-east-1"}),
        )
        db.add(workspace)
        await db.commit()
        listing = await list_workspaces(None, "workspaces", org_id, db)
        row = next(w for w in listing.workspaces if w.id == workspace.id)
        assert row.cluster_health == "Healthy" and row.last_heartbeat is not None
        assert row.account_id == "123456789012" and row.cluster_region == "us-east-1"
        detail = await get_workspace(workspace.id, org_id, db)
        assert detail.cluster_name == row.cluster_name == "actual"
        provider = await workspace_provider_authority(workspace.id, org_id, db)
        assert provider["managed"] is True and provider["workspace_id"] == str(
            workspace.id
        )
        # Even a corrupt cross-tenant FK cannot leak the other cluster's reading.
        workspace.cluster_id = foreign.id
        await db.commit()
        listing = await list_workspaces(None, "workspaces", org_id, db)
        row = next(w for w in listing.workspaces if w.id == workspace.id)
        assert row.cluster_name is None and row.last_heartbeat is None
