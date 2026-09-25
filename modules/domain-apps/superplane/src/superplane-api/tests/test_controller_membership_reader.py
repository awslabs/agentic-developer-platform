"""Registry reader binding follows current member and credential journal state."""

from datetime import UTC, datetime, timedelta
import uuid

import pytest

from app.models.cluster import Cluster
from app.models.cluster_membership import ClusterMembership
from app.models.membership_credential import MembershipCredential
from app.models.workspace import Workspace
from app.routers.controller_management import reader_membership
from tests.conftest import async_session_test
from tests.test_controller_management import grant
from tests.test_organization_grants import seed


async def shared_reader(monkeypatch, *, state="active"):
    org, _, _ = await seed(monkeypatch)
    workspace_id, cluster_id, request_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    namespace = "sp-ws-" + workspace_id.hex
    async with async_session_test() as db:
        cluster = Cluster(
            id=cluster_id,
            org_id=org,
            workspace_id=uuid.uuid4(),
            name="shared",
            sharing_enabled=True,
            platform_eligible=True,
            status="Ready",
            endpoint="https://shared.example.test",
            eks_cluster_arn="arn:aws:eks:us-east-1:123456789012:cluster/shared",
        )
        workspace = Workspace(
            id=workspace_id,
            org_id=org,
            name="member",
            cluster_id=cluster_id,
            shared_cluster_id=cluster_id,
            namespace_name=namespace,
            isolation_mode="shared",
            status="Ready",
        )
        member = ClusterMembership(
            id=uuid.uuid4(),
            org_id=org,
            workspace_id=workspace_id,
            cluster_id=cluster_id,
            generation="a" * 64,
            namespace=namespace,
            namespace_uid="namespace-uid",
            state="active",
            operation_id=request_id,
        )
        credential = MembershipCredential(
            membership_id=member.id,
            revision=1,
            scope="reader",
            namespace_uid="namespace-uid",
            service_account_uid="sa-uid",
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
            state=state,
            projection_uid="secret-uid",
            projection_version="5",
            observed_at=datetime.now(UTC) if state == "active" else None,
        )
        db.add_all([cluster, workspace, member, credential])
        await db.commit()
        return org, workspace, member, credential, cluster


async def test_registry_exposes_only_current_reader_metadata(client, monkeypatch):
    org, workspace, member, credential, cluster = await shared_reader(monkeypatch)
    headers = grant(monkeypatch, org)
    response = await client.post(
        "/internal/controller/reconcile",
        headers=headers,
        json={
            "org_id": str(org),
            "instance_id": str(uuid.uuid4()),
        },
    )
    assert response.status_code == 200
    target = response.json()["targets"][0]
    assert target["shared_membership"] is True
    assert target["platform_eligible"] is True
    metadata = target["membership_credential"]
    assert metadata["scope"] == "reader" and metadata["revision"] == 1
    assert metadata["namespace_uid"] == member.namespace_uid
    assert metadata["service_account_uid"] == credential.service_account_uid
    assert metadata["workspace_id"] == str(workspace.id)
    assert metadata["cluster_id"] == str(cluster.id)
    assert "token" not in metadata and "shared_cluster_id" not in target


@pytest.mark.parametrize(
    "change",
    ["revoked", "expired", "namespace", "removed", "cluster", "sharing", "mutator"],
)
async def test_reader_binding_refuses_stale_membership_or_credential(
    monkeypatch, change
):
    org, workspace, member, credential, cluster = await shared_reader(monkeypatch)
    async with async_session_test() as db:
        current = await db.get(MembershipCredential, (member.id, 1, "reader"))
        if change == "revoked":
            current.state = "revoked"
        elif change == "expired":
            current.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        elif change == "namespace":
            current.namespace_uid = "replacement"
        elif change == "mutator":
            current.scope = "mutator"
        elif change == "removed":
            (await db.get(ClusterMembership, member.id)).state = "removed"
        elif change == "cluster":
            (await db.get(Workspace, workspace.id)).shared_cluster_id = uuid.uuid4()
        else:
            (await db.get(Cluster, cluster.id)).sharing_enabled = False
        await db.commit()
        assert await reader_membership(db, org, str(workspace.id)) is None


async def test_projected_revision_is_not_normal_authority_and_claim_must_match(
    monkeypatch,
):
    org, workspace, member, _, cluster = await shared_reader(
        monkeypatch, state="projected"
    )
    identity = {
        "membership_generation": member.generation,
        "membership_request_id": str(member.operation_id),
        "membership_cluster_id": str(member.cluster_id),
        "namespace": member.namespace,
        "cluster_arn": cluster.eks_cluster_arn,
    }
    async with async_session_test() as db:
        assert await reader_membership(db, org, str(workspace.id)) is None
        binding = await reader_membership(
            db, org, str(workspace.id), provisional_identity=identity
        )
        assert binding["membership_credential"]["revision"] == 1
        assert (
            await reader_membership(
                db,
                org,
                str(workspace.id),
                provisional_identity={
                    **identity,
                    "membership_request_id": str(uuid.uuid4()),
                },
            )
            is None
        )
        assert (
            await reader_membership(
                db, uuid.uuid4(), str(workspace.id), provisional_identity=identity
            )
            is None
        )
