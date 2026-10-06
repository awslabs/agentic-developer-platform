"""Current cluster authority through maintained discovery and PostgreSQL interfaces."""

import asyncio
import uuid
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from app.auth import VerifiedCaller
from app.cluster_authorization import REFUSAL, authorized_cluster_ids
from app.config import settings
from app.current_identity import CurrentIdentity
from app.main import app
from app.models.cluster import Cluster
from app.models.cluster_grant_scope import (
    CLUSTER_PERMISSIONS,
    OrganizationGrantClusterScope,
)
from app.models.cluster_membership import ClusterMembership
from app.models.organization import Organization
from app.models.organization_grant import (
    ORGANIZATION_ADMINISTER,
    ORGANIZATION_READ,
    OrganizationGrantRecord,
)
from app.models.workspace import Workspace
from app.services.cluster_sharing import resolve_shared_target
from app.services.provisioning import ProvisioningRefused
from superplane_auth.policy import DomainPrincipal
from tests.test_auth import _mint
from tests import test_workspace_access_postgres as workspace_fixtures

enforcing = workspace_fixtures.enforcing
installation_postgres_url = workspace_fixtures.installation_postgres_url
postgres_access = workspace_fixtures.postgres_access
pytestmark = workspace_fixtures.pytestmark
rsa_keys = workspace_fixtures.rsa_keys

PATH = "/workspaces?view=eligible-clusters"
ADP_ORG = "adp-transaction-test"


class ClusterIdentities:
    def __init__(self):
        self.identities = {
            ("owner", "human"): CurrentIdentity(
                "owner", "human", ADP_ORG, "owner-membership", True, True
            ),
            ("owner", "service"): CurrentIdentity(
                "owner",
                "service",
                ADP_ORG,
                "service-membership",
                True,
                True,
                "delegation",
            ),
            ("approver", "human"): CurrentIdentity(
                "approver", "human", ADP_ORG, "approver-membership", True, True
            ),
        }

    async def read(self, *, subject, principal_type, adp_org_id):
        return self.identities.get((subject, principal_type))


def caller(org_id, principal_type="human"):
    return VerifiedCaller(
        principal=DomainPrincipal(
            subject="owner",
            org_id=str(org_id),
            client_id="test-client",
            account_type=principal_type,
        ),
        safe_headers={},
        source_org_id=ADP_ORG,
        identity_evidence="owner-membership"
        if principal_type == "human"
        else "service-membership",
    )


@pytest.fixture
async def cluster_access(postgres_access, monkeypatch):
    client, sessions, workspace_id, _, token = postgres_access
    identities = ClusterIdentities()
    monkeypatch.setattr(app.state, "current_identity_reader", identities)
    async with sessions() as session:
        connection = await session.connection()
        await connection.run_sync(
            lambda sync: Organization.metadata.create_all(
                sync,
                tables=[
                    OrganizationGrantRecord.__table__,
                    OrganizationGrantClusterScope.__table__,
                    ClusterMembership.__table__,
                ],
            )
        )
        workspace = await session.get(Workspace, workspace_id)
        org_id = workspace.org_id
        foreign_org = Organization(
            id=uuid.uuid4(), name="foreign", adp_org_id="foreign-adp"
        )
        session.add(foreign_org)
        await session.flush()
        cluster = Cluster(
            org_id=org_id, name="visible", sharing_enabled=True, status="Ready"
        )
        hidden = Cluster(
            org_id=org_id, name="hidden", sharing_enabled=True, status="Ready"
        )
        foreign = Cluster(
            org_id=foreign_org.id, name="foreign", sharing_enabled=True, status="Ready"
        )
        owner = OrganizationGrantRecord(
            org_id=org_id,
            principal="owner",
            principal_type="human",
            permissions=ORGANIZATION_READ,
            granted_by="fixture",
        )
        foreign_owner = OrganizationGrantRecord(
            org_id=foreign_org.id,
            principal="owner",
            principal_type="human",
            permissions=ORGANIZATION_ADMINISTER,
            granted_by="fixture",
        )
        session.add_all(
            [
                cluster,
                hidden,
                foreign,
                owner,
                foreign_owner,
                OrganizationGrantRecord(
                    org_id=org_id,
                    principal="approver",
                    principal_type="human",
                    permissions=ORGANIZATION_ADMINISTER,
                    granted_by="fixture",
                ),
            ]
        )
        await session.flush()
        scope = OrganizationGrantClusterScope(
            org_id=org_id,
            grant_id=owner.id,
            cluster_id=cluster.id,
            permissions="cluster:use",
            generation="a" * 64,
        )
        workspace.cluster_id = hidden.id
        session.add_all(
            [
                scope,
                ClusterMembership(
                    org_id=org_id,
                    workspace_id=workspace_id,
                    cluster_id=hidden.id,
                    generation="b" * 64,
                    namespace="colocated",
                    state="active",
                ),
                OrganizationGrantClusterScope(
                    org_id=foreign_org.id,
                    grant_id=foreign_owner.id,
                    cluster_id=foreign.id,
                    permissions="cluster:use",
                    generation="c" * 64,
                ),
            ]
        )
        await session.commit()
    yield client, sessions, org_id, cluster.id, owner.id, scope.id, identities, token


async def test_discovery_filters_before_metadata_without_scope_inheritance(
    cluster_access,
):
    client, _, _, cluster_id, _, _, _, token = cluster_access
    response = await client.get(PATH, headers=token())
    assert response.status_code == 200, response.text
    assert [
        (row["id"], row["member_count"]) for row in response.json()["clusters"]
    ] == [(str(cluster_id), 0)]
    assert "hidden" not in response.text and "foreign" not in response.text
    response = await client.get(PATH, headers=token("approver"))
    assert response.status_code == 200 and response.json()["clusters"] == []
    assert (await client.get(PATH)).status_code == 401


@pytest.mark.parametrize(
    "permission", sorted(CLUSTER_PERMISSIONS) + ["cluster:*", "workspace:administer"]
)
async def test_cluster_permissions_are_independent_in_api_and_resolver(
    cluster_access, permission
):
    client, sessions, org_id, cluster_id, _, scope_id, identities, token = (
        cluster_access
    )
    async with sessions() as session:
        scope = await session.get(OrganizationGrantClusterScope, scope_id)
        scope.permissions = permission
        await session.commit()
        for requested in CLUSTER_PERMISSIONS:
            permitted = await authorized_cluster_ids(
                session, org_id, caller(org_id), requested, identity_reader=identities
            )
            assert permitted == (
                frozenset({cluster_id}) if permission == requested else frozenset()
            )
    response = await client.get(PATH, headers=token())
    assert response.status_code == 200, response.text
    assert [row["id"] for row in response.json()["clusters"]] == (
        [str(cluster_id)] if permission == "cluster:use" else []
    )


async def test_cluster_only_grant_does_not_replace_organization_route_authority(
    cluster_access,
):
    client, sessions, org_id, cluster_id, grant_id, _, identities, token = (
        cluster_access
    )
    async with sessions() as session:
        grant = await session.get(OrganizationGrantRecord, grant_id)
        grant.permissions = ""
        await session.commit()
        assert await authorized_cluster_ids(
            session, org_id, caller(org_id), "cluster:use", identity_reader=identities
        ) == frozenset({cluster_id})
    assert (await client.get(PATH, headers=token())).status_code == 403


@pytest.mark.parametrize("kind", ["parent", "child"])
async def test_revoked_cluster_authority_is_not_restored_on_retry(cluster_access, kind):
    client, sessions, org_id, cluster_id, grant_id, scope_id, identities, token = (
        cluster_access
    )
    assert (await client.get(PATH, headers=token())).status_code == 200
    async with sessions() as session:
        grant = await session.get(
            OrganizationGrantRecord
            if kind == "parent"
            else OrganizationGrantClusterScope,
            grant_id if kind == "parent" else scope_id,
        )
        grant.revoked_at = datetime.now(UTC)
        await session.commit()
    for _attempt in range(2):
        response = await client.get(PATH, headers=token())
        assert response.status_code == (403 if kind == "parent" else 200)
        assert not response.json().get("clusters")
        async with sessions() as session:
            with pytest.raises(ProvisioningRefused, match=REFUSAL):
                await resolve_shared_target(
                    session,
                    org_id,
                    cluster_id,
                    caller=caller(org_id),
                    identity_reader=identities,
                )


@pytest.mark.parametrize(
    "kind",
    [
        "missing",
        "unavailable",
        "removed",
        "disabled",
        "other-org",
        "wrong-type",
        "new-membership",
    ],
)
async def test_cluster_resolution_rechecks_current_identity_with_ingress_flag_off(
    cluster_access, monkeypatch, kind
):
    client, _, _, _, _, _, identities, token = cluster_access
    monkeypatch.setattr(settings, "current_identity_enforced", False)
    identity = identities.identities[("owner", "human")]
    if kind == "missing":
        monkeypatch.setattr(app.state, "current_identity_reader", None)
    elif kind == "unavailable":

        async def unavailable(**kwargs):
            raise RuntimeError("private reader detail")

        monkeypatch.setattr(identities, "read", unavailable)
    elif kind == "removed":
        identities.identities.clear()
    elif kind == "new-membership":
        monkeypatch.setattr(settings, "current_identity_enforced", True)
        original = identities.read
        calls = 0

        async def replaced_membership(**kwargs):
            nonlocal calls
            calls += 1
            current = await original(**kwargs)
            return (
                current if calls == 1 else replace(current, membership_id="replacement")
            )

        monkeypatch.setattr(identities, "read", replaced_membership)
    else:
        changes = {
            "disabled": {"enabled": False},
            "other-org": {"adp_org_id": "foreign-adp"},
            "wrong-type": {"principal_type": "service", "delegation_id": "delegation"},
        }
        identities.identities[("owner", "human")] = replace(identity, **changes[kind])
    response = await client.get(PATH, headers=token())
    assert response.status_code == 403, response.text
    assert response.json() == {"detail": REFUSAL}
    if kind == "missing":
        assert (await client.get("/workspaces", headers=token())).status_code == 200


async def test_service_requires_own_typed_grant_and_current_delegation(
    cluster_access, enforcing, monkeypatch
):
    client, sessions, _, cluster_id, grant_id, _, identities, token = cluster_access
    headers = {
        "Authorization": f"Bearer {_mint(enforcing, sub='owner', **{'custom:org_id': ADP_ORG, 'custom:account_type': 'service'})}"
    }
    assert (await client.get(PATH, headers=headers)).status_code == 403
    async with sessions() as session:
        grant = await session.get(OrganizationGrantRecord, grant_id)
        grant.principal_type = "service"
        await session.commit()
    response = await client.get(PATH, headers=headers)
    assert response.status_code == 200, response.text
    assert [row["id"] for row in response.json()["clusters"]] == [str(cluster_id)]
    assert (await client.get(PATH, headers=token())).status_code == 403
    monkeypatch.setattr(settings, "current_identity_enforced", False)
    identities.identities[("owner", "service")] = replace(
        identities.identities[("owner", "service")], delegation_id=None
    )
    assert (await client.get(PATH, headers=headers)).status_code == 403


async def test_cluster_scope_rechecked_after_middleware_authorization(
    cluster_access, monkeypatch
):
    from app import cluster_authorization

    client, sessions, _, _, _, scope_id, _, token = cluster_access
    entered, resume = asyncio.Event(), asyncio.Event()
    original = cluster_authorization.require_current_identity

    async def delayed_identity(*args, **kwargs):
        identity = await original(*args, **kwargs)
        entered.set()
        await resume.wait()
        return identity

    monkeypatch.setattr(
        cluster_authorization, "require_current_identity", delayed_identity
    )
    pending = asyncio.create_task(client.get(PATH, headers=token()))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        async with sessions() as session:
            scope = await session.get(OrganizationGrantClusterScope, scope_id)
            scope.revoked_at = datetime.now(UTC)
            await session.commit()
    finally:
        resume.set()
    response = await asyncio.wait_for(pending, timeout=10)
    assert response.status_code == 200 and response.json()["clusters"] == []


@pytest.mark.parametrize(
    "kind", ["membership", "binding", "missing-reader", "wrong-type"]
)
async def test_selection_revalidates_identity_after_discovery(cluster_access, kind):
    client, sessions, org_id, cluster_id, _, _, identities, token = cluster_access
    response = await client.get(PATH, headers=token())
    assert response.status_code == 200 and response.json()["clusters"]
    selected_caller = caller(org_id)
    if kind == "membership":
        identities.identities.clear()
    elif kind == "wrong-type":
        selected_caller = caller(org_id, "service")
    async with sessions() as session:
        if kind == "binding":
            organization = await session.get(Organization, org_id)
            organization.adp_org_id = "different-binding"
            await session.commit()
        with pytest.raises(ProvisioningRefused, match=REFUSAL):
            await resolve_shared_target(
                session,
                org_id,
                cluster_id,
                caller=selected_caller,
                identity_reader=None if kind == "missing-reader" else identities,
            )
