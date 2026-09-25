"""Rotation retains peer authority and cannot revive revoked credential identity."""
# ruff: noqa: F811

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from superplane_bootstrap.membership import SharedMembership
from workspace_provisioning.member_credentials.binding import CredentialBinding
from workspace_provisioning import membership_credential_journal as journal
from workspace_provisioning.runtime_config import LifecycleRefused
from workspace_provisioning.shared_membership import reserve

from workspace_bootstrap.tests import conftest as identities
from .test_bootstrap_runtime_postgres import bootstrap_harness  # noqa: F401
from .postgres_bridge import requires_harness_postgres

pytestmark = requires_harness_postgres


def test_credential_rotation_requires_exact_observed_projection_and_preserves_peers(
    bootstrap_harness,
):
    harness = bootstrap_harness
    cluster_id = str(uuid4())
    members = [
        SharedMembership.create(
            org_id=identities.ORG_ID,
            workspace_id=str(uuid4()),
            cluster_id=cluster_id,
            request_id=str(uuid4()),
            cluster_arn="arn:aws:eks:us-east-1:123456789012:cluster/shared",
            endpoint="https://shared.example",
        )
        for _ in range(2)
    ]

    async def run():
        async with harness.connect() as c:
            await c.execute(
                "INSERT INTO clusters(id,org_id,name,status,eks_cluster_arn,endpoint,sharing_enabled) "
                "VALUES($1,$2,'shared','Ready',$3,$4,true)",
                UUID(cluster_id),
                UUID(identities.ORG_ID),
                members[0].cluster_arn,
                members[0].endpoint,
            )
            for member in members:
                await c.execute(
                    "INSERT INTO workspaces(id,org_id,name,isolation_mode,status,is_default) "
                    "VALUES($1,$2,$3,'namespace','Provisioning',false)",
                    UUID(member.workspace_id),
                    UUID(member.org_id),
                    member.namespace,
                )
                async with c.transaction():
                    await reserve(c, member)
            first = CredentialBinding(members[0], "ns-first", 1, "reader")
            peer = CredentialBinding(members[1], "ns-peer", 1, "reader")

            async def publish(binding):
                async with c.transaction():
                    await journal.reserve(c, binding)
                    await journal.delegated(
                        c, binding, service_account_uid=binding.service_account
                    )
                    await journal.issued(
                        c,
                        binding,
                        service_account_uid=binding.service_account,
                        expires_at=datetime.now(UTC) + timedelta(minutes=15),
                    )
                    await journal.projection_intent(
                        c,
                        binding,
                        secret_uid="reader-secret",
                        namespace="superplane",
                        namespace_uid="management-ns",
                        secret_name="reader",
                        content_digest="a" * 64,
                    )
                    await journal.projected(
                        c,
                        binding,
                        secret_uid="reader-secret",
                        resource_version="10",
                        content_digest="a" * 64,
                    )
                    await journal.activate(
                        c,
                        binding,
                        service_account_uid=binding.service_account,
                        secret_uid="reader-secret",
                        resource_version="10",
                    )

            await publish(first)
            await publish(peer)
            second = replace(first, revision=2)
            async with c.transaction():
                await journal.reserve(c, second)
                with pytest.raises(LifecycleRefused, match="another issuance"):
                    await journal.reserve(c, replace(first, revision=3))
                await journal.delegated(
                    c, second, service_account_uid=second.service_account
                )
                await journal.issued(
                    c,
                    second,
                    service_account_uid=second.service_account,
                    expires_at=datetime.now(UTC) + timedelta(minutes=15),
                )
                await journal.projection_intent(
                    c,
                    second,
                    secret_uid="reader-secret",
                    namespace="superplane",
                    namespace_uid="management-ns",
                    secret_name="reader",
                    content_digest="b" * 64,
                )
                with pytest.raises(LifecycleRefused, match="original issuance"):
                    await journal.projection_intent(
                        c,
                        second,
                        secret_uid="reader-secret",
                        namespace="superplane",
                        namespace_uid="management-ns",
                        secret_name="reader",
                        content_digest="c" * 64,
                    )
                await journal.projected(
                    c,
                    second,
                    secret_uid="reader-secret",
                    resource_version="11",
                    content_digest="b" * 64,
                )
                with pytest.raises(LifecycleRefused, match="acknowledgement"):
                    await journal.activate(
                        c,
                        second,
                        service_account_uid=first.service_account,
                        secret_uid="reader-secret",
                        resource_version="11",
                    )
                assert (
                    await c.fetchval(
                        "SELECT count(*) FROM membership_credentials WHERE state='active'"
                    )
                    == 2
                )
                await journal.activate(
                    c,
                    second,
                    service_account_uid=second.service_account,
                    secret_uid="reader-secret",
                    resource_version="11",
                )
                with pytest.raises(LifecycleRefused, match="revived"):
                    await journal.reserve(c, first)
            rows = await c.fetch(
                "SELECT m.workspace_id::text,c.revision,c.state FROM membership_credentials c "
                "JOIN cluster_memberships m ON m.id=c.membership_id ORDER BY c.revision"
            )
            assert {(r["workspace_id"], r["revision"], r["state"]) for r in rows} == {
                (members[0].workspace_id, 1, "revoking"),
                (members[0].workspace_id, 2, "active"),
                (members[1].workspace_id, 1, "active"),
            }
            async with c.transaction():
                await c.execute(
                    "UPDATE cluster_memberships SET state='removed' WHERE workspace_id=$1",
                    UUID(members[0].workspace_id),
                )
                with pytest.raises(LifecycleRefused, match="withdrawn"):
                    await journal.reserve(c, replace(first, revision=3))
                await journal.fence_revocation(c, second)
                with pytest.raises(LifecycleRefused, match="ServiceAccount"):
                    await journal.revoked(
                        c, second, service_account_uid=first.service_account
                    )
                # Fixture acknowledgement stands for confirmed provider absence;
                # changing database state alone is not a live revocation claim.
                await journal.revoked(
                    c, second, service_account_uid=second.service_account
                )
                await journal.revoked(
                    c, second, service_account_uid=second.service_account
                )
                assert (
                    await c.fetchval(
                        "SELECT count(*) FROM membership_credentials WHERE state='active'"
                    )
                    == 1
                )

    harness.run(run())
