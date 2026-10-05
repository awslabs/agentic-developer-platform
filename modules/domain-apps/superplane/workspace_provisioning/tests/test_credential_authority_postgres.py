"""Real registry takeover/revocation fencing and immutable delegation intent."""
# ruff: noqa: F811

import json
from uuid import UUID, uuid4

import pytest
from superplane_bootstrap.errors import BootstrapRefused
from superplane_bootstrap.membership import SharedMembership

from workspace_provisioning import membership_credential_journal as journal
from workspace_provisioning.credential_controller import components, registry
from workspace_provisioning.member_credentials import (
    CredentialBinding,
    delegation_specs,
)
from workspace_provisioning.shared_membership import reserve
from workspace_bootstrap.tests import conftest as identities

from .test_bootstrap_runtime_postgres import bootstrap_harness  # noqa: F401
from .test_credential_authority import authority_document
from .postgres_bridge import requires_harness_postgres

pytestmark = requires_harness_postgres


def test_registry_lease_and_membership_withdrawal_fence_provider_authority(
    bootstrap_harness,
):
    harness = bootstrap_harness
    cluster_id, workspace_id = str(uuid4()), str(uuid4())
    doc = authority_document(org_id=identities.ORG_ID, cluster_id=cluster_id)
    authority = registry.Authority.read(str(uuid4()), json.dumps(doc))
    member = SharedMembership.create(
        org_id=identities.ORG_ID,
        workspace_id=workspace_id,
        cluster_id=cluster_id,
        request_id=str(uuid4()),
        cluster_arn=doc["target"]["cluster_arn"],
        endpoint=doc["target"]["endpoint"],
    )
    binding = CredentialBinding(member, "original-namespace", 1, "reader")

    async def run():
        async with harness.connect() as c:
            await c.execute(
                "INSERT INTO clusters(id,org_id,name,status,eks_cluster_arn,endpoint,sharing_enabled) VALUES($1,$2,'shared','Ready',$3,$4,true)",
                UUID(cluster_id),
                UUID(member.org_id),
                member.cluster_arn,
                member.endpoint,
            )
            await c.execute(
                "INSERT INTO workspaces(id,org_id,name,isolation_mode,status,is_default) VALUES($1,$2,'member','namespace','Provisioning',false)",
                UUID(workspace_id),
                UUID(member.org_id),
            )
            async with c.transaction():
                await reserve(c, member)
                await journal.reserve(c, binding)
            await c.execute(
                "UPDATE cluster_memberships SET state='active',namespace_uid=$2 WHERE workspace_id=$1",
                UUID(workspace_id),
                binding.namespace_uid,
            )
            await c.execute(
                "UPDATE workspaces SET status='Ready' WHERE id=$1", UUID(workspace_id)
            )
            await c.execute(
                "INSERT INTO cluster_credential_authorities(authority_id,org_id,cluster_id,document_json,enabled) VALUES($1,$2,$3,$4,true)",
                UUID(authority.authority_id),
                UUID(member.org_id),
                UUID(cluster_id),
                authority.document_json,
            )
            assert (
                await registry.load_authority(c, member.org_id, cluster_id) == authority
            )
            fence = await registry.acquire(c, authority, "controller-one")
            await registry.verify(
                c, authority, "controller-one", fence, binding, "issue"
            )
            with pytest.raises(BootstrapRefused, match="lease is held"):
                await registry.acquire(c, authority, "controller-two")
            spec = delegation_specs(binding)[0]
            async with c.transaction():
                row = await components.intent(c, binding, spec)
                await components.observed(
                    c,
                    row,
                    {
                        "uid": "original-sa",
                        "generation": member.generation,
                        "digest": "a" * 64,
                    },
                )
                with pytest.raises(BootstrapRefused, match="identity changed"):
                    await components.observed(c, row, {"uid": "replacement"})
            await c.execute(
                "UPDATE cluster_credential_authorities SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE authority_id=$1",
                UUID(authority.authority_id),
            )
            takeover = await registry.acquire(c, authority, "controller-two")
            assert takeover > fence
            with pytest.raises(BootstrapRefused, match="fenced"):
                await registry.verify(
                    c, authority, "controller-one", fence, binding, "issue"
                )
            with pytest.raises(BootstrapRefused, match="expired or was replaced"):
                await registry.renew(c, authority, "controller-one", fence)
            await registry.verify(
                c, authority, "controller-two", takeover, binding, "issue"
            )
            async with c.transaction():
                await journal.fence_revocation(c, binding)
            with pytest.raises(BootstrapRefused, match="no longer authorizes"):
                await registry.verify(
                    c, authority, "controller-two", takeover, binding, "issue"
                )
            await registry.verify(
                c, authority, "controller-two", takeover, binding, "cleanup"
            )
            await c.execute(
                "UPDATE cluster_credential_authorities SET enabled=false WHERE authority_id=$1",
                UUID(authority.authority_id),
            )
            with pytest.raises(BootstrapRefused, match="revoked or fenced"):
                await registry.verify(
                    c, authority, "controller-two", takeover, binding, "cleanup"
                )

    harness.run(run())
